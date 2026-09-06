"""Bounded recovery: failure classification (Goal.md section 8 taxonomy) and plans.

Layering (master-mission section 5): this module imports only ``models``, ``state``, and
``limits``. Sibling exception types (backend/validator/grounding/provider) are recognized
STRUCTURALLY — by class name or stable attributes — so recovery stays below the controller
layer and does not depend on sibling import surfaces that evolve in parallel waves.

Doctrine (Goal.md section 8, master-mission P0-B):
- Failures are classified into the 12-value :class:`~computer_use_mcp.models.FailureClass`
  taxonomy; :class:`TaskStopped` is NOT a failure (the classifier returns ``None``).
- Recovery is bounded: per-action and per-task recovery budgets come from
  :class:`~computer_use_mcp.limits.Limits` (defaults 2/action, 6/task); a budget that is
  exhausted terminates the task safely instead of looping.
- Recovery NEVER blind-retries the same coordinates: coordinate-bearing failure classes
  re-observe and re-decide (the failed action's coordinates are discarded); only
  explicitly bounded same-instance retries (``RETRY_ONCE`` / dismiss-then-retry) reuse an
  approved action instance, and even those re-ground and re-validate against a FRESH
  observation first.
- Authentication prompts are never satisfied automatically: ``AUTH_REQUIRED`` always
  terminates safely (credentials are never auto-typed — hard rule).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .limits import LimitEnforcer, Limits
from .models import FailureClass, Observation, TerminationReason
from .state import TaskStopped

__all__ = [
    "RecoveryContext",
    "RecoveryController",
    "RecoveryPlan",
    "RecoveryStrategy",
    "classify_failure",
    "classify_verification_failure",
]


class RecoveryStrategy(StrEnum):
    """What the controller should do next after a classified failure."""

    RECOVER_REOBSERVE = "recover_reobserve"
    RECOVER_DISMISS = "recover_dismiss"
    RETRY_ONCE = "retry_once"
    REPLAN = "replan"
    TERMINATE_SAFELY = "terminate_safely"
    COMPLETE = "complete"


# --- structural exception recognition (no sibling imports) --------------------------------

_BLOCKED_UI_ERROR_NAMES = frozenset({"InputBlockedError"})
_STALE_COORDINATE_ERROR_NAMES = frozenset({"CoordinateSpaceError", "StaleObservationError"})
_APP_CRASH_ERROR_NAMES = frozenset({"DisplayUnavailableError"})
_UNSUPPORTED_ACTION_ERROR_NAMES = frozenset({"UnsupportedActionError"})
_GROUNDING_ERROR_NAMES = frozenset({"UnsupportedGroundingError"})
_PROVIDER_ERROR_NAMES = frozenset({"ProviderError", "ProviderParseError", "ProviderHTTPError"})

#: ValidationOutcome code -> FailureClass (validator codes are stable strings).
_VALIDATION_CODE_MAP: dict[str, FailureClass] = {
    "point_out_of_bounds": FailureClass.STALE_COORDINATES,
    "missing_observation_binding": FailureClass.STALE_COORDINATES,
    "observation_binding_mismatch": FailureClass.STALE_COORDINATES,
    "STALE_OBSERVATION": FailureClass.STALE_COORDINATES,
    "coordinate_space_unverifiable": FailureClass.STALE_COORDINATES,
    "window_not_allowed": FailureClass.WRONG_WINDOW,
    "window_identity_unavailable": FailureClass.WRONG_WINDOW,
    "process_not_allowed": FailureClass.WRONG_WINDOW,
    "process_identity_unavailable": FailureClass.WRONG_WINDOW,
    "confidence_below_floor": FailureClass.LOW_CONFIDENCE,
    "missing_point": FailureClass.LOW_CONFIDENCE,
    "missing_text": FailureClass.LOW_CONFIDENCE,
    "missing_keys": FailureClass.LOW_CONFIDENCE,
    "missing_target": FailureClass.LOW_CONFIDENCE,
}

#: Dialog-like window title markers (heuristic; matched case-insensitively as substrings).
_DIALOG_TITLE_MARKERS: tuple[str, ...] = (
    "dialog",
    "error",
    "warning",
    "confirm",
    "prompt",
    "save as",
    "open file",
    "user account control",
    "uac",
    "properties",
)

#: Authentication-like window title markers. A dialog asking for credentials maps to
#: ``AUTH_REQUIRED`` so the controller fails safely instead of typing into it.
_AUTH_TITLE_MARKERS: tuple[str, ...] = (
    "sign in",
    "signin",
    "log in",
    "login",
    "password",
    "credential",
    "user name",
    "username",
    "authenticate",
    "verification code",
    "2fa",
    "passkey",
)


def _window_key(observation: Observation | None) -> tuple[Any, ...] | None:
    """Stable active-window identity key (hwnd, title, process); None when unknown."""
    if observation is None:
        return None
    info = observation.active_window_info
    if info is not None:
        return (info.hwnd, (info.title or "").casefold(), (info.process_name or "").casefold())
    if observation.active_window:
        return (None, observation.active_window.casefold(), None)
    return None


def _active_title(observation: Observation | None) -> str:
    if observation is None:
        return ""
    info = observation.active_window_info
    if info is not None and info.title:
        return info.title
    return observation.active_window or ""


def _active_hwnd(observation: Observation | None) -> int | None:
    if observation is not None and observation.active_window_info is not None:
        return observation.active_window_info.hwnd
    return None


def classify_verification_failure(
    source: Observation | None, after: Observation | None
) -> FailureClass:
    """Classify a definitive verification failure from propose-time vs after observations.

    Heuristics (documented, deterministic):
    - active-window identity changed AND the new title looks like an auth prompt ->
      ``AUTH_REQUIRED`` (fail safely; never auto-type credentials);
    - identity changed AND the new title looks dialog-like -> ``UNEXPECTED_DIALOG``;
    - identity changed, same hwnd but different title -> ``NAVIGATION_DRIFT`` (in-app
      navigation moved the UI context);
    - identity changed (different hwnd/process) -> ``WRONG_WINDOW``;
    - same window -> ``MOVED_UI`` (the target moved within the window or the expected
      effect did not occur).
    """
    source_key = _window_key(source)
    after_key = _window_key(after)
    if source_key is not None and after_key is not None and source_key != after_key:
        title = _active_title(after).casefold()
        if any(marker in title for marker in _AUTH_TITLE_MARKERS):
            return FailureClass.AUTH_REQUIRED
        if any(marker in title for marker in _DIALOG_TITLE_MARKERS):
            return FailureClass.UNEXPECTED_DIALOG
        if _active_hwnd(source) is not None and _active_hwnd(source) == _active_hwnd(after):
            return FailureClass.NAVIGATION_DRIFT
        return FailureClass.WRONG_WINDOW
    return FailureClass.MOVED_UI


def classify_failure(
    failure: Any,
    context: RecoveryContext | None = None,
) -> FailureClass | None:
    """Classify an exception, validation outcome, or verification result.

    Returns ``None`` for :class:`TaskStopped` — a stop is not a failure; callers must
    treat it as the kill path (P0-C), never as a recoverable condition. Unknown inputs
    classify as :attr:`FailureClass.UNKNOWN` (fail closed).

    Args:
        failure: One of — an exception instance; a :class:`TaskStopped`; a validation
            outcome carrying ``codes`` (e.g. ``ValidationOutcome``); a verification result
            carrying ``outcome`` (e.g. ``VerificationResult``); or a plain code string.
        context: Optional context used for observation-based heuristics (window-identity
            comparison) and explicit ``failure_class`` overrides.
    """
    if context is not None and context.failure_class is not None:
        return context.failure_class
    if isinstance(failure, TaskStopped):
        return None
    if isinstance(failure, str):
        return _VALIDATION_CODE_MAP.get(failure, FailureClass.UNKNOWN)
    if isinstance(failure, BaseException):
        name = type(failure).__name__
        if name in _BLOCKED_UI_ERROR_NAMES:
            return FailureClass.BLOCKED_UI
        if name in _STALE_COORDINATE_ERROR_NAMES:
            return FailureClass.STALE_COORDINATES
        if name in _APP_CRASH_ERROR_NAMES:
            return FailureClass.APP_CRASH
        if name in _UNSUPPORTED_ACTION_ERROR_NAMES:
            return FailureClass.UNKNOWN
        if name in _PROVIDER_ERROR_NAMES:
            return FailureClass.LOW_CONFIDENCE
        if name in _GROUNDING_ERROR_NAMES:
            # Grounding refused: treat as UI drift unless the window identity changed.
            if context is not None and classify_verification_failure(
                context.source_observation, context.after_observation
            ) in {FailureClass.WRONG_WINDOW, FailureClass.AUTH_REQUIRED, FailureClass.UNEXPECTED_DIALOG}:
                return FailureClass.WRONG_WINDOW
            return FailureClass.MOVED_UI
        return FailureClass.UNKNOWN

    codes = getattr(failure, "codes", None)
    if isinstance(codes, (list, tuple, set)):
        for code in codes:
            if code in _VALIDATION_CODE_MAP:
                return _VALIDATION_CODE_MAP[code]
        return FailureClass.UNKNOWN

    outcome = getattr(failure, "outcome", None)
    if outcome is not None:
        if outcome == "failed":
            if context is not None:
                return classify_verification_failure(context.source_observation, context.after_observation)
            return FailureClass.MOVED_UI
        if outcome == "uncertain":
            return FailureClass.LOW_CONFIDENCE
        return FailureClass.UNKNOWN

    return FailureClass.UNKNOWN


@dataclass
class RecoveryContext:
    """Everything the classifier/controller may need about one failure.

    Attributes:
        phase: Pipeline phase that failed (``observe``/``decide``/``ground``/``validate``/
            ``risk``/``approval``/``execute``/``verify``) — drives the termination-reason
            mapping when recovery is exhausted.
        action: The failing action instance, when one existed.
        message: Human-readable failure detail (already redacted upstream where needed).
        source_observation: The observation the action was grounded/proposed from.
        after_observation: The post-action observation (verification failures).
        failure_class: Explicit classification override (bypasses inference).
        dismiss_allowed: Whether policy permits the Escape-key dismiss attempt for
            ``BLOCKED_UI``; when False the controller plans a replan instead.
        goal: Current task goal (for context/telemetry only — never executed).
        details: Free-form structured extras for audit metadata.
    """

    phase: str = "unknown"
    action: Any = None
    message: str = ""
    source_observation: Observation | None = None
    after_observation: Observation | None = None
    failure_class: FailureClass | None = None
    dismiss_allowed: bool = True
    goal: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryPlan:
    """The controller's decision for one classified failure.

    Attributes:
        strategy: The recovery strategy to execute.
        failure_class: The classified failure this plan responds to.
        reason: Human-readable explanation (audited; safe to surface).
        reobserve: Take a fresh observation before the next attempt.
        redecide: Re-consult the model with the updated task state + failure summary
            (replanning); the failed action's coordinates are discarded.
        retry_same_instance: Re-execute the SAME approved action instance after fresh
            grounding/validation (approval budget is NOT re-consumed).
        dismiss: Attempt the single bounded Escape-key dismiss (``BLOCKED_UI`` only).
        then_replan: After one same-instance retry, fall back to replanning.
        termination_reason: Set only for ``TERMINATE_SAFELY`` plans.
        details: Structured extras for audit metadata.
    """

    strategy: RecoveryStrategy
    failure_class: FailureClass
    reason: str
    reobserve: bool = False
    redecide: bool = False
    retry_same_instance: bool = False
    dismiss: bool = False
    then_replan: bool = False
    termination_reason: TerminationReason | None = None
    details: dict[str, Any] = field(default_factory=dict)


#: Failure classes that allow a bounded recovery attempt (budget-checked).
_RECOVERABLE_CLASSES: frozenset[FailureClass] = frozenset(
    {
        FailureClass.STALE_COORDINATES,
        FailureClass.MOVED_UI,
        FailureClass.WRONG_WINDOW,
        FailureClass.UNEXPECTED_DIALOG,
        FailureClass.BLOCKED_UI,
        FailureClass.NAVIGATION_DRIFT,
        FailureClass.APP_CRASH,
        FailureClass.LOW_CONFIDENCE,
    }
)

#: Default termination reason per failing phase when recovery is exhausted.
_PHASE_TERMINATION: dict[str, TerminationReason] = {
    "decide": TerminationReason.PROVIDER_ERROR,
    "verify": TerminationReason.FAILED_VERIFICATION,
}


class RecoveryController:
    """Maps classified failures to bounded :class:`RecoveryPlan` decisions.

    Args:
        limits: A :class:`~computer_use_mcp.limits.Limits` (budget checks against the
            static bounds) or a :class:`~computer_use_mcp.limits.LimitEnforcer` (budget
            checks against live counters — the controller-agent wiring). ``None`` uses
            default limits without live counters.
    """

    def __init__(self, limits: Limits | LimitEnforcer | None = None) -> None:
        if isinstance(limits, LimitEnforcer):
            self._enforcer: LimitEnforcer | None = limits
            self.limits: Limits = limits.limits
        else:
            self._enforcer = None
            self.limits = (limits if limits is not None else Limits()).validate()

    def _recovery_budget_available(self) -> bool:
        """True when both per-action and per-task recovery budgets have headroom."""
        if self._enforcer is None:
            return True
        try:
            self._enforcer.check_recovery()
        except Exception:  # noqa: BLE001 - budget exhaustion is a planned condition
            return False
        return True

    def _exhausted_termination(self, context: RecoveryContext | None) -> TerminationReason:
        phase = context.phase if context is not None else "unknown"
        return _PHASE_TERMINATION.get(phase, TerminationReason.UNRECOVERABLE)

    def handle(self, failure_class: FailureClass, context: RecoveryContext | None = None) -> RecoveryPlan:
        """Produce the recovery plan for ``failure_class`` (never raises)."""
        context = context or RecoveryContext()
        failure_class = FailureClass(failure_class)

        if failure_class is FailureClass.ALREADY_COMPLETED:
            return RecoveryPlan(
                strategy=RecoveryStrategy.COMPLETE,
                failure_class=failure_class,
                reason="The goal state is already reached; completing the task.",
            )

        if failure_class is FailureClass.AUTH_REQUIRED:
            return RecoveryPlan(
                strategy=RecoveryStrategy.TERMINATE_SAFELY,
                failure_class=failure_class,
                reason=(
                    "Authentication is required; failing safely. Credentials are never "
                    "typed automatically (hard safety rule)."
                ),
                termination_reason=TerminationReason.BLOCKED_SAFETY,
                details={"phase": context.phase},
            )

        if failure_class in {FailureClass.UNRECOVERABLE, FailureClass.UNKNOWN}:
            return RecoveryPlan(
                strategy=RecoveryStrategy.TERMINATE_SAFELY,
                failure_class=failure_class,
                reason=(
                    f"Failure {failure_class.value} in phase {context.phase!r} is not "
                    "recoverable; terminating safely."
                ),
                termination_reason=TerminationReason.UNRECOVERABLE,
                details={"phase": context.phase, "message": context.message[:300]},
            )

        # Every remaining class is recoverable — enforce the bounded budget first.
        if not self._recovery_budget_available():
            limits = self.limits
            return RecoveryPlan(
                strategy=RecoveryStrategy.TERMINATE_SAFELY,
                failure_class=failure_class,
                reason=(
                    f"Recovery budget exhausted (per-action {limits.max_recovery_per_action}, "
                    f"per-task {limits.max_recovery_per_task}) after {failure_class.value} "
                    f"in phase {context.phase!r}; terminating safely."
                ),
                termination_reason=self._exhausted_termination(context),
                details={"phase": context.phase, "budget_exhausted": True},
            )

        if failure_class is FailureClass.BLOCKED_UI:
            if context.dismiss_allowed:
                return RecoveryPlan(
                    strategy=RecoveryStrategy.RECOVER_DISMISS,
                    failure_class=failure_class,
                    reason=(
                        "Physical input was blocked; attempting one bounded Escape dismiss, "
                        "then re-observing and retrying the same action instance."
                    ),
                    reobserve=True,
                    retry_same_instance=True,
                    dismiss=True,
                    details={"phase": context.phase},
                )
            return RecoveryPlan(
                strategy=RecoveryStrategy.REPLAN,
                failure_class=failure_class,
                reason="Input is blocked and dismissal is not permitted; replanning.",
                reobserve=True,
                redecide=True,
                details={"phase": context.phase},
            )

        if failure_class is FailureClass.LOW_CONFIDENCE:
            return RecoveryPlan(
                strategy=RecoveryStrategy.RETRY_ONCE,
                failure_class=failure_class,
                reason=(
                    "Outcome confidence was too low; retrying once with fresh grounding, "
                    "then replanning."
                ),
                reobserve=True,
                retry_same_instance=True,
                then_replan=True,
                details={"phase": context.phase},
            )

        if failure_class is FailureClass.APP_CRASH:
            return RecoveryPlan(
                strategy=RecoveryStrategy.REPLAN,
                failure_class=failure_class,
                reason="Application/display state crashed or disappeared; re-deciding from fresh state.",
                reobserve=True,
                redecide=True,
                details={"phase": context.phase},
            )

        # STALE_COORDINATES / MOVED_UI / WRONG_WINDOW / UNEXPECTED_DIALOG / NAVIGATION_DRIFT:
        # fresh observation + fresh grounding + re-decide; the failed coordinates are discarded.
        return RecoveryPlan(
            strategy=RecoveryStrategy.RECOVER_REOBSERVE,
            failure_class=failure_class,
            reason=(
                f"{failure_class.value} in phase {context.phase!r}; re-observing, discarding the "
                "failed coordinates, and re-deciding with fresh grounding."
            ),
            reobserve=True,
            redecide=True,
            details={"phase": context.phase},
        )
