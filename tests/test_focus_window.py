"""FOCUS_WINDOW action tests: bring a window to the foreground by title.

The focus_window is an additive ActionType member with a dedicated trailing-optional
``target`` field (a window title — deliberately NOT ``text``, which is the
redaction/secret-scan channel). Covered here:

- models: ``focus_window`` requires a non-empty stripped target (fail closed);
  ``target`` stays ``None`` for other actions and is length-bounded to 200;
- backends (fake + real): stop/input-blocked gates record nothing; a title miss raises
  ``WindowFocusError`` and records nothing; the fake swaps ``active_window`` and records
  ``focused``; the real backend runs the AttachThreadInput + ALT-nudge sequence and
  verifies the foreground actually moved (refusal -> ``WindowFocusError``);
- window lookup: case-insensitive title match with precedence exact > prefix >
  substring, first in Z-order (module-level ``find_window_by_title`` and the fake's
  identical rule over its injected ``windows`` list);
- grounding is trivial (non-spatial); the validator adds the ``missing_target`` code;
- the agent-level allowlist gate checks the focus TARGET (pre-foreground) against BOTH
  configured allowlists: the process allowlist (``process_identity_unavailable`` /
  ``process_not_allowed``) and the window-title allowlist (``window_identity_unavailable``
  / ``window_not_allowed``), all mapping to WRONG_WINDOW like ordinary violations.
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
    ComputerBackend,
    FakeComputerBackend,
    InputBlockedError,
    WindowFocusError,
)
from computer_use_mcp.grounding import GroundingRouter
from computer_use_mcp.limits import Limits
from computer_use_mcp.models import (
    ActionType,
    FailureClass,
    GroundedAction,
    Observation,
    SessionState,
    WindowInfo,
)
from computer_use_mcp.provider import (
    RESPONSE_SCHEMA_TEXT,
    ProviderParseError,
    parse_decision,
)
from computer_use_mcp.recovery import _VALIDATION_CODE_MAP
from computer_use_mcp.safety import RiskLevel, SafetyContext, SafetyPolicy
from computer_use_mcp.state import SessionRegistry, StopToken, TaskStopped
from computer_use_mcp.validator import GroundingValidator, WindowIdentityUnavailableError
from computer_use_mcp.verification import VerificationEngine

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")


def focus(target: str, **kwargs: Any) -> GroundedAction:
    kwargs.setdefault("confidence", 1.0)
    return GroundedAction(action="focus_window", target=target, **kwargs)


PAINT = WindowInfo(hwnd=1, pid=11, process_name="mspaint.exe", title="Untitled - Paint")
CALC = WindowInfo(hwnd=2, pid=22, process_name="calc.exe", title="Calculator")
NOTEPAD = WindowInfo(hwnd=3, pid=33, process_name="notepad.exe", title="Untitled - Notepad")


# --- models: focus_window requires a target -------------------------------------------------------


def test_focus_window_member_value() -> None:
    assert ActionType.FOCUS_WINDOW.value == "focus_window"
    assert ActionType("focus_window") is ActionType.FOCUS_WINDOW


def test_focus_window_requires_non_empty_target() -> None:
    ok = focus("Calculator")
    assert ok.target == "Calculator"
    with pytest.raises(ValidationError, match="target"):
        GroundedAction(action="focus_window")
    with pytest.raises(ValidationError, match="target"):
        GroundedAction(action="focus_window", target="")
    with pytest.raises(ValidationError, match="target"):
        GroundedAction(action="focus_window", target="   ")


def test_target_defaults_to_none_for_other_actions() -> None:
    assert GroundedAction(action="click", point={"x": 1, "y": 2}).target is None
    assert GroundedAction(action="keypress", keys=["esc"]).target is None
    assert GroundedAction(action="done").target is None


def test_target_over_200_chars_rejected() -> None:
    with pytest.raises(ValidationError):
        GroundedAction(action="focus_window", target="x" * 201)
    assert GroundedAction(action="focus_window", target="x" * 200).target == "x" * 200


# --- FakeComputerBackend: focus_window + find_window_by_title --------------------------------------


def test_fake_focus_window_swaps_active_window_and_records_focused() -> None:
    backend = FakeComputerBackend(active_window=PAINT, windows=[PAINT, CALC, NOTEPAD])
    action = focus("Calculator")
    message = backend.execute(action)
    assert message == "Simulated focus_window."
    assert backend.active_window == CALC
    assert backend.active_window is not CALC  # stored as a defensive copy
    assert backend.focused == ["Calculator"]
    assert backend.executed == [action]
    observation = backend.observe()
    assert observation.active_window == "Calculator"
    assert observation.active_window_info is not None
    assert observation.active_window_info.process_name == "calc.exe"


def test_fake_focus_window_no_match_raises_and_records_nothing() -> None:
    backend = FakeComputerBackend(active_window=PAINT, windows=[PAINT])
    with pytest.raises(WindowFocusError, match="no window matching 'Calculator'"):
        backend.execute(focus("Calculator"))
    assert backend.executed == []
    assert backend.focused == []
    assert backend.active_window is PAINT  # unchanged


def test_fake_focus_window_prestopped_token_performs_zero_inputs() -> None:
    backend = FakeComputerBackend(active_window=PAINT, windows=[PAINT, CALC])
    stop = StopToken()
    stop.stop()
    with pytest.raises(TaskStopped):
        backend.execute(focus("Calculator"), stop=stop)
    assert backend.executed == []
    assert backend.focused == []
    assert backend.active_window is PAINT


def test_fake_focus_window_input_blocked_records_nothing() -> None:
    backend = FakeComputerBackend(active_window=PAINT, windows=[PAINT, CALC], input_blocked=True)
    with pytest.raises(InputBlockedError):
        backend.execute(focus("Calculator"))
    assert backend.executed == []
    assert backend.focused == []


def test_fake_find_window_by_title_precedence_exact_over_prefix_over_substring() -> None:
    backend = FakeComputerBackend(
        windows=[
            WindowInfo(title="My Calculator Notes"),  # substring, top of Z-order
            WindowInfo(title="Calculator Plus"),  # prefix
            WindowInfo(title="CALCULATOR"),  # exact, last in Z-order
        ]
    )
    assert backend.find_window_by_title("Calculator") is not None
    found = backend.find_window_by_title("Calculator")
    assert found is not None and found.title == "CALCULATOR"  # exact wins regardless of Z-order
    prefix_only = FakeComputerBackend(
        windows=[WindowInfo(title="Calculator Plus"), WindowInfo(title="My Calculator Notes")]
    )
    assert prefix_only.find_window_by_title("Calculator").title == "Calculator Plus"
    assert prefix_only.find_window_by_title("Notes").title == "My Calculator Notes"
    assert prefix_only.find_window_by_title("nomatch") is None
    assert FakeComputerBackend(windows=[WindowInfo(title="X")]).find_window_by_title("") is None


def test_fake_find_window_by_title_is_case_insensitive_and_strips() -> None:
    backend = FakeComputerBackend(windows=[PAINT, CALC])
    assert backend.find_window_by_title("  CALCULATOR ").title == "Calculator"


def test_backend_default_find_window_returns_none_without_enumeration() -> None:
    class _BareBackend(ComputerBackend):
        def observe(self) -> Any:
            raise AssertionError("not used")

        def execute(self, action: Any, stop: Any = None) -> str:
            raise AssertionError("not used")

        def _build_default_context(self) -> Any:
            raise AssertionError("not used")

    assert _BareBackend().find_window_by_title("anything") is None


def test_fake_windows_default_to_active_window_list() -> None:
    assert FakeComputerBackend(active_window=CALC).windows == [CALC]
    assert FakeComputerBackend().windows == []
    injected = FakeComputerBackend(active_window=CALC, windows=[PAINT])
    assert injected.windows == [PAINT]


# --- LocalComputerBackend (real path, fake Win32): the focus sequence ------------------------------


class _FakeUser32:
    """Win32 user32 stand-in monkeypatched over ``backend_module._user32``."""

    def __init__(
        self,
        windows: dict[int, tuple[str, str, tuple[int, int, int, int]]],
        z_order: list[int],
        foreground: int,
    ) -> None:
        self._windows = windows  # hwnd -> (title, class, (left, top, width, height))
        self._z_order = z_order  # EnumWindows order: top of Z-order first
        self.foreground = foreground
        self.minimized: set[int] = set()
        self.show_calls: list[tuple[int, int]] = []
        self.set_foreground_calls: list[int] = []
        self.attach_calls: list[tuple[int, int, bool]] = []
        self.keybd_calls: list[tuple[int, int, int, int]] = []
        self.refuse_focus = False

    # enumeration + identity (mirrors the real call patterns in backend.py)
    def EnumWindows(self, callback: Any, lparam: int) -> int:
        for hwnd in list(self._z_order):
            callback(hwnd, lparam)
        return 1

    def GetAncestor(self, hwnd: int, _flag: int) -> int:
        return hwnd  # every fake window is its own root owner

    def GetWindowTextLengthW(self, hwnd: int) -> int:
        return len(self._windows.get(hwnd, ("",))[0])

    def GetWindowTextW(self, hwnd: int, buffer: Any, size: int) -> int:
        title = self._windows.get(hwnd, ("",))[0]
        buffer.value = title
        return len(title)

    def GetClassNameW(self, hwnd: int, buffer: Any, size: int) -> int:
        entry = self._windows.get(hwnd)
        buffer.value = (entry[1] if entry else "") or ""
        return 1

    def GetWindowThreadProcessId(self, hwnd: int, pid_ptr: Any) -> int:
        if pid_ptr is not None and getattr(pid_ptr, "_obj", None) is not None:
            pid_ptr._obj.value = 1000 + hwnd
        return 4242

    def GetWindowRect(self, hwnd: int, rect_ptr: Any) -> int:
        rect = rect_ptr._obj
        left, top, width, height = self._windows.get(hwnd, ("", None, (0, 0, 0, 0)))[2]
        rect.left, rect.top = left, top
        rect.right, rect.bottom = left + width, top + height
        return 1

    # focus sequence
    def GetForegroundWindow(self) -> int:
        return self.foreground

    def SetForegroundWindow(self, hwnd: int) -> int:
        self.set_foreground_calls.append(hwnd)
        if not self.refuse_focus:
            self.foreground = hwnd
        return 1

    def IsIconic(self, hwnd: int) -> int:
        return 1 if hwnd in self.minimized else 0

    def ShowWindow(self, hwnd: int, cmd: int) -> int:
        self.show_calls.append((hwnd, cmd))
        return 1

    def AttachThreadInput(self, me: int, fg_thread: int, attach: Any) -> int:
        self.attach_calls.append((me, fg_thread, bool(attach)))
        return 1

    def keybd_event(self, vk: int, scan: int, flags: int, extra: int) -> None:
        self.keybd_calls.append((vk, scan, flags, extra))


class _FakeKernel32:
    """Win32 kernel32 stand-in: fixed thread id, no process handles (exe degrades)."""

    def GetCurrentThreadId(self) -> int:
        return 4242

    def OpenProcess(self, *args: Any) -> int:
        return 0

    def QueryFullProcessImageNameW(self, *args: Any) -> int:
        return 0

    def CloseHandle(self, handle: Any) -> int:
        return 1


def _install_fake_win32(
    monkeypatch: pytest.MonkeyPatch, user32: _FakeUser32
) -> tuple[_FakeUser32, _FakeKernel32]:
    kernel32 = _FakeKernel32()
    monkeypatch.setattr(backend_module, "_user32", user32)
    monkeypatch.setattr(backend_module, "_kernel32", kernel32)
    return user32, kernel32


@WINDOWS_ONLY
def test_real_focus_window_success_path(real_backend, monkeypatch: pytest.MonkeyPatch) -> None:
    user32, _ = _install_fake_win32(
        monkeypatch,
        _FakeUser32(
            windows={
                1: ("Untitled - Paint", "Notepad", (0, 0, 800, 600)),
                2: ("Calculator", "Calc", (100, 100, 400, 300)),
            },
            z_order=[1, 2],
            foreground=1,
        ),
    )
    action = focus("calculator")  # case-insensitive
    message = real_backend.execute(action)
    assert message == "Focused window 'Calculator'."
    assert user32.foreground == 2
    assert user32.set_foreground_calls == [2]
    assert [(attach) for _, _, attach in user32.attach_calls] == [True, False]
    assert user32.keybd_calls == [(0x12, 0, 0, 0), (0x12, 0, 2, 0)]  # ALT nudge down+up
    # The ALT nudge and SetForegroundWindow happen while ATTACHED.
    assert len(user32.attach_calls) == 2
    assert user32.show_calls == []  # not minimized -> no restore


@WINDOWS_ONLY
def test_real_focus_window_refused_by_set_foreground_window(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    user32, _ = _install_fake_win32(
        monkeypatch,
        _FakeUser32(
            windows={
                1: ("Untitled - Paint", "Notepad", (0, 0, 800, 600)),
                2: ("Calculator", "Calc", (100, 100, 400, 300)),
            },
            z_order=[1, 2],
            foreground=1,
        ),
    )
    user32.refuse_focus = True  # SetForegroundWindow silently does nothing
    with pytest.raises(WindowFocusError, match="refused"):
        real_backend.execute(focus("Calculator"))
    # The sequence still ran cleanly: attached, nudged, attempted, DETACHED.
    assert [(attach) for _, _, attach in user32.attach_calls] == [True, False]
    assert user32.keybd_calls == [(0x12, 0, 0, 0), (0x12, 0, 2, 0)]
    assert user32.foreground == 1  # foreground never moved


@WINDOWS_ONLY
def test_real_focus_window_no_match_raises(real_backend, monkeypatch: pytest.MonkeyPatch) -> None:
    user32, _ = _install_fake_win32(
        monkeypatch,
        _FakeUser32(windows={1: ("Untitled - Paint", "Notepad", (0, 0, 800, 600))}, z_order=[1], foreground=1),
    )
    with pytest.raises(WindowFocusError, match="no window matching 'Ghost Window'"):
        real_backend.execute(focus("Ghost Window"))
    assert user32.set_foreground_calls == []
    assert user32.attach_calls == []


@WINDOWS_ONLY
def test_real_focus_window_restores_minimized_window(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    user32, _ = _install_fake_win32(
        monkeypatch,
        _FakeUser32(
            windows={2: ("Calculator", "Calc", (100, 100, 400, 300))},
            z_order=[2],
            foreground=0,  # no foreground window at all
        ),
    )
    user32.minimized.add(2)
    message = real_backend.execute(focus("Calculator"))
    assert message == "Focused window 'Calculator'."
    assert user32.show_calls == [(2, 9)]  # SW_RESTORE
    assert user32.foreground == 2


@WINDOWS_ONLY
def test_real_focus_window_requires_windows(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_module, "IS_WINDOWS", False)
    with pytest.raises(WindowFocusError, match="requires Windows"):
        real_backend.execute(focus("Calculator"))


@WINDOWS_ONLY
def test_real_find_window_by_title_populates_identity(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_win32(
        monkeypatch,
        _FakeUser32(
            windows={
                1: ("Untitled - Paint", "Notepad", (0, 0, 800, 600)),
                2: ("Calculator", "Calc", (100, 100, 400, 300)),
            },
            z_order=[2, 1],
            foreground=1,
        ),
    )
    info = backend_module.find_window_by_title("Calculator")
    assert info is not None
    assert info.hwnd == 2
    assert info.pid == 1002
    assert info.window_class == "Calc"
    assert info.title == "Calculator"
    assert info.bounds == (100, 100, 400, 300)
    assert backend_module.find_window_by_title("Ghost") is None


# --- grounding: focus_window is non-spatial (trivial "none") ----------------------------------------


def test_grounding_focus_window_is_trivially_none() -> None:
    backend = FakeComputerBackend()
    observation = backend.observe()
    grounding = GroundingRouter().route(focus("Calculator", confidence=0.9), observation)
    assert grounding.strategy == "none"
    assert "non-spatial" in grounding.evidence[0]


# --- validator: missing_target for focus_window ------------------------------------------------------


def test_validator_focus_window_empty_target_missing_target() -> None:
    """A caller bug that bypasses model validation is caught by the shape check too."""
    backend = FakeComputerBackend()
    observation = backend.observe()
    outcome = GroundingValidator().validate(
        GroundedAction.model_construct(action=ActionType.FOCUS_WINDOW, target="   "), observation
    )
    assert outcome.valid is False
    assert "missing_target" in outcome.codes


def test_validator_focus_window_with_target_is_valid() -> None:
    backend = FakeComputerBackend()
    observation = backend.observe()
    outcome = GroundingValidator().validate(focus("Calculator"), observation)
    assert outcome.valid is True
    assert outcome.codes == []


# --- safety: focus_window is always MEDIUM / window_focus_change -------------------------------------


def test_safety_focus_window_is_medium_window_focus_change() -> None:
    risk, category, why = SafetyPolicy().classify(focus("Calculator"), SafetyContext())
    assert risk is RiskLevel.MEDIUM
    assert category == "window_focus_change"
    assert why == "the action brings a different window to the foreground"


def test_safety_focus_window_requires_approval_by_default() -> None:
    policy = SafetyPolicy()
    decision = policy.evaluate(focus("Calculator"), SessionState(session_id="t"))
    assert decision.allowed is True
    assert decision.requires_approval is True
    assert decision.risk is RiskLevel.MEDIUM
    assert decision.category == "window_focus_change"
    relaxed = policy.evaluate(
        focus("Calculator"), SessionState(session_id="t", require_approval=False)
    )
    assert relaxed.requires_approval is False


def test_safety_focus_approval_message_names_target() -> None:
    decision = SafetyPolicy().evaluate(focus("Calculator"), SessionState(session_id="t"))
    assert "focus_window window 'Calculator'" in decision.reason  # action names the target
    assert "focusing window 'Calculator'" in decision.reason  # target location names it too
    assert "Subsequent input could land in an unintended application." in decision.reason


# --- agent-level allowlist gate (fail-closed focus resolution) ----------------------------------------


def _gate_agent(backend: FakeComputerBackend, allowed: list[str] | None) -> ComputerUseAgent:
    return ComputerUseAgent(
        backend,
        provider=object(),
        session_id="focus-gate",
        allowed_processes=allowed or None,
        limits=Limits(min_screenshot_interval_ms=0),
    )


def _gate_state(allowed_windows: list[str] | None = None) -> SessionState:
    return SessionState(
        session_id="focus-gate",
        dry_run=False,
        require_approval=False,
        allowed_windows=list(allowed_windows or []),
    )


def _window_observation(title: str) -> Observation:
    image = Image.new("RGB", (64, 48), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Observation(
        image_base64=base64.b64encode(buffer.getvalue()).decode("ascii"),
        width=64,
        height=48,
        active_window=title,
        active_window_info=WindowInfo(title=title),
    )


async def test_allowlisted_focus_target_outside_allowlist_rejected_before_execution() -> None:
    # The ACTIVE window (notepad.exe) satisfies the allowlist, so the validator passes
    # and the focus-specific gate is what rejects the calc.exe TARGET.
    backend = FakeComputerBackend(active_window=NOTEPAD, windows=[NOTEPAD, CALC])
    agent = _gate_agent(backend, ["notepad.exe"])
    outcome = await agent.run_single(_gate_state(), focus("Calculator"))
    assert outcome.kind == "rejected"
    assert outcome.result is None
    assert any("calc.exe" in reason for reason in outcome.reasons)
    assert backend.executed == []  # the backend never ran
    assert backend.focused == []
    rejection = agent._focus_allowlist_rejection(focus("Calculator"))
    assert rejection is not None
    assert rejection.codes == ["process_not_allowed"]


async def test_allowlisted_focus_target_not_found_is_fail_closed() -> None:
    backend = FakeComputerBackend(active_window=NOTEPAD, windows=[NOTEPAD])
    agent = _gate_agent(backend, ["notepad.exe"])
    rejection = agent._focus_allowlist_rejection(focus("Ghost Window"))
    assert rejection is not None
    assert rejection.codes == ["process_identity_unavailable"]
    outcome = await agent.run_single(_gate_state(), focus("Ghost Window"))
    assert outcome.kind == "rejected"
    assert backend.executed == []


async def test_focus_without_allowlist_executes_and_verifies_window_state() -> None:
    backend = FakeComputerBackend(active_window=NOTEPAD, windows=[NOTEPAD, CALC])
    agent = _gate_agent(backend, None)
    outcome = await agent.run_single(_gate_state(), focus("Calculator"))
    assert outcome.kind == "executed"
    assert outcome.result is not None and outcome.result.ok is True
    assert backend.focused == ["Calculator"]
    assert backend.active_window == CALC
    assert outcome.result.verification is not None
    assert outcome.result.verification.outcome == "verified"  # deterministic window_state


def test_focus_gate_skipped_without_allowlist_even_for_unresolvable_target() -> None:
    agent = _gate_agent(FakeComputerBackend(active_window=NOTEPAD, windows=[NOTEPAD]), None)
    assert agent._focus_allowlist_rejection(focus("Ghost Window")) is None


# --- agent-level title allowlist gate (M-1): focus_window respects allowed_windows --------------------


async def test_title_allowlist_blocks_focus_to_disallowed_title_before_execution() -> None:
    backend = FakeComputerBackend(active_window=NOTEPAD, windows=[NOTEPAD, CALC])
    agent = _gate_agent(backend, None)  # title allowlist only (no process allowlist)
    state = _gate_state(allowed_windows=["Notepad"])
    outcome = await agent.run_single(state, focus("Calculator"))
    assert outcome.kind == "rejected"
    assert outcome.result is None
    assert backend.executed == []  # zero backend executions: containment is pre-foreground
    assert backend.focused == []
    rejection = agent._focus_allowlist_rejection(focus("Calculator"), state)
    assert rejection is not None
    assert rejection.codes == ["window_not_allowed"]
    assert any("Calculator" in reason for reason in rejection.reasons)


async def test_title_allowlist_allows_matching_title_focus() -> None:
    backend = FakeComputerBackend(active_window=NOTEPAD, windows=[NOTEPAD, CALC])
    agent = _gate_agent(backend, None)
    state = _gate_state(allowed_windows=["Notepad"])
    outcome = await agent.run_single(state, focus("Notepad"))
    assert outcome.kind == "executed"
    assert outcome.result is not None and outcome.result.ok is True
    assert backend.focused == ["Notepad"]
    assert outcome.result.verification is not None
    assert outcome.result.verification.outcome == "verified"


async def test_title_allowlist_unresolvable_target_fails_closed() -> None:
    backend = FakeComputerBackend(active_window=NOTEPAD, windows=[NOTEPAD])
    agent = _gate_agent(backend, None)
    state = _gate_state(allowed_windows=["Notepad"])
    rejection = agent._focus_allowlist_rejection(focus("Ghost Window"), state)
    assert rejection is not None
    assert rejection.codes == ["window_identity_unavailable"]
    assert isinstance(rejection.error, WindowIdentityUnavailableError)
    # Recovery mapping mirrors the process-identity fail-closed code: WRONG_WINDOW.
    assert _VALIDATION_CODE_MAP["window_identity_unavailable"] is FailureClass.WRONG_WINDOW
    outcome = await agent.run_single(state, focus("Ghost Window"))
    assert outcome.kind == "rejected"
    assert backend.executed == []


async def test_combined_allowlists_process_gate_still_applies() -> None:
    backend = FakeComputerBackend(active_window=NOTEPAD, windows=[NOTEPAD, CALC])
    agent = _gate_agent(backend, ["notepad.exe"])
    state = _gate_state(allowed_windows=["Notepad"])
    rejection = agent._focus_allowlist_rejection(focus("Calculator"), state)
    assert rejection is not None
    assert rejection.codes == ["process_not_allowed"]  # process gate fires first
    outcome = await agent.run_single(state, focus("Calculator"))
    assert outcome.kind == "rejected"
    assert backend.executed == []


async def test_combined_allowlists_title_gate_applies_after_process_passes() -> None:
    # notepad.exe process but a disallowed TITLE: the process gate passes and the
    # title gate is what rejects — both gates apply.
    sneaky = WindowInfo(hwnd=4, pid=44, process_name="notepad.exe", title="Calculator")
    backend = FakeComputerBackend(active_window=NOTEPAD, windows=[NOTEPAD, sneaky])
    agent = _gate_agent(backend, ["notepad.exe"])
    state = _gate_state(allowed_windows=["Notepad"])
    rejection = agent._focus_allowlist_rejection(focus("Calculator"), state)
    assert rejection is not None
    assert rejection.codes == ["window_not_allowed"]
    outcome = await agent.run_single(state, focus("Calculator"))
    assert outcome.kind == "rejected"
    assert backend.executed == []


async def test_combined_allowlists_matching_target_passes_both_gates() -> None:
    backend = FakeComputerBackend(active_window=NOTEPAD, windows=[NOTEPAD, CALC])
    agent = _gate_agent(backend, ["notepad.exe"])
    state = _gate_state(allowed_windows=["Notepad"])
    outcome = await agent.run_single(state, focus("Notepad"))
    assert outcome.kind == "executed"
    assert outcome.result is not None and outcome.result.ok is True
    assert backend.focused == ["Notepad"]
    assert backend.executed == [backend.executed[0]]  # exactly one execution


# --- verification: deterministic window_state for focus_window ----------------------------------------


def test_verification_focus_already_focused_window_is_verified() -> None:
    agent = _gate_agent(FakeComputerBackend(), None)
    intent = agent._build_intent(focus("Calculator"), None, None)
    assert intent.kind == "window_state"
    assert intent.expected_window_title == "Calculator"
    assert intent.window_title_match == "contains"
    engine = VerificationEngine()
    before = _window_observation("Calculator")
    after = _window_observation("Calculator")
    assert engine.verify(intent, before, after).outcome == "verified"


def test_verification_focus_other_window_title_case_insensitive_match() -> None:
    agent = _gate_agent(FakeComputerBackend(), None)
    intent = agent._build_intent(focus("calc"), None, None)
    result = VerificationEngine().verify(
        intent, _window_observation("Untitled - Notepad"), _window_observation("Calculator")
    )
    assert result.outcome == "verified"
    failed = VerificationEngine().verify(
        intent, _window_observation("Untitled - Notepad"), _window_observation("Untitled - Notepad")
    )
    assert failed.outcome == "failed"


# --- provider: strict parsing accepts focus_window, rejects a missing target --------------------------


def test_parse_decision_accepts_focus_window() -> None:
    payload = json.dumps(
        {
            "status": "action",
            "action": {"action": "focus_window", "target": "Calculator", "confidence": 0.9},
        }
    )
    decision = parse_decision(payload)
    assert decision.status == "action"
    assert decision.action is not None
    assert decision.action.action is ActionType.FOCUS_WINDOW
    assert decision.action.target == "Calculator"
    assert decision.action.text is None  # target is a dedicated field, NOT the text channel


def test_parse_decision_focus_window_without_target_fails_closed() -> None:
    payload = json.dumps({"status": "action", "action": {"action": "focus_window"}})
    with pytest.raises(ProviderParseError):
        parse_decision(payload)


def test_response_schema_documents_all_three_new_actions() -> None:
    assert '"move"' in RESPONSE_SCHEMA_TEXT
    assert '"hotkey"' in RESPONSE_SCHEMA_TEXT
    assert '"focus_window"' in RESPONSE_SCHEMA_TEXT
    assert '"target"' in RESPONSE_SCHEMA_TEXT
    assert '["ctrl", "s"]' in RESPONSE_SCHEMA_TEXT  # hotkey example
    assert '"point" is required' in RESPONSE_SCHEMA_TEXT  # move rule
    assert "ENVIRONMENT CONTENT" in RESPONSE_SCHEMA_TEXT  # target source doctrine


# --- server: computer_execute focus_window integration ------------------------------------------------


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


async def test_computer_execute_focus_window_swaps_active_window_and_verifies(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeComputerBackend(active_window=NOTEPAD, windows=[NOTEPAD, CALC])
    session_id = _make_session(
        monkeypatch, backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = await server.computer_execute(session_id, "focus_window", target="Calculator")
    assert result["ok"] is True, result
    assert result["message"] == "Simulated focus_window."
    assert result["verification"]["outcome"] == "verified"  # deterministic window_state
    assert len(backend.executed) == 1
    executed = backend.executed[0]
    assert executed.action is ActionType.FOCUS_WINDOW
    assert executed.target == "Calculator"
    assert backend.focused == ["Calculator"]
    assert backend.active_window == CALC
    observation = backend.observe()
    assert observation.active_window == "Calculator"


async def test_computer_execute_focus_window_requires_approval_then_approved(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeComputerBackend(active_window=NOTEPAD, windows=[NOTEPAD, CALC])
    session_id = _make_session(
        monkeypatch, backend, dry_run=False, require_approval=True, limits=FAST_LIMITS
    )
    result = await server.computer_execute(session_id, "focus_window", target="Calculator")
    assert result["ok"] is False
    assert result["requires_approval"] is True
    assert "Calculator" in result["message"]  # the approval message names the target
    assert backend.executed == []
    approved = await server.computer_execute(
        session_id, "focus_window", target="Calculator", approved=True
    )
    assert approved["ok"] is True, approved
    assert len(backend.executed) == 1
    assert backend.focused == ["Calculator"]


async def test_computer_execute_focus_window_without_target_fails_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeComputerBackend(active_window=NOTEPAD, windows=[NOTEPAD, CALC])
    session_id = _make_session(
        monkeypatch, backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = await server.computer_execute(session_id, "focus_window")
    assert result["ok"] is False
    assert result["error"] == "invalid_action"
    assert backend.executed == []
