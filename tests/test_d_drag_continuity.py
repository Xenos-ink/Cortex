"""L1-NEW-2 drag-continuity pins: a drag must STROKE, not teleport (R-4).

Live defect (Kimi/Paint session, ``.orvex/live/l1_step5_minimission.log``): the
default SendInput dispatch emitted ``LEFTDOWN -> ONE teleport MOVE -> LEFTUP`` —
freehand apps sample held-button WM_MOUSEMOVE events, so the model's drags
painted DOTS (6 attempts for one visible line). Root cause reproduced in
``.orvex/artifacts/r4_drag_stream.md`` (profile A: 2 moves, 0.05 ms span).

The fix lives at the FACTORY (``LocalComputerBackend._build_input_engine``):
the live engine is built interpolated (~8 px segments) with ~1 ms per-segment
pacing. A bare ``SendInputEngine`` keeps the legacy minimal defaults (its unit
contract is pinned in ``test_input_engines.py`` and stays frozen), and the
geometry helper's legacy ~40 px mode is pinned in ``test_drag_action.py``.

Everything here runs on STUBS (a fake user32 / RecordingEngine), so no test in
this file dispatches real desktop input. The pins, per the R-4 contract:

(a) down at start + up at end, moves BETWEEN them only;
(b) >= N intermediate held-button moves (N = ceil(400 / 8) = 50 for the
    default 8 px granularity on a 400 px stroke);
(c) monotonic toward the target with sane per-step deltas (<= ~8 px each);
(d) move count bounded (no event floods, even for a full-screen stroke);
(e) elapsed time bounded (speed pin: a 400 px stroke well under ~150 ms
    synthesis budget, pauses included);
(f) zero-length drag (x==x2, y==y2) -> press+release only, no intermediate
    moves, no crash.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from recording_engine import RecordingEngine

import computer_use_mcp.backend as backend_module
from computer_use_mcp.backend import (
    LocalComputerBackend,
    SendInputEngine,
    _drag_segment_points,
)
from computer_use_mcp.models import GroundedAction

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")

METRICS = {0: 1920, 1: 1080, 76: 0, 77: 0, 78: 1920, 79: 1080}

#: The drag the live defect was reproduced with: 400 px horizontal, the
#: screenshot==physical passthrough space (matching the r4 artifact).
DRAG_START = (600, 400)
DRAG_END = (1000, 400)
DRAG_DISTANCE = 400

#: Default dense granularity is ~8 px -> 50 intermediate moves for 400 px.
#: The pin accepts the floor of 4 minimum segments too (short strokes).
EXPECTED_MIN_MOVES = DRAG_DISTANCE // backend_module._SENDINPUT_FACTORY_DRAG_STEP_PIXELS

#: Speed pin budget: the contract demands a 400 px line well under ~150 ms.
SPEED_BUDGET_SECONDS = 0.150


class _StreamFakeUser32:
    """user32 stand-in: records every SendInput event batch with timestamps."""

    def __init__(self, cursor: tuple[int, int] = (960, 540)) -> None:
        self.cursor = cursor
        self.batches: list[tuple[float, list[dict[str, int]]]] = []

    def GetCursorPos(self, point_ref: Any) -> int:
        point_ref._obj.x, point_ref._obj.y = self.cursor
        return 1

    def GetSystemMetrics(self, index: int) -> int:
        return METRICS[index]

    def SendInput(self, count: int, events: Any, _size: int) -> int:
        batch = []
        for index in range(count):
            event = events[index]
            if event.type == 0:
                batch.append(
                    {
                        "kind": 0,
                        "flags": int(event.union.mi.dwFlags),
                        "dx": int(event.union.mi.dx),
                        "dy": int(event.union.mi.dy),
                    }
                )
            else:
                batch.append(
                    {
                        "kind": 1,
                        "flags": int(event.union.ki.dwFlags),
                        "vk": int(event.union.ki.wVk),
                    }
                )
        self.batches.append((time.perf_counter(), batch))
        return count


def _to_pixel(dx: int, dy: int) -> tuple[int, int]:
    return (
        backend_module._from_absolute_65535(dx, 0, 1920),
        backend_module._from_absolute_65535(dy, 0, 1080),
    )


def _decode_stream(fake: _StreamFakeUser32) -> list[tuple[str, tuple[int, int] | None]]:
    """Flatten every batch into an ordered (label, point) event stream."""
    stream: list[tuple[str, tuple[int, int] | None]] = []
    for _timestamp, batch in fake.batches:
        for event in batch:
            if event["kind"] != 0:
                continue  # keyboard events never appear in a drag
            if event["flags"] & backend_module._MOUSEEVENTF_MOVE:
                stream.append(("move", _to_pixel(event["dx"], event["dy"])))
            elif event["flags"] & backend_module._MOUSEEVENTF_LEFTDOWN:
                stream.append(("down", None))
            elif event["flags"] & backend_module._MOUSEEVENTF_LEFTUP:
                stream.append(("up", None))
    return stream


def _drag_action(
    start: tuple[int, int], end: tuple[int, int], **kwargs: Any
) -> GroundedAction:
    kwargs.setdefault("confidence", 1.0)
    return GroundedAction(
        action="drag",
        point={"x": start[0], "y": start[1]},
        to_point={"x": end[0], "y": end[1]},
        **kwargs,
    )


@pytest.fixture
def stubbed_user32(monkeypatch: pytest.MonkeyPatch) -> _StreamFakeUser32:
    fake = _StreamFakeUser32()
    monkeypatch.setattr(backend_module, "_user32", fake)
    return fake


def _factory_engine() -> SendInputEngine:
    """The engine the backend factory builds for live sessions (sendinput default).

    Mirrors ``_build_input_engine``'s sendinput branch verbatim (no env set) so
    these pins track the LIVE default, not the frozen bare-engine contract.
    """
    return SendInputEngine(
        drag_interpolate=True,
        drag_step_pause=backend_module._SENDINPUT_FACTORY_DRAG_STEP_PAUSE,
        drag_step_pixels=backend_module._SENDINPUT_FACTORY_DRAG_STEP_PIXELS,
    )


def _factory_default_engine() -> SendInputEngine:
    """Drive the REAL factory (sendinput branch, no env overrides)."""
    engine = LocalComputerBackend._build_input_engine(
        None, None, None, None, None  # type: ignore[arg-type]
    )
    assert isinstance(engine, SendInputEngine)
    return engine


# --- (a) down at start + up at end, moves between them only ----------------------------------


@WINDOWS_ONLY
def test_drag_stream_down_first_up_last_moves_between(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_user32: _StreamFakeUser32,
) -> None:
    """The button goes down at the START point and up only after the full stroke."""
    monkeypatch.setattr(real_backend, "_engine", _factory_engine())
    monkeypatch.setattr(real_backend, "_active_context", None)  # passthrough
    message = real_backend.execute(_drag_action(DRAG_START, DRAG_END))
    assert message == "Executed drag."

    stream = _decode_stream(stubbed_user32)
    labels = [label for label, _ in stream]
    assert labels[0] == "move" and labels[1] == "down"  # place, then press
    assert labels[-1] == "up"  # release exactly once, last
    assert labels.count("down") == 1
    assert labels.count("up") == 1
    # every stroke move happens with the button HELD: after down, before up
    first_up = labels.index("up")
    stroke_moves = [point for label, point in stream[labels.index("down") + 1 : first_up]]
    assert all(label == "move" for label, _ in stream[labels.index("down") + 1 : first_up])
    assert stream[0][1] == DRAG_START  # down happens at the start point
    assert stroke_moves[-1] == DRAG_END  # stroke ends exactly at the end point


# --- (b) >= N intermediate held-button moves ---------------------------------------------------


@WINDOWS_ONLY
def test_drag_stream_has_dense_intermediate_moves(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_user32: _StreamFakeUser32,
) -> None:
    """A 400 px stroke carries >= ceil(400 / 8 px) = 50 intermediate moves.

    This is THE anti-dot pin: with the old teleport default the same drag
    produced exactly ONE intermediate move (the r4 artifact's profile A).
    """
    monkeypatch.setattr(real_backend, "_engine", _factory_engine())
    monkeypatch.setattr(real_backend, "_active_context", None)
    real_backend.execute(_drag_action(DRAG_START, DRAG_END))

    stream = _decode_stream(stubbed_user32)
    held_moves = [
        point for index, (label, point) in enumerate(stream)
        if label == "move" and index > 1 and index < len(stream) - 1
    ]
    assert len(held_moves) >= EXPECTED_MIN_MOVES
    assert len(held_moves) > 1  # never a single teleport move again


# --- (c) monotonic toward the target, sane deltas ----------------------------------------------


@WINDOWS_ONLY
def test_drag_stream_monotonic_with_sane_deltas(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_user32: _StreamFakeUser32,
) -> None:
    """Each held-button step advances toward the target by <= one segment (~8 px)."""
    monkeypatch.setattr(real_backend, "_engine", _factory_engine())
    monkeypatch.setattr(real_backend, "_active_context", None)
    real_backend.execute(_drag_action(DRAG_START, DRAG_END))

    stream = _decode_stream(stubbed_user32)
    points = [point for label, point in stream if label == "move"]
    # progress along the stroke axis (x here) must never backtrack and must land
    progress = [point[0] for point in points]
    assert all(right >= left for left, right in zip(progress, progress[1:]))
    # per-step Chebyshev delta bounded by the segment size + rounding slack
    max_delta = max(
        max(abs(b[0] - a[0]), abs(b[1] - a[1])) for a, b in zip(points, points[1:])
    )
    assert max_delta <= backend_module._SENDINPUT_FACTORY_DRAG_STEP_PIXELS + 1


# --- (d) move count bounded (no floods) ---------------------------------------------------------


def test_drag_waypoint_count_bounded_no_flood() -> None:
    """Even a full-screen 1920 px diagonal stays far below flood territory.

    1920 px at 8 px steps = 240 moves; the bound allows only the geometry the
    step math predicts (+ the floor-4 minimum), so a bug that dropped the
    step limit (e.g. 1 px steps -> 1920 moves) fails this pin.
    """
    dense = _drag_segment_points((0, 0), (1919, 1079), step_pixels=8)
    expected = max(
        backend_module._DRAG_MIN_SEGMENTS,
        -(-max(1919, 1079) // 8),
    )
    assert len(dense) == expected
    assert len(dense) <= 300  # hard flood ceiling for a full-screen stroke
    # legacy geometry unchanged (pinned in test_drag_action.py, guarded here)
    legacy = _drag_segment_points((10, 10), (200, 200))
    assert len(legacy) == 5


def test_drag_waypoints_end_exactly_at_target() -> None:
    """The final waypoint is exactly the end point (dense and legacy modes)."""
    dense = _drag_segment_points((600, 400), (1000, 477), step_pixels=8)
    legacy = _drag_segment_points((600, 400), (1000, 477))
    assert dense[-1] == (1000, 477)
    assert legacy[-1] == (1000, 477)


# --- (e) elapsed time bounded (speed pin) -------------------------------------------------------


@WINDOWS_ONLY
def test_drag_stream_completes_within_speed_budget(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_user32: _StreamFakeUser32,
) -> None:
    """A 400 px stroke synthesizes in well under ~150 ms, pauses included.

    Default pacing is 1 ms per segment; 50 segments on this box sleep ~1.65 ms
    each (Windows timer granularity), so the expected wall time is ~85 ms —
    the pin allows generous headroom up to the contract's 150 ms budget.
    """
    monkeypatch.setattr(real_backend, "_engine", _factory_engine())
    monkeypatch.setattr(real_backend, "_active_context", None)
    start = time.perf_counter()
    real_backend.execute(_drag_action(DRAG_START, DRAG_END))
    elapsed = time.perf_counter() - start
    assert elapsed < SPEED_BUDGET_SECONDS


# --- (f) zero-length drag: press + release only, no crash ----------------------------------------


@WINDOWS_ONLY
def test_zero_length_drag_is_press_release_only(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_user32: _StreamFakeUser32,
) -> None:
    """x==x2 and y==y2: down + up at the spot, zero intermediate moves, no crash."""
    monkeypatch.setattr(real_backend, "_engine", _factory_engine())
    monkeypatch.setattr(real_backend, "_active_context", None)
    message = real_backend.execute(_drag_action(DRAG_START, DRAG_START))
    assert message == "Executed drag."

    stream = _decode_stream(stubbed_user32)
    labels = [label for label, _ in stream]
    assert labels == ["move", "down", "up"]  # place, press, release — nothing between
    assert stubbed_user32.batches, "the press/release dispatched"


# --- knob pins: the escape hatches stay honest ---------------------------------------------------


@WINDOWS_ONLY
def test_cortex_drag_interpolate_zero_restores_teleport(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    stubbed_user32: _StreamFakeUser32,
) -> None:
    """CORTEX_DRAG_INTERPOLATE=0 rebuilds the minimal teleport stroke (escape hatch)."""
    monkeypatch.setenv(backend_module.DRAG_INTERPOLATE_ENV, "0")
    engine = _factory_default_engine()
    assert engine.drag_interpolate is False
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    real_backend.execute(_drag_action(DRAG_START, DRAG_END))
    stream = _decode_stream(stubbed_user32)
    labels = [label for label, _ in stream]
    assert labels == ["move", "down", "move", "up"]  # the documented minimal profile


@WINDOWS_ONLY
def test_factory_builds_dense_interpolated_default_engine() -> None:
    """The factory (the LIVE path) wires interpolate + 1 ms pacing + 8 px steps —
    while the BARE engine contract stays frozen at the legacy minimal defaults."""
    engine = _factory_default_engine()
    assert engine.drag_interpolate is True
    assert engine.drag_step_pause == pytest.approx(0.001)
    assert engine.drag_step_pixels == 8

    bare = SendInputEngine()
    assert bare.drag_interpolate is False  # pinned contract, unchanged (test_input_engines)
    assert bare.drag_step_pause == 0.0
    assert bare.drag_step_pixels == 0


def test_env_step_pixels_garbage_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garbage/zero/negative CORTEX_DRAG_STEP_PIXELS falls back to the 8 px default."""
    for garbage in ("0", "-5", "abc", ""):
        monkeypatch.setenv(backend_module.DRAG_STEP_PIXELS_ENV, garbage)
        engine = _factory_default_engine()
        assert getattr(engine, "drag_step_pixels", None) == 8, garbage
    monkeypatch.setenv(backend_module.DRAG_STEP_PIXELS_ENV, "16")
    engine = _factory_default_engine()
    assert engine.drag_step_pixels == 16


def test_recording_engine_strokes_with_engine_density(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any engine carrying drag_step_pixels strokes at ITS granularity (the
    backend reads the attribute, absent => legacy ~40 px — stubs unaffected)."""
    engine = RecordingEngine(drag_interpolate=True)
    engine.drag_step_pixels = 8
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    real_backend.execute(_drag_action(DRAG_START, DRAG_END))
    moves = [call for call in engine.calls if call[0] == "move"]
    held = moves[1:]  # first move is the pre-press placement
    assert len(held) >= EXPECTED_MIN_MOVES
    assert held[-1] == ("move", *DRAG_END)
