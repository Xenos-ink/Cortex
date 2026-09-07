"""Parity matrix: pyautogui fallback path vs SendInput path, per action type.

Mission acceptance (PERF-004 change 2): for every action type the OLD (pyautogui) and
NEW (SendInput) input paths must match on

- dispatch succeeds (same result message);
- errors match (failsafe corner -> the SAME ``InputBlockedError`` semantics);
- coordinates equivalent (the ONE screenshot->physical transform feeds both engines;
  the SendInput 0-65535 virtual-desktop normalization must round-trip to the same
  physical pixel the pyautogui engine would target).

Everything here runs on STUBS (a recording pyautogui stand-in + a fake user32), so no
test in this file dispatches real input on the desktop. Stroke POLICY differences
(interpolated vs minimal drag segments) are recorded as documented, deliberate
divergences with equal endpoints.
"""

from __future__ import annotations

from typing import Any

import pytest

import computer_use_mcp.backend as backend_module
from computer_use_mcp.backend import (
    CoordinateVerdict,
    InputBlockedError,
    LocalComputerBackend,
    PyAutoGuiInputEngine,
    SendInputEngine,
    _CaptureContext,
    _from_absolute_65535,
)
from computer_use_mcp.models import CoordinateSpace, GroundedAction, MonitorInfo

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")

METRICS = (0, 0, 1920, 1080)


class _ParityPyautogui:
    """pyautogui stand-in: records calls, can raise FailSafeException on cue."""

    FailSafeException = type("FailSafeException", (Exception,), {})

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.fail = fail
        self.PAUSE = 0.1
        self.FAILSAFE = True

    def moveTo(self, x: int, y: int) -> None:
        self._guard()
        self.calls.append(("moveTo", x, y))

    def mouseDown(self, button: str = "left") -> None:
        self._guard()
        self.calls.append(("mouseDown", button))

    def mouseUp(self, button: str = "left") -> None:
        self._guard()
        self.calls.append(("mouseUp", button))

    def click(self, *args: object, **kwargs: object) -> None:
        self._guard()
        self.calls.append(("click", *args))

    def write(self, *args: object, **kwargs: object) -> None:
        self._guard()
        self.calls.append(("write", *args))

    def hotkey(self, *args: object, **kwargs: object) -> None:
        self._guard()
        self.calls.append(("hotkey", *args))

    def scroll(self, *args: object, **kwargs: object) -> None:
        self._guard()
        self.calls.append(("scroll", *args))

    def _guard(self) -> None:
        if self.fail:
            raise self.FailSafeException("failsafe")


class _ParityFakeUser32:
    """user32 stand-in for the SendInput half of the matrix."""

    def __init__(self, *, cursor: tuple[int, int] = (960, 540), send_results: list[int] | None = None) -> None:
        self.cursor = cursor
        self.send_results = send_results
        self.batches: list[list[dict[str, int]]] = []

    def GetCursorPos(self, point_ref: Any) -> int:
        point_ref._obj.x, point_ref._obj.y = self.cursor
        return 1

    def GetSystemMetrics(self, index: int) -> int:
        return {0: 1920, 1: 1080, 76: METRICS[0], 77: METRICS[1], 78: METRICS[2], 79: METRICS[3]}[index]

    def SendInput(self, count: int, events: Any, _size: int) -> int:
        def decode(event: Any) -> dict[str, int]:
            if event.type == 0:
                return {
                    "kind": 0,
                    "flags": int(event.union.mi.dwFlags),
                    "dx": int(event.union.mi.dx),
                    "dy": int(event.union.mi.dy),
                    "data": int(event.union.mi.mouseData),
                }
            return {
                "kind": 1,
                "flags": int(event.union.ki.dwFlags),
                "vk": int(event.union.ki.wVk),
                "scan": int(event.union.ki.wScan),
                "data": 0,
            }

        self.batches.append([decode(events[index]) for index in range(count)])
        if self.send_results:
            return self.send_results.pop(0)
        return count


def _run_both(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch, action: GroundedAction
) -> tuple[list[tuple[object, ...]], _ParityFakeUser32, str, str]:
    """Run one action through the pyautogui engine and the SendInput engine (stubs)."""
    pa_module = _ParityPyautogui()
    monkeypatch.setattr(
        real_backend,
        "_engine",
        PyAutoGuiInputEngine(
            pa_module,
            pause=0.0,
            click_interval=0.0,
            type_interval=0.0,
            drag_interpolate=True,
            drag_step_pause=0.0,
        ),
    )
    monkeypatch.setattr(real_backend, "_active_context", None)  # passthrough transform
    pyautogui_message = real_backend.execute(action)

    fake_user32 = _ParityFakeUser32()
    monkeypatch.setattr(backend_module, "_user32", fake_user32)
    monkeypatch.setattr(real_backend, "_engine", SendInputEngine())
    sendinput_message = real_backend.execute(action)
    return pa_module.calls, fake_user32, pyautogui_message, sendinput_message


