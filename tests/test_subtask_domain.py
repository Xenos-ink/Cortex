"""Subtask domain unit tests (SubtasksProtocol section 17, "Subtasks" area).

Covers: creation, listing, lifecycle transitions (start/complete/fail/pause/resume),
blocked propagation, dependencies and dependency ordering, cycle detection, the 50-subtask
ceiling, bounded results retention, failure/recovery info, and snapshot/restore round-trips.
Pure domain tests — no backend, no execution, no real Windows automation.
"""

from __future__ import annotations

import threading

import pytest

from computer_use_mcp.models import (
    MAX_SUBTASKS,
    SUBTASK_RESULTS_CAP,
    ExecutionResult,
    FailureClass,
    GroundedAction,
    Subtask,
    SubtaskStatus,
)
from computer_use_mcp.plan_validator import find_cycle
from computer_use_mcp.subtask_manager import (
    SUBTASK_RESULTS_CAP as MANAGER_RESULTS_CAP,
)
from computer_use_mcp.subtask_manager import (
    TRANSITIONS,
    DependencyCycleError,
    InvalidSubtaskError,
    InvalidTransitionError,
    SelfDependencyError,
    SubtaskAlreadyExistsError,
    SubtaskError,
    SubtaskLimitExceeded,
    SubtaskManager,
    SubtaskNotReadyError,
    UnknownDependencyError,
    UnknownSubtaskError,
)


def _result(message: str, screenshot: str | None = None) -> ExecutionResult:
    return ExecutionResult(
        ok=True,
        action=GroundedAction(action="wait"),
        message=message,
        screenshot_after_base64=screenshot,
    )


def _chain(manager: SubtaskManager, *descriptions: str) -> list[str]:
    """Create a dependency chain a -> b -> c ... and return the ids in order."""
    ids: list[str] = []
    for index, description in enumerate(descriptions):
        subtask = manager.create(description, depends_on=[] if index == 0 else [ids[-1]], subtask_id=f"s{index}")
        ids.append(subtask.subtask_id)
    return ids


# --- creation -----------------------------------------------------------------------------

def test_create_returns_pending_subtask_with_stable_id() -> None:
    manager = SubtaskManager()
    subtask = manager.create("Open the monthly report", subtask_id="report")
    assert subtask.status is SubtaskStatus.PENDING
    assert subtask.subtask_id == "report"
    assert subtask.description == "Open the monthly report"
    assert subtask.depends_on == []
    assert subtask.created_at is not None
    assert subtask.started_at is None and subtask.completed_at is None
    assert list(subtask.results) == []
    # Stable id: the same id comes back from the manager, lossless.
    assert manager.require("report").subtask_id == "report"


def test_create_rejects_malformed_descriptions() -> None:
    manager = SubtaskManager()
    for bad in ("", "   ", "x" * 2_001, "line\nbreak", "nul\x00byte", 42):
        with pytest.raises(InvalidSubtaskError):
            manager.create(bad)  # type: ignore[arg-type]


def test_create_rejects_duplicate_explicit_id() -> None:
    manager = SubtaskManager()
    manager.create("first", subtask_id="a")
    with pytest.raises(SubtaskAlreadyExistsError):
        manager.create("second", subtask_id="a")


def test_create_rejects_unknown_dependency() -> None:
    manager = SubtaskManager()
    with pytest.raises(UnknownDependencyError) as excinfo:
        manager.create("needs ghost", depends_on=["ghost"])
    assert excinfo.value.dependency_id == "ghost"


def test_create_rejects_duplicate_dependencies() -> None:
    manager = SubtaskManager()
    manager.create("existing", subtask_id="a")
    with pytest.raises(InvalidSubtaskError):
        manager.create("dup deps", depends_on=["a", "a"])
    # create() checks id collisions before dependencies, so a self-dependency through an
    # existing id surfaces as a collision; genuine self-dependencies are caught on the
    # restore path (see restore fail-closed cases below).
    with pytest.raises(SubtaskAlreadyExistsError):
        manager.create("self dep", depends_on=["a"], subtask_id="a")


