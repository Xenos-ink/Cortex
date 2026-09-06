"""Checkpoint manager tests (spec sections 7 + 17 "Checkpoints").

Covers: the periodic cadence (50 steps / 30 minutes, whichever first, injectable clock),
the lifecycle triggers, atomic writes (temp + os.replace, no partial observation, previous
checkpoint intact after failed writes), redaction of secret-bearing state (fail-closed
refusal when redaction cannot clean a value), the corruption matrix (truncated JSON, wrong
version, missing fields, tampered types, non-canonical limits, duplicate ids, screenshot
payloads, incoherent state — all rejected fail-closed without deleting the file), version
validation, and bounded checkpoint sizes/history.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from computer_use_mcp.checkpoint_manager import (
    CHECKPOINT_EVERY_SECONDS,
    CHECKPOINT_EVERY_STEPS,
    CHECKPOINT_FILENAME,
    CHECKPOINT_SCHEMA_VERSION,
    ENV_VAR_CHECKPOINT_DIR,
    HISTORY_ENTRY_MAX_CHARS,
    LIFECYCLE_TRIGGERS,
    RECENT_HISTORY_CAP,
    CheckpointManager,
    CheckpointRedactionError,
    CheckpointTrigger,
    CheckpointValidationError,
    CheckpointWriteError,
    EnvironmentExpectations,
    SessionSnapshot,
    should_checkpoint,
)
from computer_use_mcp.context_manager import ContextManager
from computer_use_mcp.limits import Limits, SessionBudgetTracker
from computer_use_mcp.models import ActionType, ExecutionResult, GroundedAction
from computer_use_mcp.redaction import contains_secret
from computer_use_mcp.subtask_manager import SubtaskManager


def make_limits(**overrides: Any) -> Limits:
    defaults: dict[str, Any] = {
        "max_session_actions": 100,
        "max_session_steps": 100,
        "max_session_model_calls": 50,
        "max_subtasks": 50,
        "context_summarize_every": 10,
    }
    return Limits(**{**defaults, **overrides}).validate()


def make_subtask_manager() -> tuple[SubtaskManager, str, str, str]:
    """Chain: first (completed, one result) <- second <- third (all stable ids returned)."""
    manager = SubtaskManager()
    first = manager.create("collect the source data")
    second = manager.create("format the workbook", depends_on=[first.subtask_id])
    third = manager.create("send the report", depends_on=[second.subtask_id])
    manager.start(first.subtask_id)
    manager.record_result(
        first.subtask_id,
        ExecutionResult(
            ok=True,
            action=GroundedAction(action=ActionType.WAIT),
            message="collected 12 rows",
        ),
    )
    manager.complete(first.subtask_id)
    return manager, first.subtask_id, second.subtask_id, third.subtask_id


def make_budget(limits: Limits) -> SessionBudgetTracker:
    tracker = SessionBudgetTracker(limits, start_monotonic=time.monotonic() - 1234.5)
    for _ in range(3):
        tracker.record_action()
    for _ in range(2):
        tracker.record_model_call()
    for _ in range(7):
        tracker.record_step()
    tracker.record_subtask()
    return tracker


def make_context() -> ContextManager:
    context = ContextManager(goal="prepare the monthly report", summarize_every=10)
    context.set_current_task("formatting the workbook")
    context.set_app_window_state("notepad.exe focused: notes.txt")
    context.append_history("opened the source workbook")
    context.append_history("cleaned sheet 1")
    context.record_accomplishment("loaded the source data")
    context.record_error("row 42 skipped")
    for _ in range(5):
        context.record_step()
    return context


def make_environment(**overrides: Any) -> EnvironmentExpectations:
    defaults: dict[str, Any] = {
        "active_process_name": "notepad.exe",
        "active_window_title": "notes.txt - Notepad",
        "app_window_state": "notepad.exe focused: notes.txt",
        "allowed_processes": ["notepad.exe", "calc*"],
        "allowed_windows": ["notes.txt - Notepad"],
    }
    return EnvironmentExpectations(**{**defaults, **overrides})


def build_state(
    base_dir: Path, session_id: str = "sess-abc123"
) -> tuple[CheckpointManager, dict[str, Any], dict[str, Any]]:
    """Live components + capture kwargs; the second dict exposes the originals."""
    limits = make_limits()
    subtasks, first, second, third = make_subtask_manager()
    tracker = make_budget(limits)
    context = make_context()
    manager = CheckpointManager(base_dir=base_dir)
    kwargs: dict[str, Any] = {
        "session_id": session_id,
        "goal": "prepare the monthly report",
        "subtasks_snapshot": subtasks.snapshot(),
        "budget_snapshot": tracker.snapshot(),
        "limits": limits,
        "context_snapshot": context.snapshot(),
        "session": SessionSnapshot(
            status="running", dry_run=False, require_approval=True, max_steps=100
        ),
        "environment": make_environment(),
        "current_subtask_id": second,
        "trigger": CheckpointTrigger.SUBTASK_COMPLETED,
    }
    state = {
        "limits": limits,
        "subtasks": subtasks,
        "tracker": tracker,
        "context": context,
        "ids": (first, second, third),
    }
    return manager, kwargs, state


def iter_string_values(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from iter_string_values(value)
    elif isinstance(node, list | tuple):
        for item in node:
            yield from iter_string_values(item)


# --- periodic cadence: 50 steps OR 30 minutes, whichever first -----------------------------


def test_cadence_constants_match_spec() -> None:
    assert CHECKPOINT_EVERY_STEPS == 50
    assert CHECKPOINT_EVERY_SECONDS == 1800.0


def test_should_checkpoint_every_50_steps() -> None:
    assert should_checkpoint(0, 0.0) is False
    assert should_checkpoint(49, 0.0) is False
    assert should_checkpoint(50, 0.0) is True
    assert should_checkpoint(51, 0.0) is True
    assert should_checkpoint(500, 0.0) is True


def test_should_checkpoint_at_30_minutes_with_injected_clock() -> None:
    assert should_checkpoint(0, CHECKPOINT_EVERY_SECONDS - 0.1) is False
    assert should_checkpoint(0, CHECKPOINT_EVERY_SECONDS) is True
    assert should_checkpoint(10, 1800.5) is True
    assert should_checkpoint(0, 3600.0) is True


def test_should_checkpoint_whichever_comes_first() -> None:
    assert should_checkpoint(50, 1.0) is True  # steps reached first
    assert should_checkpoint(49, 1799.0) is False  # neither reached
    assert should_checkpoint(49, 1800.0) is True  # time reached first


@pytest.mark.parametrize(
    ("steps", "seconds"),
    [
        ("5", 0.0),
        (True, 0.0),
        (None, 0.0),
        (-1, 0.0),
        (1, None),
        (1, -0.5),
        (1, "soon"),
    ],
)
def test_should_checkpoint_rejects_bad_input_fail_closed(steps: Any, seconds: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        should_checkpoint(steps, seconds)


# --- lifecycle triggers ---------------------------------------------------------------------


def test_lifecycle_triggers_match_spec() -> None:
    assert {trigger.value for trigger in LIFECYCLE_TRIGGERS} == {
        "subtask_completed",
        "subtask_failed",
        "subtask_transition",
        "before_session_end",
        "before_resume",
    }


def test_payload_records_trigger_and_identity(tmp_path: Path) -> None:
    manager, kwargs, _ = build_state(tmp_path)
    for trigger in CheckpointTrigger:
        payload = manager.capture_payload(**{**kwargs, "trigger": trigger})
        assert payload.trigger == trigger.value
    assert manager.capture_payload(**kwargs).created_at.tzinfo is not None
    chained = manager.capture_payload(**{**kwargs, "continuation_of": "sess-root"})
    assert chained.continuation_of == "sess-root"


# --- base directory + env override -----------------------------------------------------------


def test_base_dir_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR_CHECKPOINT_DIR, str(tmp_path))
    assert CheckpointManager().base_dir == tmp_path
    monkeypatch.delenv(ENV_VAR_CHECKPOINT_DIR, raising=False)
    assert CheckpointManager().base_dir != tmp_path  # default: temp dir convention


def test_session_paths_are_sanitized(tmp_path: Path) -> None:
    manager = CheckpointManager(base_dir=tmp_path)
    hostile = "weird/id with spaces"
    assert manager.session_dir(hostile).parent == tmp_path
    assert "/" not in manager.session_dir(hostile).name and "\\" not in manager.session_dir(hostile).name


# --- write + load roundtrip --------------------------------------------------------------------


def test_write_and_load_roundtrip(tmp_path: Path) -> None:
    manager, kwargs, state = build_state(tmp_path)
    path = manager.write_checkpoint(**kwargs)
    assert path == tmp_path / "sess-abc123" / CHECKPOINT_FILENAME
    payload = manager.load(path)
    assert payload.schema_version == CHECKPOINT_SCHEMA_VERSION
    assert payload.session_id == "sess-abc123"
    assert payload.goal == "prepare the monthly report"
    assert payload.trigger == CheckpointTrigger.SUBTASK_COMPLETED.value
    assert payload.current_subtask_id == state["ids"][1]
    assert payload.created_at.tzinfo is not None
    assert len(payload.subtasks["subtasks"]) == 3
    assert payload.dependency_edges[state["ids"][2]] == [state["ids"][1]]
    assert payload.environment.active_process_name == "notepad.exe"
    assert payload.environment.allowed_processes == ["notepad.exe", "calc*"]
    assert payload.session.status == "running" and payload.session.dry_run is False
    for key in ("actions", "model_calls", "steps", "subtasks"):
        assert payload.budget[key] == state["tracker"].snapshot()[key]
    results = payload.subtasks["subtasks"][0]["results"]
    assert len(results) == 1 and results[0]["screenshot_after_base64"] is None
    assert payload.recent_history == ["opened the source workbook", "cleaned sheet 1"]


def test_capture_injectable_timestamp(tmp_path: Path) -> None:
    manager, kwargs, _ = build_state(tmp_path)
    stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    payload = manager.capture_payload(**{**kwargs, "created_at": stamp})
    assert payload.created_at == stamp


def test_capture_rejects_non_canonical_limits(tmp_path: Path) -> None:
    manager, kwargs, _ = build_state(tmp_path)
    wild = Limits(max_session_actions=99_999)  # not validated/clamped
    with pytest.raises(ValueError, match="canonical"):
        manager.capture_payload(**{**kwargs, "limits": wild})


def test_capture_bounds_recent_history(tmp_path: Path) -> None:
    manager, kwargs, _ = build_state(tmp_path)
    payload = manager.capture_payload(**{**kwargs, "recent_history": [f"e{index}" for index in range(500)]})
    assert len(payload.recent_history) == RECENT_HISTORY_CAP
    assert all(len(entry) <= HISTORY_ENTRY_MAX_CHARS for entry in payload.recent_history)


# --- atomic write ---------------------------------------------------------------------------------


def test_atomic_write_no_partial_observation_and_crash_safety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, kwargs, _ = build_state(tmp_path)
    path = manager.write_checkpoint(**kwargs)
    first_text = path.read_text(encoding="utf-8")

    observed: dict[str, Any] = {}

    def crashing_replace(src: Any, dst: Any, **kw: Any) -> None:
        observed["src"] = Path(src)
        observed["dst"] = Path(dst)
        # A concurrent reader at this instant must see the PREVIOUS valid checkpoint.
        observed["previous_goal"] = json.loads(path.read_text(encoding="utf-8"))["goal"]
        raise OSError("simulated crash between temp write and replace")

    monkeypatch.setattr("computer_use_mcp.checkpoint_manager.os.replace", crashing_replace)
    with pytest.raises(CheckpointWriteError, match="previous checkpoint left intact"):
        manager.write_checkpoint(**{**kwargs, "goal": "revised goal"})
    monkeypatch.undo()

    # Previous checkpoint intact, no temp litter, temp file lived in the same directory.
    assert path.read_text(encoding="utf-8") == first_text
    assert manager.load(path).goal == "prepare the monthly report"
    assert observed["previous_goal"] == "prepare the monthly report"
    assert observed["src"].parent == path.parent
    assert observed["src"].name.startswith(".checkpoint-") and observed["src"].name.endswith(".tmp")
    assert observed["dst"] == path
    assert [item.name for item in path.parent.iterdir()] == [CHECKPOINT_FILENAME]


def test_successful_rewrite_replaces_content(tmp_path: Path) -> None:
    manager, kwargs, _ = build_state(tmp_path)
    manager.write_checkpoint(**kwargs)
    manager.write_checkpoint(**{**kwargs, "goal": "revised goal", "trigger": CheckpointTrigger.BEFORE_SESSION_END})
    payload = manager.load(manager.checkpoint_path("sess-abc123"))
    assert payload.goal == "revised goal"
    assert payload.trigger == CheckpointTrigger.BEFORE_SESSION_END.value


def test_write_refuses_oversized_checkpoint(tmp_path: Path) -> None:
    manager, kwargs, _ = build_state(tmp_path)
    tiny = CheckpointManager(base_dir=tmp_path, max_checkpoint_bytes=200)
    with pytest.raises(CheckpointWriteError, match="byte cap"):
        tiny.write_checkpoint(**kwargs)
    # Nothing was written (the gate fires before any filesystem mutation).
    assert not manager.checkpoint_path("sess-abc123").exists()


def test_load_rejects_oversized_file(tmp_path: Path) -> None:
    manager = CheckpointManager(base_dir=tmp_path, max_checkpoint_bytes=100)
    big = tmp_path / "sess-abc123"
    big.mkdir()
    (big / CHECKPOINT_FILENAME).write_text("x" * 500, encoding="utf-8")
    with pytest.raises(CheckpointValidationError, match="byte cap"):
        manager.load(big / CHECKPOINT_FILENAME)


# --- redaction ------------------------------------------------------------------------------------


def test_checkpoint_redacts_secrets_before_disk(tmp_path: Path) -> None:
    manager = CheckpointManager(base_dir=tmp_path)
    limits = make_limits()
    subtasks = SubtaskManager()
    seeded = subtasks.create("use api_key=supersecret123 carefully")
    subtasks.start(seeded.subtask_id)
    subtasks.record_result(
        seeded.subtask_id,
        ExecutionResult(
            ok=True,
            action=GroundedAction(action=ActionType.WAIT),
            message="aws key AKIAABCDEFGHIJKLMNOP appeared in logs",
        ),
    )
    context = ContextManager(goal="ship it (password=hunter2secret)")
    context.append_history("auth header: bearer abcdefghijklmnopqrstuvwxyz012345")
    payload = manager.capture_payload(
        session_id="sess-secret",
        goal="goal with token=aaaaaaaaaaaa inside",
        subtasks_snapshot=subtasks.snapshot(),
        budget_snapshot=SessionBudgetTracker(limits).snapshot(),
        limits=limits,
        context_snapshot=context.snapshot(),
    )
    path = manager.write(payload)
    text = path.read_text(encoding="utf-8")
    assert "hunter2secret" not in text
    assert "supersecret123" not in text
    assert "AKIAABCDEFGHIJKLMNOP" not in text
    assert "abcdefghijklmnopqrstuvwxyz012345" not in text
    assert "aaaaaaaaaaaa" not in text
    assert "[REDACTED:" in text
    data = json.loads(text)
    for value in iter_string_values(data):
        assert not contains_secret(value)


def test_redaction_refusal_is_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, kwargs, _ = build_state(tmp_path)
    path = manager.write_checkpoint(**kwargs)
    good_text = path.read_text(encoding="utf-8")

    # Simulate a broken redaction layer: values pass through untouched.
    monkeypatch.setattr(
        "computer_use_mcp.checkpoint_manager.redact_text", lambda text: (text, 0)
    )
    with pytest.raises(CheckpointRedactionError, match="refusing to write"):
        manager.write_checkpoint(**{**kwargs, "goal": "exfiltrate password=hunter2secret"})
    monkeypatch.undo()

    assert path.read_text(encoding="utf-8") == good_text
    assert manager.load(path).goal == "prepare the monthly report"
    assert [item.name for item in path.parent.iterdir()] == [CHECKPOINT_FILENAME]


# --- corruption matrix: fail-closed rejection, file never deleted ---------------------------------


def _mutated_copy(data: dict[str, Any], mutator: Any) -> str:
    mutator(data)
    return json.dumps(data)


def _dup_id(data: dict[str, Any]) -> None:
    data["subtasks"]["subtasks"].append(dict(data["subtasks"]["subtasks"][0]))


def _unknown_dep(data: dict[str, Any]) -> None:
    data["subtasks"]["subtasks"][0]["depends_on"] = ["ghost"]


def _self_dep(data: dict[str, Any]) -> None:
    entry = data["subtasks"]["subtasks"][1]
    entry["depends_on"] = [entry["subtask_id"]]


def _dep_cycle(data: dict[str, Any]) -> None:
    entries = data["subtasks"]["subtasks"]
    entries[0]["depends_on"] = [entries[2]["subtask_id"]]


def _bad_subtask_shape(data: dict[str, Any]) -> None:
    data["subtasks"]["subtasks"][0]["description"] = ""


def _screenshot_payload(data: dict[str, Any]) -> None:
    data["subtasks"]["subtasks"][0]["results"] = [
        {
            "ok": True,
            "action": {"action": "wait"},
            "message": "x",
            "screenshot_after_base64": "QUFB",
            "retry_count": 0,
        }
    ]


CORRUPTION_MUTATORS = (
    ("duplicate_subtask_ids", _dup_id),
    ("unknown_dependency", _unknown_dep),
    ("self_dependency", _self_dep),
    ("dependency_cycle", _dep_cycle),
    ("invalid_subtask_entity", _bad_subtask_shape),
    ("screenshot_payload_persisted", _screenshot_payload),
    ("subtasks_wrong_shape", lambda d: d.update(subtasks={"nope": []})),
    ("subtasks_not_mapping", lambda d: d.update(subtasks=[1, 2, 3])),
    ("budget_missing_field", lambda d: d["budget"].pop("steps")),
    ("budget_tampered_type", lambda d: d["budget"].update(actions="5")),
    ("budget_negative", lambda d: d["budget"].update(actions=-1)),
    ("budget_extra_key", lambda d: d["budget"].update(extra=1)),
    ("budget_version_type", lambda d: d["budget"].update(snapshot_version="1")),
    ("limits_missing_field", lambda d: d["limits"].pop("max_actions")),
    ("limits_unknown_field", lambda d: d["limits"].update(nonsense=5)),
    ("limits_tampered_type", lambda d: d["limits"].update(max_actions="100")),
    ("limits_bool_value", lambda d: d["limits"].update(max_actions=True)),
    ("limits_non_canonical", lambda d: d["limits"].update(max_session_actions=99_999)),
    ("limits_over_ceiling_subtasks", lambda d: d["limits"].update(max_subtasks=2)),
    ("context_missing_field", lambda d: d["context"].pop("steps")),
    ("context_steps_type", lambda d: d["context"].update(steps="3")),
    ("context_summary_type", lambda d: d["context"].update(summary="nope")),
    ("current_subtask_unknown", lambda d: d.update(current_subtask_id="ghost")),
    ("trigger_unknown", lambda d: d.update(trigger="whenever")),
    ("goal_blank", lambda d: d.update(goal="   ")),
    ("goal_control_char", lambda d: d.update(goal="bad\x00goal")),
    ("created_at_naive", lambda d: d.update(created_at="2026-01-01T00:00:00")),
    ("termination_incoherent", lambda d: d.update(termination={"terminated": True, "reason": None})),
    ("termination_unknown_reason", lambda d: d.update(termination={"terminated": True, "reason": "banana"})),
    ("session_unknown_status", lambda d: d["session"].update(status="quantum")),
    ("history_entry_too_long", lambda d: d.update(recent_history=["x" * (HISTORY_ENTRY_MAX_CHARS + 1)])),
    ("history_too_many_entries", lambda d: d.update(recent_history=["e"] * (RECENT_HISTORY_CAP + 1))),
    ("top_level_extra_field", lambda d: d.update(injected=True)),
)


@pytest.mark.parametrize(("name", "mutator"), CORRUPTION_MUTATORS, ids=[name for name, _ in CORRUPTION_MUTATORS])
def test_corrupted_checkpoint_rejected_fail_closed(
    tmp_path: Path, name: str, mutator: Any
) -> None:
    del name  # parametrization id only
    manager, kwargs, _ = build_state(tmp_path)
    path = manager.write_checkpoint(**kwargs)
    mutated = _mutated_copy(json.loads(path.read_text(encoding="utf-8")), mutator)
    path.write_text(mutated, encoding="utf-8")
    with pytest.raises(CheckpointValidationError):
        manager.load(path)
    assert path.exists()  # corrupt files are rejected, never deleted or repaired


def test_version_validation_rejects_unknown_and_newer(tmp_path: Path) -> None:
    manager, kwargs, _ = build_state(tmp_path)
    path = manager.write_checkpoint(**kwargs)
    data = json.loads(path.read_text(encoding="utf-8"))
    for bad_version in (2, 0, -1, "1", None):
        data["schema_version"] = bad_version
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(CheckpointValidationError, match="schema_version"):
            manager.load(path)
    data.pop("schema_version", None)
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CheckpointValidationError, match="schema_version"):
        manager.load(path)


def test_truncated_and_non_object_json_rejected(tmp_path: Path) -> None:
    manager, kwargs, _ = build_state(tmp_path)
    path = manager.write_checkpoint(**kwargs)
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw[: len(raw) // 2], encoding="utf-8")
    with pytest.raises(CheckpointValidationError, match="JSON"):
        manager.load(path)
    for junk in ("[1, 2, 3]", '"a string"', "42", ""):
        path.write_text(junk, encoding="utf-8")
        with pytest.raises(CheckpointValidationError):
            manager.load(path)
    assert path.exists()


def test_load_missing_or_directory_fails_closed(tmp_path: Path) -> None:
    manager = CheckpointManager(base_dir=tmp_path)
    with pytest.raises(CheckpointValidationError):
        manager.load(tmp_path / "missing" / CHECKPOINT_FILENAME)
    with pytest.raises(CheckpointValidationError):
        manager.load(tmp_path)  # a directory is not a checkpoint
