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
    ScriptedBackend,
    ScriptedProvider,
    audit_events,
    audit_types,
    executed_summary,
    make_session,
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
    """RETARGETED (run_goal removal): the direct surface drives two sessions
    concurrently; every isolation invariant below is the same one the loop path
    pinned (distinct stop tokens, per-session state/metrics/audit, disjoint
    observations, zero cross-execution)."""
    backend_one = ScriptedBackend()
    backend_two = ScriptedBackend()
    sid_one, bundle_one, _, _ = make_session(
        monkeypatch, backend=backend_one,
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    sid_two, bundle_two, _, _ = make_session(
        monkeypatch, backend=backend_two,
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    await asyncio.gather(
        server.computer_execute(sid_one, "click", x=10, y=10),
        server.computer_execute(sid_two, "click", x=40, y=40),
        server.computer_execute(sid_two, "click", x=50, y=50),
    )

    # Executed actions: each backend saw exactly its own session's calls.
    assert executed_summary(backend_one) == [("click", (10, 10), None)]
    assert executed_summary(backend_two) == [
        ("click", (40, 40), None),
        ("click", (50, 50), None),
    ]

    # Task state: step counts are per-session.
    assert bundle_one.state.step_count == 1
    assert bundle_two.state.step_count == 2

    # Stop tokens are distinct objects and both remain unarmed.
    assert bundle_one.context.stop is not bundle_two.context.stop
    assert bundle_one.context.stop.stopped is False
    assert bundle_two.context.stop.stopped is False

    # Metrics registries are independent.
    counters_one = bundle_one.metrics.snapshot()["counters"]
    counters_two = bundle_two.metrics.snapshot()["counters"]
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
    """RETARGETED (run_goal removal): step counts and action histories stay
    per-session across separate direct calls."""
    sid_one, bundle_one, _, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    sid_two, bundle_two, _, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await server.computer_execute(sid_one, "click", x=10, y=10)
    await server.computer_execute(sid_one, "click", x=20, y=20)
    await server.computer_execute(sid_two, "click", x=60, y=60)

    assert bundle_one.state.step_count == 2
    assert bundle_two.state.step_count == 1
    assert len(bundle_one.agent.task.action_history) == 2
    assert len(bundle_two.agent.task.action_history) == 1
    assert bundle_one.agent.task.action_history[0].point == (10, 10)
    assert bundle_two.agent.task.action_history[0].point == (60, 60)


# --- stop isolation ---------------------------------------------------------------------------
# REMOVED (run_goal removal): the mid-loop stop-isolation scenario (a gated second
# decide proving stopping session A never stops a live session-B LOOP) drove the
# loop's between-steps machinery; its direct replacement is the test below.


async def test_stop_session_a_does_not_stop_session_b(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopping session A leaves session B fully usable on the direct surface (the
    same no-cross-stop invariant the loop test pinned)."""
    backend_one = ScriptedBackend()
    backend_two = ScriptedBackend()
    sid_one, bundle_one, _, _ = make_session(
        monkeypatch, backend=backend_one,
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    sid_two, bundle_two, backend_two_ref, _ = make_session(
        monkeypatch, backend=backend_two,
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    stop_response = server.stop_session(sid_one)
    assert stop_response["ok"] is True
    assert bundle_two.context.stop.stopped is False
    assert bundle_two.state.stopped is False

    # Session B completes untouched after A's stop: no stop audit leaked into B.
    response = await server.computer_execute(sid_two, "click", x=40, y=40)
    payload = json.loads(response[0].text)
    assert payload["ok"] is True
    assert executed_summary(backend_two_ref) == [("click", (40, 40), None)]
    events_two = audit_events(bundle_two, sid_two)
    assert "emergency_stop" not in audit_types(events_two)
    assert "emergency_stop" in audit_types(audit_events(bundle_one, sid_one))


# --- approval isolation ------------------------------------------------------------------------
# REMOVED (run_goal removal): cross-session APPROVAL-BUDGET isolation was a run_goal
# call-budget semantic (one budget per run_goal call, consumed across loop steps).
# On the direct surface approval is the per-call ``approved`` flag — structurally
# per-session (the safety decision runs inside each session's agent), pinned by
# test_controller_integration.test_computer_execute_confidence_semantics... and by
# the zero-cross-talk invariants above.




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
    assert "Traceback" not in json.dumps(
        [observe, stopped_tool, executed]
    )


# --- stopped sessions stay fail-safe -----------------------------------------------------------------


async def test_tools_on_a_stopped_session_fail_safe(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    server.stop_session(sid)

    # D1 (fixed): stopping closes and removes the bundle — memory freed, registry consistent.
    assert sid not in server._bundles
    assert server._registry.get(sid) is None

    # AMENDED (run_goal removal): the removed loop tool's stopped-session shape check
    # died with the loop; every surviving tool fails closed with session_stopped.
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
    """Repeat the surviving tool calls in a loop: zero unknown_session.

    AMENDED (run_goal removal): get_session_progress was a removed tool; the loop
    now alternates observe + screenshot + execute — the surviving high-frequency
    host call pattern (40 iterations each)."""
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False,
        limits={"min_screenshot_interval_ms": 0},
    )
    for _ in range(40):
        observe = server.computer_observe(session_id)
        assert not (isinstance(observe, dict) and observe.get("error") == "unknown_session")
        shot = server.computer_screenshot(session_id)
        assert not (isinstance(shot, dict) and shot.get("error") == "unknown_session")
        execute = await server.computer_execute(session_id, "wait", delta=0)
        assert not (isinstance(execute, dict) and execute.get("error") == "unknown_session")
        assert session_id in server._bundles
        assert server._registry.get(session_id) is bundle.context
    # 120 distinct tool calls total: the session is exactly as alive as at the start.
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
