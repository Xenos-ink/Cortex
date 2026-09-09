"""HOTKEY action tests: compound keyboard chords (2..12 keys) as a keypress sibling.

The hotkey is an additive ActionType member reusing the existing ``keys`` field. It is
deliberately split from keypress: a hotkey is a COMPOUND chord (2..12 non-empty key
names, passed verbatim in pyautogui vocabulary); single-key presses stay on keypress.
Grounding is trivial (non-spatial), stop discipline is one check immediately before the
single chord input, and the shape guard fails closed at both the model and the backend.
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
from computer_use_mcp.backend import FakeComputerBackend, InputBlockedError
from computer_use_mcp.grounding import GroundingRouter
from computer_use_mcp.models import ActionType, GroundedAction, SessionState
from computer_use_mcp.provider import ProviderParseError, parse_decision
from computer_use_mcp.safety import RiskLevel, SafetyContext, SafetyPolicy
from computer_use_mcp.state import SessionRegistry, StopToken, TaskStopped
from computer_use_mcp.validator import GroundingValidator

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")


def hotkey(keys: list[str], **kwargs: Any) -> GroundedAction:
    kwargs.setdefault("confidence", 1.0)
    return GroundedAction(action="hotkey", keys=keys, **kwargs)


# The session-scoped ``real_backend`` fixture comes from tests/conftest.py, which also
# provides the RecordingEngine stub (no real input dispatch).


# --- models: hotkey is a compound chord -----------------------------------------------------------


def test_hotkey_action_member_value() -> None:
    assert ActionType.HOTKEY.value == "hotkey"
    assert ActionType("hotkey") is ActionType.HOTKEY


def test_hotkey_reuses_keys_field_and_accepts_valid_chord() -> None:
    action = hotkey(["ctrl", "shift", "s"])
    assert action.keys == ["ctrl", "shift", "s"]
    assert action.point is None and action.to_point is None  # non-spatial


def test_hotkey_single_key_rejected() -> None:
    with pytest.raises(ValidationError, match="2 to 12"):
        GroundedAction(action="hotkey", keys=["ctrl"])


def test_hotkey_more_than_twelve_keys_rejected() -> None:
    with pytest.raises(ValidationError):
        GroundedAction(action="hotkey", keys=[f"k{index}" for index in range(13)])


def test_hotkey_empty_or_blank_key_names_rejected() -> None:
    with pytest.raises(ValidationError, match="2 to 12"):
        GroundedAction(action="hotkey", keys=["ctrl", ""])
    with pytest.raises(ValidationError, match="2 to 12"):
        GroundedAction(action="hotkey", keys=["ctrl", "   "])


# --- FakeComputerBackend: hotkey execution contract -----------------------------------------------


def test_fake_hotkey_records() -> None:
    backend = FakeComputerBackend()
    action = hotkey(["ctrl", "s"])
    message = backend.execute(action)
    assert message == "Simulated hotkey."
    assert backend.executed == [action]


def test_fake_hotkey_prestopped_token_performs_zero_inputs() -> None:
    backend = FakeComputerBackend()
    stop = StopToken()
    stop.stop()
    with pytest.raises(TaskStopped):
        backend.execute(hotkey(["ctrl", "s"]), stop=stop)
    assert backend.executed == []


def test_fake_hotkey_input_blocked_records_nothing() -> None:
    backend = FakeComputerBackend(input_blocked=True)
    with pytest.raises(InputBlockedError):
        backend.execute(hotkey(["ctrl", "s"]))
    assert backend.executed == []


# --- LocalComputerBackend (real path, stubbed input engine) -----------------------------------------


@WINDOWS_ONLY
def test_real_hotkey_calls_chord_with_key_names_verbatim(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    message = real_backend.execute(hotkey(["ctrl", "s"]))
    assert message == "Executed hotkey."
    assert engine.calls == [("chord", "ctrl", "s")]


@WINDOWS_ONLY
def test_real_hotkey_prestopped_token_performs_zero_inputs(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    stop = StopToken()
    stop.stop()
    with pytest.raises(TaskStopped):
        real_backend.execute(hotkey(["ctrl", "s"]), stop=stop)
    assert engine.calls == []


@WINDOWS_ONLY
def test_real_hotkey_bad_chord_value_error_at_backend(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller bug that bypasses model validation still fails closed at the backend."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    single = GroundedAction.model_construct(action=ActionType.HOTKEY, keys=["ctrl"])
    with pytest.raises(ValueError, match="2 to 12"):
        real_backend.execute(single)
    blank = GroundedAction.model_construct(action=ActionType.HOTKEY, keys=["ctrl", "  "])
    with pytest.raises(ValueError, match="2 to 12"):
        real_backend.execute(blank)
    assert engine.calls == []


# --- grounding: hotkey is non-spatial (trivial "none") ----------------------------------------------


def test_grounding_hotkey_is_trivially_none() -> None:
    backend = FakeComputerBackend()
    observation = backend.observe()
    grounding = GroundingRouter().route(hotkey(["ctrl", "s"], confidence=0.9), observation)
    assert grounding.strategy == "none"
    assert "non-spatial" in grounding.evidence[0]


# --- validator: shape check extends to hotkey -------------------------------------------------------


def test_validator_hotkey_empty_keys_missing_keys() -> None:
    """A caller bug that bypasses model validation is caught by the shape check too."""
    backend = FakeComputerBackend()
    observation = backend.observe()
    outcome = GroundingValidator().validate(
        GroundedAction.model_construct(action=ActionType.HOTKEY, keys=[]), observation
    )
    assert outcome.valid is False
    assert "missing_keys" in outcome.codes


def test_validator_hotkey_valid_chord_is_valid() -> None:
    backend = FakeComputerBackend()
    observation = backend.observe()
    outcome = GroundingValidator().validate(hotkey(["ctrl", "s"]), observation)
    assert outcome.valid is True
    assert outcome.codes == []


# --- safety: hotkey is classified exactly like keypress ---------------------------------------------


def test_safety_hotkey_state_changing_combo_is_medium_and_gated() -> None:
    policy = SafetyPolicy()
    risk, category, why = policy.classify(hotkey(["ctrl", "s"]), SafetyContext())
    assert risk is RiskLevel.MEDIUM
    assert category == "keyboard_shortcut_state_change"
    assert why == "the key combination can alter application state"
    decision = policy.evaluate(hotkey(["ctrl", "s"]), SessionState(session_id="t"))
    assert decision.allowed is True
    assert decision.requires_approval is True
    assert decision.risk is RiskLevel.MEDIUM
    relaxed = policy.evaluate(
        hotkey(["ctrl", "s"]), SessionState(session_id="t", require_approval=False)
    )
    assert relaxed.requires_approval is False


def test_safety_hotkey_plain_combo_is_low_and_ungated() -> None:
    policy = SafetyPolicy()
    risk, category, _why = policy.classify(hotkey(["shift", "f5"]), SafetyContext())
    assert risk is RiskLevel.LOW
    assert category == "low_routine_action"
    decision = policy.evaluate(hotkey(["shift", "f5"]), SessionState(session_id="t"))
    assert decision.allowed is True
    assert decision.requires_approval is False  # even with require_approval=True


# --- provider: strict parsing accepts hotkey, rejects a single key -----------------------------------


def test_parse_decision_accepts_hotkey() -> None:
    payload = json.dumps(
        {
            "status": "action",
            "action": {"action": "hotkey", "keys": ["ctrl", "s"], "confidence": 0.9},
        }
    )
    decision = parse_decision(payload)
    assert decision.status == "action"
    assert decision.action is not None
    assert decision.action.action is ActionType.HOTKEY
    assert decision.action.keys == ["ctrl", "s"]


def test_parse_decision_hotkey_single_key_fails_closed() -> None:
    payload = json.dumps({"status": "action", "action": {"action": "hotkey", "keys": ["ctrl"]}})
    with pytest.raises(ProviderParseError):
        parse_decision(payload)


# --- verification intents: visual_change default + launch-prefix promotion ---------------------------


def test_verification_hotkey_defaults_to_visual_change_with_stated_effect() -> None:
    agent = ComputerUseAgent(FakeComputerBackend(), provider=object(), session_id="t")
    intent = agent._build_intent(hotkey(["ctrl", "s"]), None, "The document is saved.")
    assert intent.kind == "visual_change"
    assert intent.expected_change is True


def test_verification_hotkey_launch_prefix_promotes_to_window_state() -> None:
    agent = ComputerUseAgent(FakeComputerBackend(), provider=object(), session_id="t")
    intent = agent._build_intent(hotkey(["ctrl", "shift", "esc"]), None, "open Task Manager")
    assert intent.kind == "window_state"
    assert intent.expected_window_title == "Task Manager"


def test_verification_hotkey_without_effect_is_visual_change_without_expectation() -> None:
    agent = ComputerUseAgent(FakeComputerBackend(), provider=object(), session_id="t")
    intent = agent._build_intent(hotkey(["ctrl", "s"]), None, None)
    assert intent.kind == "visual_change"
    assert intent.expected_change is None


# --- server: computer_execute hotkey integration -----------------------------------------------------


class _FlippingFake(FakeComputerBackend):
    """FakeComputerBackend whose screenshot flips color on every completed execute."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._executes = 0

    def observe(self) -> Any:
        observation = super().observe()
        if self._executes % 2 == 1:
            observation.image_base64 = _png("black", self.width, self.height)
        return observation

    def execute(self, action: GroundedAction, stop: Any = None) -> str:
        message = super().execute(action, stop)
        self._executes += 1
        return message


def _png(color: str, width: int, height: int) -> str:
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


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


def _execute_payload(result: Any) -> dict[str, Any]:
    """REM-A: unwrap an executed computer_execute response (blocks) to its dict."""
    if isinstance(result, list):
        return json.loads(result[0].text)
    return result


async def test_computer_execute_hotkey_executes_and_records(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _FlippingFake()
    session_id = _make_session(
        monkeypatch, backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = await server.computer_execute(session_id, "hotkey", keys=["ctrl", "s"])
    result = _execute_payload(result)  # REM-A: executed -> content blocks
    assert result["ok"] is True, result
    assert result["message"] == "Simulated hotkey."
    assert result["verification"]["outcome"] == "verified"  # flip: visual change detected
    assert len(backend.executed) == 1
    executed = backend.executed[0]
    assert executed.action is ActionType.HOTKEY
    assert executed.keys == ["ctrl", "s"]


async def test_computer_execute_hotkey_single_key_fails_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _FlippingFake()
    session_id = _make_session(
        monkeypatch, backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = await server.computer_execute(session_id, "hotkey", keys=["ctrl"])
    assert result["ok"] is False
    assert result["error"] == "invalid_action"
    assert backend.executed == []
