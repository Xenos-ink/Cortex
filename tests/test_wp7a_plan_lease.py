"""WP7a plan LEASE + STEP-SLICE + TERMINAL-AGGREGATE tests.

The stepwise handshake state (dev ``_PlanLease`` + install/release + the pure
step-state helpers) now lives server-import-free in ``computer_use_mcp.plan``:

* **Install atomically, release idempotently** — a second lease on a session (or a
  session id still marked in-flight) is refused fail-closed with nothing changed,
  and ``release`` is safe from every terminal/session-exit path, however often it
  runs. The helpers operate on a bundle-LIKE object (``.extra`` dict +
  ``.context.session_id``) — the module never imports the server.
* **The cursor rule (K-53)** — advance iff >= 1 item of the step dispatched; a
  pre-dispatch refusal keeps the cursor so a retry re-executes the step.
* **The terminal aggregate (master-mission §1.2/§1.3, V/4244 fix)** —
  ``result`` is closed {verified, unknown, failed, stopped} and
  ``ok = (result == "verified")`` BY CONSTRUCTION: unknown and stopped are
  never success.
* **Step slices/context and the retained-prefix state** — one indivisible
  handshake step per waypoint with the final wait riding the last step, prefix
  merges that preserve both index sets, truthful remainder/remaining/not_executed.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest

from computer_use_mcp.plan import (
    DISPATCHED_ITEM_KINDS,
    PLAN_EVIDENCE_KEY,
    TERMINAL_PLAN_RESULTS,
    PlanLease,
    PlanLeaseGuard,
    merge_plan_step_prefix,
    normalize_plan,
    plan_cursor_after,
    plan_next_step,
    plan_not_executed,
    plan_remaining,
    plan_result_from_entries,
    plan_step_attempted,
    plan_step_context,
    plan_step_slices,
    plan_terminal_outcome,
    redact_queue_entry,
    set_plan_step_remainder,
)
from computer_use_mcp.plan import WaypointPlan as _PlanModel


def _lease(points: int = 2, **plan_kwargs: Any) -> PlanLease:
    plan = _PlanModel(
        points=[
            {"id": f"p{i}", "x": i, "y": i, "actions": []} for i in range(points)
        ],
        **plan_kwargs,
    )
    specs, context = normalize_plan(plan)
    return PlanLease(
        model=plan,
        specs=specs,
        context=context,
        steps=plan_step_slices(specs, context),
    )


def _bundle() -> Any:
    return SimpleNamespace(extra={}, context=SimpleNamespace(session_id="sess-1"))


def _entry(
    result: str = "verified", kind: str = "executed", **extra: Any
) -> dict[str, Any]:
    """A queue entry shaped like the REAL ``agent._queue_entry`` output.

    Every real entry carries the unified §1.2 ``result`` token (from the same
    derivation as the flat reply) and ``ok``; ``verification_outcome`` exists ONLY
    when a verification block exists. ``verification_outcome`` can be forced via
    ``extra``; omitting ``result`` entirely (``result=None``) builds the
    dispatched-unverified exception shape, which has no ``result`` key at all.
    """
    entry: dict[str, Any] = {
        "index": 0,
        "action_type": "click",
        "kind": kind,
    }
    if result is not None:
        entry["result"] = result
        entry["ok"] = result in ("verified", "marker")
    else:
        entry["ok"] = False
    entry.update(extra)
    return entry


#: The REAL entry shapes the classifier must survive (VAL-WP7a-01 #3), each with the
#: aggregate verdict it must produce. ``result=None`` = the key is ABSENT (the real
#: dispatched_unverified entry, agent.py's TaskStopped branch, has no ``result``).
REAL_SHAPE_MATRIX: tuple[tuple[dict[str, Any], str], ...] = (
    (_entry("verified"), "verified"),
    (_entry("marker", kind="executed"), "verified"),
    (_entry("uncertain", verification_outcome="uncertain"), "unknown"),
    (_entry("failed", verification_outcome="failed"), "failed"),
    (_entry("rejected", kind="rejected", reasons=["focus_taken_by"]), "failed"),
    (_entry("not_executed", kind="approval_required", requires_approval=True), "failed"),
    (_entry("dry_run", kind="executed"), "failed"),
    # the real dispatched-unverified entry: NO result key, uncertain outcome
    (_entry(None, kind="dispatched_unverified", verification_outcome="uncertain"), "unknown"),
    # the fail-closed core: an executed entry with NO verification outcome at all
    (_entry(None, kind="executed"), "unknown"),
    # absent result + a failed verification outcome is the only absent-result failure
    (_entry(None, kind="executed", verification_outcome="failed"), "failed"),
)


# --- step slices -------------------------------------------------------------------------------


def test_step_slices_are_one_indivisible_step_per_point() -> None:
    plan = _PlanModel(
        points=[
            {"id": "a", "x": 1, "y": 2, "actions": [{"action": "click"}]},
            {"id": "b", "x": 3, "y": 4, "actions": [{"action": "click"}, {"action": "type", "text": "hi"}]},
        ],
        transition_duration_ms=0,
    )
    specs, context = normalize_plan(plan)
    steps = plan_step_slices(specs, context)
    assert [len(step) for step in steps] == [2, 3]
    assert steps[0][0].action.value == "move" and steps[0][1].action.value == "click"
    assert steps[1][0].x == 3 and steps[1][1].y == 4  # point coordinates injected


def test_the_final_wait_rides_on_the_final_step() -> None:
    plan = _PlanModel(
        points=[
            {"id": "a", "x": 1, "y": 2, "actions": []},
            {"id": "b", "x": 3, "y": 4, "actions": []},
        ],
        final_wait_ms=1_000,
    )
    specs, context = normalize_plan(plan)
    steps = plan_step_slices(specs, context)
    assert len(steps) == 2
    assert steps[-1][-1].action.value == "wait"  # no synthetic host step required
    assert steps[0][-1].action.value == "move"


def test_the_step_context_rebases_indices_and_appends_the_final_wait_policy() -> None:
    plan = _PlanModel(
        points=[
            {"id": "a", "x": 1, "y": 2, "actions": [{"action": "click"}]},
            {"id": "b", "x": 3, "y": 4, "actions": []},
        ],
        transition_duration_ms=50,
        final_wait_ms=1_000,
    )
    specs, context = normalize_plan(plan)
    lease = PlanLease(model=plan, specs=specs, context=context, steps=plan_step_slices(specs, context))
    step_context, policies = plan_step_context(lease, 1, lease.steps[1])
    point = step_context["points"][0]
    assert point["id"] == "b"
    assert point["first_item"] == 0 and point["last_item"] == len(lease.steps[1]) - 1
    assert step_context["final_wait_item"] == len(lease.steps[1]) - 1
    assert policies[-1] is None  # the final wait carries no settle policy
    assert len(policies) == len(lease.steps[1])
    assert step_context["plan_id"] == context["plan_id"]
    first_context, first_policies = plan_step_context(lease, 0, lease.steps[0])
    assert first_context["final_wait_item"] is None
    assert first_policies[-1] is not None  # the point's own settle policy rides


# --- lease install / release -------------------------------------------------------------------


def test_install_is_visible_on_the_bundle_and_in_flight() -> None:
    guard = PlanLeaseGuard()
    bundle = _bundle()
    lease = _lease()
    guard.install(bundle, lease)
    assert guard.active_lease(bundle) is lease
    assert guard.is_active("sess-1")
    assert bundle.extra["plan_lease"] is lease


def test_a_second_install_is_refused_fail_closed_with_nothing_changed() -> None:
    guard = PlanLeaseGuard()
    bundle = _bundle()
    first = _lease()
    guard.install(bundle, first)
    with pytest.raises(ValueError, match="already active"):
        guard.install(bundle, _lease())
    assert guard.active_lease(bundle) is first  # the incumbent is untouched
    # a DIFFERENT bundle with the SAME session id is refused too (the in-flight mark)
    other = SimpleNamespace(extra={}, context=SimpleNamespace(session_id="sess-1"))
    with pytest.raises(ValueError, match="already active"):
        guard.install(other, _lease())
    assert other.extra == {}


def test_release_is_idempotent_and_clears_both_halves() -> None:
    guard = PlanLeaseGuard()
    bundle = _bundle()
    guard.install(bundle, _lease())
    guard.release("sess-1", bundle)
    assert bundle.extra.get("plan_lease") is None
    assert not guard.is_active("sess-1")
    guard.release("sess-1", bundle)  # every terminal/session-exit path may repeat it
    guard.release("unknown-session", bundle)
    assert guard.active_lease(bundle) is None


def test_concurrent_installs_on_one_session_produce_exactly_one_lease() -> None:
    """The V8/D18 defense-in-depth property as a REAL race (VAL-WP7a-01 #11): every
    racer is delayed INSIDE the critical section (via the guard's ``_race_probe``
    seam, between the occupancy check and the set), so the check->set window is wide
    enough that an unlocked implementation would admit all eight racers — the lock
    admits exactly one."""
    import time

    guard = PlanLeaseGuard()
    bundle = _bundle()
    racers = 8
    results: list[object] = []

    def _race() -> Any:
        return guard.install(bundle, _lease(), _race_probe=lambda: time.sleep(0.05))

    with ThreadPoolExecutor(max_workers=racers) as pool:
        futures = [pool.submit(_race) for _ in range(racers)]
        for future in futures:
            try:
                future.result()
                results.append("installed")
            except ValueError:
                results.append("refused")
    assert results.count("installed") == 1, results
    assert results.count("refused") == racers - 1
    assert isinstance(guard.active_lease(bundle), PlanLease)


def test_release_during_a_race_leaves_the_guard_consistent() -> None:
    """Release under held-critical-section installs (VAL-WP7a-01 #11): each install
    holds the widened check->set window while the release runs concurrently, so at
    most ONE install can land before the release and at most ONE after it — an
    unlocked implementation would stack several leases. Whatever the interleaving,
    BOTH views (bundle key + in-flight set) must agree at the end; a torn state
    fails this test."""
    import threading
    import time

    guard = PlanLeaseGuard()
    bundle = _bundle()
    racers = 6
    installed: list[PlanLease] = []
    refused = 0
    begun = threading.Event()

    def _race() -> None:
        nonlocal refused
        begun.set()
        try:
            lease = _lease()
            guard.install(bundle, lease, _race_probe=lambda: time.sleep(0.05))
            installed.append(lease)
        except ValueError:
            refused += 1

    with ThreadPoolExecutor(max_workers=racers + 1) as pool:
        futures = [pool.submit(_race) for _ in range(racers)]
        begun.wait(timeout=5)
        time.sleep(0.02)  # let the racers pile onto the lock/probe window
        guard.release("sess-1", bundle)
        for future in futures:
            future.result()
    assert len(installed) <= 2, installed  # one before the release, one after
    assert refused == racers - len(installed)
    lease_present = bundle.extra.get("plan_lease") is not None
    assert lease_present == guard.is_active("sess-1")
    if lease_present:
        assert isinstance(bundle.extra["plan_lease"], PlanLease)


def test_the_guard_needs_only_an_extra_dict_and_a_session_id() -> None:
    """No server import: the bundle protocol is ``.extra`` + ``.context.session_id``,
    and a non-str id works."""
    guard = PlanLeaseGuard()
    bundle = SimpleNamespace(extra={}, context=SimpleNamespace(session_id=42))
    guard.install(bundle, _lease())
    assert guard.is_active(42)
    guard.release(42, bundle)
    assert bundle.extra == {}


# --- the retained prefix -----------------------------------------------------------------------


def test_merge_preserves_both_index_sets_and_collects_repositions() -> None:
    lease = _lease()
    entries = [
        _entry("executed", repositions=[{"sample": 1}]),
        _entry("dispatched_unverified"),
    ]
    merge_plan_step_prefix(lease, entries, source_offset=2)
    assert [e["index"] for e in lease.prefix] == [0, 1]
    assert lease.source_indices == [2, 3]
    assert lease.repositions == [{"sample": 1}]
    assert lease.context["partial_results"] is lease.prefix
    # redaction happened at the sink: entries are copies, never the caller's dicts
    assert entries[0]["index"] == 0


def test_set_remainder_records_only_the_unexecuted_tail() -> None:
    plan = _PlanModel(
        points=[{"id": "p0", "x": 0, "y": 0, "actions": [{"action": "click"}]}],
        transition_duration_ms=0,
    )
    specs, context = normalize_plan(plan)
    lease = PlanLease(model=plan, specs=specs, context=context, steps=plan_step_slices(specs, context))
    step_specs = lease.steps[0]
    set_plan_step_remainder(lease, 0, step_specs, [_entry()])
    assert lease.remainder == [
        {
            "step_index": 0,
            "point": {"id": "p0", "x": 0, "y": 0},
            "action_types": ["click"],  # the move's entry consumed slot 0; the click is the tail
            "reason": "previous_step_failed",
        }
    ]
    set_plan_step_remainder(lease, 0, step_specs, [_entry(), _entry()])
    assert lease.remainder == []


def test_remaining_names_unexecuted_points_and_honors_the_cursor() -> None:
    lease = _lease(points=3)
    lease.cursor = 1
    remaining = plan_remaining(lease, "awaiting_plan_action")
    assert [r["step_index"] for r in remaining] == [1, 2]
    assert all(r["reason"] == "awaiting_plan_action" for r in remaining)
    # a stored remainder wins over the cursor-derived description for its own step
    set_plan_step_remainder(lease, 1, lease.steps[1], [])
    assert lease.remainder
    remaining = plan_remaining(lease, "awaiting_plan_action")
    assert remaining[0]["step_index"] == 1
    assert remaining[0]["reason"] == "previous_step_failed"


def test_not_executed_re_reasons_every_entry() -> None:
    lease = _lease(points=2)
    set_plan_step_remainder(lease, 0, lease.steps[0], [_entry()])
    entries = plan_not_executed(lease, "limit_exceeded")
    assert all(e["reason"] == "limit_exceeded" for e in entries)
    assert [e["step_index"] for e in entries] == [0, 1]


def test_next_step_describes_the_pending_point_and_stops_at_the_end() -> None:
    plan = _PlanModel(
        points=[{"id": "p0", "x": 1, "y": 2, "actions": [{"action": "click"}]}],
    )
    specs, context = normalize_plan(plan)
    lease = PlanLease(model=plan, specs=specs, context=context, steps=plan_step_slices(specs, context))
    nxt = plan_next_step(lease)
    spec = plan.points[0].actions[0]
    assert nxt["step_index"] == 0
    assert nxt["point"] == {"id": "p0", "x": 1, "y": 2}
    assert nxt["actions"] == [spec.model_dump(mode="json")]
    assert nxt["action_types"] == ["click"]
    assert nxt["point_status"] == "pending"
    lease.cursor = len(lease.steps)
    assert plan_next_step(lease) is None


def test_next_step_redacts_user_text() -> None:
    plan = _PlanModel(
        points=[
            {
                "id": "p0",
                "x": 1,
                "y": 2,
                "actions": [{"action": "type", "text": "key AKIAIOSFODNN7EXAMPLE"}],
            }
        ],
    )
    specs, context = normalize_plan(plan)
    lease = PlanLease(model=plan, specs=specs, context=context, steps=plan_step_slices(specs, context))
    action = plan_next_step(lease)["actions"][0]
    assert "AKIAIOSFODNN7EXAMPLE" not in str(action["text"])


def test_redact_queue_entry_redacts_the_three_text_keys_at_the_sink() -> None:
    entry = {
        "message": "typed AKIAIOSFODNN7EXAMPLE into the field",
        "reasons": ["gate refused AKIAIOSFODNN7EXAMPLE"],
        "verification_note": "note AKIAIOSFODNN7EXAMPLE",
        "ok": False,
    }
    redacted = redact_queue_entry(entry)
    for key in ("message", "reasons", "verification_note"):
        assert "AKIAIOSFODNN7EXAMPLE" not in str(redacted[key]), key
    assert redacted["ok"] is False  # non-text fields pass through


# --- the cursor rule (K-53) --------------------------------------------------------------------


def test_the_cursor_advances_iff_an_item_of_the_step_dispatched() -> None:
    for kind in sorted(DISPATCHED_ITEM_KINDS):
        result = "verified" if kind == "executed" else "uncertain"
        assert plan_cursor_after(0, 0, [_entry(result, kind=kind)]) == 1
    for kind in ("rejected", "safety_denied", "approval_required", "error", "cancelled"):
        assert plan_cursor_after(0, 0, [_entry("rejected", kind=kind)]) == 0
    assert plan_cursor_after(3, 3, []) == 3  # nothing resolved -> nothing advanced


def test_a_partially_dispatched_step_still_advances() -> None:
    """The step is the unit of truth: one executed item out of three advances the
    cursor past the whole step (the remainder records what did not run)."""
    entries = [
        _entry("verified"),
        _entry("rejected", kind="rejected"),
        _entry("not_executed", kind="approval_required"),
    ]
    assert plan_step_attempted(entries)
    assert plan_cursor_after(1, 1, entries) == 2


def test_pre_dispatch_refusal_kinds_are_the_closed_complement() -> None:
    assert DISPATCHED_ITEM_KINDS == frozenset({"executed", "dispatched_unverified"})


# --- the terminal aggregate (V/4244 fix) -------------------------------------------------------


def test_the_terminal_token_set_is_closed() -> None:
    assert TERMINAL_PLAN_RESULTS == ("verified", "unknown", "failed", "stopped")


def test_all_verified_prefix_is_verified_and_ok() -> None:
    outcome = plan_terminal_outcome([_entry("verified"), _entry("verified")], expected_items=2)
    assert outcome.result == "verified"
    assert outcome.ok is True


def test_a_marker_only_prefix_is_verified_and_ok() -> None:
    """A plan of done/wait points: markers are the §1.1 non-screen ok tokens, so a
    marker prefix's terminal verdict is verified (nothing to disprove it)."""
    outcome = plan_terminal_outcome([_entry("marker")], expected_items=1)
    assert outcome.result == "verified"
    assert outcome.ok is True


def test_an_uncertain_prefix_is_unknown_and_ok_false() -> None:
    """The V/4244 fix: an all-uncertain plan is ``unknown`` with ok FALSE — never the
    dev ``ok = result != 'failed'`` honesty hole that reported unknown as success."""
    outcome = plan_terminal_outcome(
        [_entry("uncertain", verification_outcome="uncertain")], expected_items=1
    )
    assert outcome.result == "unknown"
    assert outcome.ok is False


def test_a_dispatched_unverified_item_is_unknown_and_ok_false() -> None:
    outcome = plan_terminal_outcome(
        [_entry("verified"), _entry(None, kind="dispatched_unverified",
                                    verification_outcome="uncertain")],
        expected_items=2,
    )
    assert outcome.result == "unknown"
    assert outcome.ok is False


@pytest.mark.parametrize(("entry", "expected"), REAL_SHAPE_MATRIX)
def test_every_real_entry_shape_classifies_fail_closed(entry: dict[str, Any], expected: str) -> None:
    """VAL-WP7a-01 #3: the classifier over the REAL ``agent._queue_entry`` shapes.
    Absent verification outcome / absent result is UNCERTAIN, never verified;
    dry_run and not_executed and rejected are never verified; only
    verified/marker count."""
    assert plan_result_from_entries([entry]) == expected
    outcome = plan_terminal_outcome([entry], expected_items=1)
    assert outcome.ok == (expected == "verified")


def test_an_executed_entry_with_no_verification_outcome_is_never_verified() -> None:
    """The fail-open hole the validator found: ``[{"kind": "executed", "index": 0}]``
    (no result, no verification_outcome) is uncertain/ok:false — never verified."""
    outcome = plan_terminal_outcome([{"kind": "executed", "index": 0}], expected_items=1)
    assert outcome.result == "unknown"
    assert outcome.ok is False


def test_a_failed_item_is_failed_and_ok_false() -> None:
    outcome = plan_terminal_outcome(
        [_entry("verified"), _entry("failed", verification_outcome="failed")],
        expected_items=2,
    )
    assert outcome.result == "failed"
    assert outcome.ok is False


def test_an_empty_prefix_fails_closed() -> None:
    outcome = plan_terminal_outcome([], expected_items=0)
    assert outcome.result == "failed"
    assert outcome.ok is False


def test_a_host_requested_stop_is_stopped_and_ok_false() -> None:
    """§1.2/§1.3: a stopped plan verified NOTHING, whatever its prefix looked like —
    even an all-verified prefix reports ``stopped`` with ok false."""
    outcome = plan_terminal_outcome([_entry("verified")], stopped=True, expected_items=1)
    assert outcome.result == "stopped"
    assert outcome.ok is False
    assert plan_terminal_outcome([], stopped=True, expected_items=0).result == "stopped"


def test_ok_is_verified_by_construction_over_the_whole_token_set() -> None:
    outcomes = {
        "verified": plan_terminal_outcome([_entry("verified")], expected_items=1),
        "unknown": plan_terminal_outcome([_entry("uncertain")], expected_items=1),
        "failed": plan_terminal_outcome([], expected_items=0),
        "stopped": plan_terminal_outcome([], stopped=True, expected_items=0),
    }
    assert set(outcomes) == set(TERMINAL_PLAN_RESULTS)
    for token, outcome in outcomes.items():
        assert outcome.result == token
        assert outcome.ok == (token == "verified")


# --- VAL-WP7a-01 #4: completeness ------------------------------------------------------------


def test_a_verified_but_partial_prefix_is_not_verified() -> None:
    """A prefix that verified everything it RAN, but covers fewer items than the plan
    declared, is capped at ``unknown`` (ok false): the unexecuted remainder's silence
    is an absence of evidence about the plan's declared end state — per §1.2,
    ``unknown`` ("dispatched, unproven" covers the remainder), not ``stopped`` (the
    host requested no stop) and not ``failed`` (nothing was proven not achieved)."""
    outcome = plan_terminal_outcome(
        [_entry("verified"), _entry("verified")], expected_items=4
    )
    assert outcome.result == "unknown"
    assert outcome.ok is False


def test_a_complete_verified_prefix_is_verified_with_the_expected_count() -> None:
    outcome = plan_terminal_outcome(
        [_entry("verified"), _entry("verified")], expected_items=2
    )
    assert outcome.result == "verified"
    assert outcome.ok is True


def test_a_partial_prefix_with_a_failure_is_still_failed() -> None:
    """Completeness caps VERIFIED only — a real failure in a partial prefix stays
    ``failed`` (the stronger verdict), never downgraded to unknown."""
    outcome = plan_terminal_outcome(
        [_entry("failed", verification_outcome="failed")], expected_items=4
    )
    assert outcome.result == "failed"
    assert outcome.ok is False


def test_an_uncertain_prefix_is_not_affected_by_the_expected_count() -> None:
    outcome = plan_terminal_outcome([_entry("uncertain")], expected_items=4)
    assert outcome.result == "unknown"
    assert outcome.ok is False


def test_a_stopped_plan_is_stopped_regardless_of_completeness() -> None:
    outcome = plan_terminal_outcome(
        [_entry("verified")], stopped=True, expected_items=4
    )
    assert outcome.result == "stopped"
    assert outcome.ok is False


def test_the_expected_count_is_a_required_argument() -> None:
    """CHANGED PIN (WP7b, §-reason: VAL-WP7a-02 #15 carry-over): ``expected_items`` is
    now a REQUIRED keyword of ``plan_terminal_outcome`` (no ``None`` default), so
    every caller passes the plan's declared item count and no caller can silently
    drop the completeness check. The old node pinned the parameter as OPTIONAL —
    that pin is superseded by the explicit carry-over instruction; the property it
    guarded (prefix-only semantics for unit contexts) is preserved by passing the
    ``len(entries)`` the context actually declares. No safety assertion weakened:
    the completeness cap now fires on every real call path."""
    assert plan_terminal_outcome([_entry("verified")], expected_items=1).result == "verified"
    with pytest.raises(TypeError):
        plan_terminal_outcome([_entry("verified")])  # type: ignore[call-arg]


def test_plan_result_from_entries_matches_the_step_vocabulary() -> None:
    assert plan_result_from_entries([]) == "failed"
    assert plan_result_from_entries([_entry("verified")]) == "verified"
    assert plan_result_from_entries([_entry("marker")]) == "verified"
    assert plan_result_from_entries([_entry("uncertain")]) == "unknown"
    assert plan_result_from_entries(
        [_entry(None, kind="dispatched_unverified", verification_outcome="uncertain")]
    ) == "unknown"
    assert plan_result_from_entries([_entry("rejected", kind="rejected")]) == "failed"
    assert plan_result_from_entries([_entry("dry_run", kind="executed")]) == "failed"
    assert plan_result_from_entries([{"kind": "executed"}]) == "unknown"


def test_plan_evidence_key_is_the_closed_d3_key() -> None:
    assert PLAN_EVIDENCE_KEY == "evidence_by_backend"


# --- lease construction sanity -----------------------------------------------------------------


def test_a_lease_carries_every_retained_field() -> None:
    lease = _lease()
    for name in (
        "model", "specs", "context", "steps", "cursor", "prefix", "source_indices",
        "approved", "repositions", "remainder", "last_failure_reason",
        "replaced_plan_id", "interference_events",
    ):
        assert hasattr(lease, name), name
    assert lease.cursor == 0 and lease.approved is False
    assert lease.prefix == [] and lease.source_indices == []
    assert lease.interference_events == []
