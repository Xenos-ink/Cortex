"""Batch-7 permanent regressions: the queue's ``"error"`` stop reason is classified.

Ported from the dev tree (``test_batch7_failure_classification.py``), adapted to
the stable tree: the classification tables and the classifier live in
``computer_use_mcp.plan`` (re-exported through ``server`` — plan.py imports
nothing from server.py, the WP7a AST pin), and the step surface uses the unified
reply contract (§1.1 ok rule; §1.3 step token set). Nothing else changed: the
``"error"`` stop reason classifies ``execution_error``, ``unclassified`` stays a
reachable documented fallback, and the pre-existing reason for every other
signal is unchanged.

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

import computer_use_mcp.server.lifecycle as srv_server_lifecycle
import computer_use_mcp.server.wait_machine as srv_server_wait_machine
import computer_use_mcp.server.wait as srv_server_wait
import computer_use_mcp.server.plan_surface as srv_server_plan_surface
import computer_use_mcp.server.execute as srv_server_execute
import computer_use_mcp.planning.plan as srv_planning_plan
from computer_use_mcp.core.models import GroundedAction, WindowInfo
from computer_use_mcp.planning.plan import PLAN_FAILURE_REASONS, plan_failure_reason
from computer_use_mcp.core.state import SessionRegistry


def wire_plan(plan):
    from plan_explicitness import complete_plan

    return complete_plan(plan)


# The point's FIRST action lands, its SECOND (follow-up) action explodes: the
# queue's first item still succeeded, so the step cannot be classified from the
# outcome kind and the only remaining signal is the stop reason. The FIRST click
# keeps an unstated expectation and still verifies (pixel change), so the step's
# classification is decided by the FOLLOW-UP's dispatch error.
FOLLOW_UP_PLAN: dict[str, Any] = {
    "points": [
        {
            "id": "fault",
            "x": 100,
            "y": 100,
            "actions": [
                {"action": "click"},
                {"action": "click", "expected_effect": "the second click landed"},
            ],
        },
        {
            "id": "next",
            "x": 300,
            "y": 300,
            "actions": [{"action": "click", "expected_effect": "the next point landed"}],
        },
    ]
}
# T2's exact reproduction: a single-point drag whose segment generator explodes
# after the third sample, i.e. mid-drag inside a plan step.
DRAG_PLAN: dict[str, Any] = {
    "points": [
        {
            "id": "pull",
            "x": 100,
            "y": 100,
            "actions": [
                {
                    "action": "drag",
                    "x2": 500,
                    "y2": 100,
                    "expected_effect": "the drag reached the handle",
                }
            ],
        }
    ]
}

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
    "step_number",
    "follow_ups_stopped_reason",
)
LEGACY_EVIDENCE_FIELDS = ("captures", "repositions")  # D-2: real on both backends


class FollowUpExplodesBackend(ScriptedBackend):
    """Land the first N backend dispatches, then raise on dispatch ``explode_on``."""

    def __init__(self, *, explode_on: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.explode_on = explode_on
        self.dispatches = 0

    def execute(self, action: GroundedAction, stop: Any = None) -> str:
        self.dispatches += 1
        if self.dispatches == self.explode_on:
            raise RuntimeError("uv7 synthetic follow-up explosion")
        return super().execute(action, stop)


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(srv_server_lifecycle, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(srv_server_lifecycle, "_bundles", {})
    monkeypatch.setattr(srv_server_wait_machine, "_bundles", srv_server_lifecycle._bundles)
    monkeypatch.setattr(srv_server_wait, "_bundles", srv_server_lifecycle._bundles)
    monkeypatch.setattr(srv_server_plan_surface, "_plan_guard", srv_planning_plan.PlanLeaseGuard())
    monkeypatch.setattr(srv_server_execute, "_plan_guard", srv_server_plan_surface._plan_guard)
    monkeypatch.setattr(srv_server_lifecycle, "_plan_guard", srv_server_plan_surface._plan_guard)
    yield


def _assert_legacy_surface(payload: dict[str, Any]) -> None:
    for field in LEGACY_STEP_FIELDS:
        assert field in payload, f"legacy field {field} vanished: {sorted(payload)}"
    evidence = payload["evidence_by_backend"]
    for field in LEGACY_EVIDENCE_FIELDS:
        assert field in evidence, f"legacy evidence field {field} vanished"
    assert payload["plan"]["plan_id"] == payload["plan_id"]


# --- (a) the gap: an exception in a step's follow-up action --------------------------------


async def test_follow_up_action_exception_is_not_unclassified(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The step still fails honestly; it now NAMES the failure instead of escalating."""
    # dispatch 1 = the in-plan move, 2 = the point's first action, 3 = its follow-up.
    backend = FollowUpExplodesBackend(explode_on=3, flip=True)
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan=wire_plan(FOLLOW_UP_PLAN), approved=True, include_screenshot_after=False
        )
    )

    kinds = [entry["kind"] for entry in payload["executed_prefix"]]
    assert kinds == ["executed", "executed", "error"], kinds
    assert "uv7 synthetic follow-up explosion" in payload["executed_prefix"][-1]["message"]

    assert payload["failure_reason"] != "unclassified", payload["failure_reason"]
    assert payload["failure_reason"] == "execution_error", payload["failure_reason"]
    assert payload["failure_reason"] in PLAN_FAILURE_REASONS

    # §1.1 ok rule (adapted from the dev legacy rule): a failed step is ok:false.
    assert payload["result"] == "failed"
    assert payload["ok"] is False
    assert payload["blocked"] is True
    assert payload.get("error") is None
    assert payload["follow_ups_stopped_reason"] == "error"
    _assert_legacy_surface(payload)
    not_executed = payload["not_executed"]
    assert [entry["step_index"] for entry in not_executed] == [1], not_executed
    for entry in not_executed:
        assert set(entry) == {"step_index", "point", "action_types", "reason"}, entry
        assert entry["reason"] == "execution_error"
        assert set(entry["point"]) == {"id", "x", "y"}
    assert [entry["step_index"] for entry in payload["remaining"]] == [1]
    assert payload["remaining"][0]["reason"] == "previous_step_failed"
    assert srv_server_plan_surface._plan_guard.is_active(session_id)
    json.dumps(payload)


