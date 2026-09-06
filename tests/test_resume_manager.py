"""Resume manager tests (spec sections 8/14 + 17 "Resume").

Covers: successful resume, state/dependencies/context/counters restored correctly (counter
values EQUAL the checkpoint values — no reset, no refill), limits never enlarged (the
checkpoint's own clamped limits), continuation identity (the resumed session carries the
original session id), invalid/corrupt checkpoints refused fail-closed, and the safety
re-verification hooks enforced at unit level (missing or mismatched current environment →
refusal, not continuation; allowlists re-checked).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_checkpoint_manager import build_state, make_limits, make_subtask_manager

from computer_use_mcp.checkpoint_manager import (
    CHECKPOINT_FILENAME,
    CheckpointManager,
    CheckpointTrigger,
    CheckpointValidationError,
    EnvironmentExpectations,
)
from computer_use_mcp.context_manager import ContextManager
from computer_use_mcp.limits import SessionBudgetTracker
from computer_use_mcp.resume_manager import (
    STATIC_CHECK_NAMES,
    ResumeManager,
    ResumeRefusalError,
)

MATCHING_ENVIRONMENT = {
    "active_process_name": "notepad.exe",
    "active_window_title": "notes.txt - Notepad",
}


def write_session(base_dir: Path) -> tuple[CheckpointManager, Path, dict[str, Any]]:
    manager, kwargs, state = build_state(base_dir)
    path = manager.write_checkpoint(**kwargs)
    return manager, path, state


def test_resume_restores_state_dependencies_and_identity(tmp_path: Path) -> None:
    manager, path, state = write_session(tmp_path)
    bundle = ResumeManager(manager).prepare(path)

    assert bundle.session_id == "sess-abc123"  # continuation identity (original session)
    assert bundle.continuation_identity == "sess-abc123"
    assert bundle.goal == "prepare the monthly report"
    assert bundle.current_subtask_id == state["ids"][1]
    first, second, third = state["ids"]
    assert {subtask.subtask_id: subtask.status.value for subtask in bundle.subtasks} == {
        first: "completed",
        second: "pending",
        third: "pending",
    }
    assert bundle.subtasks.ready_set() == [second]  # dependency graph restored exactly
    assert bundle.subtasks.dependents(first) == [second]
    assert bundle.recent_history == ["opened the source workbook", "cleaned sheet 1"]
    assert bundle.session.status == "running" and bundle.session.dry_run is False
    assert bundle.session.require_approval is True  # approval state carried, not bypassed


def test_resume_restores_counters_equal_to_checkpoint_without_reset(tmp_path: Path) -> None:
    manager, path, state = write_session(tmp_path)
    bundle = ResumeManager(manager).prepare(path)
    restored = bundle.budget.snapshot()
    checkpointed = manager.load(path).budget
    # Values are EQUAL to the checkpoint (never zeroed, never refilled).
    for key in ("actions", "model_calls", "steps", "subtasks"):
        assert restored[key] == checkpointed[key] == state["tracker"].snapshot()[key]
    assert checkpointed["actions"] == 3 and checkpointed["steps"] == 7
    # Elapsed time continues from the checkpoint anchor (no fresh budget).
    assert restored["elapsed_seconds"] >= checkpointed["elapsed_seconds"]
    bundle.budget.record_action()
    assert bundle.budget.snapshot()["actions"] == checkpointed["actions"] + 1


def test_resume_does_not_enlarge_limits(tmp_path: Path) -> None:
    manager, kwargs, _ = build_state(tmp_path, session_id="sess-limits")
    kwargs["limits"] = make_limits(max_session_actions=17, max_session_steps=23, max_session_model_calls=29)
    path = manager.write_checkpoint(**kwargs)
    bundle = ResumeManager(manager).prepare(path, current_environment=MATCHING_ENVIRONMENT)
    # The checkpoint's OWN limits are restored (clamped by the current mechanism), never
    # the defaults: a checkpoint with small limits stays small.
    assert bundle.limits.max_session_actions == 17
    assert bundle.limits.max_session_steps == 23
    assert bundle.limits.max_session_model_calls == 29
    assert bundle.budget.limits == bundle.limits


async def test_resume_restores_context_summary_and_counters(tmp_path: Path) -> None:
    manager = CheckpointManager(base_dir=tmp_path)
    limits = make_limits()
    context = ContextManager(goal="prepare the monthly report", summarize_every=10)
    context.set_current_task("formatting the workbook")
    context.append_history("opened the source workbook")
    context.record_accomplishment("loaded the source data")
    for _ in range(5):
        context.record_step()
    await context.summarize()
    context.append_history("post summary entry")
    context.record_step()
    original = context.build_request_payload()

    subtasks, _first, second, _third = make_subtask_manager()
    path = manager.write_checkpoint(
        session_id="sess-ctx",
        goal="prepare the monthly report",
        subtasks_snapshot=subtasks.snapshot(),
        budget_snapshot=SessionBudgetTracker(limits).snapshot(),
        limits=limits,
        context_snapshot=context.snapshot(),
        current_subtask_id=second,
        trigger=CheckpointTrigger.BEFORE_RESUME,
    )
    bundle = ResumeManager(manager).prepare(path, current_environment=MATCHING_ENVIRONMENT)

    restored = bundle.context.build_request_payload()
    assert restored["summary"] == original["summary"]
    assert restored["recent_history"] == original["recent_history"]
    assert bundle.context.steps == context.steps
    # Summarization cadence continues from the restored counters (no reset): it falls due
    # exactly summarize_every steps after the restored last-summary marker.
    while not bundle.context.should_summarize():
        bundle.context.record_step()
    assert bundle.context.steps == context.steps + 10 - 1


def test_resume_refuses_invalid_checkpoints_fail_closed(tmp_path: Path) -> None:
    manager, path, _ = write_session(tmp_path)
    resumes = ResumeManager(manager)
    raw = path.read_text(encoding="utf-8")
    cases = [
        raw[: len(raw) // 2],  # truncated
        json.dumps({**json.loads(raw), "schema_version": 99}),  # newer version
        json.dumps({**json.loads(raw), "budget": {"bogus": True}}),  # broken structure
    ]
    for index, case in enumerate(cases):
        target = path if index == 0 else tmp_path / f"case-{index}" / CHECKPOINT_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(case, encoding="utf-8")
        with pytest.raises(CheckpointValidationError):
            resumes.prepare(target)


def test_resume_refuses_when_component_restore_fails(tmp_path: Path) -> None:
    manager, path, _ = write_session(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["budget"]["snapshot_version"] = 99  # load-compatible, restore-incompatible
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ResumeRefusalError, match="budget_restored") as excinfo:
        ResumeManager(manager).prepare(path, current_environment=MATCHING_ENVIRONMENT)
    failed = [check.name for check in excinfo.value.checks.outcomes if not check.ok]
    assert "budget_restored" in failed


def test_resume_refuses_environment_mismatch(tmp_path: Path) -> None:
    manager, path, _ = write_session(tmp_path)
    with pytest.raises(ResumeRefusalError) as excinfo:
        ResumeManager(manager).prepare(
            path,
            current_environment={
                "active_process_name": "excel.exe",
                "active_window_title": "Book1 - Excel",
            },
        )
    failed = {check.name for check in excinfo.value.checks.outcomes if not check.ok}
    assert "active_process_matches" in failed
    assert "active_window_matches" in failed


def test_resume_refuses_when_current_environment_missing(tmp_path: Path) -> None:
    manager, path, _ = write_session(tmp_path)
    # Explicitly empty current environment: the checkpoint recorded expectations, so an
    # unverified environment must stop the continuation (stale-state enforcement).
    with pytest.raises(ResumeRefusalError, match="environment") as excinfo:
        ResumeManager(manager).prepare(path, current_environment={})
    assert "current_environment_provided" in {
        check.name for check in excinfo.value.checks.outcomes if not check.ok
    }


def test_resume_without_verification_is_marked_not_ok(tmp_path: Path) -> None:
    manager, path, _ = write_session(tmp_path)
    bundle = ResumeManager(manager).prepare(path)
    # Pending environment verification keeps the checklist not-ok: continuation requires
    # an explicit verify_environment pass (a refusal result, never a silent continuation).
    assert bundle.checks.ok is False
    provided = [check for check in bundle.checks.outcomes if check.name == "current_environment_provided"]
    assert provided and provided[0].ok is False
    verified = bundle.verify_environment(MATCHING_ENVIRONMENT)
    assert verified.ok is True
    assert set(STATIC_CHECK_NAMES).issubset({check.name for check in verified.outcomes})
    assert bundle.verify_environment(None).ok is False


def test_resume_enforces_allowlists_after_resume(tmp_path: Path) -> None:
    manager, path, _ = write_session(tmp_path)
    bundle = ResumeManager(manager).prepare(path, current_environment=MATCHING_ENVIRONMENT)
    process_violation = bundle.verify_environment(
        {"active_process_name": "cmd.exe", "active_window_title": "notes.txt - Notepad"}
    )
    assert process_violation.ok is False
    assert "process_allowlist_satisfied" in [check.name for check in process_violation.failures]
    window_violation = bundle.verify_environment(
        {"active_process_name": "notepad.exe", "active_window_title": "Registry Editor"}
    )
    assert window_violation.ok is False
    assert "window_allowlist_satisfied" in [check.name for check in window_violation.failures]


def test_resume_with_no_recorded_expectations_passes_checks(tmp_path: Path) -> None:
    manager, kwargs, _ = build_state(tmp_path, session_id="sess-bare")
    kwargs["environment"] = EnvironmentExpectations()
    path = manager.write_checkpoint(**kwargs)
    bundle = ResumeManager(manager).prepare(path)
    assert bundle.checks.ok is True  # nothing recorded to re-verify; allowlists still flow
    assert bundle.environment.active_process_name is None


def test_resume_carries_continuation_chain_identity(tmp_path: Path) -> None:
    manager, kwargs, _ = build_state(tmp_path, session_id="sess-resumed")
    kwargs["continuation_of"] = "sess-original"
    path = manager.write_checkpoint(**kwargs)
    bundle = ResumeManager(manager).prepare(path, current_environment=MATCHING_ENVIRONMENT)
    assert bundle.session_id == "sess-resumed"
    assert bundle.continuation_identity == "sess-original"  # original id carried forward


def test_resume_termination_state_roundtrip(tmp_path: Path) -> None:
    manager, kwargs, _ = build_state(tmp_path, session_id="sess-term")
    kwargs["termination"] = {"terminated": True, "reason": "unrecoverable"}
    path = manager.write_checkpoint(**kwargs)
    bundle = ResumeManager(manager).prepare(path, current_environment=MATCHING_ENVIRONMENT)
    assert bundle.termination.terminated is True
    assert bundle.termination.reason == "unrecoverable"


def test_resume_manager_uses_injected_checkpoint_manager(tmp_path: Path) -> None:
    manager = CheckpointManager(base_dir=tmp_path)
    assert ResumeManager(manager).checkpoints is manager


def test_allowlist_matcher_semantics() -> None:
    from computer_use_mcp.resume_manager import matches_allowlist

    assert matches_allowlist("Notepad.exe", ["notepad.exe"])  # case-insensitive exact
    assert matches_allowlist("Calculator.exe", ["calc*"])  # trailing-wildcard prefix
    assert not matches_allowlist("Untitled - Notepad", ["Notepad*"])  # prefix, not substring
    assert not matches_allowlist("cmd.exe", [])
    assert not matches_allowlist("cmd.exe", ["notepad.exe", ""])
