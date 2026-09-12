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

PERF-004 loop-economics doctrines (additive; no gate, guarantee, or audit is removed):

- **Observe reuse (C1)**: the post-action capture becomes the next step's loop_top
  observation when available (3 -> 2 full captures per step; the reuse sidesteps the
  rate gate because no capture happens). The pre-execution staleness check stays a
  fresh, burst-exempt capture and is now validated DIGEST-FIRST: the grounding source's
  pixel digest is compared with the fresh capture's digest (``digest match`` = the
  screen is pixel-identical since grounding, so identity drift is provably impossible;
  ``mismatch`` = pixels changed and the full identity staleness validation decides —
  a mismatch is a hint, never a verdict). Every validator guarantee and audit event is
  preserved; the digest result is audited per validation.
- **Rate-gate semantics (C2)**: ``min_screenshot_interval_ms`` protects FRESH
  observations (loop_top on a new step, host-driven observes); intra-step verification
  captures (validate probe, post-action, P0-H revalidate) are burst-exempt but still
  recorded into the enforcer, so session-wide protection stays enforced and bounded.
- **Verification ladder (C3)**: for ``model_judge`` intents the deterministic tiers
  (window/process/text/predicate criteria derived from the action intent) run first,
  the pixel-diff supporting tier second, and the provider judge ONLY when both cheap
  tiers are inconclusive. A deterministic verdict always skips the judge.
