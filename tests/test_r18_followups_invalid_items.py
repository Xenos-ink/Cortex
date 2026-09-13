"""R-18: queue items that are ActionSpec-valid but GroundedAction-invalid get the
typed ``invalid_action`` rejection — never an uncaught pydantic ValidationError.

ROADMAP R-18 (red-team finding, MEDIUM): a target-less ``focus_window``, a
half-specified ``drag`` (one endpoint missing), or a 1-key ``hotkey`` passes the
``computer_execute`` boundary's ``ActionSpec`` validation and then blew up INSIDE
the queue (``agent.py:_run_action_queue`` -> ``ActionSpec.to_grounded()``) with a
raw pydantic ``ValidationError``. The effect was fail-closed (zero items dispatch)
but the host received an unstructured error. The contract:

- each malformed class returns the typed ``invalid_action`` rejection with
  teach-in-text hints listing the valid shapes;
- ZERO uncaught ValidationError on every path (the server boundary pre-validates;
  the agent queue guard covers direct ``run_single`` callers);
- queue stop/continue semantics are otherwise unchanged (a valid batch still runs
  to completion; the malformed batch still dispatches nothing).
"""

from __future__ import annotations

from typing import Any

import pytest
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    SessionRegistry,
    execute_payload,
    executed_summary,
    make_session,
)

from computer_use_mcp import server
from computer_use_mcp.agent import ComputerUseAgent
from computer_use_mcp.models import ActionSpec, ActionType, GroundedAction
from computer_use_mcp.server import ACTION_VOCABULARY


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Module-local copy of the shared ``fresh_server`` fixture (same body): a fresh
    bounded registry/bundles + per-test audit dir for full session isolation. Kept
    local so test signatures never shadow an imported name."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    return server


async def _execute_with_followups(
    session_id: str, primary: dict[str, Any], follow_ups: list[dict[str, Any]]
) -> dict[str, Any]:
    return execute_payload(
        await server.computer_execute(
            session_id,
            approved=True,
            follow_ups=follow_ups,
            include_screenshot_after=False,
            **primary,
        )
    )


# --- the three malformed classes through the MCP boundary --------------------------------------


