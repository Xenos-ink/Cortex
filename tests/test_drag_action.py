"""DRAG action tests: press-move-release input with stop-checked stroke segments.

The drag is an additive ActionType member treated exactly like click everywhere the
runtime branches on interactivity:

- models: a drag requires BOTH endpoints (``point`` = start, ``to_point`` = end);
- backends (fake + real): stop token verified before every stroke step, interpolated
  segments (~40 px, min 4), and the mouse button ALWAYS released — including on a
  mid-stroke TaskStopped (try/finally);
- grounding/validation: drag is a coordinate action (both points bounds-checked in
  screenshot space, source-observation binding + staleness like click);
- safety: classified like click (MEDIUM unverified / LOW known app, approval by default);
- provider: strict parse accepts ``to_point`` for drag, rejects malformed drags;
- server: ``computer_execute`` gains trailing ``x2``/``y2`` for the drag end point.
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError
from recording_engine import RecordingEngine

import computer_use_mcp.backend as backend_module
from computer_use_mcp import server
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
    RiskLevel,
    SessionState,
    WindowInfo,
)
from computer_use_mcp.provider import RESPONSE_SCHEMA_TEXT, ProviderParseError, parse_decision
from computer_use_mcp.safety import SafetyContext, SafetyPolicy
from computer_use_mcp.state import SessionRegistry, StopToken, TaskStopped
from computer_use_mcp.validator import (
    GroundingValidator,
    MissingObservationBindingError,
    StaleObservationError,
)

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")
FAST_LIMITS = {"min_screenshot_interval_ms": 0}


def drag(start: tuple[int, int], end: tuple[int, int], **kwargs: Any) -> GroundedAction:
    kwargs.setdefault("confidence", 1.0)
    return GroundedAction(
        action="drag",
        point={"x": start[0], "y": start[1]},
        to_point={"x": end[0], "y": end[1]},
        **kwargs,
    )


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


class _CountingStopToken(StopToken):
    """Stop token that never fires but counts ``ensure_live`` checks."""

    def __init__(self) -> None:
        super().__init__()
        self.checks = 0

    def ensure_live(self) -> None:
        self.checks += 1
        super().ensure_live()


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


# The session-scoped ``real_backend`` fixture comes from tests/conftest.py (one
# LocalComputerBackend per process — see conftest for why this is load-bearing).


# --- models: drag requires both endpoints -----------------------------------------------------


def test_drag_action_member_value() -> None:
    assert ActionType.DRAG.value == "drag"
    assert ActionType("drag") is ActionType.DRAG


def test_drag_requires_both_endpoints() -> None:
    ok = drag((10, 20), (110, 60))
    assert ok.point == Point(x=10, y=20)
    assert ok.to_point == Point(x=110, y=60)
    with pytest.raises(ValidationError):  # start only
        GroundedAction(action="drag", point={"x": 1, "y": 2})
    with pytest.raises(ValidationError):  # end only
        GroundedAction(action="drag", to_point={"x": 1, "y": 2})
    with pytest.raises(ValidationError):  # neither
        GroundedAction(action="drag")


def test_to_point_defaults_to_none_for_other_actions() -> None:
    action = GroundedAction(action="click", point={"x": 5, "y": 6})
    assert action.to_point is None


def test_drag_endpoints_keep_legacy_point_bounds() -> None:
    with pytest.raises(ValidationError):
        GroundedAction(
            action="drag", point={"x": -9_000, "y": 0}, to_point={"x": 10, "y": 10}
        )
    with pytest.raises(ValidationError):
        GroundedAction(
            action="drag", point={"x": 0, "y": 0}, to_point={"x": 10, "y": 20_000}
        )


# --- FakeComputerBackend: drag execution contract ----------------------------------------------


def test_fake_drag_records_start_end_and_walks_cursor_to_end() -> None:
    backend = FakeComputerBackend(width=400, height=400)
    action = drag((10, 10), (110, 60))
    message = backend.execute(action)
    assert message == "Simulated drag."
    assert backend.executed == [action]
    assert backend.drags == [((10, 10), (110, 60))]
    assert backend._cursor_physical == (110, 60)
    assert backend._drag_button_down is False  # button never left held
    observation = backend.observe()
    assert (observation.cursor_x, observation.cursor_y) == (110, 60)


def test_fake_drag_prestopped_token_performs_zero_inputs() -> None:
    backend = FakeComputerBackend()
    stop = StopToken()
    stop.stop()
    with pytest.raises(TaskStopped):
        backend.execute(drag((10, 10), (110, 60)), stop=stop)
    assert backend.executed == []
    assert backend.drags == []
    assert backend._drag_button_down is False


def test_fake_drag_checks_stop_token_before_every_step() -> None:
    """Header + pre-move + pre-mouseDown + per-segment + pre-mouseUp checks (8 for 4 segments)."""
    backend = FakeComputerBackend()
    stop = _CountingStopToken()
    backend.execute(drag((10, 10), (110, 60)), stop=stop)  # chebyshev 100 -> 4 segments
    assert stop.checks == 8


def test_fake_drag_mid_stroke_stop_releases_button_and_records_nothing() -> None:
    backend = FakeComputerBackend()
    # 5 stroke checks planned (chebyshev 190 -> 5 segments): header, pre-move, pre-down,
    # then one per segment. Firing on check 6 stops the stroke after two segments.
    stop = _StopOnNthCheck(6)
    with pytest.raises(TaskStopped):
        backend.execute(drag((10, 10), (200, 200)), stop=stop)
    assert backend.executed == []
    assert backend.drags == []
    assert backend._drag_button_down is False  # the button WAS released (try/finally)
    expected_waypoints = backend_module._drag_segment_points((10, 10), (200, 200))
    assert backend._cursor_physical == expected_waypoints[1]  # halted mid-stroke


def test_fake_drag_missing_endpoints_value_error() -> None:
    """A caller bug that bypasses model validation still fails closed at the backend."""
    backend = FakeComputerBackend()
    start_only = GroundedAction.model_construct(action=ActionType.DRAG, point=Point(x=1, y=2))
    with pytest.raises(ValueError, match="start point and an end point"):
        backend.execute(start_only)
    bare = GroundedAction.model_construct(action=ActionType.DRAG)
    with pytest.raises(ValueError, match="start point and an end point"):
        backend.execute(bare)
    assert backend.executed == []


def test_fake_drag_refuses_unverifiable_coordinates() -> None:
    monitor = MonitorInfo(id="m", index=0, bounds=(0, 0, 1920, 1080), dpi_scale_x=1.25, dpi_scale_y=1.25)
    backend = FakeComputerBackend(width=1000, height=720, monitors=[monitor])
    backend.set_screenshot_size(900, 700)  # no verified transform -> UNVERIFIABLE
    with pytest.raises(CoordinateSpaceError):
        backend.execute(drag((10, 10), (110, 60)))
    assert backend.executed == []
    assert backend.drags == []


def test_fake_drag_input_blocked_records_nothing() -> None:
    backend = FakeComputerBackend(input_blocked=True)
    with pytest.raises(InputBlockedError):
        backend.execute(drag((10, 10), (110, 60)))
    assert backend.executed == []
    assert backend.drags == []


# --- LocalComputerBackend (real path, stubbed input engine): stop + release guarantees ------------


@WINDOWS_ONLY
def test_real_drag_prestopped_token_performs_zero_inputs(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    stop = StopToken()
    stop.stop()
    with pytest.raises(TaskStopped):
        real_backend.execute(drag((10, 10), (110, 60)), stop=stop)
    assert engine.calls == []


@WINDOWS_ONLY
def test_real_drag_minimal_stroke_move_down_move_up(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default SendInput policy: move-press-move-release with a single segment."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)  # passthrough transform
    message = real_backend.execute(drag((10, 10), (100, 60)))
    assert message == "Executed drag."
    assert engine.calls == [
        ("move", 10, 10),
        ("mouse_down", "left"),
        ("move", 100, 60),  # stroke ends exactly at the drag end point
        ("mouse_up", "left"),
    ]


@WINDOWS_ONLY
def test_real_drag_interpolated_option_draws_waypoints(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drag interpolation option restores the ~40 px stroke (legacy parity)."""
    engine = RecordingEngine(drag_interpolate=True)
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    real_backend.execute(drag((10, 10), (100, 60)))
    waypoints = backend_module._drag_segment_points((10, 10), (100, 60))
    assert len(waypoints) == 4  # ~40 px segments with a floor of 4
    assert waypoints[-1] == (100, 60)
    assert engine.calls == [
        ("move", 10, 10),
        ("mouse_down", "left"),
        *[("move", x, y) for x, y in waypoints],
        ("mouse_up", "left"),
    ]


