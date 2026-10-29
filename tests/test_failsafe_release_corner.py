"""DELTA 1 (VAL-WP2 F1 BLOCKER): releases must never be refused by the failsafe corner.

The failsafe corner is the USER'S OWN ABORT GESTURE (fling the mouse into a screen
corner). A release is the halt-safe action: refusing ``mouse_up``/``key_up``/
``release_modifiers`` there would leave a button or keys PHYSICALLY DOWN — stuck
input rewrites every later keystroke system-wide, strictly worse than what the user
is aborting. So:

- ``SendInputEngine``: every release primitive routes through ``_send_release``, which
  SKIPS ONLY the corner check — the non-Windows guard and the zero-return fail-closed
  check (``InputBlockedError`` when SendInput is blocked) BOTH stay;
- ``PyAutoGuiInputEngine``: every release primitive suspends ``FAILSAFE`` for the one
  release call and restores the exact previous value in a ``finally``;
- presses, downs, moves, clicks, type, chord and scroll KEEP the corner check.

Every test here uses STUBBED engines only (recorded ``_dispatch`` / a stub pyautogui
module) with the cursor stubbed into a corner — no real input, per the WP2 HARD RULE.
"""

from __future__ import annotations

from typing import Any

import pytest
from recording_engine import RecordingEngine

import computer_use_mcp.backend as backend_module
from computer_use_mcp import server
from computer_use_mcp.backend import (
    _KEYEVENTF_KEYUP,
    _MOUSEEVENTF_LEFTUP,
    _MOUSEEVENTF_RIGHTUP,
    _MOUSEEVENTF_WHEEL,
    InputBlockedError,
    InputEngine,
    LocalComputerBackend,
    PyAutoGuiInputEngine,
    SendInputEngine,
    UnsupportedActionError,
)
from computer_use_mcp.models import GroundedAction
from computer_use_mcp.state import StopToken

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")

CORNER = (0, 0)  # the top-left failsafe corner


def corner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the cursor into the top-left failsafe corner (primary screen 100x100)."""
    monkeypatch.setattr(backend_module, "get_cursor_position", lambda: CORNER)
    monkeypatch.setattr(backend_module, "_primary_screen_size", lambda: (100, 100))


def recording_sendinput(monkeypatch: pytest.MonkeyPatch) -> tuple[SendInputEngine, list[Any]]:
    """A REAL SendInputEngine whose SendInput dispatch is recorded (no real input)."""
    engine = SendInputEngine()
    sent: list[Any] = []
    monkeypatch.setattr(engine, "_dispatch", lambda events: (sent.extend(events), len(events))[1])
    return engine, sent


# --- SendInputEngine: presses stay guarded, releases dispatch in the corner ----------------


@WINDOWS_ONLY
def test_press_stays_blocked_in_the_corner(monkeypatch: pytest.MonkeyPatch) -> None:
    """A press keeps the corner check: key_down/move in a corner raise, zero events."""
    corner(monkeypatch)
    engine, sent = recording_sendinput(monkeypatch)
    with pytest.raises(InputBlockedError, match="failsafe"):
        engine.key_down("a")
    with pytest.raises(InputBlockedError, match="failsafe"):
        engine.move(50, 50)
    assert sent == []


@WINDOWS_ONLY
def test_mouse_up_releases_in_the_corner(monkeypatch: pytest.MonkeyPatch) -> None:
    corner(monkeypatch)
    engine, sent = recording_sendinput(monkeypatch)
    engine.mouse_up("left")
    assert [event.union.mi.dwFlags for event in sent] == [_MOUSEEVENTF_LEFTUP]


@WINDOWS_ONLY
def test_right_mouse_up_releases_in_the_corner(monkeypatch: pytest.MonkeyPatch) -> None:
    corner(monkeypatch)
    engine, sent = recording_sendinput(monkeypatch)
    engine.mouse_up("right")
    assert [event.union.mi.dwFlags for event in sent] == [_MOUSEEVENTF_RIGHTUP]


@WINDOWS_ONLY
def test_key_up_releases_in_the_corner(monkeypatch: pytest.MonkeyPatch) -> None:
    corner(monkeypatch)
    engine, sent = recording_sendinput(monkeypatch)
    engine.key_up("a")
    assert sent and all(event.union.ki.dwFlags == _KEYEVENTF_KEYUP for event in sent)


@WINDOWS_ONLY
def test_release_modifiers_releases_in_the_corner(monkeypatch: pytest.MonkeyPatch) -> None:
    corner(monkeypatch)
    engine, sent = recording_sendinput(monkeypatch)
    engine.release_modifiers(["ctrl", "shift"])
    assert len(sent) == 2
    assert all(event.union.ki.dwFlags == _KEYEVENTF_KEYUP for event in sent)


@WINDOWS_ONLY
def test_blocked_release_still_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A release skips ONLY the corner check: a blocked SendInput (zero return) still
    raises ``InputBlockedError`` — the zero-return fail-closed contract is intact."""
    corner(monkeypatch)
    engine, _sent = recording_sendinput(monkeypatch)
    monkeypatch.setattr(engine, "_dispatch", lambda events: 0)
    with pytest.raises(InputBlockedError, match="blocked"):
        engine.mouse_up("left")
    with pytest.raises(InputBlockedError, match="blocked"):
        engine.key_up("a")


