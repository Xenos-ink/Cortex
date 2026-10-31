"""WP7b — the stepwise waypoint-plan surface through computer_execute (§1.3).

Ported from the dev copy's deferred surface nodes (``test_plan_surface.py``,
``test_plan_uncertain_continues.py``, ``test_final_wait_not_executed.py``,
``test_d3_plan_interference_retention.py``, w5 PART 5 wire proofs, the
move-settle plan-drive nodes, ``test_orvex_107_key_hold.py`` section E2 and the
``test_p5_redteam.py`` rt7 trailing-param pin — that last one lives in its own
file) and ADAPTED to the unified contract:

- a plan is ALWAYS stepwise (S-01: the dev copy's legacy single-call plan path is
  not ported — one call == one waypoint step);
- every step and terminal reply passes ``validate_reply`` (§1.3 surfaces);
- the STEP reply's ``result`` follows the §1.3 plan-step set, so a step whose
  items abstained reports ``uncertain`` (the dev step replies said ``unknown``,
  which is the TERMINAL token for the same state) and its ``ok`` follows the ONE
  §1.1 ok-rule (the dev replies echoed item 0's ok — several dev assertions
  ``payload["ok"] is True`` are therefore re-pinned ``ok is False`` here with a
  §-reason; nothing about the per-item verdicts changed);
- terminal replies come from ``plan.plan_terminal_outcome``: ``ok`` is True IFF
  ``result == "verified"`` — ``unknown`` and ``stopped`` are never success
  (V/4244), pinned through the real surface below;
- the lease guard is ``server._plan_guard`` (the dev copy's ``_PLANS_IN_FLIGHT``
  set is gone with the legacy path).

New pins the dev copy lacked: fetch ALLOWED mid-lease (S-04), flat/wait/cancel/
observe/zoom refused mid-lease, wait_status ALLOWED mid-lease, replace inherits
the FIRST call's approval and cannot escalate (O-03), lease released on
terminal / stop / stop_session / idle reap / never installed on a bad plan, and
the legacy single-call path absent (the ``plan_context`` grep pin).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from plan_explicitness import complete_plan as wire_plan
from reply_contract import validate_reply
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    SessionRegistry,
    audit_events,
    execute_payload,
    executed_summary,
    make_session,
)

import computer_use_mcp.desktop.backend as backend_module
import computer_use_mcp.server.plan_surface as srv_server_plan_surface
import computer_use_mcp.server.lifecycle as srv_server_lifecycle
import computer_use_mcp.server.wait_machine as srv_server_wait_machine
import computer_use_mcp.server.wait as srv_server_wait
import computer_use_mcp.server.execute as srv_server_execute
import computer_use_mcp.planning.plan as srv_planning_plan
import computer_use_mcp.server.observe as srv_server_observe
import computer_use_mcp.server.fetch as srv_server_fetch
import computer_use_mcp.server.replies as srv_server_replies
from computer_use_mcp.planning.plan import PLAN_EVIDENCE_KEY, PlanLeaseGuard
from computer_use_mcp.core.models import WindowInfo

SERVER_PATH = Path(srv_server_plan_surface.__file__)

USER_PLAN: dict[str, Any] = {
    "transition_duration_ms": 0,
    "point_timeout_ms": 5_000,
    "settle_ms": 0,
    "on_failure": "stop",
    "on_uncertain": "continue",
    "points": [
        {
            "id": "open-menu",
            "x": 420,
            "y": 180,
            "actions": [{"action": "click"}],
            "arrival_condition": "the menu is open",
        },
        {
            "id": "choose-item",
            "x": 460,
            "y": 260,
            "actions": [{"action": "click"}],
            "arrival_condition": "the item view is showing",
        },
        {
            "id": "save",
            "x": 850,
            "y": 720,
            "actions": [{"action": "click"}],
            "arrival_condition": "the document is saved",
        },
    ],
}

CROSSING_PLAN: dict[str, Any] = {
    "transition_duration_ms": 0,
    "point_timeout_ms": 5_000,
    "settle_ms": 0,
    "on_failure": "stop",
    "on_uncertain": "continue",
    "points": [
        {"id": "open-menu", "x": 420, "y": 180, "actions": []},
        {"id": "choose-item", "x": 460, "y": 260, "actions": []},
    ],
}

STATIC_PLAN: dict[str, Any] = {
    "points": [
        {"id": "a", "x": 10, "y": 10, "actions": [{"action": "click"}]},
        {"id": "b", "x": 20, "y": 20, "actions": [{"action": "click"}]},
    ]
}

ONE_POINT_PLAN: dict[str, Any] = {
    "points": [{"id": "only", "x": 30, "y": 40, "actions": [{"action": "click"}]}]
}

NOT_EXECUTED: dict[str, Any] = {"outcome": "not_executed", "ok": False}


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(srv_server_lifecycle, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(srv_server_lifecycle, "_bundles", {})
    monkeypatch.setattr(srv_server_wait_machine, "_bundles", srv_server_lifecycle._bundles)
    monkeypatch.setattr(srv_server_wait, "_bundles", srv_server_lifecycle._bundles)
    # a fresh guard per test: a stale in-flight marker from another test's session
    # id must never reject a lease install
    monkeypatch.setattr(srv_server_plan_surface, "_plan_guard", PlanLeaseGuard())
    monkeypatch.setattr(srv_server_execute, "_plan_guard", srv_server_plan_surface._plan_guard)
    monkeypatch.setattr(srv_server_lifecycle, "_plan_guard", srv_server_plan_surface._plan_guard)
    yield


async def _continue(session_id: str) -> dict[str, Any]:
    return execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan_action="continue", include_screenshot_after=False
        )
    )


async def _drive_plan(
    session_id: str, plan: dict[str, Any], **kwargs: Any
) -> dict[str, Any]:
    """Drive the stepwise handshake through to the last step reply (or terminal)."""
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan=wire_plan(plan), **kwargs
        )
    )
    for _ in range(20):
        if not (
            payload.get("blocked") is True
            and payload.get("next_step") is not None
            and payload.get("error") is None
        ):
            return payload
        payload = await _continue(session_id)
    raise AssertionError(f"plan handshake did not terminate: {payload}")


# --- the stepwise handshake ---------------------------------------------------------------------


async def test_user_reference_plan_executes_stepwise_in_order(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user's three-point plan executes ONE POINT PER CALL, in order, through the
    FULL pipeline per item; the step replies carry the honest per-point aggregate.

    SPEC 5.2 consequence (unchanged from dev): a stated ``arrival_condition`` on a
    pixel-only change honestly resolves ``uncertain`` (ScriptedBackend(flip=True)
    only changes pixels), so the walk ends parked on the last step — never upgraded
    to a success.  §-reason for the ok pin: the unified contract's §1.1 ok-rule
    applies to the STEP reply too, so ``ok`` is False exactly when the step's
    result is ``uncertain`` (dev echoed item 0's ok)."""
    backend = ScriptedBackend(flip=True, width=1024, height=800)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    first = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(USER_PLAN),
            approved=True,
            include_screenshot_after=False,
        )
    )
    assert first["blocked"] is True and first["step_index"] == 0
    assert first["next_step"]["point"]["id"] == "choose-item"
    validate_reply(first, "plan_step")
    # one point per call: only point 0's items dispatched so far
    assert executed_summary(backend) == [
        ("move", (420, 180), None),
        ("click", (420, 180), None),
    ]

    second = await _continue(session_id)
    assert second["step_index"] == 1 and second["blocked"] is True
    third = await _continue(session_id)
    # DELTA 2 N3: the last step did not verify itself (stated pixel-only
    # expectation abstains), so it PARKS — the step reply keeps the honest
    # per-point aggregate and the lease stays for stop/replace.
    validate_reply(third, "plan_step")
    assert third["step_index"] == 2
    assert third["next_step"] is None  # no further step to propose
    assert third["result"] == "uncertain" and third["ok"] is False
    assert third["blocked"] is True
    assert srv_server_plan_surface._plan_guard.is_active(session_id) is True
    # every uncertain point is reported uncertain — never completed, never verified
    assert [p["status"] for p in third["plan"]["points"]] == [
        "uncertain",
        "uncertain",
        "uncertain_stopped",
    ]
    assert len(third["follow_up_results"]) == 6
    assert third["plan"]["points"][0]["transition"]["action"] == "move"
    assert third["plan"]["points"][0]["actions"][0]["action"] == "click"
    assert executed_summary(backend) == [
        ("move", (420, 180), None),
        ("click", (420, 180), None),
        ("move", (460, 260), None),
        ("click", (460, 260), None),
        ("move", (850, 720), None),
        ("click", (850, 720), None),
    ]
    # every uncertain point is reported uncertain — never completed, never verified
    assert [p["status"] for p in third["plan"]["points"]] == [
        "uncertain",
        "uncertain",
        "uncertain_stopped",
    ]
    assert len(third["follow_up_results"]) == 6
    assert third["plan"]["points"][0]["transition"]["action"] == "move"
    assert third["plan"]["points"][0]["actions"][0]["action"] == "click"
    # continue once more: the walk-completed terminal reply (whole-plan aggregate)
    terminal = await _continue(session_id)
    validate_reply(terminal, "terminal_plan")
    assert terminal["result"] == "unknown" and terminal["ok"] is False
    assert srv_server_plan_surface._plan_guard.is_active(session_id) is False