@WINDOWS_ONLY
def test_real_drag_mid_stroke_stop_releases_button(real_backend, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    stop = _StopOnNthCheck(4)  # header, pre-move, pre-down pass; fires before the segment move
    with pytest.raises(TaskStopped):
        real_backend.execute(drag((10, 10), (200, 200)), stop=stop)
    assert engine.calls == [
        ("move", 10, 10),
        ("mouse_down", "left"),
        ("mouse_up", "left"),  # released by the finally guard despite the stop
    ]
    assert engine.down is False


@WINDOWS_ONLY
def test_real_drag_interpolated_mid_stroke_stop_releases_button(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = RecordingEngine(drag_interpolate=True)
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    stop = _StopOnNthCheck(6)  # fires before the third of five stroke segments
    with pytest.raises(TaskStopped):
        real_backend.execute(drag((10, 10), (200, 200)), stop=stop)
    waypoints = backend_module._drag_segment_points((10, 10), (200, 200))
    assert engine.calls == [
        ("move", 10, 10),
        ("mouse_down", "left"),
        *[("move", x, y) for x, y in waypoints[:2]],
        ("mouse_up", "left"),  # released by the finally guard despite the stop
    ]
    assert engine.down is False


# --- grounding: drag is a spatial coordinate action (both endpoints) ----------------------------


def test_grounding_drag_routes_to_coordinate_strategy_and_validates_both_points() -> None:
    backend = FakeComputerBackend(width=200, height=200)
    observation = backend.observe()
    action = drag((10, 20), (110, 60), confidence=0.9)
    grounding = GroundingRouter().route(action, observation)
    assert grounding.strategy == "coordinate"
    assert grounding.normalized is False
    assert any("End point (110, 60)" in item for item in grounding.evidence)


def test_grounding_drag_end_point_out_of_bounds_refused() -> None:
    backend = FakeComputerBackend(width=100, height=100)
    observation = backend.observe()
    with pytest.raises(UnsupportedGroundingError, match="End point"):
        GroundingRouter().route(drag((10, 10), (150, 10)), observation)


def test_grounding_drag_start_point_out_of_bounds_refused() -> None:
    backend = FakeComputerBackend(width=100, height=100)
    observation = backend.observe()
    with pytest.raises(UnsupportedGroundingError, match="Point"):
        GroundingRouter().route(drag((-5, 10), (50, 10)), observation)


def test_grounding_drag_unverifiable_coordinate_space_refused() -> None:
    monitor = MonitorInfo(id="m", index=0, bounds=(0, 0, 1920, 1080), dpi_scale_x=1.25, dpi_scale_y=1.25)
    backend = FakeComputerBackend(width=1000, height=720, monitors=[monitor])
    backend.set_screenshot_size(900, 700)  # UNVERIFIABLE space
    observation = backend.observe()
    assert observation.coordinate_space_verified is False
    with pytest.raises(UnsupportedGroundingError, match="unverifiable"):
        GroundingRouter().route(drag((10, 10), (50, 50)), observation)


def test_grounding_drag_scaled_space_validates_both_points_and_records_scale() -> None:
    monitor = MonitorInfo(id="m", index=0, bounds=(0, 0, 1920, 1080), dpi_scale_x=1.25, dpi_scale_y=1.25)
    backend = FakeComputerBackend(width=1536, height=864, monitors=[monitor])
    observation = backend.observe()
    assert observation.coordinate_space.value == "scaled"
    action = drag((100, 100), (1400, 700), confidence=0.9)
    grounding = GroundingRouter().route(action, observation)
    assert grounding.normalized is True  # scale recorded; points stay in screenshot space
    assert any("End point (1400, 700)" in item for item in grounding.evidence)


# --- validator: binding + staleness + endpoint bounds like click --------------------------------


def test_validator_drag_missing_observation_binding_rejected() -> None:
    backend = FakeComputerBackend()
    observation = backend.observe()
    outcome = GroundingValidator().validate(
        drag((10, 10), (50, 50)), observation, current_observation=observation
    )
    assert outcome.valid is False
    assert "missing_observation_binding" in outcome.codes
    assert isinstance(outcome.error, MissingObservationBindingError)


def test_validator_drag_stale_observation_rejected_like_click() -> None:
    backend = FakeComputerBackend()
    backend.set_active_window(
        WindowInfo(hwnd=1, pid=1, process_name="mspaint.exe", title="Untitled - Paint")
    )
    source = backend.observe()
    backend.set_active_window(
        WindowInfo(hwnd=2, pid=2, process_name="calc.exe", title="Calculator")
    )
    current = backend.observe()
    action = drag((10, 10), (50, 50), source_observation_id=source.observation_id)
    outcome = GroundingValidator().validate(action, source, current_observation=current)
    assert outcome.valid is False
    assert "STALE_OBSERVATION" in outcome.codes
    assert isinstance(outcome.error, StaleObservationError)


def test_validator_drag_end_point_out_of_bounds_rejected() -> None:
    backend = FakeComputerBackend(width=100, height=100)
    observation = backend.observe()
    outcome = GroundingValidator().validate(drag((10, 10), (150, 10)), observation)
    assert outcome.valid is False
    assert "point_out_of_bounds" in outcome.codes


def test_validator_drag_bound_and_in_bounds_is_valid() -> None:
    backend = FakeComputerBackend()
    observation = backend.observe()
    action = drag((10, 10), (50, 50), source_observation_id=observation.observation_id)
    outcome = GroundingValidator().validate(action, observation, current_observation=observation)
    assert outcome.valid is True
    assert outcome.codes == []


# --- safety: drag is classified and gated exactly like click -------------------------------------


def test_drag_safety_unverified_identity_is_medium_like_click() -> None:
    risk, category, reason = SafetyPolicy().classify(drag((10, 20), (110, 60)), SafetyContext())
    assert risk is RiskLevel.MEDIUM
    assert category == "unverified_target_application"
    assert reason


def test_drag_safety_known_application_is_low_like_click() -> None:
    context = SafetyContext(active_process_name="mspaint.exe", window_title="Untitled - Paint")
    risk, category, _reason = SafetyPolicy().classify(drag((10, 20), (110, 60)), context)
    assert risk is RiskLevel.LOW
    assert category == "known_application_interaction"


def test_drag_evaluate_requires_approval_by_default_like_click() -> None:
    policy = SafetyPolicy()
    decision = policy.evaluate(drag((10, 20), (110, 60)), SessionState(session_id="t"))
    assert decision.allowed is True
    assert decision.requires_approval is True
    assert decision.risk is RiskLevel.MEDIUM
    relaxed = policy.evaluate(
        drag((10, 20), (110, 60)), SessionState(session_id="t", require_approval=False)
    )
    assert relaxed.requires_approval is False


def test_drag_approval_message_describes_both_endpoints() -> None:
    decision = SafetyPolicy().evaluate(drag((10, 20), (110, 60)), SessionState(session_id="t"))
    assert "drag from screenshot coordinates (x=10, y=20) to (x=110, y=60)" in decision.reason
    assert "Target:" in decision.reason  # identity context, never bare coordinates


# --- provider: strict parsing accepts drag with to_point, rejects malformed drags ----------------


def test_parse_decision_accepts_drag_with_to_point() -> None:
    payload = json.dumps(
        {
            "status": "action",
            "action": {
                "action": "drag",
                "point": {"x": 5, "y": 6},
                "to_point": {"x": 50, "y": 60},
                "confidence": 0.9,
            },
        }
    )
    decision = parse_decision(payload)
    assert decision.status == "action"
    assert decision.action is not None
    assert decision.action.action is ActionType.DRAG
    assert decision.action.point == Point(x=5, y=6)
    assert decision.action.to_point == Point(x=50, y=60)


def test_parse_decision_drag_without_start_point_fails_closed() -> None:
    payload = json.dumps(
        {"status": "action", "action": {"action": "drag", "to_point": {"x": 50, "y": 60}}}
    )
    with pytest.raises(ProviderParseError):
        parse_decision(payload)


def test_parse_decision_drag_without_end_point_fails_closed() -> None:
    payload = json.dumps(
        {"status": "action", "action": {"action": "drag", "point": {"x": 5, "y": 6}}}
    )
    with pytest.raises(ProviderParseError):
        parse_decision(payload)


def test_parse_decision_unknown_action_still_fails_closed() -> None:
    payload = json.dumps(
        {"status": "action", "action": {"action": "teleport", "point": {"x": 1, "y": 2}}}
    )
    with pytest.raises(ProviderParseError):
        parse_decision(payload)


def test_response_schema_documents_drag() -> None:
    assert '"drag"' in RESPONSE_SCHEMA_TEXT
    assert '"to_point"' in RESPONSE_SCHEMA_TEXT
    assert "drag start" in RESPONSE_SCHEMA_TEXT


# --- server: computer_execute x2/y2 integration ---------------------------------------------------


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh bounded registry/bundles + per-test audit dir (mirrors integration suites)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    return server


def _make_session(
    monkeypatch: pytest.MonkeyPatch, backend: Any, **start_kwargs: Any
) -> str:
    monkeypatch.setattr(server, "_backend_factory", lambda: backend)
    monkeypatch.setattr(server, "_provider_factory", lambda: object())
    response = server.start_session(**start_kwargs)
    assert response.get("session_id"), response
    return str(response["session_id"])


async def test_computer_execute_drag_via_x2_y2_executes_and_fake_records(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _FlippingFake()
    session_id = _make_session(
        monkeypatch, backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = await server.computer_execute(session_id, "drag", x=10, y=20, x2=110, y2=70)
    assert result["ok"] is True, result
    assert result["message"] == "Simulated drag."
    assert result["verification"]["outcome"] == "verified"  # flip: visual change detected
    assert len(backend.executed) == 1
    executed = backend.executed[0]
    assert executed.action is ActionType.DRAG
    assert (executed.point.x, executed.point.y) == (10, 20)
    assert executed.to_point == Point(x=110, y=70)
    assert backend.drags == [((10, 20), (110, 70))]


async def test_computer_execute_drag_without_end_point_fails_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _FlippingFake()
    session_id = _make_session(
        monkeypatch, backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = await server.computer_execute(session_id, "drag", x=10, y=20)  # no x2/y2
    assert result["ok"] is False
    assert result["error"] == "invalid_action"
    assert backend.executed == []


async def test_computer_execute_drag_out_of_bounds_end_point_rejected(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _FlippingFake()
    session_id = _make_session(
        monkeypatch, backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = await server.computer_execute(session_id, "drag", x=10, y=20, x2=5000, y2=70)
    assert result["ok"] is False
    assert result["message"] == "Grounding rejected."
    assert result["reasons"]
    assert backend.executed == []


async def test_computer_execute_drag_requires_approval_by_default(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _FlippingFake()
    session_id = _make_session(
        monkeypatch, backend, dry_run=False, require_approval=True, limits=FAST_LIMITS
    )
    result = await server.computer_execute(session_id, "drag", x=10, y=20, x2=110, y2=70)
    assert result["ok"] is False
    assert result["requires_approval"] is True
    assert result["message"]
    assert backend.executed == []
    approved = await server.computer_execute(
        session_id, "drag", x=10, y=20, x2=110, y2=70, approved=True
    )
    assert approved["ok"] is True
    assert len(backend.executed) == 1