async def test_follow_up_action_exception_keeps_every_legacy_key(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No legacy key disappears, only the token changes."""
    backend = FollowUpExplodesBackend(explode_on=3, flip=True)
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan=wire_plan(FOLLOW_UP_PLAN), approved=True, include_screenshot_after=False
        )
    )
    assert set(LEGACY_STEP_FIELDS) <= set(payload), sorted(payload)


# --- (b) T2's exact reproduction: a mid-drag fault inside a plan step ------------------------


async def test_mid_drag_fault_in_a_step_is_classified_not_unclassified(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2 t3.11.4 verbatim — same plan, same fault, now an explicit reason."""
    from computer_use_mcp.desktop import backend as backend_module

    from computer_use_mcp.desktop import fake_backend as fake_module

    real_segments = fake_module._drag_segment_points

    def exploding(start: Any, end: Any) -> Any:
        for index, point in enumerate(real_segments(start, end)):
            if index >= 3:
                raise RuntimeError("synthetic mid-drag fault inside a plan step")
            yield point

    # C-3: ScriptedBackend extends FakeComputerBackend, whose execute reads the
    # fake_backend binding — the fault must be planted THERE.
    monkeypatch.setattr(fake_module, "_drag_segment_points", exploding)
    backend = ScriptedBackend(flip=True)
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan=wire_plan(DRAG_PLAN), approved=True, include_screenshot_after=False
        )
    )
    assert payload["failure_reason"] == "execution_error", payload["failure_reason"]
    assert payload["result"] == "failed"
    assert payload["blocked"] is True
    assert payload.get("error") is None
    assert payload["follow_ups_stopped_reason"] == "error"
    assert any(
        entry["kind"] == "error" and "mid-drag fault" in str(entry.get("message"))
        for entry in payload["executed_prefix"]
    ), payload["executed_prefix"]
    assert srv_server_plan_surface._plan_guard.is_active(session_id)


# --- (c) the classification itself: one table entry, existing vocabulary --------------------


def test_error_stop_reason_maps_to_execution_error_in_one_table() -> None:
    from computer_use_mcp.planning import plan as plan_module

    tables = (
        plan_module._PLAN_SPECIFIC_STOP_REASON_TO_FAILURE_REASON,
        plan_module._PLAN_STOP_REASON_TO_FAILURE_REASON,
    )
    mapped = [table["error"] for table in tables if "error" in table]
    assert mapped == ["execution_error"], mapped
    assert "execution_error" in PLAN_FAILURE_REASONS


