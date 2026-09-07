"""SendInput + pyautogui-fallback input engine tests (mocked Win32, no real input).

The SendInput engine reads the module-level ``_user32`` binding at call time, so every
test here monkeypatches ``backend_module._user32`` with a fake that records decoded
events — the same convention as the window-identity tests. Nothing in this file
dispatches real input on the desktop.

Covered:
- absolute-move normalization: MOUSEEVENTF_ABSOLUTE|VIRTUALDESK over the virtual
  desktop, exact round-trip via ``_from_absolute_65535``, multi-monitor offsets;
- batching: one click = move+down+up in ONE SendInput call; chords = one call with
  modifiers released in reverse; typing = whole-string KEYEVENTF_UNICODE (layout-proof,
  Arabic included), '\\n'->VK_RETURN, astral chars -> surrogate pairs, chunked at
  1000 events;
- fail-closed semantics: SendInput ret==0 -> InputBlockedError; failsafe screen corner
  (replicated pyautogui check) -> InputBlockedError before any dispatch; failsafe can
  be disabled;
- pacing options: click_interval between repeated clicks, per-char type_interval;
- pyautogui fallback engine: same call vocabulary + FailSafeException mapping.
"""

from __future__ import annotations

from typing import Any

import pytest
from recording_engine import RecordingEngine

import computer_use_mcp.backend as backend_module
from computer_use_mcp.backend import (
    InputBlockedError,
    PyAutoGuiInputEngine,
    SendInputEngine,
    _from_absolute_65535,
    _key_event,
    _mouse_flag_event,
    _mouse_move_event,
    _text_to_key_units,
    _to_absolute_65535,
)

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")

METRICS = (0, 0, 1920, 1080)  # virtual screen (x, y, w, h)


def _decode_event(event: Any) -> dict[str, int]:
    """Lightweight decode of one _INPUT for assertions."""
    if event.type == 0:  # INPUT_MOUSE
        return {
            "kind": "mouse",
            "flags": int(event.union.mi.dwFlags),
            "dx": int(event.union.mi.dx),
            "dy": int(event.union.mi.dy),
            "mouseData": int(event.union.mi.mouseData),
        }
    return {
        "kind": "keyboard",
        "vk": int(event.union.ki.wVk),
        "scan": int(event.union.ki.wScan),
        "flags": int(event.union.ki.dwFlags),
    }


class _FakeUser32:
    """user32 stand-in: records SendInput batches, injectable cursor + return values."""

    def __init__(
        self,
        *,
        cursor: tuple[int, int] = (960, 540),
        metrics: tuple[int, int, int, int] = METRICS,
        primary: tuple[int, int] = (1920, 1080),
        send_input_results: list[int] | None = None,
    ) -> None:
        self.cursor = cursor
        self.metrics = metrics
        self.primary = primary
        self.send_input_results = send_input_results
        self.batches: list[list[dict[str, int]]] = []

    def GetCursorPos(self, point_ref: Any) -> int:
        point_ref._obj.x, point_ref._obj.y = self.cursor
        return 1

    def GetSystemMetrics(self, index: int) -> int:
        mapping = {0: self.primary[0], 1: self.primary[1]}
        mapping.update(
            {
                backend_module._SM_XVIRTUALSCREEN: self.metrics[0],
                backend_module._SM_YVIRTUALSCREEN: self.metrics[1],
                backend_module._SM_CXVIRTUALSCREEN: self.metrics[2],
                backend_module._SM_CYVIRTUALSCREEN: self.metrics[3],
            }
        )
        return mapping[index]

    def SendInput(self, count: int, events: Any, _size: int) -> int:
        batch = [_decode_event(events[index]) for index in range(count)]
        self.batches.append(batch)
        if self.send_input_results:
            return self.send_input_results.pop(0)
        return count


@pytest.fixture
def fake_user32(monkeypatch: pytest.MonkeyPatch) -> _FakeUser32:
    fake = _FakeUser32()
    monkeypatch.setattr(backend_module, "_user32", fake)
    return fake


def _engine(**kwargs: Any) -> SendInputEngine:
    return SendInputEngine(**kwargs)


# --- move normalization ------------------------------------------------------------------------