# --- PyAutoGuiInputEngine: FAILSAFE suspended for the release call, restored exactly -------


class _CornerPyautogui:
    """A stub pyautogui whose key/mouse primitives check FAILSAFE like the real module.

    ``keyDown``/``mouseDown``/``moveTo`` raise ``FailSafeException`` whenever FAILSAFE
    is truthy (the corner is active in these tests); ``keyUp``/``mouseUp`` do the SAME
    when FAILSAFE is truthy — which is exactly what pyautogui's own primitives do, and
    precisely the defect class DELTA 1 fixes: the release must succeed because the
    engine suspends FAILSAFE around the call, not because the stub got softer.
    """

    FailSafeException = type("FailSafeException", (Exception,), {})

    def __init__(self) -> None:
        self.PAUSE = 0.0
        self.FAILSAFE = True
        self.pressed: list[str] = []
        self.released: list[str] = []
        self.mouse_released: list[str] = []
        self.moves: list[tuple[int, int]] = []

    def _corner_guard(self) -> None:
        if self.FAILSAFE:
            raise self.FailSafeException("simulated corner failsafe")

    def keyDown(self, key: str) -> None:
        self._corner_guard()
        self.pressed.append(key)

    def keyUp(self, key: str) -> None:
        self._corner_guard()
        self.released.append(key)

    def mouseDown(self, button: str = "left") -> None:
        self._corner_guard()

    def mouseUp(self, button: str = "left") -> None:
        self._corner_guard()
        self.mouse_released.append(button)

    def moveTo(self, x: int, y: int) -> None:
        self._corner_guard()
        self.moves.append((x, y))


def corner_pyautogui_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PyAutoGuiInputEngine, _CornerPyautogui]:
    corner(monkeypatch)  # the corner is active for the whole test
    stub = _CornerPyautogui()
    return PyAutoGuiInputEngine(stub), stub


