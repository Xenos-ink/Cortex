"""Wave 4 audit-compliance tests: schema conformance, redaction at the sink, metrics sanity.

Scenarios are driven through the MCP tool surface (``run_goal`` / ``start_session`` /
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
    ScriptedProvider,
    audit_events,
    audit_types,
    make_session,
)
from test_controller_integration import (
    click as make_click,
)

from computer_use_mcp import server
from computer_use_mcp.audit import AuditEventType, AuditLogger, Metrics
from computer_use_mcp.models import AgentDecision, WindowInfo
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

async def _drive_audit_scenarios(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, Any]]:
    """Run five scripted sessions that together cover every audit event type."""
    scenarios: list[tuple[str, Any]] = []

    # 1. Happy path with approval: session_start/observation/model_decision/grounding/
    #    validation/safety/approval/execution/verification/session_stop.
    provider = ScriptedProvider(
        [make_click(100, 100, expected_change="window appears"), AgentDecision(status="done")]
    )
    sid, bundle, _, _ = make_session(monkeypatch, provider=provider, dry_run=False, limits=FAST_LIMITS)
    await server.run_goal(sid, "click the button", approve_next_action=True)
    scenarios.append((sid, bundle))

    # 2. Recovery: window switch between propose and execute -> recovery event.
    backend = ScriptedBackend(
        active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="App")
    )
    moved = WindowInfo(hwnd=2, pid=20, process_name="other.exe", title="Other Window")
    provider = ScriptedProvider(
        [make_click(100, 100), make_click(150, 160), AgentDecision(status="done")],
        hooks=[lambda: backend.set_active_window(moved), None, None],
    )
    sid, bundle, _, _ = make_session(
        monkeypatch, backend=backend, provider=provider, dry_run=False, require_approval=False,
        limits=FAST_LIMITS,
    )
    await server.run_goal(sid, "click the moved target")
    scenarios.append((sid, bundle))

    # 3. Provider failure (single, recovered): failure event.
    provider = ScriptedProvider(
        [make_click(40, 50), AgentDecision(status="done")],
        errors=[RuntimeError("malformed provider JSON")],
    )
    sid, bundle, _, _ = make_session(
        monkeypatch, provider=provider, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await server.run_goal(sid, "survive a bad model response")
    scenarios.append((sid, bundle))

    # 4. Limit exceeded: limit_exceeded event.
    provider = ScriptedProvider([make_click(1, 1), make_click(2, 2), make_click(3, 3)])
    sid, bundle, _, _ = make_session(
        monkeypatch, provider=provider, dry_run=False, require_approval=False,
        limits={**FAST_LIMITS, "max_actions": 1},
    )
    await server.run_goal(sid, "one action only")
    scenarios.append((sid, bundle))

    # 5. Emergency stop: stop + emergency_stop events (stop armed before the run).
    sid, bundle, _, _ = make_session(
        monkeypatch, provider=ScriptedProvider([make_click(5, 5)]), dry_run=False,
        require_approval=False, limits=FAST_LIMITS,
    )
    server.stop_session(sid)
    await server.run_goal(sid, "stop before start")
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
    # long-running/resume test modules, not by this legacy single-goal scenario drive.
    # All legacy event types remain fully required here.
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
    goal = "configure api_key=AKIAIOSFODNN7EXAMPLE and use password=hunter2secret to sign in"
    provider = ScriptedProvider([make_click(10, 10), AgentDecision(status="done")])
    sid, bundle, _, _ = make_session(
        monkeypatch, provider=provider, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.run_goal(sid, goal)

    assert response["termination_reason"] == "completed"
    payload = json.dumps(response)
    assert "AKIAIOSFODNN7EXAMPLE" not in payload
    assert "hunter2secret" not in payload
    content = bundle.auditor.path_for(sid).read_text(encoding="utf-8")
    assert "AKIAIOSFODNN7EXAMPLE" not in content  # goal redacted at the audit sink
    assert "hunter2secret" not in content
    assert "[REDACTED:" in content


# --- metrics sanity ------------------------------------------------------------------------------


async def test_metrics_snapshot_invariants_after_scripted_runs(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Happy path: every counter name present, counters add up, latencies recorded.
    provider = ScriptedProvider(
        [make_click(10, 10, expected_change="window appears"), AgentDecision(status="done")]
    )
    sid, bundle, _, _ = make_session(monkeypatch, provider=provider, dry_run=False, limits=FAST_LIMITS)
    await server.run_goal(sid, "happy path", approve_next_action=True)
    snapshot = bundle.metrics.snapshot()
    counters = snapshot["counters"]
    for name in Metrics.COUNTER_NAMES:
        assert name in counters
    assert counters["task_started"] == 1
    assert counters["task_completed"] == 1
    assert counters["action_total"] == counters["action_success"] + counters["action_failure"]
    assert counters["approval_requested"] == counters["approval_granted"] == 1
    assert snapshot["latencies"]["task_ms"]["count"] == 1
    assert snapshot["latencies"]["observation_ms"]["count"] >= 3

    # Recovery exhaustion: invariants still hold on a failing task.
    provider = ScriptedProvider([make_click(10, 10, expected_change="screen must change")])
    sid, bundle, backend, _ = make_session(
        monkeypatch, backend=ScriptedBackend(flip=False), provider=provider, dry_run=False,
        require_approval=False, limits={**FAST_LIMITS, "max_recovery_per_task": 2},
    )
    response = await server.run_goal(sid, "impossible change")

    assert response["termination_reason"] == "failed_verification"
    counters = bundle.metrics.snapshot()["counters"]
    assert counters["task_started"] == 1
    assert counters["task_completed"] == 0
    assert counters["task_failed"] == 1
    assert counters["recovery_total"] == 2
    assert counters["verification_failed"] >= 3
    assert counters["action_total"] == counters["action_success"] + counters["action_failure"]
    assert len(backend.executed) == 3