def test_unclassified_stays_the_documented_fallback() -> None:
    """Scope guard: a state the vocabulary genuinely does not name still escalates."""
    assert "unclassified" in PLAN_FAILURE_REASONS

    class _UnnamedOutcome:
        kind = None
        follow_ups_stopped_reason = "a_stop_reason_no_table_names"

    assert (
        plan_failure_reason(_UnnamedOutcome(), [], result="failed", error=None)
        == "unclassified"
    )


def test_every_classification_token_is_inside_the_closed_vocabulary() -> None:
    from computer_use_mcp.planning import plan as plan_module

    for table in (
        plan_module._PLAN_ERROR_TO_FAILURE_REASON,
        plan_module._PLAN_OUTCOME_KIND_TO_FAILURE_REASON,
        plan_module._PLAN_SPECIFIC_STOP_REASON_TO_FAILURE_REASON,
        plan_module._PLAN_STOP_REASON_TO_FAILURE_REASON,
    ):
        for key, value in table.items():
            assert value in PLAN_FAILURE_REASONS, (key, value)


def test_the_same_error_literal_agrees_in_both_places_it_appears() -> None:
    """``"error"`` as an outcome kind and as a stop reason mean the same thing."""
    from computer_use_mcp.planning import plan as plan_module

    assert plan_module._PLAN_OUTCOME_KIND_TO_FAILURE_REASON["error"] == "execution_error"

    class _ErrorOutcome:
        kind = None  # the step's first item succeeded: only the stop reason speaks
        follow_ups_stopped_reason = "error"

    assert (
        plan_failure_reason(_ErrorOutcome(), [], result="failed", error=None)
        == "execution_error"
    )


def test_verified_step_still_reports_no_failure() -> None:
    class _OkOutcome:
        kind = "executed"
        follow_ups_stopped_reason = None

    assert plan_failure_reason(_OkOutcome(), [], result="verified", error=None) is None


# --- (d) no other reason moved -------------------------------------------------------------


def test_pre_existing_classifications_are_unchanged() -> None:
    """The dev Batch-6/7 entries keep their exact tokens (stable table frozen)."""
    from computer_use_mcp.planning import plan as plan_module

    # F8: the error table carries ONLY keys the classifier actually receives
    # (``cancelled``/``invalid_action`` are passed explicitly by the exception
    # builder and never classified).
    assert plan_module._PLAN_ERROR_TO_FAILURE_REASON == {
        "limit_exceeded": "limit_exceeded",
        "action_error": "execution_error",
    }
    assert plan_module._PLAN_OUTCOME_KIND_TO_FAILURE_REASON == {
        "safety_denied": "safety_denied",
        "approval_required": "approval_required",
        "digest_surprise": "digest_surprise",
        "error": "execution_error",
        "rejected": "rejected",
    }
    assert plan_module._PLAN_SPECIFIC_STOP_REASON_TO_FAILURE_REASON == {
        "safety_denied": "safety_denied",
        "limit_exceeded": "limit_exceeded",
        "modal_dialog": "modal_dialog",
        "focus_drifted": "focus_drifted",
    }
    assert plan_module._PLAN_STOP_REASON_TO_FAILURE_REASON == {
        "verification_failed": "verification_failed",
        "invalid_follow_up": "invalid_follow_up",
        "policy_stop": "policy_stop",
        "no_post_action_observation": "no_post_action_observation",
        "error": "execution_error",
        "rejected": "rejected",
        "approval_required": "approval_required",
        "digest_surprise": "digest_surprise",
        # stable-only: the queue's ensure-app guard stop (dev has no such literal).
        "ensure_app_failed": "verification_uncertain",
        # stable-only (WP13 A): the follow-up chain stops on the host time budget
        # instead of being killed by the host (dev has no such literal).
        "time_budget": "verification_uncertain",
        "focus_taken_by": "focus_drifted",
        "focus_identity_unknown": "focus_drifted",
        "stuck_modifier": "rejected",
        "target_gone": "rejected",
    }
    assert PLAN_FAILURE_REASONS == frozenset(
        {
            "verification_failed",
            "verification_uncertain",
            "execution_error",
            "limit_exceeded",
            "cancelled",
            "safety_denied",
            "approval_required",
            "digest_surprise",
            "rejected",
            "invalid_action",
            "invalid_follow_up",
            "policy_stop",
            "modal_dialog",
            "focus_drifted",
            "no_post_action_observation",
            "unclassified",
        }
    )