def test_create_rejects_malformed_dependency_shapes() -> None:
    manager = SubtaskManager()
    manager.create("existing", subtask_id="a")
    for bad in ("a", 42, ["a", 42], [""], ["x" * 129], ["bad\nid"], [["a"]]):
        with pytest.raises(InvalidSubtaskError):
            manager.create("bad deps", depends_on=bad)  # type: ignore[arg-type]


def test_create_cycle_defense() -> None:
    """Dependencies must pre-exist, so creation cannot close a cycle; the check stays
    fail-closed via the shared deterministic cycle finder."""
    manager = SubtaskManager()
    ids = _chain(manager, "a", "b", "c")
    assert find_cycle({sid: set(manager.require(sid).depends_on) for sid in ids}) is None
    leaf = manager.create("leaf", depends_on=[ids[-1]])
    assert manager.require(leaf.subtask_id).depends_on == [ids[-1]]


# --- listing (bounded structured output) --------------------------------------------------

def test_list_returns_bounded_structured_summaries_in_creation_order() -> None:
    manager = SubtaskManager()
    first = manager.create("first", subtask_id="a")
    manager.create("second", depends_on=["a"], subtask_id="b")
    manager.start("a")
    summaries = manager.list()
    assert [s["subtask_id"] for s in summaries] == ["a", "b"]
    assert summaries[0]["status"] == "running"
    assert summaries[1]["depends_on"] == ["a"]
    for summary in summaries:
        assert set(summary) == {
            "subtask_id",
            "description",
            "status",
            "depends_on",
            "created_at",
            "started_at",
            "completed_at",
            "result_count",
            "recovery_attempts",
            "failure",
        }
    assert summaries[0]["started_at"] is not None
    assert summaries[1]["started_at"] is None
    # Snapshots are detached: mutating the projection cannot touch manager state.
    summaries[0]["status"] = "hacked"
    assert manager.require(first.subtask_id).status is SubtaskStatus.RUNNING


def test_list_status_filter_is_deterministic() -> None:
    manager = SubtaskManager()
    manager.create("a", subtask_id="a")
    manager.create("b", subtask_id="b")
    assert [s["subtask_id"] for s in manager.list(status=SubtaskStatus.PENDING)] == ["a", "b"]
    manager.start("a")
    assert [s["subtask_id"] for s in manager.list(status="running")] == ["a"]
    assert manager.list(status="completed") == []
    with pytest.raises(InvalidSubtaskError):
        manager.list(status="flying")


def test_counts_has_every_status_key() -> None:
    manager = SubtaskManager()
    manager.create("a", subtask_id="a")
    manager.create("b", subtask_id="b")
    manager.start("a")
    counts = manager.counts()
    assert counts == {
        "pending": 1,
        "running": 1,
        "completed": 0,
        "failed": 0,
        "blocked": 0,
        "paused": 0,
    }


# --- deterministic transitions ------------------------------------------------------------

def test_transition_table_is_fixed_and_terminal_states_are_closed() -> None:
    assert set(TRANSITIONS) == set(SubtaskStatus)
    assert TRANSITIONS[SubtaskStatus.PENDING] == frozenset(
        {SubtaskStatus.RUNNING, SubtaskStatus.BLOCKED, SubtaskStatus.PAUSED, SubtaskStatus.FAILED}
    )
    assert TRANSITIONS[SubtaskStatus.RUNNING] == frozenset(
        {SubtaskStatus.COMPLETED, SubtaskStatus.FAILED, SubtaskStatus.PAUSED}
    )
    assert TRANSITIONS[SubtaskStatus.COMPLETED] == frozenset()
    assert TRANSITIONS[SubtaskStatus.FAILED] == frozenset()


