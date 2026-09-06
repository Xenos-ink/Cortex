"""Approval epochs and the prolonged-unattended runtime policy modifier (spec sections 11-12).

Layering rule (master-mission section 5): this module imports only ``models`` and
``limits`` — never orchestration, provider, or MCP modules. It lives in the
orchestration/policy layer ABOVE :class:`~computer_use_mcp.safety.SafetyPolicy` and
CONSUMES its decisions (``SafetyDecision.risk`` / ``requires_approval``); the semantics
of ``SafetyPolicy.evaluate``/``classify`` are untouched.

Doctrines implemented here:

- Approval epochs (spec section 11, conflict C9): an epoch is granted when approval is
  granted at session start and expires on TWO independent axes — wall-clock age
  (``Limits.approval_epoch_seconds``, default 30 minutes) and the count of interactive
  actions (``Limits.approval_epoch_actions``, default 50) — whichever comes first ends
  the epoch. A material change (application/process changed, risk escalated, goal
  changed, policy changed, expected environment changed) invalidates the epoch
  IMMEDIATELY via :meth:`ApprovalEpochManager.invalidate`. After expiry/invalidation
  every approval-requiring action stops FAIL-CLOSED: :meth:`ApprovalEpochManager.authorize_action`
  returns a refusal with ``requires_fresh_approval=True`` and the caller must stop and
  request fresh approval; nothing auto-renews and no accumulation of old grants exists —
  fresh approval is a NEW epoch via :meth:`ApprovalEpochManager.grant`.
- ``require_approval=false`` is NOT an unlimited pass: it is modeled as a full-scope
  epoch (the standing grant covers interactive actions without the per-action approval
  flow) that still expires exactly like any other epoch. The long-running policy applies
  on top of the session-start flag; it never bypasses it.
- Prolonged unattended execution (spec section 12): :func:`effective_protection` is a
  pure raise-only runtime policy modifier. Once the session has run (without human
  interaction) for at least one hour it may only RAISE effective protection — MEDIUM is
  treated as HIGH, LOW as MEDIUM, CRITICAL stays CRITICAL (already maximal) — and
  non-routine actions (original risk MEDIUM and above) require fresh approval. There is
  NO fifth RiskLevel and no risk level is ever lowered.

All mutable state is guarded by ``threading.RLock``; every time-dependent input accepts
an explicit ``now`` (or an injectable clock) so tests are deterministic without sleeps.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, Field

from .limits import Limits
from .models import RiskLevel

__all__ = [
    "PROLONGED_UNATTENDED_SECONDS",
    "ApprovalDecision",
    "ApprovalEpoch",
    "ApprovalEpochExpired",
    "ApprovalEpochManager",
    "ExpiryAxis",
    "InvalidationReason",
    "UnattendedProtection",
    "effective_protection",
]


def _new_id() -> str:
    return uuid.uuid4().hex


#: Detail strings stored on the epoch are bounded (they may echo controller context).
_INVALIDATION_DETAIL_MAX_CHARS = 500


class InvalidationReason(StrEnum):
    """Typed material-change reasons that invalidate a live epoch immediately (§11)."""

    APPLICATION_CHANGED = "application_changed"
    PROCESS_CHANGED = "process_changed"
    RISK_ESCALATED = "risk_escalated"
    GOAL_CHANGED = "goal_changed"
    POLICY_CHANGED = "policy_changed"
    ENVIRONMENT_CHANGED = "environment_changed"


class ExpiryAxis(StrEnum):
    """Which mechanism ended the epoch (reported for audit and fail-closed messages)."""

    TIME = "time"
    ACTIONS = "actions"
    INVALIDATED = "invalidated"


class ApprovalEpoch(BaseModel):
    """One granted approval epoch with bounded expiry state.

    An epoch is the container of a grant: it expires when its wall-clock age reaches
    ``max_age_seconds`` OR ``interactive_actions_used`` reaches ``max_interactive_actions``
    (whichever first), and dies instantly when ``invalidated``. Expiry is inclusive
    (``age >= max_age``) — fail-closed at the boundary.
    """

    epoch_id: str = Field(default_factory=_new_id)
    granted_at_monotonic: float
    max_age_seconds: float = Field(gt=0)
    max_interactive_actions: int = Field(ge=1)
    interactive_actions_used: int = Field(default=0, ge=0)
    invalidated: bool = False
    invalidation_reason: InvalidationReason | None = None
    invalidation_detail: str = Field(default="", max_length=_INVALIDATION_DETAIL_MAX_CHARS)

    def age_seconds(self, now: float) -> float:
        """Elapsed seconds since the epoch was granted (clamped at zero)."""
        return max(0.0, float(now) - self.granted_at_monotonic)

    def time_remaining(self, now: float) -> float:
        """Wall-clock seconds left before time expiry (0.0 once expired)."""
        return max(0.0, self.max_age_seconds - self.age_seconds(now))

    def actions_remaining(self) -> int:
        """Interactive actions left before count expiry (0 once exhausted)."""
        return max(0, self.max_interactive_actions - self.interactive_actions_used)

    def expiry_axis(self, now: float) -> ExpiryAxis | None:
        """Return the axis that ended the epoch, or ``None`` while it is valid.

        Deterministic precedence: an explicit invalidation is always reported (a material
        change is the strongest signal for the caller, even on an already time-expired
        epoch), then time, then the interactive-action count.
        """
        if self.invalidated:
            return ExpiryAxis.INVALIDATED
        if self.age_seconds(now) >= self.max_age_seconds:
            return ExpiryAxis.TIME
        if self.interactive_actions_used >= self.max_interactive_actions:
            return ExpiryAxis.ACTIONS
        return None

    def is_valid(self, now: float) -> bool:
        return self.expiry_axis(now) is None

    def describe_expiry(self, now: float) -> str:
        """Human-readable fail-closed explanation of why the epoch is dead."""
        axis = self.expiry_axis(now)
        if axis is ExpiryAxis.INVALIDATED:
            reason = self.invalidation_reason.value if self.invalidation_reason else "unknown"
            detail = f": {self.invalidation_detail}" if self.invalidation_detail else ""
            return f"epoch invalidated by material change ({reason}{detail})."
        if axis is ExpiryAxis.TIME:
            return (
                f"epoch expired after {self.max_age_seconds:.0f}s "
                f"(age {self.age_seconds(now):.0f}s)."
            )
        if axis is ExpiryAxis.ACTIONS:
            return (
                f"epoch expired after {self.interactive_actions_used} interactive actions "
                f"(limit {self.max_interactive_actions})."
            )
        return "epoch is valid."


class ApprovalEpochExpired(RuntimeError):
    """Typed fail-closed signal raised by :meth:`ApprovalEpochManager.require_valid`.

    The caller must stop and request fresh approval (a new epoch via ``grant``);
    nothing in this module auto-renews an epoch.
    """

    def __init__(
        self,
        message: str,
        *,
        axis: ExpiryAxis,
        epoch_id: str | None = None,
        invalidation_reason: InvalidationReason | None = None,
    ) -> None:
        super().__init__(message)
        self.axis = axis
        self.epoch_id = epoch_id
        self.invalidation_reason = invalidation_reason


@dataclass(frozen=True)
class ApprovalDecision:
    """Result of the epoch-level gate consulted before an interactive action.

    Exactly one of ``granted`` / ``requires_action_approval`` / ``requires_fresh_approval``
    describes the caller's obligation:

    - ``granted=True``: the live epoch's scope covers the action; proceed (the caller
      still records the interactive action afterwards).
    - ``requires_action_approval=True``: the epoch is alive but the session started with
      ``require_approval=True`` — run the normal per-action approval flow
      (``SafetyPolicy.evaluate`` semantics unchanged) and re-enter with ``approved=True``.
    - ``requires_fresh_approval=True``: the epoch is dead — the caller MUST stop and
      request fresh approval (``grant``). This is the fail-closed refusal: a per-action
      approval can never resurrect a dead epoch.
    """

    granted: bool
    requires_action_approval: bool
    requires_fresh_approval: bool
    reason: str
    epoch_id: str | None = None


class ApprovalEpochManager:
    """Thread-safe holder of the session's live approval epoch (fail-closed gate).

    One epoch is granted at construction (session start). ``full_scope=True`` models a
    session started with ``require_approval=False``: the standing grant covers interactive
    actions while the epoch lives — and STILL expires (time/actions/invalidation), because
    a session-start flag is never an unlimited pass. All state is RLock-protected; the
    clock is injectable for deterministic tests (default ``time.monotonic``).
    """

    def __init__(
        self,
        limits: Limits | None = None,
        *,
        full_scope: bool = False,
        clock: Callable[[], float] | None = None,
        start_monotonic: float | None = None,
    ) -> None:
        self.limits = (limits if limits is not None else Limits()).validate()
        self._clock = clock if clock is not None else time.monotonic
        self._lock = threading.RLock()
        self._full_scope = bool(full_scope)
        granted_at = self._clock() if start_monotonic is None else float(start_monotonic)
        self._epoch = self._new_epoch(granted_at)

    # --- internals -----------------------------------------------------------
    def _new_epoch(self, granted_at: float) -> ApprovalEpoch:
        return ApprovalEpoch(
            granted_at_monotonic=granted_at,
            max_age_seconds=self.limits.approval_epoch_seconds,
            max_interactive_actions=self.limits.approval_epoch_actions,
        )

    def _resolve_now(self, now: float | None) -> float:
        return self._clock() if now is None else float(now)

    # --- grants ----------------------------------------------------------------
    @property
    def full_scope(self) -> bool:
        """True when the epoch models a ``require_approval=false`` session start."""
        return self._full_scope

    def current_epoch(self) -> ApprovalEpoch:
        """Defensive copy of the live epoch for audit/introspection."""
        with self._lock:
            return self._epoch.model_copy(deep=True)

    def grant(self, *, full_scope: bool | None = None, now: float | None = None) -> ApprovalEpoch:
        """Issue a FRESH epoch, replacing the current one (no accumulation of grants).

        This is the only renewal path: fresh approval is a new epoch with reset clocks
        and counters. ``full_scope=None`` keeps the current scope; passing a value
        switches the scope for the new epoch (e.g. a fresh human grant on a session that
        started with ``require_approval=False`` keeps ``True``).
        """
        with self._lock:
            if full_scope is not None:
                self._full_scope = bool(full_scope)
            self._epoch = self._new_epoch(self._resolve_now(now))
            return self._epoch.model_copy(deep=True)

    # --- invalidation ------------------------------------------------------------
    def invalidate(self, reason: InvalidationReason | str, detail: str = "") -> None:
        """Invalidate the live epoch IMMEDIATELY (material change, spec section 11).

        ``reason`` must be a known :class:`InvalidationReason` (or its value) — unknown
        reasons fail closed with ``ValueError`` rather than being ignored. Idempotent and
        deterministic: the FIRST cause of invalidation is retained.
        """
        try:
            parsed = InvalidationReason(reason)
        except ValueError:
            raise ValueError(f"Unknown approval invalidation reason: {reason!r}.") from None
        with self._lock:
            epoch = self._epoch
            if epoch.invalidated:
                return
            epoch.invalidated = True
            epoch.invalidation_reason = parsed
            epoch.invalidation_detail = detail[:_INVALIDATION_DETAIL_MAX_CHARS]

    # --- counters ------------------------------------------------------------------
    def record_interactive_action(self) -> None:
        """Consume one interactive action from the live epoch's count budget.

        The orchestrator calls this after each interactive action executes; the
        ``authorize_action`` gate does NOT consume the budget implicitly.
        """
        with self._lock:
            self._epoch.interactive_actions_used += 1

    # --- validity queries -------------------------------------------------------------
    def is_valid(self, now: float | None = None) -> bool:
        with self._lock:
            return self._epoch.is_valid(self._resolve_now(now))

    def require_valid(self, now: float | None = None) -> ApprovalEpoch:
        """Return a copy of the live epoch or raise :class:`ApprovalEpochExpired`."""
        with self._lock:
            resolved = self._resolve_now(now)
            epoch = self._epoch
            axis = epoch.expiry_axis(resolved)
            if axis is not None:
                raise ApprovalEpochExpired(
                    f"Approval epoch {epoch.epoch_id} is no longer valid: "
                    f"{epoch.describe_expiry(resolved)} The caller must stop and request "
                    "fresh approval before any approval-requiring action.",
                    axis=axis,
                    epoch_id=epoch.epoch_id,
                    invalidation_reason=epoch.invalidation_reason,
                )
            return epoch.model_copy(deep=True)

    def seconds_remaining(self, now: float | None = None) -> float:
        """Wall-clock seconds left in the epoch (0.0 once dead — fail-closed report)."""
        with self._lock:
            resolved = self._resolve_now(now)
            epoch = self._epoch
            if not epoch.is_valid(resolved):
                return 0.0  # a dead epoch has nothing remaining, whatever killed it
            return epoch.time_remaining(resolved)

    def actions_remaining(self) -> int:
        """Interactive actions left in the epoch (0 once dead — fail-closed report)."""
        with self._lock:
            epoch = self._epoch
            if not epoch.is_valid(self._resolve_now(None)):
                return 0  # a dead epoch has nothing remaining, whatever killed it
            return epoch.actions_remaining()

    # --- the fail-closed gate ------------------------------------------------------------
    def authorize_action(
        self, *, approved: bool = False, now: float | None = None
    ) -> ApprovalDecision:
        """Epoch-level gate the orchestrator consults before an interactive action.

        Fail-closed: when the epoch is dead the decision REFUSES with
        ``requires_fresh_approval=True`` regardless of ``approved`` — the caller must
        stop and request fresh approval (``grant``); a per-action approval can never
        resurrect a dead epoch and nothing auto-renews. When the epoch is alive, a
        full-scope session (``require_approval=False`` start) is granted directly;
        otherwise ``requires_action_approval=True`` routes the caller through the normal
        per-action approval flow, and that explicit human approval (``approved=True``)
        completes the gate while the epoch is still alive.
        """
        with self._lock:
            resolved = self._resolve_now(now)
            epoch = self._epoch
            axis = epoch.expiry_axis(resolved)
            if axis is not None:
                return ApprovalDecision(
                    granted=False,
                    requires_action_approval=False,
                    requires_fresh_approval=True,
                    reason=(
                        f"Refused fail-closed: {epoch.describe_expiry(resolved)} "
                        "Stop and request fresh approval (a new epoch); this gate never "
                        "auto-renews."
                    ),
                    epoch_id=epoch.epoch_id,
                )
            if self._full_scope:
                return ApprovalDecision(
                    granted=True,
                    requires_action_approval=False,
                    requires_fresh_approval=False,
                    reason="Granted by the live full-scope approval epoch.",
                    epoch_id=epoch.epoch_id,
                )
            if approved:
                return ApprovalDecision(
                    granted=True,
                    requires_action_approval=False,
                    requires_fresh_approval=False,
                    reason="Explicit per-action human approval within the live epoch.",
                    epoch_id=epoch.epoch_id,
                )
            return ApprovalDecision(
                granted=False,
                requires_action_approval=True,
                requires_fresh_approval=False,
                reason="Live epoch requires the per-action approval flow (require_approval).",
                epoch_id=epoch.epoch_id,
            )

    # --- introspection ---------------------------------------------------------------------
    def snapshot(self) -> dict[str, float | int | str | bool | None]:
        """Bounded snapshot for audit/progress surfaces (never a renewal source).

        Deliberately NO ``restore`` counterpart exists: a resumed session must obtain
        fresh approval via ``grant`` — epoch state is never resurrected from data.
        """
        with self._lock:
            epoch = self._epoch
            resolved = self._resolve_now(None)
            return {
                "epoch_id": epoch.epoch_id,
                "full_scope": self._full_scope,
                "valid": epoch.is_valid(resolved),
                "seconds_remaining": epoch.time_remaining(resolved),
                "interactive_actions_used": epoch.interactive_actions_used,
                "max_interactive_actions": epoch.max_interactive_actions,
                "invalidated": epoch.invalidated,
                "invalidation_reason": (
                    epoch.invalidation_reason.value if epoch.invalidation_reason else None
                ),
            }


# --- prolonged-unattended runtime policy modifier (spec section 12) --------------------------

#: A session unattended for at least one hour activates the raise-only modifier.
PROLONGED_UNATTENDED_SECONDS = 3600.0

#: Raise-only minimum treatment applied while the modifier is active. Order is preserved
#: by construction: every value is >= its key (LOW<MEDIUM<HIGH<CRITICAL), CRITICAL stays
#: CRITICAL (already maximal). NO fifth RiskLevel exists; the enum is untouched.
_UNATTENDED_MIN_TREATMENT: dict[RiskLevel, RiskLevel] = {
    RiskLevel.LOW: RiskLevel.MEDIUM,
    RiskLevel.MEDIUM: RiskLevel.HIGH,
    RiskLevel.HIGH: RiskLevel.HIGH,
    RiskLevel.CRITICAL: RiskLevel.CRITICAL,
}

_IDENTITY_TREATMENT: dict[RiskLevel, RiskLevel] = {level: level for level in RiskLevel}

_RISK_RANK: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


@dataclass(frozen=True)
class UnattendedProtection:
    """Routing data produced by :func:`effective_protection` (never executed directly).

    ``min_risk_treatment`` maps each classified :class:`RiskLevel` to the MINIMUM level it
    must be treated as (raise-only; identity mapping while the modifier is inactive).
    ``requires_fresh_approval`` is True only while the modifier is active, in which case
    non-routine actions (original risk MEDIUM and above) require a fresh approval epoch
    via :meth:`ApprovalEpochManager.grant` — see :meth:`requires_fresh_approval_for`.
    """

    modifier_active: bool
    unattended_seconds: float
    min_risk_treatment: Mapping[RiskLevel, RiskLevel]
    requires_fresh_approval: bool

    def treated_risk(self, risk: RiskLevel | str) -> RiskLevel:
        """Return the minimum level ``risk`` must be treated as (never lower than input)."""
        try:
            level = RiskLevel(risk)
        except ValueError:
            raise ValueError(f"Unknown risk level: {risk!r}.") from None
        return self.min_risk_treatment[level]

    def requires_fresh_approval_for(self, risk: RiskLevel | str) -> bool:
        """True when ``risk`` is non-routine (>= MEDIUM) while the modifier is active."""
        try:
            level = RiskLevel(risk)
        except ValueError:
            raise ValueError(f"Unknown risk level: {risk!r}.") from None
        return self.modifier_active and _RISK_RANK[level] >= _RISK_RANK[RiskLevel.MEDIUM]


def effective_protection(
    *,
    session_elapsed_seconds: float,
    last_human_interaction_monotonic: float | None = None,
    now_monotonic: float | None = None,
) -> UnattendedProtection:
    """Pure raise-only modifier: (session elapsed, last human interaction) -> protection.

    The unattended duration is the time since the last human interaction, or the whole
    session duration when no interaction was ever recorded. The modifier activates once
    that duration reaches one hour (``>= PROLONGED_UNATTENDED_SECONDS``; inclusive
    boundary, fail-closed). While active, protection may only go UP: MEDIUM is treated as
    HIGH, LOW as MEDIUM, CRITICAL stays CRITICAL, and non-routine actions require fresh
    approval. While inactive the treatment mapping is the identity and nothing is
    required — this function can never LOWER protection for any input.
    """
    if last_human_interaction_monotonic is None:
        unattended = max(0.0, float(session_elapsed_seconds))
    else:
        reference = time.monotonic() if now_monotonic is None else float(now_monotonic)
        unattended = max(0.0, reference - float(last_human_interaction_monotonic))
    active = unattended >= PROLONGED_UNATTENDED_SECONDS
    mapping = _UNATTENDED_MIN_TREATMENT if active else _IDENTITY_TREATMENT
    return UnattendedProtection(
        modifier_active=active,
        unattended_seconds=unattended,
        min_risk_treatment=MappingProxyType(dict(mapping)),
        requires_fresh_approval=active,
    )
