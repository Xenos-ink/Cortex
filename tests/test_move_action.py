"""MOVE action tests: cursor repositioning without a click.

The move is an additive ActionType member carrying a screenshot-space ``point`` that
repositions the cursor WITHOUT pressing. It grounds/validates/binds exactly like click
on the coordinate path (bounds + staleness + observation binding), but it is not an
interactive press: safety classifies it LOW and it never requires approval by itself,
and verification is a deterministic cursor-at-target predicate.
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError

import computer_use_mcp.backend as backend_module
from computer_use_mcp import server
from computer_use_mcp.agent import ComputerUseAgent
from computer_use_mcp.backend import (
    CoordinateSpaceError,
    FakeComputerBackend,
    InputBlockedError,
)
from computer_use_mcp.grounding import GroundingRouter, UnsupportedGroundingError
from computer_use_mcp.models import (
    ActionType,
    GroundedAction,
    MonitorInfo,
    Observation,
    Point,
    SessionState,
    WindowInfo,
)
from computer_use_mcp.provider import ProviderParseError, parse_decision
from computer_use_mcp.safety import RiskLevel, SafetyContext, SafetyPolicy
from computer_use_mcp.state import SessionRegistry, StopToken, TaskStopped
from computer_use_mcp.validator import (
    GroundingValidator,
    MissingObservationBindingError,
    StaleObservationError,
)
from computer_use_mcp.verification import VerificationEngine

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")


def move(point: tuple[int, int], **kwargs: Any) -> GroundedAction:
    kwargs.setdefault("confidence", 1.0)
    return GroundedAction(action="move", point={"x": point[0], "y": point[1]}, **kwargs)


class _StopOnNthCheck(StopToken):
    """Stop token that fires (raises) on its own ``n``-th ``ensure_live`` call."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self._remaining = n

    def ensure_live(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self.stop()
        super().ensure_live()


class _RecordingPyautogui:
    """Minimal pyautogui stand-in recording every input call (no real mouse movement)."""

    FailSafeException = type("FailSafeException", (Exception,), {})

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, *args))

    def moveTo(self, x: int, y: int) -> None:
        self._record("moveTo", x, y)

    def mouseDown(self, button: str = "left") -> None:
        self._record("mouseDown", button)

    def mouseUp(self, button: str = "left") -> None:
        self._record("mouseUp", button)

    def click(self, *args: object, **kwargs: object) -> None:
        self._record("click", *args)

    def write(self, *args: object, **kwargs: object) -> None:
        self._record("write", *args)

    def hotkey(self, *args: object, **kwargs: object) -> None:
        self._record("hotkey", *args)

    def scroll(self, *args: object, **kwargs: object) -> None:
        self._record("scroll", *args)


# The session-scoped ``real_backend`` fixture comes from tests/conftest.py.


# --- models: move requires a point --------------------------------------------------------------


def test_move_action_member_value() -> None:
    assert ActionType.MOVE.value == "move"
    assert ActionType("move") is ActionType.MOVE


def test_move_requires_point() -> None:
    ok = move((30, 40))
    assert ok.point == Point(x=30, y=40)
    with pytest.raises(ValidationError, match="point"):
        GroundedAction(action="move")


def test_move_point_keeps_legacy_point_bounds() -> None:
    with pytest.raises(ValidationError):
        GroundedAction(action="move", point={"x": -9_000, "y": 0})


# --- FakeComputerBackend: move execution contract ------------------------------------------------


def test_fake_move_records_and_moves_cursor_to_mapped_physical_point() -> None:
    # Scaled space: screenshot 1536x864 on a 1920x1080 monitor -> verified scale 1.25.
    monitor = MonitorInfo(id="m", index=0, bounds=(0, 0, 1920, 1080), dpi_scale_x=1.25, dpi_scale_y=1.25)
    backend = FakeComputerBackend(width=1536, height=864, monitors=[monitor])
    action = move((100, 100))
    message = backend.execute(action)
    assert message == "Simulated move."
    assert backend.executed == [action]
    assert backend._cursor_physical == (125, 125)  # the ONE screenshot->physical transform
    observation = backend.observe()
    assert (observation.cursor_x, observation.cursor_y) == (100, 100)  # localized back