@WINDOWS_ONLY
def test_pyautogui_key_up_releases_in_the_corner(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, stub = corner_pyautogui_engine(monkeypatch)
    engine.key_up("a")
    assert stub.released == ["a"]
    assert stub.FAILSAFE is True  # restored to the exact previous value


@WINDOWS_ONLY
def test_pyautogui_mouse_up_releases_in_the_corner(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, stub = corner_pyautogui_engine(monkeypatch)
    engine.mouse_up("left")
    assert stub.mouse_released == ["left"]
    assert stub.FAILSAFE is True


@WINDOWS_ONLY
def test_pyautogui_release_modifiers_releases_in_the_corner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, stub = corner_pyautogui_engine(monkeypatch)
    engine.release_modifiers(["ctrl", "shift"])
    assert stub.released == ["ctrl", "shift"]
    assert stub.FAILSAFE is True


@WINDOWS_ONLY
def test_pyautogui_press_still_blocked_in_the_corner(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, stub = corner_pyautogui_engine(monkeypatch)
    with pytest.raises(InputBlockedError, match="failsafe"):
        engine.key_down("a")
    with pytest.raises(InputBlockedError, match="failsafe"):
        engine.mouse_down("left")
    with pytest.raises(InputBlockedError, match="failsafe"):
        engine.move(50, 50)
    assert stub.pressed == [] and stub.FAILSAFE is True


@WINDOWS_ONLY
def test_pyautogui_failsafe_restored_even_when_the_release_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact previous value is restored even when the release raises."""
    engine, stub = corner_pyautogui_engine(monkeypatch)
    stub.FAILSAFE = False  # a caller may run with failsafe off; restore THAT value

    def _explode(key: str) -> None:
        raise RuntimeError("synthetic release failure")

    monkeypatch.setattr(stub, "keyUp", _explode)
    with pytest.raises(RuntimeError, match="synthetic release failure"):
        engine.key_up("a")
    assert stub.FAILSAFE is False  # restored to False, not to the default True


# --- end-to-end through the backend: the hold release uses the corner-safe path -------------


class _ReleaseThroughSendInput(RecordingEngine):
    """RecordingEngine whose RELEASE primitives route through a REAL SendInputEngine.

    Presses/segments are ordinary recordings; every release goes through the real
    SendInput release path (with the recorded dispatch) so the end-to-end hold tests
    prove the BACKEND's release reaches the corner-safe primitives while the cursor
    sits in the corner for the whole hold.
    """

    def __init__(self, release_engine: SendInputEngine) -> None:
        super().__init__()
        self._release_engine = release_engine

    def mouse_up(self, button: str = "left") -> None:
        self.calls.append(("mouse_up", button))
        self._release_engine.mouse_up(button)

    def key_up(self, key: str) -> None:
        self.calls.append(("key_up", key))
        self._release_engine.key_up(key)


def _corner_backend(real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch):
    """real_backend + the hybrid engine, corner active, dispatch recorded."""
    corner(monkeypatch)
    release_engine, sent = recording_sendinput(monkeypatch)
    engine = _ReleaseThroughSendInput(release_engine)
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    monkeypatch.setattr(real_backend, "_last_focus_transition", 0.0)
    monkeypatch.setattr(real_backend, "_last_key_dispatch", 0.0)
    return engine, sent


def _click(point: tuple[int, int], **kwargs: Any) -> GroundedAction:
    kwargs.setdefault("confidence", 1.0)
    return GroundedAction(action="click", point={"x": point[0], "y": point[1]}, **kwargs)


def _right_click(point: tuple[int, int], **kwargs: Any) -> GroundedAction:
    kwargs.setdefault("confidence", 1.0)
    return GroundedAction(action="right_click", point={"x": point[0], "y": point[1]}, **kwargs)


def keypress(keys: list[str], **kwargs: Any) -> GroundedAction:
    return GroundedAction(action="keypress", keys=keys, confidence=1.0, **kwargs)


def hotkey(keys: list[str], **kwargs: Any) -> GroundedAction:
    return GroundedAction(action="hotkey", keys=keys, confidence=1.0, **kwargs)


def _released_vk_flags(sent: list[Any]) -> list[int]:
    return [event.union.ki.wVk for event in sent if event.union.ki.dwFlags == _KEYEVENTF_KEYUP]


@WINDOWS_ONLY
def test_held_click_releases_in_the_corner(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, sent = _corner_backend(real_backend, monkeypatch)
    monkeypatch.setattr(backend_module.time, "sleep", lambda _s: None)
    message = real_backend.execute(_click((10, 10), hold_ms=100))
    assert message == "Executed click."
    assert ("mouse_up", "left") in engine.calls  # the backend attempted the release
    assert any(event.union.mi.dwFlags == _MOUSEEVENTF_LEFTUP for event in sent)  # and it dispatched
    # and the press itself was NOT blocked here (the hold pressed while corner-active
    # would have been refused — this test holds with the corner active from the start,
    # so the press on the recording half succeeds and only the RELEASE matters).


@WINDOWS_ONLY
def test_held_right_click_releases_in_the_corner(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, sent = _corner_backend(real_backend, monkeypatch)
    monkeypatch.setattr(backend_module.time, "sleep", lambda _s: None)
    message = real_backend.execute(_right_click((10, 10), hold_ms=100))
    assert message == "Executed right_click."
    assert ("mouse_up", "right") in engine.calls
    assert any(event.union.mi.dwFlags == _MOUSEEVENTF_RIGHTUP for event in sent)


@WINDOWS_ONLY
def test_held_keypress_releases_in_the_corner(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, sent = _corner_backend(real_backend, monkeypatch)
    monkeypatch.setattr(backend_module.time, "sleep", lambda _s: None)
    message = real_backend.execute(keypress(["right"], hold_ms=100))
    assert message == "Executed keypress."
    assert ("key_up", "right") in engine.calls
    vk = backend_module._resolve_key_events("right")[0]
    assert _released_vk_flags(sent) == [vk]


@WINDOWS_ONLY
def test_held_hotkey_releases_every_key_in_reverse_in_the_corner(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, sent = _corner_backend(real_backend, monkeypatch)
    monkeypatch.setattr(backend_module.time, "sleep", lambda _s: None)
    message = real_backend.execute(hotkey(["ctrl", "c"], hold_ms=100))
    assert message == "Executed hotkey."
    assert [name for kind, name in engine.calls if kind == "key_up"] == ["c", "ctrl"]
    ctrl_vk = backend_module._resolve_key_events("ctrl")[0]
    c_vk = backend_module._resolve_key_events("c")[0]
    assert _released_vk_flags(sent) == [c_vk, ctrl_vk]  # reverse order


@WINDOWS_ONLY
def test_drag_mouse_up_releases_in_the_corner(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, sent = _corner_backend(real_backend, monkeypatch)
    action = GroundedAction(
        action="drag",
        point={"x": 10, "y": 10},
        to_point={"x": 20, "y": 20},
        confidence=1.0,
    )
    message = real_backend.execute(action)
    assert message == "Executed drag."
    assert ("mouse_up", "left") in engine.calls
    assert any(event.union.mi.dwFlags == _MOUSEEVENTF_LEFTUP for event in sent)


@WINDOWS_ONLY
def test_release_modifiers_through_the_guard_releases_in_the_corner(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The T8 HotkeyGuard release path (backend ``release_modifiers``) also reaches the
    corner-safe engine primitive."""
    corner(monkeypatch)
    release_engine, sent = recording_sendinput(monkeypatch)
    monkeypatch.setattr(real_backend, "_engine", release_engine)
    real_backend.release_modifiers(["ctrl"])  # the backend's release path
    ctrl_vk = backend_module._resolve_key_events("ctrl")[0]
    assert _released_vk_flags(sent) == [ctrl_vk]


# --- F3: the base engine fails a held keypress honestly -------------------------------------


class _NoKeyPrimitivesEngine(InputEngine):
    """A third-party engine implementing ONLY the pre-existing abstract primitives."""

    click_interval: float = 0.0
    type_interval: float = 0.0
    drag_interpolate: bool = False
    drag_step_pause: float = 0.0

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def move(self, x: int, y: int) -> None:
        self.calls.append(("move", x, y))

    def click(self, x: int, y: int, clicks: int = 1, button: str = "left") -> None:
        self.calls.append(("click", x, y, clicks, button))

    def mouse_down(self, button: str = "left") -> None:
        self.calls.append(("mouse_down", button))

    def mouse_up(self, button: str = "left") -> None:
        self.calls.append(("mouse_up", button))

    def type_text(self, text: str, before_chunk: object = None) -> None:
        self.calls.append(("type_text", text))

    def chord(self, keys: list[str]) -> None:
        self.calls.append(("chord", *keys))

    def scroll(self, delta: int) -> None:
        self.calls.append(("scroll", delta))


@WINDOWS_ONLY
def test_engine_without_the_key_pair_fails_a_held_keypress_honestly(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 (F3): an engine without key_down/key_up must FAIL a held keypress with
    the typed error — never report "Executed keypress." for input that never happened.
    (A held CHORD-free action is unaffected; the legacy chord path is untouched.)"""
    corner(monkeypatch)  # irrelevant here; kept off the release path checks
    stub = _NoKeyPrimitivesEngine()
    monkeypatch.setattr(real_backend, "_engine", stub)
    monkeypatch.setattr(real_backend, "_last_focus_transition", 0.0)
    monkeypatch.setattr(real_backend, "_last_key_dispatch", 0.0)
    with pytest.raises(UnsupportedActionError, match="key_down/key_up"):
        real_backend.execute(keypress(["right"], hold_ms=120))
    assert stub.calls == []  # no chord, no input, no lie


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Per-module session isolation (the shared server-level fixture pattern)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    from computer_use_mcp.state import SessionRegistry

    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    yield server


async def test_engine_without_the_pair_fails_at_the_agent_pipeline(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pipeline parity for F3: a held keypress on a pair-less engine resolves to a
    §1 ``failed`` reply (ok False) — never a silent success."""
    from test_controller_integration import FAST_LIMITS, ScriptedBackend, execute_payload, make_session
    from computer_use_mcp import server

    class PairlessScripted(ScriptedBackend):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.engine = _NoKeyPrimitivesEngine()

        def execute(self, action: Any, stop: Any = None):  # match ScriptedBackend exactly
            # simulate a real engine lacking the pair: held keys raise typed
            if getattr(action, "hold_ms", None) is not None and action.action in {"keypress", "hotkey"}:
                raise UnsupportedActionError(
                    "this input engine does not implement key_down/key_up; "
                    "held-key input cannot be dispatched on it"
                )
            return super().execute(action, stop)

    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=PairlessScripted(flip=False), dry_run=False,
        require_approval=False, limits=FAST_LIMITS,
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id, "keypress", keys=["right"], approved=True,
            include_screenshot_after=False, follow_ups=[
                {"action": "keypress", "keys": ["a"], "hold_ms": 100}
            ],
        )
    )
    entries = payload.get("follow_up_results") or []
    assert entries, payload
    entry = entries[-1]
    assert entry["result"] == "failed" and entry["ok"] is False
    assert entry["message"] and "key_down/key_up" in entry["message"]