def test_start_complete_records_timestamps() -> None:
    manager = SubtaskManager()
    manager.create("work", subtask_id="a")
    with pytest.raises(InvalidTransitionError):
        manager.complete("a")  # pending -> completed is not a legal transition
    running = manager.start("a")
    assert running.status is SubtaskStatus.RUNNING
    assert running.started_at is not None
    with pytest.raises(InvalidTransitionError):
        manager.start("a")  # already running
    completed = manager.complete("a")
    assert completed.status is SubtaskStatus.COMPLETED
    assert completed.completed_at is not None
    with pytest.raises(InvalidTransitionError):
        manager.start("a")  # terminal


def test_start_requires_all_dependencies_completed() -> None:
    manager = SubtaskManager()
    manager.create("dep", subtask_id="a")
    manager.create("work", depends_on=["a"], subtask_id="b")
    with pytest.raises(SubtaskNotReadyError) as excinfo:
        manager.start("b")
    assert excinfo.value.unmet == ["a"]
    manager.start("a")
    manager.complete("a")
    assert manager.start("b").status is SubtaskStatus.RUNNING


def test_invalid_transitions_are_typed_and_deterministic() -> None:
    manager = SubtaskManager()
    manager.create("a", subtask_id="a")
    with pytest.raises(UnknownSubtaskError):
        manager.start("ghost")
    with pytest.raises(InvalidTransitionError) as excinfo:
        manager.resume("a")  # only paused subtasks can be resumed
    assert excinfo.value.current is SubtaskStatus.PENDING
    manager.start("a")
    with pytest.raises(InvalidTransitionError):
        manager.resume("a")  # running is not paused either
    manager.pause("a")
    assert manager.resume("a").status is SubtaskStatus.RUNNING  # started_at set
    manager.pause("a")
    manager.requeue("a")
    with pytest.raises(InvalidTransitionError):
        manager.resume("a")  # requeued to pending: still, only paused subtasks resume


def test_pause_resume_round_trip_keeps_original_start_timestamp() -> None:
    manager = SubtaskManager()
    manager.create("a", subtask_id="a")
    manager.start("a")
    started_at = manager.require("a").started_at
    assert manager.pause("a").status is SubtaskStatus.PAUSED
    assert manager.resume("a").status is SubtaskStatus.RUNNING
    assert manager.require("a").started_at == started_at  # never reset
    manager.pause("a")
    assert manager.requeue("a").status is SubtaskStatus.PENDING  # paused -> pending is legal


def test_fail_records_failure_info_and_keeps_completed_at() -> None:
    manager = SubtaskManager()
    manager.create("a", subtask_id="a")
    manager.start("a")
    manager.record_recovery_attempt("a")
    manager.record_recovery_attempt("a")
    failed = manager.fail("a", failure_class=FailureClass.APP_CRASH, error="display gone")
    assert failed.status is SubtaskStatus.FAILED
    assert failed.completed_at is not None
    assert failed.failure is not None
    assert failed.failure.failure_class is FailureClass.APP_CRASH
    assert failed.failure.error == "display gone"
    assert failed.failure.recovery_attempts == 2
    assert failed.recovery_attempts == 2
    with pytest.raises(InvalidTransitionError):
        manager.start("a")


def test_fail_rejects_non_string_error_fields() -> None:
    manager = SubtaskManager()
    manager.create("a", subtask_id="a")
    manager.start("a")
    with pytest.raises(InvalidSubtaskError):
        manager.fail("a", error=42)  # type: ignore[arg-type]


# --- dependencies, ordering, blocked propagation -----------------------------------------

def test_dependency_ordering_diamond_ready_set_evolution() -> None:
    manager = SubtaskManager()
    manager.create("root", subtask_id="a")
    manager.create("left", depends_on=["a"], subtask_id="b")
    manager.create("right", depends_on=["a"], subtask_id="c")
    manager.create("join", depends_on=["b", "c"], subtask_id="d")
    assert manager.ready_set() == ["a"]
    manager.start("a")
    assert manager.ready_set() == []
    manager.complete("a")
    assert manager.ready_set() == ["b", "c"]  # deterministic creation order
    manager.start("b")
    manager.complete("b")
    assert manager.ready_set() == ["c"]  # d not ready: c incomplete
    manager.start("c")
    manager.complete("c")
    assert manager.ready_set() == ["d"]
    manager.start("d")
    manager.complete("d")
    assert manager.ready_set() == []