async def test_bad_plan_is_rejected_before_any_capture_or_motion(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan missing a coordinate is a typed teaching rejection BEFORE anything:
    zero executions, zero captures, and NO lease ever installed."""
    backend = ScriptedBackend(flip=True)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    bad_plan = {"points": [{"x": 420, "y": 180}, {"x": 460}]}  # second point has no y
    payload = await srv_server_execute.computer_execute(session_id, "done", plan=bad_plan)
    assert payload["ok"] is False
    assert payload["result"] == "rejected" and "invalid_action" in payload["reasons"]
    assert executed_summary(backend) == []
    assert len(backend.observed) == 0  # not even one capture happened
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is None


async def test_plan_and_flat_action_surface_are_mutually_exclusive(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend(flip=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = await srv_server_execute.computer_execute(session_id, "click", x=10, y=10, plan=USER_PLAN)
    assert payload["ok"] is False
    assert payload["result"] == "rejected" and "invalid_action" in payload["reasons"]
    assert executed_summary(backend) == []


async def test_legacy_flat_call_without_plan_stays_byte_identical(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract item (a): the flat surface without plan/plan_action behaves exactly
    as before. DELTA 1 #7: the pin is the EXACT key set — derived from the code
    (``ExecutionResult.model_fields`` minus the popped image blob, plus the additive
    keys the flat path itself sets), not a hand list — so ANY leaked plan-only key
    (failure_reason, not_executed, evidence_by_backend, blocked, plan_id, ...) fails."""
    from computer_use_mcp.core.models import ExecutionResult

    backend = ScriptedBackend(flip=True)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(await srv_server_execute.computer_execute(session_id, "move", x=40, y=50))
    assert payload["ok"] is True
    validate_reply(payload, "flat_execute")
    expected_flat_keys = (
        set(ExecutionResult.model_fields)
        - {"screenshot_after_base64"}  # the opt-out popped it
        | {
            "result",
            "model_confidence",
            "grounding_confidence",
            "verification_confidence",
            "image_outbound_scale",  # the honest None of an opt-out reply
            "image_format",  # the served-bytes format note (flat execute additive)
            # the WP5 evidence block this executed reply's retained frame publishes
            # (the ``EvidenceStore.store()`` reply block, evidence.py — code-derived)
            "image_stored",
            "image_attached",
            "image_id",
            "image_captured_at",
            "image_age_seconds",
            "image_bytes",
            "image_width",
            "image_height",
            "evidence_retention",
            "evidence_source",
            "evidence_fetch",
        }
    )
    assert set(payload) == expected_flat_keys, (
        sorted(set(payload) - expected_flat_keys),
        sorted(expected_flat_keys - set(payload)),
    )
    assert executed_summary(backend) == [("move", (40, 50), None)]
    followups = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "click",
            x=40,
            y=50,
            approved=True,
            follow_ups=[{"action": "wait", "delta": 1}],
            include_screenshot_after=False,
        )
    )
    assert followups["ok"] is True
    # a queued opt-out reply retains no frame (the blob was popped before the WP5
    # retention point), so the evidence block keys are absent there
    assert set(followups) == expected_flat_keys - {
        "image_format",  # only a single-shot served frame carries it
        "image_stored", "image_attached", "image_id", "image_captured_at",
        "image_age_seconds", "image_bytes", "image_width", "image_height",
        "evidence_retention", "evidence_source", "evidence_fetch",
    } | {"follow_up_results", "follow_ups_stopped_reason"}
    assert [item["action_type"] for item in followups["follow_up_results"]] == ["click", "wait"]


async def test_crossing_point_plan_walks_to_terminal_and_releases_the_lease(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crossing-point plan fully verifies (moves only) and the lease is released
    exactly once on the terminal step."""
    backend = ScriptedBackend(flip=True, width=1024, height=800)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = await _drive_plan(session_id, CROSSING_PLAN, include_screenshot_after=False)
    assert payload["ok"] is True
    assert payload["result"] == "verified"
    assert payload["blocked"] is False
    validate_reply(payload, "plan_step")
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is None
    assert srv_server_plan_surface._plan_guard.is_active(session_id) is False
    assert payload["plan"]["status"] == "completed"
    assert [p["status"] for p in payload["plan"]["points"]] == ["completed", "completed"]


async def test_plan_tolerates_json_encoded_string(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The follow_ups tolerance pattern: a JSON-encoded plan string parses and runs
    the SAME strict boundary afterwards (S-10: the advertised schema stays
    object-only; the string arm is runtime-only)."""
    backend = ScriptedBackend(flip=True)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=json.dumps(wire_plan({"points": [{"x": 5, "y": 6}]})),
            include_screenshot_after=False,
        )
    )
    assert payload["ok"] is True
    assert executed_summary(backend)[0] == ("move", (5, 6), None)


async def test_plan_items_carry_hold_ms_through_the_queue(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """hold_ms rides inside plans untouched (an action AT the point)."""
    backend = ScriptedBackend(flip=True)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    plan = {"points": [{"x": 10, "y": 10, "actions": [{"action": "click", "hold_ms": 250}]}]}
    payload = await _drive_plan(
        session_id, plan, approved=True, include_screenshot_after=False
    )
    assert backend.executed[1].hold_ms == 250


def test_plan_move_specs_carry_the_resolved_duration_to_the_backend_contract() -> None:
    """The plan's internal transition duration reaches the backend contract."""
    plan_model = srv_planning_plan.coerce_tolerant_plan(wire_plan(USER_PLAN))
    specs, _context = srv_planning_plan.normalize_plan(plan_model)
    grounded_moves = [
        s.to_grounded(reason_prefix="MCP plan action")
        for s in specs
        if s.action.value == "move"
    ]
    assert grounded_moves
    assert all(g.duration_ms == backend_module.PLAN_MOVE_DURATION_MS for g in grounded_moves)


async def test_plan_in_dry_run_executes_nothing(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry-run inheritance: the plan normalizes and validates, zero inputs dispatch."""
    backend = ScriptedBackend(flip=True, width=1024, height=800)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=True, require_approval=False, limits=FAST_LIMITS
    )
    payload = await _drive_plan(
        session_id, USER_PLAN, approved=True, include_screenshot_after=False
    )
    assert backend.executed == []
    assert "plan" in payload


# --- w5 PART 5: the rejection happens BEFORE any effect ------------------------------------------


@pytest.mark.parametrize(
    ("missing", "named", "layer"),
    [
        ("actions", "points[0].actions", "explicitness"),
        ("transition_duration_ms", "transition_duration_ms", "explicitness"),
        ("settle_ms", "settle_ms", "explicitness"),
        ("arrival_condition", "arrival_condition", "explicitness"),
        ("point_timeout_ms", "point_timeout_ms", "explicitness"),
        ("on_failure", "on_failure", "explicitness"),
        ("on_uncertain", "on_uncertain", "explicitness"),
    ],
)
async def test_an_incomplete_plan_dispatches_nothing_and_captures_nothing(
    fresh_server: Any,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
    named: str,
    layer: str,
) -> None:
    """AC-15 wire proof (w5 PART 5): the rejection is pre-dispatch — the scripted
    backend records every dispatch and every capture, so empty lists ARE the proof;
    and no lease was ever installed."""
    from test_wp7a_plan_boundary import COMPLETE_PLAN, _without

    backend = ScriptedBackend(flip=True)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=_without(missing),
            approved=True,
            include_screenshot_after=False,
        )
    )
    assert payload["ok"] is False
    assert payload["result"] == "rejected" and "invalid_action" in payload["reasons"]
    assert named in payload["message"], payload["message"]
    assert backend.executed == [], backend.executed
    assert backend.observed == [], backend.observed
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is None


async def test_a_complete_plan_dispatches_and_captures(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror image: the accepted plan really runs, so the zero-effect proofs
    above are not passing because the tool is inert."""
    from test_wp7a_plan_boundary import COMPLETE_PLAN

    backend = ScriptedBackend(flip=True)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=COMPLETE_PLAN,
            approved=True,
            include_screenshot_after=False,
        )
    )
    # §-reason (changed pin, DELTA 2 N3): the single-point step did not verify
    # itself (stated expectation on a pixel-only change abstains), so it PARKS —
    # the step reply reports ``uncertain`` (ok false). What this node pins is
    # DISPATCH, and the dispatch really happened.
    assert payload["result"] == "uncertain"
    assert [a.action.value for a in backend.executed] == ["move", "click"]


# --- the queue attachment: plan_policies mark point ends (E-01 producer, WP2 consumer) -----------


def _plan_settle_flags(backend: Any) -> list[Any]:
    """The settle marker each executed action actually carried to the backend."""
    return [getattr(action, "plan_point_settle", "MISSING") for action in backend.executed]


