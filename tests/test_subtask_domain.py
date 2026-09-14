"""Subtask-domain checkpoint/resume tests (post loop-removal).

The mutation/transition APIs the removed loop family drove (create/start/complete/
fail/pause/resume/requeue, result + recovery bookkeeping, ready-set/dependents/list
reads, the fixed TRANSITIONS table) were REMOVED with it; the pins that exercised them
died with the loop. What survives in :class:`SubtaskManager` is the checkpoint/resume
container — constructor ceiling, ``snapshot``, and the fail-closed ``restore`` — and
these tests pin exactly that surface. Pure domain tests — no network, no provider, no
execution.
"""

from __future__ import annotations

import pytest

from computer_use_mcp.models import (
    MAX_SUBTASKS,
    SUBTASK_RESULTS_CAP,
    ExecutionResult,
    GroundedAction,
    Subtask,
    SubtaskStatus,
)
from computer_use_mcp.subtask_manager import (
    DependencyCycleError,
    InvalidSubtaskError,
    SelfDependencyError,
    SubtaskAlreadyExistsError,
    SubtaskError,
    SubtaskLimitExceeded,
    SubtaskManager,
    UnknownDependencyError,
)


def _result(message: str, screenshot: str | None = None) -> ExecutionResult:
    return ExecutionResult(
        ok=True,
        action=GroundedAction(action="wait"),
        message=message,
        screenshot_after_base64=screenshot,
    )


# --- ceiling configuration ----------------------------------------------------------------

def test_manager_rejects_out_of_range_ceilings() -> None:
    assert MAX_SUBTASKS == 50
    for bad_range in (0, -1, 51, 10_000):
        with pytest.raises(ValueError):
            SubtaskManager(max_subtasks=bad_range)
    for bad_type in (True, 2.5, "50"):
        with pytest.raises(TypeError):
            SubtaskManager(max_subtasks=bad_type)  # type: ignore[arg-type]
    assert SubtaskManager(max_subtasks=2).max_subtasks == 2


def test_restore_rejects_over_cap_snapshots() -> None:
    manager = SubtaskManager()
    items = [{"subtask_id": f"s{i}", "description": f"d{i}"} for i in range(51)]
    with pytest.raises(SubtaskLimitExceeded):
        manager.restore(items)
    assert len(manager) == 0  # fail-closed: nothing partially accepted


# --- restore accepts well-formed input ------------------------------------------------------

def test_restore_accepts_subtask_entities_and_bare_lists() -> None:
    manager = SubtaskManager()
    manager.restore(
        [
            Subtask(subtask_id="a", description="a"),
            Subtask(subtask_id="b", description="b", depends_on=["a"]),
        ]
    )
    assert len(manager) == 2
    ids = [dump["subtask_id"] for dump in manager.snapshot()["subtasks"]]
    assert ids == ["a", "b"]


def test_snapshot_restore_round_trip_is_lossless() -> None:
    source = SubtaskManager()
    source.restore(
        [
            Subtask(subtask_id="a", description="root work", status=SubtaskStatus.COMPLETED),
            Subtask(
                subtask_id="b",
                description="child work",
                depends_on=["a"],
                status=SubtaskStatus.PENDING,
            ),
            Subtask(
                subtask_id="c",
                description="running work",
                status=SubtaskStatus.RUNNING,
                results=[_result("kept step", screenshot="must-not-survive")],
            ),
        ]
    )

    snapshot = source.snapshot()
    target = SubtaskManager()
    target.restore(snapshot)

    assert snapshot["subtasks"] == target.snapshot()["subtasks"]
    by_id = {dump["subtask_id"]: dump for dump in target.snapshot()["subtasks"]}
    assert by_id["a"]["status"] == "completed"
    assert by_id["b"]["depends_on"] == ["a"]
    assert by_id["c"]["results"][0]["message"] == "kept step"
    # The container is lossless: whatever the checkpoint holds round-trips untouched.
    # (Heavy-payload STRIPPING lived in the removed record_result mutation API.)
    assert (
        by_id["c"]["results"][0]["screenshot_after_base64"]
        == snapshot["subtasks"][2]["results"][0]["screenshot_after_base64"]
    )
    # The SOURCE manager is untouched by restoring into another manager.
    assert source.snapshot() == snapshot


def test_json_round_trip_preserves_ids_statuses_and_bounds() -> None:
    import json

    source = SubtaskManager()
    source.restore(
        [
            Subtask(
                subtask_id="a",
                description="a",
                status=SubtaskStatus.RUNNING,
                results=[_result("json step", screenshot="stripped")],
            ),
            Subtask(subtask_id="b", description="b", depends_on=["a"]),
        ]
    )
    payload = json.loads(json.dumps(source.snapshot(), default=lambda o: getattr(o, "isoformat", lambda: str(o))()))
    target = SubtaskManager()
    target.restore(payload)
    dumps = {dump["subtask_id"]: dump for dump in target.snapshot()["subtasks"]}
    assert dumps["a"]["status"] == "running"
    assert dumps["a"]["results"][0]["message"] == "json step"
    assert dumps["b"]["depends_on"] == ["a"]


# --- restore is fail-closed on defects -------------------------------------------------------

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
    manager.restore({"subtasks": []})  # restoring into an EMPTY manager is legal
    populated = SubtaskManager()
    populated.restore(
        {"subtasks": [{"subtask_id": "a", "description": "a"}]}
    )
    with pytest.raises(InvalidSubtaskError):
        populated.restore({"subtasks": []})  # a second restore would mutate a live graph
