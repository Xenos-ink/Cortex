"""Wave 4 audit-compliance tests: schema conformance, redaction at the sink, metrics sanity.

Scenarios are driven through the MCP tool surface (``start_session`` /
``stop_session``) with fake backends and scripted providers, then the JSONL audit files
are validated directly:

- every event type enumerated in ``audit.AuditEventType`` is emitted at least once across
  the scripted scenarios (Goal.md section 17 coverage);
- every event carries the required field set (timestamp, session_id, event_type, task_id,
  and observation/action ids where applicable);
- every JSONL line parses and each per-session file contains only its own session;
- redaction is ENFORCED at the sink: secret-like metadata and goals never reach disk raw;
- the metrics snapshot stays internally consistent after scripted runs (Goal.md section 18).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, get_args

import pytest
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    audit_events,
    audit_types,
    execute_payload,
    make_session,
)

from computer_use_mcp import server
from computer_use_mcp.security.audit import AuditEventType, AuditLogger, Metrics
from computer_use_mcp.backend import DisplayUnavailableError
from computer_use_mcp.core.limits import LimitExceeded
from computer_use_mcp.core.models import WindowInfo
from computer_use_mcp.core.state import SessionRegistry


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


def _payload(result: Any) -> dict[str, Any]:
    """Unwrap an executed computer_execute content-block response to its dict payload."""
    if isinstance(result, list):
        return json.loads(result[0].text)
    return result


def wire_plan(plan):
    """WP8 (ported from dev): a submitted plan states its policies — a COMPLETE
    plan, so the plan lifecycle events under assertion are the product's own."""
    from plan_explicitness import complete_plan

    return complete_plan(plan)

async def _drive_audit_scenarios(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, Any]]:
    """Scripted sessions (RETARGETED to the five-tool surface, loop removal)
    that together cover every audit event type the direct path can emit."""
    scenarios: list[tuple[str, Any]] = []
    # 1. Happy path with approval: session_start/observation/grounding/validation/
    #    safety/approval-denial/execution/verification (the approval EVENT fires on
    #    the denial shape; an approved=True call executes without the approval phase).
    sid, bundle, _, _ = make_session(
        monkeypatch, dry_run=False, require_approval=True, limits=FAST_LIMITS
    )
    denied = await server.computer_execute(sid, "click", x=100, y=100)  # approval_required
    assert denied.get("requires_approval") is True, denied
    approved = await server.computer_execute(sid, "click", x=100, y=100, approved=True)
    _ = _payload(approved)
    scenarios.append((sid, bundle))

    # 2. Rejection path: out-of-bounds coordinates -> audited failed grounding +
    #    rejected validation (the "recovery" event family was loop-only; the direct
    #    path's refusal audits are the surviving coverage for refusal evidence).
    sid, bundle, _, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    rejected = await server.computer_execute(sid, "click", x=8000, y=10)
    assert rejected.get("ok") is False, rejected
    scenarios.append((sid, bundle))

    # 3. Failure path: observe fault -> failure event (typed action_error).
    backend = ScriptedBackend()
    backend.observe_faults = [DisplayUnavailableError("screen capture failed")]
    sid, bundle, _, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    failed = await server.computer_execute(sid, "click", x=40, y=50)
    assert failed.get("error") == "action_error", failed
    scenarios.append((sid, bundle))

    # 4. Limit exceeded: limit_exceeded event.
    sid, bundle, _, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False,
        limits={**FAST_LIMITS, "max_actions": 1},
    )
    first = await server.computer_execute(sid, "click", x=1, y=1)
    _ = _payload(first)
    limited = await server.computer_execute(sid, "click", x=2, y=2)
    # §1.4 delta (ORVEX-2026-108): a limit trip is a pre-dispatch gate refusal —
    # rejection envelope, typed limit as the first reason, no error key.
    assert limited.get("result") == "rejected", limited
    assert limited.get("reasons") == ["limit_exceeded: max_actions"], limited
    scenarios.append((sid, bundle))

    # 5. Stop: stop + emergency_stop events (stop armed, then a tool call refused).
    sid, bundle, _, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await server.stop_session(sid)
    stopped = await server.computer_execute(sid, "wait", delta=1)
    # §1.4 delta (ORVEX-2026-108): execute-surface stopped refusal = rejection
    # envelope; the stop itself is what this scenario's audit assertions read below.
    assert stopped.get("result") == "rejected", stopped
    assert stopped.get("reasons") == ["session_stopped"], stopped
    scenarios.append((sid, bundle))

    # 6. PERF-004 queued host actions: the per-queue summary ("queue") event; every
    #    item also emits its own grounding/validation/safety/execution/verification.
    sid, bundle, _, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await server.computer_execute(
        sid,
        "click",
        x=10,
        y=10,
        follow_ups=[{"action": "click", "x": 20, "y": 20}],
    )
    scenarios.append((sid, bundle))

    # 7. WP8 plan lifecycle: successful start/point/complete vocabulary. The point
    #    is a CROSSING point (``"actions": []``) — it declares its actionlessness,
    #    its move verifies, and the SAME call ends the walk: plan_start +
    #    plan_point + plan_complete in one call.
    sid, bundle, _, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    completed_plan = await server.computer_execute(
        sid,
        "done",
        approved=True,
        plan=wire_plan(
            {
                "transition_duration_ms": 0,
                "points": [{"x": 10, "y": 10, "actions": []}],
            }
        ),
    )
    assert _payload(completed_plan)["plan"]["status"] == "completed"
    scenarios.append((sid, bundle))

    # 8. WP8 stopped terminal: start + point + declared limit stop, never complete.
    sid, bundle, _, _ = make_session(
        monkeypatch,
        dry_run=False,
        require_approval=False,
        limits={**FAST_LIMITS, "max_actions": 1},
    )
    stopped_plan = _payload(
        await server.computer_execute(
            sid,
            "done",
            approved=True,
            plan=wire_plan(
                {
                    "transition_duration_ms": 0,
                    "points": [{"x": 10, "y": 10, "actions": [{"action": "click"}]}],
                }
            ),
        )
    )
    # stable adaptation: with max_actions=1 the single slot is consumed by the
    # transition move and the click is refused; the step PARKS (never completes),
    # and the host stop below is what closes the lifecycle as stopped.
    assert stopped_plan["result"] != "verified"
    assert stopped_plan["blocked"] is True
    await server.computer_execute(sid, "done", plan_action="stop")
    scenarios.append((sid, bundle))
    return scenarios


