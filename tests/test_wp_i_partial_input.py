"""WP-I (ORVEX-2026-109 Addendum 2): SendInput partial-delivery handling.

Win32 ``SendInput`` returns the NUMBER of events it inserted, so a partial insert
(``0 < n < len(events)``) used to pass silently as a full success — a cut chord
left Ctrl/Shift stuck system-wide and a cut click left the button down. The
contract now:

- ``n == len`` -> success, byte-identical to before (no extra dispatch call);
- ``n == 0`` -> today's ``InputBlockedError``, unchanged (nothing inserted,
  nothing stuck);
- ``0 < n < len`` -> the landed prefix (SendInput inserts events in order) is
  walked; EXACTLY the keys/buttons it left down are released in REVERSE press
  order in ONE halt-safe batch (never a resend, one attempt only), and a typed
  ``PartialInputError`` (an ``InputBlockedError`` SUBCLASS — every existing
  ``except InputBlockedError`` keeps failing closed) carries
  sent/total/released/still_stuck (+ ``delivered_chars`` for typing).

Every test here injects a SCRIPTED ``_dispatch`` thunk on a real
``SendInputEngine`` (the same seam test_failsafe_release_corner.py and
test_wp13_field_fixes.py use). The REAL SendInput is never called; the desktop
tripwire stays armed.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from recording_engine import RecordingEngine

import computer_use_mcp.desktop.backend as backend_module
import computer_use_mcp.desktop.input_engine as input_engine_module
from computer_use_mcp.desktop.errors import InputBlockedError, PartialInputError
from computer_use_mcp.desktop.input_engine import (
    _KEYEVENTF_KEYUP,
    SendInputEngine,
)
from computer_use_mcp.desktop.win32 import (
    _KEYEVENTF_UNICODE,
    _MOUSEEVENTF_LEFTDOWN,
    _MOUSEEVENTF_LEFTUP,
)

from computer_use_mcp.core.models import GroundedAction
from computer_use_mcp.core.state import StopToken
from test_controller_integration import (  # noqa: F401 - pytest fixture import
    FAST_LIMITS,
    execute_payload,
    make_session,
)
from test_controller_integration import fresh_server as fresh_server  # noqa: F401 - fixture

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")

SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "computer_use_mcp"


# --- the scripted dispatch seam -----------------------------------------------------------------


class _ScriptedDispatch:
    """A scripted ``_dispatch`` thunk: per-call return values, every batch recorded.

    Returns the scripted value for each call IN ORDER; once the script is
    exhausted every later call succeeds fully (``len(events)``) — so a test only
    scripts the interesting prefix of the call sequence.
    """

    def __init__(self, *returns: int) -> None:
        self.returns = list(returns)
        self.calls: list[list[Any]] = []
        self.retvals: list[int] = []

    def __call__(self, events: list[Any]) -> int:
        self.calls.append([event for event in events])
        if len(self.retvals) < len(self.returns):
            n = self.returns[len(self.retvals)]
        else:
            n = len(events)
        self.retvals.append(n)
        return n


def _partial_engine(
    monkeypatch: pytest.MonkeyPatch, scripted: _ScriptedDispatch, **kwargs: Any
) -> SendInputEngine:
    """A REAL SendInputEngine whose ``_dispatch`` is the scripted thunk.

    Hermetic: the cursor probe is pinned off-corner and the virtual-screen
    metrics are pinned, so no Win32 call runs at all (the tripwire stays armed).
    """
    engine = SendInputEngine(**kwargs)
    monkeypatch.setattr(engine, "_dispatch", scripted)
    monkeypatch.setattr(input_engine_module, "get_cursor_position", lambda: (960, 540))
    monkeypatch.setattr(
        input_engine_module, "_virtual_screen_metrics", lambda: (0, 0, 1920, 1080)
    )
    return engine


def _kbd(event: Any) -> tuple[int, int, int]:
    """Decode one keyboard event to ``(vk, scan, flags)`` for assertions."""
    return (int(event.union.ki.wVk), int(event.union.ki.wScan), int(event.union.ki.dwFlags))


def _mouse_flags(event: Any) -> int:
    return int(event.union.mi.dwFlags)


def payload_of(result: Any) -> dict[str, Any]:
    """The JSON of a reply — either a plain dict or MCP content blocks."""
    import json

    if isinstance(result, list):
        return json.loads(result[0].text)
    return result


# --- I1/I2: exact count check + precise release -------------------------------------------------


@WINDOWS_ONLY
def test_chord_cut_after_modifier_downs_releases_exactly_reverse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ctrl+shift+s cut after ctrl+shift down: the cleanup batch is EXACTLY
    [shift up, ctrl up] (reverse press order), and the error carries the truth."""
    scripted = _ScriptedDispatch(2)  # the OS accepted only ctrl_dn + shift_dn
    engine = _partial_engine(monkeypatch, scripted)
    with pytest.raises(PartialInputError) as excinfo:
        engine.chord(["ctrl", "shift", "s"])
    exc = excinfo.value
    assert (exc.sent, exc.total) == (2, 6)
    assert exc.released == ["shift", "ctrl"]  # release (reverse press) order
    assert exc.still_stuck == []
    # ONE cleanup dispatch — exactly the two stuck modifiers' ups, nothing else
    # (no resend of s, no blind sweep).
    assert len(scripted.calls) == 2
    cleanup = scripted.calls[1]
    assert [_kbd(event) for event in cleanup] == [
        (0x10, 0, _KEYEVENTF_KEYUP),  # shift
        (0x11, 0, _KEYEVENTF_KEYUP),  # ctrl
    ]