async def test_three_point_plan_marks_transitions_false_and_point_ends_true(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[move,click]x3 -> markers [F,T, F,T, F,T]: every transition move suppressed,
    every point's LAST item marked for the single per-point settle (D15)."""
    backend = ScriptedBackend(flip=True, width=1024, height=800)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = await _drive_plan(
        session_id, USER_PLAN, approved=True, include_screenshot_after=False
    )
    assert payload["ok"] is False  # §1.1: the stated pixel-only expectations abstain
    assert _plan_settle_flags(backend) == [False, True, False, True, False, True]


async def test_actionless_point_marks_its_move_as_the_point_end(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crossing point settles after its move (the move IS the last element)."""
    backend = ScriptedBackend(flip=True, width=1024, height=800)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = await _drive_plan(session_id, CROSSING_PLAN, include_screenshot_after=False)
    assert payload["ok"] is True
    assert _plan_settle_flags(backend) == [True, True]


async def test_final_wait_element_carries_no_settle_marker(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trailing ``final_wait_ms`` element belongs to no point: marker False."""
    backend = ScriptedBackend(flip=True, width=1024, height=800)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = await _drive_plan(
        session_id,
        {**ONE_POINT_PLAN, "final_wait_ms": 1_000},
        approved=True,
        include_screenshot_after=False,
    )
    assert _plan_settle_flags(backend) == [False, True, False]


async def test_the_declared_settle_ms_reaches_the_backend_stamps(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E-01 end to end through the SURFACE: the point's declared ``settle_ms`` rides
    the point's last item as ``plan_point_settle_ms`` (the value the backend sleeps)."""
    backend = ScriptedBackend(flip=True, width=1024, height=800)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    plan = {**ONE_POINT_PLAN, "settle_ms": 40}
    await _drive_plan(session_id, plan, approved=True, include_screenshot_after=False)
    stamps = [getattr(a, "plan_point_settle_ms", "MISSING") for a in backend.executed]
    assert stamps == [None, 40], stamps


# --- on_failure / on_uncertain policies ----------------------------------------------------------


def _failed_first_point_plan(**policy: Any) -> dict[str, Any]:
    """A two-point plan whose FIRST point deterministically fails (a window_state
    expectation that never appears on the scripted world)."""
    return {
        "points": [
            {
                "id": "open",
                "x": 10,
                "y": 10,
                "actions": [
                    {"action": "keypress", "keys": ["enter"], "expected_effect": "open Calculator"}
                ],
            },
            {"id": "later", "x": 20, "y": 20, "actions": [{"action": "click"}]},
        ],
        **policy,
    }


def _windowed_backend() -> ScriptedBackend:
    return ScriptedBackend(
        flip=False,
        active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="App"),
    )


async def test_on_failure_stop_halts_the_sequence_on_a_failed_point(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D7 (on_failure=stop): the later points NEVER execute against a broken
    sequence; the reply names the failure and the lease stays parked for stop/replace.

    §-reason (changed pin): dev asserted the flat item-0 ``ok is True``; the unified
    §1.1 ok-rule keys ``ok`` to the STEP verdict (``failed`` here), which is strictly
    more honest."""
    backend = _windowed_backend()
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            approved=True,
            plan=wire_plan(_failed_first_point_plan()),
            include_screenshot_after=False,
        )
    )
    assert executed_summary(backend) == [("move", (10, 10), None), ("keypress", (10, 10), None)]
    validate_reply(payload, "plan_step")
    assert payload["ok"] is False and payload["result"] == "failed"
    assert payload["failure_reason"] == "verification_failed"
    assert payload["blocked"] is True
    assert payload["plan"]["points"][0]["status"] == "failed"
    assert payload["plan"]["points"][0]["actions"][0]["ok"] is False
    assert payload["plan"]["points"][0]["actions"][0]["verification"] == "failed"
    assert payload["plan"]["points"][1]["status"] == "skipped"
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is not None
    stopped = await srv_server_execute.computer_execute(
        session_id, "done", plan_action="stop", include_screenshot_after=False
    )
    assert stopped["blocked"] is False
    assert stopped["plan"]["status"] == "stopped"
    validate_reply(stopped, "terminal_plan")
    assert stopped["ok"] is False and stopped["result"] == "stopped"
    assert len(stopped["executed_prefix"]) == 2


async def test_on_failure_continue_runs_on_with_honest_verdicts(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """on_failure=continue: the sequence proceeds and the failed verdict stays
    honestly on its own point (never laundered)."""
    backend = _windowed_backend()
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    plan = {
        "points": [
            {
                "id": "open",
                "x": 10,
                "y": 10,
                "on_failure": "continue",
                "actions": [
                    {"action": "keypress", "keys": ["enter"], "expected_effect": "open Calculator"}
                ],
            },
            # a deterministic VERIFIED action for the second point (the already-active
            # window refocuses -> window_state verified), so the point completes
            {"id": "later", "x": 20, "y": 20, "actions": [{"action": "focus_window", "target": "App"}]},
        ]
    }
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            approved=True,
            plan=wire_plan(plan),
            include_screenshot_after=False,
        )
    )
    assert payload["blocked"] is True and payload["result"] == "failed"
    payload = await _continue(session_id)
    # DELTA 1 #1: the last step completes the walk → terminal reply; the failed
    # first point keeps the whole-plan verdict ``failed`` (ok false) even though
    # the final step verified.
    validate_reply(payload, "terminal_plan")
    assert payload["result"] == "failed" and payload["ok"] is False
    assert executed_summary(backend) == [
        ("move", (10, 10), None),
        ("keypress", (10, 10), None),
        ("move", (20, 20), None),
        ("focus_window", (20, 20), None),
    ]
    assert payload["plan"]["points"][0]["status"] == "failed"  # honest, not laundered
    assert payload["plan"]["points"][1]["status"] == "completed"


async def test_point_level_on_uncertain_stop_still_halts_the_sequence(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-point override is the escape hatch back to halt-on-uncertainty."""
    backend = ScriptedBackend(flip=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    plan = {
        "points": [
            {
                "id": "a",
                "x": 10,
                "y": 10,
                "on_uncertain": "stop",
                "actions": [{"action": "click"}],
            },
            {"id": "b", "x": 20, "y": 20, "actions": [{"action": "click"}]},
        ]
    }
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(plan),
            approved=True,
            include_screenshot_after=False,
        )
    )
    assert executed_summary(backend) == [("move", (10, 10), None), ("click", (10, 10), None)]
    assert payload["blocked"] is True
    assert payload["plan"]["points"][0]["status"] == "uncertain_stopped"
    assert payload["plan"]["points"][1]["status"] == "skipped"
    stopped = await _drive_plan_stop(session_id)
    assert stopped["blocked"] is False
    assert stopped["plan"]["status"] == "stopped"


async def _drive_plan_stop(session_id: str) -> dict[str, Any]:
    return execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan_action="stop", include_screenshot_after=False
        )
    )


async def test_plan_level_on_uncertain_stop_still_halts_the_sequence(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same override at the PLAN level (the inherited default) still halts."""
    backend = ScriptedBackend(flip=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan({**STATIC_PLAN, "on_uncertain": "stop"}),
            approved=True,
            include_screenshot_after=False,
        )
    )
    assert [a.action.value for a in backend.executed] == ["move", "click"]
    assert payload["plan"]["points"][0]["status"] == "uncertain_stopped"
    assert payload["plan"]["points"][1]["status"] == "skipped"


# --- uncertain continues: passed-over points stay honest, terminal is unknown --------------------


async def test_uncertain_point_is_passed_over_but_never_reported_completed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """on_uncertain=continue walks past an unverifiable point; every honest field
    still says uncertain; the passed-over point reads plain ``uncertain`` while the
    parked step reads ``uncertain_stopped``.

    §-reason (changed pins): the dev step replies reported the handshake token
    ``unknown``; the §1.3 STEP surface names that state ``uncertain`` (``unknown``
    is reserved for the TERMINAL aggregate), and ``ok`` follows the §1.1 ok-rule."""
    backend = ScriptedBackend(flip=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    first = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(STATIC_PLAN),
            approved=True,
            include_screenshot_after=False,
        )
    )
    validate_reply(first, "plan_step")
    assert first["step_index"] == 0
    assert first["result"] == "uncertain", first
    assert first["plan"]["points"][0]["status"] == "uncertain_stopped", first["plan"]
    assert first["plan"]["points"][0]["actions"][0]["verification"] == "uncertain"
    assert first["plan"]["points"][0]["actions"][0]["ok"] is False
    assert first["blocked"] is True
    assert first["next_step"] is not None and first["next_step"]["point"]["id"] == "b"

    second = await _continue(session_id)
    assert second["step_index"] == 1, second
    assert executed_summary(backend) == [
        ("move", (10, 10), None),
        ("click", (10, 10), None),
        ("move", (20, 20), None),
        ("click", (20, 20), None),
    ]
    assert second["next_step"] is None, second
    points = second["plan"]["points"]
    assert [point["status"] for point in points] == ["uncertain", "uncertain_stopped"]
    assert not any(point["status"] == "completed" for point in points)
    assert all(point["actions"][0]["verification"] == "uncertain" for point in points)
    assert [entry["verification_outcome"] for entry in second["executed_prefix"]] == [
        "verified",  # the point's transition move (cursor predicate)
        "uncertain",
        "verified",
        "uncertain",
    ]
    assert [entry["ok"] for entry in second["executed_prefix"]] == [True, False, True, False]
    assert second["plan"]["status"] != "completed", second["plan"]


async def test_terminal_of_an_all_uncertain_plan_is_unknown_and_ok_false(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE V/4244 PIN through the real surface (DELTA 1 #2i): the step that
    COMPLETES the walk — the last step, reached by one continue — IS the terminal
    reply; the aggregate over the whole prefix reports ``unknown`` with ``ok:false``
    even though the final step itself dispatched fine, and the lease is released
    with the honest answer."""
    backend = ScriptedBackend(flip=False)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    first = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(STATIC_PLAN),
            approved=True,
            include_screenshot_after=False,
        )
    )
    assert first["blocked"] is True and first["step_index"] == 0
    parked = await _continue(session_id)  # the last step: uncertain -> PARKS (N3)
    validate_reply(parked, "plan_step")
    assert parked["result"] == "uncertain" and parked["blocked"] is True
    assert srv_server_plan_surface._plan_guard.is_active(session_id) is True
    terminal = await _continue(session_id)  # the explicit walk-end request
    validate_reply(terminal, "terminal_plan")
    assert terminal["result"] == "unknown"
    assert terminal["ok"] is False
    assert terminal["failure_reason"] == "verification_uncertain"
    assert terminal["blocked"] is False
    assert terminal["next_step"] is None
    assert terminal["not_executed"] == []
    assert terminal["plan"]["points"][0]["status"] == "uncertain"
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is None
    assert srv_server_plan_surface._plan_guard.is_active(session_id) is False
    assert first["plan"] is not None


async def test_terminal_failed_then_verified_is_failed_and_ok_false(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 #2ii: a deterministically FAILED first step passed over with
    ``on_failure="continue"``, then a verified last step (a crossing point) — the
    step that completes the walk reports the whole-plan verdict ``failed`` with
    ``ok:false`` (the last step's own verified token must NOT launder the plan)."""
    backend = _windowed_backend()
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    plan = {
        "points": [
            {
                "id": "open",
                "x": 10,
                "y": 10,
                "on_failure": "continue",
                "actions": [
                    {"action": "keypress", "keys": ["enter"], "expected_effect": "open Calculator"}
                ],
            },
            {"id": "hop", "x": 20, "y": 20, "actions": []},
        ]
    }
    first = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            approved=True,
            plan=wire_plan(plan),
            include_screenshot_after=False,
        )
    )
    assert first["blocked"] is True and first["plan"]["points"][0]["status"] == "failed"
    terminal = await _continue(session_id)  # the verified last step completes the walk
    validate_reply(terminal, "terminal_plan")
    assert terminal["result"] == "failed"
    assert terminal["ok"] is False
    assert terminal["failure_reason"] == "verification_failed"
    assert terminal["plan"]["points"][0]["status"] == "failed"
    assert terminal["plan"]["points"][1]["status"] == "completed"
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is None
    assert srv_server_plan_surface._plan_guard.is_active(session_id) is False