def _pyautogui_move_points(pa_calls: list[tuple[object, ...]]) -> list[tuple[int, int]]:
    return [(int(call[1]), int(call[2])) for call in pa_calls if call[0] == "moveTo"]


def _sendinput_move_points(fake_user32: _ParityFakeUser32) -> list[tuple[int, int]]:
    """Decode every absolute-move event back into the physical pixel it targets."""
    points: list[tuple[int, int]] = []
    for batch in fake_user32.batches:
        for event in batch:
            if event["kind"] == 0 and event["flags"] & backend_module._MOUSEEVENTF_MOVE:
                points.append(
                    (
                        _from_absolute_65535(event["dx"], 0, 1920),
                        _from_absolute_65535(event["dy"], 0, 1080),
                    )
                )
    return points


@WINDOWS_ONLY
def test_parity_move(real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    action = GroundedAction(confidence=1.0, action="move", point={"x": 30, "y": 45})
    pa_calls, fake_user32, pa_msg, si_msg = _run_both(real_backend, monkeypatch, action)
    assert pa_msg == si_msg == "Executed move."
    assert _pyautogui_move_points(pa_calls) == _sendinput_move_points(fake_user32) == [(30, 45)]


@WINDOWS_ONLY
def test_parity_click(real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    action = GroundedAction(confidence=1.0, action="click", point={"x": 30, "y": 45})
    pa_calls, fake_user32, pa_msg, si_msg = _run_both(real_backend, monkeypatch, action)
    assert pa_msg == si_msg == "Executed click."
    assert pa_calls == [("click", 30, 45)]
    assert _sendinput_move_points(fake_user32) == [(30, 45)]
    flags = [event["flags"] for event in fake_user32.batches[0]]
    assert flags[1:] == [backend_module._MOUSEEVENTF_LEFTDOWN, backend_module._MOUSEEVENTF_LEFTUP]


@WINDOWS_ONLY
def test_parity_double_click(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = GroundedAction(confidence=1.0, action="double_click", point={"x": 30, "y": 45})
    pa_calls, fake_user32, pa_msg, si_msg = _run_both(real_backend, monkeypatch, action)
    assert pa_msg == si_msg == "Executed double_click."
    assert pa_calls == [("click", 30, 45)]
    downs = [e for e in fake_user32.batches[0] if e["flags"] == backend_module._MOUSEEVENTF_LEFTDOWN]
    assert len(downs) == 2
    assert _sendinput_move_points(fake_user32) == [(30, 45)]


@WINDOWS_ONLY
def test_parity_type(real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    action = GroundedAction(confidence=1.0, action="type", text="hi")
    pa_calls, fake_user32, pa_msg, si_msg = _run_both(real_backend, monkeypatch, action)
    assert pa_msg == si_msg == "Executed type."
    assert pa_calls == [("write", "h"), ("write", "i")]
    keys = [
        event["scan"]
        for batch in fake_user32.batches
        for event in batch
        if event["kind"] == 1 and event["flags"] == backend_module._KEYEVENTF_UNICODE
    ]
    assert keys == [ord("h"), ord("i")]


@WINDOWS_ONLY
def test_parity_keypress(real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    action = GroundedAction(confidence=1.0, action="keypress", keys=["ctrl", "s"])
    pa_calls, fake_user32, pa_msg, si_msg = _run_both(real_backend, monkeypatch, action)
    assert pa_msg == si_msg == "Executed keypress."
    assert pa_calls == [("hotkey", "ctrl", "s")]
    downs = [e["vk"] for e in fake_user32.batches[0] if e["kind"] == 1 and e["flags"] == 0]
    assert downs == [0x11, 0x53]  # same chord (ctrl, s), one batch


@WINDOWS_ONLY
def test_parity_hotkey(real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    action = GroundedAction(confidence=1.0, action="hotkey", keys=["ctrl", "s"])
    pa_calls, fake_user32, pa_msg, si_msg = _run_both(real_backend, monkeypatch, action)
    assert pa_msg == si_msg == "Executed hotkey."
    assert pa_calls == [("hotkey", "ctrl", "s")]
    downs = [e["vk"] for e in fake_user32.batches[0] if e["kind"] == 1 and e["flags"] == 0]
    assert downs == [0x11, 0x53]


@WINDOWS_ONLY
def test_parity_scroll(real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    action = GroundedAction(confidence=1.0, action="scroll", delta=5)
    pa_calls, fake_user32, pa_msg, si_msg = _run_both(real_backend, monkeypatch, action)
    assert pa_msg == si_msg == "Executed scroll."
    assert pa_calls == [("scroll", 5)]
    wheel = [e for e in fake_user32.batches[0] if e["flags"] == backend_module._MOUSEEVENTF_WHEEL]
    assert wheel[0]["data"] == 5 * 120


@WINDOWS_ONLY
def test_parity_drag_endpoints_equal_stroke_policy_documented(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Endpoints equal on both paths; stroke policy diverges BY DESIGN.

    pyautogui fallback: interpolated ~40 px segments (legacy behavior).
    SendInput default: minimal stroke (move-press-move-release). The interpolated
    option is preserved behind ``drag_interpolate`` (verified in test_drag_action).
    """
    action = GroundedAction(
        confidence=1.0, action="drag", point={"x": 10, "y": 10}, to_point={"x": 60, "y": 50}
    )
    pa_calls, fake_user32, pa_msg, si_msg = _run_both(real_backend, monkeypatch, action)
    assert pa_msg == si_msg == "Executed drag."
    assert pa_calls[0] == ("moveTo", 10, 10)  # stroke start
    assert pa_calls[1] == ("mouseDown", "left")  # press AFTER move(start) on both paths
    assert pa_calls[-1] == ("mouseUp", "left")
    points = _sendinput_move_points(fake_user32)
    assert points[0] == (10, 10)  # stroke start
    assert points[-1] == (60, 50)  # stroke end


@WINDOWS_ONLY
def test_parity_failsafe_corner_same_error_semantics(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failsafe corner -> InputBlockedError on BOTH paths; zero dispatch on SendInput."""
    action = GroundedAction(confidence=1.0, action="click", point={"x": 30, "y": 45})
    monkeypatch.setattr(real_backend, "_active_context", None)

    pa_module = _ParityPyautogui(fail=True)  # emulates pyautogui's FailSafeException
    monkeypatch.setattr(real_backend, "_engine", PyAutoGuiInputEngine(pa_module))
    with pytest.raises(InputBlockedError):
        real_backend.execute(action)

    fake_user32 = _ParityFakeUser32(cursor=(0, 0))  # the REAL replicated corner check
    monkeypatch.setattr(backend_module, "_user32", fake_user32)
    monkeypatch.setattr(real_backend, "_engine", SendInputEngine())
    with pytest.raises(InputBlockedError, match="failsafe"):
        real_backend.execute(action)
    assert fake_user32.batches == []  # blocked BEFORE any physical input


@WINDOWS_ONLY
def test_parity_sendinput_blocked_input_fails_closed(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SendInput ret==0 (UIPI/blocked) -> InputBlockedError. pyautogui has NO equivalent
    (it silently swallows PermissionError/OSError) — the new path is strictly stronger."""
    action = GroundedAction(confidence=1.0, action="click", point={"x": 30, "y": 45})
    monkeypatch.setattr(real_backend, "_active_context", None)
    monkeypatch.setattr(backend_module, "_user32", _ParityFakeUser32(send_results=[0]))
    monkeypatch.setattr(real_backend, "_engine", SendInputEngine())
    with pytest.raises(InputBlockedError, match="blocked"):
        real_backend.execute(action)


@WINDOWS_ONLY
def test_parity_scaled_space_coordinates_through_the_one_transform(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The screenshot->physical transform feeds BOTH engines identically (F1 invariant)."""
    monitor = MonitorInfo(id="m", index=0, bounds=(0, 0, 1920, 1080), dpi_scale_x=1.25, dpi_scale_y=1.25)
    verdict = CoordinateVerdict(
        space=CoordinateSpace.SCALED, scale_x=1.25, scale_y=1.25, input_width=1920, input_height=1080
    )
    monkeypatch.setattr(
        real_backend, "_active_context", _CaptureContext(monitor=monitor, verdict=verdict)
    )
    action = GroundedAction(confidence=1.0, action="move", point={"x": 100, "y": 64})

    pa_module = _ParityPyautogui()
    monkeypatch.setattr(real_backend, "_engine", PyAutoGuiInputEngine(pa_module))
    real_backend.execute(action)
    fake_user32 = _ParityFakeUser32()
    monkeypatch.setattr(backend_module, "_user32", fake_user32)
    monkeypatch.setattr(real_backend, "_engine", SendInputEngine())
    real_backend.execute(action)

    assert _pyautogui_move_points(pa_module.calls) == [(125, 80)]
    assert _sendinput_move_points(fake_user32) == [(125, 80)]