@WINDOWS_ONLY
def test_click_cut_after_move_and_down_releases_left_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """click cut after move+down: the button would stay down — the cleanup is
    exactly [left up]."""
    scripted = _ScriptedDispatch(2)  # move + left_down landed, left_up did not
    engine = _partial_engine(monkeypatch, scripted)
    with pytest.raises(PartialInputError) as excinfo:
        engine.click(10, 20)
    exc = excinfo.value
    assert (exc.sent, exc.total) == (2, 3)
    assert exc.released == ["mouse_left"]
    assert exc.still_stuck == []
    assert len(scripted.calls) == 2
    assert [_mouse_flags(event) for event in scripted.calls[1]] == [_MOUSEEVENTF_LEFTUP]


@WINDOWS_ONLY
def test_click_cut_after_move_only_needs_no_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """click cut after the MOVE only: nothing is down, so there is NO second
    dispatch at all — and nothing is claimed as released."""
    scripted = _ScriptedDispatch(1)  # only the move landed
    engine = _partial_engine(monkeypatch, scripted)
    with pytest.raises(PartialInputError) as excinfo:
        engine.click(10, 20)
    exc = excinfo.value
    assert (exc.sent, exc.total) == (1, 3)
    assert exc.released == [] and exc.still_stuck == []
    assert len(scripted.calls) == 1  # no release batch was sent


@WINDOWS_ONLY
def test_key_down_with_implied_shift_cut_releases_shift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """key_down(\"A\") (implied shift) cut after shift down: shift is released,
    exactly once, and the hold never happened."""
    scripted = _ScriptedDispatch(1)  # shift_dn landed, a_dn did not
    engine = _partial_engine(monkeypatch, scripted)
    with pytest.raises(PartialInputError) as excinfo:
        engine.key_down("A")
    exc = excinfo.value
    assert (exc.sent, exc.total) == (1, 2)
    assert exc.released == ["shift"]
    assert exc.still_stuck == []
    assert [_kbd(event) for event in scripted.calls[1]] == [(0x10, 0, _KEYEVENTF_KEYUP)]


@WINDOWS_ONLY
def test_cleanup_batch_cut_lists_exactly_the_leftovers_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The release batch ITSELF returns short: still_stuck names exactly the
    leftovers, and there is exactly ONE cleanup attempt (never a loop)."""
    scripted = _ScriptedDispatch(2, 1)  # cut batch -> 2; cleanup [shift,ctrl] -> 1
    engine = _partial_engine(monkeypatch, scripted)
    with pytest.raises(PartialInputError) as excinfo:
        engine.chord(["ctrl", "shift", "s"])
    exc = excinfo.value
    assert exc.sent == 2 and exc.total == 6
    assert exc.released == ["shift"]  # the cleanup's landed prefix
    assert exc.still_stuck == ["ctrl"]  # the cleanup's unlanded remainder
    assert len(scripted.calls) == 2  # one cleanup attempt, no loop


@WINDOWS_ONLY
def test_unicode_batch_cut_releases_the_landed_vk_packet_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A KEYEVENTF_UNICODE down that landed is released with the MATCHING unicode
    up (same scan code unit, UNICODE|KEYUP flags)."""
    scripted = _ScriptedDispatch(1)  # the U+0627 down landed, its up did not
    engine = _partial_engine(monkeypatch, scripted)
    with pytest.raises(PartialInputError) as excinfo:
        engine.type_text("ا")  # Arabic alef: one VK_PACKET down + up
    exc = excinfo.value
    assert (exc.sent, exc.total) == (1, 2)
    assert exc.released == ["U+0627"]
    vk, scan, flags = _kbd(scripted.calls[1][0])
    assert vk == 0 and scan == 0x0627
    assert flags == (_KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP)