async def test_terminal_of_a_verified_plan_is_verified_and_ok_true(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: a fully-verifying plan terminates ``verified`` with ok True."""
    backend = ScriptedBackend(flip=True, width=1024, height=800)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await srv_server_execute.computer_execute(
        session_id,
        "done",
        plan=wire_plan(CROSSING_PLAN),
        include_screenshot_after=False,
    )
    terminal = await _continue(session_id)
    validate_reply(terminal, "terminal_plan")
    assert terminal["result"] == "verified"
    assert terminal["ok"] is True
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is None


async def test_terminal_stop_of_a_verified_prefix_is_stopped_and_ok_false(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE second V/4244 arm: even a so-far-verified prefix reports ``stopped`` with
    ``ok:false`` when the HOST stops the plan — a stopped plan verified nothing."""
    backend = ScriptedBackend(flip=True, width=1024, height=800)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await srv_server_execute.computer_execute(
        session_id,
        "done",
        plan=wire_plan(CROSSING_PLAN),
        include_screenshot_after=False,
    )
    stopped = await _drive_plan_stop(session_id)
    validate_reply(stopped, "terminal_plan")
    assert stopped["result"] == "stopped"
    assert stopped["ok"] is False
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is None


# --- the final_wait aggregate (sparse prefix must not raise) -------------------------------------


async def test_sparse_prefix_reports_the_final_wait_as_not_executed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A halted plan step with a trailing ``final_wait_ms``: no exception, an honest
    ``final_wait_outcome``.

    §-reason (changed pin): ``ok`` follows the §1.1 ok-rule on the STEP verdict —
    the explicitly-stated on_uncertain=stop leaves the step ``uncertain``, so ok is
    False (dev echoed item 0's ok True)."""
    backend = ScriptedBackend(flip=False, width=1024, height=800)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan({**ONE_POINT_PLAN, "on_uncertain": "stop", "final_wait_ms": 1_000}),
            approved=True,
            include_screenshot_after=False,
        )
    )
    # DELTA 2 N3: the single-point step did not verify itself (on_uncertain=stop),
    # so it PARKS — the step reply is ``uncertain`` (ok false) with the lease kept,
    # and the trailing wait that never ran is still named.
    assert payload["result"] == "uncertain" and payload["ok"] is False
    validate_reply(payload, "plan_step")
    assert payload["plan"]["final_wait_outcome"] == NOT_EXECUTED, payload["plan"]
    assert "wait" not in [a.action.value for a in backend.executed]
    assert payload["plan"]["final_wait_ms"] == 1_000


async def test_default_continue_policy_dispatches_the_final_wait(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the continue default the uncertain point is passed over, the trailing wait
    rides the LAST point's step and really runs, and the aggregate says so."""
    backend = ScriptedBackend(flip=False, width=1024, height=800)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan({**ONE_POINT_PLAN, "final_wait_ms": 1_000}),
            approved=True,
            include_screenshot_after=False,
        )
    )
    # DELTA 2 N3: the single-point step parks (it did not verify itself) — the step
    # reply reports ``uncertain`` while the trailing wait really ran.
    assert payload["result"] == "uncertain" and payload["ok"] is False
    assert payload["plan"]["points"][0]["status"] == "uncertain", payload["plan"]
    assert payload["plan"]["final_wait_outcome"]["outcome"] == "executed", payload["plan"]
    assert [a.action.value for a in backend.executed] == ["move", "click", "wait"]


async def test_stop_after_a_halted_step_reports_the_final_wait_as_not_executed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``plan_action="stop"`` rebuilds the same padded aggregate — it must answer."""
    backend = ScriptedBackend(flip=False, width=1024, height=800)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await srv_server_execute.computer_execute(
        session_id,
        "done",
        plan=wire_plan(
            {
                "points": [
                    {"id": "first", "x": 30, "y": 40, "actions": [{"action": "click"}]},
                    {"id": "second", "x": 60, "y": 70, "actions": [{"action": "click"}]},
                ],
                "final_wait_ms": 1_000,
            }
        ),
        approved=True,
        include_screenshot_after=False,
    )
    stopped = await _drive_plan_stop(session_id)
    assert stopped["blocked"] is False, stopped
    assert stopped["plan"]["final_wait_outcome"] == NOT_EXECUTED, stopped["plan"]


async def test_continue_after_a_halted_step_reports_the_final_wait_as_not_executed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a three-point plan the trailing wait (riding the LAST point's step) is
    still unreached one step later — the same guard, one step further."""
    backend = ScriptedBackend(flip=False, width=1024, height=800)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    three = {
        "points": [
            {"id": "first", "x": 30, "y": 40, "actions": [{"action": "click"}]},
            {"id": "second", "x": 60, "y": 70, "actions": [{"action": "click"}]},
            {"id": "third", "x": 90, "y": 100, "actions": [{"action": "click"}]},
        ],
        "final_wait_ms": 1_000,
    }
    first = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(three),
            approved=True,
            include_screenshot_after=False,
        )
    )
    assert first["step_index"] == 0, first
    second = await _continue(session_id)
    assert second["step_index"] == 1, second
    assert second["plan"]["final_wait_outcome"] == NOT_EXECUTED, second["plan"]
    kinds = [a.action.value for a in backend.executed]
    assert kinds.count("click") == 2, kinds
    assert "wait" not in kinds, kinds


async def test_fully_verifying_plan_still_reports_the_final_wait_as_executed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression guard in the OTHER direction: when the wait really runs, the
    aggregate reports ``executed`` — the guard never flattens a populated entry."""
    backend = ScriptedBackend(flip=True, width=1024, height=800)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan({**ONE_POINT_PLAN, "final_wait_ms": 1_000}),
            approved=True,
            include_screenshot_after=False,
        )
    )
    assert payload["plan"]["final_wait_outcome"] == {"outcome": "executed", "ok": True}
    assert [a.action.value for a in backend.executed] == ["move", "click", "wait"]


# --- E2: the aggregate echoes hold_ms ------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "keys"),
    [("keypress", ["a"]), ("hotkey", ["ctrl", "s"])],
)
async def test_e2_plan_aggregate_echoes_the_hold_of_a_held_key(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, action: str, keys: list[str]
) -> None:
    """The plan aggregate's ``points[].actions[].hold_ms`` carries the hold exactly
    (the PLAN-tier twin of the flat E1 pin). The verdict is deliberately NOT
    asserted: what is under test is that the aggregate NAMES the hold."""
    backend = ScriptedBackend(flip=True)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    plan = {
        "points": [
            {
                "id": "hold-the-key",
                "x": 10,
                "y": 10,
                "actions": [{"action": action, "keys": keys, "hold_ms": 250}],
            }
        ]
    }
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            approved=True,
            plan=wire_plan(plan),
            include_screenshot_after=False,
        )
    )
    point = payload["plan"]["points"][0]
    assert point["id"] == "hold-the-key"
    assert len(point["actions"]) == 1, point
    assert point["actions"][0]["action"] == action
    assert point["actions"][0]["hold_ms"] == 250, "the requested hold must round-trip EXACTLY"
    assert backend.executed[-1].action.value == action
    assert backend.executed[-1].hold_ms == 250


# --- lease refusals: zero-effect typed rejections while a lease is active ------------------------


async def _start_parked_plan(session_id: str, backend: Any) -> dict[str, Any]:
    """Start a two-point plan that parks after step 0 (an uncertain pixel-only point
    on a static world) and return the step reply."""
    return execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(STATIC_PLAN),
            approved=True,
            include_screenshot_after=False,
        )
    )


async def test_a_flat_call_is_refused_mid_lease(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend(flip=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    first = await _start_parked_plan(session_id, backend)
    assert first["blocked"] is True
    dispatched = executed_summary(backend)
    observed = len(backend.observed)
    refusal = await srv_server_execute.computer_execute(session_id, "click", x=99, y=99, approved=True)
    # DELTA 1 #6: the execute-surface refusal carries the rejected envelope + reasons
    validate_reply(refusal, "plan_refusal")
    assert refusal["error"] == "plan_lease" and refusal["blocked"] is True
    assert "executed_prefix" in refusal and PLAN_EVIDENCE_KEY in refusal
    assert executed_summary(backend) == dispatched  # zero effect
    assert len(backend.observed) == observed  # zero captures


async def test_a_second_plan_is_refused_mid_lease(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend(flip=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await _start_parked_plan(session_id, backend)
    refusal = await srv_server_execute.computer_execute(session_id, "done", plan=wire_plan(USER_PLAN))
    validate_reply(refusal, "plan_refusal")
    assert refusal["error"] == "plan_lease"


async def test_a_plan_without_replace_is_refused_mid_lease(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend(flip=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await _start_parked_plan(session_id, backend)
    refusal = await srv_server_execute.computer_execute(
        session_id, "done", plan=wire_plan(USER_PLAN), plan_action="continue"
    )
    validate_reply(refusal, "plan_refusal")
    assert refusal["error"] == "plan_lease"


async def test_plan_action_without_a_lease_is_invalid(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend(flip=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = await srv_server_execute.computer_execute(session_id, "done", plan_action="continue")
    validate_reply(payload, "plan_refusal")
    assert payload["error"] == "invalid_plan_action"


async def test_a_bare_done_without_plan_action_mid_lease_returns_plan_lease(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 #13: a bare defaults call mid-lease is a ``plan_lease`` refusal
    naming the handshake verb to send — ``invalid_plan_action`` is reserved for a
    GIVEN-but-invalid plan_action."""
    backend = ScriptedBackend(flip=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await _start_parked_plan(session_id, backend)
    payload = await srv_server_execute.computer_execute(session_id, "done", include_screenshot_after=False)
    validate_reply(payload, "plan_refusal")
    assert payload["error"] == "plan_lease"


async def test_observe_zoom_screenshot_wait_and_cancel_are_refused_mid_lease(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The typed ``plan_lease`` rejection on every independent/mutating tool; the
    read-only ``computer_wait_status`` stays ALLOWED (WP6 decision)."""
    backend = ScriptedBackend(flip=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await _start_parked_plan(session_id, backend)
    dispatched_before = executed_summary(backend)
    observed_before = len(backend.observed)
    observe = srv_server_observe.computer_observe(session_id)
    assert observe["ok"] is False and observe["error"] == "plan_lease"
    zoom = srv_server_observe.computer_zoom(session_id)
    assert zoom["ok"] is False and zoom["error"] == "plan_lease"
    screenshot = srv_server_observe.computer_screenshot(session_id)
    assert screenshot["ok"] is False and screenshot["error"] == "plan_lease"
    wait = await srv_server_wait.computer_wait(session_id, duration_seconds=5)
    assert wait["ok"] is False and wait["error"] == "plan_lease"
    cancel = await srv_server_wait.computer_cancel_wait(session_id, wait_id="nope")
    assert cancel["error"] == "plan_lease", cancel
    # DELTA 1 #12: the refusals cost NOTHING — no capture, no dispatch
    assert len(backend.observed) == observed_before
    assert executed_summary(backend) == dispatched_before
    status = await srv_server_wait.computer_wait_status(session_id, wait_id="nope")
    # NOT plan_lease: status is read-only — its unknown-id refusal is the §1.1
    # rejected shape (reasons, no error key)
    assert status["ok"] is False and "unknown_wait" in status["reasons"], status


async def test_a_fetch_succeeds_mid_lease(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-04 (the removed fetch-lease seam, pinned behaviorally): a fetch is a
    read-only retrieval of evidence the session already holds and stays ALLOWED
    while a plan lease is active."""
    backend = ScriptedBackend(flip=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    step = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(STATIC_PLAN),
            approved=True,
            include_screenshot_after=True,
        )
    )
    image_id = step.get("image_id")
    assert image_id, sorted(step)
    fetched = await srv_server_fetch.computer_fetch_image(session_id, image_id)
    assert isinstance(fetched, list) and fetched[0].type == "text"  # served, not refused
    payload = json.loads(fetched[0].text)
    assert payload["ok"] is True and payload["image_id"] == image_id


# --- O-03: approval evaluated once on the FIRST plan call ----------------------------------------


async def test_unapproved_first_call_gates_every_step_and_replace_cannot_escalate(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O-03: require_approval is evaluated on the FIRST plan call for the whole plan.
    An unapproved plan gates every step (approval_required, result not_executed,
    cursor stays), ``continue`` never re-asks and never proceeds, and a ``replace``
    carrying approved=True CANNOT escalate — it inherits the first call's verdict."""
    backend = ScriptedBackend(flip=True)
    session_id, bundle, backend, _ = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=True,
        limits=FAST_LIMITS,
    )
    # THREE points, so the walk is not over after two gated steps and the lease is
    # still parked when the escalation control runs.
    three_click_plan = {
        "points": [
            {"id": "a", "x": 10, "y": 10, "actions": [{"action": "click"}]},
            {"id": "b", "x": 20, "y": 20, "actions": [{"action": "click"}]},
            {"id": "c", "x": 30, "y": 30, "actions": [{"action": "click"}]},
        ]
    }
    first = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(three_click_plan),
            approved=False,
            include_screenshot_after=False,
        )
    )
    validate_reply(first, "plan_step")
    assert first["result"] == "not_executed", first
    assert first["requires_approval"] is True
    assert first["failure_reason"] == "approval_required"
    # §1.1/K-53 honesty: the point's benign transition MOVE dispatched (a move needs
    # no approval), the point's ACTION is the gated item — the K-53 cursor rule
    # advances (>= 1 item dispatched) and the click never lands anywhere.
    assert executed_summary(backend) == [("move", (10, 10), None)]
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is not None  # the lease stays

    # continue never re-asks: the next step gates the same way (only moves dispatch)
    second = await _continue(session_id)
    assert second["result"] == "not_executed" and second["requires_approval"] is True
    assert [action.action.value for action in backend.executed] == ["move", "move"]
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is not None

    # a replace carrying approved=True cannot escalate the unapproved plan: a
    # CROSSING replacement runs (moves are benign and need no approval) — the
    # escalation control is the input-action plan below, whose click stays gated.
    replaced = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(CROSSING_PLAN),
            plan_action="replace",
            approved=True,
            include_screenshot_after=False,
        )
    )
    assert replaced["result"] in {"verified", "uncertain"}
    assert replaced.get("requires_approval") is not True
    assert not any(action.action.value == "click" for action in backend.executed)
    escalated = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(STATIC_PLAN),
            plan_action="replace",
            approved=True,
            include_screenshot_after=False,
        )
    )
    assert escalated["result"] == "not_executed"
    assert escalated["requires_approval"] is True
    assert not any(action.action.value == "click" for action in backend.executed)

    stopped = await _drive_plan_stop(session_id)
    assert stopped["ok"] is False and stopped["result"] == "stopped"


