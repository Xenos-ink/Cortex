"""WP1 / §1.4.3 (ORVEX-2026-108) — a dry run is NOT an execution, on every field.

Ported from the dev copy's ``test_dry_run_is_not_an_execution.py`` (F2-5, decision
D-7a) and adapted to the unified reply contract: the dry-run row carries
``ok=False``, the additive ``dry_run: true`` marker, ``verification_method:
"dry_run"``, a ``"cannot determine"`` (``uncertain``) verification — never the
phantom ``failed`` a never-dispatched action has not earned — and the top-level
``result`` token is ``"dry_run"`` (§1.2), with ``ok`` derived from that token
through the shared ok-rule.

The short-circuit is unchanged in position: it still returns BEFORE
``_backend_execute`` and before the post-action capture, so a dry run dispatches no
input and performs no post-action capture.
"""

from __future__ import annotations

import json
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

from computer_use_mcp import server


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    yield server


def _state(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "dry_run": True,
        "stopped": False,
        "allowed_windows": [],
        "min_confidence": 0.0,
        "max_steps": 100,
        "step_count": 0,
        "require_approval": False,
        "max_retries_per_action": 1,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# --- the payload ---------------------------------------------------------------------------------


async def test_a_dry_run_reports_not_ok_and_carries_the_marker() -> None:
    from computer_use_mcp.pipeline.agent import ComputerUseAgent
    from computer_use_mcp.core.limits import Limits
    from computer_use_mcp.core.models import GroundedAction, Point
    from computer_use_mcp.security.safety import SafetyPolicy
    from computer_use_mcp.core.state import StopToken, TaskState

    backend = ScriptedBackend(flip=True)  # a live world: any dispatch WOULD change pixels
    agent = ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        limits=Limits(min_screenshot_interval_ms=1, max_actions=100).validate(),
    )
    outcome = await agent.run_single(
        _state(), GroundedAction(action="click", point=Point(x=10, y=10), confidence=1.0), approved=True
    )
    assert outcome.result is not None
    payload = outcome.result.model_dump()
    assert payload["ok"] is False, payload
    assert payload["dry_run"] is True, payload
    # the phantom `failed` verification is gone with the contradiction: the row reads
    # UNCERTAIN ("cannot determine") at the dry_run tier (§1.2)
    assert payload["verification"]["outcome"] == "uncertain", payload["verification"]
    assert payload["verification"]["verified"] is False
    assert payload["verification"]["verification_method"] == "dry_run"
    # the honest note is unchanged
    assert payload["verification"]["note"] == (
        "DRY-RUN (no input dispatched): execution and post-action verification "
        "were not performed."
    )
    assert payload["message"].startswith("DRY-RUN (no input dispatched):")
    # no input was dispatched
    assert [action.action.value for action in backend.executed] == []


async def test_a_dry_run_performs_no_post_action_capture() -> None:
    from computer_use_mcp.pipeline.agent import ComputerUseAgent
    from computer_use_mcp.core.limits import Limits
    from computer_use_mcp.core.models import GroundedAction, Point
    from computer_use_mcp.security.safety import SafetyPolicy
    from computer_use_mcp.core.state import StopToken, TaskState

    backend = ScriptedBackend(flip=True)
    agent = ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        limits=Limits(min_screenshot_interval_ms=1, max_actions=100).validate(),
    )
    phases: list[str] = []
    original = agent._observe

    def traced(phase: str) -> Any:
        phases.append(phase)
        return original(phase)

    agent._observe = traced  # type: ignore[method-assign]
    await agent.run_single(
        _state(), GroundedAction(action="click", point=Point(x=10, y=10), confidence=1.0), approved=True
    )
    assert "post_action" not in phases, phases
    assert phases == ["direct_request", "validate"], phases


async def test_a_real_execution_is_not_marked_as_a_dry_run() -> None:
    """The other direction: the marker is a marker, and it never rides a real run."""
    from computer_use_mcp.pipeline.agent import ComputerUseAgent
    from computer_use_mcp.core.limits import Limits
    from computer_use_mcp.core.models import GroundedAction, Point
    from computer_use_mcp.security.safety import SafetyPolicy
    from computer_use_mcp.core.state import StopToken, TaskState

    backend = ScriptedBackend(flip=True)
    agent = ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        limits=Limits(min_screenshot_interval_ms=1, max_actions=100).validate(),
    )
    outcome = await agent.run_single(
        _state(dry_run=False),
        GroundedAction(action="click", point=Point(x=10, y=10), confidence=1.0),
        approved=True,
    )
    assert outcome.result is not None
    assert outcome.result.model_dump()["dry_run"] is False
    assert outcome.result.ok is True


# --- through the tool surface ---------------------------------------------------------------------


async def test_a_dry_run_through_computer_execute_is_not_ok(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend(flip=True, width=1024, height=800)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=True, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await server.computer_execute(session_id, "click", x=10, y=10, approved=True)
    )
    validate_reply(payload, "flat_execute")
    assert payload["result"] == "dry_run", payload
    assert payload["ok"] is False, payload
    assert payload["dry_run"] is True, payload
    assert payload["message"].startswith("DRY-RUN (no input dispatched):"), payload
    assert backend.executed == [], backend.executed
    # ok and the verification outcome agree through the shared ok-rule
    assert payload["ok"] is (payload["verification"]["outcome"] == "verified")


async def test_a_dry_run_executed_row_serialises_the_marker() -> None:
    """The marker must be on the wire, not only on the model: a host reads the JSON."""
    from computer_use_mcp.core.models import ExecutionResult, GroundedAction, Point, VerificationResult

    result = ExecutionResult(
        ok=False,
        action=GroundedAction(action="click", point=Point(x=1, y=2), confidence=1.0),
        message="DRY-RUN",
        verification=VerificationResult(outcome="uncertain", changed=False, note="DRY-RUN"),
        dry_run=True,
    )
    dumped = json.loads(result.model_dump_json())
    assert dumped["dry_run"] is True
    assert dumped["ok"] is False
