"""Wave 4 session-isolation tests: concurrent sessions share nothing (master-mission P0-K).

Two (or more) concurrent sessions with different fake backends/providers/goals must have
zero state cross-talk: observations, executed actions, approvals, stop tokens, step
counts, metrics, and audit logs stay separate; stopping one session never stops another;
one session's approval never authorizes another's action; the registry cap is enforced
fail-closed (no eviction of live sessions); and unknown/removed sessions fail closed on
every tool.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from test_controller_integration import (
    FAST_LIMITS,
    GatedProvider,
    ScriptedBackend,
    ScriptedProvider,
    audit_events,
    audit_types,
    executed_summary,
    make_session,
)
from test_controller_integration import (
    click as make_click,
)

from computer_use_mcp import server
from computer_use_mcp.models import AgentDecision
from computer_use_mcp.state import SessionRegistry


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh bounded registry/bundles + per-test audit dir for full session isolation.

    Resets EVERY piece of module-level server state the tools touch, including the
    D1 ``_stopped_sessions`` memory (stop_session records stopped ids there; without
    the reset that dict leaks across tests and makes results order-dependent).
    """
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    monkeypatch.setattr(server, "_stopped_sessions", {})
    return server

# --- zero cross-talk -------------------------------------------------------------------------


async def test_concurrent_sessions_have_zero_state_cross_talk(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend_one = ScriptedBackend()
    backend_two = ScriptedBackend()
    provider_one = ScriptedProvider(
        [make_click(10, 10), AgentDecision(status="done", summary="one done")]
    )
    provider_two = ScriptedProvider(
        [
            make_click(40, 40),
            make_click(50, 50),
            AgentDecision(status="done", summary="two done"),
        ]
    )
    sid_one, bundle_one, _, _ = make_session(
        monkeypatch, backend=backend_one, provider=provider_one,
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    sid_two, bundle_two, _, _ = make_session(
        monkeypatch, backend=backend_two, provider=provider_two,
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    result_one, result_two = await asyncio.gather(
        server.run_goal(sid_one, "goal one"),
        server.run_goal(sid_two, "goal two"),
    )

    assert result_one["ok"] is True and result_two["ok"] is True
    assert result_one["task_id"] != result_two["task_id"]

    # Executed actions: each backend saw exactly its own session's script.
    assert executed_summary(backend_one) == [("click", (10, 10), None)]
    assert executed_summary(backend_two) == [("click", (40, 40), None), ("click", (50, 50), None)]

    # Task state: goals and step counts are per-session.
    assert bundle_one.agent.task.goal == "goal one"
    assert bundle_two.agent.task.goal == "goal two"
    assert bundle_one.state.step_count == 1
    assert bundle_two.state.step_count == 2

    # Stop tokens are distinct objects and both remain unarmed.
    assert bundle_one.context.stop is not bundle_two.context.stop
    assert bundle_one.context.stop.stopped is False
    assert bundle_two.context.stop.stopped is False

    # Metrics registries are independent.
    counters_one = bundle_one.metrics.snapshot()["counters"]
    counters_two = bundle_two.metrics.snapshot()["counters"]
    assert counters_one["model_calls"] == 2
    assert counters_two["model_calls"] == 3
    assert counters_one["action_total"] == 1
    assert counters_two["action_total"] == 2

    # Observations never cross backends.
    obs_one = {obs.observation_id for obs in backend_one.observed}
    obs_two = {obs.observation_id for obs in backend_two.observed}
    assert obs_one and obs_two
    assert obs_one.isdisjoint(obs_two)

    # Audit logs are separate files and every event carries its own session id.
    assert bundle_one.auditor.path_for(sid_one) != bundle_two.auditor.path_for(sid_two)
    for sid, bundle in ((sid_one, bundle_one), (sid_two, bundle_two)):
        events = audit_events(bundle, sid)
        assert events
        assert {event["session_id"] for event in events} == {sid}


async def test_step_counts_and_histories_stay_per_session(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_one = ScriptedProvider(
        [make_click(10, 10), AgentDecision(status="done"), make_click(20, 20), AgentDecision(status="done")]
    )
    provider_two = ScriptedProvider([make_click(60, 60), AgentDecision(status="done")])
    sid_one, bundle_one, _, _ = make_session(
        monkeypatch, provider=provider_one, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    sid_two, bundle_two, _, _ = make_session(
        monkeypatch, provider=provider_two, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await server.run_goal(sid_one, "part one")
    await server.run_goal(sid_one, "part two")
    await server.run_goal(sid_two, "other task")

    assert bundle_one.state.step_count == 2
    assert bundle_two.state.step_count == 1
    assert len(bundle_one.agent.task.action_history) == 2
    assert len(bundle_two.agent.task.action_history) == 1
    assert bundle_one.agent.task.action_history[0].point == (10, 10)
    assert bundle_two.agent.task.action_history[0].point == (60, 60)


# --- stop isolation ---------------------------------------------------------------------------


async def test_stop_session_a_does_not_stop_session_b(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend_one = ScriptedBackend()
    provider_one = GatedProvider()  # second decide blocks on an asyncio gate
    provider_two = ScriptedProvider([make_click(40, 40), AgentDecision(status="done")])
    sid_one, bundle_one, backend_for_one, _ = make_session(
        monkeypatch, backend=backend_one, provider=provider_one,
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    sid_two, bundle_two, backend_two, _ = make_session(
        monkeypatch, provider=provider_two, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    task_one = asyncio.create_task(server.run_goal(sid_one, "long task one"))
    task_two = asyncio.create_task(server.run_goal(sid_two, "short task two"))
    for _ in range(500):
        if len(backend_one.executed) >= 1:
            break
        await asyncio.sleep(0.01)
    assert len(backend_one.executed) == 1  # session A is mid-task

    stop_response = server.stop_session(sid_one)
    assert stop_response["ok"] is True
    provider_one.gate.set()
    result_one, result_two = await asyncio.gather(task_one, task_two)

    assert result_one["stopped"] is True
    assert result_one["termination_reason"] == "stopped_by_user"
    # Session B completed untouched: its stop token was never armed, no stop audit leaked.
    assert result_two["termination_reason"] == "completed"
    assert result_two["ok"] is True
    assert bundle_two.context.stop.stopped is False
    assert bundle_two.state.stopped is False
    events_two = audit_events(bundle_two, sid_two)
    assert "emergency_stop" not in audit_types(events_two)
    assert "emergency_stop" in audit_types(audit_events(bundle_one, sid_one))
    assert executed_summary(backend_two) == [("click", (40, 40), None)]
    del backend_for_one  # readability alias; the executed assertions used backend_one


# --- approval isolation ------------------------------------------------------------------------


async def test_approval_granted_to_a_does_not_authorize_b(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_one = ScriptedProvider([make_click(10, 10), AgentDecision(status="done")])
    provider_two = ScriptedProvider([make_click(40, 40), AgentDecision(status="done")])
    sid_one, bundle_one, backend_one, _ = make_session(
        monkeypatch, provider=provider_one, dry_run=False, require_approval=True, limits=FAST_LIMITS
    )
    sid_two, bundle_two, backend_two, _ = make_session(
        monkeypatch, provider=provider_two, dry_run=False, require_approval=True, limits=FAST_LIMITS
    )
    result_one, result_two = await asyncio.gather(
        server.run_goal(sid_one, "goal one", approve_next_action=True),
        server.run_goal(sid_two, "goal two"),  # no approval budget
    )

    # A consumed its own budget and executed its own action.
    assert result_one["termination_reason"] == "completed"
    assert result_one["approval_budget_remaining"] == 0
    assert executed_summary(backend_one) == [("click", (10, 10), None)]
    assert len(bundle_one.agent._approved_action_ids) == 1

    # B was NOT authorized by A's approval: denied fail-closed, nothing executed.
    assert result_two["ok"] is False
    assert result_two["requires_approval"] is True
    assert result_two["termination_reason"] == "approval_exhausted"
    assert result_two["approval_budget_remaining"] == 0
    assert executed_summary(backend_two) == []
    assert bundle_two.agent._approved_action_ids == set()

    counters_two = bundle_two.metrics.snapshot()["counters"]
    assert counters_two["approval_denied"] == 1
    assert counters_two["approval_granted"] == 0


# --- registry cap (fail-closed, no eviction) ------------------------------------------------------


def test_registry_cap_enforced_fail_closed(fresh_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=2))
    first = server.start_session(dry_run=True)
    second = server.start_session(dry_run=True)
    assert "session_id" in first and "session_id" in second

    third = server.start_session(dry_run=True)
    assert third["ok"] is False
    assert third["error"] == "session_limit_exceeded"
    assert third["max_sessions"] == 2
    assert len(server._registry) == 2  # live sessions were never evicted


async def test_removed_session_frees_slot_and_unknown_sessions_fail_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=1))
    sid, _bundle, _, _ = make_session(monkeypatch, dry_run=True)
    assert server._registry.remove(sid) is not None  # slot released
    fourth = server.start_session(dry_run=True)
    assert "session_id" in fourth  # the freed slot is reusable

    # Every tool fails closed for an unknown session id — no state resurrection.
    observe = server.computer_observe("does-not-exist")
    assert observe["ok"] is False
    assert observe["error"] == "unknown_session"
    assert observe["session_id"] == "does-not-exist"
    stopped_tool = server.stop_session("does-not-exist")
    assert stopped_tool["ok"] is False and stopped_tool["error"] == "unknown_session"
    executed = await server.computer_execute("does-not-exist", "wait", delta=1)
    assert executed["ok"] is False and executed["error"] == "unknown_session"
    goal = await server.run_goal("does-not-exist", "no goal")
    assert goal["ok"] is False and goal["error"] == "unknown_session"
    assert "Traceback" not in json.dumps(
        [observe, stopped_tool, executed, goal]
    )


# --- stopped sessions stay fail-safe -----------------------------------------------------------------


async def test_tools_on_a_stopped_session_fail_safe(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([make_click(10, 10), AgentDecision(status="done")])
    sid, _bundle, backend, _ = make_session(
        monkeypatch, provider=provider, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    server.stop_session(sid)

    # D1 (fixed): stopping closes and removes the bundle — memory freed, registry consistent.
    assert sid not in server._bundles
    assert server._registry.get(sid) is None

    # D2 (fixed): run_goal on a stopped session refuses fail-closed with an explicit FAILED
    # result entry — never a vacuously-ok empty list.
    goal = await server.run_goal(sid, "run after stop")
    assert goal["ok"] is False
    assert goal["termination_reason"] == "stopped_by_user"
    assert goal["stopped"] is True
    assert len(goal["results"]) == 1
    assert goal["results"][0]["ok"] is False
    assert "stopped" in goal["results"][0]["message"].lower()

    observe = server.computer_observe(sid)
    assert observe["ok"] is False
    assert observe["error"] == "session_stopped"

    executed = await server.computer_execute(sid, "click", x=5, y=5)
    assert executed["ok"] is False
    assert executed["error"] == "session_stopped"
    assert executed_summary(backend) == []  # a stopped session never produces inputs


# --- T8 anomaly-B4 regression: session registry lifecycle under repeated calls ----------------
# The measurement bridge observed `unknown_session` for a session-id still in use while
# the server process was alive. The only in-process path producing that signature is a
# teardown that pops the bundle WITHOUT remembering the session as stopped (B4 fix: the
# snapshot is remembered FIRST, atomically under the lock, with fallible pieces guarded),
# plus divergence between the registry and the bundle store. These tests pin both.


async def test_repeated_session_calls_in_a_loop_never_lose_the_session(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeat session calls in a loop (execute/observe/progress): zero unknown_session."""
    provider = ScriptedProvider([AgentDecision(status="done", summary="done")])
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    for _ in range(40):
        observe = server.computer_observe(session_id)
        assert not (isinstance(observe, dict) and observe.get("error") == "unknown_session")
        progress = server.get_session_progress(session_id)
        assert progress["ok"] is True
        assert session_id in server._bundles
        assert server._registry.get(session_id) is bundle.context
    # 60 distinct tool calls total: the session is exactly as alive as at the start.
    assert server._bundles.get(session_id) is bundle


async def test_stop_session_then_every_tool_says_session_stopped_not_unknown(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([AgentDecision(status="done", summary="done")])
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, dry_run=True, require_approval=False, limits=FAST_LIMITS
    )
    stopped = server.stop_session(session_id)
    assert stopped["ok"] is True
    for _ in range(10):
        observe = server.computer_observe(session_id)
        assert observe["error"] == "session_stopped"  # NEVER unknown_session
        execute = await server.computer_execute(session_id, "wait", delta=0)
        assert execute["error"] == "session_stopped"


async def test_teardown_snapshot_failure_still_remembers_session_as_stopped(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B4 core regression: a failure inside teardown can NEVER leave a popped-but-
    unremembered session (the exact state that surfaces as bogus unknown_session)."""
    provider = ScriptedProvider([AgentDecision(status="done", summary="done")])
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, dry_run=True, require_approval=False, limits=FAST_LIMITS
    )

    def _exploding_snapshot() -> dict[str, Any]:
        raise RuntimeError("metrics sink exploded during teardown")

    monkeypatch.setattr(bundle.metrics, "snapshot", _exploding_snapshot)
    stopped = server.stop_session(session_id)
    assert stopped["ok"] is True  # teardown survived the injected failure
    # The session is REMEMBERED as stopped: structured session_stopped, never unknown.
    response = server.computer_observe(session_id)
    assert response["error"] == "session_stopped"
    assert session_id not in server._bundles
    assert session_id in server._stopped_sessions


def test_registry_and_bundle_store_stay_consistent_across_start_stop_cycles(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([AgentDecision(status="done", summary="done")])
    for _ in range(6):
        session_id, _bundle, _backend, _ = make_session(
            monkeypatch, provider=provider, dry_run=True, require_approval=False, limits=FAST_LIMITS
        )
        assert server._registry.get(session_id) is not None
        assert server.stop_session(session_id)["ok"] is True
        assert server._registry.get(session_id) is None
        assert session_id not in server._bundles
        assert session_id in server._stopped_sessions