async def test_approved_first_call_never_re_asks_on_continue(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: an APPROVED first plan call runs every later step without the
    host ever passing ``approved`` again."""
    backend = ScriptedBackend(flip=True, width=1024, height=800)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=True,
        limits=FAST_LIMITS,
    )
    first = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(CROSSING_PLAN),
            approved=True,
            include_screenshot_after=False,
        )
    )
    assert first["result"] == "verified"  # step 1 ran: the approval carried
    second = await _continue(session_id)
    assert second["result"] == "verified"  # step 2 ran WITHOUT an approved param
    assert executed_summary(backend) == [
        ("move", (420, 180), None),
        ("move", (460, 260), None),
    ]


async def test_replace_inherits_the_first_call_approval_and_runs(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O-03 inheritance, positive arm: a replace under an APPROVED first call runs
    the replacement's first step immediately, without re-asking."""
    backend = ScriptedBackend(flip=True, width=1024, height=800)
    session_id, bundle, backend, _ = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=True,
        limits=FAST_LIMITS,
    )
    await srv_server_execute.computer_execute(
        session_id,
        "done",
        plan=wire_plan(STATIC_PLAN),
        approved=True,
        include_screenshot_after=False,
    )
    replaced = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(CROSSING_PLAN),
            plan_action="replace",
            include_screenshot_after=False,
        )
    )
    assert replaced["result"] == "verified"  # the replacement's step ran: approval inherited
    assert replaced["replaced_plan_id"]
    assert replaced["replaced_prefix"]  # the old plan's executed prefix is preserved
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is not None


