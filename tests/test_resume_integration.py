"""Resume integration tests: checkpoint -> restart -> ``start_session(resume_from_checkpoint=...)``.

End-to-end through the SERVER tool surface with fake backends/providers (no real
desktop): a long-running session builds subtask state, writes checkpoints (lifecycle
triggers + the BEFORE_SESSION_END trigger on ``stop_session``), and a NEW live session
is resumed from the checkpoint file as a CONTINUATION:

- counters are restored EXACTLY (never reset, never zeroed, never refilled);
- subtasks/dependencies/context/goal are restored verbatim;
- the resumed session carries the checkpoint's session id as continuation identity;
- the CURRENT environment is re-verified against the checkpoint's expectations
  (mismatch -> typed fail-closed refusal; corrupt/missing checkpoint -> refusal);
- approval is FRESH after resume (epochs are never resurrected from data);
- the checkpointed limits stay in force (no budget enlargement);
- execution continues on the restored plan through the same executor.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_controller_integration import FAST_LIMITS, ScriptedBackend, ScriptedProvider, make_session

from computer_use_mcp import server
from computer_use_mcp.checkpoint_manager import CheckpointManager
from computer_use_mcp.models import AgentDecision, WindowInfo
from computer_use_mcp.resume_manager import ResumeManager
from computer_use_mcp.state import SessionRegistry


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Same per-test server isolation as ``test_mcp_subtasks_tools.fresh_server``."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    monkeypatch.setattr(server, "_stopped_sessions", {})
    checkpoint_manager = CheckpointManager(tmp_path / "checkpoints")
    monkeypatch.setattr(server, "_checkpoint_manager", checkpoint_manager)
    monkeypatch.setattr(server, "_resume_manager", ResumeManager(checkpoint_manager))
    return server


def _done_provider() -> ScriptedProvider:
    return ScriptedProvider([AgentDecision(status="done", summary="done")])


def _checkpoint_path(session_id: str) -> Any:
    return server._checkpoint_manager.checkpoint_path(session_id)


async def _session_with_progress(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, dict[str, Any]]:
    """A session with a goal, one completed + one pending dependent subtask, and counters."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=_done_provider(), limits=FAST_LIMITS
    )
    await server.run_goal(session_id, "restore this goal")  # sets the session goal
    first = server.create_subtask(session_id=session_id, description="stage one")
    server.create_subtask(
        session_id=session_id,
        description="stage two",
        depends_on=[first["subtask"]["subtask_id"]],
    )
    await server.run_subtask(
        session_id=session_id, subtask_id=first["subtask"]["subtask_id"]
    )
    progress = server.get_session_progress(session_id)
    return session_id, first["subtask"]["subtask_id"], progress


