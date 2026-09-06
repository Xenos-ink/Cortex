from __future__ import annotations

import base64
import io

from PIL import Image

from computer_use_mcp.agent import ComputerUseAgent
from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.models import AgentDecision, GroundedAction, Observation, SessionState
from computer_use_mcp.validator import GroundingValidator
from computer_use_mcp.verification import VerificationEngine


class SequenceProvider:
    def __init__(self, decisions: list[AgentDecision]) -> None:
        self.decisions = iter(decisions)

    async def decide(self, goal: str, observation: Observation, history: list[str]) -> AgentDecision:
        return next(self.decisions)


class ChangingBackend(FakeComputerBackend):
    def __init__(self) -> None:
        super().__init__()
        self._counter = 0

    def observe(self) -> Observation:
        self._counter += 1
        base = super().observe()
        if self._counter > 1:
            image = Image.new("RGB", (base.width, base.height), "black")
            output = io.BytesIO()
            image.save(output, format="PNG")
            base.image_base64 = base64.b64encode(output.getvalue()).decode("ascii")
        return base


def test_validator_rejects_out_of_bounds_coordinates() -> None:
    observation = FakeComputerBackend(width=100, height=100).observe()
    state = SessionState(session_id="test", min_confidence=0.0)
    result = GroundingValidator().validate(
        GroundedAction(action="click", point={"x": 100, "y": 20}, confidence=1.0), observation, state
    )
    assert result.valid is False
    assert "outside" in result.reasons[0]


def test_validator_rejects_coordinate_space_mismatch() -> None:
    observation = Observation(
        image_base64=FakeComputerBackend().observe().image_base64,
        width=1920,
        height=1080,
        input_width=1536,
        input_height=864,
        coordinate_scale_x=0.8,
        coordinate_scale_y=0.8,
        coordinate_space_verified=False,
    )
    result = GroundingValidator().validate(
        GroundedAction(action="click", point={"x": 100, "y": 100}, confidence=1.0),
        observation,
        SessionState(session_id="test", min_confidence=0.0),
    )
    assert result.valid is False
    assert "coordinate spaces differ" in result.reasons[0]


def test_verification_detects_visual_change() -> None:
    backend = ChangingBackend()
    before = backend.observe()
    after = backend.observe()
    result = VerificationEngine().compare(before, after, expected_change="window changes")
    assert result.changed is True
    assert result.verified is True


async def test_dry_run_reports_not_verified_and_never_executes() -> None:
    backend = FakeComputerBackend()
    action = GroundedAction(action="wait", delta=1, confidence=1.0)
    provider = SequenceProvider([
        AgentDecision(status="action", action=action),
        AgentDecision(status="done", summary="done"),
    ])
    agent = ComputerUseAgent(backend, provider)
    results = await agent.run("wait", SessionState(session_id="test", dry_run=True), approval=lambda *_: True)
    assert results[0].ok is True
    assert results[0].verification is not None
    assert results[0].verification.verified is False
    assert backend.executed == []


async def test_interactive_approval_is_consumed_per_action() -> None:
    backend = ChangingBackend()
    action = GroundedAction(action="click", point={"x": 10, "y": 10}, confidence=1.0)
    provider = SequenceProvider([
        AgentDecision(status="action", action=action),
        AgentDecision(status="done", summary="done"),
    ])
    approvals = 0

    def approve_once(*_) -> bool:
        nonlocal approvals
        approvals += 1
        return approvals == 1

    state = SessionState(session_id="test", dry_run=False, min_confidence=0.0)
    results = await ComputerUseAgent(backend, provider).run("click", state, approval=approve_once)
    assert approvals == 1
    assert results[0].ok is True