def test_fake_move_prestopped_token_performs_zero_inputs() -> None:
    backend = FakeComputerBackend()
    stop = StopToken()
    stop.stop()
    with pytest.raises(TaskStopped):
        backend.execute(move((10, 10)), stop=stop)
    assert backend.executed == []


def test_fake_move_mid_input_stop_releases_nothing_and_records_nothing() -> None:
    backend = FakeComputerBackend()
    stop = _StopOnNthCheck(1)  # fires on the very first ensure_live (the execute header)
    with pytest.raises(TaskStopped):
        backend.execute(move((10, 10)), stop=stop)
    assert backend.executed == []
    assert backend._cursor_physical is None


def test_fake_move_input_blocked_records_nothing() -> None:
    backend = FakeComputerBackend(input_blocked=True)
    with pytest.raises(InputBlockedError):
        backend.execute(move((10, 10)))
    assert backend.executed == []


def test_fake_move_refuses_unverifiable_coordinates() -> None:
    monitor = MonitorInfo(id="m", index=0, bounds=(0, 0, 1920, 1080), dpi_scale_x=1.25, dpi_scale_y=1.25)
    backend = FakeComputerBackend(width=1000, height=720, monitors=[monitor])
    backend.set_screenshot_size(900, 700)  # no verified transform -> UNVERIFIABLE
    with pytest.raises(CoordinateSpaceError):
        backend.execute(move((10, 10)))
    assert backend.executed == []


def test_fake_move_missing_point_value_error() -> None:
    """A caller bug that bypasses model validation still fails closed at the backend."""
    backend = FakeComputerBackend()
    bare = GroundedAction.model_construct(action=ActionType.MOVE)
    with pytest.raises(ValueError, match="point is required"):
        backend.execute(bare)
    assert backend.executed == []


# --- LocalComputerBackend (real path, stubbed pyautogui) ------------------------------------------