@WINDOWS_ONLY
def test_key_up_release_batch_cut_names_the_unlanded_up_as_stuck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELTA 1 D5: a RELEASE batch cut mid-sweep — key_up("A")'s release half is
    [a_up, shift_up]; a return of 1 landed only a_up, so shift is still down and
    is named in still_stuck with NO cleanup batch (exactly ONE dispatch)."""
    scripted = _ScriptedDispatch(1)
    engine = _partial_engine(monkeypatch, scripted)
    with pytest.raises(PartialInputError) as excinfo:
        engine.key_up("A")
    exc = excinfo.value
    assert (exc.sent, exc.total) == (1, 2)
    assert exc.released == [] and exc.still_stuck == ["shift"]
    assert len(scripted.calls) == 1  # one dispatch, no cleanup batch


class _ExplodingCleanup(_ScriptedDispatch):
    """A scripted dispatch whose CLEANUP call raises (simulated OSError from the
    ctypes SendInput call)."""

    def __call__(self, events: list[Any]) -> int:
        if len(self.retvals) >= len(self.returns):
            self.calls.append(list(events))
            raise OSError("simulated ctypes failure inside the cleanup dispatch")
        return super().__call__(events)


@WINDOWS_ONLY
def test_raising_cleanup_batch_still_raises_the_partial_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELTA 1 D2: a RAISING cleanup dispatch must never replace
    PartialInputError — everything the landed prefix left down goes to
    still_stuck (release order), released is empty, still ONE attempt."""
    scripted = _ExplodingCleanup(2)  # cut batch -> 2; cleanup RAISES
    engine = _partial_engine(monkeypatch, scripted)
    with pytest.raises(PartialInputError) as excinfo:
        engine.chord(["ctrl", "shift", "s"])
    exc = excinfo.value
    assert (exc.sent, exc.total) == (2, 6)
    assert exc.released == []
    assert exc.still_stuck == ["shift", "ctrl"]  # release order, nothing masked
    assert type(excinfo.value) is PartialInputError  # the OSError never surfaces
    assert len(scripted.calls) == 2  # one cleanup attempt, no loop, no retry


# --- I1: the untouched branches -----------------------------------------------------------------


@WINDOWS_ONLY
def test_full_return_is_byte_identical_no_extra_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """n == len: success with EXACTLY ONE dispatch call — no extra call, no
    cleanup, no error."""
    scripted = _ScriptedDispatch(6)
    engine = _partial_engine(monkeypatch, scripted)
    engine.chord(["ctrl", "shift", "s"])  # must not raise
    assert len(scripted.calls) == 1
    assert len(scripted.calls[0]) == 6


@WINDOWS_ONLY
def test_zero_return_keeps_the_plain_input_blocked_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """n == 0: today's InputBlockedError, NOT a PartialInputError — nothing was
    inserted, so nothing is stuck and the type stays exact."""
    scripted = _ScriptedDispatch(0)
    engine = _partial_engine(monkeypatch, scripted)
    with pytest.raises(InputBlockedError) as excinfo:
        engine.chord(["a"])
    assert excinfo.type is InputBlockedError  # NOT the subclass
    assert len(scripted.calls) == 1  # the failed dispatch, no cleanup attempt


@WINDOWS_ONLY
@pytest.mark.parametrize("bogus", [99, -3])
def test_out_of_band_returns_are_clamped_without_crashing(
    monkeypatch: pytest.MonkeyPatch, bogus: int
) -> None:
    """n > len clamps to len (plain success); negative n clamps to 0 (today's
    block error). Neither crashes."""
    over = _ScriptedDispatch(bogus)
    engine = _partial_engine(monkeypatch, over)
    if bogus < 0:
        with pytest.raises(InputBlockedError) as excinfo:
            engine.chord(["a"])
        assert excinfo.type is InputBlockedError
    else:
        engine.chord(["a", "b"])  # clamped full success, no raise
    assert len(over.calls) == 1


# --- I3: typing delivered_chars ----------------------------------------------------------------


