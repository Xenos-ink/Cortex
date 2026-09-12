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
    audit_events,
    audit_types,
    make_session,
)

from computer_use_mcp import server
from computer_use_mcp.audit import AuditEventType, AuditLogger, Metrics
from computer_use_mcp.backend import DisplayUnavailableError
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


def _payload(result: Any) -> dict[str, Any]:
    """Unwrap an executed computer_execute content-block response to its dict payload."""
    if isinstance(result, list):
        return json.loads(result[0].text)
    return result

async def _drive_audit_scenarios(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, Any]]:
    """Scripted sessions (RETARGETED to the five-tool surface, run_goal removal)
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
    assert limited.get("error") == "limit_exceeded", limited
    scenarios.append((sid, bundle))

    # 5. Stop: stop + emergency_stop events (stop armed, then a tool call refused).
    sid, bundle, _, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    server.stop_session(sid)
    stopped = await server.computer_execute(sid, "wait", delta=1)
    assert stopped.get("error") == "session_stopped", stopped
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
    # run_goal removal: the loop-only event types below were emitted exclusively by the
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
    """RETARGETED (run_goal removal): the goal channel died with the loop; the same
    sink guarantee is driven through the direct surface — a host-supplied
    ``expected_effect`` carrying secrets is redacted in the audit rows and never
    appears raw in the tool response."""
    sid, bundle, _, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(
        sid,
        "click",
        x=10,
        y=10,
        expected_effect="configure api_key=AKIAIOSFODNN7EXAMPLE and use password=hunter2secret to sign in",
    )
    response = _payload(response)
    assert response["ok"] is True, response

    payload = json.dumps(response["verification"])
    assert "AKIAIOSFODNN7EXAMPLE" not in payload
    assert "hunter2secret" not in payload
    content = bundle.auditor.path_for(sid).read_text(encoding="utf-8")
    assert "AKIAIOSFODNN7EXAMPLE" not in content  # redacted at the audit sink
    assert "hunter2secret" not in content
    assert "[REDACTED:" in content


# --- metrics sanity ------------------------------------------------------------------------------


async def test_metrics_snapshot_invariants_after_scripted_runs(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RETARGETED (run_goal removal): counter/latency invariants on the direct path —
    every counter name present, counters add up, latencies recorded; a FAILED
    verification keeps the same invariants (failed verdict never counted as success)."""
    # Happy path: every counter name present, counters add up, latencies recorded.
    sid, bundle, _, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(sid, "click", x=10, y=10, expected_effect="window appears")
    response = _payload(response)
    assert response["ok"] is True, response
    snapshot = bundle.metrics.snapshot()
    counters = snapshot["counters"]
    for name in Metrics.COUNTER_NAMES:
        assert name in counters
    assert counters["action_total"] == counters["action_success"] + counters["action_failure"]
    assert snapshot["latencies"]["observation_ms"]["count"] >= 2
    assert snapshot["latencies"]["execution_ms"]["count"] == 1
    assert snapshot["latencies"]["verification_ms"]["count"] == 1

    # Failing path: invariants still hold on a failed verification (flip=False ->
    # a stated change expectation is reported failed — never silently OK). The
    # action is a HOTKEY (an unflagged visual-change intent): a flagged CLICK with a
    # stated focus-type expectation now degrades to uncertain under the W-1 (057)
    # contract, so the click would no longer produce the definitive failed verdict
    # this invariant pin requires.
    sid, bundle, backend, _ = make_session(
        monkeypatch, backend=ScriptedBackend(flip=False), dry_run=False,
        require_approval=False, limits=FAST_LIMITS,
    )
    response = await server.computer_execute(
        sid, "hotkey", keys=["ctrl", "a"], expected_effect="screen must change"
    )
    response = _payload(response)
    assert response["ok"] is False
    assert response["verification"]["outcome"] == "failed"
    counters = bundle.metrics.snapshot()["counters"]
    assert counters["action_total"] == counters["action_success"] + counters["action_failure"]
    assert counters["verification_failed"] >= 1
    assert len(backend.executed) == 1
