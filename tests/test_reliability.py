from __future__ import annotations

import base64
import io

from PIL import Image

from computer_use_mcp.agent import ComputerUseAgent
from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.models import GroundedAction, Observation, SessionState
from computer_use_mcp.validator import GroundingValidator
from computer_use_mcp.verification import VerificationEngine


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
    """RETARGETED (loop removal): the dry-run guarantee is pinned on the direct surface —
    ``run_single`` reports an executed-shaped stub whose verification is honestly NOT
    verified, and no input is ever dispatched."""
    backend = FakeComputerBackend()
    agent = ComputerUseAgent(backend, object())
    outcome = await agent.run_single(
        SessionState(session_id="test", dry_run=True),
        GroundedAction(action="wait", delta=1, confidence=1.0),
    )
    assert outcome.kind == "executed"
    assert outcome.result is not None and outcome.result.ok is True
    assert outcome.result.verification is not None
    assert outcome.result.verification.verified is False
    assert backend.executed == []


async def test_interactive_approval_is_required_per_direct_call() -> None:
    """RETARGETED (loop removal): the per-action approval CALLBACK was loop machinery.
    The direct surface gates the same interactive risk with the per-call ``approved``
    flag: without it an interactive action is refused (approval_required, zero
    dispatches); with it the action runs. Every call is judged independently."""
    backend = ChangingBackend()
    action = GroundedAction(action="click", point={"x": 10, "y": 10}, confidence=1.0)
    agent = ComputerUseAgent(backend, object())
    state = SessionState(session_id="test", dry_run=False, min_confidence=0.0)

    refused = await agent.run_single(state, action)
    assert refused.kind == "approval_required"
    assert refused.requires_approval is True
    assert backend.executed == []

    executed = await agent.run_single(state, action, approved=True)
    assert executed.kind == "executed"
    assert len(backend.executed) == 1
