"""MCP tool-surface integration tests for the four new long-running tools (A5, spec §9).

Everything runs through the SERVER tool functions against ``FakeComputerBackend``
derivatives and scripted providers (repo fake patterns; no network, no real desktop).
Covered here:

- ``create_subtask``: argument validation, dependency-graph fail-closed, the
  50-subtask cap, unknown/stopped session refusals;
- ``list_subtasks``: structured BOUNDED summaries (no result payloads, no history);
- ``run_subtask``: one ready subtask per call through the REAL closed-loop executor,
  dependency gating (``subtask_not_ready``), the ``approve_next_action`` pause/resume
  path with approval-epoch accounting, unknown subtask fail-closed;
- ``get_session_progress``: deterministic percentage, counts, counters, checkpoint
  status; no-runtime and fail-closed paths;
- state persistence across separate MCP calls (no cross-call state loss);
- ``run_goal`` backward compatibility (exact legacy key set, default path untouched)
  and the additive ``auto_subtasks`` Multi-Subtask mode (plan -> sequential execution,
  planner fail-closed, malformed-plan rejection).
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from test_controller_integration import FAST_LIMITS, ScriptedProvider, make_session

from computer_use_mcp import server
from computer_use_mcp.checkpoint_manager import CheckpointManager
from computer_use_mcp.models import AgentDecision, GroundedAction
from computer_use_mcp.resume_manager import ResumeManager
from computer_use_mcp.state import SessionRegistry

_LEGACY_START_SESSION_KEYS = {
    "session_id",
    "step_count",
    "max_steps",
    "max_retries_per_action",
    "min_confidence",
    "dry_run",
    "require_approval",
    "stopped",
    "allowed_windows",
    "pending_approval_token",
    "allowed_processes",
    "limits",
    "task_id",
}

_LEGACY_RUN_GOAL_KEYS = {
    "ok",
    "approval_budget_remaining",
    "results",
    "session_id",
    "task_id",
    "termination_reason",
    "stopped",
    "requires_approval",
    "step_count",
    "metrics",
}


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh bounded registry/bundles + per-test audit and checkpoint directories.

    Resets EVERY piece of module-level server state the tools touch, including the
    stopped-session memory and the shared checkpoint store (long-running sessions
    persist checkpoints server-side, so tests must isolate the store per test).
    """
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    monkeypatch.setattr(server, "_stopped_sessions", {})
    checkpoint_manager = CheckpointManager(tmp_path / "checkpoints")
    monkeypatch.setattr(server, "_checkpoint_manager", checkpoint_manager)
    monkeypatch.setattr(server, "_resume_manager", ResumeManager(checkpoint_manager))
    return server


