"""Long-running session budget tests (spec sections 5 + 17 "Resource Limits").

Covers the ADDITIVE session-level limits (defaults 4h/2000 actions/500 model calls/500
steps/50 subtasks + ``context_summarize_every``) and the restorable shared-budget
tracker. POST LOOP-REMOVAL the session-budget family is ACCEPTED AND VALIDATED but
RESERVED — not enforced on the direct five-tool path since the internal loop's removal;
the former trip pins died with the loop (the surviving shared-budget gate is exercised
at its own seam in ``test_checkpoint_integrity.py``). Clamp semantics are pinned here
unchanged; counters-sharing, snapshot->restore round-trips, and ``_parse_limits``
acceptance keep their pins. Existing per-run limits are untouched and pinned by
``test_foundation_limits_audit.py``.
"""

from __future__ import annotations

import time

import pytest

from computer_use_mcp.limits import (
    Limits,
    SessionBudgetExceeded,
    SessionBudgetTracker,
)


def backdated_tracker(limits: Limits, elapsed: float) -> SessionBudgetTracker:
    """Tracker whose session "started" ``elapsed`` seconds ago (no real waiting)."""
    return SessionBudgetTracker(limits, start_monotonic=time.monotonic() - elapsed)


# --- additive Limits fields: defaults + clamps -----------------------------------------------------


def test_session_limits_defaults_match_spec() -> None:
    limits = Limits()
    assert limits.max_session_seconds == 14400.0  # 4h default mission duration
    assert limits.max_session_actions == 2000
    assert limits.max_session_model_calls == 500
    assert limits.max_session_steps == 500
    assert limits.max_subtasks == 50
    assert limits.context_summarize_every == 25
    # Existing limits remain exactly as they were (additive change only).
    assert limits.max_task_seconds == 900.0
    assert limits.max_actions == 100
    assert limits.max_model_calls == 60


def test_session_limits_clamp_high_values() -> None:
    clamped = Limits(
        max_session_seconds=100_000.0,  # > 24h
        max_session_actions=9999,
        max_session_model_calls=9999,
        max_session_steps=9999,
        max_subtasks=999,
        context_summarize_every=10_000,
    ).validate()
    assert clamped.max_session_seconds == 86400.0  # 24h maximum configurable
    assert clamped.max_session_actions == 2000
    assert clamped.max_session_model_calls == 500
    assert clamped.max_session_steps == 500
    assert clamped.max_subtasks == 50
    assert clamped.context_summarize_every == 500


def test_session_limits_clamp_low_values() -> None:
    clamped = Limits(
        max_session_seconds=0.0,
        max_session_actions=0,
        max_session_model_calls=0,
        max_session_steps=0,
        max_subtasks=0,
        context_summarize_every=0,
    ).validate()
    assert clamped.max_session_seconds == 1.0
    assert clamped.max_session_actions == 1
    assert clamped.max_session_model_calls == 1
    assert clamped.max_session_steps == 1
    assert clamped.max_subtasks == 1
    assert clamped.context_summarize_every == 1


def test_session_limits_validate_does_not_mutate_original() -> None:
    original = Limits(max_session_actions=10_000, max_session_seconds=100_000.0)
    original.validate()
    assert original.max_session_actions == 10_000
    assert original.max_session_seconds == 100_000.0


# --- counters shared across subtasks -----------------------------------------------------------------


def test_session_budget_shared_across_subtasks() -> None:
    tracker = SessionBudgetTracker(Limits(max_session_actions=10, max_session_steps=100))
    # Three simulated subtasks, each consuming the SAME shared budget.
    for subtask_index in range(3):
        tracker.check_actions()
        tracker.check_steps()
        tracker.check_subtasks()
        for _ in range(3):
            tracker.record_action()
            tracker.record_step()
        tracker.record_model_call()
        tracker.record_subtask()
        assert tracker.snapshot()["actions"] == 3 * (subtask_index + 1)
    snapshot = tracker.snapshot()
    assert snapshot["actions"] == 9
    assert snapshot["steps"] == 9
    assert snapshot["model_calls"] == 3
    assert snapshot["subtasks"] == 3