async def test_resume_restores_counters_subtasks_and_continues(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_session_id, _second_id, progress_before = await _session_with_progress(monkeypatch)

    assert progress_before["progress_percent"] == 50.0
    # The tracker counts ORCHESTRATION consumption (the subtask); the earlier
    # single-goal run_goal call predates the runtime and is not part of it.
    assert progress_before["resource_counters"]["model_calls"] == 1
    path = _checkpoint_path(old_session_id)
    assert path.exists()  # lifecycle checkpoint from the completed subtask

    epoch_before = progress_before["approval_epoch"]
    server.stop_session(old_session_id)  # writes the BEFORE_SESSION_END checkpoint
    assert server._checkpoint_manager.has_checkpoint(old_session_id)

    response = server.start_session(
        limits=FAST_LIMITS, resume_from_checkpoint=str(path)
    )
    # Resume succeeds and returns the legacy shape plus additive continuation fields.
    assert "error" not in response, response
    assert response["resumed"] is True
    assert response["continuation_of"] == old_session_id
    new_session_id = str(response["session_id"])
    assert new_session_id != old_session_id  # a NEW live id...

    progress_after = server.get_session_progress(new_session_id)
    # ...that continues the SAME session: counters EQUAL, never reset or zeroed.
    assert progress_after["continuation_of"] == old_session_id
    assert progress_after["resource_counters"] == progress_before["resource_counters"]
    assert progress_after["resource_counters"]["model_calls"] == 1
    assert progress_after["completed_subtasks"] == 1
    assert progress_after["counts"] == {"pending": 1, "running": 0, "completed": 1,
                                        "failed": 0, "blocked": 0, "paused": 0}
    assert progress_after["goal"] == progress_before["goal"]
    # Approval is FRESH (never resurrected from the checkpoint) and valid.
    assert progress_after["approval_epoch"]["valid"] is True
    assert progress_after["approval_epoch"]["epoch_id"] != epoch_before["epoch_id"]
    # Subtask list restored verbatim (statuses + dependency edges).
    listed = server.list_subtasks(new_session_id)
    assert listed["counts"]["completed"] == 1
    assert listed["counts"]["pending"] == 1

    # The restored plan continues through the same executor to 100%.
    remaining = [
        summary["subtask_id"] for summary in listed["subtasks"] if summary["status"] == "pending"
    ]
    final = await server.run_subtask(session_id=new_session_id, subtask_id=remaining[0])
    assert final["ok"] is True
    assert final["progress"]["progress_percent"] == 100.0
    # Continued consumption accrues on TOP of the restored counters.
    assert final["progress"]["resource_counters"]["model_calls"] == 2


async def test_resume_context_is_restored_from_checkpoint(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_session_id, _second_id, _progress = await _session_with_progress(monkeypatch)
    path = _checkpoint_path(old_session_id)
    payload = server._checkpoint_manager.load(path)
    assert payload.context["goal"] == "restore this goal"
    server.stop_session(old_session_id)

    response = server.start_session(limits=FAST_LIMITS, resume_from_checkpoint=str(path))
    assert "error" not in response, response
    new_session_id = str(response["session_id"])
    runtime = server._bundles[new_session_id].extra["long_running"]

    restored = runtime.context.snapshot()
    assert restored["steps"] == payload.context["steps"]
    assert restored["steps_at_last_summary"] == payload.context["steps_at_last_summary"]
    assert restored["goal"] == payload.context["goal"]
    assert restored["recent_history"] == payload.context["recent_history"]
    assert restored["categories"] == payload.context["categories"]


async def test_resume_preserves_checkpoint_limits_and_budget(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = {"min_screenshot_interval_ms": 0, "max_session_steps": 500,
              "max_session_model_calls": 500, "max_session_actions": 2000}
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=_done_provider(), limits=limits
    )
    created = server.create_subtask(session_id=session_id, description="unit")
    await server.run_subtask(session_id=session_id, subtask_id=created["subtask"]["subtask_id"])
    before = server.get_session_progress(session_id)
    path = _checkpoint_path(session_id)
    server.stop_session(session_id)

    response = server.start_session(limits=limits, resume_from_checkpoint=str(path))
    assert "error" not in response, response
    new_session_id = str(response["session_id"])
    after = server.get_session_progress(new_session_id)

    # The checkpoint's OWN limits stay in force — no budget enlargement, no reset.
    assert after["resource_limits"] == before["resource_limits"]
    assert after["resource_limits"]["max_session_steps"] == 500
    assert after["resource_counters"] == before["resource_counters"]
    assert after["elapsed_seconds"] >= before["elapsed_seconds"] - 0.05


async def test_resume_refuses_environment_mismatch_fail_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_window = WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="App")
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch,
        provider=_done_provider(),
        backend=ScriptedBackend(active_window=app_window),
        limits=FAST_LIMITS,
    )
    created = server.create_subtask(session_id=session_id, description="unit")
    await server.run_subtask(session_id=session_id, subtask_id=created["subtask"]["subtask_id"])
    payload = server._checkpoint_manager.load(_checkpoint_path(session_id))
    assert payload.environment.active_process_name == "app.exe"
    path = _checkpoint_path(session_id)
    server.stop_session(session_id)
    sessions_before = len(server._registry)

    # The "restarted" machine now has a DIFFERENT foreground application: the resume
    # must refuse (stale-state enforcement, spec section 14) and release the slot.
    other_window = WindowInfo(hwnd=2, pid=20, process_name="other.exe", title="Other Window")
    monkeypatch.setattr(server, "_backend_factory", lambda: ScriptedBackend(active_window=other_window))
    response = server.start_session(limits=FAST_LIMITS, resume_from_checkpoint=str(path))

    assert response["ok"] is False
    assert response["error"] == "resume_refused"
    assert "active_process_matches" in response["checks"]
    assert len(server._registry) == sessions_before  # no live session was created


async def test_resume_refuses_corrupt_and_missing_checkpoints(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    corrupt = tmp_path / "corrupt-checkpoint.json"
    corrupt.write_text(json.dumps({"schema_version": 1, "garbage": True}), encoding="utf-8")
    response = server.start_session(limits=FAST_LIMITS, resume_from_checkpoint=str(corrupt))
    assert response["ok"] is False
    assert response["error"] == "invalid_checkpoint"

    truncated = tmp_path / "truncated-checkpoint.json"
    truncated.write_text('{"schema_version": 1, "sessi', encoding="utf-8")
    response = server.start_session(limits=FAST_LIMITS, resume_from_checkpoint=str(truncated))
    assert response["ok"] is False
    assert response["error"] == "invalid_checkpoint"

    missing = tmp_path / "does-not-exist.json"
    response = server.start_session(limits=FAST_LIMITS, resume_from_checkpoint=str(missing))
    assert response["ok"] is False
    assert response["error"] == "invalid_checkpoint"


async def test_resume_refuses_newer_schema_version(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    old_session_id, _second_id, _progress = await _session_with_progress(monkeypatch)
    path = _checkpoint_path(old_session_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = 999  # unknown/newer version: rejected fail-closed
    forged = tmp_path / "forged-version.json"
    forged.write_text(json.dumps(data), encoding="utf-8")
    server.stop_session(old_session_id)

    response = server.start_session(limits=FAST_LIMITS, resume_from_checkpoint=str(forged))
    assert response["ok"] is False
    assert response["error"] == "invalid_checkpoint"


async def test_checkpoint_files_contain_no_secrets(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from computer_use_mcp.redaction import contains_secret

    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=_done_provider(), limits=FAST_LIMITS
    )
    server.create_subtask(
        session_id=session_id,
        description="work with api_key=sk-supersecret123 and token=hunter2 in the goal",
    )
    await server.run_subtask(
        session_id=session_id,
        subtask_id=server.list_subtasks(session_id)["subtasks"][0]["subtask_id"],
    )
    path = _checkpoint_path(session_id)
    text = path.read_text(encoding="utf-8")
    assert contains_secret(text) is False
    assert "sk-supersecret123" not in text