@WINDOWS_ONLY
def test_real_move_calls_move_to_once_with_mapped_coords(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingPyautogui()
    monkeypatch.setattr(real_backend, "_pyautogui", recorder)
    monkeypatch.setattr(real_backend, "_active_context", None)  # passthrough transform
    message = real_backend.execute(move((30, 45)))
    assert message == "Executed move."
    assert recorder.calls == [("moveTo", 30, 45)]


@WINDOWS_ONLY
def test_real_move_prestopped_token_performs_zero_inputs(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingPyautogui()
    monkeypatch.setattr(real_backend, "_pyautogui", recorder)
    stop = StopToken()
    stop.stop()
    with pytest.raises(TaskStopped):
        real_backend.execute(move((30, 45)), stop=stop)
    assert recorder.calls == []


# --- grounding: move is a spatial coordinate action (single point) -------------------------------


def test_grounding_move_routes_to_coordinate_strategy() -> None:
    backend = FakeComputerBackend(width=200, height=200)
    observation = backend.observe()
    grounding = GroundingRouter().route(move((10, 20), confidence=0.9), observation)
    assert grounding.strategy == "coordinate"
    assert grounding.normalized is False
    assert any("Point (10, 20)" in item for item in grounding.evidence)


def test_grounding_move_out_of_bounds_refused() -> None:
    backend = FakeComputerBackend(width=100, height=100)
    observation = backend.observe()
    with pytest.raises(UnsupportedGroundingError, match="Point"):
        GroundingRouter().route(move((150, 10)), observation)


def test_grounding_move_scaled_space_validates_and_records_scale() -> None:
    monitor = MonitorInfo(id="m", index=0, bounds=(0, 0, 1920, 1080), dpi_scale_x=1.25, dpi_scale_y=1.25)
    backend = FakeComputerBackend(width=1536, height=864, monitors=[monitor])
    observation = backend.observe()
    assert observation.coordinate_space.value == "scaled"
    grounding = GroundingRouter().route(move((100, 100), confidence=0.9), observation)
    assert grounding.normalized is True  # scale recorded; the point stays in screenshot space


def test_grounding_move_unverifiable_coordinate_space_refused() -> None:
    monitor = MonitorInfo(id="m", index=0, bounds=(0, 0, 1920, 1080), dpi_scale_x=1.25, dpi_scale_y=1.25)
    backend = FakeComputerBackend(width=1000, height=720, monitors=[monitor])
    backend.set_screenshot_size(900, 700)  # UNVERIFIABLE space
    observation = backend.observe()
    assert observation.coordinate_space_verified is False
    with pytest.raises(UnsupportedGroundingError, match="unverifiable"):
        GroundingRouter().route(move((10, 10)), observation)


# --- validator: binding + staleness + point bounds like click -------------------------------------


def test_validator_move_missing_observation_binding_rejected() -> None:
    backend = FakeComputerBackend()
    observation = backend.observe()
    outcome = GroundingValidator().validate(
        move((10, 10)), observation, current_observation=observation
    )
    assert outcome.valid is False
    assert "missing_observation_binding" in outcome.codes
    assert isinstance(outcome.error, MissingObservationBindingError)


def test_validator_move_stale_observation_rejected_like_click() -> None:
    backend = FakeComputerBackend()
    backend.set_active_window(
        WindowInfo(hwnd=1, pid=1, process_name="mspaint.exe", title="Untitled - Paint")
    )
    source = backend.observe()
    backend.set_active_window(WindowInfo(hwnd=2, pid=2, process_name="calc.exe", title="Calculator"))
    current = backend.observe()
    action = move((10, 10), source_observation_id=source.observation_id)
    outcome = GroundingValidator().validate(action, source, current_observation=current)
    assert outcome.valid is False
    assert "STALE_OBSERVATION" in outcome.codes
    assert isinstance(outcome.error, StaleObservationError)


def test_validator_move_out_of_bounds_rejected() -> None:
    backend = FakeComputerBackend(width=100, height=100)
    observation = backend.observe()
    outcome = GroundingValidator().validate(move((150, 10)), observation)
    assert outcome.valid is False
    assert "point_out_of_bounds" in outcome.codes


def test_validator_move_bound_and_in_bounds_is_valid() -> None:
    backend = FakeComputerBackend()
    observation = backend.observe()
    action = move((10, 10), source_observation_id=observation.observation_id)
    outcome = GroundingValidator().validate(action, observation, current_observation=observation)
    assert outcome.valid is True
    assert outcome.codes == []


def test_validator_move_below_confidence_floor_rejected() -> None:
    backend = FakeComputerBackend()
    observation = backend.observe()
    outcome = GroundingValidator().validate(
        move((10, 10), confidence=0.2),
        observation,
        SessionState(session_id="t", min_confidence=0.7),
    )
    assert outcome.valid is False
    assert "confidence_below_floor" in outcome.codes


# --- safety: move is LOW and never requires approval by itself -------------------------------------


def test_safety_move_is_low_and_never_requires_approval() -> None:
    policy = SafetyPolicy()
    risk, category, why = policy.classify(move((10, 20)), SafetyContext())
    assert risk is RiskLevel.LOW
    assert category == "low_routine_action"
    assert why
    decision = policy.evaluate(move((10, 20)), SessionState(session_id="t"))
    assert decision.allowed is True
    assert decision.requires_approval is False  # even with require_approval=True
    assert decision.risk is RiskLevel.LOW


def test_safety_move_known_application_stays_low() -> None:
    context = SafetyContext(active_process_name="mspaint.exe", window_title="Untitled - Paint")
    risk, category, _why = SafetyPolicy().classify(move((10, 20)), context)
    assert risk is RiskLevel.LOW
    assert category == "low_routine_action"


# --- provider: strict parsing accepts move, rejects malformed moves --------------------------------


def test_parse_decision_accepts_move() -> None:
    payload = json.dumps(
        {
            "status": "action",
            "action": {"action": "move", "point": {"x": 5, "y": 6}, "confidence": 0.9},
        }
    )
    decision = parse_decision(payload)
    assert decision.status == "action"
    assert decision.action is not None
    assert decision.action.action is ActionType.MOVE
    assert decision.action.point == Point(x=5, y=6)


def test_parse_decision_move_without_point_fails_closed() -> None:
    payload = json.dumps({"status": "action", "action": {"action": "move"}})
    with pytest.raises(ProviderParseError):
        parse_decision(payload)


# --- verification: deterministic cursor-at-target predicate -----------------------------------------


def _observation(cursor: tuple[int, int] | None) -> Observation:
    image = Image.new("RGB", (64, 48), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Observation(
        image_base64=base64.b64encode(buffer.getvalue()).decode("ascii"),
        width=64,
        height=48,
        cursor_x=cursor[0] if cursor else None,
        cursor_y=cursor[1] if cursor else None,
    )


def _intent_agent() -> ComputerUseAgent:
    return ComputerUseAgent(FakeComputerBackend(), provider=object(), session_id="intent-tests")


def test_verification_move_cursor_at_target_is_verified() -> None:
    intent = _intent_agent()._build_intent(move((50, 30)), None, None)
    assert intent.kind == "predicate"
    assert intent.predicate_name == "cursor_at_target"
    engine = VerificationEngine()
    assert engine.verify(intent, _observation((10, 10)), _observation((50, 30))).outcome == "verified"
    assert engine.verify(intent, _observation((10, 10)), _observation((52, 28))).outcome == "verified"


def test_verification_move_far_cursor_fails() -> None:
    intent = _intent_agent()._build_intent(move((50, 30)), None, None)
    result = VerificationEngine().verify(intent, _observation((10, 10)), _observation((120, 30)))
    assert result.outcome == "failed"


def test_verification_move_missing_cursor_fields_is_uncertain() -> None:
    """Missing cursor fields must yield uncertain — never a false success."""
    intent = _intent_agent()._build_intent(move((50, 30)), None, None)
    result = VerificationEngine().verify(intent, _observation((10, 10)), _observation(None))
    assert result.outcome == "uncertain"


# --- server: computer_execute move integration ------------------------------------------------------


def _png(color: str, width: int, height: int) -> str:
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class _FlippingFake(FakeComputerBackend):
    """FakeComputerBackend whose screenshot flips color on every completed execute."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._executes = 0

    def observe(self) -> Observation:
        observation = super().observe()
        if self._executes % 2 == 1:
            observation.image_base64 = _png("black", self.width, self.height)
        return observation

    def execute(self, action: GroundedAction, stop: Any = None) -> str:
        message = super().execute(action, stop)
        self._executes += 1
        return message


FAST_LIMITS = {"min_screenshot_interval_ms": 0}


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh bounded registry/bundles + per-test audit dir (mirrors integration suites)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    return server


def _make_session(monkeypatch: pytest.MonkeyPatch, backend: Any, **start_kwargs: Any) -> str:
    monkeypatch.setattr(server, "_backend_factory", lambda: backend)
    monkeypatch.setattr(server, "_provider_factory", lambda: object())
    response = server.start_session(**start_kwargs)
    assert response.get("session_id"), response
    return str(response["session_id"])


async def test_computer_execute_move_executes_and_moves_cursor(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _FlippingFake()
    session_id = _make_session(
        monkeypatch, backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = await server.computer_execute(session_id, "move", x=40, y=50)
    assert result["ok"] is True, result
    assert result["message"] == "Simulated move."
    assert result["verification"]["outcome"] == "verified"  # deterministic cursor predicate
    assert len(backend.executed) == 1
    executed = backend.executed[0]
    assert executed.action is ActionType.MOVE
    assert (executed.point.x, executed.point.y) == (40, 50)
    assert backend._cursor_physical == (40, 50)
    observation = backend.observe()
    assert (observation.cursor_x, observation.cursor_y) == (40, 50)


async def test_computer_execute_move_without_x_y_fails_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _FlippingFake()
    session_id = _make_session(
        monkeypatch, backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = await server.computer_execute(session_id, "move")
    assert result["ok"] is False
    assert result["error"] == "invalid_action"
    assert backend.executed == []