def test_failed_dependency_blocks_dependents_transitively() -> None:
    manager = SubtaskManager()
    manager.create("a", subtask_id="a")
    manager.create("b", depends_on=["a"], subtask_id="b")
    manager.create("c", depends_on=["b"], subtask_id="c")
    manager.create("independent", subtask_id="i")
    manager.fail("a", error="boom")
    assert manager.require("a").status is SubtaskStatus.FAILED
    assert manager.require("b").status is SubtaskStatus.BLOCKED
    assert manager.require("c").status is SubtaskStatus.BLOCKED
    assert manager.require("i").status is SubtaskStatus.PENDING
    assert manager.dependents("a", transitive=True) == ["b", "c"]
    assert manager.dependents("a") == ["b"]
    with pytest.raises(SubtaskNotReadyError):
        manager.start("b")


def test_blocked_dependency_blocks_dependents_too() -> None:
    manager = SubtaskManager()
    manager.create("a", subtask_id="a")
    manager.create("b", depends_on=["a"], subtask_id="b")
    manager.create("c", depends_on=["b"], subtask_id="c")
    manager.fail("a")
    assert manager.require("c").status is SubtaskStatus.BLOCKED  # via blocked b
    assert manager.ready_set() == []


def test_paused_dependent_is_blocked_by_failure_and_resume_paths_are_deterministic() -> None:
    manager = SubtaskManager()
    manager.create("a", subtask_id="a")
    manager.create("b", depends_on=["a"], subtask_id="b")
    manager.pause("b")
    assert manager.require("b").status is SubtaskStatus.PAUSED
    manager.fail("a")
    assert manager.require("b").status is SubtaskStatus.BLOCKED
    manager.requeue("b")
    assert manager.require("b").status is SubtaskStatus.PENDING
    # Paused without started_at resumes to pending; the failed dependency still gates start.
    manager.pause("b")
    assert manager.resume("b").status is SubtaskStatus.PENDING
    with pytest.raises(SubtaskNotReadyError):
        manager.start("b")


def test_paused_after_start_resumes_to_running() -> None:
    manager = SubtaskManager()
    manager.create("a", subtask_id="a")
    manager.start("a")
    manager.pause("a")
    assert manager.resume("a").status is SubtaskStatus.RUNNING


def test_blocked_dependent_via_paused_chain_from_earlier_failure() -> None:
    manager = SubtaskManager()
    manager.create("a", subtask_id="a")
    manager.create("b", depends_on=["a"], subtask_id="b")
    manager.create("c", depends_on=["b"], subtask_id="c")
    manager.fail("a")
    # Re-failing a terminal subtask is refused; propagation already reached c.
    with pytest.raises(InvalidTransitionError):
        manager.fail("a")
    assert manager.require("c").status is SubtaskStatus.BLOCKED


# --- 50-subtask ceiling -------------------------------------------------------------------

def test_subtask_cap_fifty_is_enforced_on_create() -> None:
    assert MAX_SUBTASKS == 50
    manager = SubtaskManager()
    for index in range(MAX_SUBTASKS):
        manager.create(f"subtask {index}", subtask_id=f"s{index}")
    with pytest.raises(SubtaskLimitExceeded) as excinfo:
        manager.create("one too many")
    assert excinfo.value.max_subtasks == 50
    assert len(manager) == 50


def test_manager_rejects_out_of_range_ceilings() -> None:
    for bad_range in (0, -1, 51, 10_000):
        with pytest.raises(ValueError):
            SubtaskManager(max_subtasks=bad_range)
    for bad_type in (True, 2.5, "50"):
        with pytest.raises(TypeError):
            SubtaskManager(max_subtasks=bad_type)  # type: ignore[arg-type]
    small = SubtaskManager(max_subtasks=2)
    small.create("a", subtask_id="a")
    small.create("b", subtask_id="b")
    with pytest.raises(SubtaskLimitExceeded):
        small.create("c")