@WINDOWS_ONLY
def test_move_dispatches_one_absolute_virtual_desk_event(fake_user32: _FakeUser32) -> None:
    _engine().move(960, 540)
    assert len(fake_user32.batches) == 1
    batch = fake_user32.batches[0]
    assert len(batch) == 1
    event = batch[0]
    assert event["kind"] == "mouse"
    assert event["flags"] == (
        backend_module._MOUSEEVENTF_MOVE
        | backend_module._MOUSEEVENTF_ABSOLUTE
        | backend_module._MOUSEEVENTF_VIRTUALDESK
    )
    assert _from_absolute_65535(event["dx"], 0, 1920) == 960
    assert _from_absolute_65535(event["dy"], 0, 1080) == 540


@WINDOWS_ONLY
def test_move_round_trips_every_axis_endpoint(fake_user32: _FakeUser32) -> None:
    engine = _engine()
    for point in ((0, 0), (1919, 1079), (100, 200), (42, 900)):
        engine.move(*point)
        event = fake_user32.batches[-1][0]
        assert (
            _from_absolute_65535(event["dx"], 0, 1920),
            _from_absolute_65535(event["dy"], 0, 1080),
        ) == point


@WINDOWS_ONLY
def test_move_secondary_monitor_offset_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    """VIRTUALDESK normalization covers the whole virtual desktop (negative origins)."""
    fake = _FakeUser32(metrics=(-1920, 0, 3840, 1080))
    monkeypatch.setattr(backend_module, "_user32", fake)
    _engine().move(-1000, 500)
    event = fake.batches[0][0]
    assert (
        _from_absolute_65535(event["dx"], -1920, 3840),
        _from_absolute_65535(event["dy"], 0, 1080),
    ) == (-1000, 500)


