"""WP1 / §1.4.2 (ORVEX-2026-108) — ``done`` and ``wait`` are MARKERS, not screen claims.

Ported from the dev copy's ``test_c9_done_is_a_marker.py`` (F2-4, decision D-6a) and
adapted to the unified reply contract and the v0.7.5 verification ladder:

- a marker replies ``ok: true`` with ``verification_method: "no_op_marker"``,
  confidence 1.0, and the top-level ``result`` token ``"marker"`` (§1.2);
- a stated ``expected_effect`` is NOT consulted — a marker cannot make an effect
  happen, so claiming to have checked would be a lie;
- a DIRECT marker costs exactly ONE capture (the post_action frame that fills the
  response image); a QUEUED marker reuses the previous item's post-action frame as
  its premise, skips the validate capture, and still takes its own fresh post-action
  frame so the NEXT item's premise check is not weakened;
- stable-tree delta (recorded): the dev copy's F2-3 late-repaint re-capture is
  WP4a scope, so the click control's capture list here is 3 (not 4) and the queued
  marker's post_action count is 2 (not 3).

Unchanged: ``done`` still dispatches nothing, still does not end the session, and
the plan surface still never dispatches it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from reply_contract import validate_reply
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    SessionRegistry,
    execute_payload,
    make_session,
)

import computer_use_mcp.server.lifecycle as srv_server_lifecycle
import computer_use_mcp.server.wait_machine as srv_server_wait_machine
import computer_use_mcp.server.wait as srv_server_wait
import computer_use_mcp.server.execute as srv_server_execute
from computer_use_mcp.pipeline.agent import _NO_OP_MARKER_KINDS, ComputerUseAgent
from computer_use_mcp.core.models import ActionSpec, ActionType, GroundedAction, Point


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(srv_server_lifecycle, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(srv_server_lifecycle, "_bundles", {})
    monkeypatch.setattr(srv_server_wait_machine, "_bundles", srv_server_lifecycle._bundles)
    monkeypatch.setattr(srv_server_wait, "_bundles", srv_server_lifecycle._bundles)
    yield


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        dry_run=False,
        stopped=False,
        allowed_windows=[],
        min_confidence=0.0,
        max_steps=100,
        step_count=0,
        require_approval=False,
        max_retries_per_action=1,
    )


async def _marker(
    spec: dict[str, Any], **kwargs: Any
) -> tuple[dict[str, Any], ScriptedBackend, list[str]]:
    backend = ScriptedBackend(flip=False)
    agent = _agent_for(backend)
    phases: list[str] = []
    original = agent._observe

    def traced(phase: str) -> Any:
        phases.append(phase)
        return original(phase)

    agent._observe = traced  # type: ignore[method-assign]
    outcome = await agent.run_single(_state(), _grounded(spec), approved=True, **kwargs)
    assert outcome.result is not None, outcome.reasons
    return outcome.result.model_dump(), backend, phases


def _agent_for(backend: Any) -> ComputerUseAgent:
    from computer_use_mcp.core.limits import Limits
    from computer_use_mcp.security.safety import SafetyPolicy
    from computer_use_mcp.core.state import StopToken, TaskState

    return ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        limits=Limits(min_screenshot_interval_ms=1, max_actions=100).validate(),
    )


def _grounded(spec: dict[str, Any]) -> GroundedAction:
    if "point" not in spec and ("x" in spec or "y" in spec):
        spec = {**spec, "point": Point(x=spec["x"], y=spec["y"])}
    return GroundedAction.model_validate(spec)


# --- the marker verdict -------------------------------------------------------------------------


async def test_a_bare_done_is_a_marker_verified_with_exactly_one_capture() -> None:
    """THE HEADLINE CASE. ``ok: true``, ``result:"marker"``, ``no_op_marker``, and ONE
    ``observe()`` — down from the three a click costs."""
    payload, backend, phases = await _marker({"action": "done"})
    assert payload["ok"] is True, payload
    assert payload["verification"]["outcome"] == "verified", payload["verification"]
    assert payload["verification"]["verification_method"] == "no_op_marker"
    assert payload["verification"]["confidence"] == 1.0
    assert payload["verification"]["changed"] is False
    # the response image still rides the one capture
    assert payload["screenshot_after_base64"]
    # exactly one capture, and it is the post-action one (which fills the image)
    assert phases == ["post_action"], phases
    assert len(backend.observed) == 1, len(backend.observed)
    # the marker dispatches its own action and nothing else
    assert [action.action.value for action in backend.executed] == ["done"]


async def test_a_bare_wait_is_a_marker_too() -> None:
    """``wait`` is the other marker kind: it sleeps, it does not claim a screen state,
    and it must not inherit a structurally-guaranteed ``uncertain`` either."""
    payload, _backend, phases = await _marker({"action": "wait", "delta": 0})
    assert payload["ok"] is True, payload
    assert payload["verification"]["outcome"] == "verified"
    assert payload["verification"]["verification_method"] == "no_op_marker"
    assert phases == ["post_action"], phases


async def test_the_marker_kinds_are_exactly_done_and_wait() -> None:
    assert _NO_OP_MARKER_KINDS == frozenset({ActionType.DONE, ActionType.WAIT})


async def test_a_done_with_a_stated_expected_effect_does_not_consult_it() -> None:
    """A marker cannot make an effect happen, so a stated effect is not evidence about
    it — the verdict is the marker's, unchanged by the prose, and the note never claims
    the effect was observed (§1.2 marker: expected_effect ignored)."""
    payload, _backend, phases = await _marker(
        {"action": "done", "expected_effect": "the task is complete"}
    )
    assert payload["ok"] is True
    assert payload["verification"]["verification_method"] == "no_op_marker"
    assert "the task is complete" not in payload["verification"]["note"]
    assert phases == ["post_action"], phases


async def test_a_click_is_untouched_by_the_marker_branch() -> None:
    """The control: a real screen claim still costs the full pipeline and still abstains
    on a static screen. (Stable-tree delta, recorded: the dev copy's F2-3 late-repaint
    re-capture is WP4a scope, so the click costs 3 captures here, not 4; what matters
    is that the marker branch never fires.)"""
    payload, _backend, phases = await _marker({"action": "click", "x": 10, "y": 10})
    assert payload["ok"] is False
    assert payload["verification"]["outcome"] == "uncertain"
    assert payload["verification"]["verification_method"] != "no_op_marker"
    assert phases == ["direct_request", "validate", "post_action"], phases


# --- inside a follow_ups batch ------------------------------------------------------------------


async def test_a_done_inside_a_batch_has_an_ok_true_entry() -> None:
    """The queue's per-item entry is what a driver reads for every item after the
    first, so the marker verdict — and its ``result:"marker"`` token — has to reach IT
    too (§1: identical envelope on every follow_up_results[i])."""
    backend = ScriptedBackend(flip=False)
    agent = _agent_for(backend)
    outcome = await agent.run_single(
        _state(),
        _grounded({"action": "click", "x": 10, "y": 10}),
        approved=True,
        follow_ups=[ActionSpec(action="done")],
    )
    assert outcome.result is not None
    entries = outcome.follow_up_results or []
    assert len(entries) == 2, entries
    assert entries[1]["action_type"] == "done"
    assert entries[1]["result"] == "marker", entries[1]
    assert entries[1]["ok"] is True, entries[1]
    assert entries[1]["verification_outcome"] == "verified", entries[1]
    # the click item is untouched and the batch still completed
    assert entries[0]["ok"] is False
    assert outcome.follow_ups_stopped_reason is None
    assert [action.action.value for action in backend.executed] == ["click", "done"]


async def test_a_queued_done_costs_exactly_one_capture() -> None:
    """One marker, one capture — inside a batch too. The queued marker takes NO
    premise capture (it reuses the previous item's post-action frame, per the queue's
    observe-reuse doctrine) and NO validate capture, and takes its own fresh
    post-action frame — which is what the NEXT queue item's premise is checked
    against, exactly as for any other action. Reusing the previous item's frame as its
    own post-action frame instead would hand the next item a two-captures-stale
    premise and trip the queue's digest check; that regression is pinned below.
    (Stable-tree delta, recorded: no F2-3 re-capture, so the click's own post_action
    capture plus the marker's = 2.)"""
    backend = ScriptedBackend(flip=False)
    agent = _agent_for(backend)
    phases: list[str] = []
    original = agent._observe

    def traced(phase: str) -> Any:
        phases.append(phase)
        return original(phase)

    agent._observe = traced  # type: ignore[method-assign]
    await agent.run_single(
        _state(),
        _grounded({"action": "click", "x": 10, "y": 10}),
        approved=True,
        follow_ups=[ActionSpec(action="done")],
    )
    # click: direct_request + validate + post_action; the queued marker: premise
    # reused, validate skipped, one post_action of its own.
    assert phases.count("direct_request") == 1, phases
    assert phases.count("validate") == 1, phases
    assert phases.count("post_action") == 2, phases


async def test_a_marker_does_not_stale_the_next_queue_items_premise() -> None:
    """The regression guard for the fix above: a ``wait`` marker in the middle of a
    batch must not make the FOLLOWING item's premise stale enough to trip the queue's
    digest/identity check — the batch survives."""
    backend = ScriptedBackend(flip=True)
    agent = _agent_for(backend)
    outcome = await agent.run_single(
        _state(),
        _grounded({"action": "click", "x": 10, "y": 10}),
        approved=True,
        follow_ups=[ActionSpec(action="wait", delta=0), ActionSpec(action="click", x=20, y=20)],
    )
    assert outcome.result is not None
    assert [action.action.value for action in backend.executed] == ["click", "wait", "click"]
    assert outcome.follow_ups_stopped_reason is None, outcome.follow_up_results


# --- through the tool surface --------------------------------------------------------------------


async def test_a_done_through_computer_execute_is_a_marker_ok_true(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend(flip=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(await srv_server_execute.computer_execute(session_id, "done"))
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "marker", payload
    assert payload["ok"] is True, payload
    assert payload["verification"]["outcome"] == "verified"
    assert payload["verification"]["verification_method"] == "no_op_marker"
    assert payload["verification"]["confidence"] == 1.0
    assert len(backend.observed) == 1, len(backend.observed)
    # the session is still live: `done` is a marker, not a terminator
    follow_up = execute_payload(
        await srv_server_execute.computer_execute(session_id, "done", include_screenshot_after=False)
    )
    validate_reply(follow_up, "flat_execute")
    assert follow_up["result"] == "marker"
    assert follow_up["ok"] is True