def test_restore_rejects_over_cap_snapshots() -> None:
    manager = SubtaskManager()
    items = [{"subtask_id": f"s{i}", "description": f"d{i}"} for i in range(51)]
    with pytest.raises(SubtaskLimitExceeded):
        manager.restore(items)
    assert len(manager) == 0  # fail-closed: nothing partially accepted


def test_concurrent_create_respects_the_cap() -> None:
    manager = SubtaskManager()
    refused: list[int] = []
    barrier = threading.Barrier(60)

    def worker(index: int) -> None:
        barrier.wait()
        try:
            manager.create(f"subtask {index}")
        except SubtaskLimitExceeded:
            refused.append(index)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(60)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(manager) == 50
    assert len(refused) == 10


# --- bounded results retention (conflict C5) ----------------------------------------------

def test_results_are_saved_with_heavy_payloads_stripped() -> None:
    manager = SubtaskManager()
    manager.create("a", subtask_id="a")
    manager.start("a")
    manager.record_result("a", _result("step 1", screenshot="Zm9vYmFy"))  # base64 must not persist
    stored = manager.require("a")
    assert len(stored.results) == 1
    assert stored.results[0].message == "step 1"
    assert stored.results[0].ok is True
    assert stored.results[0].screenshot_after_base64 is None
    assert manager.list()[0]["result_count"] == 1


def test_results_retention_is_bounded_oldest_evicted() -> None:
    assert SUBTASK_RESULTS_CAP == MANAGER_RESULTS_CAP == 20
    manager = SubtaskManager()
    manager.create("a", subtask_id="a")
    manager.start("a")
    for index in range(SUBTASK_RESULTS_CAP + 5):
        manager.record_result("a", _result(f"r{index}"))
    stored = manager.require("a")
    assert len(stored.results) == SUBTASK_RESULTS_CAP
    assert [r.message for r in stored.results] == [f"r{i}" for i in range(5, SUBTASK_RESULTS_CAP + 5)]


def test_results_only_recorded_while_running_or_paused() -> None:
    manager = SubtaskManager()
    manager.create("a", subtask_id="a")
    with pytest.raises(InvalidTransitionError):
        manager.record_result("a", _result("too early"))
    manager.start("a")
    manager.record_result("a", _result("ok"))
    manager.complete("a")
    with pytest.raises(InvalidTransitionError):
        manager.record_result("a", _result("too late"))
    with pytest.raises(InvalidSubtaskError):
        manager.record_result("a", "not an ExecutionResult")  # type: ignore[arg-type]


def test_recovery_attempts_recorded_while_running() -> None:
    manager = SubtaskManager()
    manager.create("a", subtask_id="a")
    with pytest.raises(InvalidTransitionError):
        manager.record_recovery_attempt("a")
    manager.start("a")
    assert manager.record_recovery_attempt("a") == 1
    assert manager.record_recovery_attempt("a") == 2
    assert manager.require("a").recovery_attempts == 2


# --- snapshot / restore round-trips -------------------------------------------------------

def test_snapshot_restore_round_trip_is_lossless() -> None:
    source = SubtaskManager()
    source.create("root work", subtask_id="a")
    source.create("child work", depends_on=["a"], subtask_id="b")
    source.start("a")
    source.record_result("a", _result("kept step", screenshot="must-not-survive"))
    source.record_recovery_attempt("a")
    source.complete("a")
    source.create("paused work", depends_on=["b"], subtask_id="c")
    source.pause("c")
    source.create("doomed", subtask_id="d")
    source.start("d")
    source.fail("d", error="it failed", last_known_state="desktop focused")

    snapshot = source.snapshot()
    target = SubtaskManager()
    target.restore(snapshot)

    assert target.ids() == source.ids()
    for sid in source.ids():
        original = source.require(sid)
        restored = target.require(sid)
        assert restored.model_dump() == original.model_dump()
    restored_a = target.require("a")
    assert restored_a.status is SubtaskStatus.COMPLETED
    assert restored_a.completed_at == source.require("a").completed_at
    assert restored_a.results[0].message == "kept step"
    assert restored_a.results[0].screenshot_after_base64 is None
    assert target.require("c").status is SubtaskStatus.PAUSED
    assert target.require("d").failure is not None
    assert target.require("d").failure.failure_class is FailureClass.SUBTASK_FAILED
    # The restored graph is live: with 'a' completed, 'b' is ready and starts.
    assert target.ready_set() == ["b"]
    assert target.start("b").status is SubtaskStatus.RUNNING
    # The SOURCE manager is untouched by restoring into another manager.
    assert source.require("d").status is SubtaskStatus.FAILED
    assert source.require("b").status is SubtaskStatus.PENDING


