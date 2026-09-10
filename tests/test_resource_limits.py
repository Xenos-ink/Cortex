"""Wave 4 resource-limit tests: every limit class trips with a clean, audited termination.

Each of the 8 limit classes (master-mission section 6) is driven to its trip point through
the server tool surface with fakes only. Every trip must produce: ``LimitExceeded``
fail-closed handling, a structured ``limit_exceeded`` termination reason, an audited
``limit_exceeded`` event, and a tool response that returns (never hangs, never crashes).

Note on injectability: ``Limits.validate()`` clamps ``max_task_seconds`` to >= 1.0s, so the
task-duration limit is injected by backdating the enforcer's monotonic start (the
established W3 pattern) instead of waiting real time. The screenshot-rate trip requires an
interval above the 2s controller wait ceiling and therefore costs ~2s of real time; the
gate is additionally asserted at enforcer level with no waiting.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_controller_integration import (
    FAST_LIMITS,
    audit_events,
    executed_summary,
    make_session,
)

from computer_use_mcp import server
from computer_use_mcp.limits import LimitEnforcer, LimitExceeded, Limits
from computer_use_mcp.state import SessionRegistry, TaskState


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


def limit_events(bundle: Any, session_id: str) -> list[dict[str, Any]]:
    return [
        event
        for event in audit_events(bundle, session_id)
        if event["event_type"] == "limit_exceeded"
    ]


def _payload(result: Any) -> dict[str, Any]:
    """Unwrap an executed computer_execute content-block response to its dict payload."""
    import json as _json

    if isinstance(result, list):
        return _json.loads(result[0].text)
    return result


def _payload(result: Any) -> dict[str, Any]:
    """Unwrap an executed computer_execute content-block response to its dict payload."""
    import json as _json

    if isinstance(result, list):
        return _json.loads(result[0].text)
    return result


# --- action / model-call / duration limits (RETARGETED, run_goal removal) ----------------
# The limit classes survive; the enforcement SURFACE for the direct path is pinned
# via computer_execute (max_actions, screenshot interval) and the enforcer seam
# (task duration — the loop-top check the loop used to make). Loop-only limit
# classes that have no direct-path equivalent (max_model_calls, max_context_items,
# in-loop retry/recovery budgets) died with the loop: the direct path makes no
# model calls and keeps no conversation context.


async def test_max_actions_limit_trips_cleanly(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False,
        limits={**FAST_LIMITS, "max_actions": 1},
    )
    first = await server.computer_execute(session_id, "click", x=10, y=10)
    first = _payload(first)
    assert first["ok"] is True, first
    response = await server.computer_execute(session_id, "click", x=20, y=20)

    assert response["ok"] is False
    assert response["error"] == "limit_exceeded"
    assert response["limit"] == "max_actions"
    assert len(executed_summary(backend)) == 1  # the first action ran, nothing further
    events = limit_events(bundle, session_id)
    assert events and events[-1]["metadata"]["limit"] == "max_actions"


async def test_max_task_seconds_limit_trips_cleanly(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # validate() clamps the floor to 1.0s, so the tiny limit is injected by backdating.
    assert Limits(max_task_seconds=0.01).validate().max_task_seconds == 1.0
    _session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    bundle.enforcer._started_monotonic -= 10_000.0  # task "started" 10000s ago
    # AMENDED (run_goal removal): the loop-top check the loop used to make, pinned at
    # the enforcer seam — the same method, the same typed error.
    with pytest.raises(LimitExceeded) as excinfo:
        bundle.enforcer.check_task_duration()
    assert excinfo.value.limit_name == "max_task_seconds"


# --- screenshot rate gate -----------------------------------------------------------------------------


async def test_screenshot_rate_gate_trips_after_controller_wait_ceiling(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PERF-004 C2 refined gate semantics on the DIRECT path: fresh observations
    stay protected. The host-driven observe path (computer_execute ->
    direct_request capture) is gated: the first action executes, and the second
    action's fresh capture arrives within the 60s interval, waits past the 2s
    controller ceiling, and fails closed with an audited typed error.

    RETARGETED (run_goal removal): the old test's loop half (gate-free in-loop
    cycle) died with the loop."""
    session_id, bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False,
        limits={"min_screenshot_interval_ms": 60_000},
    )
    first = await server.computer_execute(session_id, "click", x=10, y=10)
    first = _payload(first)
    assert first["ok"] is True, first
    assert len(executed_summary(backend)) == 1

    # The host-driven observe path is STILL gated: this capture is far inside the
    # 60s interval, waits past the 2s ceiling, and fails closed.
    limited = await server.computer_execute(session_id, "wait", delta=1)
    assert limited["ok"] is False
    assert limited["error"] == "limit_exceeded"
    assert limited["limit"] == "min_screenshot_interval_ms"
    events = limit_events(bundle, session_id)
    assert events and events[-1]["metadata"]["limit"] == "min_screenshot_interval_ms"


def test_screenshot_rate_gate_enforces_minimum_interval() -> None:
    enforcer = LimitEnforcer(Limits(min_screenshot_interval_ms=250))
    assert enforcer.can_screenshot() is True  # first screenshot always passes
    enforcer.record_screenshot()
    assert enforcer.can_screenshot() is False  # too soon
    with pytest.raises(LimitExceeded) as excinfo:
        enforcer.check_screenshot()
    assert excinfo.value.limit_name == "min_screenshot_interval_ms"


# --- context growth cap ---------------------------------------------------------------------------------
# REMOVED (run_goal removal): the conversation-context growth cap (max_context_items
# tripping at the third decide) was loop machinery — the direct path keeps no
# conversation context. The ContextManager's own bounded-window behavior stays
# pinned by test_context_manager.py; TaskState history bounds by
# test_task_state_histories_are_bounded below.


def test_task_state_histories_are_bounded() -> None:
    task = TaskState()
    for index in range(120):
        task.add_plan_note(f"note {index}")
        task.record_observation_id(f"obs-{index}")
    assert len(task.plan_notes) == 50
    assert len(task.observation_history) == 50
    assert task.plan_notes[-1] == "note 119"  # newest entries retained, oldest dropped


# --- concurrent session cap --------------------------------------------------------------------------------


def test_concurrent_session_cap_fail_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=2))
    first = server.start_session(dry_run=True)
    second = server.start_session(dry_run=True)
    assert "session_id" in first
    assert "session_id" in second

    third = server.start_session(dry_run=True)
    assert third["ok"] is False
    assert third["error"] == "session_limit_exceeded"
    assert third["max_sessions"] == 2
    assert len(server._registry) == 2  # fail closed: no live session was evicted
