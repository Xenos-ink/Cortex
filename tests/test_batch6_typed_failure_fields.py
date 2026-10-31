"""Batch-6 permanent regressions: typed failure fields on every plan step.

Ported from the dev tree (``test_batch6_typed_failure_fields.py``) and adapted to
the stable stepwise plan surface (WP8). What the tests pin:

* ``failure_reason`` — ALWAYS present on every plan step and terminal reply, drawn
  from ONE closed vocabulary (:data:`computer_use_mcp.plan.PLAN_FAILURE_REASONS`),
  ``None`` on a verified step; ``unclassified`` is the documented fallback.
* ``not_executed`` — the bounded, explicit list of the plan work that never ran,
  each entry carrying that typed reason.
* ``error`` — the pre-existing typed error key, also present on the exception path.

Adaptations to the unified reply contract (each with its §-reason, recorded in
``.orvex/mission/artifacts/gates/wp8-notes.md``):

* §1.3 stable step token set: a step reply reports ``uncertain`` (never the
  terminal-only ``unknown``) and a session/TaskStopped kill reports ``stopped``
  (WP7b DELTA 1 #5).
* §1.1 ok rule: ``ok`` is True iff ``result`` in {verified, marker} — a failed
  step is ``ok: false`` (the dev copy's legacy ``result != "failed"`` rule is
  gone).
* The dev tree's frozen PRE_BATCH6_KEY key-set diffs are replaced by the
  ``LEGACY_STEP_FIELDS`` presence check: the stable reply contract (§1) adds
  several additive keys (image/evidence retention, ensure_app_outcome, ...), so
  an exact key-set diff would pin the WRONG baseline; the guarantee measured —
  no pre-existing key vanished, only additive keys appear — is unchanged.
* ``server._PLANS_IN_FLIGHT`` is the WP7a :class:`PlanLeaseGuard`
  (``server._plan_guard.is_active``).

Only ScriptedBackend and a scripted provider are used: no physical input, no
network.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    execute_payload,
    make_session,
)

from computer_use_mcp import server
from computer_use_mcp.core.models import GroundedAction, WindowInfo
from computer_use_mcp.planning.plan import PLAN_FAILURE_REASONS
from computer_use_mcp.core.state import SessionRegistry, TaskStopped


def wire_plan(plan):
    from plan_explicitness import complete_plan

    return complete_plan(plan)

FAIL_PLAN: dict[str, Any] = {
    "points": [
        {
            "id": "failure",
            "x": 10,
            "y": 20,
            "actions": [
                {"action": "keypress", "keys": ["enter"], "expected_effect": "open Calculator"}
            ],
        },
        {"id": "next", "x": 30, "y": 40, "actions": [{"action": "click"}]},
    ]
}

OK_PLAN: dict[str, Any] = {
    "points": [
        {"id": "first", "x": 10, "y": 20, "actions": [{"action": "click"}]},
        {"id": "second", "x": 30, "y": 40, "actions": [{"action": "click"}]},
    ]
}

#: A CROSSING point declares its ``actions: []`` and needs no arrival_condition, so
#: its transition move verifies — the world the "verified" half of the vocabulary is
#: measured in (a stated expectation on a pixel-only backend honestly resolves
#: ``uncertain``).
VERIFIED_PLAN: dict[str, Any] = {
    "points": [
        {"id": "first", "x": 10, "y": 20, "actions": []},
        {"id": "second", "x": 30, "y": 40, "actions": []},
    ]
}

#: Every pre-existing stepwise reply key (nothing removed, nothing renamed).
LEGACY_STEP_FIELDS = (
    "ok",
    "result",
    "message",
    "step_index",
    "evidence_by_backend",
    "executed_prefix",
    "follow_up_results",
    "next_step",
    "remaining",
    "blocked",
    "plan_id",
    "plan",
)
LEGACY_STEP_NUMBER_FIELDS = ("step_number",)
LEGACY_STOPPED_REASON_FIELDS = ("follow_ups_stopped_reason",)
# D-2 (master-mission §1.4 #7): the evidence block is REAL on both backends —
# {captures, repositions}; the dev block's fake-only ``backend.executed`` /
# ``backend.executed_count`` fields were deliberately REMOVED, not ported.
LEGACY_EVIDENCE_FIELDS = ("captures", "repositions")
LEGACY_CUTOFF_FIELDS = ("limit_name", "required", "available", "executed_count", "resolved_count")


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    monkeypatch.setattr(server, "_plan_guard", server.PlanLeaseGuard())
    yield server


def _assert_legacy_surface(payload: dict[str, Any], *, extras: tuple[str, ...] = ()) -> None:
    for field in (*LEGACY_STEP_FIELDS, *extras):
        assert field in payload, f"legacy field {field} vanished: {sorted(payload)}"
    evidence = payload["evidence_by_backend"]
    for field in LEGACY_EVIDENCE_FIELDS:
        assert field in evidence, f"legacy evidence field {field} vanished"
    assert payload["plan"]["plan_id"] == payload["plan_id"]


def _assert_typed_failure(
    payload: dict[str, Any], expected: str, *, extras: tuple[str, ...] = ()
) -> None:
    _assert_legacy_surface(payload, extras=extras)
    assert "failure_reason" in payload
    assert "not_executed" in payload
    assert payload["failure_reason"] == expected
    assert payload["failure_reason"] in PLAN_FAILURE_REASONS
    not_executed = payload["not_executed"]
    assert isinstance(not_executed, list) and not_executed, not_executed
    for entry in not_executed:
        assert set(entry) == {"step_index", "point", "action_types", "reason"}, entry
        assert entry["reason"] == expected
        assert set(entry["point"]) == {"id", "x", "y"}
    assert [entry["step_index"] for entry in payload["remaining"]] == [
        entry["step_index"] for entry in not_executed
    ]


# --- (a) a step whose verification fails ---------------------------------------------------


async def test_verification_failure_reports_typed_reason_and_not_executed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend(
        flip=False,
        active_window=WindowInfo(hwnd=1, pid=1, process_name="app.exe", title="App"),
    )
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id, "done", plan=wire_plan(FAIL_PLAN), approved=True, include_screenshot_after=False
        )
    )
    assert payload["result"] == "failed"
    assert payload["blocked"] is True
    _assert_typed_failure(
        payload,
        "verification_failed",
        extras=(*LEGACY_STEP_NUMBER_FIELDS, *LEGACY_STOPPED_REASON_FIELDS),
    )
    assert payload["executed_prefix"]
    assert [entry["step_index"] for entry in payload["not_executed"]] == [1]
    assert payload["not_executed"][0]["point"]["id"] == "next"
    assert payload["remaining"][0]["reason"] == "previous_step_failed"
    assert "error" not in payload


# --- (b) a step whose execution raises -----------------------------------------------------


async def test_execution_error_reports_typed_reason_error_and_not_executed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenBackend(ScriptedBackend):
        def execute(self, action: GroundedAction, stop: Any = None) -> str:
            raise RuntimeError("uv6 synthetic explosion")

    backend = BrokenBackend(flip=True)
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id, "done", plan=wire_plan(OK_PLAN), approved=True, include_screenshot_after=False
        )
    )
    assert payload["ok"] is False  # §1.1 ok rule (dev legacy: result != "failed")
    assert payload["result"] == "failed"
    _assert_typed_failure(payload, "execution_error", extras=LEGACY_STEP_NUMBER_FIELDS)
    assert payload["error"] == "action_error"
    assert "uv6 synthetic explosion" in payload["message"]
    assert payload["blocked"] is False
    assert not server._plan_guard.is_active(session_id)


# --- (c) a cutoff at a session limit -------------------------------------------------------


async def test_limit_cutoff_reports_typed_reason_and_not_executed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend(flip=True)
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=False,
        limits={**FAST_LIMITS, "max_actions": 2},
    )
    # §1.3 stable step token set: an abstaining step reports ``uncertain`` (the
    # terminal-only ``unknown`` of the dev copy); the typed class is unchanged.
    first = execute_payload(
        await server.computer_execute(
            session_id, "done", plan=wire_plan(OK_PLAN), approved=True, include_screenshot_after=False
        )
    )
    assert first["result"] == "uncertain"
    assert first["failure_reason"] == "verification_uncertain"

    cut = execute_payload(
        await server.computer_execute(
            session_id, "done", plan_action="continue", include_screenshot_after=False
        )
    )
    _assert_typed_failure(cut, "limit_exceeded", extras=(*LEGACY_STEP_NUMBER_FIELDS, "cutoff"))
    assert cut["error"] == "limit_exceeded"
    assert cut["limit"] == "max_actions"
    for field in LEGACY_CUTOFF_FIELDS:
        assert field in cut["cutoff"], f"legacy cutoff field {field} vanished"
    assert cut["cutoff"]["limit_name"] == "max_actions"
    assert cut["cutoff"]["executed_count"] == len(cut["executed_prefix"])


# --- (d) a cancellation (TaskStopped) after dispatch ----------------------------------------


async def test_cancellation_reports_typed_reason_error_and_not_executed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CancelAfterDispatchBackend(ScriptedBackend):
        def execute(self, action: GroundedAction, stop: Any = None) -> str:
            super().execute(action, stop)
            raise TaskStopped("uv6 synthetic stop immediately after dispatch")

    backend = CancelAfterDispatchBackend(flip=True)
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id, "done", plan=wire_plan(OK_PLAN), approved=True, include_screenshot_after=False
        )
    )
    # §1.3 / DELTA 1 #5: a killed plan is ``stopped`` (ok false), never a step
    # verdict that could read as an honest completion.
    assert payload["result"] == "stopped"
    assert payload["stopped"] is True
    _assert_typed_failure(payload, "cancelled", extras=(*LEGACY_STEP_NUMBER_FIELDS, "stopped", "cutoff"))
    assert payload["error"] == "cancelled"
    assert payload["blocked"] is False
    assert payload["cutoff"]["limit_name"] == "cancel"
    for field in LEGACY_CUTOFF_FIELDS:
        assert field in payload["cutoff"], f"legacy cutoff field {field} vanished"
    assert not server._plan_guard.is_active(session_id)


# --- (e) a verified step carries no failure ------------------------------------------------


async def test_verified_step_has_no_failure_reason_and_sensible_not_executed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend(flip=True)
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id,
            "done",
            plan=wire_plan(VERIFIED_PLAN),
            approved=True,
            include_screenshot_after=False,
        )
    )
    _assert_legacy_surface(payload, extras=(*LEGACY_STEP_NUMBER_FIELDS, *LEGACY_STOPPED_REASON_FIELDS))
    assert payload["result"] == "verified"
    assert payload["failure_reason"] is None
    assert "error" not in payload
    assert [entry["step_index"] for entry in payload["not_executed"]] == [1]
    assert payload["not_executed"][0]["reason"] == "awaiting_plan_action"
    assert payload["not_executed"] == payload["remaining"]

    # Completing the plan empties the unexecuted list and still names no failure.
    final = execute_payload(
        await server.computer_execute(
            session_id, "done", plan_action="continue", include_screenshot_after=False
        )
    )
    _assert_legacy_surface(final, extras=(*LEGACY_STEP_NUMBER_FIELDS, *LEGACY_STOPPED_REASON_FIELDS))
    assert final["result"] == "verified"
    assert final["failure_reason"] is None
    assert final["not_executed"] == []
    assert final["remaining"] == []
    assert final["blocked"] is False


# --- (f) a host stop is a cut, not a failure ----------------------------------------------


async def test_host_stop_keeps_prefix_and_names_unexecuted_work(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend(flip=True)
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await server.computer_execute(
        session_id, "done", plan=wire_plan(OK_PLAN), approved=True, include_screenshot_after=False
    )
    stopped = execute_payload(
        await server.computer_execute(
            session_id, "done", plan_action="stop", include_screenshot_after=False
        )
    )
    _assert_legacy_surface(stopped, extras=("cutoff",))
    assert stopped["result"] == "stopped"
    assert stopped["blocked"] is False
    assert stopped["next_step"] is None
    assert len(stopped["executed_prefix"]) == 2
    assert stopped["failure_reason"] is None
    assert [entry["step_index"] for entry in stopped["not_executed"]] == [1]
    assert stopped["not_executed"] == stopped["remaining"]


# --- additive-only guarantee across every scenario ----------------------------------------


async def test_every_step_outcome_keeps_legacy_keys_and_adds_only_typed_fields(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 6, adapted: every scenario keeps the pre-existing stepwise key set
    and the typed ``failure_reason`` / ``not_executed`` additions; only a verified
    step reports no failure. (The dev tree's exact PRE_BATCH6_KEY key-set diffs
    pinned the dev reply contract; the stable contract adds several additive keys
    by design, so this port measures PRESENCE of the legacy set, not absence of
    everything else — see the module docstring.)"""
    class BrokenBackend(ScriptedBackend):
        def execute(self, action: GroundedAction, stop: Any = None) -> str:
            raise RuntimeError("uv6 synthetic explosion")

    class CancelAfterDispatchBackend(ScriptedBackend):
        def execute(self, action: GroundedAction, stop: Any = None) -> str:
            super().execute(action, stop)
            raise TaskStopped("uv6 synthetic stop immediately after dispatch")

    no_flip = ScriptedBackend(
        flip=False,
        active_window=WindowInfo(hwnd=1, pid=1, process_name="app.exe", title="App"),
    )
    verdicts: list[tuple[str, dict[str, Any]]] = []

    for label, backend, plan, limits in (
        ("verified", ScriptedBackend(flip=True), wire_plan(VERIFIED_PLAN), FAST_LIMITS),
        ("verification_failed", no_flip, wire_plan(FAIL_PLAN), FAST_LIMITS),
        ("execution_error", BrokenBackend(flip=True), wire_plan(OK_PLAN), FAST_LIMITS),
        ("cancelled", CancelAfterDispatchBackend(flip=True), wire_plan(OK_PLAN), FAST_LIMITS),
        (
            "limit_exceeded",
            ScriptedBackend(flip=True),
            wire_plan(OK_PLAN),
            {**FAST_LIMITS, "max_actions": 2},
        ),
    ):
        session_id, _bundle, _backend, _provider = make_session(
            monkeypatch,
            backend=backend,
            dry_run=False,
            require_approval=False,
            limits=limits,
        )
        payload = execute_payload(
            await server.computer_execute(
                session_id, "done", plan=plan, approved=True, include_screenshot_after=False
            )
        )
        if label == "limit_exceeded":
            payload = execute_payload(
                await server.computer_execute(
                    session_id, "done", plan_action="continue", include_screenshot_after=False
                )
            )
        verdicts.append((label, payload))
        assert payload["failure_reason"] in (PLAN_FAILURE_REASONS | {None}), label
        json.dumps(payload)

    for label, payload in verdicts:
        # the executed-shape rows carry the queue's stopped reason; the exception
        # replies (execution_error / cancelled / limit_exceeded) never did
        extras = (
            LEGACY_STEP_NUMBER_FIELDS
            if label in ("execution_error", "cancelled", "limit_exceeded")
            else (*LEGACY_STEP_NUMBER_FIELDS, *LEGACY_STOPPED_REASON_FIELDS)
        )
        _assert_legacy_surface(
            payload, extras=(*extras, "failure_reason", "not_executed")
        )
    # The typed vocabulary is closed: nothing invented outside the known set, and
    # only a verified step reports no failure.
    for label, payload in verdicts:
        assert (payload["failure_reason"] is None) is (label == "verified"), label