@WINDOWS_ONLY
def test_paced_type_cut_inside_a_character_excludes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paced typing cut inside a character's event pair: delivered_chars counts
    only the COMPLETED earlier characters; the cut character's down is released
    with its unicode up."""
    scripted = _ScriptedDispatch(2, 1)  # 'a' full; 'b' cut after its down
    engine = _partial_engine(monkeypatch, scripted, type_interval=0.01)
    monkeypatch.setattr(input_engine_module.time, "sleep", lambda _s: None)
    with pytest.raises(PartialInputError) as excinfo:
        engine.type_text("ab")
    exc = excinfo.value
    assert (exc.sent, exc.total) == (1, 2)
    assert exc.delivered_chars == 1  # 'a' fully landed; 'b' did not
    vk, scan, flags = _kbd(scripted.calls[2][0])
    assert (vk, scan) == (0, ord("b"))
    assert flags == (_KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP)


@WINDOWS_ONLY
def test_unpaced_type_cut_mid_chunk_exact_delivered_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unpaced typing cut mid-chunk: delivered_chars is EXACT — a character
    counts only when its down AND up both landed."""
    scripted = _ScriptedDispatch(5)  # 'a' (2) + high pair half (2) + low down (1)
    engine = _partial_engine(monkeypatch, scripted)
    text = "a\U0001F600b"  # BMP char, astral surrogate PAIR, BMP char
    with pytest.raises(PartialInputError) as excinfo:
        engine.type_text(text)
    exc = excinfo.value
    assert exc.total == 8  # 2 + 4 + 2 events
    assert exc.sent == 5
    assert exc.delivered_chars == 1  # only 'a'; the surrogate pair is incomplete
    # the landed low-surrogate down is released with its matching unicode up
    vk, scan, flags = _kbd(scripted.calls[1][0])
    assert vk == 0 and 0xDC00 <= scan <= 0xDFFF
    assert flags == (_KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP)