@pytest.mark.parametrize(
    ("follow_up", "hint_fragment"),
    [
        # target-less focus_window
        (
            {"action": "focus_window"},
            "focus_window requires a non-empty target window title",
        ),
        # half-specified drag: only the start endpoint
        (
            {"action": "drag", "x": 5, "y": 5},
            "BOTH endpoints",
        ),
        # 1-key hotkey
        (
            {"action": "hotkey", "keys": ["a"]},
            "2-12 key names",
        ),
    ],
)
async def test_malformed_follow_up_returns_typed_invalid_action_with_hints(
    fresh_server: Any,
    monkeypatch: pytest.MonkeyPatch,
    follow_up: dict[str, Any],
    hint_fragment: str,
) -> None:
    """Each malformed class -> typed ``invalid_action`` (never a bare pydantic error),
    with the valid shapes taught in the reasons; NOTHING dispatches."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch,
        backend=ScriptedBackend(flip=False),
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    payload = await _execute_with_followups(
        session_id, {"action": "click", "x": 10, "y": 10}, [follow_up]
    )
    assert payload["ok"] is False, payload
    assert payload["error"] == "invalid_action", payload
    assert "ValidationError" not in payload["message"], payload
    assert hint_fragment in " ".join(payload["reasons"]), payload
    # the teach-in-text contract: the full valid vocabulary rides the rejection
    assert ACTION_VOCABULARY in payload["message"], payload
    # fail-closed preserved: the queue never started, zero items dispatched
    assert executed_summary(backend) == [], payload


async def test_malformed_follow_up_never_starts_the_queue(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A VALID primary plus one malformed follow-up dispatches NOTHING (the legacy
    fail-closed effect is preserved; only the error surface became typed)."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch,
        backend=ScriptedBackend(flip=False),
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    payload = await _execute_with_followups(
        session_id,
        {"action": "click", "x": 10, "y": 10},
        [
            {"action": "keypress", "keys": ["ctrl", "a"]},  # valid item before the bad one
            {"action": "focus_window"},  # malformed: no target
        ],
    )
    assert payload["ok"] is False, payload
    assert payload["error"] == "invalid_action", payload
    assert executed_summary(backend) == [], payload


async def test_move_and_ensure_app_grounded_invalid_classes_also_teach(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same typed path covers the remaining GroundedAction validator classes:
    a point-less ``move`` and a target-less ``ensure_app``."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch,
        backend=ScriptedBackend(flip=False),
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    for follow_up, fragment in (
        ({"action": "move"}, "move needs both coordinates"),
        ({"action": "ensure_app"}, "ensure_app requires a non-empty target"),
    ):
        payload = await _execute_with_followups(
            session_id, {"action": "click", "x": 10, "y": 10}, [follow_up]
        )
        assert payload["error"] == "invalid_action", (follow_up, payload)
        assert fragment in " ".join(payload["reasons"]), (follow_up, payload)
    assert executed_summary(backend) == [], payload


# --- the agent-level guard (direct run_single callers) ------------------------------------------


async def test_agent_queue_guard_rejects_invalid_spec_without_exception(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct ``run_single`` callers (no server boundary) get a typed rejected
    outcome with ``invalid_follow_up`` — zero items dispatch, no exception escapes."""
    backend = ScriptedBackend(flip=False)
    _session_id, bundle, _backend, _ = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    agent: ComputerUseAgent = bundle.agent
    primary = GroundedAction(
        action=ActionType.CLICK,
        point={"x": 10, "y": 10},
        reason="R-18 pin",
        confidence=1.0,
    )
    outcome = await agent.run_single(
        bundle.state,
        primary,
        approved=True,
        follow_ups=[ActionSpec(action=ActionType.FOCUS_WINDOW)],  # no target
    )
    assert outcome.kind == "rejected", outcome
    assert outcome.follow_ups_stopped_reason == "invalid_follow_up", outcome
    assert outcome.result is None, outcome
    assert any("focus_window" in reason for reason in outcome.reasons), outcome
    assert len(outcome.follow_up_results or []) == 1, outcome
    entry = (outcome.follow_up_results or [])[0]
    assert entry["ok"] is False and entry["kind"] == "rejected", entry
    assert entry["index"] == 1, entry
    # zero dispatch: the guard sits before the pipeline loop
    assert executed_summary(backend) == [], outcome


async def test_agent_queue_guard_fires_before_any_dispatch_for_valid_primary(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard sits in the conversion loop (before the pipeline loop): even the
    PRIMARY action never dispatches when a later spec cannot be converted — the
    exact legacy fail-closed effect, now typed."""
    backend = ScriptedBackend(flip=False)
    _session_id, bundle, _backend, _ = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    agent: ComputerUseAgent = bundle.agent
    primary = GroundedAction(
        action=ActionType.KEYPRESS,
        keys=["ctrl", "a"],
        reason="R-18 pin",
        confidence=1.0,
    )
    outcome = await agent.run_single(
        bundle.state,
        primary,
        approved=True,
        follow_ups=[
            ActionSpec(action=ActionType.DRAG, x=5, y=5),  # half-specified drag
        ],
    )
    assert outcome.kind == "rejected", outcome
    assert outcome.follow_ups_stopped_reason == "invalid_follow_up", outcome
    assert "drag" in " ".join(outcome.reasons), outcome
    assert executed_summary(backend) == [], outcome


# --- queue semantics unchanged for valid batches ------------------------------------------------


async def test_valid_follow_ups_batch_still_runs_to_completion(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The R-18 validation is purely additive: a fully valid batch still executes
    every item and completes with ``follow_ups_stopped_reason=None``."""
    monkeypatch.delenv("CORTEX_QUEUE_STRICT_VERIFY", raising=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch,
        backend=ScriptedBackend(flip=True),
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    payload = await _execute_with_followups(
        session_id,
        {"action": "click", "x": 10, "y": 10},
        [
            {"action": "keypress", "keys": ["ctrl", "a"]},  # valid compound chord
            {"action": "wait", "delta": 1},  # valid: converts and executes cleanly
        ],
    )
    assert len(executed_summary(backend)) == 3, payload
    assert payload["follow_ups_stopped_reason"] is None, payload
    assert [item["action_type"] for item in payload["follow_up_results"]] == [
        "click",
        "keypress",
        "wait",
    ], payload
