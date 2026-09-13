"""RIGHT_CLICK action tests: the owner-commissioned context-menu press.

The right_click is an additive ActionType member (v0.6.0) carrying a screenshot-space
``point`` that presses/releases the RIGHT mouse button once — opening a context menu.
It grounds/validates/binds exactly like click on the coordinate path (bounds + staleness
+ observation binding), safety classifies it inside the click family (MEDIUM
``unverified_target_application`` when identity is unknown, LOW
``known_application_interaction`` otherwise), the backend dispatches it with
``button="right"`` through the same stop-check/single-transform discipline, and
verification falls through to ``visual_change`` exactly like click.
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
from computer_use_mcp.agent import ComputerUseAgent
from computer_use_mcp.backend import (
    CoordinateSpaceError,
    FakeComputerBackend,
    InputBlockedError,
    PyAutoGuiInputEngine,
    SendInputEngine,
)
from computer_use_mcp.grounding import GroundingRouter, UnsupportedGroundingError
from computer_use_mcp.models import (
    ActionSpec,
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

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")


def right_click(point: tuple[int, int], **kwargs: Any) -> GroundedAction:
    kwargs.setdefault("confidence", 1.0)
    return GroundedAction(action="right_click", point={"x": point[0], "y": point[1]}, **kwargs)


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


# The session-scoped ``real_backend`` fixture comes from tests/conftest.py, which also
# provides the RecordingEngine stub (no real input dispatch).


# --- models: right_click is a point-bearing click-family member ---------------------------------


def test_right_click_action_member_value() -> None:
    assert ActionType.RIGHT_CLICK.value == "right_click"
    assert ActionType("right_click") is ActionType.RIGHT_CLICK


def test_right_click_requires_point() -> None:
    ok = right_click((30, 40))
    assert ok.point == Point(x=30, y=40)
    with pytest.raises(ValidationError, match="point"):
        GroundedAction(action="right_click")


def test_right_click_point_keeps_legacy_point_bounds() -> None:
    with pytest.raises(ValidationError):
        GroundedAction(action="right_click", point={"x": -9_000, "y": 0})


def test_right_click_action_spec_converts_additively() -> None:
    """A queued follow_up spec for right_click converts like any single action."""
    spec = ActionSpec(action="right_click", x=12, y=34)
    grounded = spec.to_grounded()
    assert grounded.action is ActionType.RIGHT_CLICK
    assert grounded.point == Point(x=12, y=34)


# --- FakeComputerBackend: right_click execution contract (parity with click) ---------------------


def test_fake_right_click_records_and_moves_cursor_to_mapped_physical_point() -> None:
    # Scaled space: screenshot 1536x864 on a 1920x1080 monitor -> verified scale 1.25.
    monitor = MonitorInfo(id="m", index=0, bounds=(0, 0, 1920, 1080), dpi_scale_x=1.25, dpi_scale_y=1.25)
    backend = FakeComputerBackend(width=1536, height=864, monitors=[monitor])
    action = right_click((100, 100))
    message = backend.execute(action)
    assert message == "Simulated right_click."
    assert backend.executed == [action]
    assert backend._cursor_physical == (125, 125)  # the ONE screenshot->physical transform
    observation = backend.observe()
    assert (observation.cursor_x, observation.cursor_y) == (100, 100)  # localized back


def test_fake_right_click_prestopped_token_performs_zero_inputs() -> None:
    backend = FakeComputerBackend()
    stop = StopToken()
    stop.stop()
    with pytest.raises(TaskStopped):
        backend.execute(right_click((10, 10)), stop=stop)
    assert backend.executed == []


def test_fake_right_click_input_blocked_records_nothing() -> None:
    backend = FakeComputerBackend(input_blocked=True)
    with pytest.raises(InputBlockedError):
        backend.execute(right_click((10, 10)))
    assert backend.executed == []


def test_fake_right_click_refuses_unverifiable_coordinates() -> None:
    monitor = MonitorInfo(id="m", index=0, bounds=(0, 0, 1920, 1080), dpi_scale_x=1.25, dpi_scale_y=1.25)
    backend = FakeComputerBackend(width=1000, height=720, monitors=[monitor])
    backend.set_screenshot_size(900, 700)  # no verified transform -> UNVERIFIABLE
    with pytest.raises(CoordinateSpaceError):
        backend.execute(right_click((10, 10)))
    assert backend.executed == []


def test_fake_right_click_missing_point_value_error() -> None:
    """A caller bug that bypasses model validation still fails closed at the backend."""
    backend = FakeComputerBackend()
    bare = GroundedAction.model_construct(action=ActionType.RIGHT_CLICK)
    with pytest.raises(ValueError, match="point is required"):
        backend.execute(bare)
    assert backend.executed == []


# --- LocalComputerBackend (real path, stubbed input engine): right-button dispatch ---------------


@WINDOWS_ONLY
def test_real_right_click_presses_right_button_once_at_mapped_point(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)  # passthrough transform
    message = real_backend.execute(right_click((30, 45)))
    assert message == "Executed right_click."
    assert engine.calls == [("click", 30, 45, 1, "right")]


@WINDOWS_ONLY
def test_real_right_click_stops_before_dispatch_on_prestopped_token(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    stop = StopToken()
    stop.stop()
    with pytest.raises(TaskStopped):
        real_backend.execute(right_click((30, 45)), stop=stop)
    assert engine.calls == []


@WINDOWS_ONLY
def test_real_right_click_stop_check_runs_after_transform_before_input(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop check sits between the coordinate transform and the physical press."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)  # passthrough transform
    stop = _StopOnNthCheck(2)  # 1: execute header, 2: right before the click input
    with pytest.raises(TaskStopped):
        real_backend.execute(right_click((30, 45)), stop=stop)
    assert engine.calls == []


# --- engines: both engines honor button="right" --------------------------------------------------


class _FakePyautogui:
    """pyautogui stand-in recording calls (same convention as test_input_engines)."""

    FailSafeException = type("FailSafeException", (Exception,), {})

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.PAUSE = 0.0
        self.FAILSAFE = False

    def click(self, *args: object, **kwargs: object) -> None:
        self.calls.append(("click", *args, *kwargs.items()))


def test_pyautogui_engine_click_passes_right_button() -> None:
    pa = _FakePyautogui()
    engine = PyAutoGuiInputEngine(pa)
    engine.click(5, 6, button="right")
    assert pa.calls == [("click", 5, 6, ("clicks", 1), ("interval", 0.08), ("button", "right"))]


def test_pyautogui_engine_click_default_stays_left() -> None:
    pa = _FakePyautogui()
    engine = PyAutoGuiInputEngine(pa)
    engine.click(5, 6)
    assert pa.calls == [("click", 5, 6, ("clicks", 1), ("interval", 0.08), ("button", "left"))]


def test_pyautogui_engine_click_forwards_button_verbatim() -> None:
    """The pyautogui fallback forwards the button name verbatim.

    Fail-closed button-name validation is pinned on the DEFAULT SendInput engine
    (``unsupported mouse button`` ValueError before any dispatch); the fallback
    engine hands ``button`` to pyautogui, which owns that validation.
    """
    pa = _FakePyautogui()
    engine = PyAutoGuiInputEngine(pa)
    engine.click(5, 6, button="middle")
    assert pa.calls == [("click", 5, 6, ("clicks", 1), ("interval", 0.08), ("button", "middle"))]


class _FakeUser32:
    """user32 stand-in recording SendInput batches (no real input)."""

    def __init__(self) -> None:
        self.batches: list[list[dict[str, int]]] = []

    def GetCursorPos(self, point_ref: Any) -> int:
        point_ref._obj.x, point_ref._obj.y = (960, 540)
        return 1

    def GetSystemMetrics(self, index: int) -> int:
        mapping = {0: 1920, 1: 1080}
        mapping.update(
            {
                backend_module._SM_XVIRTUALSCREEN: 0,
                backend_module._SM_YVIRTUALSCREEN: 0,
                backend_module._SM_CXVIRTUALSCREEN: 1920,
                backend_module._SM_CYVIRTUALSCREEN: 1080,
            }
        )
        return mapping[index]

    def SendInput(self, count: int, events: Any, _size: int) -> int:
        batch = []
        for index in range(count):
            event = events[index]
            if event.type == 0:  # INPUT_MOUSE
                batch.append({"kind": "mouse", "flags": int(event.union.mi.dwFlags)})
        self.batches.append(batch)
        return count


@pytest.fixture
def fake_user32(monkeypatch: pytest.MonkeyPatch) -> _FakeUser32:
    fake = _FakeUser32()
    monkeypatch.setattr(backend_module, "_user32", fake)
    return fake


@WINDOWS_ONLY
def test_sendinput_engine_right_click_sends_rightdown_rightup(fake_user32: _FakeUser32) -> None:
    """One right click = move + RIGHTDOWN + RIGHTUP in ONE SendInput call."""
    SendInputEngine().click(30, 45, button="right")
    assert len(fake_user32.batches) == 1
    flags = [event["flags"] for event in fake_user32.batches[0]]
    assert flags == [
        backend_module._MOUSEEVENTF_MOVE
        | backend_module._MOUSEEVENTF_ABSOLUTE
        | backend_module._MOUSEEVENTF_VIRTUALDESK,
        backend_module._MOUSEEVENTF_RIGHTDOWN,
        backend_module._MOUSEEVENTF_RIGHTUP,
    ]


@WINDOWS_ONLY
def test_sendinput_engine_default_click_stays_left(fake_user32: _FakeUser32) -> None:
    SendInputEngine().click(30, 45)
    flags = [event["flags"] for event in fake_user32.batches[0]]
    assert flags[1:] == [
        backend_module._MOUSEEVENTF_LEFTDOWN,
        backend_module._MOUSEEVENTF_LEFTUP,
    ]


@WINDOWS_ONLY
def test_sendinput_engine_click_rejects_unknown_button_before_dispatch(
    fake_user32: _FakeUser32,
) -> None:
    with pytest.raises(ValueError, match="unsupported mouse button"):
        SendInputEngine().click(30, 45, button="middle-unknown")
    assert fake_user32.batches == []


def test_mouse_button_flags_pin_right_vocabulary() -> None:
    """The Win32 plumbing right_click relies on: typed right down/up flag pair."""
    assert backend_module._MOUSE_BUTTON_FLAGS["right"] == (
        backend_module._MOUSEEVENTF_RIGHTDOWN,
        backend_module._MOUSEEVENTF_RIGHTUP,
    )
    assert backend_module._MOUSEEVENTF_RIGHTDOWN == 0x0008
    assert backend_module._MOUSEEVENTF_RIGHTUP == 0x0010


# --- grounding: right_click is a spatial coordinate action (single point) ------------------------


def test_grounding_right_click_routes_to_coordinate_strategy() -> None:
    backend = FakeComputerBackend(width=200, height=200)
    observation = backend.observe()
    grounding = GroundingRouter().route(right_click((10, 20), confidence=0.9), observation)
    assert grounding.strategy == "coordinate"
    assert grounding.normalized is False
    assert any("Point (10, 20)" in item for item in grounding.evidence)


def test_grounding_right_click_out_of_bounds_refused() -> None:
    backend = FakeComputerBackend(width=100, height=100)
    observation = backend.observe()
    with pytest.raises(UnsupportedGroundingError, match="Point"):
        GroundingRouter().route(right_click((150, 10)), observation)


def test_grounding_right_click_scaled_space_validates_and_records_scale() -> None:
    monitor = MonitorInfo(id="m", index=0, bounds=(0, 0, 1920, 1080), dpi_scale_x=1.25, dpi_scale_y=1.25)
    backend = FakeComputerBackend(width=1536, height=864, monitors=[monitor])
    observation = backend.observe()
    assert observation.coordinate_space.value == "scaled"
    grounding = GroundingRouter().route(right_click((100, 100), confidence=0.9), observation)
    assert grounding.normalized is True  # scale recorded; the point stays in screenshot space


def test_grounding_right_click_is_never_non_spatial() -> None:
    """right_click stays OFF the non-spatial set: without a point it cannot ground."""
    from computer_use_mcp.grounding import NON_SPATIAL_ACTIONS

    assert ActionType.RIGHT_CLICK not in NON_SPATIAL_ACTIONS


# --- validator: binding + staleness + point bounds exactly like click ----------------------------


def test_validator_right_click_missing_observation_binding_rejected() -> None:
    backend = FakeComputerBackend()
    observation = backend.observe()
    outcome = GroundingValidator().validate(
        right_click((10, 10)), observation, current_observation=observation
    )
    assert outcome.valid is False
    assert "missing_observation_binding" in outcome.codes
    assert isinstance(outcome.error, MissingObservationBindingError)


def test_validator_right_click_stale_observation_rejected_like_click() -> None:
    backend = FakeComputerBackend()
    backend.set_active_window(
        WindowInfo(hwnd=1, pid=1, process_name="mspaint.exe", title="Untitled - Paint")
    )
    source = backend.observe()
    backend.set_active_window(WindowInfo(hwnd=2, pid=2, process_name="calc.exe", title="Calculator"))
    current = backend.observe()
    action = right_click((10, 10), source_observation_id=source.observation_id)
    outcome = GroundingValidator().validate(action, source, current_observation=current)
    assert outcome.valid is False
    assert "STALE_OBSERVATION" in outcome.codes
    assert isinstance(outcome.error, StaleObservationError)


def test_validator_right_click_out_of_bounds_rejected() -> None:
    backend = FakeComputerBackend(width=100, height=100)
    observation = backend.observe()
    outcome = GroundingValidator().validate(right_click((150, 10)), observation)
    assert outcome.valid is False
    assert "point_out_of_bounds" in outcome.codes


def test_validator_right_click_bound_and_in_bounds_is_valid() -> None:
    backend = FakeComputerBackend()
    observation = backend.observe()
    action = right_click((10, 10), source_observation_id=observation.observation_id)
    outcome = GroundingValidator().validate(action, observation, current_observation=observation)
    assert outcome.valid is True
    assert outcome.codes == []


def test_validator_right_click_is_a_coordinate_action() -> None:
    from computer_use_mcp.validator import COORDINATE_ACTIONS

    assert ActionType.RIGHT_CLICK in COORDINATE_ACTIONS


# --- safety: classified inside the click family (same sets as click) -----------------------------


def test_safety_right_click_unknown_identity_is_medium() -> None:
    policy = SafetyPolicy()
    risk, category, why = policy.classify(right_click((10, 20)), SafetyContext())
    assert risk is RiskLevel.MEDIUM
    assert category == "unverified_target_application"
    assert why


def test_safety_right_click_known_application_is_low() -> None:
    context = SafetyContext(active_process_name="notepad.exe", window_title="Untitled - Notepad")
    risk, category, _why = SafetyPolicy().classify(right_click((10, 20)), context)
    assert risk is RiskLevel.LOW
    assert category == "known_application_interaction"


def test_safety_right_click_evaluation_matches_click_approval_semantics() -> None:
    """Interactive default: allowed, approval required only when the session demands it."""
    policy = SafetyPolicy()
    state = SessionState(session_id="t", require_approval=True)
    decision = policy.evaluate(right_click((10, 20)), state)
    assert decision.allowed is True
    assert decision.requires_approval is True
    assert decision.risk is RiskLevel.MEDIUM

    relaxed = SessionState(session_id="t", require_approval=False)
    decision = policy.evaluate(right_click((10, 20)), relaxed)
    assert decision.allowed is True
    assert decision.requires_approval is False


def test_safety_right_click_approval_message_describes_coordinates() -> None:
    policy = SafetyPolicy()
    state = SessionState(session_id="t", require_approval=True)
    decision = policy.evaluate(right_click((30, 45)), state)
    assert "right_click at screenshot coordinates (x=30, y=45)" in decision.reason


# --- provider: strict parsing accepts right_click, rejects malformed ones ------------------------


def test_parse_decision_accepts_right_click() -> None:
    payload = json.dumps(
        {
            "status": "action",
            "action": {"action": "right_click", "point": {"x": 5, "y": 6}, "confidence": 0.9},
        }
    )
    decision = parse_decision(payload)
    assert decision.status == "action"
    assert decision.action is not None
    assert decision.action.action is ActionType.RIGHT_CLICK
    assert decision.action.point == Point(x=5, y=6)


def test_parse_decision_right_click_without_point_fails_closed() -> None:
    payload = json.dumps({"status": "action", "action": {"action": "right_click"}})
    with pytest.raises(ProviderParseError):
        parse_decision(payload)


# --- verification: default falls through to visual_change exactly like click ---------------------


def _observation() -> Observation:
    image = Image.new("RGB", (64, 48), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Observation(
        image_base64=base64.b64encode(buffer.getvalue()).decode("ascii"),
        width=64,
        height=48,
    )


def _intent_agent() -> ComputerUseAgent:
    return ComputerUseAgent(FakeComputerBackend(), provider=object(), session_id="intent-tests")


def test_verification_right_click_defaults_to_visual_change() -> None:
    """No stated effect: the same visual_change fall-through as click."""
    intent = _intent_agent()._build_intent(right_click((50, 30)), None, None)
    assert intent.kind == "visual_change"
    assert intent.expected_change is None


def test_verification_right_click_stated_effect_requires_change_without_focus_flag() -> None:
    """A stated effect is REQUIRED to be observed (expected_change=True).

    The FOCUS_CHANGE_INTENT_FLAG deliberately stays click/double_click-scoped (see
    agent.py REM-B H2c: generalizing it let a mere window change verify a stated
    effect — a false success the fault-injection pin catches). right_click keeps the
    plain visual_change intent; an opened context menu is a large pixel change.
    """
    intent = _intent_agent()._build_intent(right_click((50, 30)), None, "context menu opens")
    assert intent.kind == "visual_change"
    assert intent.expected_change is True
    from computer_use_mcp.agent import FOCUS_CHANGE_INTENT_FLAG

    assert FOCUS_CHANGE_INTENT_FLAG not in (intent.metadata or {})


# --- server: served schema advertises right_click (no anyOf/$ref) + integration ------------------


def _tool_meta(tool_name: str) -> Any:
    tool = server.mcp._tool_manager.get_tool(tool_name)
    assert tool is not None, f"tool {tool_name} not registered"
    return tool.fn_metadata


def test_served_computer_execute_schema_advertises_right_click() -> None:
    """The WIRE contract: the served description lists right_click; the schema is plain."""
    tool = server.mcp._tool_manager.get_tool("computer_execute")
    description = tool.description or ""
    assert "right_click" in description
    schema = _tool_meta("computer_execute").arg_model.model_json_schema()
    flattened = json.dumps(schema)
    assert "anyOf" not in flattened, "anyOf leaked into the served computer_execute schema"
    assert "$ref" not in flattened, "$ref leaked into the served computer_execute schema"
    assert "ActionType" not in flattened  # action stays a plain string on the wire


def test_teaching_invalid_action_hints_right_click_for_context_menu_misnomers() -> None:
    result = server._teaching_invalid_action(ValueError("bad"), "context_menu")
    hints = " ".join(str(reason) for reason in result["reasons"])
    assert 'action="right_click"' in hints


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


def _execute_payload(result: Any) -> dict[str, Any]:
    """Unwrap an executed computer_execute response (blocks) to its dict."""
    if isinstance(result, list):
        return json.loads(result[0].text)
    return result


async def test_server_execute_right_click_through_full_pipeline(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """computer_execute("right_click") end-to-end: grounded, bound, executed, verified."""
    from test_move_action import FAST_LIMITS  # reuse the zero-throttle limits

    class _FlippingFake(FakeComputerBackend):
        """Screenshot flips color on every completed execute -> visual_change verifies."""

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._executes = 0

        def observe(self) -> Observation:
            observation = super().observe()
            if self._executes % 2 == 1:
                image = Image.new("RGB", (self.width, self.height), "black")
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                observation.image_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
            return observation

        def execute(self, action: GroundedAction, stop: Any = None) -> str:
            message = super().execute(action, stop)
            self._executes += 1
            return message

    backend = _FlippingFake(width=800, height=600)
    session_id = _make_session(
        monkeypatch, backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = _execute_payload(
        await server.computer_execute(session_id, "right_click", x=400, y=300)
    )
    assert result["ok"] is True, result
    assert result["action"]["action"] == "right_click"
    assert result["verification"]["outcome"] == "verified"
    assert backend.executed[0].action is ActionType.RIGHT_CLICK
    assert backend._cursor_physical == (400, 300)


async def test_server_rejects_right_click_out_of_bounds_typed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_move_action import FAST_LIMITS

    backend = FakeComputerBackend(width=800, height=600)
    session_id = _make_session(
        monkeypatch, backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = _execute_payload(
        await server.computer_execute(session_id, "right_click", x=8000, y=10)
    )
    assert result["ok"] is False
    assert backend.executed == []


async def test_server_right_click_respects_approval_gate(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With require_approval=True a right_click asks for approval like click."""
    from test_move_action import FAST_LIMITS

    backend = FakeComputerBackend(width=800, height=600)
    session_id = _make_session(
        monkeypatch, backend, dry_run=False, require_approval=True, limits=FAST_LIMITS
    )
    result = _execute_payload(
        await server.computer_execute(session_id, "right_click", x=400, y=300)
    )
    assert result["ok"] is False
    assert result.get("requires_approval") is True
    assert "right_click at screenshot coordinates" in result.get("message", "")
    assert backend.executed == []
