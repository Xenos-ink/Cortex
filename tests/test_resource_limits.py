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
    ScriptedBackend,
    ScriptedProvider,
    audit_events,
    executed_summary,
    make_session,
)
from test_controller_integration import (
    click as make_click,
)

from computer_use_mcp import server
from computer_use_mcp.limits import LimitEnforcer, LimitExceeded, Limits
from computer_use_mcp.models import ActionType, AgentDecision, GroundedAction
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


class ReblockingBackend(ScriptedBackend):
    """Blocking UI that unblocks for Escape but re-blocks before every click lands.

    Drives BLOCKED_UI dismiss-then-retry cycles against one action instance until the
    per-action recovery budget (2) is exhausted -> TERMINATE_SAFELY, never an infinite loop.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.set_input_blocked(True)
        self.execute_hooks.append(self._toggle_block)

    def _toggle_block(self, action: GroundedAction) -> None:
        if action.action == ActionType.KEYPRESS and any(k.lower() == "esc" for k in action.keys):
            self.set_input_blocked(False)  # the dismiss gets through...
        elif action.action in {ActionType.CLICK, ActionType.DOUBLE_CLICK}:
            self.set_input_blocked(True)  # ...but the click is blocked again


# --- action / model-call / duration limits -------------------------------------------------------


async def test_max_actions_limit_trips_cleanly(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider(
        [make_click(10, 10), make_click(20, 20), make_click(30, 30), AgentDecision(status="done")]
    )
    session_id, bundle, backend, _ = make_session(
        monkeypatch, provider=provider, dry_run=False, require_approval=False,
        limits={**FAST_LIMITS, "max_actions": 1},
    )
    response = await server.run_goal(session_id, "one action only")

    assert response["ok"] is False
    assert response["termination_reason"] == "limit_exceeded"
    assert len(executed_summary(backend)) == 1  # the first action ran, nothing further
    assert "limit" in response["results"][-1]["message"].lower()
    events = limit_events(bundle, session_id)
    assert events and events[-1]["metadata"]["limit"] == "max_actions"


async def test_max_model_calls_limit_trips_cleanly(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider(
        [make_click(10, 10), make_click(20, 20), make_click(30, 30), AgentDecision(status="done")]
    )
    session_id, bundle, backend, _ = make_session(
        monkeypatch, provider=provider, dry_run=False, require_approval=False,
        limits={**FAST_LIMITS, "max_model_calls": 2},
    )
    response = await server.run_goal(session_id, "chatty model")

    assert response["termination_reason"] == "limit_exceeded"
    assert bundle.agent.task.model_call_count == 2
    assert len(executed_summary(backend)) == 2
    events = limit_events(bundle, session_id)
    assert events and events[-1]["metadata"]["limit"] == "max_model_calls"


async def test_max_task_seconds_limit_trips_cleanly(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # validate() clamps the floor to 1.0s, so the tiny limit is injected by backdating.
    assert Limits(max_task_seconds=0.01).validate().max_task_seconds == 1.0
    provider = ScriptedProvider([make_click(10, 10), AgentDecision(status="done")])
    session_id, bundle, backend, _ = make_session(
        monkeypatch, provider=provider, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    bundle.enforcer._started_monotonic -= 10_000.0  # task "started" 10000s ago
    response = await server.run_goal(session_id, "slow task")

    assert response["termination_reason"] == "limit_exceeded"
    assert response["ok"] is False
    assert executed_summary(backend) == []  # tripped at the loop top, before any action
    events = limit_events(bundle, session_id)
    assert events and events[-1]["metadata"]["limit"] == "max_task_seconds"


# --- retry / recovery budgets -----------------------------------------------------------------------


async def test_max_retries_per_action_zero_blocks_same_instance_retry(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Identical screenshots with no stated expectation -> uncertain verification ->
    # LOW_CONFIDENCE -> RETRY_ONCE plan -> the zero retry budget refuses it (fail closed).
    provider = ScriptedProvider([make_click(10, 10), AgentDecision(status="done")])
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=ScriptedBackend(flip=False), provider=provider, dry_run=False,
        require_approval=False, limits={**FAST_LIMITS, "max_retries_per_action": 0},
    )
    response = await server.run_goal(session_id, "no retries allowed")

    assert response["termination_reason"] == "limit_exceeded"
    assert len(executed_summary(backend)) == 1  # the original action ran once, no retry
    events = limit_events(bundle, session_id)
    assert events and events[-1]["metadata"]["limit"] == "max_retries_per_action"


async def test_per_action_recovery_budget_exhaustion_terminates_safely(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ReblockingBackend()
    provider = ScriptedProvider(
        [make_click(70, 90, expected_change="dialog handled"), AgentDecision(status="done")]
    )
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, provider=provider, dry_run=False, require_approval=False,
        limits=FAST_LIMITS,
    )
    response = await server.run_goal(session_id, "click through a re-blocking dialog")

    assert response["ok"] is False
    assert response["termination_reason"] == "unrecoverable"  # TERMINATE_SAFELY, no loop
    # Only the two Escape dismisses executed; the blocked click never landed.
    assert executed_summary(backend) == [("keypress", None, None), ("keypress", None, None)]
    counters = bundle.metrics.snapshot()["counters"]
    assert counters["recovery_total"] == 2  # exactly the per-action budget
    assert "Recovery budget exhausted" in response["results"][-1]["message"]
    events = audit_events(bundle, session_id)
    recovery_results = [event["result"] for event in events if event["event_type"] == "recovery"]
    assert recovery_results.count("recover_dismiss") == 2
    assert "terminate_safely" in recovery_results


async def test_per_task_recovery_budget_exhaustion_terminates_safely(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([make_click(10, 10, expected_change="screen must change")])
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=ScriptedBackend(flip=False), provider=provider, dry_run=False,
        require_approval=False, limits=FAST_LIMITS,  # default budgets: 2/action, 6/task
    )
    response = await server.run_goal(session_id, "verification can never succeed")

    assert response["ok"] is False
    assert response["termination_reason"] == "failed_verification"  # verify-phase exhaustion
    assert len(executed_summary(backend)) == 7  # 7 attempts, then bounded termination
    counters = bundle.metrics.snapshot()["counters"]
    assert counters["recovery_total"] == 6  # exactly the per-task budget
    assert "Recovery budget exhausted" in response["results"][-1]["message"]
    events = audit_events(bundle, session_id)
    recovery_results = [event["result"] for event in events if event["event_type"] == "recovery"]
    assert recovery_results.count("recover_reobserve") == 6
    assert "terminate_safely" in recovery_results


# --- screenshot rate gate -----------------------------------------------------------------------------


async def test_screenshot_rate_gate_trips_after_controller_wait_ceiling(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PERF-004 C2 refined gate semantics: fresh observations stay protected.

    The in-loop action cycle is gate-free under observe reuse (C1) + intra-step burst
    exemption (a multi-step task COMPLETES under a 60s interval — previously it tripped
    at the second capture). The gate still binds fail-closed on host-driven observes:
    the ``computer_execute`` direct_request capture arrives within the interval, waits
    past the 2s controller ceiling, and terminates with an audited ``limit_exceeded``.
    """
    provider = ScriptedProvider([make_click(10, 10), AgentDecision(status="done")])
    session_id, bundle, backend, _ = make_session(
        monkeypatch, provider=provider, dry_run=False, require_approval=False,
        limits={"min_screenshot_interval_ms": 60_000},
    )
    response = await server.run_goal(session_id, "rate limited")

    # The in-loop cycle paid NO gate wait: one action executed, the task completed.
    assert response["termination_reason"] == "completed"
    assert len(executed_summary(backend)) == 1
    counters = bundle.metrics.snapshot()["counters"]
    # Step 1: loop_top + validate probe + post_action; step 2: reuse (no capture).
    assert counters["screenshot_count"] == 3
    # Intra-step burst-exempt captures: exactly the validate probe + post_action.
    assert bundle.enforcer.snapshot()["burst_screenshots"] == 2

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


async def test_context_growth_cap_trips_fail_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider(
        [
            make_click(10, 10),
            make_click(20, 20),
            make_click(30, 30),
            make_click(40, 40),
            AgentDecision(status="done"),
        ]
    )
    session_id, bundle, backend, _ = make_session(
        monkeypatch, provider=provider, dry_run=False, require_approval=False,
        limits={**FAST_LIMITS, "max_context_items": 2},
    )
    response = await server.run_goal(session_id, "verbose history")

    assert response["termination_reason"] == "limit_exceeded"
    assert bundle.agent.task.model_call_count == 2  # the third decide was refused
    assert len(executed_summary(backend)) == 2
    events = limit_events(bundle, session_id)
    assert events and events[-1]["metadata"]["limit"] == "max_context_items"


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
