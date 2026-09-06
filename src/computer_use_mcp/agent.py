"""Closed-loop controller: the ComputerUseAgent phase machine (master-mission section 6).

Pipeline per action::

    OBSERVE -> DECIDE(model) -> GROUND -> VALIDATE(staleness) -> RISK/POLICY -> APPROVAL
            -> EXECUTE(stop-checked) -> RE-OBSERVE -> VERIFY -> RECOVER/REPLAN
            -> CONTINUE | COMPLETE | FAIL-SAFELY

Doctrines implemented here (binding):

- **TaskState-driven**: every status transition, counter, and ``termination_reason`` lives
  on :class:`~computer_use_mcp.state.TaskState`; every exit path terminates with an
  explicit reason (completed / failed_verification / blocked_safety / approval_exhausted /
  limit_exceeded / stopped_by_user / unrecoverable / provider_error).
- **Stop discipline (P0-C)**: the :class:`~computer_use_mcp.state.StopToken` is checked at
  the loop top, before each provider call, before validation capture, before execution,
  inside recovery/dismiss attempts, and during waits. ``TaskStopped`` maps to
  ``stopped_by_user`` with an ``emergency_stop`` audit event. The stop token is NEVER
  reachable from model output: provider decisions are data (an :class:`AgentDecision`),
  not control — nothing a decision carries can arm the token or alter policy.
- **Verification baseline**: the baseline for verification is ALWAYS the observation the
  action was grounded from (the pre-action observation), never an after-screenshot; the
  Wave-0 bug where retries shifted the baseline with the previous after-image is fixed.
  The pre-execution staleness capture checks screen IDENTITY (hwnd/monitor/dimensions/
  coordinate space), not pixels, and never becomes the verification baseline.
- **Uncertain is never success (P0-A)**: a ``verification`` outcome of ``uncertain``
  routes to recovery classification. The single documented exception: ``wait`` actions —
  a wait makes no semantic state claim (the screen may legitimately change or stay still
  during a wait), so ``uncertain`` for ``wait`` continues with an audited note.
- **Confidence separation (Goal.md section 6)**: model confidence
  (``AgentDecision.action.confidence``), grounding confidence
  (``GroundingResult.confidence``), execution success (``ExecutionResult.ok``), and
  verification confidence (``VerificationResult.confidence``) stay separate end-to-end
  and are carried in per-action results.
- **Provider fail-closed**: ANY provider exception becomes an audited failure, a consumed
  step, and a LOW_CONFIDENCE recovery classification — it never raises out of :meth:`run`.
- **Limits**: :class:`~computer_use_mcp.limits.LimitEnforcer` gates screenshots (rate),
  model calls, actions, retries, recovery attempts, context size, and task duration.
  ``LimitExceeded`` is audited and terminates the task cleanly (fail safely).

Verification-intent defaults (from ``action.expected_effect`` +
``ProviderDecision.verification_hint`` + action type):

- explicit hint that names a :class:`~computer_use_mcp.verification.VerificationKind`
  value -> that kind (criteria filled from the expected effect);
- ``type`` -> ``expected_text`` (the typed text must appear in OCR evidence);
- ``move`` -> ``predicate`` (deterministic cursor-at-target: after-cursor within 2 px
  of the requested point on both axes; missing cursor fields -> uncertain);
- ``focus_window`` -> ``window_state`` (the active window title must contain
  ``action.target``, case-insensitive — deterministic via active_window_info, never
  pixels);
- ``keypress``/``hotkey`` whose expected effect says open/launch/switch-to ->
  ``window_state``;
- everything else (click/double_click/drag/scroll/wait/hotkey without a launch-prefix
  effect) -> ``visual_change``; when an expected effect is stated, a change is REQUIRED
  (unchanged screen = failed); with no stated expectation pixels alone stay ambiguous
  (identical screen -> ``uncertain`` -> recovery).
"""

from __future__ import annotations

import base64
import inspect
import io
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from PIL import Image

from .audit import AuditLogger, Metrics
from .backend import ComputerBackend
from .grounding import GroundingRouter
from .limits import LimitEnforcer, LimitExceeded, Limits
from .models import (
    ActionType,
    AgentDecision,
    ExecutionResult,
    FailureClass,
    GroundedAction,
    GroundingResult,
    Observation,
    Point,
    TerminationReason,
    VerificationResult,
)
from .observation import ObservationEngine
from .recovery import (
    RecoveryContext,
    RecoveryController,
    RecoveryStrategy,
    classify_failure,
)
from .safety import SafetyPolicy
from .state import StopToken, TaskState, TaskStatus, TaskStopped
from .validator import (
    COORDINATE_ACTIONS,
    GroundingValidator,
    ProcessIdentityUnavailableError,
    ProcessNotAllowedError,
    ValidationOutcome,
    WindowIdentityUnavailableError,
    _exe_matches,
    _process_matches,
    _title_matches_allowlist,
)
from .verification import VerificationEngine, VerificationIntent, VerificationKind

try:  # E4 lands SafetyContext in parallel; absent -> policy called without context.
    from .safety import SafetyContext  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - pre-E4 state
    SafetyContext = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: Maximum seconds the controller will WAIT for the screenshot-rate gate before failing.
_MAX_SCREENSHOT_WAIT_SECONDS = 2.0
_SCREENSHOT_POLL_SECONDS = 0.05
_HISTORY_WINDOW = 10
_LAUNCH_PREFIXES: tuple[str, ...] = ("open ", "launch ", "start ", "switch to ", "focus ")

#: Cursor-at-target tolerance for the deterministic ``move`` verification predicate (px).
_CURSOR_TOLERANCE_PX = 2

#: Actions for which an ``uncertain`` verification continues the task (documented carve-out).
_TOLERANT_UNCERTAIN_ACTIONS = frozenset({ActionType.WAIT})

#: Cap on the per-agent map of action_id -> suspicious provider content (D3).
_SUSPICIOUS_CONTENT_CAP = 200


