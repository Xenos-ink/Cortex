"""Approval epoch tests (spec sections 11 + 17 "Approval") and the prolonged-unattended
raise-only modifier (spec section 12).

Covers: temporal expiry and action-count expiry on injected clocks (no sleeps),
whichever-comes-first semantics, immediate invalidation on every material-change reason,
``require_approval=false`` NOT bypassing long-running expiry (full-scope epoch still
expires), fail-closed refusals after expiry, fresh approval creating a NEW epoch with no
accumulation, and the property that :func:`effective_protection` can only RAISE
protection (never lower any risk level, no fifth RiskLevel).
"""

from __future__ import annotations

import pytest

from computer_use_mcp.approval import (
    PROLONGED_UNATTENDED_SECONDS,
    ApprovalEpochExpired,
    ApprovalEpochManager,
    ExpiryAxis,
    InvalidationReason,
    effective_protection,
)
from computer_use_mcp.limits import Limits
from computer_use_mcp.models import RiskLevel

ALL_INVALIDATION_REASONS = [
    InvalidationReason.APPLICATION_CHANGED,
    InvalidationReason.PROCESS_CHANGED,
    InvalidationReason.RISK_ESCALATED,
    InvalidationReason.GOAL_CHANGED,
    InvalidationReason.POLICY_CHANGED,
    InvalidationReason.ENVIRONMENT_CHANGED,
]


