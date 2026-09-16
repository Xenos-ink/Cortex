"""Direct-action controller: the ComputerUseAgent pipeline (master-mission section 6).

The closed-loop decide phase machine (``run``: OBSERVE -> DECIDE(model) -> ... ->
RECOVER/REPLAN) was removed with the removed-loop family; this module now carries the
LIVE per-action pipeline used by ``run_single`` (``computer_execute``)::

    GROUND -> VALIDATE(staleness) -> RISK/POLICY -> APPROVAL
            -> EXECUTE(stop-checked) -> RE-OBSERVE -> VERIFY

The doctrine bullets below still apply verbatim to that per-action pipeline.

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
  post-action PREMISE INVALIDITY (a queued item's staleness probe shows the attached
  window's identity — hwnd + title + bounds — changed or closed since the premise it
  was grounded from: true staleness. Ordinary pixel changes from earlier items do NOT
  stop the batch; ``CORTEX_QUEUE_STRICT_DIGEST=1`` restores the v0.5.6 whole-screen
  digest stop). An UNCERTAIN verification does not stop the queue
  (REM-B: uncertain is not failure), and neither does the ``failed`` verdict
  of an EXECUTED item (the input dispatched, the next item re-grounds from
  the fresh post-action capture, and the honest verdict rides the per-item entry;
  ``CORTEX_QUEUE_STRICT_VERIFY=1`` restores the strict stop-on-failed behavior).
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

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
    ExecutionResult,
    GroundedAction,
    GroundingResult,
    Observation,
    Point,
    VerificationResult,
)
from .observation import ObservationEngine, digest_matches
from .safety import SafetyPolicy
from .state import StopToken, TaskState, TaskStopped
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
_LAUNCH_PREFIXES: tuple[str, ...] = ("open ", "launch ", "start ", "switch to ", "focus ")

#: Observe phases that are INTRA-STEP verification captures (PERF-004 C2): burst-exempt.
#: The rate gate protects fresh observations (loop_top, host-driven direct_request,
#: recovery re-observes); these phases capture within one logical action step and skip
#: the interval wait while still recording into the enforcer.
_INTRA_STEP_EXEMPT_PHASES = frozenset({"validate", "post_action", "revalidate"})

#: Cursor-at-target tolerance for the deterministic ``move`` verification predicate (px).
_CURSOR_TOLERANCE_PX = 2

# --- capture-source integrity guard (v0.7.1 Defect A) ----------------------------------------
#
#: Consecutive byte-identical pre/post capture pairs (across DISTINCT screen-affecting
#: actions) after which the capture SOURCE itself is called suspect. Field evidence
#: (v0.7.0 live Blender session): a dead/frozen capture source returned byte-identical
#: frames for every action, so the pixel tier confidently reported
#: ``Mean pixel difference 0.000000`` forever — a verdict-shaped lie. The marker never
#: changes a verdict; it converts that silent stream into typed, actionable diagnostics.
_CAPTURE_SUSPECT_THRESHOLD = 3

#: Marker vocabulary — same uppercase family as TARGET_GONE / FOCUS_DRIFTED /
#: STUCK_MODIFIER / NO_INSTANCE. Generic by design (any frozen capture source: a
#: disconnected session, a protected desktop, a stale duplicator — never app-specific).
CAPTURE_SOURCE_SUSPECTED = "CAPTURE_SOURCE_SUSPECTED"

#: Action kinds whose execution makes no screen-affecting claim; their (naturally
#: static) capture pairs must never feed the suspicion counter.
_CAPTURE_PAIR_EXEMPT_KINDS = frozenset({ActionType.WAIT, ActionType.DONE})

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


# --- queue premise-identity knob -----------------------------------------------------------
#: Env knob name: ``CORTEX_QUEUE_STRICT_DIGEST`` — set to exactly ``1`` to restore the
#: v0.5.6 whole-screen digest-surprise stop for QUEUED follow-ups (any pixel change
#: since the item's premise stops the batch). Default (unset/any other value): the
#: per-item validity check governs — the attached window's identity (hwnd + title +
#: bounds) unchanged AND the normal per-item grounding still applying means the item
#: executes; a changed/closed attached window is true staleness and still stops
#: (digest_surprise). Live evidence: the whole-screen stop fired on EVERY batch
#: whose earlier items legitimately changed the screen (drawing/typing), forcing one
#: full model turn per action. Read lazily at each queue decision (test-toggleable
#: like CORTEX_DIFF_FAST / CORTEX_QUEUE_STRICT_VERIFY).
QUEUE_STRICT_DIGEST_ENV = "CORTEX_QUEUE_STRICT_DIGEST"


def _queue_strict_digest() -> bool:
    """Resolve the strict-digest escape hatch (only the exact string ``1`` enables)."""
    import os

    return os.environ.get(QUEUE_STRICT_DIGEST_ENV, "").strip() == "1"


def _queue_window_identity(observation: Observation) -> tuple[Any, ...] | None:
    """The attached-window identity tuple (hwnd, title, bounds), or ``None``.

    ``None`` means the observation carries NO window identity at all (a backend
    without identity reporting, or no foreground window) — the caller decides the
    policy for that case; the identity is never partially compared.
    """
    info = observation.active_window_info
    if info is None:
        return None
    return (
        info.hwnd,
        info.title or "",
        tuple(info.bounds) if info.bounds is not None else None,
    )


def _queue_premise_valid(premise: Observation, current: Observation) -> bool:
    """Per-item queue validity: is this queued item's premise still current?

    Default (``CORTEX_QUEUE_STRICT_DIGEST`` unset): VALID when the attached window's
    identity (hwnd + title + bounds) is UNCHANGED between the item's premise capture
    and the fresh validate capture — earlier queue items legitimately change pixels
    (drawing, typing, dialogs) without invalidating the window the driver is driving.
    Identity-unavailable asymmetry is true staleness: the attached window CLOSED
    (premise had identity, current has none) or APPEARED (the reverse) stops the
    batch. When NEITHER side carries identity (identity-less backends), the legacy
    whole-screen digest comparison governs so the protection never silently weakens.
    ``CORTEX_QUEUE_STRICT_DIGEST=1`` restores the v0.5.6 whole-screen digest stop.
    The response-side digest_surprise concept for the PRIMARY action is untouched.
    """
    if _queue_strict_digest():
        return digest_matches(premise, current)
    premise_id = _queue_window_identity(premise)
    current_id = _queue_window_identity(current)
    if premise_id is None or current_id is None:
        if premise_id != current_id:
            return False  # the attached window closed or appeared: true staleness
        return digest_matches(premise, current)  # identity-less backend: legacy probe
    return premise_id == current_id


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
        self.session_id = session_id
        self.task = task if task is not None else TaskState()
        self.stop_token = stop if stop is not None else StopToken()
        self.limits = (limits if limits is not None else Limits()).validate()
        self.enforcer = enforcer if enforcer is not None else LimitEnforcer(self.limits)
        self.auditor = auditor if auditor is not None else AuditLogger()
        self.metrics = metrics if metrics is not None else Metrics()
        self.allowed_processes = list(allowed_processes or [])
        self.grounding = grounding if grounding is not None else GroundingRouter()
        # T8 Interference Guard (protection upgrade; runs as ADDITIONAL gates — it can
        # only add rejections/annotations, never bypass an existing one). Defaults to
        # the A12 protective policy when the caller omits it.
        self.interference = interference if interference is not None else parse_interference(None)
        self.guard = InterferenceGuard(backend, self.interference, emit=self._audit_guard_event)
        # v0.7.1 Defect A: capture-source integrity guard state (O(1), per session).
        # ``_capture_identical_pairs`` counts CONSECUTIVE byte-identical pre/post
        # visual-tier capture pairs across DISTINCT screen-affecting actions; the
        # action-id/base pair makes the expected-text fallback re-verify replace its
        # own contribution instead of double-counting one action.
        self._capture_identical_pairs = 0
        self._capture_pairs_action_id: str | None = None
        self._capture_pairs_counter_base = 0

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
                # R-01: the deterministic text needle for a TYPE action is the TYPED
                # TEXT itself — the string that actually lands in the field's value —
                # not the effect prose. Searching the prose made the deterministic
                # ui_control_text tier abstain on EVERY stated-effect type action and
                # escalated them all to the pixel band; searching the typed text lets
                # the deterministic tier decide (verified when the text is visible in
                # the window title or a control's name/value) with the pixel tier kept
                # as the escalation for genuinely invisible content (canvas, grid).
                expected_text = action.text or effect
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
            # R-01 deterministic-first default for TYPE: the typed text is the primary
            # needle (see the model-judge branch comment); every other expected-text
            # source keeps the effect-first preference. The tier's absence verdict is
            # UNCERTAIN (never failed), so an invisible needle can only escalate.
            if action.action is ActionType.TYPE:
                expected_text = action.text or effect
            else:
                expected_text = effect or action.text
        elif kind == VerificationKind.WINDOW_STATE.value:
            window_title = window_title or effect
        elif kind == VerificationKind.PROCESS_STATE.value:
            process_name = process_name or effect
        elif kind == VerificationKind.VISUAL_CHANGE.value and effect:
            expected_change = True
            # REM-B (H2c): a CLICK/DOUBLE_CLICK with a stated effect is a focus-type
            # expectation ("Hex input focused") — flag it so the deterministic
            # FocusChangeStrategy tier (UIA focused element / window identity /
            # corroborated digest) runs BEFORE the pixel-diff tier.
            # does NOT widen this flag: FocusChangeStrategy's transition signals stay
            # click-scoped (generalizing them let a mere window change "verify" a
            # stated type effect — a false success the fault-injection pin catches).
            # The D11 doctrine lives in ScreenshotDiffStrategy: ANY intent carrying a
            # stated expected_effect degrades a zero/sub-threshold diff to
            # ``uncertain`` instead of a definitive false ``failed`` (keypress Enter
            # committing a shape with sub-threshold dashed handles). Unstated
            # expectations are untouched.
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

    def _capture_source_guard(
        self, action: GroundedAction | None, result: VerificationResult
    ) -> tuple[VerificationResult, str | None]:
        """v0.7.1 Defect A: detect a dead/frozen capture source and say so honestly.

        Consumes the visual tier's ``capture_provenance`` (hash+size of the pre/post
        frames ALREADY diffed — no new captures): a byte-identical pair feeds a
        per-session counter of consecutive identical pairs across DISTINCT
        screen-affecting actions; any pair whose hashes/bytes differ RESETS it;
        pairs without provenance (a deterministic tier decided first, a starved
        ladder) and exempt kinds (``wait``/``done`` — and ensure_app probe results,
        which never reach this method with provenance) leave it untouched.

        At :data:`_CAPTURE_SUSPECT_THRESHOLD` consecutive identical pairs the result
        is re-issued with the typed marker ``CAPTURE_SOURCE_SUSPECTED
        identical_pairs=<n>`` appended to note+evidence, and the marker string is
        returned for the verification audit metadata. THE VERDICT ITSELF IS NEVER
        ALTERED — ``verified``/``failed``/``uncertain`` stay exactly what the
        evidence honestly supports (fail-open on the verdict, fail-loud in
        diagnostics). The expected-text fallback re-verifies the SAME action; the
        stored counter base makes that re-verification replace its own contribution
        so one action counts at most one pair.
        """
        provenance = result.capture_provenance
        if provenance is None or action is None or action.action in _CAPTURE_PAIR_EXEMPT_KINDS:
            return result, None
        if action.action_id != self._capture_pairs_action_id:
            self._capture_pairs_action_id = action.action_id
            self._capture_pairs_counter_base = self._capture_identical_pairs
        else:
            # same action re-verified (expected-text fallback): undo the previous
            # contribution before re-applying, so the pair is counted exactly once.
            self._capture_identical_pairs = self._capture_pairs_counter_base
        identical = (
            provenance.before_bytes == provenance.after_bytes
            and provenance.before_sha256 == provenance.after_sha256
        )
        if identical:
            self._capture_identical_pairs += 1
        else:
            self._capture_identical_pairs = 0
        if self._capture_identical_pairs < _CAPTURE_SUSPECT_THRESHOLD:
            return result, None
        marker = f"{CAPTURE_SOURCE_SUSPECTED} identical_pairs={self._capture_identical_pairs}"
        annotated = result.model_copy(
            update={
                "note": result.note if marker in result.note else f"{result.note} | {marker}"[:900],
                "evidence": [*result.evidence, marker],
            }
        )
        return annotated, marker

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
        # v0.7.1 Defect A: honest capture-source diagnostics (marker rides ALONGSIDE
        # the verdict; the verdict itself is never altered).
        result, capture_marker = self._capture_source_guard(action, result)
        duration_ms = (time.perf_counter() - started) * 1000.0
        self.metrics.record_latency("verification_ms", duration_ms)
        if result.outcome == "verified":
            self.metrics.incr("verification_verified")
        elif result.outcome == "failed":
            self.metrics.incr("verification_failed")
        else:
            self.metrics.incr("verification_uncertain")
        provenance = result.capture_provenance
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
                # honest provenance on every verified-action audit event (None dropped)
                "capture_provenance": (
                    (
                        f"before={provenance.before_sha256[:12]}:{provenance.before_bytes} "
                        f"after={provenance.after_sha256[:12]}:{provenance.after_bytes}"
                    )
                    if provenance is not None
                    else None
                ),
                "capture_source_suspected": capture_marker,
            },
        )
        return result

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
        approval requirement, validator rejection, PREMISE INVALIDITY (the
        attached window's identity changed or closed since the item's premise — true
        staleness; ordinary pixel changes from earlier items do not stop the batch, and
        ``CORTEX_QUEUE_STRICT_DIGEST=1`` restores the v0.5.6 whole-screen digest stop),
        stop token, limit trip, or a missing post-action observation. REM-B (H2b): an
        item whose verification is UNCERTAIN (no expectation stated / non-visual action
        the diff cannot judge) does NOT stop the queue — uncertain is not failure — but
        its per-item result keeps the honest uncertain verdict and ``ok=False``.
        an EXECUTED item whose verification outcome is ``failed`` also does
        NOT stop the queue by default — the input dispatched and the next item
        re-grounds from the fresh post-action capture; the honest failed verdict rides
        the per-item entry. ``CORTEX_QUEUE_STRICT_VERIFY=1`` restores the v0.5.5
        stop-on-failed behavior. R-18: an item that cannot even be CONVERTED to a
        :class:`GroundedAction` (target-less ``focus_window``, half-specified ``drag``,
        1-key ``hotkey``, ...) stops before ANY dispatch with the typed
        ``invalid_follow_up`` stop — zero items run, no raw pydantic
        ``ValidationError`` escapes (the MCP boundary maps the same condition onto the
        teaching ``invalid_action`` rejection).
        """
        queue_items: list[tuple[GroundedAction, str | None]] = [(action, expected_effect)]
        for position, spec in enumerate(follow_ups, start=1):
            try:
                grounded_spec = spec.to_grounded(reason_prefix="MCP follow_up action")
            except Exception as exc:  # noqa: BLE001 - R-18: never an uncaught ValidationError
                # The conversion loop precedes the pipeline loop, so NOTHING has
                # dispatched — the legacy fail-closed effect is preserved exactly;
                # only the error SURFACE changes: a typed rejected outcome (the server
                # boundary pre-validates the same condition and answers with the
                # teaching ``invalid_action`` shape; this guard covers direct
                # ``run_single`` callers with the same zero-uncaught guarantee).
                rejection = SingleActionOutcome(
                    kind="rejected",
                    reasons=[
                        (
                            f"follow_ups[{position}] ({spec.action.value}) is not a "
                            f"valid action: {exc}"
                        ),
                        (
                            "Valid shapes: focus_window needs a non-empty target window "
                            "title; drag needs x,y start AND x2,y2 end; hotkey needs 2-12 "
                            "key names (single keys belong on keypress); move needs x,y; "
                            "ensure_app needs a non-empty \"process[|doc-token]\" target."
                        ),
                    ],
                    message=(
                        f"Queue rejected before dispatch: follow_ups[{position}] "
                        f"({spec.action.value}) is not a valid action: {str(exc)[:180]}"
                    ),
                    follow_up_results=[
                        {
                            "index": position,
                            "action_type": spec.action.value,
                            "kind": "rejected",
                            "ok": False,
                            "message": f"{type(exc).__name__}: {str(exc)[:200]}",
                            "reasons": ["invalid_action"],
                            "requires_approval": False,
                            "model_confidence": None,
                            "grounding_confidence": None,
                            "verification_confidence": None,
                        }
                    ],
                    follow_ups_stopped_reason="invalid_follow_up",
                )
                self._audit(
                    "queue",
                    action=action,
                    result="invalid_follow_up",
                    metadata={
                        "items": len(follow_ups) + 1,
                        "executed": 0,
                        "stopped_reason": "invalid_follow_up",
                        "invalid_index": position,
                        "invalid_action": spec.action.value,
                    },
                )
                return rejection
            queue_items.append((grounded_spec, spec.expected_effect))
        follow_up_results: list[dict[str, Any]] = []
        stopped_reason: str | None = None
        source_observation: Observation | None = None
        executed_count = 0
        first_outcome: SingleActionOutcome | None = None
        for index, (item, item_effect) in enumerate(queue_items):
            self.stop_token.ensure_live()  # P0-C: kill path checked between queue items
            strict_digest = index > 0  # follow-ups stop on attached-window identity change
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
        follow-ups) applies per-item premise validity before executing the
        speculative item: the attached window's identity (hwnd + title + bounds)
        changed or closed since the premise -> a ``digest_surprise`` stop
        (``CORTEX_QUEUE_STRICT_DIGEST=1`` restores the v0.5.6 whole-screen digest
        comparison; ordinary pixel changes from earlier items no longer stop).

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
            if strict_digest and not _queue_premise_valid(observation, current):
                # PERF-004 C7: the queued action's premise is no longer
                # current. Per-item validity: the attached window's identity (hwnd +
                # title + bounds) changed or closed since the premise — TRUE
                # staleness; speculative actions never run against a window nobody is
                # driving. A mere pixel change (earlier items drew/typed) does NOT
                # stop the batch anymore; ``CORTEX_QUEUE_STRICT_DIGEST=1`` restores
                # the legacy whole-screen digest stop.
                premise_id = _queue_window_identity(observation)
                current_id = _queue_window_identity(current)
                if (
                    premise_id is not None
                    and current_id is not None
                    and premise_id != current_id
                ):
                    detail = "the attached window's identity changed"
                elif premise_id is not None and current_id is None:
                    detail = "the attached window closed"
                elif premise_id is None and current_id is not None:
                    detail = "an attached window identity appeared where none was"
                else:
                    detail = "the screen changed since the premise (identity-less backend)"
                self.metrics.incr("digest_surprise")
                self._audit(
                    "validation",
                    observation=current,
                    action=action,
                    result="digest_surprise",
                    metadata={
                        "reason": (
                            f"Post-action staleness: {detail} since the queued "
                            "action's premise was captured; the queue stopped."
                        ),
                        "premise_window": premise_id,
                        "current_window": current_id,
                    },
                )
                return (
                    SingleActionOutcome(
                        kind="digest_surprise",
                        message=(
                            f"Post-action staleness: {detail} since this queued "
                            "action's premise was captured; the queue stopped "
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