async def test_audit_event_types_fields_and_jsonl_conformance(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenarios = await _drive_audit_scenarios(fresh_server, monkeypatch)
    all_events: list[dict[str, Any]] = []
    for sid, bundle in scenarios:
        events = audit_events(bundle, sid)
        assert events  # the session produced a non-empty audit trail
        assert {event["session_id"] for event in events} == {sid}
        all_events.extend(events)

    expected = set(get_args(AuditEventType))
    # Additive master-mission 003 update: the long-running event types below are emitted
    # by the orchestration layer (long_running.py) and are exercised by the dedicated
    # long-running/resume test modules, not by this scenario drive.
    _LONG_RUNNING_EVENT_TYPES = {
        "subtask_created",
        "subtask_started",
        "subtask_completed",
        "subtask_failed",
        "subtask_paused",
        "replan",
        "checkpoint",
        "resume",
        "approval_epoch",
        "health_check",
    }
    expected -= _LONG_RUNNING_EVENT_TYPES
    # loop removal: the loop-only event types below were emitted exclusively by the
    # internal decide/decide-phase machinery; with the loop gone, no tool path can emit
    # them. Every SURVIVING event type remains fully required here.
    _LOOP_ONLY_EVENT_TYPES = {
        "model_decision",
        "recovery",
        "session_stop",
    }
    expected -= _LOOP_ONLY_EVENT_TYPES
    missing = expected - audit_types(all_events)
    assert not missing, f"audit event types never emitted: {sorted(missing)}"

    for event in all_events:
        assert isinstance(datetime.fromisoformat(event["timestamp"]), datetime)
        assert event["session_id"]
        assert event["event_type"] in expected
        assert event["task_id"] is not None
        if event["event_type"] == "observation":
            assert event["observation_id"]
        if event["event_type"] == "execution":
            assert event["action_id"]

    # Raw JSONL: every line parses independently and the file layout is per-session.
    for sid, bundle in scenarios:
        path = bundle.auditor.path_for(sid)
        assert path.exists()
        assert path.name == f"audit_{sid}.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            assert isinstance(json.loads(line), dict)


async def test_plan_lifecycle_audit_events_agree_with_the_replies(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP8 (E-05 rule, ported from dev scenarios 7/8): the plan lifecycle audit
    record and the reply verdict can never disagree — each event is emitted AFTER
    the reply's verdict fields are decided and its metadata is READ OFF the reply.

    The drive: a two-point click plan whose steps honestly abstain (pixel-only
    backend) parks the lease; the host stop then closes the lifecycle."""
    backend = ScriptedBackend(flip=True)
    sid, bundle, _, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    first = execute_payload(
        await server.computer_execute(
            sid,
            "done",
            plan=wire_plan(
                {
                    "transition_duration_ms": 0,
                    "points": [
                        {"x": 10, "y": 10, "actions": [{"action": "click"}]},
                        {"x": 30, "y": 40, "actions": [{"action": "click"}]},
                    ],
                }
            ),
            approved=True,
            include_screenshot_after=False,
        )
    )
    assert first["blocked"] is True and first["result"] == "uncertain"
    events = [
        event
        for event in audit_events(bundle, sid)
        if event["event_type"].startswith("plan_")
    ]
    assert [event["event_type"] for event in events] == ["plan_start", "plan_point"], events
    start, point = events
    assert start["result"] == "started"
    assert start["metadata"]["plan_id"] == first["plan_id"]
    assert start["metadata"]["point_count"] == 2
    assert point["result"] == first["result"]
    assert point["metadata"]["failure_reason"] == first["failure_reason"]
    assert point["metadata"]["step_index"] == first["step_index"]

    stop = execute_payload(
        await server.computer_execute(sid, "done", plan_action="stop", include_screenshot_after=False)
    )
    assert stop["result"] == "stopped" and stop["failure_reason"] is None
    events = [
        event
        for event in audit_events(bundle, sid)
        if event["event_type"].startswith("plan_")
    ]
    assert [event["event_type"] for event in events][-1] == "plan_stopped"
    stopped_event = events[-1]
    assert stopped_event["result"] == "stopped"
    assert stopped_event["metadata"]["reason"] == "stop"
    assert stopped_event["metadata"]["plan_id"] == first["plan_id"]
    # F4: the counts come from the ONE helper over the same prefix the reply built.
    assert stopped_event["metadata"]["resolved_count"] == len(stop["executed_prefix"])
    assert stopped_event["metadata"]["executed_count"] == sum(
        entry.get("kind") in ("executed", "dispatched_unverified")
        for entry in stop["executed_prefix"]
    )


def _plan_events(bundle: Any, sid: str) -> list[dict[str, Any]]:
    return [
        event
        for event in audit_events(bundle, sid)
        if event["event_type"].startswith("plan_")
    ]


def _assert_agrees(event: dict[str, Any], reply: dict[str, Any]) -> None:
    """F4/F5 (VAL-WP8-01): the audit record equals the reply it accompanies.

    ``failure_reason``: the shared ``agent._audit`` sink drops None metadata
    values, so an explicit null (a verified step) is recorded as the key's
    ABSENCE — absent ≡ null for plan events (documented in wp8-notes).
    """
    assert event["result"] == reply["result"], (event, reply.get("result"))
    assert event["metadata"].get("failure_reason") == reply.get("failure_reason")
    assert event["metadata"]["plan_id"] == reply["plan_id"]
    if "step_index" in event["metadata"]:
        assert event["metadata"]["step_index"] == reply["step_index"]
    assert event["metadata"]["resolved_count"] == len(reply["executed_prefix"])


# --- F5 (VAL-WP8-01): every plan path's audit record agrees with its reply -----

_CROSSING_TWO: dict[str, Any] = {
    "transition_duration_ms": 0,
    "points": [
        {"x": 10, "y": 10, "actions": []},
        {"x": 30, "y": 40, "actions": []},
    ],
}

_STAT_TWO: dict[str, Any] = {
    "transition_duration_ms": 0,
    "points": [
        {"x": 10, "y": 10, "actions": [{"action": "click"}]},
        {"x": 30, "y": 40, "actions": [{"action": "click"}]},
    ],
}


class _CancelAfterDispatch(ScriptedBackend):
    def execute(self, action: Any, stop: Any = None, **kwargs: Any) -> str:
        message = super().execute(action, stop)
        from computer_use_mcp.core.state import TaskStopped

        raise TaskStopped("synthetic stop immediately after dispatch")


class _BrokenBackend(ScriptedBackend):
    def execute(self, action: Any, stop: Any = None, **kwargs: Any) -> str:
        raise RuntimeError("synthetic backend explosion")


async def test_verified_walk_audit_agrees_with_the_walk_ending_and_terminal_replies(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk-ending step (plan_complete) and the parked step (plan_point)
    records carry the replies' own result tokens; a verified plan_point records
    failure_reason as ABSENT (= null); exactly one plan_start and one terminal."""
    backend = ScriptedBackend(flip=True)
    sid, bundle, _, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    step0 = execute_payload(
        await server.computer_execute(
            sid, "done", plan=wire_plan(_CROSSING_TWO), approved=True, include_screenshot_after=False
        )
    )
    assert step0["result"] == "verified" and step0["blocked"] is True
    events = _plan_events(bundle, sid)
    assert [e["event_type"] for e in events] == ["plan_start", "plan_point"]
    _assert_agrees(events[1], step0)
    assert "failure_reason" not in events[1]["metadata"]  # explicit null ≡ absent

    final = execute_payload(
        await server.computer_execute(
            sid, "done", plan_action="continue", include_screenshot_after=False
        )
    )
    assert final["result"] == "verified" and final["blocked"] is False
    events = _plan_events(bundle, sid)
    assert [e["event_type"] for e in events] == [
        "plan_start", "plan_point", "plan_point", "plan_complete",
    ]
    _assert_agrees(events[2], final)  # the walk-ending step's own plan_point
    terminal = events[3]
    assert terminal["result"] == "verified"
    assert terminal["metadata"]["reason"] == "completed"
    _assert_agrees(terminal, final)
    # exactly ONE plan_start and ONE terminal event for the whole plan
    assert sum(e["event_type"] == "plan_start" for e in events) == 1
    assert sum(e["event_type"] in ("plan_complete", "plan_stopped") for e in events) == 1


async def test_replace_audit_sequence_agrees_with_the_replies(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replace: plan_stopped (result stopped, reason replaced) for the old plan,
    then a fresh plan_start for the replacement — one terminal per plan."""
    backend = ScriptedBackend(flip=True)
    sid, bundle, _, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    first = execute_payload(
        await server.computer_execute(
            sid, "done", plan=wire_plan(_STAT_TWO), approved=True, include_screenshot_after=False
        )
    )
    assert first["blocked"] is True
    replaced = execute_payload(
        await server.computer_execute(
            sid, "done", plan=wire_plan(_CROSSING_TWO), plan_action="replace",
            include_screenshot_after=False,
        )
    )
    assert replaced["replaced_plan_id"] == first["plan_id"]
    events = _plan_events(bundle, sid)
    old_events = [e for e in events if e["metadata"]["plan_id"] == first["plan_id"]]
    new_events = [e for e in events if e["metadata"]["plan_id"] == replaced["plan_id"]]
    assert [e["event_type"] for e in old_events] == [
        "plan_start", "plan_point", "plan_stopped",
    ]
    assert old_events[-1]["result"] == "stopped"
    assert old_events[-1]["metadata"]["reason"] == "replaced"
    assert [e["event_type"] for e in new_events] == ["plan_start", "plan_point"]
    assert new_events[0]["metadata"]["replaced_plan_id"] == first["plan_id"]
    _assert_agrees(new_events[1], replaced)
    # one terminal per plan: the old plan's, and none yet for the replacement
    assert sum(
        e["event_type"] in ("plan_complete", "plan_stopped") for e in old_events
    ) == 1
    assert not [e for e in new_events if e["event_type"].startswith(("plan_complete", "plan_stopped"))]


async def test_task_stopped_and_error_terminals_agree_exactly_once(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TaskStopped: one plan_stopped, result stopped, reason cancelled (the
    reply's own failure_reason). Error kind: result failed, reason
    execution_error. Each plan terminates EXACTLY once."""
    backend = _CancelAfterDispatch(flip=True)
    sid, bundle, _, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    reply = execute_payload(
        await server.computer_execute(
            sid, "done", plan=wire_plan(_STAT_TWO), approved=True, include_screenshot_after=False
        )
    )
    assert reply["result"] == "stopped" and reply["failure_reason"] == "cancelled"
    events = _plan_events(bundle, sid)
    # N4: the killed step emits its own plan_point BEFORE the terminal record
    assert [e["event_type"] for e in events] == ["plan_start", "plan_point", "plan_stopped"]
    _assert_agrees(events[1], reply)
    _assert_agrees(events[2], reply)
    assert events[2]["metadata"]["reason"] == "cancelled"

    backend = _BrokenBackend(flip=True)
    sid, bundle, _, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    reply = execute_payload(
        await server.computer_execute(
            sid, "done", plan=wire_plan(_STAT_TWO), approved=True, include_screenshot_after=False
        )
    )
    assert reply["result"] == "failed" and reply["failure_reason"] == "execution_error"
    events = _plan_events(bundle, sid)
    assert [e["event_type"] for e in events] == ["plan_start", "plan_stopped"]
    _assert_agrees(events[1], reply)
    assert events[1]["metadata"]["reason"] == "execution_error"


async def test_limit_cut_parks_and_the_records_agree(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A limit cut parks the lease: the plan_point record carries the cut reply's
    failed/limit_exceeded verdict; the host stop then terminates once. The cut
    lands on the SECOND step's dispatch (the budget is spent after step 0), the
    same shape batch6 pins."""
    backend = ScriptedBackend(flip=True)
    sid, bundle, _, _ = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=False,
        limits={**FAST_LIMITS, "max_actions": 2},
    )
    await server.computer_execute(
        sid, "done", plan=wire_plan(_STAT_TWO), approved=True, include_screenshot_after=False
    )
    cut = execute_payload(
        await server.computer_execute(
            sid, "done", plan_action="continue", include_screenshot_after=False
        )
    )
    assert cut["error"] == "limit_exceeded" and cut["failure_reason"] == "limit_exceeded"
    assert cut["limit"] == "max_actions" and "cutoff" in cut
    events = _plan_events(bundle, sid)
    assert [e["event_type"] for e in events] == [
        "plan_start", "plan_point", "plan_point",
    ]
    _assert_agrees(events[2], cut)
    stop = execute_payload(
        await server.computer_execute(sid, "done", plan_action="stop", include_screenshot_after=False)
    )
    events = _plan_events(bundle, sid)
    assert events[-1]["event_type"] == "plan_stopped"
    _assert_agrees(events[-1], stop)
    assert sum(
        e["event_type"] in ("plan_complete", "plan_stopped") for e in events
    ) == 1


class _LimitCutThenKill(ScriptedBackend):
    """R1/R2 (VAL-WP8-03): the session kill lands while a step that will end in
    a LimitExceeded cut is in flight — the funnel DEFERS, the flag clears after
    the merge, and the lifecycle still closes EXACTLY once."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.kill_started = False

    def execute(self, action: Any, stop: Any = None, **kwargs: Any) -> str:
        if not self.kill_started:
            import asyncio as _asyncio
            import threading as _threading

            from computer_use_mcp import server as _server

            self.kill_started = True
            session_ids = list(_server._bundles.keys())
            assert session_ids, "no live session to stop"
            thread = _threading.Thread(
                target=lambda: _asyncio.run(_server.stop_session(session_ids[0]))
            )
            thread.start()
            thread.join()  # the funnel already ran (deferred) BEFORE the cut
        message = super().execute(action, stop)
        raise LimitExceeded("max_actions", "synthetic cut after the kill")


async def test_limit_cut_then_stop_session_emits_exactly_one_terminal(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 (VAL-WP8-03): a plan parked after a limit cut, then stop_session-killed,
    emits EXACTLY ONE terminal event with the final reply's counts — the stale
    step_in_flight flag (DELTA 2 regression) would have emitted ZERO."""
    backend = ScriptedBackend(flip=True)
    sid, bundle, _, _ = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=False,
        limits={**FAST_LIMITS, "max_actions": 2},
    )
    await server.computer_execute(
        sid, "done", plan=wire_plan(_STAT_TWO), approved=True, include_screenshot_after=False
    )
    cut = execute_payload(
        await server.computer_execute(
            sid, "done", plan_action="continue", include_screenshot_after=False
        )
    )
    assert cut["failure_reason"] == "limit_exceeded" and cut["blocked"] is True
    # the LimitExceeded handler cleared the in-flight marker (R1 invariant)
    lease = server._plan_guard.active_lease(bundle)
    assert lease is not None and lease.step_in_flight is False
    import asyncio as _asyncio

    await _asyncio.wait_for(server.stop_session(sid), 5)
    terminals = [
        event for event in _plan_events(bundle, sid)
        if event["event_type"] in ("plan_complete", "plan_stopped")
    ]
    assert len(terminals) == 1, terminals
    assert terminals[0]["event_type"] == "plan_stopped"
    assert terminals[0]["result"] == "stopped"
    assert terminals[0]["metadata"]["reason"] == "cancelled"
    assert terminals[0]["metadata"]["resolved_count"] == len(cut["executed_prefix"])
    assert terminals[0]["metadata"]["executed_count"] == sum(
        entry.get("kind") in ("executed", "dispatched_unverified")
        for entry in cut["executed_prefix"]
    )


async def test_the_funnel_defer_decision_is_atomic_with_the_step_finally(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 5 (Commander review): the funnel's defer DECISION happens INSIDE the
    terminal-audit lock. The interleaving that used to lose the terminal — the
    funnel reads step_in_flight True, the step's finally clears it, the funnel
    then sets kill_deferred on a FINISHED step — is forced deterministically by
    letting the step's finally complete inside the funnel's decision window:
    with the fix the funnel reads the CLEARED flag and emits its own terminal,
    so exactly ONE plan_stopped exists."""
    import asyncio as _asyncio
    import threading as _threading

    backend = ScriptedBackend(flip=True)
    sid, bundle, _, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await server.computer_execute(
        sid, "done", plan=wire_plan(_STAT_TWO), approved=True, include_screenshot_after=False
    )
    lease = server._plan_guard.active_lease(bundle)
    assert lease is not None
    # simulate the in-flight step: the funnel must consult the flag under the lock
    lease.step_in_flight = True

    real_lock = server._plan_terminal_audit_lock

    class _StepFinishesInsideTheFunnelDecision:
        """A lock whose acquisition completes the step's lifecycle FIRST — the
        exact interleaving the pre-DELTA-5 check-then-act lost."""

        def __enter__(self) -> Any:
            real_lock.acquire()
            # the step's finally, racing the funnel's decision:
            lease.step_in_flight = False
            return self

        def __exit__(self, *exc_info: Any) -> bool:
            real_lock.release()
            return False

    monkeypatch.setattr(server, "_plan_terminal_audit_lock", _StepFinishesInsideTheFunnelDecision())
    await _asyncio.wait_for(server.stop_session(sid), 5)
    terminals = [
        event for event in _plan_events(bundle, sid)
        if event["event_type"] in ("plan_complete", "plan_stopped")
    ]
    assert len(terminals) == 1, terminals
    assert terminals[0]["event_type"] == "plan_stopped"
    assert terminals[0]["result"] == "stopped"
    assert terminals[0]["metadata"]["reason"] == "cancelled"
    assert lease.step_in_flight is False
    assert lease.kill_deferred is False


async def test_limit_cut_then_idle_reap_emits_exactly_one_terminal(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 (VAL-WP8-03): the same invariant through the WP-S idle reaper."""
    backend = ScriptedBackend(flip=True)
    sid, bundle, _, _ = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=False,
        limits={**FAST_LIMITS, "max_actions": 2},
    )
    await server.computer_execute(
        sid, "done", plan=wire_plan(_STAT_TWO), approved=True, include_screenshot_after=False
    )
    cut = execute_payload(
        await server.computer_execute(
            sid, "done", plan_action="continue", include_screenshot_after=False
        )
    )
    assert cut["failure_reason"] == "limit_exceeded"
    monkeypatch.setattr(server, "_session_idle_timeout", lambda: 0.05)
    import asyncio as _asyncio

    await _asyncio.sleep(0.08)
    assert sid in server._sweep_idle_sessions()
    terminals = [
        event for event in _plan_events(bundle, sid)
        if event["event_type"] in ("plan_complete", "plan_stopped")
    ]
    assert len(terminals) == 1, terminals
    assert terminals[0]["metadata"]["reason"] == "cancelled"
    assert terminals[0]["metadata"]["source"] == "idle_timeout"
    assert terminals[0]["metadata"]["resolved_count"] == len(cut["executed_prefix"])


async def _kill_lands_mid_step(
    bundle: Any, sid: str
) -> None:
    """The kill lands WHILE the step is in flight: stop_session runs to
    completion on a side thread — the funnel finds ``step_in_flight`` set and
    records ``kill_deferred`` (no terminal of its own)."""
    import asyncio as _asyncio
    import threading as _threading

    done = _threading.Event()

    def _kill() -> None:
        _asyncio.run(server.stop_session(sid))
        done.set()

    thread = _threading.Thread(target=_kill)
    thread.start()
    thread.join()
    assert done.is_set()
    assert bundle is not None


async def _assert_one_terminal_after_kill(
    bundle: Any,
    sid: str,
    lease: Any,
    reply: dict[str, Any] | None,
    *,
    result: str = "stopped",
    reason: str = "cancelled",
) -> None:
    """Sequence plan_start → plan_point → plan_stopped, ONE terminal, counts
    equal to the (reply's) final prefix, step_in_flight cleared (N-B)."""
    assert lease.step_in_flight is False
    events = _plan_events(bundle, sid)
    terminals = [
        event for event in events
        if event["event_type"] in ("plan_complete", "plan_stopped")
    ]
    assert len(terminals) == 1, events
    assert terminals[0]["event_type"] == "plan_stopped"
    assert terminals[0]["result"] == result
    assert terminals[0]["metadata"]["reason"] == reason
    point_events = [event for event in events if event["event_type"] == "plan_point"]
    assert point_events, events
    assert events.index(point_events[-1]) < events.index(terminals[0]), events
    if reply is not None:
        assert terminals[0]["metadata"]["resolved_count"] == len(reply["executed_prefix"])


# DELTA 4 (VAL-WP8-04): the kill matrix — run_single itself is driven, not
# backend.execute wrapping. Parametrized over how the step ends after the kill
# landed mid-flight (funnel deferred).


@pytest.mark.parametrize(
    ("case", "expected_reply_result"),
    [
        ("limit", "failed"),  # N-A: LimitExceeded propagating out of run_single
        ("task", "stopped"),
        ("base", None),  # no reply: the exception reaches the caller
        ("normal", "stopped"),  # DELTA 1 #5: the killed step reply says stopped
        ("merge", None),  # N-B (VAL-WP8-05): the MERGE itself raises post-kill
    ],
)
async def test_kill_then_step_exit_always_closes_the_lifecycle(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, case: str, expected_reply_result: str | None
) -> None:
    """DELTA 4: with the kill DEFERRED by the funnel, every step exit — a
    LimitExceeded out of run_single (N-A), a TaskStopped, a BaseException, and a
    normal return — ends with EXACTLY ONE terminal event, AFTER the plan_point,
    with the final prefix counts and the in-flight marker cleared."""
    from computer_use_mcp.pipeline.agent import SingleActionOutcome
    from computer_use_mcp.core.models import ExecutionResult, GroundedAction
    from computer_use_mcp.core.state import TaskStopped

    backend = ScriptedBackend(flip=True)
    sid, bundle, _, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await server.computer_execute(
        sid, "done", plan=wire_plan(_STAT_TWO), approved=True, include_screenshot_after=False
    )
    lease = server._plan_guard.active_lease(bundle)
    assert lease is not None and lease.step_in_flight is False
    real_run_single = bundle.agent.run_single
    merge_fault = {"v": False}

    async def wrapped_run_single(state: Any, action: Any, **kwargs: Any) -> Any:
        # the kill lands WHILE this step is in flight (the flag is already set)
        await _kill_lands_mid_step(bundle, sid)
        if case == "limit":
            raise LimitExceeded("max_actions", "synthetic cut after the kill")
        if case == "task":
            raise TaskStopped("synthetic kill-then-stop")
        if case == "base":
            raise RuntimeError("synthetic kill-then-base")
        if case == "merge":
            merge_fault["v"] = True
            return await real_run_single(state, action, **kwargs)
        # normal return: a hand-built executed outcome (the armed stop token
        # would make a REAL pipeline run raise TaskStopped — the point of this
        # case is the finally on the SUCCESS path)
        return SingleActionOutcome(
            kind="executed",
            result=ExecutionResult(
                ok=True,
                action=GroundedAction(action="move", point={"x": 30, "y": 40}, confidence=1.0),
                message="synthetic post-kill normal return",
            ),
        )

    monkeypatch.setattr(bundle.agent, "run_single", wrapped_run_single)
    reply: dict[str, Any] | None = None
    if case == "base":
        # the BaseException handler re-raises: no reply reaches the caller
        with pytest.raises(RuntimeError, match="synthetic kill-then-base"):
            await server.computer_execute(
                sid, "done", plan_action="continue", include_screenshot_after=False
            )
    elif case == "merge":
        real_merge = server.plan_layer.merge_plan_step_prefix

        def faulting_merge(lease_arg: Any, entries: Any, **kwargs: Any) -> None:
            if merge_fault["v"]:
                raise ValueError("synthetic merge failure")
            real_merge(lease_arg, entries, **kwargs)

        monkeypatch.setattr(server.plan_layer, "merge_plan_step_prefix", faulting_merge)
        with pytest.raises(ValueError, match="synthetic merge failure"):
            await server.computer_execute(
                sid, "done", plan_action="continue", include_screenshot_after=False
            )
    else:
        reply = execute_payload(
            await server.computer_execute(
                sid, "done", plan_action="continue", include_screenshot_after=False
            )
        )
        assert reply["result"] == expected_reply_result, sorted(reply)
    # N-B: the flag is cleared even when the merge itself raised; exactly one
    # terminal, after the point, with the final counts
    await _assert_one_terminal_after_kill(
        bundle, sid, lease, reply,
        **({"result": "failed", "reason": "exception"} if case == "base" else {}),
    )


async def test_idle_reap_funnel_emits_one_terminal_with_the_kill_source(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The WP-S reaper funnels through the same kill path: exactly ONE terminal
    plan_stopped (result stopped, reason cancelled, source idle_timeout)."""
    backend = ScriptedBackend(flip=True)
    sid, bundle, _, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await server.computer_execute(
        sid, "done", plan=wire_plan(_STAT_TWO), approved=True, include_screenshot_after=False
    )
    assert server._plan_guard.is_active(sid)
    monkeypatch.setattr(server, "_session_idle_timeout", lambda: 0.05)
    import asyncio as _asyncio

    await _asyncio.sleep(0.08)
    assert sid in server._sweep_idle_sessions()
    events = _plan_events(bundle, sid)
    terminals = [e for e in events if e["event_type"] in ("plan_complete", "plan_stopped")]
    assert len(terminals) == 1, events
    assert terminals[0]["event_type"] == "plan_stopped"
    assert terminals[0]["result"] == "stopped"
    assert terminals[0]["metadata"]["reason"] == "cancelled"
    assert terminals[0]["metadata"]["source"] == "idle_timeout"


async def test_a_raising_audit_writer_leaves_the_reply_unchanged(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit failures never break the plan path: a writer that RAISES leaves the
    step reply byte-identical in its verdict fields."""
    backend = ScriptedBackend(flip=True)
    sid, bundle, _, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )

    def _raising_emit(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic audit sink failure")

    # patch the SINK (auditor.emit), not agent._audit — the agent's own _audit
    # wrapper is the production swallow layer under test here.
    monkeypatch.setattr(bundle.auditor, "emit", _raising_emit)
    reply = execute_payload(
        await server.computer_execute(
            sid, "done", plan=wire_plan(_CROSSING_TWO), approved=True, include_screenshot_after=False
        )
    )
    assert reply["result"] == "verified"
    assert reply["failure_reason"] is None
    assert "failure_reason" in reply and "not_executed" in reply


# --- DELTA 2 (VAL-WP8-02): N1 atomicity, N3 non-verified records, F5 gaps -------


async def test_concurrent_terminal_emitters_produce_exactly_one_event(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1 (VAL-WP8-02): two terminal emitters racing on DIFFERENT threads (the
    stop-session thread and the event loop) produce exactly ONE terminal event —
    the check-and-set is atomic under the terminal-audit lock."""
    import threading

    backend = ScriptedBackend(flip=True)
    sid, bundle, _, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await server.computer_execute(
        sid, "done", plan=wire_plan(_STAT_TWO), approved=True, include_screenshot_after=False
    )
    lease = server._plan_guard.active_lease(bundle)
    assert lease is not None
    start = threading.Barrier(2)

    def _emit() -> None:
        start.wait()
        server._audit_plan_terminal(
            bundle, lease, reply_result="stopped", reason="cancelled",
            failure_reason="cancelled",
        )

    threads = [threading.Thread(target=_emit), threading.Thread(target=_emit)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    terminals = [
        event for event in _plan_events(bundle, sid)
        if event["event_type"] in ("plan_complete", "plan_stopped")
    ]
    assert len(terminals) == 1, terminals
    assert terminals[0]["event_type"] == "plan_stopped"
    assert terminals[0]["result"] == "stopped"


async def test_non_verified_walk_ending_and_terminal_walk_records_agree(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F5 gap: the walk-ending (unknown) and terminal-walk (failed) records carry
    the replies' own result tokens — not only the verified shape."""
    # unknown: an uncertain first point caps the walk-ending aggregate
    backend = ScriptedBackend(flip=True)
    sid, bundle, _, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    plan = {
        "transition_duration_ms": 0,
        "points": [
            {"x": 10, "y": 10, "actions": [{"action": "click", "expected_effect": "the click landed"}]},
            {"x": 30, "y": 40, "actions": []},
        ],
    }
    await server.computer_execute(
        sid, "done", plan=wire_plan(plan), approved=True, include_screenshot_after=False
    )
    walk_end = execute_payload(
        await server.computer_execute(
            sid, "done", plan_action="continue", include_screenshot_after=False
        )
    )
    assert walk_end["result"] == "unknown" and walk_end["ok"] is False
    events = _plan_events(bundle, sid)
    # the walk-ending aggregate of an UNKNOWN plan is still a plan_stopped
    # record (plan_complete iff the plan verified) — but it carries the reply's
    # own result token and failure_reason (F5: agreement, not just type)
    assert events[-1]["event_type"] == "plan_stopped"
    _assert_agrees(events[-1], walk_end)
    assert events[-1]["metadata"]["reason"] == "verification_uncertain"

    # failed: stated WINDOW expectations on a no-flip backend are DEFINITIVELY
    # failed (the deterministic tier) -> the terminal walk reads the aggregate
    # over the prefix (result failed)
    backend = ScriptedBackend(flip=False)
    sid, bundle, _, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    failed_plan = {
        "transition_duration_ms": 0,
        "points": [
            {"x": 10, "y": 20, "actions": [{"action": "keypress", "keys": ["enter"], "expected_effect": "open Calculator"}]},
            {"x": 30, "y": 40, "actions": [{"action": "click"}]},
        ],
    }
    step0 = execute_payload(
        await server.computer_execute(
            sid, "done", plan=wire_plan(failed_plan), approved=True, include_screenshot_after=False
        )
    )
    assert step0["result"] in ("failed", "uncertain"), step0.get("result")
    await server.computer_execute(
        sid, "done", plan_action="continue", include_screenshot_after=False
    )
    terminal = execute_payload(
        await server.computer_execute(
            sid, "done", plan_action="continue", include_screenshot_after=False
        )
    )
    # NON-verified terminal walk (whatever honest verdict the prefix earns):
    # the terminal record carries the reply's own token (F5)
    assert terminal["result"] != "verified"
    events = _plan_events(bundle, sid)
    assert events[-1]["event_type"] == "plan_stopped"
    _assert_agrees(events[-1], terminal)
    assert sum(
        event["event_type"] in ("plan_complete", "plan_stopped") for event in events
    ) == 1


async def test_invalid_conversion_and_base_exception_paths_agree(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F5 gap: the structural-rejection (invalid conversion) and BaseException
    terminal records carry their reply's (or envelope's) verdict and counts."""
    from computer_use_mcp.core.models import ActionSpec
    import weakref

    # invalid conversion: each spec converts ONCE at submission; the STEP's own
    # re-conversion is what fails (the structural gate in _execute_plan_step)
    backend = ScriptedBackend(flip=True)
    sid, bundle, _, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    real_to_grounded = ActionSpec.to_grounded
    real_install = server._plan_guard.install
    seen_specs: "list[weakref.ref]" = []
    started = {"v": False}

    def wrapped_install(bundle: Any, lease: Any, **kwargs: Any) -> None:
        started["v"] = True
        real_install(bundle, lease, **kwargs)

    monkeypatch.setattr(server._plan_guard, "install", wrapped_install)

    def exploding_to_grounded(self: Any, **kwargs: Any) -> Any:
        # only the STEP's own re-conversion (after the lease is installed) fails;
        # every submission-time conversion passes (ActionSpec is unhashable, so
        # identity is tracked via weak references)
        already = any(ref() is self for ref in seen_specs)
        if started["v"] and already:
            raise ValueError("synthetic structural conversion failure")
        if not already:
            seen_specs.append(weakref.ref(self))
        return real_to_grounded(self, **kwargs)

    monkeypatch.setattr(ActionSpec, "to_grounded", exploding_to_grounded)
    reply = execute_payload(
        await server.computer_execute(
            sid, "done", plan=wire_plan(_STAT_TWO), approved=True, include_screenshot_after=False
        )
    )
    assert reply["result"] == "failed" and reply["failure_reason"] == "invalid_action"
    events = _plan_events(bundle, sid)
    assert [event["event_type"] for event in events] == ["plan_start", "plan_stopped"]
    _assert_agrees(events[1], reply)
    assert events[1]["metadata"]["reason"] == "invalid_action"
    assert events[1]["metadata"]["resolved_count"] == 0
    # (the BaseException leg lives in its own test below — the conversion
    # fault's monkeypatch persists to the end of this test)


async def test_base_exception_terminal_record_agrees(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F5 gap: the BaseException path — no step reply exists; the terminal
    record names the exception cause and the exception reaches the caller."""
    backend = ScriptedBackend(flip=True)
    sid, bundle, _, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )

    async def exploding_run_single(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("synthetic base exception")

    monkeypatch.setattr(bundle.agent, "run_single", exploding_run_single)
    # the BaseException handler re-raises; the exception reaches the caller (no
    # step reply exists for this step)
    import pytest as _pytest

    with _pytest.raises(RuntimeError, match="synthetic base exception"):
        await server.computer_execute(
            sid, "done", plan=wire_plan(_STAT_TWO), approved=True, include_screenshot_after=False
        )
    events = _plan_events(bundle, sid)
    assert [event["event_type"] for event in events] == ["plan_start", "plan_stopped"]
    assert events[1]["result"] == "failed"
    assert events[1]["metadata"]["reason"] == "exception"
    assert "failure_reason" not in events[1]["metadata"]  # no reply: null ≡ absent


# --- redaction enforced at the sink ------------------------------------------------------------


def test_redaction_enforced_at_sink_for_secret_metadata(tmp_path: Any) -> None:
    logger = AuditLogger(tmp_path / "audit-direct")
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI0MjQyNDI0MiJ9.c2VjcmV0c2VjcmV0c2VjcmV0"
    logger.emit(
        "session_start",
        "sess-redact",
        task_id="task-1",
        result="ok",
        metadata={
            "password": "hunter2secret",
            "note": "authorization: bearer abcdefghijklmnopabcdefghijklmnop",
            "goal": "use api_key=AKIAIOSFODNN7EXAMPLE now",
            "jwt": jwt,
        },
    )
    content = (tmp_path / "audit-direct" / "audit_sess-redact.jsonl").read_text(encoding="utf-8")

    assert "hunter2secret" not in content  # sensitive metadata key redacted wholesale
    assert "abcdefghijklmnop" not in content  # bearer token redacted
    assert "AKIAIOSFODNN7EXAMPLE" not in content  # AWS access key redacted
    assert "eyJhbGciOiJIUzI1NiJ9" not in content  # JWT redacted
    assert "[REDACTED:" in content  # the placeholders are actually on disk


async def test_redaction_enforced_for_secrets_in_goal_through_runtime(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RETARGETED (loop removal): the goal channel died with the loop; the same
    sink guarantee is driven through the direct surface — a host-supplied
    ``expected_effect`` carrying secrets is redacted in the audit rows and never
    appears raw in the tool response. (WP4a runs this variant with flip=False so the
    effect prose rides the absent-evidence NOTE path — "No pixel evidence of the
    stated expectation: …" — which the response sink redacts; F5 (VAL-WP4a): the
    flip=True sibling below proves the response redaction on the digest-arm EVIDENCE
    channel instead, and ``test_audit_sink_redacts_effect_bearing_fields`` keeps the
    audit-sink placeholder assertion non-vacuous now that the abstention notes no
    longer echo the effect into the audit ``note``.)"""
    sid, bundle, _, _ = make_session(
        monkeypatch, backend=ScriptedBackend(flip=False), dry_run=False,
        require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(
        sid,
        "click",
        x=10,
        y=10,
        expected_effect="configure api_key=AKIAIOSFODNN7EXAMPLE and use password=hunter2secret to sign in",
    )
    response = _payload(response)
    # RE-PINNED by WP4a (E-03): the stated effect over a generic flip is the honest
    # ``uncertain`` (ok False) — the redaction guarantees below are unchanged.
    assert response["ok"] is False, response
    assert response["result"] == "uncertain", response.get("result")

    payload = json.dumps(response["verification"])
    assert "AKIAIOSFODNN7EXAMPLE" not in payload
    assert "hunter2secret" not in payload
    content = bundle.auditor.path_for(sid).read_text(encoding="utf-8")
    assert "AKIAIOSFODNN7EXAMPLE" not in content  # redacted at the audit sink
    assert "hunter2secret" not in content
    # The placeholders actually fired on this path (WP4a re-pin: the E-03 abstention
    # notes no longer assert the effect into the audit `note`, so the tool RESPONSE —
    # whose absent-evidence note still echoes the effect prose — is the surface this
    # scenario proves the placeholder machinery on; the audit sink guarantee (secret
    # raw NOWHERE on disk) is asserted above on both sinks).
    assert "[REDACTED:" in payload


async def test_redaction_enforced_on_the_flip_screen_evidence_channel(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F5 (VAL-WP4a DELTA 2): the flip=True sibling. On a flipping screen the E-03
    abstention NOTE never echoes the effect, but the identity tier's evidence channel
    still does (``expected_effect=…`` lines) — and the response sink redacts evidence
    item-wise, so the placeholder machinery is proven on THIS channel too, with the
    secret raw nowhere (response payload or audit file)."""
    sid, bundle, _, _ = make_session(
        monkeypatch, backend=ScriptedBackend(flip=True), dry_run=False,
        require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(
        sid,
        "click",
        x=10,
        y=10,
        expected_effect="configure api_key=AKIAIOSFODNN7EXAMPLE and use password=hunter2secret to sign in",
    )
    response = _payload(response)
    assert response["ok"] is False, response
    assert response["result"] == "uncertain", response.get("result")

    payload = json.dumps(response["verification"])
    assert "AKIAIOSFODNN7EXAMPLE" not in payload
    assert "hunter2secret" not in payload
    assert "[REDACTED:" in payload  # the evidence channel carried the effect prose
    content = bundle.auditor.path_for(sid).read_text(encoding="utf-8")
    assert "AKIAIOSFODNN7EXAMPLE" not in content  # raw nowhere at the audit sink
    assert "hunter2secret" not in content


def test_audit_sink_redacts_effect_bearing_fields(tmp_path: Any) -> None:
    """F5 (VAL-WP4a DELTA 2): the audit-sink placeholder assertion stays NON-vacuous.
    The E-03 abstention notes no longer echo a stated effect into the audit ``note``,
    so the runtime scenario above can no longer exercise the sink's placeholder on
    that field — this test drives the exact effect-bearing field shape through
    :class:`AuditLogger` directly and proves the sink still replaces the secrets with
    placeholders (never raw) and never drops the row."""
    logger = AuditLogger(tmp_path / "audit-effect-field")
    logger.emit(
        "verification",
        "sess-effect-field",
        result="uncertain",
        metadata={
            "note": (
                "No pixel evidence of the stated expectation: configure "
                "api_key=AKIAIOSFODNN7EXAMPLE and use password=hunter2secret to sign "
                "in; absent pixels are not proof of absence."
            ),
        },
    )
    content = logger.path_for("sess-effect-field").read_text(encoding="utf-8")
    assert "AKIAIOSFODNN7EXAMPLE" not in content
    assert "hunter2secret" not in content
    assert "[REDACTED:" in content
    assert '"result": "uncertain"' in content  # the row itself still landed


# --- metrics sanity ------------------------------------------------------------------------------


async def test_metrics_snapshot_invariants_after_scripted_runs(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RETARGETED (loop removal): counter/latency invariants on the direct path —
    every counter name present, counters add up, latencies recorded; a FAILED
    verification keeps the same invariants (failed verdict never counted as success)."""
    # Happy path: every counter name present, counters add up, latencies recorded.
    sid, bundle, _, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(sid, "click", x=10, y=10, expected_effect="window appears")
    response = _payload(response)
    # RE-PINNED by WP4a (E-03): the stated effect over a generic flip is the honest
    # ``uncertain`` (ok False, uncertain is never success) — the counter/latency
    # invariants below are unchanged (and the single-record structure keeps exactly
    # one verification latency sample per action).
    assert response["ok"] is False, response
    assert response["result"] == "uncertain", response.get("result")
    snapshot = bundle.metrics.snapshot()
    counters = snapshot["counters"]
    for name in Metrics.COUNTER_NAMES:
        assert name in counters
    assert counters["action_total"] == counters["action_success"] + counters["action_failure"]
    assert snapshot["latencies"]["observation_ms"]["count"] >= 2
    assert snapshot["latencies"]["execution_ms"]["count"] == 1
    assert snapshot["latencies"]["verification_ms"]["count"] == 1

    # Failing path: invariants still hold on a failed verification (a stated
    # window expectation that never appears is reported failed — never silently
    # OK). RC-D11 (058) update: a HOTKEY with a pixel expectation can no longer
    # produce a DEFINITIVE failed (any stated effect on a visual-change intent now
    # degrades to uncertain — absent pixels are not proof of absence), so the
    # failing action is a keypress whose launch-prefix effect promotes the intent
    # to the deterministic window_state tier; the window title never appears ->
    # definitive failed, the honest-failure evidence class this invariant pins.
    sid, bundle, backend, _ = make_session(
        monkeypatch,
        backend=ScriptedBackend(
            flip=False,
            active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="App"),
        ),
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    response = await server.computer_execute(
        sid, "keypress", keys=["enter"], expected_effect="open Calculator"
    )
    response = _payload(response)
    assert response["ok"] is False
    assert response["verification"]["outcome"] == "failed"
    counters = bundle.metrics.snapshot()["counters"]
    assert counters["action_total"] == counters["action_success"] + counters["action_failure"]
    assert counters["verification_failed"] >= 1
    assert len(backend.executed) == 1