class FakeClock:
    """Deterministic monotonic-style clock; tests advance it explicitly (no sleeps)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_manager(
    *,
    full_scope: bool = False,
    epoch_seconds: float = 1800.0,
    epoch_actions: int = 50,
    clock: FakeClock | None = None,
) -> tuple[ApprovalEpochManager, FakeClock]:
    clock = clock if clock is not None else FakeClock()
    limits = Limits(approval_epoch_seconds=epoch_seconds, approval_epoch_actions=epoch_actions)
    manager = ApprovalEpochManager(limits, full_scope=full_scope, clock=clock)
    return manager, clock


# --- construction / limits wiring -------------------------------------------------------------------


def test_epoch_defaults_match_spec() -> None:
    manager = ApprovalEpochManager()
    epoch = manager.current_epoch()
    assert epoch.max_age_seconds == 1800.0  # 30 minutes (spec section 11)
    assert epoch.max_interactive_actions == 50
    assert epoch.interactive_actions_used == 0
    assert not epoch.invalidated
    assert manager.is_valid(0.0)  # epoch starts fresh at its grant anchor
    assert manager.full_scope is False  # require_approval=True default session


def test_epoch_uses_clamped_limits() -> None:
    limits = Limits(approval_epoch_seconds=0.5, approval_epoch_actions=0).validate()
    manager = ApprovalEpochManager(limits, clock=FakeClock())
    epoch = manager.current_epoch()
    assert epoch.max_age_seconds == 60.0  # clamped low bound
    assert epoch.max_interactive_actions == 1


def test_grant_starts_at_session_start_anchor() -> None:
    manager = ApprovalEpochManager(clock=FakeClock(start=5000.0))
    assert manager.current_epoch().granted_at_monotonic == 5000.0


# --- temporal expiry (injected clock) ---------------------------------------------------------------


def test_time_expiry_with_injected_clock() -> None:
    manager, clock = make_manager(epoch_seconds=1800.0)
    assert manager.is_valid()
    clock.advance(1799.0)
    assert manager.is_valid()
    assert manager.seconds_remaining() == pytest.approx(1.0)
    clock.advance(1.0)  # exactly 1800s -> inclusive boundary, fail closed
    assert not manager.is_valid()
    assert manager.seconds_remaining() == 0.0


def test_require_valid_raises_typed_error_on_time_expiry() -> None:
    manager, clock = make_manager(epoch_seconds=100.0)
    clock.advance(100.0)
    with pytest.raises(ApprovalEpochExpired) as excinfo:
        manager.require_valid()
    assert excinfo.value.axis is ExpiryAxis.TIME
    assert excinfo.value.epoch_id == manager.current_epoch().epoch_id


# --- action-count expiry -----------------------------------------------------------------------------


def test_action_count_expiry() -> None:
    manager, _ = make_manager(epoch_actions=5)
    for _ in range(4):
        manager.record_interactive_action()
        assert manager.is_valid()
    manager.record_interactive_action()  # 5th action: inclusive boundary, fail closed
    assert not manager.is_valid()
    assert manager.actions_remaining() == 0


def test_record_interactive_action_consumes_budget() -> None:
    manager, _ = make_manager(epoch_actions=3)
    assert manager.actions_remaining() == 3
    manager.record_interactive_action()
    assert manager.actions_remaining() == 2
    assert manager.current_epoch().interactive_actions_used == 1


# --- whichever comes first ---------------------------------------------------------------------------


def test_time_ends_epoch_first_when_actions_remain() -> None:
    manager, clock = make_manager(epoch_seconds=60.0, epoch_actions=1000)
    clock.advance(60.0)
    assert not manager.is_valid()
    # A dead epoch reports nothing remaining on EITHER axis (fail-closed reporting),
    # even though its raw action budget was never consumed.
    assert manager.actions_remaining() == 0
    assert manager.current_epoch().actions_remaining() == 1000


def test_actions_end_epoch_first_when_time_remains() -> None:
    manager, _ = make_manager(epoch_seconds=1800.0, epoch_actions=2)
    manager.record_interactive_action()
    manager.record_interactive_action()
    assert not manager.is_valid()
    epoch = manager.current_epoch()
    assert epoch.interactive_actions_used == 2


def test_whichever_first_with_injected_clock_and_counts() -> None:
    # Tight on both axes: whichever the caller hits first ends the epoch.
    manager, clock = make_manager(epoch_seconds=120.0, epoch_actions=3)
    manager.record_interactive_action()
    manager.record_interactive_action()
    assert manager.is_valid()
    clock.advance(30.0)
    manager.record_interactive_action()
    assert not manager.is_valid()  # actions ran out while 90s of time remained

    manager2, clock2 = make_manager(epoch_seconds=120.0, epoch_actions=100)
    clock2.advance(120.0)
    assert not manager2.is_valid()  # time ran out with actions remaining


# --- immediate invalidation on material change -------------------------------------------------------


@pytest.mark.parametrize("reason", ALL_INVALIDATION_REASONS)
def test_invalidation_on_each_material_change_reason(reason: InvalidationReason) -> None:
    manager, _clock = make_manager()
    assert manager.is_valid()
    manager.invalidate(reason, detail="unit test")
    # Immediate: no time has passed and no actions were consumed.
    assert not manager.is_valid()
    assert manager.seconds_remaining() == 0.0
    with pytest.raises(ApprovalEpochExpired) as excinfo:
        manager.require_valid()
    assert excinfo.value.axis is ExpiryAxis.INVALIDATED
    assert excinfo.value.invalidation_reason is reason
    assert manager.current_epoch().invalidation_detail == "unit test"


def test_invalidation_accepts_reason_value_strings() -> None:
    manager, _ = make_manager()
    manager.invalidate("goal_changed")
    assert not manager.is_valid()
    assert manager.current_epoch().invalidation_reason is InvalidationReason.GOAL_CHANGED


def test_invalidation_unknown_reason_fails_closed() -> None:
    manager, _ = make_manager()
    with pytest.raises(ValueError):
        manager.invalidate("not_a_real_reason")
    assert manager.is_valid()  # nothing was invalidated by a rejected call


def test_invalidation_is_idempotent_first_reason_wins() -> None:
    manager, _ = make_manager()
    manager.invalidate(InvalidationReason.PROCESS_CHANGED)
    manager.invalidate(InvalidationReason.GOAL_CHANGED)
    epoch = manager.current_epoch()
    assert epoch.invalidation_reason is InvalidationReason.PROCESS_CHANGED


def test_invalidation_detail_is_bounded() -> None:
    manager, _ = make_manager()
    manager.invalidate(InvalidationReason.ENVIRONMENT_CHANGED, detail="x" * 10_000)
    assert len(manager.current_epoch().invalidation_detail) == 500


def test_invalidated_is_reported_even_on_already_expired_epoch() -> None:
    # Deterministic precedence: a material change is always reported to the caller.
    manager, clock = make_manager(epoch_seconds=10.0)
    clock.advance(20.0)  # already time-expired
    manager.invalidate(InvalidationReason.RISK_ESCALATED)
    with pytest.raises(ApprovalEpochExpired) as excinfo:
        manager.require_valid()
    assert excinfo.value.axis is ExpiryAxis.INVALIDATED
    assert excinfo.value.invalidation_reason is InvalidationReason.RISK_ESCALATED


# --- fail-closed after expiry --------------------------------------------------------------------------


def test_authorize_action_refuses_fail_closed_after_time_expiry() -> None:
    manager, clock = make_manager(full_scope=True)
    clock.advance(1800.0)
    decision = manager.authorize_action()
    assert decision.granted is False
    assert decision.requires_fresh_approval is True
    assert decision.requires_action_approval is False
    assert "fail-closed" in decision.reason.lower()


def test_authorize_action_refuses_after_action_budget_expiry() -> None:
    manager, _ = make_manager(full_scope=True, epoch_actions=1)
    manager.record_interactive_action()
    decision = manager.authorize_action()
    assert decision.requires_fresh_approval is True
    assert decision.granted is False


def test_per_action_approval_cannot_resurrect_dead_epoch() -> None:
    # Fail-closed doctrine (section 11): after expiry the caller must STOP and request
    # fresh approval; even an explicit per-action approval is not a renewal.
    manager, clock = make_manager(epoch_seconds=60.0)
    clock.advance(60.0)
    decision = manager.authorize_action(approved=True)
    assert decision.granted is False
    assert decision.requires_fresh_approval is True


def test_require_approval_false_does_not_bypass_long_running_expiry() -> None:
    # Full-scope epoch models a require_approval=false start: granted while alive,
    # but the long-running policy applies on top and it still expires.
    manager, clock = make_manager(full_scope=True)
    decision = manager.authorize_action()
    assert decision.granted is True
    clock.advance(1801.0)
    decision = manager.authorize_action()
    assert decision.granted is False
    assert decision.requires_fresh_approval is True


def test_full_scope_epoch_also_dies_on_action_budget_and_invalidation() -> None:
    manager, _ = make_manager(full_scope=True, epoch_actions=2)
    manager.record_interactive_action()
    manager.record_interactive_action()
    assert manager.authorize_action().requires_fresh_approval is True

    manager2, _ = make_manager(full_scope=True)
    manager2.invalidate(InvalidationReason.APPLICATION_CHANGED)
    assert manager2.authorize_action().requires_fresh_approval is True


def test_no_auto_renewal_after_expiry() -> None:
    manager, clock = make_manager(epoch_seconds=60.0, epoch_actions=1)
    manager.record_interactive_action()
    clock.advance(10_000.0)
    assert not manager.is_valid()
    assert manager.seconds_remaining() == 0.0
    assert manager.actions_remaining() == 0
    assert manager.authorize_action().requires_fresh_approval is True


def test_require_approval_flow_within_live_epoch() -> None:
    # Non-full-scope sessions: the epoch routes the caller through the normal per-action
    # approval flow; the explicit human approval completes the gate while alive.
    manager, _ = make_manager(full_scope=False)
    decision = manager.authorize_action()
    assert decision.granted is False
    assert decision.requires_action_approval is True
    assert decision.requires_fresh_approval is False
    decision = manager.authorize_action(approved=True)
    assert decision.granted is True
    assert decision.requires_fresh_approval is False


# --- fresh approval is a new epoch ---------------------------------------------------------------------


def test_fresh_grant_creates_new_epoch_without_accumulation() -> None:
    manager, clock = make_manager(full_scope=True, epoch_actions=50)
    manager.record_interactive_action()
    manager.invalidate(InvalidationReason.GOAL_CHANGED)
    old_epoch = manager.current_epoch()
    clock.advance(5.0)

    new_epoch = manager.grant()

    assert new_epoch.epoch_id != old_epoch.epoch_id  # a NEW epoch, not a patch of the old
    assert new_epoch.granted_at_monotonic == clock.now
    assert new_epoch.interactive_actions_used == 0  # counters never accumulate
    assert not new_epoch.invalidated
    assert new_epoch.invalidation_reason is None
    assert manager.is_valid()
    assert manager.authorize_action().granted is True
    # The dead epoch is gone entirely (no accumulation of old grants).
    assert manager.current_epoch().epoch_id == new_epoch.epoch_id


def test_grant_keeps_scope_by_default_and_can_switch_it() -> None:
    manager, _ = make_manager(full_scope=False)
    manager.grant()
    assert manager.full_scope is False
    manager.grant(full_scope=True)
    assert manager.full_scope is True
    assert manager.authorize_action().granted is True


def test_snapshot_reports_fail_closed_state() -> None:
    manager, clock = make_manager(full_scope=True, epoch_seconds=60.0)
    snapshot = manager.snapshot()
    assert snapshot["valid"] is True
    assert snapshot["full_scope"] is True
    clock.advance(61.0)
    snapshot = manager.snapshot()
    assert snapshot["valid"] is False
    assert snapshot["seconds_remaining"] == 0.0
    manager.invalidate(InvalidationReason.POLICY_CHANGED)
    assert manager.snapshot()["invalidation_reason"] == "policy_changed"


# --- prolonged-unattended modifier (spec section 12) ---------------------------------------------------


def test_modifier_identity_before_one_hour() -> None:
    protection = effective_protection(
        session_elapsed_seconds=3599.0, last_human_interaction_monotonic=None, now_monotonic=0.0
    )
    assert protection.modifier_active is False
    assert protection.requires_fresh_approval is False
    assert protection.unattended_seconds == 3599.0
    for level in RiskLevel:
        assert protection.treated_risk(level) is level  # identity: nothing changed


def test_modifier_active_at_one_hour_inclusive_boundary() -> None:
    at_hour = effective_protection(session_elapsed_seconds=PROLONGED_UNATTENDED_SECONDS)
    assert at_hour.modifier_active is True  # "after an hour" is inclusive (fail closed)
    assert at_hour.requires_fresh_approval is True
    just_before = effective_protection(
        session_elapsed_seconds=PROLONGED_UNATTENDED_SECONDS - 0.5
    )
    assert just_before.modifier_active is False


def test_modifier_uses_last_human_interaction_time() -> None:
    # 2h session but a human interacted 5 minutes ago -> NOT active.
    protection = effective_protection(
        session_elapsed_seconds=7200.0,
        last_human_interaction_monotonic=0.0,
        now_monotonic=300.0,
    )
    assert protection.modifier_active is False
    # Human last seen 61 minutes ago -> active even early in the session.
    protection = effective_protection(
        session_elapsed_seconds=1200.0,
        last_human_interaction_monotonic=0.0,
        now_monotonic=3660.0,
    )
    assert protection.modifier_active is True
    assert protection.unattended_seconds == 3660.0


def test_modifier_raise_only_mapping_when_active() -> None:
    protection = effective_protection(session_elapsed_seconds=7200.0)
    assert protection.treated_risk(RiskLevel.LOW) is RiskLevel.MEDIUM
    assert protection.treated_risk(RiskLevel.MEDIUM) is RiskLevel.HIGH
    assert protection.treated_risk(RiskLevel.HIGH) is RiskLevel.HIGH
    assert protection.treated_risk(RiskLevel.CRITICAL) is RiskLevel.CRITICAL  # already maximal


def test_modifier_never_lowers_protection_for_any_input() -> None:
    # Property sweep: for every elapsed time and every risk level the treated level is
    # never lower than the classified level, and the mapping only ever covers the four
    # real levels (NO fifth RiskLevel; safety.py untouched).
    rank = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.CRITICAL: 3}
    for elapsed in (0.0, 1.0, 3599.0, 3600.0, 7200.0, 100_000.0):
        for interacted_ago in (None, 0.0, 1800.0, 4000.0):
            if interacted_ago is None:
                protection = effective_protection(session_elapsed_seconds=elapsed)
            else:
                protection = effective_protection(
                    session_elapsed_seconds=elapsed,
                    last_human_interaction_monotonic=0.0,
                    now_monotonic=interacted_ago,
                )
            assert set(protection.min_risk_treatment) == set(RiskLevel)
            for level in RiskLevel:
                treated = protection.treated_risk(level)
                assert treated in RiskLevel
                assert rank[treated] >= rank[level]  # raise-only, never lower


def test_modifier_requires_fresh_approval_for_non_routine_actions_only() -> None:
    protection = effective_protection(session_elapsed_seconds=7200.0)
    assert protection.requires_fresh_approval_for(RiskLevel.LOW) is False
    assert protection.requires_fresh_approval_for(RiskLevel.MEDIUM) is True
    assert protection.requires_fresh_approval_for(RiskLevel.HIGH) is True
    assert protection.requires_fresh_approval_for(RiskLevel.CRITICAL) is True
    inactive = effective_protection(session_elapsed_seconds=10.0)
    for level in RiskLevel:
        assert inactive.requires_fresh_approval_for(level) is False


def test_treated_risk_rejects_unknown_level_fail_closed() -> None:
    protection = effective_protection(session_elapsed_seconds=0.0)
    with pytest.raises(ValueError):
        protection.treated_risk("catastrophic")