def test_shared_budget_exhaustion_blocks_next_subtask() -> None:
    tracker = SessionBudgetTracker(Limits(max_session_actions=4))
    for _ in range(2):  # two subtasks burn 2 actions each
        tracker.check_actions()
        tracker.record_action()
        tracker.record_action()
    with pytest.raises(SessionBudgetExceeded):
        tracker.check_actions()  # the third subtask is refused: shared budget exhausted


# --- snapshot -> restore: counters persist, never reset ------------------------------------------------


def test_counters_persist_after_snapshot_restore() -> None:
    source = backdated_tracker(Limits(max_session_seconds=3600.0), elapsed=100.0)
    source.record_action()
    source.record_action()
    source.record_model_call()
    for _ in range(5):
        source.record_step()
    source.record_subtask()
    snapshot = source.snapshot()
    assert snapshot["elapsed_seconds"] >= 100.0

    restored = SessionBudgetTracker(Limits(max_session_seconds=3600.0))
    restored.restore(snapshot)
    assert restored.snapshot()["actions"] == 2
    assert restored.snapshot()["model_calls"] == 1
    assert restored.snapshot()["steps"] == 5
    assert restored.snapshot()["subtasks"] == 1
    # Elapsed-time anchor preserved: the resumed session continues the original duration.
    assert restored.elapsed_seconds() >= 100.0
    # Counters keep growing from the checkpoint values; nothing was zeroed.
    restored.record_action()
    assert restored.snapshot()["actions"] == 3


def test_restore_does_not_refill_duration_budget() -> None:
    snapshot = backdated_tracker(Limits(), elapsed=90_000.0).snapshot()  # 25h "elapsed"
    restored = SessionBudgetTracker(Limits(max_session_seconds=3600.0))
    restored.restore(snapshot)
    with pytest.raises(SessionBudgetExceeded) as excinfo:
        restored.check_duration()
    assert excinfo.value.limit_name == "max_session_seconds"


def test_restore_never_lowers_existing_counters() -> None:
    tracker = SessionBudgetTracker(Limits())
    tracker.record_action()
    tracker.record_action()
    tracker.record_action()
    stale = {"snapshot_version": 1, "elapsed_seconds": 0.0, "actions": 1,
             "model_calls": 0, "steps": 0, "subtasks": 0}
    tracker.restore(stale)
    assert tracker.snapshot()["actions"] == 3
    assert tracker.snapshot()["elapsed_seconds"] >= 0.0


def test_restore_rejects_malformed_snapshots_fail_closed() -> None:
    tracker = SessionBudgetTracker(Limits())
    good = tracker.snapshot()
    for malformed in (
        {**good, "snapshot_version": 2},
        {key: value for key, value in good.items() if key != "steps"},
        {**good, "actions": -1},
        {**good, "actions": "many"},
        "not-a-mapping",
    ):
        with pytest.raises((TypeError, ValueError)):
            tracker.restore(malformed)  # type: ignore[arg-type]
        assert tracker.snapshot()["actions"] == 0  # failed restore left state untouched


# --- the new limits flow through the existing settings mechanism -------------------------------------


def test_new_limits_accepted_by_existing_parse_limits() -> None:
    from computer_use_mcp.server import _parse_limits

    parsed = _parse_limits(
        {"max_session_seconds": 100_000.0, "max_session_actions": 9999.0,
         "max_session_model_calls": 9999.0, "max_session_steps": 9999.0,
         "max_subtasks": 999.0, "context_summarize_every": 0.0}
    )
    assert parsed.max_session_seconds == 86400.0
    assert parsed.max_session_actions == 2000
    assert parsed.max_session_model_calls == 500
    assert parsed.max_session_steps == 500
    assert parsed.max_subtasks == 50
    assert parsed.context_summarize_every == 1
    with pytest.raises(ValueError):
        _parse_limits({"max_session_actions": 1, "bogus_limit": 1})