- **Queued host actions (C7)**: ``run_single`` accepts trailing-optional
  ``follow_ups`` (max :data:`~computer_use_mcp.models.MAX_FOLLOW_UPS`). Every queue
  item passes the FULL independent pipeline exactly like a single action; the queue
  stops on safety rejection, approval requirement, validator/grounding rejection, or
  post-action DIGEST SURPRISE (a queued item's staleness probe shows the screen
  changed since the premise it was grounded from — speculative actions never run
  against a screen nobody has seen). An UNCERTAIN verification does not stop the
  queue (REM-B: uncertain is not failure), and neither does the ``failed`` verdict
  of an EXECUTED item (the input dispatched, the next item re-grounds from
  the fresh post-action capture, and the honest verdict rides the per-item entry;
  ``CORTEX_QUEUE_STRICT_VERIFY=1`` restores the strict stop-on-failed behavior).
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
from .backend import ComputerBackend, FocusDriftError
from .focus_guard import GuardVerdict, InterferenceGuard
from .grounding import GroundingRouter
from .interference import REFOCUS_HINT, InterferencePolicy, parse_interference
from .limits import LimitEnforcer, LimitExceeded, Limits
from .models import (
    MAX_FOLLOW_UPS,
    ActionSpec,
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
from .observation import ObservationEngine, digest_matches
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


def _process_matches_pattern(candidate: str, pattern: str) -> bool:
    """Case-insensitive, ``.exe``-tolerant process/exe-basename match (allowlist gate)."""
    normalize = lambda value: value.strip().casefold().removesuffix(".exe")
    return normalize(candidate) == normalize(pattern)
from .verification import (
    FOCUS_CHANGE_INTENT_FLAG,
    ScreenshotDiffStrategy,
    VerificationEngine,
    VerificationIntent,
    VerificationKind,
    deterministic_tiers,
)

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

#: Observe phases that are INTRA-STEP verification captures (PERF-004 C2): burst-exempt.
#: The rate gate protects fresh observations (loop_top, host-driven direct_request,
#: recovery re-observes); these phases capture within one logical action step and skip
#: the interval wait while still recording into the enforcer.
_INTRA_STEP_EXEMPT_PHASES = frozenset({"validate", "post_action", "revalidate"})

#: Cursor-at-target tolerance for the deterministic ``move`` verification predicate (px).
_CURSOR_TOLERANCE_PX = 2

#: Actions for which an ``uncertain`` verification continues the task (documented carve-out).
_TOLERANT_UNCERTAIN_ACTIONS = frozenset({ActionType.WAIT})

#: Cap on the per-agent map of action_id -> suspicious provider content (D3).
_SUSPICIOUS_CONTENT_CAP = 200

# --- R-5 mechanical-speed knobs (ORVEX-CORTEX-056-LIVEFIX) -----------------------------------
#: Env knob name: ``CORTEX_VALIDATE_REUSE_MS`` — the freshness window (milliseconds)
#: within which a DIRECT action's premise capture (taken at the start of this very
#: tool call) is reused as the validate-phase observation instead of re-capturing the
#: identical screen microseconds later. The window is the staleness guard: past it the
#: pipeline re-captures exactly as before. ``0`` disables the reuse (legacy behavior).
#: Queued follow-ups NEVER reuse: their premise is a previous item's post-action
#: capture (genuinely older), so the fresh validate capture — and the strict
#: digest-surprise probe — keep running for every queued item.
VALIDATE_REUSE_MS_ENV = "CORTEX_VALIDATE_REUSE_MS"

#: Default reuse window (ms). Generous relative to the microseconds of pure
#: computation between the direct_request capture and the validate point (grounding +
#: guard binding only — no input is dispatched between them), yet bounded.
_VALIDATE_REUSE_MS_DEFAULT = 1500.0


def _validate_reuse_window_ms() -> float:
    """Resolve the R-5 validate-reuse freshness window (0 disables; garbage -> default)."""
    import os

    raw = os.environ.get(VALIDATE_REUSE_MS_ENV)
    if raw is None or not raw.strip():
        return _VALIDATE_REUSE_MS_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return _VALIDATE_REUSE_MS_DEFAULT
    return value if value >= 0 else _VALIDATE_REUSE_MS_DEFAULT


# --- queue strict-verify knob -------------------------------------------------------------
#: Env knob name: ``CORTEX_QUEUE_STRICT_VERIFY`` — set to exactly ``1`` to restore the
#: v0.5.5 stop-on-failed queue semantics (an EXECUTED item whose verification outcome is
#: ``failed`` stops the batch). Default (unset/any other value): the queue CONTINUES past
#: an executed item's failed verdict — the input physically dispatched and safety already
#: admitted it; the honest failed verdict (ok=False + evidence) still rides the per-item
#: ``follow_up_results`` entry. Read lazily at each queue decision (test-toggleable).
QUEUE_STRICT_VERIFY_ENV = "CORTEX_QUEUE_STRICT_VERIFY"


def _queue_strict_verify() -> bool:
    """Resolve the strict-verify escape hatch (only the exact string ``1`` enables)."""
    import os

    return os.environ.get(QUEUE_STRICT_VERIFY_ENV, "").strip() == "1"


def _image_to_base64(image: Image.Image) -> str:
    """Encode a PIL image as base64 PNG for the provider judge callback."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _cursor_predicate(
    action: GroundedAction,
) -> tuple[Callable[[Observation, Observation], bool | None], str]:
    """Build the deterministic cursor-at-target predicate for ``move`` actions (PERF-004 C3)."""
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

    return _cursor_at_target, "cursor_at_target"


class _ProviderFailure(RuntimeError):
    """Internal sentinel: any provider-layer failure, already audited fail-closed."""


@dataclass
class SingleActionOutcome:
    """Result of one direct (client-supplied) action through the pipeline.

    ``kind`` selects the legacy ``computer_execute`` response shape: ``executed`` carries
    an :class:`ExecutionResult`, ``rejected`` carries validator ``reasons``,
    ``safety_denied``/``approval_required`` carry a policy message, ``error`` is the
    fail-closed catch-all. PERF-004 adds ``digest_surprise`` (a queued speculative
    action whose staleness probe shows the screen changed since its premise was
    captured — the queue stops before executing it).

    PERF-004 C7 (additive): when the call carried ``follow_ups``, the FIRST action's
    outcome stays the legacy response payload and the per-item queue results travel in
    ``follow_up_results`` (bounded dicts, heavy payloads stripped) with
    ``follow_ups_stopped_reason`` naming where the queue halted (``None`` = every item
    executed and verified).

    T8 (additive): ``interference_events`` carries the Interference Guard's structured
    event payloads observed for this action (MODAL_DIALOG/FOCUS_DRIFTED/...); the
    queue reads them for the named stops (``modal_dialog``/``focus_drifted``).
    """

    kind: str
    result: ExecutionResult | None = None
    reasons: list[str] = field(default_factory=list)
    message: str = ""
    requires_approval: bool = False
    model_confidence: float | None = None
    grounding_confidence: float | None = None
    verification_confidence: float | None = None
    follow_up_results: list[dict[str, Any]] | None = None
    follow_ups_stopped_reason: str | None = None
    interference_events: list[str] | None = None


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
        interference: InterferencePolicy | None = None,
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
        # T8 Interference Guard (protection upgrade; runs as ADDITIONAL gates — it can
        # only add rejections/annotations, never bypass an existing one). Defaults to
        # the A12 protective policy when the caller omits it.
        self.interference = interference if interference is not None else parse_interference(None)
        self.guard = InterferenceGuard(backend, self.interference, emit=self._audit_guard_event)

    def set_enforcer(self, enforcer: LimitEnforcer) -> None:
        """Swap the per-run limit enforcer (long-running orchestration seam, A5).

        Per-subtask limits (SubtasksProtocol section 3) give each subtask a fresh
        per-subtask budget scope; the recovery controller shares the enforcer's LIVE
        counters, so it is rebuilt alongside. No loop phase, ordering, approval, or
        verification semantics change — this only re-binds which counters gate a run.
        """
        self.enforcer = enforcer
        self._recovery = RecoveryController(enforcer)

    def _audit_guard_event(self, event_type: str, **kwargs: Any) -> None:
        """Audit-adapter for the Interference Guard (audit failures never break control)."""
        self._audit(event_type, **kwargs)

    # ------------------------------------------------------------------ interference helpers (T8)

    def _guard_pre_dispatch(self, action: GroundedAction) -> GuardVerdict | None:
        """Run the Interference Guard's pre-dispatch checks; audit + metric on rejections."""
        verdict = self.guard.verify_pre_dispatch(action)
        if verdict is not None:
            self.metrics.incr("interference_events")
            if verdict.blocking:
                self.metrics.incr("interference_rejections")
        return verdict

    def _guard_rejection_outcome(self, verdict: GuardVerdict) -> SingleActionOutcome:
        """Structured ``rejected`` outcome carrying the guard's event payload + hints."""
        return SingleActionOutcome(
            kind="rejected",
            reasons=[verdict.event, *verdict.hints],
            message=verdict.message,
        )

    @staticmethod
    def _verified_reanchor(guard: InterferenceGuard, verification_outcome: str, after_window: Any) -> Any:
        """B10 (b): re-anchor the session AFTER a VERIFIED action moved the surface.

        The guard's own rules decide whether the new surface is followable (same-process
        dialog/launcher, launch out of a launcher surface, dead anchor); a foreign steal
        keeps the anchor so the next dispatch rejects with FOCUS_TAKEN_BY.
        """
        if verification_outcome != "verified":
            return None  # only VERIFIED successes may move the anchor
        window = getattr(after_window, "active_window_info", None)
        guard.reanchor_after_success(window)
        return guard.bound

    def _bind_guard_from_observation(self, observation: Observation | None) -> None:
        """Arm the session's focus binding from an allowlisted observation (dormant otherwise)."""
        try:
            self.guard.maybe_bind(observation, self.allowed_processes)
        except Exception:
            logger.debug("guard binding failed", exc_info=True)

    def _guard_focus_hook(self, action: GroundedAction) -> Callable[[], None] | None:
        """Per-chunk focus-continuity hook for ``type`` actions (T8 mechanism iv)."""
        if action.action is not ActionType.TYPE:
            return None

        def _hook() -> None:
            self.guard.verify_mid_type(action)

        return _hook

    def _backend_execute(self, action: GroundedAction, stop: StopToken | None) -> str:
        """Backend execute seam with the T8 kwargs passed ONLY when they apply.

        Legacy call shape preserved for every action that needs neither the per-chunk
        focus hook (armed guard + ``type``) nor the ensure_app launch gate — the many
        valid legacy backend/fake implementations with the two-argument signature keep
        working unchanged.
        """
        focus_hook = (
            self._guard_focus_hook(action)
            if self.interference.focus_continuity.enabled and self.guard.armed
            else None
        )
        if focus_hook is None and action.action is not ActionType.ENSURE_APP:
            return self.backend.execute(action, stop)
        return self.backend.execute(
            action,
            stop,
            focus_hook=focus_hook,
            allow_launch=self._ensure_app_allow_launch(action),
        )

    def _ensure_app_allow_launch(self, action: GroundedAction) -> bool:
        """Whether THIS ensure_app may spawn a process (T8 mechanism ii policy gate).

        Server-side launching requires the ``attach_or_launch.launch="server"`` policy
        (the REM-B DEFAULT — an explicitly requested ``launch="driver"`` policy or the
        ``CORTEX_ATTACH_OR_LAUNCH=driver`` env knob restores the old never-launch
        default) AND — when a process allowlist is configured — the target process
        being allowlisted. Approval semantics, StopToken, and limits are unchanged.
        """
        if action.action is not ActionType.ENSURE_APP:
            return False
        if not self.interference.attach_or_launch.enabled:
            return False
        if self.interference.attach_or_launch.launch != "server":
            return False
        process = (action.target or "").split("|", 1)[0].strip()
        if self.allowed_processes and not any(
            _process_matches_pattern(process, pattern) for pattern in self.allowed_processes
        ):
            self._audit(
                "interference",
                action=action,
                result="ensure_app_launch_denied",
                metadata={"reason": "target process is not in the configured process allowlist"},
            )
            return False
        return True

    @staticmethod
    def _ensure_app_probe_outcome(message: str) -> bool:
        """True when an ensure_app execution returned a PROBE outcome (no screen claim)."""
        text = str(message or "")
        return text.startswith(("NO_INSTANCE", "AMBIGUOUS_INSTANCE"))

    def _post_action_guard_events(
        self,
        action: GroundedAction,
        after: Observation | None,
    ) -> tuple[list[str], list[str], list[str]]:
        """Run the post-action guard checks; returns (events, annotations, stop_reasons)."""
        events: list[str] = []
        annotations: list[str] = []
        stop_reasons: list[str] = []
        try:
            verdicts = self.guard.post_action_events(action, after)
        except Exception:  # noqa: BLE001 - sentinel failures never break the pipeline
            return events, annotations, stop_reasons
        for verdict in verdicts:
            self.metrics.incr("interference_events")
            events.append(verdict.event)
            annotations.append(f"{verdict.message} {verdict.event}")
            if verdict.stop_reason:
                stop_reasons.append(verdict.stop_reason)
        return events, annotations, stop_reasons

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
        """OBSERVE phase: capture with audit + metrics (rate-gated per PERF-004 C2).

        Fresh observations (every phase outside :data:`_INTRA_STEP_EXEMPT_PHASES`) wait
        behind ``min_screenshot_interval_ms`` exactly as before. Intra-step verification
        captures are burst-exempt: they skip the wait but are still recorded into the
        enforcer (count + pacing timestamp), keeping session-wide protection truthful.
        """
        gated = phase not in _INTRA_STEP_EXEMPT_PHASES
        started = time.perf_counter()
        if gated:
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
        try:  # R-5: capture-time freshness clock for the validate-phase reuse window
            observation._captured_monotonic = time.perf_counter()
        except Exception:  # noqa: BLE001 - PrivateAttr stamping must never break capture
            pass
        if gated:
            self.enforcer.record_screenshot()
        else:
            self.enforcer.record_burst_screenshot()
        self.metrics.incr("screenshot_count")
        duration_ms = (time.perf_counter() - started) * 1000.0
        self.metrics.record_latency("observation_ms", duration_ms)
        self.task.record_observation_id(observation.observation_id)
        self._audit(
            "observation",
            observation=observation,
            result="ok",
            duration_ms=duration_ms,
            phase=phase,
            gated=gated,
        )
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
                predicate, predicate_name = _cursor_predicate(action)
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
            elif action.action is ActionType.ENSURE_APP:
                # T8: a REATTACHED ensure_app outcome is verified by the foreground
                # PROCESS identity (probe outcomes short-circuit before verification).
                kind = VerificationKind.PROCESS_STATE.value
                process_name = (action.target or "").split("|", 1)[0].strip() or None
            if kind is None:
                kind = VerificationKind.VISUAL_CHANGE.value
        elif kind == VerificationKind.MODEL_JUDGE.value:
            # PERF-004 C3: a model-judge intent ALSO carries the deterministic criteria
            # the action itself implies, so the cheap-first verification ladder
            # (deterministic tiers -> pixel diff -> judge) can skip the judge whenever
            # a deterministic strategy already reaches a verdict. The kind stays
            # model_judge: when every cheap tier is inconclusive the judge still runs.
            if action.action is ActionType.TYPE:
                expected_text = effect or action.text
            elif action.action is ActionType.MOVE:
                predicate, predicate_name = _cursor_predicate(action)
            elif action.action in {ActionType.KEYPRESS, ActionType.HOTKEY} and effect:
                folded = effect.casefold()
                for prefix in _LAUNCH_PREFIXES:
                    if folded.startswith(prefix) and len(effect) > len(prefix):
                        window_title = effect[len(prefix):].strip() or None
                        break
            elif action.action is ActionType.FOCUS_WINDOW:
                window_title = action.target
        if kind == VerificationKind.EXPECTED_TEXT.value:
            expected_text = effect or action.text
        elif kind == VerificationKind.WINDOW_STATE.value:
            window_title = window_title or effect
        elif kind == VerificationKind.PROCESS_STATE.value:
            process_name = process_name or effect
        elif kind == VerificationKind.VISUAL_CHANGE.value and effect:
            expected_change = True
            # REM-B (H2c): a CLICK/DOUBLE_CLICK with a stated effect is a
            # focus-type expectation ("Hex input focused", "Edit colors dialog
            # opens") far more often than a pixel-threshold one — focusing a field
            # or opening a dialog sits BELOW the pixel-diff thresholds. Flag the
            # intent so the deterministic FocusChangeStrategy tier (UIA focused
            # element / window identity / digest) runs BEFORE the pixel-diff
            # definitive failure. Other visual-change intents are untouched.
            if action.action in {ActionType.CLICK, ActionType.DOUBLE_CLICK}:
                metadata: dict[str, Any] = {
                    "action_id": action.action_id,
                    "verification_hint": hint,
                    FOCUS_CHANGE_INTENT_FLAG: True,
                }
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
                    metadata=metadata,
                )
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
            # H1 (master-mission Phase 2 / REM-A): the observations already carry
            # PNG base64 — reuse those encoded bytes verbatim instead of the old
            # redundant decode -> PIL -> re-encode roundtrip. No verification-semantics
            # change: the judge receives the same image bytes as before.
            before_b64 = before.image_base64
            after_b64 = after.image_base64
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

    async def _verify_judge_ladder(
        self, intent: VerificationIntent, before: Observation, after: Observation
    ) -> tuple[VerificationResult, str]:
        """Cheap-first verification ladder for model-judge intents (PERF-004 C3).

        Tier 1 — deterministic strategies for the criteria the action intent states
        (window identity, process identity, expected text, predicates): a definitive
        verdict here SKIPS the judge entirely. Tier 2 — the pixel-diff supporting
        check, which can only definitively FAIL a judge intent (a stated expectation
        the pixels already falsify; it can never upgrade to verified). Tier 3 — the
        model judge (injected chain judge or the provider ``judge_change`` callback),
        reached only when both cheap tiers are inconclusive. Uncertain is never
        success at any tier.
        """
        for strategy, sub_intent in deterministic_tiers(intent):
            try:
                result = strategy.verify(sub_intent, before, after)
            except Exception as exc:
                logger.debug("Ladder deterministic tier %s failed", strategy.name, exc_info=exc)
                continue
            if result.outcome in {"verified", "failed"}:
                return result, "deterministic"
        if after.image_base64 != before.image_base64 or after.observation_id != before.observation_id:
            try:
                diff_result = ScreenshotDiffStrategy().verify(intent, before, after)
            except Exception:  # noqa: BLE001 - diff failure degrades to the judge tier
                diff_result = None
            if diff_result is not None and diff_result.outcome == "failed":
                return diff_result, "pixel_diff"
        else:
            # B1: the post-action capture is the SAME capture as the grounding source
            # (a starved ladder — e.g. the response screenshot was omitted and no fresh
            # verification capture exists). A self-comparison 0.0-diff is NOT evidence
            # of no-change; route to the next tier instead of false-failing.
            logger.debug("Ladder skipped pixel-diff tier: after-capture is the grounding capture")
        if self._verifier_has_judge():
            return self.verifier.verify(intent, before, after), "model_judge"
        return await self._provider_judge(intent, before, after), "model_judge"

    async def _verify(
        self,
        intent: VerificationIntent,
        before: Observation,
        after: Observation,
        action: GroundedAction | None = None,
    ) -> VerificationResult:
        """VERIFY phase: strategy chain or provider judge; uncertain never becomes success."""
        started = time.perf_counter()
        ladder_tier = "engine"
        try:
            if intent.kind == VerificationKind.MODEL_JUDGE.value:
                result, ladder_tier = await self._verify_judge_ladder(intent, before, after)
            elif after.observation_id == before.observation_id:
                # B1: the after-capture IS the grounding capture (starved ladder) — a
                # self-comparison 0.0 pixel diff is not evidence of no-change, so the
                # screenshot-diff tier is routed past (next tiers decide; all-uncertain
                # stays uncertain, never a false failure).
                strategies = [
                    strategy
                    for strategy in self.verifier.strategies
                    if getattr(strategy, "name", "") != "screenshot_diff"
                ]
                result = self.verifier.verify(intent, before, after, strategies=strategies)
                ladder_tier = "engine_no_diff"
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
                "ladder_tier": ladder_tier,
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
        # PERF-004 C1: the previous step's post-action capture is carried into the next
        # loop_top (observe reuse); single-use — consumed or discarded every step.
        carried: Observation | None = None

        try:
            for _loop_step in range(max(int(getattr(state, "max_steps", 30)), 1)):
                if getattr(state, "stopped", False) or self.stop_token.stopped:
                    termination = TerminationReason.STOPPED_BY_USER
                    results.append(self._stopped_result())  # never a vacuously-ok empty list
                    break
                self.stop_token.ensure_live()  # P0-C: loop top
                self.enforcer.check_task_duration()
                try:
                    if carried is not None:
                        # Observe reuse (PERF-004 C1): the post-action capture of the
                        # previous step IS the current screen state — no new capture,
                        # no rate-gate wait. The staleness probe at VALIDATE still
                        # provides the fresh pre-execution check.
                        observation = carried
                        carried = None
                        self.metrics.incr("observation_reuse")
                        self.task.record_observation_id(observation.observation_id)
                        self._audit(
                            "observation",
                            observation=observation,
                            result="ok",
                            duration_ms=0.0,
                            phase="loop_top",
                            reused=True,
                        )
                    else:
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
                # T8 mechanism (i): arm the session's focus binding from an allowlisted
                # observation (dormant until a target identity is seen).
                self._bind_guard_from_observation(observation)

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

                # --- VALIDATE (digest-first staleness, PERF-004 C1) -------------------
                # One fresh pre-execution capture (burst-exempt intra-step), compared
                # digest-first against the grounding source: a pixel-identical capture
                # proves identity drift impossible ("digest match = still valid"); any
                # mismatch still runs the full identity staleness validation below —
                # the digest is a hint, never a verdict. All validator guarantees and
                # audit events are preserved.
                current_observation = self._observe("validate")
                staleness_proof = (
                    "digest_match"
                    if digest_matches(observation, current_observation)
                    else "digest_mismatch"
                )
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
                        "staleness_proof": staleness_proof,
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
                            message=(
                                "DRY-RUN (no input dispatched): action validated "
                                "but not executed."
                            ),
                            verification=VerificationResult(
                                verified=False,
                                changed=False,
                                note=(
                                    "DRY-RUN (no input dispatched): execution and "
                                    "post-action verification were not performed."
                                ),
                                confidence=1.0,
                            ),
                        )
                    )
                    continue

                self.enforcer.check_action()
                self.stop_token.ensure_live()  # P0-C: immediately before execution
                # --- T8 Interference Guard (pre-dispatch, after the dry-run check) ----
                # Protection upgrade only: a blocking verdict here routes through the
                # SAME bounded recovery machinery as a validation rejection (replan),
                # never onto the foreign window. observe_only/warn annotate and proceed.
                guard_verdict = self._guard_pre_dispatch(action)
                if guard_verdict is not None and guard_verdict.blocking:
                    self._audit(
                        "interference",
                        observation=observation,
                        action=action,
                        result="rejected",
                        metadata={
                            "event": guard_verdict.event.split(" ", 1)[0],
                            "payload": guard_verdict.event[:400],
                            "failure_class": guard_verdict.failure_class.value,
                        },
                    )
                    control = self._handle_failure(
                        guard_verdict,
                        phase="interference_pre_dispatch",
                        action=action,
                        source=observation,
                        after=None,
                        state=state,
                        results=results,
                        retried=retried_low_confidence,
                        hint=(verification_hint, expected_effect),
                        failure_class_override=guard_verdict.failure_class,
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
                pre_annotation = guard_verdict.message if guard_verdict is not None else None
                attempt_count += 1
                execution_started = time.perf_counter()
                try:
                    message = self._backend_execute(action, self.stop_token)
                except FocusDriftError as exc:
                    # T8 mechanism iv: mid-type focus drift — the in-flight type aborted
                    # so text cannot land in a foreign field. No same-instance retry
                    # (a retype would duplicate the delivered prefix): replan instead.
                    duration_ms = (time.perf_counter() - execution_started) * 1000.0
                    self.metrics.incr("action_total")
                    self.metrics.incr("action_failure")
                    self.metrics.record_latency("execution_ms", duration_ms)
                    self.metrics.incr("interference_rejections")
                    self._audit(
                        "interference",
                        observation=observation,
                        action=action,
                        result="focus_drifted_mid_type",
                        duration_ms=duration_ms,
                        metadata={"payload": str(exc)[:400]},
                    )
                    control = self._handle_failure(
                        FocusDriftError(str(exc)),
                        phase="execute",
                        action=action,
                        source=observation,
                        after=None,
                        state=state,
                        results=results,
                        retried=retried_low_confidence,
                        hint=(verification_hint, expected_effect),
                        failure_class_override=FailureClass.WRONG_WINDOW,
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
                # PERF-004 C1: this fresh capture becomes the next step's loop_top
                # observation (observe reuse) unless this step diverges first.
                carried = after_observation
                # T8: re-bind after an explicit focus/reattach action; run the sentinel.
                if action.action in {ActionType.FOCUS_WINDOW, ActionType.ENSURE_APP}:
                    self.guard.rebind_from_observation(after_observation)
                if action.action in {ActionType.KEYPRESS, ActionType.HOTKEY}:
                    self.guard.note_chord(action.keys)
                post_events, post_annotations, post_stops = self._post_action_guard_events(
                    action, after_observation
                )
                intent = self._build_intent(action, verification_hint, expected_effect)
                if action.action is ActionType.ENSURE_APP and self._ensure_app_probe_outcome(message):
                    # A probe outcome (NO_INSTANCE / AMBIGUOUS_INSTANCE) makes no screen
                    # claim: the payload IS the answer the driver acts on. Reporting it
                    # as a screen verification would be dishonest; report as evidence.
                    verification = VerificationResult(
                        outcome="verified",
                        changed=False,
                        note=f"ensure_app probe outcome (no screen claim): {str(message)[:200]}",
                        confidence=0.9,
                        evidence=[str(message)[:400]],
                        verification_method="ensure_app_probe",
                        observation_id=after_observation.observation_id,
                    )
                else:
                    verification = await self._verify(
                        intent, observation, after_observation, action=action
                    )
                    # T8 B2: with OCR demoted, an uncertain expected-text verdict means
                    # the text is invisible to window/UI-control evidence — route to the
                    # reliable pixel-diff tier instead of failing/looping.
                    if (
                        verification.outcome == "uncertain"
                        and intent.kind == VerificationKind.EXPECTED_TEXT.value
                    ):
                        fallback_intent = self._build_intent(action, "visual_change", expected_effect)
                        verification = await self._verify(
                            fallback_intent, observation, after_observation, action=action
                        )
                if verification.outcome == "verified" and not post_stops:
                    # B10 (b): follow the session's own verified surface transitions.
                    self._verified_reanchor(self.guard, verification.outcome, after_observation)
                if pre_annotation:
                    post_annotations.insert(0, pre_annotation)
                if post_annotations:
                    verification = verification.model_copy(
                        update={"note": f"{verification.note} | {' | '.join(post_annotations)}"[:900]}
                    )
                drift_post = any(e.startswith("FOCUS_DRIFTED") for e in post_events)
                if drift_post and verification.outcome == "verified":
                    # T8: a post-keyboard FOCUS_DRIFTED abort downgrades a pixel-only
                    # "verified" to an honest failed outcome (the typed content may
                    # have landed in a foreign field). MODAL_DIALOG, by contrast, is an
                    # ANNOTATION on single actions (A12): the action executed; the
                    # queue stops via interference_events, single calls read the note.
                    verification = VerificationResult(
                        outcome="failed",
                        changed=verification.changed,
                        note=f"Post-action interference: {' | '.join(post_stops + post_events)}"[:900],
                        confidence=verification.confidence,
                        evidence=list(post_events),
                        verification_method="interference_post_action",
                        observation_id=after_observation.observation_id,
                    )

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
        follow_ups: list[ActionSpec] | None = None,
    ) -> SingleActionOutcome:
        """Run one client-supplied action through the full pipeline (``computer_execute``).

        The action's ``confidence`` is the client-asserted MODEL confidence (legacy
        hardcoded 1.0); grounding confidence, staleness, risk, approval, and verification
        apply independently. Coordinate-bearing actions carry the source observation
        binding; staleness rejections trigger ONE automatic re-observe + re-validate
        (P0-H) before reporting rejection.

        PERF-004 C7 (trailing-optional): ``follow_ups`` queues up to
        :data:`~computer_use_mcp.models.MAX_FOLLOW_UPS` additional actions after the
        primary action. Each queue item passes the FULL independent pipeline
        (ground -> validate -> safety -> approval semantics -> execute -> verify)
        exactly like a single action; the queue stops at a safety rejection,
        approval requirement, validator/grounding rejection, or post-action digest
        surprise. An UNCERTAIN verification (no expectation stated / a non-visual
        action pixels cannot judge) does NOT stop the queue (REM-B H2b), and
        neither does the ``failed`` verdict of an EXECUTED item (the
        honest failed verdict rides the per-item entry while the batch continues;
        ``CORTEX_QUEUE_STRICT_VERIFY=1`` restores stop-on-failed). Every executed
        action emits its own audit events. ``approved`` applies to every item (the
        safety policy evaluates each independently).
        """
        if follow_ups:
            specs = list(follow_ups)[:MAX_FOLLOW_UPS]
            return await self._run_action_queue(
                state, action, specs, approved=approved, expected_effect=expected_effect
            )
        outcome, _post = await self._run_single_pipeline(
            state, action, approved=approved, expected_effect=expected_effect
        )
        return outcome

    async def _run_action_queue(
        self,
        state: Any,
        action: GroundedAction,
        follow_ups: list[ActionSpec],
        *,
        approved: bool,
        expected_effect: str | None,
    ) -> SingleActionOutcome:
        """Speculative host queue (PERF-004 C7): primary action + bounded follow_ups.

        SAFETY MANDATE — zero bypass: every item runs the complete independent pipeline
        (no state is shared between items except the fresh post-action observation that
        becomes the next item's grounding source — the observe-reuse doctrine). Stops:
        named interference stops (modal dialog / focus drift), safety rejection,
        approval requirement, validator rejection, digest surprise, stop token, limit
        trip, or a missing post-action observation. REM-B (H2b): an item whose
        verification is UNCERTAIN (no expectation stated / non-visual action the diff
        cannot judge) does NOT stop the queue — uncertain is not failure — but its
        per-item result keeps the honest uncertain verdict and ``ok=False``.
        an EXECUTED item whose verification outcome is ``failed`` also does
        NOT stop the queue by default — the input dispatched and the next item
        re-grounds from the fresh post-action capture; the honest failed verdict rides
        the per-item entry. ``CORTEX_QUEUE_STRICT_VERIFY=1`` restores the v0.5.5
        stop-on-failed behavior.
        """
        queue_items: list[tuple[GroundedAction, str | None]] = [(action, expected_effect)]
        for spec in follow_ups:
            queue_items.append(
                (spec.to_grounded(reason_prefix="MCP follow_up action"), spec.expected_effect)
            )
        follow_up_results: list[dict[str, Any]] = []
        stopped_reason: str | None = None
        source_observation: Observation | None = None
        executed_count = 0
        first_outcome: SingleActionOutcome | None = None
        for index, (item, item_effect) in enumerate(queue_items):
            self.stop_token.ensure_live()  # P0-C: kill path checked between queue items
            strict_digest = index > 0  # follow-ups stop on a post-action digest surprise
            outcome, post = await self._run_single_pipeline(
                state,
                item,
                approved=approved,
                expected_effect=item_effect,
                source_observation=source_observation,
                strict_digest=strict_digest,
            )
            if first_outcome is None:
                first_outcome = outcome  # the legacy response payload stays item 0's
            executed_count += 1
            follow_up_results.append(self._queue_entry(index, item, outcome))
            # T8 named interference stops take precedence over the generic stop reasons:
            # a modal dialog or a post-keyboard focus drift halts the batch with a NAMED
            # reason the driver protocol teaches drivers to react to deliberately.
            interference_events = outcome.interference_events or []
            named_stop = next(
                (
                    reason
                    for reason in (
                        "modal_dialog" if any(e.startswith("MODAL_DIALOG") for e in interference_events) else None,
                        "focus_drifted" if any(e.startswith("FOCUS_DRIFTED") for e in interference_events) else None,
                    )
                    if reason is not None
                ),
                None,
            )
            if named_stop is not None:
                stopped_reason = named_stop
                break
            # REM-B (H2b queue-stop softening): an UNCERTAIN verification is not a
            # failure — the logged ctrl+a/hotkey keystrokes were flushed purely
            # because "no expectation stated" verdicts set ok=False. The queue now
            # CONTINUES past an item whose verification outcome is "uncertain" (no
            # expectation stated / a non-visual action the screenshot diff cannot
            # judge); every honest stop remains: non-executed kinds, DEFINITIVE
            # verification failures, safety rejections, approval requirements,
            # digest surprises, and the no-post-observation guard below. Per-item
            # results keep their own truthful verdict (ok=False + uncertain).
            if outcome.kind != "executed" or outcome.result is None:
                stopped_reason = (
                    self._interference_stop_reason(outcome)
                    or (outcome.kind if outcome.kind != "executed" else "verification_failed")
                )
                break
            item_verification = outcome.result.verification
            # an EXECUTED item whose verification outcome is
            # "failed" no longer DEFINITIVELY stops the queue. The input physically
            # dispatched and safety already admitted it; the next item re-grounds from
            # this item's fresh post-action capture, so nothing speculative ever runs
            # against an unseen screen. A FALSE failure (sub-threshold focus/caret
            # change, slow dialog open) must not flush the remaining batch items and
            # force 1-action-per-call driving. The honest failed verdict (ok=False,
            # note, evidence) still rides the per-item follow_up_results entry. Every
            # other stop is untouched: non-executed kinds (rejections, safety denial,
            # approval requirement, digest surprise, error), named interference stops,
            # the uncertain carve-out below, and the no-post-observation guard.
            # CORTEX_QUEUE_STRICT_VERIFY=1 restores the v0.5.5 stop-on-failed behavior
            # (read lazily; test-toggleable like CORTEX_DIFF_FAST).
            verification_failed = (
                not outcome.result.ok
                and item_verification is not None
                and item_verification.outcome == "failed"
            )
            if verification_failed and not _queue_strict_verify():
                pass  # continue: the honest failed verdict rides the per-item entry
            elif not outcome.result.ok and not (
                item_verification is not None and item_verification.outcome == "uncertain"
            ):
                stopped_reason = (
                    self._interference_stop_reason(outcome)
                    or (outcome.kind if outcome.kind != "executed" else "verification_failed")
                )
                break
            if post is None:
                # Dry-run (or a degenerate executed outcome without a fresh capture):
                # nothing further can be verified — stop before speculating.
                stopped_reason = "no_post_action_observation"
                break
            source_observation = post
        assert first_outcome is not None  # the loop always runs at least once
        self._audit(
            "queue",
            action=action,
            result=stopped_reason or "completed",
            metadata={
                "items": len(queue_items),
                "executed": executed_count,
                "stopped_reason": stopped_reason or "",
            },
        )
        # Additive queue bookkeeping on the LEGACY first-item outcome: callers without
        # follow_ups see byte-identical shapes; queue callers get the extra fields.
        first_outcome.follow_up_results = follow_up_results
        first_outcome.follow_ups_stopped_reason = stopped_reason
        return first_outcome

    @staticmethod
    def _interference_stop_reason(outcome: SingleActionOutcome) -> str | None:
        """Map a rejection's guard event payload to the NAMED queue stop reason (T8)."""
        if outcome.kind != "rejected":
            return None
        prefixes = {
            "FOCUS_TAKEN_BY": "focus_taken_by",
            "FOCUS_IDENTITY_UNKNOWN": "focus_identity_unknown",
            "FOCUS_DRIFTED": "focus_drifted",
            "MODAL_DIALOG": "modal_dialog",
            "STUCK_MODIFIER": "stuck_modifier",
            "TARGET_GONE": "target_gone",
        }
        for reason in outcome.reasons:
            first = str(reason).split(" ", 1)[0]
            if first in prefixes:
                return prefixes[first]
        return None

    @staticmethod
    def _queue_entry(index: int, action: GroundedAction, outcome: SingleActionOutcome) -> dict[str, Any]:
        """Slim bounded per-item queue result (redaction at the sink).

        REM-A H5/H6 (master-mission Phase 2): queue entries carry ONLY the per-item
        essentials (ok, action identity, verification outcome/note, confidences) —
        never screenshot bytes in ANY form (nested or otherwise) and never a second
        copy of the top-level payload blobs: the primary action's full result rides
        the top-level response payload exactly once.
        """
        result = outcome.result
        entry: dict[str, Any] = {
            "index": index,
            "action_id": action.action_id,
            "action_type": action.action.value,
            "kind": outcome.kind,
            "ok": bool(result.ok) if result is not None else False,
            "message": outcome.message or (result.message if result is not None else ""),
            "reasons": list(outcome.reasons),
            "requires_approval": outcome.requires_approval,
            "model_confidence": outcome.model_confidence,
            "grounding_confidence": outcome.grounding_confidence,
            "verification_confidence": outcome.verification_confidence,
        }
        if result is not None and result.verification is not None:
            entry["verification_outcome"] = result.verification.outcome
            entry["verification_note"] = result.verification.note[:200]
        return entry

    async def _run_single_pipeline(
        self,
        state: Any,
        action: GroundedAction,
        *,
        approved: bool,
        expected_effect: str | None = None,
        source_observation: Observation | None = None,
        strict_digest: bool = False,
    ) -> tuple[SingleActionOutcome, Observation | None]:
        """One direct action through the full pipeline; returns (outcome, post-capture).

        ``source_observation`` (PERF-004 C1 observe reuse) injects the caller's fresh
        post-action capture as the grounding source; ``None`` captures at
        ``direct_request`` (rate-gated, host-driven). ``strict_digest`` (queued
        follow-ups) turns a staleness-probe digest mismatch into a ``digest_surprise``
        stop BEFORE executing the speculative action.

        The returned observation is the fresh post-action capture (``None`` when no
        execution happened — dry-run, rejection, stop) so the caller can reuse it.
        """
        try:
            if source_observation is not None:
                observation = source_observation
                self.metrics.incr("observation_reuse")
                self.task.record_observation_id(observation.observation_id)
                self._audit(
                    "observation",
                    observation=observation,
                    result="ok",
                    duration_ms=0.0,
                    phase="direct_request",
                    reused=True,
                )
            else:
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
                return (
                    SingleActionOutcome(
                        kind="rejected",
                        reasons=[f"Grounding failed: {exc}"],
                        # name the REAL gate in `message` — weak drivers
                        # read `message` first and a generic "Grounding rejected."
                        # sent them decoding `reasons` for turns.
                        message=f"Action rejected by grounding: {str(exc)[:180]}",
                    ),
                    None,
                )

            # T8 mechanism (i): arm the focus binding from an allowlisted observation.
            self._bind_guard_from_observation(observation)
            # R-5 (W1) validate-phase capture sharing: a DIRECT action's premise was
            # captured at the start of THIS tool call (nothing but pure computation has
            # run since — grounding + guard binding, zero input dispatched), so when it
            # is still inside the freshness window the premise IS the current screen and
            # re-capturing the identical frame is pure latency. The sharing is GUARDED
            # by a cheap identity probe (monitors + foreground window + coordinate
            # space — the exact dimensions the validator's staleness check consumes; no
            # pixels): no drift means the premise still describes the screen and is
            # reused (audited truthfully: phase="validate", reused=True, duration 0.0);
            # ANY drift, a stale premise, a queued follow-up (whose premise is a
            # PREVIOUS item's post-action capture), or a backend without the probe
            # falls back to the fresh validate capture below exactly as before.
            premise_captured_at = getattr(observation, "_captured_monotonic", 0.0)
            reuse_window_ms = _validate_reuse_window_ms()
            current: Observation | None = None
            probe_used = False
            if (
                source_observation is None
                and not strict_digest
                and reuse_window_ms > 0.0
                and premise_captured_at > 0.0
                and (time.perf_counter() - premise_captured_at) * 1000.0 <= reuse_window_ms
            ):
                probe = self.backend.identity_probe(observation)
                if probe is not None:
                    probe_used = True
                    drift = self.validator._staleness_drift(observation, probe)
                    if drift is None:
                        current = observation
                        self.metrics.incr("observation_reuse")
                        self._audit(
                            "observation",
                            observation=current,
                            result="ok",
                            duration_ms=0.0,
                            phase="validate",
                            reused=True,
                        )
                    else:
                        # Real drift (window/process/monitor/space): feed the probe to
                        # the validator as the current observation so the P0-H
                        # STALE_OBSERVATION rejection + single re-observe recovery run
                        # with fresh identity — never execute against a drifted premise.
                        current = probe
                        self.metrics.incr("identity_probe_drift")
                        self._audit(
                            "observation",
                            observation=current,
                            result="ok",
                            duration_ms=0.0,
                            phase="validate",
                            reused=True,
                            probe=True,
                            drift=drift,
                        )
            if current is None:
                current = self._observe("validate")
            if strict_digest and not digest_matches(observation, current):
                # PERF-004 C7: post-action DIGEST SURPRISE — the queued action's premise
                # (the previous item's fresh post-action capture) no longer matches the
                # screen. Speculative actions never run against a screen nobody has
                # seen; the queue stops BEFORE executing this item (zero bypass).
                self.metrics.incr("digest_surprise")
                self._audit(
                    "validation",
                    observation=current,
                    action=action,
                    result="digest_surprise",
                    metadata={
                        "reason": (
                            "Post-action digest surprise: the screen changed since the "
                            "queued action's premise was captured; the queue stopped."
                        )
                    },
                )
                return (
                    SingleActionOutcome(
                        kind="digest_surprise",
                        message=(
                            "Post-action digest surprise: the screen changed since this "
                            "queued action's premise was captured; the queue stopped "
                            "before executing it."
                        ),
                    ),
                    None,
                )
            validation = self.validator.validate(
                action,
                observation,
                state,
                current_observation=current,
                allowed_processes=self.allowed_processes or None,
            )
            if not validation.valid and "STALE_OBSERVATION" in validation.codes:
                # P0-H: automatic single re-observe + re-validate on staleness.
                # (Skipped for strict queued items: a drifted premise stops the queue
                # as a digest surprise before this branch can run.)
                try:
                    fresh = self._observe("revalidate")
                    self._ground(action, fresh)
                except Exception as re_ground_error:  # noqa: BLE001
                    self.metrics.incr("grounding_failure")
                    return (
                        SingleActionOutcome(
                            kind="rejected",
                            reasons=[f"Stale observation and re-grounding failed: {re_ground_error}"],
                            # name the staleness gate, not a generic stamp.
                            message=f"Action rejected by staleness check: {str(re_ground_error)[:180]}",
                        ),
                        None,
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
                metadata={
                    "reasons": validation.reasons[:3],
                    "codes": list(validation.codes)[:5],
                    "staleness_proof": (
                        "digest_match" if digest_matches(observation, current) else "digest_mismatch"
                    ),
                },
            )
            if not validation.valid:
                self.metrics.incr("grounding_failure")
                return (
                    SingleActionOutcome(
                        kind="rejected",
                        reasons=list(validation.reasons),
                        # name the REAL gate in `message` — the live
                        # session's allowlist rejection arrived as a generic
                        # "Grounding rejected." and cost the driver thinking turns.
                        message=(
                            "Action rejected by validation: "
                            + (str(validation.reasons[0])[:180] if validation.reasons else "grounding failed.")
                        ),
                    ),
                    None,
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
                return (
                    SingleActionOutcome(
                        kind="rejected",
                        reasons=list(focus_rejection.reasons),
                        # the focus-window allowlist gate names itself.
                        message=(
                            "Action rejected by focus allowlist: "
                            + (
                                str(focus_rejection.reasons[0])[:180]
                                if focus_rejection.reasons
                                else "focus gate refused."
                            )
                        ),
                    ),
                    None,
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
                return (
                    SingleActionOutcome(kind="safety_denied", message=str(safety_decision.reason)),
                    None,
                )
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
                return (
                    SingleActionOutcome(
                        kind="approval_required",
                        message=str(safety_decision.reason),
                        requires_approval=True,
                    ),
                    None,
                )

            if getattr(state, "dry_run", False):
                return (
                    SingleActionOutcome(
                        kind="executed",
                        result=ExecutionResult(
                            ok=True,
                            action=action,
                            message=(
                                "DRY-RUN (no input dispatched): action validated "
                                "but not executed."
                            ),
                            verification=VerificationResult(
                                verified=False,
                                changed=False,
                                note=(
                                    "DRY-RUN (no input dispatched): execution and "
                                    "post-action verification were not performed."
                                ),
                                confidence=1.0,
                            ),
                        ),
                        model_confidence=action.confidence,
                        grounding_confidence=grounding.confidence,
                    ),
                    None,
                )

            # --- T8 Interference Guard (pre-dispatch, after the dry-run check) --------
            # Protection upgrade only: a blocking verdict is a structured REJECTION —
            # the foreign window is never acted on. observe_only/warn annotate instead.
            guard_verdict = self._guard_pre_dispatch(action)
            if guard_verdict is not None and guard_verdict.blocking:
                return self._guard_rejection_outcome(guard_verdict), None
            pre_annotation = guard_verdict.message if guard_verdict is not None else None

            self.enforcer.check_action()
            self.enforcer.begin_action()
            self.stop_token.ensure_live()
            started = time.perf_counter()
            try:
                message = self._backend_execute(action, self.stop_token)
            except FocusDriftError as exc:
                # T8 mechanism iv: mid-type focus drift — abort cleanly (no same-instance
                # retry: a retype would duplicate the delivered prefix).
                self.metrics.incr("action_total")
                self.metrics.incr("action_failure")
                self.metrics.incr("interference_rejections")
                self._audit(
                    "interference",
                    observation=observation,
                    action=action,
                    result="focus_drifted_mid_type",
                    metadata={"payload": str(exc)[:400]},
                )
                return (
                    SingleActionOutcome(
                        kind="rejected",
                        reasons=[str(exc), REFOCUS_HINT],
                        message=(
                            "Focus continuity: focus drifted mid-type; the in-flight type "
                            "was aborted before further chunks could land in a foreign field."
                        ),
                    ),
                    None,
                )
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

            # B1 GUARANTEE (T8): verification ALWAYS captures its own fresh post-action
            # observation here, internally — it can never depend on the screenshot the
            # RESPONSE was configured to omit (``include_screenshot_after=false`` pops
            # the image from the response payload only, after verification has run).
            after = self._observe("post_action")
            # T8: re-bind after an explicit focus/reattach action; run the sentinel.
            if action.action in {ActionType.FOCUS_WINDOW, ActionType.ENSURE_APP}:
                self.guard.rebind_from_observation(after)
            if action.action in {ActionType.KEYPRESS, ActionType.HOTKEY}:
                self.guard.note_chord(action.keys)
            post_events, post_annotations, post_stops = self._post_action_guard_events(action, after)
            outcome_message = str(message)
            intent = self._build_intent(action, None, expected_effect)
            if action.action is ActionType.ENSURE_APP and self._ensure_app_probe_outcome(outcome_message):
                # A probe outcome (NO_INSTANCE / AMBIGUOUS_INSTANCE) makes no screen
                # claim; the payload IS the answer the driver acts on.
                verification = VerificationResult(
                    outcome="verified",
                    changed=False,
                    note=f"ensure_app probe outcome (no screen claim): {outcome_message[:200]}",
                    confidence=0.9,
                    evidence=[outcome_message[:400]],
                    verification_method="ensure_app_probe",
                    observation_id=after.observation_id,
                )
            else:
                verification = await self._verify(intent, observation, after, action=action)
                # T8 B2: an uncertain expected-text verdict (typed text invisible to
                # window/UI-control evidence) routes to the reliable pixel-diff tier.
                if (
                    verification.outcome == "uncertain"
                    and intent.kind == VerificationKind.EXPECTED_TEXT.value
                ):
                    # Legacy-compat degradation for direct client calls: fall back to the
                    # deterministic visual-change check; the final outcome still comes
                    # from evidence; uncertain is never upgraded.
                    fallback_intent = self._build_intent(action, "visual_change", expected_effect)
                    verification = await self._verify(
                        fallback_intent, observation, after, action=action
                    )
            if pre_annotation:
                post_annotations.insert(0, pre_annotation)
            if post_annotations:
                verification = verification.model_copy(
                    update={"note": f"{verification.note} | {' | '.join(post_annotations)}"[:900]}
                )
            drift_post = any(e.startswith("FOCUS_DRIFTED") for e in post_events)
            if drift_post and verification.outcome == "verified":
                # T8: only a post-keyboard FOCUS_DRIFTED abort is an honest not-ok;
                # MODAL_DIALOG annotates on single actions (A12 single-action semantics).
                verification = VerificationResult(
                    outcome="failed",
                    changed=verification.changed,
                    note=f"Post-action interference: {' | '.join(post_stops + post_events)}"[:900],
                    confidence=verification.confidence,
                    evidence=list(post_events),
                    verification_method="interference_post_action",
                    observation_id=after.observation_id,
                )
            ok = verification.outcome == "verified"
            if ok:
                self._verified_reanchor(self.guard, verification.outcome, after)
            outcome = SingleActionOutcome(
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
                interference_events=post_events or None,
            )
            try:  # R-5 (W4): the post-action frame rides along for outbound bounding
                outcome.result._frame = getattr(after, "_frame", None)
            except Exception:  # noqa: BLE001 - never break the executed path
                pass
            return outcome, after
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
            return SingleActionOutcome(kind="error", message=f"{type(exc).__name__}: {exc}"), None