def _image_to_base64(image: Image.Image) -> str:
    """Encode a PIL image as base64 PNG for the provider judge callback."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class _ProviderFailure(RuntimeError):
    """Internal sentinel: any provider-layer failure, already audited fail-closed."""


@dataclass
class SingleActionOutcome:
    """Result of one direct (client-supplied) action through the pipeline.

    ``kind`` selects the legacy ``computer_execute`` response shape: ``executed`` carries
    an :class:`ExecutionResult`, ``rejected`` carries validator ``reasons``,
    ``safety_denied``/``approval_required`` carry a policy message, ``error`` is the
    fail-closed catch-all.
    """

    kind: str
    result: ExecutionResult | None = None
    reasons: list[str] = field(default_factory=list)
    message: str = ""
    requires_approval: bool = False
    model_confidence: float | None = None
    grounding_confidence: float | None = None
    verification_confidence: float | None = None


@dataclass
class _FailureControl:
    """Loop control produced by applying a recovery plan."""

    terminate: TerminationReason | None = None
    pending: GroundedAction | None = None
    pending_hint: tuple[str | None, str | None] | None = None
    consumed_retry: bool = False


class ComputerUseAgent:
    """Closed-loop phase machine driving one backend through goals and direct actions.

    Backward-compatible constructor: ``ComputerUseAgent(backend, provider)`` keeps working
    (defaults construct the foundation-layer objects); the server wires per-session
    TaskState/StopToken/LimitEnforcer/AuditLogger/Metrics explicitly.
    """

    def __init__(
        self,
        backend: ComputerBackend,
        provider: Any,
        safety: Any | None = None,
        validator: GroundingValidator | None = None,
        verifier: VerificationEngine | None = None,
        *,
        session_id: str = "unknown",
        task: TaskState | None = None,
        stop: StopToken | None = None,
        limits: Limits | None = None,
        enforcer: LimitEnforcer | None = None,
        auditor: AuditLogger | None = None,
        metrics: Metrics | None = None,
        allowed_processes: list[str] | None = None,
        grounding: GroundingRouter | None = None,
    ) -> None:
        self.backend = backend
        self.provider = provider
        self.safety = safety if safety is not None else SafetyPolicy()
        self.validator = validator if validator is not None else GroundingValidator()
        self.verifier = verifier if verifier is not None else VerificationEngine()
        self.observation = ObservationEngine(backend)
        self.history: list[str] = []
        self.session_id = session_id
        self.task = task if task is not None else TaskState()
        self.stop_token = stop if stop is not None else StopToken()
        self.limits = (limits if limits is not None else Limits()).validate()
        self.enforcer = enforcer if enforcer is not None else LimitEnforcer(self.limits)
        self.auditor = auditor if auditor is not None else AuditLogger()
        self.metrics = metrics if metrics is not None else Metrics()
        self.allowed_processes = list(allowed_processes or [])
        self.grounding = grounding if grounding is not None else GroundingRouter()
        self.approval_denied = False
        self._approved_action_ids: set[str] = set()
        self._approval: Callable[[GroundedAction, str], bool] | None = None
        self.suspicious_contents: dict[str, str] = {}
        self._recovery = RecoveryController(self.enforcer)

    def set_enforcer(self, enforcer: LimitEnforcer) -> None:
        """Swap the per-run limit enforcer (long-running orchestration seam, A5).

        Per-subtask limits (SubtasksProtocol section 3) give each subtask a fresh
        per-subtask budget scope; the recovery controller shares the enforcer's LIVE
        counters, so it is rebuilt alongside. No loop phase, ordering, approval, or
        verification semantics change — this only re-binds which counters gate a run.
        """
        self.enforcer = enforcer
        self._recovery = RecoveryController(enforcer)

    # ------------------------------------------------------------------ audit helpers

    def _active_app(self, observation: Observation | None) -> str | None:
        """Process name of the active window (title fallback) for audit events."""
        if observation is None:
            return None
        info = observation.active_window_info
        if info is not None and info.process_name:
            return info.process_name
        return observation.active_window

    def _audit(
        self,
        event_type: str,
        *,
        observation: Observation | None = None,
        action: GroundedAction | None = None,
        result: str | None = None,
        duration_ms: float | None = None,
        metadata: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        """Emit one audit event; audit failures never break the control loop."""
        merged: dict[str, Any] = dict(metadata or {})
        merged.update(extra)
        try:
            self.auditor.emit(
                str(event_type),
                self.session_id,
                task_id=self.task.task_id,
                observation_id=observation.observation_id if observation is not None else None,
                action_id=action.action_id if action is not None else None,
                active_app=self._active_app(observation),
                result=result,
                duration_ms=duration_ms,
                metadata={k: v for k, v in merged.items() if v not in (None, "")},
            )
        except Exception:
            logger.debug("Audit write failed", exc_info=True)

    # ------------------------------------------------------------------ phase helpers

    def _observe(self, phase: str) -> Observation:
        """OBSERVE phase: rate-gated fresh capture with audit + metrics."""
        started = time.perf_counter()
        waited = 0.0
        while not self.enforcer.can_screenshot():
            if waited > _MAX_SCREENSHOT_WAIT_SECONDS:
                raise LimitExceeded(
                    "min_screenshot_interval_ms",
                    (
                        f"Screenshot rate budget exhausted: interval "
                        f"{self.limits.min_screenshot_interval_ms}ms needs more than "
                        f"{_MAX_SCREENSHOT_WAIT_SECONDS:.0f}s of waiting."
                    ),
                )
            if self.stop_token.wait(_SCREENSHOT_POLL_SECONDS):
                self.stop_token.ensure_live()
            waited += _SCREENSHOT_POLL_SECONDS
        observation = self.observation.capture()
        self.enforcer.record_screenshot()
        self.metrics.incr("screenshot_count")
        duration_ms = (time.perf_counter() - started) * 1000.0
        self.metrics.record_latency("observation_ms", duration_ms)
        self.task.record_observation_id(observation.observation_id)
        self._audit("observation", observation=observation, result="ok", duration_ms=duration_ms, phase=phase)
        return observation

    async def _call_provider(self, goal: str, observation: Observation) -> Any:
        """Invoke ``decide_full`` (pinned E4 API) or fall back to legacy ``decide``."""
        decide_full = getattr(self.provider, "decide_full", None)
        if callable(decide_full):
            raw = decide_full(goal, observation, self.history)
            if inspect.isawaitable(raw):
                raw = await raw
            return raw
        decide = getattr(self.provider, "decide", None)
        if not callable(decide):
            raise TypeError("Provider exposes neither decide_full nor decide.")
        raw = decide(goal, observation, self.history)
        if inspect.isawaitable(raw):
            raw = await raw
        return raw

    async def _decide(
        self, goal: str, observation: Observation
    ) -> tuple[AgentDecision, str | None, str | None, Any]:
        """DECIDE phase: provider call wrapped fail-closed; returns normalized decision."""
        self.enforcer.check_model_call()
        self.stop_token.ensure_live()
        self.enforcer.check_context_items(len(self.history) + 1)
        started = time.perf_counter()
        try:
            raw = await self._call_provider(goal, observation)
        except Exception as exc:
            self.enforcer.record_model_call()
            self.metrics.incr("model_calls")
            self._audit(
                "failure",
                observation=observation,
                result="provider_error",
                metadata={
                    "phase": "decide",
                    "exception": type(exc).__name__,
                    "detail": str(exc)[:300],
                },
            )
            raise _ProviderFailure(f"Provider call failed: {type(exc).__name__}: {exc}") from exc
        self.enforcer.record_model_call()
        self.metrics.incr("model_calls")
        self.task.model_call_count += 1
        duration_ms = (time.perf_counter() - started) * 1000.0
        self.metrics.record_latency("model_ms", duration_ms)

        decision: AgentDecision | None
        if isinstance(raw, AgentDecision):
            decision = raw
        else:
            decision = getattr(raw, "decision", None)
        if decision is None and hasattr(raw, "status"):
            decision = raw  # duck-typed decision object (test fakes, alternate providers)
        if decision is None:
            self._audit(
                "failure",
                observation=observation,
                result="provider_error",
                metadata={"phase": "decide", "detail": "Provider returned no decision payload."},
            )
            raise _ProviderFailure("Provider returned no decision payload.")
        hint = getattr(raw, "verification_hint", None) if raw is not decision else None
        hint = hint or getattr(decision, "verification_hint", None)
        effect = getattr(raw, "expected_effect", None) or getattr(decision, "expected_change", None)
        suspicious = getattr(raw, "suspicious_content", None)
        # D3: persist provider-suspicious content as DATA (never control): audited with
        # redaction applied at the sink, and surfaced on the per-action result dict.
        suspicious_text = str(suspicious)[:300] if suspicious else ""
        if suspicious_text and decision.action is not None:
            if len(self.suspicious_contents) >= _SUSPICIOUS_CONTENT_CAP:
                self.suspicious_contents.pop(next(iter(self.suspicious_contents)))
            self.suspicious_contents[decision.action.action_id] = suspicious_text
        self.history.append(f"decision={decision.status}; summary={decision.summary}")
        self.task.add_plan_note(str(decision.summary)[:200])
        self._audit(
            "model_decision",
            observation=observation,
            action=decision.action,
            result=str(decision.status),
            duration_ms=duration_ms,
            metadata={
                "summary": str(decision.summary)[:300],
                "expected_effect": effect or "",
                "verification_hint": hint or "",
                # D3: the boolean flag stays for audit compat (E6 schema); the FULL text
                # is now persisted alongside it (redaction applies at the sink) so the
                # audit no longer collapses suspicious content to a bare boolean.
                "suspicious_content": bool(suspicious),
                "suspicious_content_detail": suspicious_text,
            },
        )
        return decision, hint, effect, suspicious

    def _ground(self, action: GroundedAction, observation: Observation) -> GroundingResult:
        """GROUND phase: route grounding, attach the result, bind the observation source."""
        grounding = self.grounding.route(action, observation)
        action.grounding = grounding
        if action.action in COORDINATE_ACTIONS:
            action.source_observation_id = observation.observation_id
        self._audit(
            "grounding",
            observation=observation,
            action=action,
            result="ok",
            metadata={
                "strategy": grounding.strategy,
                "grounding_confidence": grounding.confidence,
                "normalized": grounding.normalized,
            },
        )
        return grounding

    def _build_safety_context(self, observation: Observation | None) -> Any | None:
        """Build E4's pinned ``SafetyContext`` when the parallel landing provides it."""
        if SafetyContext is None:
            return None
        info = observation.active_window_info if observation is not None else None
        kwargs = {
            "goal": self.task.goal,
            "active_window_info": info,
            "active_process_name": info.process_name if info is not None else None,
            "window_title": (info.title if info is not None else None)
            or (observation.active_window if observation is not None else None),
            "task_summary": (self.task.goal or "")[:200],
            "recent_actions": [record.action_type for record in list(self.task.action_history)[-5:]],
        }
        try:
            return SafetyContext(**kwargs)
        except Exception:  # noqa: BLE001 - context construction must never break control
            return None

    def _denied_decision(self, reason: str) -> Any:
        """Fail-closed SafetyDecision-compatible object when the policy itself fails."""
        try:
            from .safety import SafetyDecision

            return SafetyDecision(allowed=False, requires_approval=False, reason=reason)
        except Exception:  # noqa: BLE001 - E4 field drift tolerated via shim
            return SimpleNamespace(allowed=False, requires_approval=False, reason=reason)

    def _evaluate_safety(self, action: GroundedAction, state: Any, observation: Observation | None) -> Any:
        """RISK/POLICY phase: contextual evaluation; a failing policy denies (fail-closed)."""
        context = self._build_safety_context(observation)
        try:
            if context is not None:
                try:
                    return self.safety.evaluate(action, state, context)
                except TypeError:
                    pass  # pre-E4 policy signature without context
            return self.safety.evaluate(action, state)
        except Exception as exc:  # noqa: BLE001 - policy failure denies, never crashes
            return self._denied_decision(f"Safety evaluation failed; failing closed: {exc}")

    def _build_intent(
        self,
        action: GroundedAction,
        verification_hint: str | None,
        expected_effect: str | None,
    ) -> VerificationIntent:
        """Build the semantic verification intent for an action (see module docstring)."""
        hint = (verification_hint or "").strip().casefold()
        effect = (expected_effect or action.expected_effect or "").strip() or None
        valid_kinds = {kind.value for kind in VerificationKind}
        kind = hint if hint in valid_kinds else None
        expected_text: str | None = None
        window_title: str | None = None
        window_title_match = "contains"  # case-insensitive substring (WindowStateStrategy)
        process_name: str | None = None
        expected_change: bool | None = None
        predicate: Callable[[Observation, Observation], bool | None] | None = None
        predicate_name: str | None = None
        if kind is None:
            if action.action is ActionType.TYPE:
                kind = VerificationKind.EXPECTED_TEXT.value
            elif action.action is ActionType.MOVE:
                # Deterministic cursor-at-target predicate: verified when the after
                # cursor sits within tolerance of the requested point on both axes.
                # Missing cursor fields yield None -> uncertain (never false success).
                kind = VerificationKind.PREDICATE.value
                point = action.point

                def _cursor_at_target(
                    _before: Observation, after: Observation, point: Point | None = point
                ) -> bool | None:
                    if point is None or after.cursor_x is None or after.cursor_y is None:
                        return None
                    return (
                        abs(after.cursor_x - point.x) <= _CURSOR_TOLERANCE_PX
                        and abs(after.cursor_y - point.y) <= _CURSOR_TOLERANCE_PX
                    )

                predicate = _cursor_at_target
                predicate_name = "cursor_at_target"
            elif action.action in {ActionType.KEYPRESS, ActionType.HOTKEY} and effect:
                folded = effect.casefold()
                for prefix in _LAUNCH_PREFIXES:
                    if folded.startswith(prefix) and len(effect) > len(prefix):
                        kind = VerificationKind.WINDOW_STATE.value
                        window_title = effect[len(prefix):].strip() or None
                        break
            elif action.action is ActionType.FOCUS_WINDOW:
                # Deterministic window identity via active_window_info, never pixels;
                # the title match is case-insensitive "contains" (WindowStateStrategy).
                kind = VerificationKind.WINDOW_STATE.value
                window_title = action.target
            if kind is None:
                kind = VerificationKind.VISUAL_CHANGE.value
        if kind == VerificationKind.EXPECTED_TEXT.value:
            expected_text = effect or action.text
        elif kind == VerificationKind.WINDOW_STATE.value:
            window_title = window_title or effect
        elif kind == VerificationKind.PROCESS_STATE.value:
            process_name = effect
        elif kind == VerificationKind.VISUAL_CHANGE.value and effect:
            expected_change = True
        return VerificationIntent(
            kind=kind,
            expected_text=expected_text,
            expected_window_title=window_title,
            window_title_match=window_title_match,
            expected_process_name=process_name,
            expected_change=expected_change,
            expected_effect=effect,
            predicate=predicate,
            predicate_name=predicate_name,
            metadata={"action_id": action.action_id, "verification_hint": hint},
        )

    def _focus_allowlist_rejection(
        self, action: GroundedAction, state: Any | None = None
    ) -> ValidationOutcome | None:
        """Agent-level focus allowlist gate (fail-closed), WRONG_WINDOW-shaped.

        ``focus_window`` resolves its target window BEFORE execution (the backend must
        not run at all for a disallowed target), so neither allowlist can be checked
        from the active-window observation the way other actions do. When the action is
        a focus_window, the target window is looked up through the backend and checked
        against BOTH configured allowlists:

        - process allowlist (``self.allowed_processes``): window not found -> code
          ``process_identity_unavailable`` (fail closed); found but its process/exe is
          outside the allowlist (validator matching semantics) -> code
          ``process_not_allowed``;
        - window-title allowlist (``state.allowed_windows`` — the same effective
          allowlist the validator enforces on the ACTIVE window): window not found ->
          code ``window_identity_unavailable`` (fail closed); found but its title is
          outside the allowlist (casefolded exact-or-substring, mirroring
          ``GroundingValidator._window_allowed``) -> code ``window_not_allowed``.

        The rejection reuses the validator's rejection codes and typed errors so
        callers get the same rejection shape and recovery mapping
        (``FailureClass.WRONG_WINDOW``) as ordinary allowlist violations. Returns
        ``None`` (gate skipped) when neither allowlist is configured or the action is
        not a focus_window.
        """
        if action.action is not ActionType.FOCUS_WINDOW:
            return None
        target = (action.target or "").strip()
        if self.allowed_processes:
            process_rejection = self._focus_process_allowlist_rejection(target)
            if process_rejection is not None:
                return process_rejection
        allowed_windows = list(getattr(state, "allowed_windows", None) or [])
        if allowed_windows:
            candidate = self.backend.find_window_by_title(target)
            if candidate is None:
                outcome = ValidationOutcome(
                    valid=False,
                    reasons=[
                        (
                            "focus_window target window could not be resolved while a "
                            "window-title allowlist is configured; failing closed."
                        )
                    ],
                    codes=["window_identity_unavailable"],
                )
                outcome._error = WindowIdentityUnavailableError(
                    f"Window {target!r} could not be resolved; cannot verify the window-title allowlist."
                )
                return outcome
            if not _title_matches_allowlist(candidate.title, allowed_windows):
                outcome = ValidationOutcome(
                    valid=False,
                    reasons=[
                        (
                            f"Target window title {candidate.title!r} is not in the "
                            "configured window allowlist."
                        )
                    ],
                    codes=["window_not_allowed"],
                )
                return outcome
        return None

    def _focus_process_allowlist_rejection(self, target: str) -> ValidationOutcome | None:
        """Process-allowlist half of the focus gate (semantics unchanged)."""
        candidate = self.backend.find_window_by_title(target)
        if candidate is None:
            outcome = ValidationOutcome(
                valid=False,
                reasons=[
                    (
                        "focus_window target window could not be resolved while a process "
                        "allowlist is configured; failing closed."
                    )
                ],
                codes=["process_identity_unavailable"],
            )
            outcome._error = ProcessIdentityUnavailableError(
                f"Window {target!r} could not be resolved; cannot verify the process allowlist."
            )
            return outcome
        allowed = any(
            (candidate.process_name and _process_matches(candidate.process_name, pattern))
            or (candidate.exe_path and _exe_matches(candidate.exe_path, pattern))
            for pattern in self.allowed_processes
        )
        if allowed:
            return None
        identity = candidate.process_name or candidate.exe_path
        if identity is None:
            outcome = ValidationOutcome(
                valid=False,
                reasons=[
                    (
                        "focus_window target window carries no process identity; the process "
                        "allowlist cannot be verified."
                    )
                ],
                codes=["process_identity_unavailable"],
            )
            outcome._error = ProcessIdentityUnavailableError(
                f"Window {target!r} has no process identity; cannot verify the process allowlist."
            )
            return outcome
        outcome = ValidationOutcome(
            valid=False,
            reasons=[
                f"Target window process {identity!r} is not in the configured process allowlist."
            ],
            codes=["process_not_allowed"],
        )
        outcome._error = ProcessNotAllowedError(
            f"Target window process {identity!r} is not in the configured process allowlist."
        )
        return outcome

    def _verifier_has_judge(self) -> bool:
        """True when the injected verifier chain carries a synchronous model judge."""
        for strategy in getattr(self.verifier, "strategies", []) or []:
            if getattr(strategy, "judge", None) is not None:
                return True
        return False

    async def _provider_judge(
        self, intent: VerificationIntent, before: Observation, after: Observation
    ) -> VerificationResult:
        """MODEL_JUDGE via the pinned ``provider.judge_change`` callback, fail-closed."""
        judge = getattr(self.provider, "judge_change", None)
        if not callable(judge):
            return VerificationResult(
                outcome="uncertain",
                changed=False,
                note="No provider judge_change is available; model-based verification is unavailable.",
                confidence=0.0,
                verification_method="model_visual",
                observation_id=after.observation_id,
            )
        try:
            from .verification import ScreenshotDiffStrategy

            before_b64 = _image_to_base64(ScreenshotDiffStrategy._decode(before.image_base64))
            after_b64 = _image_to_base64(ScreenshotDiffStrategy._decode(after.image_base64))
            raw = judge(before_b64, after_b64, intent.expected_effect or "")
            if inspect.isawaitable(raw):
                raw = await raw
        except Exception as exc:  # noqa: BLE001 - judge failure degrades to uncertain
            return VerificationResult(
                outcome="uncertain",
                changed=False,
                note=f"Provider judge failed: {type(exc).__name__}.",
                confidence=0.0,
                verification_method="model_visual",
                observation_id=after.observation_id,
            )
        data = raw if isinstance(raw, dict) else {
            key: getattr(raw, key, None) for key in ("outcome", "confidence", "reason")
        }
        outcome = str(data.get("outcome", "uncertain")).casefold()
        if outcome not in {"verified", "failed", "uncertain"}:
            outcome = "uncertain"
        try:
            confidence = min(max(float(data.get("confidence") or 0.0), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = 0.0
        note = str(data.get("reason") or data.get("note") or "")[:500]
        return VerificationResult(
            outcome=outcome,
            changed=outcome == "verified",
            note=note or f"Provider judge outcome: {outcome}.",
            confidence=confidence,
            verification_method="provider_judge",
            observation_id=after.observation_id,
        )

    async def _verify(
        self,
        intent: VerificationIntent,
        before: Observation,
        after: Observation,
        action: GroundedAction | None = None,
    ) -> VerificationResult:
        """VERIFY phase: strategy chain or provider judge; uncertain never becomes success."""
        started = time.perf_counter()
        try:
            if intent.kind == VerificationKind.MODEL_JUDGE.value and not self._verifier_has_judge():
                result = await self._provider_judge(intent, before, after)
            else:
                result = self.verifier.verify(intent, before, after)
        except Exception as exc:  # noqa: BLE001 - verification failure is never success
            result = VerificationResult(
                outcome="uncertain",
                changed=False,
                note=f"Verification raised {type(exc).__name__}; treated as cannot-determine.",
                confidence=0.0,
                verification_method="controller_guard",
                observation_id=after.observation_id,
            )
        duration_ms = (time.perf_counter() - started) * 1000.0
        self.metrics.record_latency("verification_ms", duration_ms)
        if result.outcome == "verified":
            self.metrics.incr("verification_verified")
        elif result.outcome == "failed":
            self.metrics.incr("verification_failed")
        else:
            self.metrics.incr("verification_uncertain")
        self._audit(
            "verification",
            observation=after,
            action=action,
            result=result.outcome,
            duration_ms=duration_ms,
            metadata={
                "method": result.verification_method,
                "verification_confidence": result.confidence,
                "note": result.note[:200],
                "intent_kind": intent.kind,
            },
        )
        return result

    # ------------------------------------------------------------------ recovery wiring

    def _dismiss_allowed(self, state: Any) -> bool:
        """Policy probe: does the safety policy permit the Escape-key dismiss input?"""
        probe = GroundedAction(
            action=ActionType.KEYPRESS,
            keys=["esc"],
            reason="Recovery policy probe for the bounded Escape dismiss",
            confidence=1.0,
        )
        decision = self._evaluate_safety(probe, state, None)
        return bool(decision.allowed)

    def _attempt_dismiss(self, state: Any, source: Observation | None) -> bool:
        """Single bounded BLOCKED_UI dismiss: press Escape via the (stop-checked) backend.

        Accounting doctrine (D6, documented): EVERY dismiss attempt increments the
        ``recovery_dismiss_total`` metric and ``action_total``. A SUCCESSFUL dismiss also
        calls :meth:`LimitEnforcer.record_action` and increments ``action_success`` (a
        real, stop-checked physical input happened). A FAILED attempt increments
        ``action_failure`` and consumes no enforcer action budget (no input occurred —
        consistent with the main execute path, which records only successful inputs).
        This keeps ``action_total == action_success + action_failure`` and keeps the
        enforcer budget truthful. Dismiss inputs are recorded against the action budget
        but never GATED by ``check_action()``: recovery machinery must not be blockable
        by the model-action budget (the bounded recovery budget gates dismisses instead).
        """
        dismiss_action = GroundedAction(
            action=ActionType.KEYPRESS,
            keys=["esc"],
            reason="Recovery: dismiss blocking UI (single bounded attempt)",
            confidence=1.0,
        )
        self.metrics.incr("recovery_dismiss_total")
        try:
            self.backend.execute(dismiss_action, self.stop_token)
        except TaskStopped:
            raise
        except Exception as exc:  # noqa: BLE001 - failed dismiss falls back to replanning
            self.metrics.incr("action_total")
            self.metrics.incr("action_failure")
            self._audit(
                "recovery",
                observation=source,
                action=dismiss_action,
                result="dismiss_failed",
                metadata={"exception": type(exc).__name__, "detail": str(exc)[:200]},
            )
            return False
        state.step_count += 1
        self.task.step_count += 1
        self.task.record_action(dismiss_action)
        self.enforcer.record_action()
        self.metrics.incr("action_total")
        self.metrics.incr("action_success")
        self._audit(
            "recovery",
            observation=source,
            action=dismiss_action,
            result="dismissed",
            metadata={"detail": "Escape dismissed the blocking UI; retrying the same action."},
        )
        return True

    def _handle_failure(
        self,
        failure: Any,
        *,
        phase: str,
        action: GroundedAction | None,
        source: Observation | None,
        after: Observation | None,
        state: Any,
        results: list[ExecutionResult],
        retried: bool,
        hint: tuple[str | None, str | None],
        failure_class_override: Any | None = None,
    ) -> _FailureControl:
        """Classify one failure and apply its bounded recovery plan (P0-B)."""
        if isinstance(failure, TaskStopped):
            raise failure  # a stop is the kill path, never a recovery candidate
        context = RecoveryContext(
            phase=phase,
            action=action,
            message=str(failure)[:500],
            source_observation=source,
            after_observation=after,
            failure_class=failure_class_override,
            dismiss_allowed=self._dismiss_allowed(state),
            goal=self.task.goal,
        )
        failure_class = classify_failure(failure, context)
        if failure_class is None:  # defensive: TaskStopped handled above
            raise TaskStopped("Task stop requested; refusing to continue.")
        plan = self._recovery.handle(failure_class, context)
        if isinstance(failure, BaseException):
            root_cause = f"{type(failure).__name__}: {failure}"
        else:
            root_cause = str(failure)
        self._audit(
            "recovery",
            observation=source,
            action=action,
            result=plan.strategy.value,
            metadata={
                "failure_class": failure_class.value,
                "reason": plan.reason[:300],
                "phase": phase,
            },
        )

        if plan.strategy is RecoveryStrategy.TERMINATE_SAFELY:
            terminal_message = plan.reason
            if root_cause:
                # Terminal results carry the root cause too (E6 contract: the failing
                # exception class must be traceable from the result message alone).
                terminal_message = f"{plan.reason} Root cause: {root_cause[:200]}"
            self._audit(
                "failure",
                observation=source,
                action=action,
                result=str(plan.termination_reason.value if plan.termination_reason else "unrecoverable"),
                metadata={"failure_class": failure_class.value, "reason": plan.reason[:300]},
            )
            results.append(
                ExecutionResult(
                    ok=False,
                    action=action or GroundedAction(action="done"),
                    message=terminal_message,
                )
            )
            return _FailureControl(terminate=plan.termination_reason or TerminationReason.UNRECOVERABLE)

        if plan.strategy is RecoveryStrategy.COMPLETE:
            results.append(
                ExecutionResult(
                    ok=True,
                    action=GroundedAction(action="done"),
                    message="Goal state was already reached.",
                    verification=VerificationResult(
                        outcome="verified",
                        changed=False,
                        note="Goal state already reached; no further action required.",
                        confidence=0.9,
                        verification_method="recovery_complete",
                    ),
                )
            )
            return _FailureControl(terminate=TerminationReason.COMPLETED)

        # Recoverable strategies consume the bounded recovery budget (per-action + per-task).
        self.enforcer.record_recovery()
        self.metrics.incr("recovery_total")
        self.task.recovery_attempts_task += 1
        self.task.recovery_attempts_action += 1
        self.task.status = TaskStatus.RECOVERING

        if plan.strategy in {RecoveryStrategy.RECOVER_REOBSERVE, RecoveryStrategy.REPLAN}:
            # Re-observe + re-decide: the failed action instance and its coordinates are
            # discarded; the model is re-consulted with the failure summary in history.
            self.history.append(
                f"recovery={failure_class.value}; phase={phase}; detail={str(failure)[:150]}"
            )
            return _FailureControl(pending=None, pending_hint=(None, None))

        if plan.strategy is RecoveryStrategy.RECOVER_DISMISS:
            if self._attempt_dismiss(state, source):
                return _FailureControl(pending=action, pending_hint=hint)
            self.history.append(
                f"recovery=dismiss_failed; phase={phase}; detail={str(failure)[:150]}"
            )
            return _FailureControl(pending=None, pending_hint=(None, None))

        # RETRY_ONCE: same approved action instance, fresh grounding + validation.
        if action is None or retried:
            self.history.append(
                f"recovery={failure_class.value}; fallback=replan; phase={phase}"
            )
            return _FailureControl(pending=None, pending_hint=(None, None))
        self.enforcer.check_retry()  # LimitExceeded propagates -> clean limit termination
        self.enforcer.record_retry()
        self.metrics.incr("retry_total")
        self.history.append(
            f"recovery={failure_class.value}; retry=same_instance; phase={phase}"
        )
        return _FailureControl(pending=action, pending_hint=hint, consumed_retry=True)

    # ------------------------------------------------------------------ result builders

    @staticmethod
    def _done_result(decision: AgentDecision) -> ExecutionResult:
        """Provider-declared completion (legacy shape preserved, honest marking — F3).

        The CUA loop still treats provider ``status=done`` as completion, but the
        synthetic verification explicitly states that the outcome is MODEL-ASSERTED
        without independent verification evidence — it is never presented as an
        evidenced check (Goal.md section 28: goal completion requires evidence).
        """
        return ExecutionResult(
            ok=True,
            action=GroundedAction(action="done"),
            message=decision.summary or "Goal completed.",
            verification=VerificationResult(
                verified=True,
                changed=False,
                note=(
                    "Provider declared completion. This is a MODEL-ASSERTED outcome "
                    "(completion_evidence=model_declared) without independent verification "
                    "evidence; no deterministic check confirmed the goal state."
                ),
                confidence=0.8,
                evidence=[
                    "provider status=done (model assertion)",
                    "no independent verification evidence was collected",
                ],
                verification_method="provider_done",
            ),
        )

    @staticmethod
    def _blocked_result(decision: AgentDecision) -> ExecutionResult:
        """Model refused/failed to propose an action (legacy shape preserved)."""
        return ExecutionResult(
            ok=False,
            action=GroundedAction(action="done"),
            message=decision.summary or "Provider blocked.",
        )

    @staticmethod
    def _stopped_result() -> ExecutionResult:
        return ExecutionResult(
            ok=False,
            action=GroundedAction(action="done"),
            message="Task stopped by user; execution halted safely.",
        )

    # ------------------------------------------------------------------ main loop

    async def run(
        self,
        goal: str,
        state: Any,
        approval: Callable[[GroundedAction, str], bool] | None = None,
    ) -> list[ExecutionResult]:
        """Run the closed-loop phase machine for ``goal`` (legacy signature preserved).

        ``approval`` is the legacy per-action callback; a granted approval is bound to the
        approved action INSTANCE id, so bounded recovery retries of the same instance do
        not re-consume budget, while a new distinct action after exhaustion is denied
        fail-closed (compat decision 3).
        """
        results: list[ExecutionResult] = []
        task = self.task
        task.goal = goal or task.goal
        task.status = TaskStatus.RUNNING
        self._approval = approval
        self.approval_denied = False
        self._approved_action_ids = set()
        self.suspicious_contents = {}
        self.metrics.incr("task_started")
        self._audit("session_start", result="task_started", metadata={"goal": (task.goal or "")[:200]})

        started = time.perf_counter()
        termination: TerminationReason | None = None
        pending: GroundedAction | None = None
        pending_hint: tuple[str | None, str | None] = (None, None)
        retried_low_confidence = False
        last_attempt_was_recovery = False
        attempt_count = 0

        try:
            for _loop_step in range(max(int(getattr(state, "max_steps", 30)), 1)):
                if getattr(state, "stopped", False) or self.stop_token.stopped:
                    termination = TerminationReason.STOPPED_BY_USER
                    results.append(self._stopped_result())  # never a vacuously-ok empty list
                    break
                self.stop_token.ensure_live()  # P0-C: loop top
                self.enforcer.check_task_duration()
                try:
                    observation = self._observe("loop_top")
                except (TaskStopped, LimitExceeded):
                    raise  # kill path and limit gates terminate; never recovery candidates
                except Exception as exc:  # noqa: BLE001 - observe failures ARE classified (D4)
                    control = self._handle_failure(
                        exc,
                        phase="observe",
                        action=None,
                        source=None,
                        after=None,
                        state=state,
                        results=results,
                        retried=retried_low_confidence,
                        hint=(None, None),
                    )
                    if control.terminate is not None:
                        termination = control.terminate
                        break
                    pending = control.pending
                    pending_hint = control.pending_hint or (None, None)
                    continue
                task.status = TaskStatus.RUNNING

                verification_hint: str | None = None
                expected_effect: str | None = None
                if pending is not None:
                    action = pending
                    verification_hint, expected_effect = pending_hint
                    pending = None
                else:
                    try:
                        decision, verification_hint, expected_effect, _suspicious = await self._decide(
                            goal, observation
                        )
                        # The user's stop wins over anything the model just returned
                        # (decisions are data; "done" from a model cannot outrank a stop).
                        self.stop_token.ensure_live()
                    except _ProviderFailure as exc:
                        # Fail-closed provider path: audited in _decide; step consumed;
                        # recovery classification LOW_CONFIDENCE (replan/retry), bounded.
                        state.step_count += 1
                        task.step_count += 1
                        control = self._handle_failure(
                            exc,
                            phase="decide",
                            action=None,
                            source=observation,
                            after=None,
                            state=state,
                            results=results,
                            retried=retried_low_confidence,
                            hint=(None, None),
                            failure_class_override=FailureClass.LOW_CONFIDENCE,
                        )
                        if control.terminate is not None:
                            termination = control.terminate
                            break
                        pending = control.pending
                        pending_hint = control.pending_hint or (None, None)
                        continue
                    if str(decision.status) == "done":
                        results.append(self._done_result(decision))
                        # F3: audit the honest marking — completion is model-asserted,
                        # not an evidenced verification.
                        self._audit(
                            "verification",
                            observation=observation,
                            result="model_declared",
                            metadata={
                                "completion_evidence": "model_declared",
                                "method": "provider_done",
                                "note": (
                                    "Completion declared by the provider without "
                                    "independent verification evidence."
                                ),
                            },
                        )
                        termination = TerminationReason.COMPLETED
                        break
                    if str(decision.status) == "blocked" or decision.action is None:
                        # The model refused / could not propose: terminate unrecoverable
                        # (the refusal is data; replanning a deliberate refusal is not
                        # bounded-progress, so this exits cleanly instead of looping).
                        results.append(self._blocked_result(decision))
                        termination = TerminationReason.UNRECOVERABLE
                        break
                    action = decision.action
                    self.enforcer.begin_action()  # new action scope: per-action budgets reset
                    task.reset_action_scope()
                    retried_low_confidence = False
                    last_attempt_was_recovery = False
                    attempt_count = 0

                # --- GROUND -----------------------------------------------------------
                try:
                    self._ground(action, observation)
                except Exception as exc:  # noqa: BLE001 - grounding failures are recoverable
                    self.metrics.incr("grounding_failure")
                    self._audit(  # D7: parity with run_single — the refusal is audited
                        "grounding",
                        observation=observation,
                        action=action,
                        result="failed",
                        metadata={"exception": type(exc).__name__, "detail": str(exc)[:200]},
                    )
                    control = self._handle_failure(
                        exc,
                        phase="ground",
                        action=action,
                        source=observation,
                        after=None,
                        state=state,
                        results=results,
                        retried=retried_low_confidence,
                        hint=(verification_hint, expected_effect),
                    )
                    if control.terminate is not None:
                        termination = control.terminate
                        break
                    pending = control.pending
                    pending_hint = control.pending_hint or (None, None)
                    if control.pending is not None:
                        retried_low_confidence = True
                        last_attempt_was_recovery = True
                    continue

                # --- VALIDATE (staleness with a fresh pre-execution observation) ------
                current_observation = self._observe("validate")
                validation: ValidationOutcome = self.validator.validate(
                    action,
                    observation,
                    state,
                    current_observation=current_observation,
                    allowed_processes=self.allowed_processes or None,
                )
                if validation.valid:
                    # focus_window allowlist gate (backend must not run on a disallowed
                    # target); same rejection shape/mapping as allowlist violations.
                    focus_rejection = self._focus_allowlist_rejection(action, state)
                    if focus_rejection is not None:
                        validation = focus_rejection
                self._audit(
                    "validation",
                    observation=current_observation,
                    action=action,
                    result="ok" if validation.valid else "rejected",
                    metadata={
                        "reasons": validation.reasons[:3],
                        "codes": list(validation.codes)[:5],
                    },
                )
                if not validation.valid:
                    self.metrics.incr("grounding_failure")
                    control = self._handle_failure(
                        validation,
                        phase="validate",
                        action=action,
                        source=observation,
                        after=None,
                        state=state,
                        results=results,
                        retried=retried_low_confidence,
                        hint=(verification_hint, expected_effect),
                    )
                    if control.terminate is not None:
                        termination = control.terminate
                        break
                    pending = control.pending
                    pending_hint = control.pending_hint or (None, None)
                    if control.pending is not None:
                        retried_low_confidence = True
                        last_attempt_was_recovery = True
                    continue

                # --- RISK / POLICY ----------------------------------------------------
                safety_decision = self._evaluate_safety(action, state, observation)
                self._audit(
                    "safety",
                    observation=observation,
                    action=action,
                    result="allowed" if safety_decision.allowed else "denied",
                    metadata={
                        "reason": str(getattr(safety_decision, "reason", ""))[:200],
                        "risk": str(getattr(safety_decision, "risk", "") or ""),
                        "category": str(getattr(safety_decision, "category", "") or ""),
                    },
                )
                if not safety_decision.allowed:
                    self.metrics.incr("safety_block")
                    results.append(
                        ExecutionResult(ok=False, action=action, message=str(safety_decision.reason))
                    )
                    termination = TerminationReason.BLOCKED_SAFETY
                    break

                # --- APPROVAL ----------------------------------------------------------
                if safety_decision.requires_approval and action.action_id in self._approved_action_ids:
                    # Already granted for THIS action instance: recovery retries of the
                    # same instance never re-consume approval budget (compat decision 3).
                    self._audit(
                        "approval",
                        observation=observation,
                        action=action,
                        result="already_granted",
                        metadata={"reason": str(safety_decision.reason)[:200]},
                    )
                elif safety_decision.requires_approval:
                    task.status = TaskStatus.AWAITING_APPROVAL
                    self.metrics.incr("approval_requested")
                    self._audit(
                        "approval",
                        observation=observation,
                        action=action,
                        result="requested",
                        metadata={"reason": str(safety_decision.reason)[:200]},
                    )
                    granted = self._approval is not None and bool(
                        self._approval(action, str(safety_decision.reason))
                    )
                    task.status = TaskStatus.RUNNING
                    if granted:
                        self._approved_action_ids.add(action.action_id)
                        self.metrics.incr("approval_granted")
                        self._audit(
                            "approval",
                            observation=observation,
                            action=action,
                            result="granted",
                            metadata={"reason": str(safety_decision.reason)[:200]},
                        )
                    else:
                        self.metrics.incr("approval_denied")
                        self.approval_denied = True
                        self._audit(
                            "approval",
                            observation=observation,
                            action=action,
                            result="denied",
                            metadata={"reason": str(safety_decision.reason)[:200]},
                        )
                        results.append(
                            ExecutionResult(
                                ok=False,
                                action=action,
                                message="Approval denied or unavailable.",
                            )
                        )
                        termination = TerminationReason.APPROVAL_EXHAUSTED
                        break

                # --- EXECUTE (dry-run short-circuits with the legacy stub) -------------
                if getattr(state, "dry_run", False):
                    results.append(
                        ExecutionResult(
                            ok=True,
                            action=action,
                            message="Dry run: action validated but not executed.",
                            verification=VerificationResult(
                                verified=False,
                                changed=False,
                                note="Dry run: execution and post-action verification were not performed.",
                                confidence=1.0,
                            ),
                        )
                    )
                    continue

                self.enforcer.check_action()
                self.stop_token.ensure_live()  # P0-C: immediately before execution
                attempt_count += 1
                execution_started = time.perf_counter()
                try:
                    message = self.backend.execute(action, self.stop_token)
                except Exception as exc:  # noqa: BLE001 - execution failures are recoverable
                    duration_ms = (time.perf_counter() - execution_started) * 1000.0
                    self.metrics.incr("action_total")
                    self.metrics.incr("action_failure")
                    self.metrics.record_latency("execution_ms", duration_ms)
                    self._audit(
                        "execution",
                        observation=observation,
                        action=action,
                        result="error",
                        duration_ms=duration_ms,
                        metadata={"exception": type(exc).__name__, "detail": str(exc)[:200]},
                    )
                    control = self._handle_failure(
                        exc,
                        phase="execute",
                        action=action,
                        source=observation,
                        after=None,
                        state=state,
                        results=results,
                        retried=retried_low_confidence,
                        hint=(verification_hint, expected_effect),
                    )
                    if control.terminate is not None:
                        termination = control.terminate
                        break
                    pending = control.pending
                    pending_hint = control.pending_hint or (None, None)
                    if control.pending is not None:
                        retried_low_confidence = True
                        last_attempt_was_recovery = True
                    continue
                duration_ms = (time.perf_counter() - execution_started) * 1000.0
                self.metrics.record_latency("execution_ms", duration_ms)
                self.enforcer.record_action()
                self.metrics.incr("action_total")
                self.metrics.incr("action_success")
                state.step_count += 1
                task.step_count += 1
                task.record_action(action)
                self._audit(
                    "execution",
                    observation=observation,
                    action=action,
                    result="ok",
                    duration_ms=duration_ms,
                    metadata={"message": str(message)[:200], "attempt": attempt_count},
                )

                # --- RE-OBSERVE + VERIFY (baseline = the grounding-source observation) --
                after_observation = self._observe("post_action")
                intent = self._build_intent(action, verification_hint, expected_effect)
                verification = await self._verify(intent, observation, after_observation, action=action)

                if verification.outcome == "verified":
                    if last_attempt_was_recovery:
                        self.metrics.incr("recovery_success")
                        last_attempt_was_recovery = False
                    results.append(
                        ExecutionResult(
                            ok=True,
                            action=action,
                            message=str(message),
                            verification=verification,
                            screenshot_after_base64=after_observation.image_base64,
                            retry_count=max(attempt_count - 1, 0),
                        )
                    )
                    continue
                if verification.outcome == "uncertain" and action.action in _TOLERANT_UNCERTAIN_ACTIONS:
                    # Documented carve-out: a wait makes no semantic state claim; the screen
                    # may legitimately change or stay still during a wait, so uncertain is
                    # informational only. The result still carries the uncertain outcome.
                    results.append(
                        ExecutionResult(
                            ok=True,
                            action=action,
                            message=f"{message} (verification uncertain: {verification.note[:150]})",
                            verification=verification,
                            screenshot_after_base64=after_observation.image_base64,
                            retry_count=max(attempt_count - 1, 0),
                        )
                    )
                    continue

                # --- RECOVER / REPLAN on failed/uncertain verification ------------------
                control = self._handle_failure(
                    verification,
                    phase="verify",
                    action=action,
                    source=observation,
                    after=after_observation,
                    state=state,
                    results=results,
                    retried=retried_low_confidence,
                    hint=(verification_hint, expected_effect),
                )
                if control.terminate is not None:
                    termination = control.terminate
                    break
                pending = control.pending
                pending_hint = control.pending_hint or (None, None)
                if control.pending is not None:
                    retried_low_confidence = True
                    last_attempt_was_recovery = True

            if termination is None:  # loop exhausted without an explicit termination
                termination = TerminationReason.LIMIT_EXCEEDED
                results.append(
                    ExecutionResult(
                        ok=False,
                        action=GroundedAction(action="done"),
                        message="Maximum session steps reached.",
                    )
                )
        except TaskStopped:
            termination = TerminationReason.STOPPED_BY_USER
            results.append(self._stopped_result())
            self._audit("emergency_stop", result="stopped", metadata={"termination_reason": "stopped_by_user"})
        except LimitExceeded as exc:
            termination = TerminationReason.LIMIT_EXCEEDED
            results.append(
                ExecutionResult(
                    ok=False,
                    action=GroundedAction(action="done"),
                    message=f"Resource limit exceeded: {exc}",
                )
            )
            self._audit(
                "limit_exceeded",
                result="failed",
                metadata={"limit": exc.limit_name, "detail": str(exc)[:200]},
            )
        except Exception as exc:  # noqa: BLE001 - fail-closed: never raise out of run()
            termination = TerminationReason.UNRECOVERABLE
            results.append(
                ExecutionResult(
                    ok=False,
                    action=GroundedAction(action="done"),
                    message=f"Internal failure (fail-closed): {type(exc).__name__}: {exc}",
                )
            )
            self._audit(
                "failure",
                result="unrecoverable",
                metadata={"exception": type(exc).__name__, "detail": str(exc)[:300]},
            )
        finally:
            reason = termination or TerminationReason.UNRECOVERABLE
            self.task.terminate(reason)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self.metrics.record_latency("task_ms", elapsed_ms)
            if reason is TerminationReason.COMPLETED:
                self.metrics.incr("task_completed")
            else:
                self.metrics.incr("task_failed")
            self._audit(
                "session_stop",
                result=reason.value,
                duration_ms=elapsed_ms,
                metadata={
                    "termination_reason": reason.value,
                    "steps": self.task.step_count,
                    "model_calls": self.task.model_call_count,
                    "recovery_attempts": self.task.recovery_attempts_task,
                },
            )
            self._approval = None
        return results

    # ------------------------------------------------------------------ direct actions

    async def run_single(
        self,
        state: Any,
        action: GroundedAction,
        *,
        approved: bool = False,
        expected_effect: str | None = None,
    ) -> SingleActionOutcome:
        """Run one client-supplied action through the full pipeline (``computer_execute``).

        The action's ``confidence`` is the client-asserted MODEL confidence (legacy
        hardcoded 1.0); grounding confidence, staleness, risk, approval, and verification
        apply independently. Coordinate-bearing actions carry the source observation
        binding; staleness rejections trigger ONE automatic re-observe + re-validate
        (P0-H) before reporting rejection.
        """
        try:
            observation = self._observe("direct_request")
            try:
                grounding = self._ground(action, observation)
            except Exception as exc:  # noqa: BLE001 - grounding refusal is a rejection
                self.metrics.incr("grounding_failure")
                self._audit(
                    "grounding",
                    observation=observation,
                    action=action,
                    result="failed",
                    metadata={"exception": type(exc).__name__, "detail": str(exc)[:200]},
                )
                return SingleActionOutcome(
                    kind="rejected",
                    reasons=[f"Grounding failed: {exc}"],
                    message="Grounding rejected.",
                )

            current = self._observe("validate")
            validation = self.validator.validate(
                action,
                observation,
                state,
                current_observation=current,
                allowed_processes=self.allowed_processes or None,
            )
            if not validation.valid and "STALE_OBSERVATION" in validation.codes:
                # P0-H: automatic single re-observe + re-validate on staleness.
                try:
                    fresh = self._observe("revalidate")
                    self._ground(action, fresh)
                except Exception as re_ground_error:  # noqa: BLE001
                    self.metrics.incr("grounding_failure")
                    return SingleActionOutcome(
                        kind="rejected",
                        reasons=[f"Stale observation and re-grounding failed: {re_ground_error}"],
                        message="Grounding rejected.",
                    )
                observation = fresh  # the freshest pre-action observation is the baseline
                validation = self.validator.validate(
                    action,
                    fresh,
                    state,
                    current_observation=fresh,
                    allowed_processes=self.allowed_processes or None,
                )
            self._audit(
                "validation",
                observation=observation,
                action=action,
                result="ok" if validation.valid else "rejected",
                metadata={"reasons": validation.reasons[:3], "codes": list(validation.codes)[:5]},
            )
            if not validation.valid:
                self.metrics.incr("grounding_failure")
                return SingleActionOutcome(
                    kind="rejected", reasons=list(validation.reasons), message="Grounding rejected."
                )

            focus_rejection = self._focus_allowlist_rejection(action, state)
            if focus_rejection is not None:
                # focus_window allowlist gate (before safety): same rejection shape and
                # WRONG_WINDOW recovery mapping as ordinary allowlist violations.
                self.metrics.incr("grounding_failure")
                self._audit(
                    "validation",
                    observation=observation,
                    action=action,
                    result="rejected",
                    metadata={
                        "reasons": focus_rejection.reasons[:3],
                        "codes": list(focus_rejection.codes)[:5],
                    },
                )
                return SingleActionOutcome(
                    kind="rejected",
                    reasons=list(focus_rejection.reasons),
                    message="Grounding rejected.",
                )

            safety_decision = self._evaluate_safety(action, state, observation)
            self._audit(
                "safety",
                observation=observation,
                action=action,
                result="allowed" if safety_decision.allowed else "denied",
                metadata={
                    "reason": str(getattr(safety_decision, "reason", ""))[:200],
                    "risk": str(getattr(safety_decision, "risk", "") or ""),
                },
            )
            if not safety_decision.allowed:
                self.metrics.incr("safety_block")
                return SingleActionOutcome(kind="safety_denied", message=str(safety_decision.reason))
            if safety_decision.requires_approval and not approved:
                self.metrics.incr("approval_requested")
                self.metrics.incr("approval_denied")
                self._audit(
                    "approval",
                    observation=observation,
                    action=action,
                    result="required",
                    metadata={"reason": str(safety_decision.reason)[:200]},
                )
                return SingleActionOutcome(
                    kind="approval_required",
                    message=str(safety_decision.reason),
                    requires_approval=True,
                )

            if getattr(state, "dry_run", False):
                return SingleActionOutcome(
                    kind="executed",
                    result=ExecutionResult(
                        ok=True,
                        action=action,
                        message="Dry run: action validated but not executed.",
                        verification=VerificationResult(
                            verified=False,
                            changed=False,
                            note="Dry run: execution and post-action verification were not performed.",
                            confidence=1.0,
                        ),
                    ),
                    model_confidence=action.confidence,
                    grounding_confidence=grounding.confidence,
                )

            self.enforcer.check_action()
            self.enforcer.begin_action()
            self.stop_token.ensure_live()
            started = time.perf_counter()
            message = self.backend.execute(action, self.stop_token)
            duration_ms = (time.perf_counter() - started) * 1000.0
            self.metrics.record_latency("execution_ms", duration_ms)
            self.enforcer.record_action()
            self.metrics.incr("action_total")
            self.metrics.incr("action_success")
            state.step_count += 1
            self.task.step_count += 1
            self.task.record_action(action)
            self._audit(
                "execution",
                observation=observation,
                action=action,
                result="ok",
                duration_ms=duration_ms,
                metadata={"message": str(message)[:200]},
            )

            after = self._observe("post_action")
            intent = self._build_intent(action, None, expected_effect)
            verification = await self._verify(intent, observation, after, action=action)
            if (
                verification.outcome == "uncertain"
                and intent.kind == VerificationKind.EXPECTED_TEXT.value
                and after.ocr_text is None
            ):
                # Legacy-compat degradation for direct client calls only: no OCR evidence
                # exists in P0, so fall back to the deterministic visual-change check.
                # The final outcome still comes from evidence; uncertain is never upgraded.
                fallback_intent = self._build_intent(action, "visual_change", expected_effect)
                verification = await self._verify(fallback_intent, observation, after, action=action)
            ok = verification.outcome == "verified"
            return SingleActionOutcome(
                kind="executed",
                result=ExecutionResult(
                    ok=ok,
                    action=action,
                    message=str(message),
                    verification=verification,
                    screenshot_after_base64=after.image_base64,
                ),
                model_confidence=action.confidence,
                grounding_confidence=grounding.confidence,
                verification_confidence=verification.confidence,
            )
        except TaskStopped:
            raise
        except LimitExceeded as exc:
            self._audit("limit_exceeded", result="failed", metadata={"limit": exc.limit_name})
            raise
        except Exception as exc:  # noqa: BLE001 - fail-closed: structured error, no crash
            self._audit(
                "failure",
                result="error",
                metadata={"exception": type(exc).__name__, "detail": str(exc)[:300]},
            )
            return SingleActionOutcome(kind="error", message=f"{type(exc).__name__}: {exc}")