class PlanningProvider(ScriptedProvider):
    """ScriptedProvider plus a deterministic ``plan_subtasks`` proposal."""

    def __init__(self, proposal: Any, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.proposal = proposal
        self.plan_calls = 0

    async def plan_subtasks(self, goal: str, **kwargs: Any) -> Any:
        self.plan_calls += 1
        if isinstance(self.proposal, Exception):
            raise self.proposal
        return self.proposal


_VALID_PROPOSAL = {
    "subtasks": [
        {"subtask_id": "step-1", "description": "prepare the workspace", "depends_on": []},
        {"subtask_id": "step-2", "description": "finish the workflow", "depends_on": ["step-1"]},
    ]
}


def _click_decision(x: int = 100, y: int = 100) -> AgentDecision:
    return AgentDecision(
        status="action",
        action=GroundedAction(
            action="click",
            point={"x": x, "y": y},
            confidence=1.0,
            expected_effect="button pressed",
        ),
    )


# --- backward compatibility of the touched existing tools -----------------------------------------


def test_start_session_without_new_params_keeps_exact_legacy_shape(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, bundle, _backend, _provider = make_session(monkeypatch, limits=FAST_LIMITS)
    assert server._bundles[session_id] is bundle
    legacy = server.start_session(limits=FAST_LIMITS)
    assert set(legacy) == _LEGACY_START_SESSION_KEYS
    # PERF-004 C5 (the one sanctioned default change): dry_run now defaults to False;
    # the response shape and every other key are unchanged.
    assert legacy["dry_run"] is False
    assert "resumed" not in legacy and "continuation_of" not in legacy


def test_start_session_new_param_is_trailing_and_optional(fresh_server: Any) -> None:
    # T8: ``interference`` is the newest trailing-optional parameter; every earlier
    # additive parameter (resume_from_checkpoint) stays trailing-optional behind it.
    parameters = list(inspect.signature(server.start_session).parameters.values())
    assert [p.name for p in parameters][-1] == "interference"
    assert parameters[-1].default is None
    resume = [p for p in parameters if p.name == "resume_from_checkpoint"]
    assert resume and resume[0].default is None


def test_run_goal_signature_gain_is_trailing_optional(fresh_server: Any) -> None:
    parameters = list(inspect.signature(server.run_goal).parameters.values())
    assert [p.name for p in parameters][-1] == "auto_subtasks"
    assert parameters[-1].default is False


async def test_run_goal_default_path_keeps_exact_legacy_shape(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([AgentDecision(status="done", summary="done")])
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, limits=FAST_LIMITS
    )
    response = await server.run_goal(session_id, "single goal")
    assert set(response) == _LEGACY_RUN_GOAL_KEYS
    assert response["ok"] is True
    assert response["termination_reason"] == "completed"
    assert response["approval_budget_remaining"] == 0


# --- create_subtask -------------------------------------------------------------------------------


def test_create_subtask_unknown_and_stopped_sessions_fail_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown = server.create_subtask(session_id="missing", description="work")
    assert unknown["ok"] is False
    assert unknown["error"] == "unknown_session"

    session_id, _bundle, _backend, _provider = make_session(monkeypatch, limits=FAST_LIMITS)
    server.stop_session(session_id)
    stopped = server.create_subtask(session_id=session_id, description="work")
    assert stopped["ok"] is False
    assert stopped["error"] == "session_stopped"


def test_create_subtask_validates_arguments_and_graph(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _provider = make_session(monkeypatch, limits=FAST_LIMITS)

    empty = server.create_subtask(session_id=session_id, description="   ")
    assert empty["ok"] is False
    assert empty["error"] == "invalid_subtask"

    unknown_dep = server.create_subtask(
        session_id=session_id, description="work", depends_on=["ghost"]
    )
    assert unknown_dep["ok"] is False
    assert unknown_dep["error"] == "unknown_dependency"

    created = server.create_subtask(session_id=session_id, description="first unit of work")
    assert created["ok"] is True
    assert created["subtask"]["status"] == "pending"
    assert created["subtask"]["depends_on"] == []
    assert created["total_subtasks"] == 1

    chained = server.create_subtask(
        session_id=session_id,
        description="second unit",
        depends_on=[created["subtask"]["subtask_id"]],
    )
    assert chained["ok"] is True
    assert chained["subtask"]["depends_on"] == [created["subtask"]["subtask_id"]]


def test_create_subtask_enforces_the_fifty_cap(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _provider = make_session(monkeypatch, limits=FAST_LIMITS)
    for index in range(50):
        response = server.create_subtask(session_id=session_id, description=f"work {index}")
        assert response["ok"] is True, response
    over_cap = server.create_subtask(session_id=session_id, description="one too many")
    assert over_cap["ok"] is False
    assert over_cap["error"] == "subtask_limit_exceeded"


# --- list_subtasks --------------------------------------------------------------------------------


def test_list_subtasks_structured_and_bounded(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _provider = make_session(monkeypatch, limits=FAST_LIMITS)

    empty = server.list_subtasks(session_id)
    assert empty["ok"] is True
    assert empty["total"] == 0
    assert empty["counts"]["pending"] == 0
    assert empty["subtasks"] == []

    server.create_subtask(session_id=session_id, description="work one")
    server.create_subtask(session_id=session_id, description="work two")
    listed = server.list_subtasks(session_id)
    assert listed["ok"] is True
    assert listed["total"] == 2
    assert listed["counts"] == {
        "pending": 2,
        "running": 0,
        "completed": 0,
        "failed": 0,
        "blocked": 0,
        "paused": 0,
    }
    for summary in listed["subtasks"]:
        # Bounded projection: scalar fields + COUNTS, never result payloads/history.
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
    assert all(summary["result_count"] == 0 for summary in listed["subtasks"])


def test_list_subtasks_unknown_session_fail_closed(fresh_server: Any) -> None:
    response = server.list_subtasks(session_id="missing")
    assert response["ok"] is False
    assert response["error"] == "unknown_session"


# --- run_subtask ----------------------------------------------------------------------------------


async def test_run_subtask_lifecycle_with_dependency_gating(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([AgentDecision(status="done", summary="done")])
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, provider=provider, limits=FAST_LIMITS
    )
    first = server.create_subtask(session_id=session_id, description="stage one")
    second = server.create_subtask(
        session_id=session_id,
        description="stage two",
        depends_on=[first["subtask"]["subtask_id"]],
    )
    second_id = second["subtask"]["subtask_id"]

    not_ready = await server.run_subtask(session_id=session_id, subtask_id=second_id)
    assert not_ready["ok"] is False
    assert not_ready["error"] == "subtask_not_ready"
    assert not_ready["unmet_dependencies"] == [first["subtask"]["subtask_id"]]

    ran_first = await server.run_subtask(
        session_id=session_id, subtask_id=first["subtask"]["subtask_id"]
    )
    assert ran_first["ok"] is True
    assert ran_first["status"] == "completed"
    assert ran_first["termination_reason"] == "completed"
    assert ran_first["progress"]["progress_percent"] == 50.0

    ran_second = await server.run_subtask(session_id=session_id, subtask_id=second_id)
    assert ran_second["ok"] is True
    progress = ran_second["progress"]
    assert progress["progress_percent"] == 100.0
    assert progress["counts"]["completed"] == 2
    assert progress["checkpoint"]["has_checkpoint"] is True
    assert progress["checkpoint"]["last_trigger"] == "subtask_completed"
    assert backend.executes == 0  # dry-run session: the fake executor stayed idle


async def test_run_subtask_unknown_subtask_fail_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _provider = make_session(monkeypatch, limits=FAST_LIMITS)
    result = await server.run_subtask(session_id=session_id, subtask_id="ghost")
    assert result["ok"] is False
    assert result["error"] == "unknown_subtask"


async def test_run_subtask_stopped_session_fail_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _provider = make_session(monkeypatch, limits=FAST_LIMITS)
    server.create_subtask(session_id=session_id, description="work")
    server.stop_session(session_id)
    result = await server.run_subtask(session_id=session_id, subtask_id="whatever")
    assert result["ok"] is False
    assert result["error"] == "session_stopped"


async def test_run_subtask_approve_next_action_pause_and_resume(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([_click_decision(), _click_decision(), AgentDecision(status="done")])
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, limits=FAST_LIMITS
    )
    created = server.create_subtask(session_id=session_id, description="click then finish")
    subtask_id = created["subtask"]["subtask_id"]

    paused = await server.run_subtask(session_id=session_id, subtask_id=subtask_id)
    # require_approval=True and no budget: the interactive action is denied fail-closed
    # and the subtask is PAUSED (not failed) awaiting fresh approval.
    assert paused["ok"] is False
    assert paused["requires_approval"] is True
    assert paused["status"] == "paused"
    assert paused["termination_reason"] == "approval_exhausted"

    resumed = await server.run_subtask(
        session_id=session_id, subtask_id=subtask_id, approve_next_action=True
    )
    # The explicit per-call approval authorizes ONE interactive action; the subtask
    # resumes and completes through the same executor.
    assert resumed["ok"] is True
    assert resumed["status"] == "completed"
    assert resumed["approval_budget_remaining"] == 0
    progress = resumed["progress"]
    assert progress["approval_epoch"]["interactive_actions_used"] == 1
    assert progress["approval_epoch"]["valid"] is True
    assert progress["progress_percent"] == 100.0


async def test_state_persists_across_separate_mcp_calls(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Independent tool calls share server-side runtime state (no state loss)."""
    provider = ScriptedProvider([AgentDecision(status="done", summary="done")])
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, limits=FAST_LIMITS
    )
    created = server.create_subtask(session_id=session_id, description="unit a")
    server.create_subtask(session_id=session_id, description="unit b")

    first = await server.run_subtask(
        session_id=session_id, subtask_id=created["subtask"]["subtask_id"]
    )
    assert first["progress"]["resource_counters"]["model_calls"] == 1

    listed = server.list_subtasks(session_id)
    assert listed["counts"]["completed"] == 1  # status survived between calls

    second_created = server.create_subtask(session_id=session_id, description="unit c")
    assert second_created["total_subtasks"] == 3  # manager state survived between calls

    progress = server.get_session_progress(session_id)
    assert progress["resource_counters"]["model_calls"] == 1  # tracker did not reset
    assert progress["completed_subtasks"] == 1


# --- get_session_progress --------------------------------------------------------------------------


def test_get_session_progress_fail_closed_and_no_runtime(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown = server.get_session_progress(session_id="missing")
    assert unknown["ok"] is False
    assert unknown["error"] == "unknown_session"

    session_id, _bundle, _backend, _provider = make_session(monkeypatch, limits=FAST_LIMITS)
    fresh = server.get_session_progress(session_id)
    assert fresh["ok"] is True
    assert fresh["total_subtasks"] == 0
    assert fresh["progress_percent"] == 0.0
    assert fresh["task_status"] == "idle"
    assert fresh["checkpoint"]["has_checkpoint"] is False
    assert fresh["counts"]["pending"] == 0

    server.stop_session(session_id)
    stopped = server.get_session_progress(session_id)
    assert stopped["ok"] is False
    assert stopped["error"] == "session_stopped"


async def test_get_session_progress_is_deterministic(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([AgentDecision(status="done", summary="done")])
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, limits=FAST_LIMITS
    )
    created = server.create_subtask(session_id=session_id, description="unit")
    for _ in range(3):
        server.create_subtask(session_id=session_id, description="queued unit")
    before = server.get_session_progress(session_id)
    after_one = await server.run_subtask(
        session_id=session_id, subtask_id=created["subtask"]["subtask_id"]
    )
    again = server.get_session_progress(session_id)

    assert before["progress_percent"] == 0.0
    assert after_one["progress"]["progress_percent"] == 25.0
    assert again["progress_percent"] == 25.0  # same state -> same deterministic number
    assert again["counts"] == {
        "pending": 3,
        "running": 0,
        "completed": 1,
        "failed": 0,
        "blocked": 0,
        "paused": 0,
    }
    assert again["current_subtask_id"] == created["subtask"]["subtask_id"]


# --- run_goal auto_subtasks mode --------------------------------------------------------------------


async def test_run_goal_auto_subtasks_plans_and_executes_sequentially(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = PlanningProvider(
        _VALID_PROPOSAL, [AgentDecision(status="done", summary="done")]
    )
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, limits=FAST_LIMITS
    )
    response = await server.run_goal(session_id, "big multi-part goal", auto_subtasks=True)

    assert response["ok"] is True
    assert response["termination_reason"] == "completed"
    # Legacy keys all still present (shape compatible), plus additive fields.
    assert _LEGACY_RUN_GOAL_KEYS <= set(response)
    assert response["planned_subtasks"] == 2
    assert response["executed_subtasks"] == ["step-1", "step-2"]
    assert response["progress"]["progress_percent"] == 100.0
    assert provider.plan_calls == 1
    listed = server.list_subtasks(session_id)
    assert listed["counts"]["completed"] == 2


async def test_run_goal_auto_subtasks_planner_unavailable_fail_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([AgentDecision(status="done", summary="done")])
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, limits=FAST_LIMITS
    )
    response = await server.run_goal(session_id, "goal", auto_subtasks=True)

    assert response["ok"] is False
    assert response["error"] == "planner_unavailable"
    assert server.list_subtasks(session_id)["total"] == 0
    # The session itself stays usable.
    assert server.get_session_progress(session_id)["ok"] is True


async def test_run_goal_auto_subtasks_rejects_malformed_plan(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = PlanningProvider({"subtasks": []}, [AgentDecision(status="done")])
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, limits=FAST_LIMITS
    )
    response = await server.run_goal(session_id, "goal", auto_subtasks=True)

    assert response["ok"] is False
    assert response["error"] == "plan_rejected"
    assert "empty_plan" in response["codes"]
    assert server.list_subtasks(session_id)["total"] == 0


async def test_run_goal_auto_subtasks_stopped_session_fail_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _provider = make_session(monkeypatch, limits=FAST_LIMITS)
    server.stop_session(session_id)
    response = await server.run_goal(session_id, "goal", auto_subtasks=True)
    assert response["ok"] is False
    assert response["stopped"] is True
    assert response["termination_reason"] == "stopped_by_user"
    assert set(response) == _LEGACY_RUN_GOAL_KEYS