def test_absolute_normalization_bijection() -> None:
    for extent in (1, 2, 1080, 1920):
        for value in (0, extent - 1, extent // 2):
            origin = 0
            normalized = _to_absolute_65535(value, origin, extent)
            assert 0 <= normalized <= 65535
            assert _from_absolute_65535(normalized, origin, extent) == value


# --- click batching -----------------------------------------------------------------------------


@WINDOWS_ONLY
def test_click_batches_move_down_up_in_one_call(fake_user32: _FakeUser32) -> None:
    _engine().click(30, 45)
    assert len(fake_user32.batches) == 1
    flags = [event["flags"] for event in fake_user32.batches[0]]
    assert flags == [
        backend_module._MOUSEEVENTF_MOVE
        | backend_module._MOUSEEVENTF_ABSOLUTE
        | backend_module._MOUSEEVENTF_VIRTUALDESK,
        backend_module._MOUSEEVENTF_LEFTDOWN,
        backend_module._MOUSEEVENTF_LEFTUP,
    ]


@WINDOWS_ONLY
def test_double_click_batches_two_pairs(fake_user32: _FakeUser32) -> None:
    _engine().click(30, 45, clicks=2)
    assert len(fake_user32.batches) == 1
    assert len(fake_user32.batches[0]) == 5  # move + two down/up pairs


@WINDOWS_ONLY
def test_click_interval_paces_repeated_clicks(
    fake_user32: _FakeUser32, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(backend_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    _engine(click_interval=0.05).click(30, 45, clicks=3)
    assert len(fake_user32.batches) == 3  # first batch + one per extra click
    assert sleeps == [0.05, 0.05]


@WINDOWS_ONLY
def test_click_rejects_nonpositive_clicks(fake_user32: _FakeUser32) -> None:
    with pytest.raises(ValueError):
        _engine().click(30, 45, clicks=0)
    assert fake_user32.batches == []


# --- fail-closed semantics -----------------------------------------------------------------------


@WINDOWS_ONLY
def test_sendinput_ret_zero_raises_input_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeUser32(send_input_results=[0])
    monkeypatch.setattr(backend_module, "_user32", fake)
    with pytest.raises(InputBlockedError, match="blocked"):
        _engine().move(100, 100)
    assert len(fake.batches) == 1  # the dispatch was attempted, then failed closed


@WINDOWS_ONLY
@pytest.mark.parametrize("corner", [(0, 0), (1919, 1079), (0, 1079), (1919, 0)])
def test_failsafe_corner_raises_before_any_dispatch(
    monkeypatch: pytest.MonkeyPatch, corner: tuple[int, int]
) -> None:
    fake = _FakeUser32(cursor=corner)
    monkeypatch.setattr(backend_module, "_user32", fake)
    with pytest.raises(InputBlockedError, match="failsafe"):
        _engine().move(500, 500)
    with pytest.raises(InputBlockedError, match="failsafe"):
        _engine().click(500, 500)
    with pytest.raises(InputBlockedError, match="failsafe"):
        _engine().type_text("hello")
    assert fake.batches == []  # fail closed BEFORE any physical input


@WINDOWS_ONLY
def test_failsafe_mid_screen_cursor_dispatches(fake_user32: _FakeUser32) -> None:
    _engine().move(960, 540)
    assert len(fake_user32.batches) == 1


@WINDOWS_ONLY
def test_failsafe_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeUser32(cursor=(0, 0))
    monkeypatch.setattr(backend_module, "_user32", fake)
    _engine(failsafe=False).move(100, 100)
    assert len(fake.batches) == 1


# --- typing (KEYEVENTF_UNICODE, layout-proof) ------------------------------------------------------


@WINDOWS_ONLY
def test_type_text_batches_whole_string_as_unicode(fake_user32: _FakeUser32) -> None:
    _engine().type_text("hi")
    assert len(fake_user32.batches) == 1
    events = fake_user32.batches[0]
    assert len(events) == 4  # two chars x (down+up)
    assert [event["scan"] for event in events] == [ord("h"), ord("h"), ord("i"), ord("i")]
    assert events[0]["flags"] == backend_module._KEYEVENTF_UNICODE
    assert events[1]["flags"] == backend_module._KEYEVENTF_UNICODE | backend_module._KEYEVENTF_KEYUP
    assert all(event["vk"] == 0 for event in events)  # wVk must be 0 for VK_PACKET


@WINDOWS_ONLY
def test_type_text_arabic_string_is_layout_proof(fake_user32: _FakeUser32) -> None:
    """Arabic must not pass through any keyboard-layout remapping (Session1 bug)."""
    arabic = "مرحبا"
    _engine().type_text(arabic)
    assert len(fake_user32.batches) == 1
    events = fake_user32.batches[0]
    assert len(events) == 2 * len(arabic)
    decoded = "".join(
        chr(event["scan"]) for event in events[::2]  # every down event carries the unit
    )
    assert decoded == arabic


def test_type_text_units_newline_tab_and_astral() -> None:
    units = _text_to_key_units("a\n\t\U0001f600")  # char, LF, TAB, astral smiley
    kinds = [(vk, scan, flags) for vk, scan, flags in units]
    assert kinds[0] == (0, ord("a"), backend_module._KEYEVENTF_UNICODE)
    assert kinds[2] == (0x0D, 0, 0)  # '\n' -> VK_RETURN down (pyautogui parity)
    assert kinds[3] == (0x0D, 0, backend_module._KEYEVENTF_KEYUP)
    assert kinds[4] == (0x09, 0, 0)  # '\t' -> VK_TAB down
    # astral char -> two surrogate code units, each down+up
    surrogate_units = [scan for _vk, scan, _flags in kinds[6::2]]
    assert len(surrogate_units) == 2
    assert all(0xD800 <= unit <= 0xDFFF for unit in surrogate_units)


@WINDOWS_ONLY
def test_type_text_chunks_at_1000_events(
    fake_user32: _FakeUser32,
) -> None:
    text = "a" * 1200  # 2400 events -> chunks of 1000, 1000, 400
    _engine().type_text(text)
    assert [len(batch) for batch in fake_user32.batches] == [1000, 1000, 400]


@WINDOWS_ONLY
def test_type_text_calls_before_chunk_per_chunk(fake_user32: _FakeUser32) -> None:
    checks: list[int] = []
    _engine().type_text("a" * 400, before_chunk=lambda: checks.append(1))  # 800 events: 1 chunk
    assert checks == [1]
    _engine().type_text("a" * 600, before_chunk=lambda: checks.append(1))  # 1200 events: 2 chunks
    assert checks == [1, 1, 1]  # the pre-type check in execute() plus one per chunk
    assert len(fake_user32.batches) == 3


@WINDOWS_ONLY
def test_type_text_paced_option_sleeps_per_char(
    fake_user32: _FakeUser32, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(backend_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    _engine(type_interval=0.005).type_text("abc")
    assert len(fake_user32.batches) == 3  # one char per paced chunk
    assert [len(batch) for batch in fake_user32.batches] == [2, 2, 2]
    assert sleeps == [0.005, 0.005, 0.005]


@WINDOWS_ONLY
def test_type_text_empty_is_noop(fake_user32: _FakeUser32) -> None:
    _engine().type_text("")
    assert fake_user32.batches == []


# --- chords ----------------------------------------------------------------------------------------


@WINDOWS_ONLY
def test_chord_ctrl_s_is_one_batch_with_reverse_release(fake_user32: _FakeUser32) -> None:
    _engine().chord(["ctrl", "s"])
    assert len(fake_user32.batches) == 1  # ONE SendInput call: safer than 4 keybd_events
    events = fake_user32.batches[0]
    assert [(event["vk"], event["flags"]) for event in events] == [
        (0x11, 0),  # ctrl down
        (0x53, 0),  # s down
        (0x53, backend_module._KEYEVENTF_KEYUP),
        (0x11, backend_module._KEYEVENTF_KEYUP),  # modifiers released in reverse order
    ]


@WINDOWS_ONLY
def test_chord_three_keys_press_in_order_release_in_reverse(fake_user32: _FakeUser32) -> None:
    _engine().chord(["ctrl", "shift", "esc"])
    events = fake_user32.batches[0]
    vks = [event["vk"] for event in events]
    assert vks == [0x11, 0x10, 0x1B, 0x1B, 0x10, 0x11]  # VK_SHIFT=0x10 (pyautogui parity)
    flags = [event["flags"] for event in events]
    assert flags[:3] == [0, 0, 0]
    assert flags[3:] == [backend_module._KEYEVENTF_KEYUP] * 3


@WINDOWS_ONLY
def test_chord_uppercase_letter_injects_shift(fake_user32: _FakeUser32) -> None:
    _engine().chord(["A"])
    events = fake_user32.batches[0]
    assert [(event["vk"], event["flags"]) for event in events] == [
        (0x10, 0),  # shift down first
        (0x41, 0),
        (0x41, backend_module._KEYEVENTF_KEYUP),
        (0x10, backend_module._KEYEVENTF_KEYUP),  # shift released last
    ]


@WINDOWS_ONLY
def test_chord_shift_member_via_vkkeyscan(monkeypatch: pytest.MonkeyPatch) -> None:
    """'!' resolves through VkKeyScanW with its shift flag honored (pyautogui parity)."""
    fake = _FakeUser32()
    fake.VkKeyScanW = lambda char: 0x0031 | 0x0100  # VK '1' + shift flag
    monkeypatch.setattr(backend_module, "_user32", fake)
    _engine().chord(["!"])
    events = fake.batches[0]
    assert [(event["vk"], event["flags"]) for event in events] == [
        (0x10, 0),  # shift down first
        (0x31, 0),
        (0x31, backend_module._KEYEVENTF_KEYUP),
        (0x10, backend_module._KEYEVENTF_KEYUP),  # shift released last
    ]


@WINDOWS_ONLY
def test_chord_unknown_key_fails_closed(fake_user32: _FakeUser32) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        _engine().chord(["definitely_not_a_key"])
    assert fake_user32.batches == []


# --- scroll ----------------------------------------------------------------------------------------


@WINDOWS_ONLY
def test_scroll_wheel_event_carries_delta_times_120(fake_user32: _FakeUser32) -> None:
    _engine().scroll(-3)
    event = fake_user32.batches[0][0]
    assert event["kind"] == "mouse"
    assert event["flags"] == backend_module._MOUSEEVENTF_WHEEL
    assert event["mouseData"] == (-3 * 120) & 0xFFFFFFFF  # DWORD wrap, pyautogui parity


# --- event builders (no user32 needed) ---------------------------------------------------------------


def test_mouse_flag_event_defaults() -> None:
    event = _decode_event(_mouse_flag_event(backend_module._MOUSEEVENTF_LEFTDOWN))
    assert event == {"kind": "mouse", "flags": 0x2, "dx": 0, "dy": 0, "mouseData": 0}


def test_key_event_unicode_fields() -> None:
    event = _decode_event(
        _key_event(0, 0x00E9, backend_module._KEYEVENTF_UNICODE | backend_module._KEYEVENTF_KEYUP)
    )
    assert event == {
        "kind": "keyboard",
        "vk": 0,
        "scan": 0x00E9,
        "flags": backend_module._KEYEVENTF_UNICODE | backend_module._KEYEVENTF_KEYUP,
    }


def test_mouse_move_event_flags() -> None:
    event = _decode_event(_mouse_move_event(10, 20, METRICS))
    assert event["flags"] == (
        backend_module._MOUSEEVENTF_MOVE
        | backend_module._MOUSEEVENTF_ABSOLUTE
        | backend_module._MOUSEEVENTF_VIRTUALDESK
    )


# --- pyautogui fallback engine -----------------------------------------------------------------------


class _FakePyautogui:
    """pyautogui stand-in for fallback-engine tests (records calls, raises on cue)."""

    FailSafeException = type("FailSafeException", (Exception,), {})

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.fail = fail
        self.PAUSE = 99.0  # sentinel: the engine must overwrite it
        self.FAILSAFE = False

    def moveTo(self, x: int, y: int) -> None:
        self._maybe_fail()
        self.calls.append(("moveTo", x, y))

    def click(self, *args: object, **kwargs: object) -> None:
        self._maybe_fail()
        self.calls.append(("click", *args, *kwargs.items()))

    def mouseDown(self, button: str = "left") -> None:
        self._maybe_fail()
        self.calls.append(("mouseDown", button))

    def mouseUp(self, button: str = "left") -> None:
        self._maybe_fail()
        self.calls.append(("mouseUp", button))

    def write(self, *args: object, **kwargs: object) -> None:
        self._maybe_fail()
        self.calls.append(("write", *args))

    def hotkey(self, *args: object, **kwargs: object) -> None:
        self._maybe_fail()
        self.calls.append(("hotkey", *args))

    def scroll(self, *args: object, **kwargs: object) -> None:
        self._maybe_fail()
        self.calls.append(("scroll", *args))

    def _maybe_fail(self) -> None:
        if self.fail:
            raise self.FailSafeException("failsafe")


def test_pyautogui_engine_maps_failsafe_to_input_blocked() -> None:
    engine = PyAutoGuiInputEngine(_FakePyautogui(fail=True))
    with pytest.raises(InputBlockedError, match="failsafe"):
        engine.move(1, 2)
    with pytest.raises(InputBlockedError, match="failsafe"):
        engine.click(1, 2)
    with pytest.raises(InputBlockedError, match="failsafe"):
        engine.chord(["ctrl", "s"])
    with pytest.raises(InputBlockedError, match="failsafe"):
        engine.scroll(3)


def test_pyautogui_engine_sets_pause_zero_by_default() -> None:
    pa = _FakePyautogui()
    PyAutoGuiInputEngine(pa)
    assert pa.PAUSE == 0.0  # PERF-004 PAUSE economics on the fallback path
    assert pa.FAILSAFE is True


def test_pyautogui_engine_call_vocabulary() -> None:
    pa = _FakePyautogui()
    engine = PyAutoGuiInputEngine(pa, pause=0.1)
    engine.move(3, 4)
    engine.click(5, 6, clicks=2)
    engine.mouse_down()
    engine.mouse_up()
    engine.chord(["ctrl", "s"])
    engine.scroll(-2)
    assert pa.PAUSE == 0.1  # configurable
    assert pa.calls == [
        ("moveTo", 3, 4),
        ("click", 5, 6, ("clicks", 2), ("interval", 0.08)),
        ("mouseDown", "left"),
        ("mouseUp", "left"),
        ("hotkey", "ctrl", "s"),
        ("scroll", -2),
    ]


def test_pyautogui_engine_types_per_char_with_before_chunk() -> None:
    pa = _FakePyautogui()
    engine = PyAutoGuiInputEngine(pa, type_interval=0.0)
    checks: list[int] = []
    engine.type_text("abc", before_chunk=lambda: checks.append(1))
    assert pa.calls == [("write", "a"), ("write", "b"), ("write", "c")]
    assert checks == [1, 1, 1]  # per-chunk (per-char) stop checks preserved


def test_engine_drag_policy_attributes() -> None:
    send = SendInputEngine()
    assert send.drag_interpolate is False  # minimal stroke by default
    assert send.drag_step_pause == 0.0
    fallback = PyAutoGuiInputEngine(_FakePyautogui())
    assert fallback.drag_interpolate is True  # legacy interpolated stroke
    assert fallback.drag_step_pause == backend_module._DRAG_STEP_PAUSE_SECONDS


def test_recording_engine_matches_engine_contract() -> None:
    """The conftest stub must stay substitutable for the real engines."""
    engine: Any = RecordingEngine()
    engine.move(1, 2)
    engine.click(1, 2, clicks=2)
    engine.mouse_down()
    engine.mouse_up()
    engine.type_text("x", before_chunk=None)
    engine.chord(["ctrl", "s"])
    engine.scroll(1)
    assert engine.calls == [
        ("move", 1, 2),
        ("click", 1, 2, 2),
        ("mouse_down", "left"),
        ("mouse_up", "left"),
        ("type_text", "x"),
        ("chord", "ctrl", "s"),
        ("scroll", 1),
    ]