async def test_replace_metadata_survives_on_an_image_bearing_reply(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP8 M2 (VAL-WP7b-03): ``replaced_plan_id`` / ``replaced_prefix`` survive on
    an IMAGE-BEARING replace reply. With ``include_screenshot_after=True`` the
    reply is a content-block LIST, which the replace branch's
    ``isinstance(response, dict)`` augmentation cannot touch — the disclosure
    must ride the payload BEFORE the blocks are built (now via the lease)."""
    backend = ScriptedBackend(flip=True, width=1024, height=800)
    session_id, bundle, backend, _ = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    await srv_server_execute.computer_execute(
        session_id,
        "done",
        plan=wire_plan(STATIC_PLAN),
        approved=True,
        include_screenshot_after=False,
    )
    # the OLD plan's executed prefix, redacted exactly as the replace reply will
    # carry it (F13: equality, not presence)
    old_lease = srv_server_plan_surface._plan_guard.active_lease(bundle)
    first_prefix = [srv_server_replies._redact_queue_entry(dict(entry)) for entry in old_lease.prefix]
    response = await srv_server_execute.computer_execute(
        session_id,
        "done",
        plan=wire_plan(CROSSING_PLAN),
        plan_action="replace",
        include_screenshot_after=True,
    )
    # the reply IS the image-bearing shape M2 is about: a content-block list
    assert isinstance(response, list) and response and response[0].type == "text"
    payload = json.loads(response[0].text)
    assert payload["replaced_plan_id"], sorted(payload)
    # F13 (VAL-WP8-01): the replaced prefix EQUALS the old plan's executed prefix,
    # entry for entry — not merely "some list is present".
    assert payload["replaced_prefix"] == first_prefix
    assert payload["plan_id"] != payload["replaced_plan_id"]
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is not None
    # F11 (VAL-WP8-01): the disclosure rides ONLY the replace call's own reply —
    # the NEXT step reply of the replacement carries neither key.
    lease = srv_server_plan_surface._plan_guard.active_lease(bundle)
    assert lease.replaced_plan_id is None and lease.replaced_prefix is None
    second = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan_action="continue", include_screenshot_after=True
        )
    )
    assert "replaced_plan_id" not in second, sorted(second)
    assert "replaced_prefix" not in second, sorted(second)


async def test_replace_without_a_plan_is_refused_and_keeps_the_lease(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend(flip=False)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await _start_parked_plan(session_id, backend)
    payload = await srv_server_execute.computer_execute(
        session_id, "done", plan_action="replace", include_screenshot_after=False
    )
    validate_reply(payload, "plan_refusal")
    assert payload["error"] == "invalid_plan_action"
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is not None


async def test_replace_with_a_malformed_plan_is_teaching_refused_and_keeps_the_lease(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend(flip=False)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await _start_parked_plan(session_id, backend)
    payload = await srv_server_execute.computer_execute(
        session_id,
        "done",
        plan={"points": [{"x": 1}]},
        plan_action="replace",
        include_screenshot_after=False,
    )
    assert payload["ok"] is False and payload["result"] == "rejected"
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is not None


# --- the lease lifecycle: exactly once, never leaked ---------------------------------------------


async def test_lease_released_on_stop_session(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend(flip=False)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await _start_parked_plan(session_id, backend)
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is not None
    await srv_server_lifecycle.stop_session(session_id)
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is None
    assert srv_server_plan_surface._plan_guard.is_active(session_id) is False


async def test_lease_released_on_idle_reap(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An idle lease with NO call in flight does not keep the session alive: the
    WP-S reaper stops the session and the lease is released with it (contract item
    (e); recorded in wp7b-notes)."""
    backend = ScriptedBackend(flip=False)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await _start_parked_plan(session_id, backend)
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is not None
    monkeypatch.setattr(srv_server_lifecycle, "_session_idle_timeout", lambda: 0.05)
    import asyncio as _asyncio

    await _asyncio.sleep(0.08)  # non-blocking: let the session go idle past the timeout
    reaped = srv_server_lifecycle._sweep_idle_sessions()
    assert session_id in reaped
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is None
    assert srv_server_plan_surface._plan_guard.is_active(session_id) is False


async def test_lease_released_when_the_step_errors_structurally(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend/agent error is a structural failure: the lease is released in the
    guaranteed path so no caller is stranded behind a dead plan."""
    backend = ScriptedBackend(flip=False)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )

    def explode(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(backend, "execute", explode)
    payload = await srv_server_execute.computer_execute(
        session_id,
        "done",
        plan=wire_plan(STATIC_PLAN),
        approved=True,
        include_screenshot_after=False,
    )
    payload = execute_payload(payload)
    assert payload["ok"] is False
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is None


async def test_second_install_is_refused_fail_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard's atomic install: a second lease on the same session changes
    nothing (the plan_lease refusal the surface reports as ``error="plan_lease"``)."""
    backend = ScriptedBackend(flip=False)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    plan = srv_planning_plan.coerce_tolerant_plan(wire_plan(STATIC_PLAN))
    specs, context = srv_planning_plan.normalize_plan(plan)
    lease = srv_planning_plan.PlanLease(
        model=plan, specs=specs, context=context,
        steps=srv_planning_plan.plan_step_slices(specs, context),
    )
    srv_server_plan_surface._plan_guard.install(bundle, lease)
    with pytest.raises(ValueError):
        srv_server_plan_surface._plan_guard.install(bundle, lease)
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is lease


# --- S-03: evidence_by_backend built from the store + the lease ----------------------------------


async def test_plan_step_reply_carries_real_evidence_by_backend(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-03/D-2: ``evidence_by_backend`` is a DICT of ``captures`` (from the session's
    EvidenceStore — filled by the reply hooks, so it is real on BOTH backends) plus
    the lease's ``repositions`` — never the fake-only ``backend.executed``/``observed``
    reads the dev copy used."""
    backend = ScriptedBackend(flip=True, width=1024, height=800)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    step = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(STATIC_PLAN),
            approved=True,
            include_screenshot_after=True,
        )
    )
    evidence = step[PLAN_EVIDENCE_KEY]
    assert isinstance(evidence, dict)
    assert set(evidence) == {"captures", "repositions"}
    assert isinstance(evidence["captures"], list) and evidence["captures"], evidence
    # the captures ARE the store's (bounded metadata: image_id, source, ...) — no
    # backend.executed payloads, no observation dumps
    assert "image_id" in evidence["captures"][0]
    assert "backend.executed" not in evidence and "backend.observed" not in evidence
    from computer_use_mcp.vision.evidence import evidence_store as _store

    store = _store(bundle.extra)
    assert store is not None and evidence["captures"] == store.captures()[-32:]
    assert isinstance(evidence["repositions"], list)


# --- D-3: interference events survive across stepwise calls --------------------------------------

TARGET = WindowInfo(hwnd=1, pid=100, process_name="EXCEL.EXE", window_class="XLMAIN", title="Book1 - Excel")
SAVE_AS = WindowInfo(hwnd=2, pid=100, process_name="EXCEL.EXE", window_class="#32770", title="Save As")
#: Foreign in EVERY identity field, so no allowlist/owned-dialog rule can heal the steal.
FOREIGN = WindowInfo(hwnd=9, pid=900, process_name="notepad.exe", window_class="Notepad", title="user notes - Notepad")

TWO_POINT_PLAN: dict[str, Any] = {
    "points": [
        {
            "id": "open-menu",
            "x": 30,
            "y": 40,
            "actions": [{"action": "click"}],
            "arrival_condition": "the menu is open",
        },
        {
            "id": "choose-item",
            "x": 50,
            "y": 60,
            "actions": [
                {"action": "keypress", "keys": ["enter"], "expected_effect": "open Calculator"}
            ],
        },
    ]
}


class ModalOnFirstDispatch(ScriptedBackend):
    """A modal takes the foreground on the plan point's FIRST dispatch (the move) —
    the shape in which the guard event is genuinely observed by the item whose
    outcome the step reply publishes."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.dispatches = 0

    def execute(self, action: Any, stop: Any = None, **kwargs: Any) -> str:
        message = super().execute(action, stop)
        self.dispatches += 1
        if self.dispatches == 1:
            self.set_active_window(SAVE_AS)
            self.system_dialog = dict(SAVE_AS.model_dump())
        return message


async def test_modal_dialog_event_is_carried_across_stepwise_plan_calls(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-3, post-action side: the SAME event string the host got on call 1 is the one
    the aggregate reports on call 2 and the terminal reply on call 3 (value identity,
    retained on the lease, deduped + bounded)."""
    from computer_use_mcp.desktop.interference import MODAL_DIALOG

    backend = ModalOnFirstDispatch(flip=True, active_window=TARGET)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    step1 = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan=wire_plan(TWO_POINT_PLAN), include_screenshot_after=False
        )
    )
    assert step1["interference_events"], f"the step that saw the dialog reported nothing: {sorted(step1)}"
    event = step1["interference_events"][0]
    assert event.startswith(MODAL_DIALOG)
    assert step1["blocked"] is True and step1["step_index"] == 0
    # the user dismisses the modal between calls — the condition is GONE, the event stays
    backend.system_dialog = None
    backend.set_active_window(TARGET)
    # DELTA 2 N3: the SECOND step completes the walk's cursor but did not verify
    # itself (its keypress deterministically failed) — the reply PARKS with the
    # retained event on the top-level key AND the aggregate.
    step2 = await _continue(session_id)
    validate_reply(step2, "plan_step")
    assert step2["blocked"] is True and step2["step_index"] == 1
    assert step2["result"] == "failed" and step2["ok"] is False
    assert event in step2["plan"]["interference_events"], step2["plan"]
    assert event in step2["interference_events"], step2.get("interference_events")
    assert step2["next_step"] is None  # the walk is over; the lease parks for stop/replace
    # the host's explicit walk-end: the terminal reply keeps the retained event
    terminal = await _drive_plan_stop(session_id)
    assert event in terminal["interference_events"], terminal.get("interference_events")
    assert event in terminal["plan"]["interference_events"]
    assert terminal["plan"]["status"] == "stopped"
    assert srv_server_plan_surface._plan_guard.is_active(session_id) is False


async def test_pre_dispatch_focus_steal_event_reaches_the_plan_aggregate(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-3b: a blocking PRE-dispatch verdict publishes its event on the step reply
    AND the aggregate; the refused step dispatched nothing."""
    from computer_use_mcp.desktop.interference import FOCUS_TAKEN_BY

    backend = ScriptedBackend(flip=True, active_window=TARGET)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    plan = {
        "points": [
            {"id": "first", "x": 30, "y": 40, "actions": [{"action": "click"}]},
            {"id": "second", "x": 50, "y": 60, "actions": [{"action": "click"}]},
        ]
    }
    step1 = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan=wire_plan(plan), include_screenshot_after=False
        )
    )
    assert step1["blocked"] is True and step1["step_index"] == 0
    assert "interference_events" not in step1, step1.get("interference_events")
    dispatched_before = executed_summary(backend)
    bundle.agent.guard.rebind(TARGET)
    assert bundle.agent.guard.armed is True
    backend.set_active_window(FOREIGN)
    step2 = await _continue(session_id)
    event = step2["reasons"][0]
    assert event.startswith(FOCUS_TAKEN_BY)
    assert event in step2["plan"]["interference_events"], step2["plan"]
    assert step2["interference_events"] == [event]
    assert step2["blocked"] is True and step2["step_index"] == 1
    assert executed_summary(backend) == dispatched_before  # negative control: zero dispatch
    stopped = await _drive_plan_stop(session_id)
    assert event in stopped["plan"]["interference_events"], stopped["plan"]
    assert stopped["plan"]["status"] == "stopped"
    assert srv_server_plan_surface._plan_guard.is_active(session_id) is False


# --- S-01: the legacy single-call plan path is absent ---------------------------------------------


def test_the_legacy_single_call_plan_path_is_absent() -> None:
    """grep pin (WP-R2b step-1 re-expression, same meaning): ``plan_context`` is
    used ONLY inside the stepwise plan machinery — plan_surface.py's WP7b section
    — AND inside computer_execute's first-call validation branch (execute.py) —
    with no dead second ingestion branch on the flat path (S-01: the dev copy's
    legacy all-at-once plan queue branch is not ported). Every other server
    module must contain NONE."""
    plan_source = SERVER_PATH.read_text(encoding="utf-8")
    execute_path = Path(srv_server_execute.__file__)
    execute_source = execute_path.read_text(encoding="utf-8")
    section_start = plan_source.index("# --- WP7b: the waypoint plan surface")
    branch_start = execute_source.index(
        "# ORVEX-2026-101 decision 1 (WP7b): a new plan is fully validated"
    )
    branch_end = execute_source.index("if follow_ups is not None:", branch_start)
    for path in sorted(SERVER_PATH.parent.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if path.name == "plan_surface.py":
            window = (section_start, len(source))
        elif path == execute_path:
            window = (branch_start, branch_end)
        else:
            window = (-1, -1)  # no plan_context anywhere else in the package
        offset = 0
        for line_number, line in enumerate(source.splitlines(), start=1):
            if "plan_context" in line:
                assert window[0] <= offset < window[1], (
                    f"plan_context used outside the stepwise machinery at "
                    f"server/{path.name}:{line_number}: {line!r}"
                )
            offset += len(line) + 1
    # the flat path has NO second plan ingestion branch: the dev copy's legacy
    # "plan REPLACES the flat primary" queue wiring is gone (WHOLE package scan)
    union = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(SERVER_PATH.parent.glob("*.py"))
    )
    assert "plan REPLACES the flat primary" not in union


def test_the_expected_tools_count_is_unchanged() -> None:
    """Contract item (i): EXPECTED_TOOLS stays at 10 — no tool was added or removed."""
    from computer_use_mcp.cli import EXPECTED_TOOLS

    assert len(EXPECTED_TOOLS) == 10


# --- DELTA 1 #3: interference events from the ACTION item (not just the move) ---------------------


class ModalOnSecondDispatch(ScriptedBackend):
    """The modal takes the foreground on the point's ACTION dispatch (#2) — the
    event is observed on a queue item at index >= 1, whose outcome the step reply
    does NOT publish (first_outcome is the move). DELTA 1 #3: the merged per-item
    event list must still carry it to the step reply, the aggregate and the lease."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.dispatches = 0

    def execute(self, action: Any, stop: Any = None, **kwargs: Any) -> str:
        message = super().execute(action, stop)
        self.dispatches += 1
        if self.dispatches == 2:
            self.set_active_window(SAVE_AS)
            self.system_dialog = dict(SAVE_AS.model_dump())
        return message


class ModalOnFinalDispatch(ScriptedBackend):
    """WP8 M1 (VAL-WP7b-03): the modal appears ONLY on the FINAL step's own
    dispatch (dispatch #3), AFTER the backend executed it — every earlier
    dispatch runs against a CLEAN window state. A completing step's guard event
    can therefore reach the walk-ending reply through the completing step's own
    outcome alone (the lease carries nothing from any earlier step)."""

    def __init__(self, fire_on: int = 3, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fire_on = fire_on
        self.dispatches = 0

    def execute(self, action: Any, stop: Any = None, **kwargs: Any) -> str:
        message = super().execute(action, stop)
        self.dispatches += 1
        if self.dispatches == self.fire_on:
            self.set_active_window(SAVE_AS)
            self.system_dialog = dict(SAVE_AS.model_dump())
        return message


async def test_modal_on_the_action_item_reaches_the_step_and_lease(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 #3: the guard event observed on the point's ACTION item (queue index
    1) is published on the step reply (top level + aggregate) and retained on the
    lease, then carried by the terminal reply — even though the first outcome (the
    transition move) saw nothing."""
    from computer_use_mcp.desktop.interference import MODAL_DIALOG

    backend = ModalOnSecondDispatch(flip=True, active_window=TARGET)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    step1 = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan=wire_plan(TWO_POINT_PLAN), include_screenshot_after=False
        )
    )
    assert step1["interference_events"], (
        f"the action item's event was dropped: {sorted(step1)}"
    )
    event = step1["interference_events"][0]
    assert event.startswith(MODAL_DIALOG)
    assert event in step1["plan"]["interference_events"], step1["plan"]
    assert step1["blocked"] is True and step1["step_index"] == 0
    # the condition is dismissed; the retained (deduped) event must survive
    backend.system_dialog = None
    backend.set_active_window(TARGET)
    terminal = await srv_server_execute.computer_execute(
        session_id, "done", plan_action="stop", include_screenshot_after=False
    )
    assert event in terminal["interference_events"], terminal.get("interference_events")
    assert event in terminal["plan"]["interference_events"]
    assert srv_server_plan_surface._plan_guard.is_active(session_id) is False


def test_plan_retain_events_dedupes_and_caps(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 #4: the lease carry is DEDUPED (first-seen order) and CAPPED at
    ``_PLAN_INTERFERENCE_EVENTS_MAX`` (the most recent entries win; the oldest are
    dropped) — pinned as behaviour, not a claim."""
    from computer_use_mcp.server.plan_surface import _PLAN_INTERFERENCE_EVENTS_MAX, _plan_retain_events

    backend = ScriptedBackend(flip=False)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    plan = srv_planning_plan.coerce_tolerant_plan(wire_plan(STATIC_PLAN))
    specs, context = srv_planning_plan.normalize_plan(plan)
    lease = srv_planning_plan.PlanLease(
        model=plan, specs=specs, context=context,
        steps=srv_planning_plan.plan_step_slices(specs, context),
    )
    distinct = [f"E{index:03d}" for index in range(100)]
    _plan_retain_events(lease, distinct + distinct[:5])  # 105 events, 5 duplicates
    assert len(lease.interference_events) == _PLAN_INTERFERENCE_EVENTS_MAX
    # first-seen order, capped to the MOST RECENT max entries
    assert lease.interference_events == distinct[-_PLAN_INTERFERENCE_EVENTS_MAX:]
    # duplicates of RETAINED events are dropped (dedupe is against the retained set)
    _plan_retain_events(lease, distinct[-5:])
    assert lease.interference_events == distinct[-_PLAN_INTERFERENCE_EVENTS_MAX:]
    _plan_retain_events(lease, ["NEW"])
    assert len(lease.interference_events) == _PLAN_INTERFERENCE_EVENTS_MAX
    assert lease.interference_events[-1] == "NEW"
    assert lease.interference_events[0] == distinct[-_PLAN_INTERFERENCE_EVENTS_MAX + 1]


# --- DELTA 1 #5: a killed session is a stopped plan, never a finished one -------------------------


class StopOnSecondDispatch(ScriptedBackend):
    """The stop token fires AFTER the first dispatch (the transition move ran, the
    point's action did not) — the TaskStopped path with a partially dispatched step."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.dispatches = 0

    def execute(self, action: Any, stop: Any = None, **kwargs: Any) -> str:
        self.dispatches += 1
        if self.dispatches == 2:
            from computer_use_mcp.core.state import TaskStopped

            raise TaskStopped("host stopped the session mid-step")
        return super().execute(action, stop)


async def test_task_stopped_mid_step_reports_stopped_and_releases_the_lease(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 #5a: TaskStopped mid-step — the reply says ``stopped`` (ok false,
    ``stopped: true``), the lease is released exactly once, and the reply NEVER
    reads as a finished plan (no blocked:false + plan.status:"completed")."""
    backend = StopOnSecondDispatch(flip=True)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    reply = await srv_server_execute.computer_execute(
        session_id, "done", plan=wire_plan(STATIC_PLAN), approved=True, include_screenshot_after=False
    )
    reply = execute_payload(reply)
    validate_reply(reply, "terminal_plan")
    assert reply["result"] == "stopped" and reply["ok"] is False
    assert reply["stopped"] is True
    assert reply["failure_reason"] == "cancelled"
    assert reply["plan"]["status"] == "cancelled"
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is None
    assert srv_server_plan_surface._plan_guard.is_active(session_id) is False
    assert [action.action.value for action in backend.executed] == ["move"]


class StopSessionMidDispatch(ScriptedBackend):
    """stop_session races the in-flight step deterministically: the FIRST dispatch
    happens only after a side thread has stopped the whole session (the real kill
    path — stop_session arms the token and closes the bundle under its locks)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.kill_started = False

    def execute(self, action: Any, stop: Any = None, **kwargs: Any) -> str:
        if not self.kill_started:
            import asyncio as _asyncio
            import threading as _threading

            

            self.kill_started = True
            session_ids = list(srv_server_lifecycle._bundles.keys())
            assert session_ids, "no live session to stop"
            thread = _threading.Thread(
                target=lambda: _asyncio.run(srv_server_lifecycle.stop_session(session_ids[0]))
            )
            thread.start()
            thread.join()  # the session is stopped BEFORE this dispatch proceeds
        return super().execute(action, stop)


async def test_stop_session_racing_a_step_reports_stopped_and_releases_once(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 #5b: stop_session wins the race against an in-flight step — the step
    reply says ``stopped`` (never a finished-plan shape), the lease was released by
    the teardown exactly once (the step's own idempotent release adds nothing), and
    the session is in the stopped registry."""
    backend = StopSessionMidDispatch(flip=True)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    reply = await srv_server_execute.computer_execute(
        session_id, "done", plan=wire_plan(STATIC_PLAN), approved=True, include_screenshot_after=False
    )
    reply = execute_payload(reply)
    assert reply["result"] == "stopped" and reply["ok"] is False, sorted(reply)
    assert reply["stopped"] is True
    # F2 (VAL-WP8-01): the kill is the cause — the failure_reason says
    # "cancelled" UNCONDITIONALLY, never an entry-derived or absent token.
    assert reply["failure_reason"] == "cancelled", sorted(reply)
    assert reply["plan"]["status"] != "completed"
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is None
    assert srv_server_plan_surface._plan_guard.is_active(session_id) is False
    assert session_id in srv_server_lifecycle._stopped_sessions
    # F3 (VAL-WP8-01): the funnel and the in-step TaskStopped handler raced —
    # the idempotent terminal emission yields EXACTLY ONE terminal event for the
    # plan, carrying the reply's own result token and failure_reason.
    # N4 (VAL-WP8-02): the funnel DEFERRED to the in-flight step — the record
    # order is the step's plan_point BEFORE the terminal, and both records carry
    # the FINAL prefix counts (equal to the reply's executed prefix).
    plan_events = [
        event for event in audit_events(bundle, session_id)
        if event["event_type"].startswith("plan_")
    ]
    assert [event["event_type"] for event in plan_events] == [
        "plan_start", "plan_point", "plan_stopped",
    ], plan_events
    point, terminal = plan_events[1], plan_events[2]
    assert point["result"] == reply["result"]
    assert point["metadata"].get("failure_reason") == reply["failure_reason"]
    assert point["metadata"]["step_index"] == reply["step_index"]
    assert terminal["event_type"] == "plan_stopped"
    assert terminal["result"] == "stopped"
    assert terminal["metadata"]["reason"] == "cancelled"
    assert terminal["metadata"]["resolved_count"] == len(reply["executed_prefix"])
    assert terminal["metadata"]["executed_count"] == sum(
        entry.get("kind") in ("executed", "dispatched_unverified")
        for entry in reply["executed_prefix"]
    )


# --- DELTA 1 #11: a whitespace-only action expected_effect is not a statement ---------------------


def test_a_whitespace_action_expected_effect_is_not_a_statement() -> None:
    """DELTA 1 #11: the #14 rule extends to the ACTION-level ``expected_effect`` —
    a whitespace-only string is an ABSENCE of a verification statement, so the wire
    gate demands the point's ``arrival_condition`` exactly as if it were missing."""
    for blank in ("", "   "):
        payload = {
            "transition_duration_ms": 0,
            "point_timeout_ms": 5_000,
            "settle_ms": 0,
            "on_failure": "stop",
            "on_uncertain": "continue",
            "points": [
                {
                    "id": "a",
                    "x": 1,
                    "y": 2,
                    "actions": [{"action": "click", "expected_effect": blank}],
                }
            ],
        }
        with pytest.raises(ValueError) as excinfo:
            srv_planning_plan.coerce_tolerant_plan(payload)
        assert "arrival_condition" in str(excinfo.value), blank


# --- DELTA 2 N3: a failed last step PARKS — continue gives the terminal, replace retries ----------


def _failed_last_point_plan() -> dict[str, Any]:
    """A two-point plan whose LAST point deterministically fails (a window_state
    expectation that never appears)."""
    return {
        "points": [
            {"id": "first", "x": 10, "y": 10, "actions": [{"action": "click"}]},
            {
                "id": "last",
                "x": 20,
                "y": 20,
                "actions": [
                    {"action": "keypress", "keys": ["enter"], "expected_effect": "open Calculator"}
                ],
            },
        ]
    }


async def test_failed_last_step_parks_continue_gives_the_terminal_reply(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 2 N3(i): the walk's last step fails -> the reply PARKS (blocked, lease
    active, §1.3 retry=replace); ``continue`` then delivers the terminal reply —
    whole-plan verdict ``failed`` with ok false — and releases the lease exactly
    once. NO ok:true anywhere on the way."""
    backend = _windowed_backend()
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    first = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            approved=True,
            plan=wire_plan(_failed_last_point_plan()),
            include_screenshot_after=False,
        )
    )
    assert first["step_index"] == 0 and first["blocked"] is True
    parked = await _continue(session_id)  # the failed last step
    validate_reply(parked, "plan_step")
    assert parked["result"] == "failed" and parked["ok"] is False
    assert parked["blocked"] is True
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is not None
    terminal = await _continue(session_id)  # the explicit walk-end request
    validate_reply(terminal, "terminal_plan")
    assert terminal["result"] == "failed" and terminal["ok"] is False
    assert terminal["failure_reason"] == "verification_failed"
    assert terminal["blocked"] is False
    assert terminal["next_step"] is None
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is None
    assert srv_server_plan_surface._plan_guard.is_active(session_id) is False


async def test_failed_last_step_parks_replace_retries_and_can_complete(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 2 N3(ii): §1.3 retry = replace — the parked failed walk can be
    REPLACED; the replacement runs under the inherited approval and completes with
    the whole-plan aggregate verdict on its final step."""
    backend = _windowed_backend()
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await srv_server_execute.computer_execute(
        session_id,
        "done",
        approved=True,
        plan=wire_plan(_failed_last_point_plan()),
        include_screenshot_after=False,
    )
    await _continue(session_id)  # the failed last step parks the lease
    replaced = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(CROSSING_PLAN),
            plan_action="replace",
            include_screenshot_after=False,
        )
    )
    assert replaced["replaced_plan_id"]  # the failed plan was closed and replaced
    # the replacement's steps: step 0 parks, step 1 verifies itself -> the SAME call
    # ends the walk with the whole-plan aggregate verdict (verified here: the
    # replacement is all crossing points and ran every move)
    assert replaced["step_index"] == 0 and replaced["blocked"] is True
    second = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan_action="continue", include_screenshot_after=False
        )
    )
    validate_reply(second, "terminal_plan")
    assert second["result"] == "verified" and second["ok"] is True
    assert second["blocked"] is False
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is None
    assert srv_server_plan_surface._plan_guard.is_active(session_id) is False


# --- DELTA 2 N1: the walk-ending reply keeps the completing step's data ---------------------------


async def test_modal_on_the_last_point_reaches_the_parked_and_terminal_replies(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 2 N1: the modal fires on the LAST (only) point's ACTION item — the
    parked last-step reply carries the event (top level + aggregate), and the
    later terminal reply still carries the retained event (value identity)."""
    from computer_use_mcp.desktop.interference import MODAL_DIALOG

    backend = ModalOnSecondDispatch(flip=True)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    plan = {
        "points": [
            {
                "id": "only",
                "x": 30,
                "y": 40,
                "actions": [{"action": "click"}],
                "arrival_condition": "the menu is open",
            }
        ]
    }
    step = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan=wire_plan(plan), include_screenshot_after=False
        )
    )
    assert step["blocked"] is True and step["step_index"] == 0
    assert step["interference_events"], f"the last point's modal was dropped: {sorted(step)}"
    event = step["interference_events"][0]
    assert event.startswith(MODAL_DIALOG)
    assert event in step["plan"]["interference_events"], step["plan"]
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is not None
    # the condition is dismissed; the terminal reply keeps the retained event
    backend.system_dialog = None
    terminal = await _drive_plan_stop(session_id)
    assert event in terminal["interference_events"], terminal.get("interference_events")
    assert event in terminal["plan"]["interference_events"]
    assert srv_server_plan_surface._plan_guard.is_active(session_id) is False


async def test_walk_ending_reply_carries_the_final_step_event_frame_and_disclosure(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 2 N1 + WP8 M1 (VAL-WP7b-03): the walk-ending reply carries the
    COMPLETING STEP'S OWN guard event — the modal fires on the FINAL step's
    dispatch (#3, the last point's crossing move) with a CLEAN window state
    before it, so the lease carries no event from any earlier step and the
    completing step's outcome is the ONLY possible source. The reply also keeps
    the step's registered frame (image_id, fetchable) and its per-item
    disclosure — while the VERDICT is the whole-plan aggregate (the earlier
    uncertain point caps it at ``unknown``, ok false)."""
    from computer_use_mcp.desktop.interference import MODAL_DIALOG

    from computer_use_mcp.core.models import WindowInfo

    target = WindowInfo(hwnd=1, pid=100, process_name="EXCEL.EXE", window_class="XLMAIN", title="Book1 - Excel")
    backend = ModalOnFinalDispatch(fire_on=3, flip=True, active_window=target, width=1024, height=800)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    plan = {
        "points": [
            {
                "id": "first",
                "x": 30,
                "y": 40,
                "actions": [{"action": "click"}],
                "arrival_condition": "the menu is open",
            },
            {"id": "last", "x": 50, "y": 60, "actions": []},
        ]
    }
    reply = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(plan),
            approved=True,
            include_screenshot_after=True,
        )
    )
    # step 0 parks and its dispatches (1 = move, 2 = click) saw a CLEAN window:
    # the lease carries NO interference event from it
    assert reply["blocked"] is True and reply["step_index"] == 0
    assert not reply.get("interference_events"), reply.get("interference_events")
    assert not backend.system_dialog
    assert backend.dispatches == 2
    # the walk ends on the continue: the last step's OWN move dispatch (#3) fires
    # the modal AFTER the dispatch, the move still verifies, so the SAME call ends
    # the walk with the aggregate verdict
    reply = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan_action="continue", include_screenshot_after=True
        )
    )
    assert reply["next_step"] is None and reply["blocked"] is False
    validate_reply(reply, "terminal_plan")
    # N1: the verdict is the AGGREGATE (the first point's stated expectation
    # abstained on a pixel change -> unknown, ok false) ...
    assert reply["result"] == "unknown" and reply["ok"] is False
    assert reply["failure_reason"] == "verification_uncertain"
    # WP7b nit (decided): the nested aggregate status agrees with the top level
    # (unknown = finished with nothing pending, not "blocked")
    assert reply["plan"]["status"] == "unknown"
    assert reply["plan"]["points"][0]["status"] == "uncertain"
    assert reply["plan"]["points"][1]["status"] == "completed"
    # ... and the COMPLETING STEP'S OWN event is the only source: the lease was
    # empty before this step, so this event can only have ridden its outcome
    assert reply["interference_events"], sorted(reply)
    assert all(e.startswith(MODAL_DIALOG) for e in reply["interference_events"])
    assert any(
        e.startswith(MODAL_DIALOG) for e in reply["plan"]["interference_events"]
    )
    image_id = reply.get("image_id")
    assert image_id, sorted(k for k in reply if "image" in k or "evidence" in k)
    fetched = await srv_server_fetch.computer_fetch_image(session_id, image_id)
    assert isinstance(fetched, list) and fetched[0].type == "text"  # the frame is fetchable
    assert json.loads(fetched[0].text)["image_id"] == image_id
    assert srv_server_plan_surface._plan_guard.active_lease(bundle) is None