def test_restore_accepts_subtask_entities_and_bare_lists() -> None:
    manager = SubtaskManager()
    manager.restore([Subtask(subtask_id="a", description="a"), Subtask(subtask_id="b", description="b", depends_on=["a"])])
    assert manager.ids() == ["a", "b"]
    assert manager.ready_set() == ["a"]


def test_restore_is_fail_closed_on_defects_and_leaves_state_empty() -> None:
    good = Subtask(subtask_id="a", description="a").model_dump()
    cases = [
        ("malformed shape", "not a snapshot"),
        ("malformed shape", {"subtasks": "nope"}),
        ("invalid entity", {"subtasks": [{"subtask_id": "a"}]}),  # missing description
        ("invalid status", {"subtasks": [{**good, "status": "flying"}]}),
        ("duplicate id", {"subtasks": [good, dict(good)]}),
        ("self dependency", {"subtasks": [{**good, "depends_on": ["a"]}]}),
        ("unknown dependency", {"subtasks": [{**good, "depends_on": ["ghost"]}]}),
        (
            "cycle",
            {
                "subtasks": [
                    {"subtask_id": "a", "description": "a", "depends_on": ["b"]},
                    {"subtask_id": "b", "description": "b", "depends_on": ["a"]},
                ]
            },
        ),
        ("oversize", [{"subtask_id": f"s{i}", "description": f"d{i}"} for i in range(51)]),
    ]
    for _label, payload in cases:
        manager = SubtaskManager()
        with pytest.raises(SubtaskError) as excinfo:
            manager.restore(payload)
        assert isinstance(
            excinfo.value,
            (
                InvalidSubtaskError,
                SubtaskAlreadyExistsError,
                UnknownDependencyError,
                SelfDependencyError,
                DependencyCycleError,
                SubtaskLimitExceeded,
            ),
        )
        assert len(manager) == 0


def test_restore_rejects_results_over_retention_cap() -> None:
    manager = SubtaskManager()
    dumped = Subtask(subtask_id="a", description="a").model_dump()
    dumped["results"] = [
        {"ok": True, "action": {"action": "wait"}, "message": f"r{i}"}  # type: ignore[dict-item]
        for i in range(SUBTASK_RESULTS_CAP + 1)
    ]
    # Fail-closed: a snapshot holding more results than the retention cap is refused whole.
    with pytest.raises(InvalidSubtaskError):
        manager.restore({"subtasks": [dumped]})
    assert len(manager) == 0


def test_restore_refuses_non_empty_manager() -> None:
    manager = SubtaskManager()
    manager.create("a", subtask_id="a")
    with pytest.raises(InvalidSubtaskError):
        manager.restore({"subtasks": []})


def test_json_round_trip_preserves_ids_statuses_and_bounds() -> None:
    source = SubtaskManager()
    source.create("a", subtask_id="a")
    source.create("b", depends_on=["a"], subtask_id="b")
    source.start("a")
    source.record_result("a", _result("json step", screenshot="stripped"))
    import json

    payload = json.loads(json.dumps(source.snapshot(), default=lambda o: getattr(o, "isoformat", lambda: str(o))()))
    target = SubtaskManager()
    target.restore(payload)
    restored = target.require("a")
    assert restored.status is SubtaskStatus.RUNNING
    assert restored.results[0].message == "json step"
    assert restored.results[0].screenshot_after_base64 is None
    assert target.ready_set() == []