async def test_the_pre_batch6_scenarios_still_classify_as_before(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end re-run of Batch 6's (b) first-action exception: still ``execution_error``."""

    class BrokenBackend(ScriptedBackend):
        def execute(self, action: GroundedAction, stop: Any = None) -> str:
            raise RuntimeError("uv7 synthetic explosion")

    backend = BrokenBackend(flip=True)
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(
                {
                    "points": [
                        {
                            "id": "first",
                            "x": 10,
                            "y": 20,
                            "actions": [
                                {
                                    "action": "click",
                                    "expected_effect": "the click landed",
                                }
                            ],
                        }
                    ]
                }
            ),
            approved=True,
            include_screenshot_after=False,
        )
    )
    assert payload["failure_reason"] == "execution_error"
    assert payload["error"] == "action_error"


async def test_no_flip_backend_verification_failure_still_classifies(
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
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(
                {
                    "points": [
                        {
                            "id": "failure",
                            "x": 10,
                            "y": 20,
                            "actions": [
                                {
                                    "action": "keypress",
                                    "keys": ["enter"],
                                    "expected_effect": "open Calculator",
                                }
                            ],
                        },
                        {
                            "id": "next",
                            "x": 30,
                            "y": 40,
                            "actions": [
                                {
                                    "action": "click",
                                    "expected_effect": "the next point landed",
                                }
                            ],
                        },
                    ]
                }
            ),
            approved=True,
            include_screenshot_after=False,
        )
    )
    assert payload["failure_reason"] == "verification_failed"
    not_executed = payload["not_executed"]
    assert not_executed
    assert {entry["reason"] for entry in not_executed} == {"verification_failed"}


# --- (e) N3 (VAL-WP8-02): never-dispatched terminal prefixes are classified -----


def test_not_executed_and_dry_run_only_prefixes_are_classified() -> None:
    """A terminal prefix of only never-dispatched work — a not_executed entry
    WITHOUT an approval hold, or a dry_run entry — classifies as the cut token
    ``cancelled`` (existing vocabulary; no token invented), never
    ``unclassified``. An approval hold still wins (checked first)."""
    _not_executed = {
        "kind": "executed",
        "result": "not_executed",
        "requires_approval": False,
    }
    _dry_run = {"kind": "executed", "result": "dry_run", "requires_approval": False}
    assert plan_failure_reason(None, [dict(_not_executed)], result="failed") == "cancelled"
    # VAL-WP8-03 (Commander decision): a dry_run walk is DELIBERATE, not a
    # cancellation — the same token the step level gives a dry-run step.
    assert (
        plan_failure_reason(None, [dict(_dry_run)], result="failed")
        == "no_post_action_observation"
    )
    held = dict(_not_executed, requires_approval=True)
    assert plan_failure_reason(None, [held], result="failed") == "approval_required"


async def test_a_dry_run_walk_terminal_is_classified_not_unclassified(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: a plan walked in a dry-run session (every entry validated,
    deliberately not dispatched) reaches a terminal reply whose failure_reason
    is the cut token, never ``unclassified``."""
    backend = ScriptedBackend(flip=True)
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, backend=backend, dry_run=True, require_approval=False, limits=FAST_LIMITS
    )
    first = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            plan=wire_plan(
                {
                    "points": [
                        {"id": "only", "x": 10, "y": 20, "actions": [{"action": "click"}]}
                    ]
                }
            ),
            approved=True,
            include_screenshot_after=False,
        )
    )
    assert first["failure_reason"] in (PLAN_FAILURE_REASONS | {None})
    terminal = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "done", plan_action="continue", include_screenshot_after=False
        )
    )
    # VAL-WP8-03 (Commander decision): a dry-run walk is DELIBERATE — the same
    # token the step level gives a dry-run step, not "cancelled".
    assert terminal["failure_reason"] == "no_post_action_observation", terminal["failure_reason"]
    assert terminal["failure_reason"] in PLAN_FAILURE_REASONS