# --- WP8 DELTA 1 (VAL-WP8-01): F6 + F15 ---------------------------------------------------


async def test_a_second_plan_on_a_live_lease_is_a_rejected_plan_lease_refusal(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F15: the first-call plan_lease install failure is a REJECTED reply — the
    closed result token, ok false, and the typed code as the first reason (the
    same envelope as the other plan_lease refusals; assert_plan_refusal)."""
    from reply_contract import validate_reply

    backend = ScriptedBackend(flip=True)
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await srv_server_execute.computer_execute(
        session_id, "done", plan=wire_plan(STATIC_PLAN), approved=True, include_screenshot_after=False
    )
    refusal = await srv_server_execute.computer_execute(
        session_id, "done", plan=wire_plan(CROSSING_PLAN), approved=True, include_screenshot_after=False
    )
    assert isinstance(refusal, dict), type(refusal)
    assert refusal["result"] == "rejected" and refusal["ok"] is False
    assert refusal["reasons"] and refusal["reasons"][0] == "plan_lease"
    assert refusal["error"] == "plan_lease"
    validate_reply(refusal, "plan_refusal")


def test_the_exception_builder_names_any_limit_and_agrees() -> None:
    """F6: EVERY limit trip (here ``min_screenshot_interval_ms`` — a non-max_*
    limit no end-to-end path can trip today, PERF-004 C2 made the pacing
    non-raising) gets the agreeing error / limit / cutoff / failure_reason."""
    from types import SimpleNamespace

    from computer_use_mcp.planning.plan import (
        PlanLease,
        coerce_tolerant_plan,
        normalize_plan,
        plan_step_slices,
    )

    model = coerce_tolerant_plan(wire_plan(CROSSING_PLAN))
    specs, context = normalize_plan(model)
    lease = PlanLease(
        model=model, specs=specs, context=context, steps=plan_step_slices(specs, context)
    )
    bundle = SimpleNamespace(
        extra={},
        enforcer=SimpleNamespace(limits=SimpleNamespace(max_actions=5), _actions=0),
        state=SimpleNamespace(max_steps=5, step_count=0),
    )
    response = srv_server_plan_surface._plan_exception_response(
        bundle,
        lease,
        step_index=0,
        result="failed",
        reason="min_screenshot_interval_ms: Screenshot interval below 5ms.",
        blocked=True,
        failure_reason="limit_exceeded",
        limit_name="min_screenshot_interval_ms",
    )
    assert response["failure_reason"] == "limit_exceeded"
    assert response["error"] == "limit_exceeded"
    assert response["limit"] == "min_screenshot_interval_ms"
    assert response["cutoff"]["limit_name"] == "min_screenshot_interval_ms"
    for field in ("limit_name", "required", "available", "executed_count", "resolved_count"):
        assert field in response["cutoff"], field