@WINDOWS_ONLY
def test_unpaced_type_cut_on_a_complete_boundary_reports_all_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cut exactly on a character boundary: every completed character counts and
    nothing is stuck (the landed downs all got their ups)."""
    scripted = _ScriptedDispatch(6)  # 'a' + all four surrogate-pair events
    engine = _partial_engine(monkeypatch, scripted)
    with pytest.raises(PartialInputError) as excinfo:
        engine.type_text("a\U0001F600b")
    exc = excinfo.value
    assert (exc.sent, exc.total) == (6, 8)
    assert exc.delivered_chars == 2  # 'a' + the astral character (all 4 events)
    assert exc.released == [] and exc.still_stuck == []
    assert len(scripted.calls) == 1  # nothing down -> no cleanup batch


# --- I4: an intentional hold is undone entirely, never recorded as held ---------


class _PartialPressEngine(RecordingEngine):
    """A RecordingEngine whose key_down_resolved delegates to a REAL
    SendInputEngine with a scripted partial insert — everything else records."""

    def __init__(self, press_engine: SendInputEngine) -> None:
        super().__init__()
        self._press_engine = press_engine

    def key_down_resolved(self, key: str, resolved: dict[str, tuple[int, bool]]) -> None:
        self.calls.append(("key_down", key))
        self._press_engine.key_down_resolved(key, resolved)


@WINDOWS_ONLY
def test_partial_hold_press_is_not_recorded_as_held(
    real_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """key_down with an implied shift cut after shift down: the engine released
    the shift, the hold bookkeeping did NOT record the key (no backend key_up
    sweep for it — the engine's cleanup already owns the release)."""
    scripted = _ScriptedDispatch(1)  # shift_dn landed, a_dn did not
    press_engine = _partial_engine(monkeypatch, scripted)
    engine = _PartialPressEngine(press_engine)
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    monkeypatch.setattr(real_backend, "_last_focus_transition", 0.0)
    monkeypatch.setattr(real_backend, "_last_key_dispatch", 0.0)
    with pytest.raises(PartialInputError) as excinfo:
        real_backend.execute(
            GroundedAction(
                action="keypress", keys=["A"], hold_ms=100, confidence=1.0
            )
        )
    assert excinfo.value.released == ["shift"]
    # the engine released the shift inside the press call...
    assert len(scripted.calls) == 2
    assert [_kbd(event) for event in scripted.calls[1]] == [(0x10, 0, _KEYEVENTF_KEYUP)]
    # ...and the backend's hold sweep did NOT re-release it (the key was never
    # recorded as held: no key_up call at all).
    assert not [call for call in engine.calls if call[0] == "key_up"]


class _PartialKeyUpEngine(RecordingEngine):
    """A RecordingEngine whose key_up_resolved raises a PartialInputError that
    left the key stuck (a cut release sweep)."""

    def key_up_resolved(self, key: str, resolved: dict[str, tuple[int, bool]]) -> None:
        raise PartialInputError(sent=1, total=1, released=[], still_stuck=[str(key)])


class _MidHoldFailure(StopToken):
    """``wait`` raises a synthetic mid-hold exception (the in-flight error)."""

    def wait(self, timeout: float | None = None) -> bool:
        raise RuntimeError("synthetic mid-hold failure")


@WINDOWS_ONLY
def test_masked_partial_release_logs_a_warning_naming_the_stuck_key(
    real_backend: Any, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """DELTA 1 D3: when a key_up_resolved PartialInputError is dropped by the
    never-mask rule (an error is already in flight), the stuck key is still
    logged as a WARNING through the desktop package's module logger."""
    import logging

    engine = _PartialKeyUpEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    monkeypatch.setattr(real_backend, "_last_focus_transition", 0.0)
    monkeypatch.setattr(real_backend, "_last_key_dispatch", 0.0)
    with caplog.at_level(logging.WARNING, logger="computer_use_mcp.desktop.backend"):
        with pytest.raises(RuntimeError, match="synthetic mid-hold"):
            real_backend.execute(
                GroundedAction(action="keypress", keys=["a"], hold_ms=100, confidence=1.0),
                stop=_MidHoldFailure(),
            )
    warnings = [
        record for record in caplog.records
        if record.levelno >= logging.WARNING and "physically down" in record.getMessage()
    ]
    assert warnings, caplog.text
    # DELTA 2 (E3): the EXACT stuck-key text and the masking error's type name,
    # not a substring that any message would contain.
    assert any(
        "physically down: a" in record.getMessage() and "RuntimeError" in record.getMessage()
        for record in warnings
    ), [record.getMessage() for record in warnings]


@WINDOWS_ONLY
def test_propagating_partial_release_logs_no_warning(
    real_backend: Any, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """DELTA 2 (E3): nothing is in flight, so the release sweep's
    PartialInputError IS the raised report — it propagates and NO warning is
    logged (no double-report)."""
    import logging

    engine = _PartialKeyUpEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    monkeypatch.setattr(real_backend, "_last_focus_transition", 0.0)
    monkeypatch.setattr(real_backend, "_last_key_dispatch", 0.0)
    monkeypatch.setattr(backend_module.time, "sleep", lambda _s: None)
    with caplog.at_level(logging.WARNING, logger="computer_use_mcp.desktop.backend"):
        with pytest.raises(PartialInputError) as excinfo:
            real_backend.execute(
                GroundedAction(action="keypress", keys=["a"], hold_ms=100, confidence=1.0),
            )
    assert excinfo.value.still_stuck == ["a"]
    assert not [
        record for record in caplog.records
        if record.levelno >= logging.WARNING and "physically down" in record.getMessage()
    ], caplog.text


# --- backend composition: the chunked type offset ------------------------------------------------


@WINDOWS_ONLY
def test_backend_chunked_type_composes_the_delivered_offset(
    real_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I3/DELTA 1 D5: the SECOND chunk of a multi-chunk type is cut mid-chunk —
    the backend composes chunk 1's offset (3) with chunk 2's intra-count (2):
    'd' and 'e' completed inside the landed prefix, 'f' did not -> 5."""
    monkeypatch.setattr(backend_module, "TYPE_CHUNK_CHARS", 3)
    scripted = _ScriptedDispatch(6, 5)  # chunk1 (6) full; chunk2 (6) -> 5 landed
    engine = _partial_engine(monkeypatch, scripted)
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    monkeypatch.setattr(real_backend, "_last_focus_transition", 0.0)
    monkeypatch.setattr(real_backend, "_last_key_dispatch", 0.0)
    with pytest.raises(PartialInputError) as excinfo:
        real_backend.execute(
            GroundedAction(action="type", text="abcdef", confidence=1.0)
        )
    exc = excinfo.value
    assert (exc.sent, exc.total) == (5, 6)  # the CUT chunk: f's down landed, up did not
    assert exc.delivered_chars == 5  # chunk 1 (3) + d + e
    assert len(scripted.calls) == 3  # two chunk batches + the one cleanup batch


@WINDOWS_ONLY
def test_unpaced_type_longer_than_one_chunk_with_pair_crossing_the_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELTA 1 D5: text longer than one 1000-event chunk whose surrogate pair
    CROSSES the chunk boundary — delivered_chars stays exact across chunks.
    Layout: 499 BMP chars (998 units) + one astral pair (4 units: 998..1001,
    crossing 1000) + one BMP char. Chunk 2 = [low_dn, low_up, y_dn, y_up], cut
    at 3: the astral char completed (all 4 events across both batches) and 'y'
    did not -> 500; 'y''s landed down is released with its unicode up."""
    scripted = _ScriptedDispatch(1000, 3)
    engine = _partial_engine(monkeypatch, scripted)
    text = "x" * 499 + "\U0001F600" + "y"
    with pytest.raises(PartialInputError) as excinfo:
        engine.type_text(text)
    exc = excinfo.value
    assert (exc.sent, exc.total) == (3, 4)  # the CUT second chunk
    assert exc.delivered_chars == 500  # 499 x + the astral char; y incomplete
    assert len(scripted.calls) == 3
    vk, scan, flags = _kbd(scripted.calls[2][0])
    assert (vk, scan) == (0, ord("y"))
    assert flags == (_KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP)


# --- I5: multi-batch actions keep completed batches, clean only the cut one ----------------------


@WINDOWS_ONLY
def test_multi_click_cut_keeps_earlier_batches_and_cleans_the_cut_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """click_interval > 0: batch 1 (move+down+up) completed fully; batch 2 is cut
    after its down — the cleanup releases exactly that button, batch 1 stays
    done (no re-send of it)."""
    scripted = _ScriptedDispatch(3, 1, 1)  # batch1 full; batch2 cut after its down; cleanup ok
    engine = _partial_engine(monkeypatch, scripted, click_interval=0.01)
    with pytest.raises(PartialInputError) as excinfo:
        engine.click(10, 20, clicks=2)
    exc = excinfo.value
    assert (exc.sent, exc.total) == (1, 2)  # the CUT batch, not the whole action
    assert exc.released == ["mouse_left"]
    assert len(scripted.calls) == 3
    assert [_mouse_flags(event) for event in scripted.calls[0]][1:] == [
        _MOUSEEVENTF_LEFTDOWN,
        _MOUSEEVENTF_LEFTUP,
    ]  # batch 1 went out complete and was never resent
    assert [_mouse_flags(event) for event in scripted.calls[2]] == [_MOUSEEVENTF_LEFTUP]


# --- I6: the reply surface through the pipeline --------------------------------------------------


def _make_partial_backend(raise_on: str, **exc_kwargs: Any) -> Any:
    """A ScriptedBackend whose ``raise_on`` action raises a scripted
    PartialInputError (as a real cut SendInput would surface through the
    backend); everything else is the ordinary fake."""

    from test_controller_integration import ScriptedBackend

    class PartialBackend(ScriptedBackend):
        def execute(self, action: Any, stop: Any = None) -> str:
            if action.action.value == raise_on:
                kwargs = dict(exc_kwargs)
                delivered = kwargs.pop("delivered_chars", None)
                exc = PartialInputError(**kwargs)
                if delivered is not None:
                    exc.delivered_chars = delivered
                raise exc
            return super().execute(action, stop)

    return PartialBackend


async def test_partial_input_in_item_0_stops_the_batch_with_the_token(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial insert in item 0 of a follow_ups batch STOPS the batch (later
    items are unsafe while input is half-landed); the reply carries the closed
    ``partial_input`` block and the ``partial_input`` stop token, on the SAME
    ``failed`` shape a blocked input uses."""
    import computer_use_mcp.server.execute as srv_server_execute

    backend = _make_partial_backend(
        "keypress", sent=2, total=6, released=["shift", "ctrl"], still_stuck=[]
    )(flip=False)
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False,
        require_approval=False, limits=FAST_LIMITS,
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "keypress", keys=["ctrl", "shift", "s"], approved=True,
            include_screenshot_after=False,
            follow_ups=[{"action": "keypress", "keys": ["a"]}],
        )
    )
    assert payload["result"] == "failed" and payload["ok"] is False
    assert payload["partial_input"] == {
        "sent": 2, "total": 6, "released": ["shift", "ctrl"], "still_stuck": [],
    }
    assert payload["follow_ups_stopped_reason"] == "partial_input"
    entries = payload["follow_up_results"]
    assert len(entries) == 1  # item 1 never ran
    assert entries[0]["partial_input"]["sent"] == 2
    # NOT presented as "nothing was dispatched":
    assert "nothing was dispatched" not in payload["message"]
    assert "not retried" in payload["message"]
    # everything still down was released -> no scary stuck sentence
    assert "PHYSICALLY DOWN" not in payload["message"]


def test_plan_classifier_maps_partial_input_to_execution_error() -> None:
    """DELTA 1 D1: the REAL classifier reads a partial-insert cut as an INPUT
    ERROR in BOTH positions — a cut in item 0 (through the outcome kind table,
    ``error`` -> execution_error) and a cut in item 1 (through the stop-reason
    table, ``partial_input`` -> execution_error) must AGREE."""
    from computer_use_mcp.planning import plan as plan_module

    class _ClassifierOutcome:
        def __init__(self, kind: str, stopped: str | None) -> None:
            self.kind = kind
            self.follow_ups_stopped_reason = stopped

    cut_entry = {"kind": "error", "result": "failed"}
    outcome_item0 = _ClassifierOutcome("error", "partial_input")
    assert (
        plan_module.plan_failure_reason(outcome_item0, [cut_entry], result="failed")
        == "execution_error"
    )
    entries_item1 = [
        {"kind": "executed", "result": "uncertain", "verification_outcome": "uncertain"},
        cut_entry,
    ]
    outcome_item1 = _ClassifierOutcome("executed", "partial_input")
    assert (
        plan_module.plan_failure_reason(outcome_item1, entries_item1, result="failed")
        == "execution_error"
    )


def _make_cut_on_nth_backend(raise_on: str, cut_on_call: int, **exc_kwargs: Any) -> Any:
    """A ScriptedBackend that executes ``raise_on`` normally cut_on_call-1 times
    and raises a scripted PartialInputError on the cut_on_call-th one."""

    from test_controller_integration import ScriptedBackend

    class CutOnNthBackend(ScriptedBackend):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.calls_of_kind = 0

        def execute(self, action: Any, stop: Any = None) -> str:
            if action.action.value == raise_on:
                self.calls_of_kind += 1
                if self.calls_of_kind == cut_on_call:
                    kwargs = dict(exc_kwargs)
                    delivered = kwargs.pop("delivered_chars", None)
                    exc = PartialInputError(**kwargs)
                    if delivered is not None:
                        exc.delivered_chars = delivered
                    raise exc
            return super().execute(action, stop)

    return CutOnNthBackend


async def test_partial_input_in_item_1_stops_the_batch_and_its_entry_carries_the_block(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 D1: a partial insert in follow_ups ITEM 1 (item 0 executes
    normally) stops the batch with ``partial_input``; the cut item's ENTRY
    carries the ``partial_input`` block (the flat payload stays item 0's)."""
    import computer_use_mcp.server.execute as srv_server_execute

    backend = _make_cut_on_nth_backend(
        "keypress", cut_on_call=2,
        sent=1, total=2, released=[], still_stuck=["shift"],
    )(flip=False)
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False,
        require_approval=False, limits=FAST_LIMITS,
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "keypress", keys=["a"], approved=True,
            include_screenshot_after=False,
            follow_ups=[{"action": "keypress", "keys": ["b"]}],
        )
    )
    assert payload["follow_ups_stopped_reason"] == "partial_input"
    entries = payload["follow_up_results"]
    assert len(entries) == 2
    assert entries[0]["result"] in {"verified", "uncertain"}  # item 0 ran normally
    assert entries[1]["partial_input"]["sent"] == 1
    assert entries[1]["partial_input"]["still_stuck"] == ["shift"]
    assert "partial_input" not in payload  # the flat payload stays item 0's


async def test_partial_input_batch_reply_still_stuck_message_names_the_keys(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-empty still_stuck is said plainly: keys/buttons may still be
    physically down and a human should press and release them."""
    import computer_use_mcp.server.execute as srv_server_execute

    backend = _make_partial_backend(
        "keypress", sent=2, total=6, released=["shift"], still_stuck=["ctrl"]
    )(flip=False)
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False,
        require_approval=False, limits=FAST_LIMITS,
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "keypress", keys=["ctrl", "shift", "s"], approved=True,
            include_screenshot_after=False,
        )
    )
    assert payload["result"] == "failed"
    assert payload["partial_input"]["still_stuck"] == ["ctrl"]
    assert "PHYSICALLY DOWN" in payload["message"]
    assert "ctrl" in payload["message"]
    assert "press and release them by hand" in payload["message"]


async def test_typing_partial_reply_carries_delivered_chars(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typing + a partial insert: the reply carries the EXISTING
    ``partial_delivery.delivered_chars`` contract beside the new block."""
    import computer_use_mcp.server.execute as srv_server_execute

    backend = _make_partial_backend(
        "type", sent=3, total=8, released=["U+0063"], still_stuck=[],
        delivered_chars=5,
    )(flip=False)
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False,
        require_approval=False, limits=FAST_LIMITS,
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id, "type", text="hello world", approved=True,
            include_screenshot_after=False,
        )
    )
    assert payload["result"] == "failed" and payload["ok"] is False
    assert payload["partial_delivery"] == {"delivered_chars": 5}
    assert payload["partial_input"]["sent"] == 3
    assert payload.get("follow_ups_stopped_reason") is None  # not a batch call


async def test_recording_around_a_partial_insert_is_not_cancelled_as_refused(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP-E x WP-I: the recording started around the action is NOT cancelled as
    ``action_refused`` — a partial insert DID land, so the record block stays
    started and collectable."""
    import asyncio

    import computer_use_mcp.server.execute as srv_server_execute
    import computer_use_mcp.server.record as srv_server_record

    backend = _make_partial_backend(
        "click", sent=2, total=3, released=["mouse_left"], still_stuck=[]
    )(flip=False)
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False,
        require_approval=False, limits=FAST_LIMITS,
    )
    reply = await srv_server_execute.computer_execute(
        session_id, "click", x=100, y=100, approved=True,
        include_screenshot_after=False, record={"duration_s": 1.0},
    )
    body = payload_of(reply)
    assert body["result"] == "failed"
    assert body["record"]["started"] is True
    assert "cancelled" not in body["record"]  # input DID land — never "action_refused"
    # tidy up: cancel the recording so the session slot frees for the next test
    record_id = body["record"]["record_id"]
    await srv_server_record.computer_record_result(session_id, record_id, cancel=True)


# --- the subclass contract -----------------------------------------------------------------------


def test_partial_input_error_is_an_input_blocked_error_subclass() -> None:
    """Every existing ``except InputBlockedError`` site keeps failing closed: the
    new error is a SUBCLASS, so it is caught everywhere the base is."""
    assert issubclass(PartialInputError, InputBlockedError)


def _input_blocked_error_special_cases(source: str) -> list[str]:
    """The sites a NEW InputBlockedError special-case would add (DELTA 1 D5).

    Detects (1) ``except`` handlers naming InputBlockedError (bare, aliased, or
    inside a tuple) and (2) comparisons against it — ``type(x) is/==
    InputBlockedError`` or ``__name__ == "InputBlockedError"`` — the exact-match
    checks a PartialInputError SUBCLASS would silently fail.
    """
    found: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            names: set[str] = set()
            for sub in ast.walk(node.type):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    names.add(sub.attr)
            if "InputBlockedError" in names:
                found.append(f"except handler at line {node.lineno}")
        if isinstance(node, ast.Compare) and "InputBlockedError" in ast.dump(node):
            found.append(f"comparison at line {node.lineno}")
    return found


def test_the_scan_helper_flags_a_special_case_site() -> None:
    """Negative self-test: the SAME helper must flag every forbidden form, or the
    src scan below would pass vacuously."""
    snippet = "\n".join(
        [
            "try:",
            "    pass",
            "except InputBlockedError:",
            "    pass",
            "flag_a = type(err) is InputBlockedError",
            "flag_b = type(err) == InputBlockedError",
            "flag_c = err.__name__ == 'InputBlockedError'",
        ]
    )
    sites = _input_blocked_error_special_cases(snippet)
    assert len(sites) == 4, sites  # one except handler + three comparisons


def test_no_input_blocked_error_special_case_sites_in_src() -> None:
    """DELTA 1 D5: src/ contains NO ``except InputBlockedError`` (alone or in a
    tuple) and NO type/``__name__`` comparison against it — a subclass travels
    with its base everywhere, and any new site forces a review of this file."""
    hits: list[str] = []
    scanned = 0
    for path in sorted(SRC_DIR.rglob("*.py")):
        scanned += 1
        for item in _input_blocked_error_special_cases(path.read_text(encoding="utf-8")):
            hits.append(f"{path.name}: {item}")
    assert scanned > 0, "the scan saw no .py files — SRC_DIR must have moved"  # DELTA 2 (E4)
    assert hits == []
