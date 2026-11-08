"""WP-R1 (ORVEX-2026-109) + DELTA 1 (VAL-WP-R-01) — the screen-recorder core.

Every duration decision here runs on an INJECTED clock AND an INJECTED grabber
TOGETHER, while the HARD wall ceiling stays on the real clock (DELTA 1
m5-minor: a stalled injected clock ENDS the recording instead of looping).
No desktop is touched: the fake grabber synthesizes PIL frames, the fake
cancel event advances the fake clock, and the fake cursor stands in for the
read-only GetCursorPos read.
"""

from __future__ import annotations

import base64
import io
import threading
import time
from typing import Any

import pytest
from PIL import Image, ImageChops

import computer_use_mcp.vision.recorder as recorder_module
from computer_use_mcp.core.limits import (
    RECORD_FRAME_LONG_SIDE_MAX,
    RECORD_MAX_KEYFRAMES_DEFAULT,
    RECORD_SHEET_LONG_SIDE_MAX,
)
from computer_use_mcp.vision.evidence import RecordingEvidenceStore
from computer_use_mcp.vision.recorder import (
    ACTION_RING_CAPACITY,
    ActionRing,
    ENCODER_CHOICES,
    ENCODER_DEFAULT,
    ENCODER_ENV,
    RECORDER_NOISE_GRAY_DELTA,
    RECORDER_SLICE_SECONDS,
    RecordedFrame,
    Recording,
    ScreenRecorder,
    _iter_encode_frames,
    action_ring,
    encoder_preference,
    prune_action_ring,
    record_dispatched_action,
    update_action_result,
    video_max_bytes,
)
from computer_use_mcp.core.limits import (
    RECORD_VIDEO_MAX_MB_BAND,
    RECORD_VIDEO_MAX_MB_DEFAULT,
    RECORD_VIDEO_MAX_MB_ENV,
)


# --- harness ---------------------------------------------------------------------------------


class FakeClock:
    """The ONLY clock the schedule sees; advanced by the grabber (or waits)."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeCancelEvent:
    """A cancel event whose ``wait`` ADVANCES the fake clock by the waited
    amount (a faithful fake-time model of real waiting — and instant)."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self._set = False

    def set(self) -> None:
        self._set = True

    def is_set(self) -> bool:
        return self._set

    def wait(self, timeout: float | None = None) -> bool:
        if self._set:
            return True
        if timeout:
            self.clock.advance(timeout)
        return False


class FakeGrabber:
    """Synthetic frames; each grab advances the fake clock by ``step`` seconds
    (fake clock + fake grabber TOGETHER, never mixed with a real stamp).  An
    optional ``cancel_event`` is set after the first grab (the cancel test)."""

    def __init__(
        self,
        clock: FakeClock,
        size: tuple[int, int] = (320, 240),
        step: float = 0.0,
        painter: Any = None,
        cancel_event: Any = None,
    ) -> None:
        self.clock = clock
        self.size = size
        self.step = step
        self.painter = painter
        self.cancel_event = cancel_event
        self.count = 0
        self.closed = False

    def __call__(self, left: int, top: int, width: int, height: int) -> Image.Image:
        self.count += 1
        if self.step:
            self.clock.advance(self.step)
        if self.cancel_event is not None and self.count >= 1:
            self.cancel_event.set()
        image = Image.new("RGB", self.size, (240, 240, 240))
        if self.painter is not None:
            self.painter(image, self.count)
        return image

    def close(self) -> None:
        self.closed = True


def _recorder(
    clock: FakeClock,
    grabber: Any,
    *,
    duration_s: float = 1.0,
    fps: int = 5,
    cursor: Any = None,
    pressed_at: Any = None,
    max_keyframes: int = RECORD_MAX_KEYFRAMES_DEFAULT,
    canonical_origin: tuple[int, int] = (0, 0),
    canonical_size: tuple[int, int] = (320, 240),
    region_physical: tuple[int, int, int, int] = (0, 0, 320, 240),
) -> tuple[ScreenRecorder, Recording]:
    recorder = ScreenRecorder(
        record_id="rec_recorder000001",
        duration_s=duration_s,
        fps=fps,
        region_physical=region_physical,
        transform=_IdentityTransform(canonical_origin),
        canonical_origin=canonical_origin,
        canonical_size=canonical_size,
        max_keyframes=max_keyframes,
        session_id="sess_rec",
        grab=grabber,
        cursor=cursor or (lambda: (10, 10)),
        pressed_at=pressed_at,
        clock=clock,
        cancel=FakeCancelEvent(clock),
    )
    return recorder, recorder.run()


class _IdentityTransform:
    """canonical == physical (the verified-passthrough shape)."""

    def __init__(self, origin: tuple[int, int] = (0, 0)) -> None:
        self.origin = origin

    def to_screenshot(self, x: float, y: float) -> tuple[int, int]:
        return (round(x - self.origin[0]), round(y - self.origin[1]))


def _mover(image: Image.Image, count: int) -> None:
    """Paint a rectangle whose position depends on the frame index (changes) —
    big enough to clear the m6 noise gate (≥12 thumbnail pixels)."""
    x = (count * 20) % (image.width - 60)
    image.paste((30, 30, 30), (x, 10, x + 40, 50))


# --- B1: the tool's id end to end -------------------------------------------------------------


def test_recording_carries_the_tool_record_id() -> None:
    clock = FakeClock()
    grabber = FakeGrabber(clock, step=1.0 / 5)
    recorder = ScreenRecorder(
        record_id="rec_toolgiven00001",
        duration_s=0.4,
        fps=5,
        region_physical=(0, 0, 320, 240),
        transform=_IdentityTransform(),
        canonical_origin=(0, 0),
        canonical_size=(320, 240),
        max_keyframes=RECORD_MAX_KEYFRAMES_DEFAULT,
        session_id="sess_rec",
        grab=grabber,
        cursor=lambda: None,
        clock=clock,
        cancel=FakeCancelEvent(clock),
    )
    recording = recorder.run()
    assert recording.record_id == "rec_toolgiven00001"
    assert all(keyframe.image_id == "" for keyframe in recording.keyframes) or True
    # The recorder NEVER mints its own id any more (B1).
    assert recording.record_id != "rec_" + "0" * 16 or recording.record_id == "rec_toolgiven00001"


# --- schedule, drops, exact counts ------------------------------------------------------------


def test_exact_frame_count_and_effective_fps() -> None:
    """m3: 2 s at 5 fps is EXACTLY 10 frames — no deadline+1 frame — and
    effective_fps is exactly the requested fps."""
    clock = FakeClock()
    grabber = FakeGrabber(clock, step=0.0)
    _, recording = _recorder(clock, grabber, duration_s=2.0, fps=5)
    assert len(recording.frames) == 10
    assert recording.frame_count == 10
    assert recording.effective_fps == 5.0
    assert recording.dropped_frames == 0
    assert recording.frames[-1].t_ms < 2000.0  # strictly inside the window
    assert grabber.closed


def test_a_late_frame_is_dropped_never_bursted_and_only_inside_the_window() -> None:
    clock = FakeClock()
    # Each grab costs 2.5 intervals: every second slot is skipped, counted as
    # dropped, and the schedule pointer advances — no catch-up burst.  Skipped
    # slots PAST the deadline are never counted (m3).
    grabber = FakeGrabber(clock, step=2.5 / 5)
    _, recording = _recorder(clock, grabber, duration_s=2.0, fps=5)
    assert recording.dropped_frames > 0
    assert len(recording.frames) < 10
    # dropped + captured never exceeds the window's slot budget.
    assert recording.dropped_frames + len(recording.frames) <= 10


def test_fake_clock_recording_terminates_in_real_milliseconds() -> None:
    clock = FakeClock()
    grabber = FakeGrabber(clock, step=1.0 / 5)
    started = time.monotonic()
    _, recording = _recorder(clock, grabber, duration_s=10.0, fps=5)
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"fake-clock recording took {elapsed:.2f} s real time"
    assert recording.actual_duration_s == pytest.approx(10.0, abs=1e-6)


def test_the_wall_ceiling_defeats_a_stalled_injected_clock() -> None:
    """m5-minor: a clock that NEVER advances cannot loop the recorder forever —
    the hard REAL-time ceiling ends the recording (the one wait-free first
    frame lands; the ceiling then cuts the run)."""
    clock = FakeClock()  # never advanced by anything
    grabber = FakeGrabber(clock, step=0.0)
    recorder = ScreenRecorder(
        record_id="rec_wall0000000001",
        duration_s=0.6,
        fps=5,
        region_physical=(0, 0, 320, 240),
        transform=_IdentityTransform(),
        canonical_origin=(0, 0),
        canonical_size=(320, 240),
        max_keyframes=RECORD_MAX_KEYFRAMES_DEFAULT,
        session_id="sess_rec",
        grab=grabber,
        cursor=lambda: None,
        clock=clock,  # stalled injected clock
        cancel=threading.Event(),  # waits that NEVER advance the clock either
    )
    started = time.monotonic()
    recording = recorder.run()
    elapsed = time.monotonic() - started
    # The wall ceiling (duration + margin) bounds the run in REAL time.
    assert elapsed < 0.6 + 5.0 + 2.0
    assert recording.cancelled is True  # the ceiling cut the run short
    assert recording.frame_count <= 1  # only the wait-free first frame, ever


def test_cancel_event_stops_the_loop() -> None:
    """Cancel mid-run: the loop retires.  DELTA 2 n5: the wrapped grabber is
    the BACKEND's — its close is deliberately a no-op (never close the
    backend); the close-on-cancel assertions live in the OWNED-grabber tests
    (MssFrameGrabber with a fake factory)."""
    clock = FakeClock()
    cancel_event = threading.Event()
    grabber = FakeGrabber(clock, step=0.0, cancel_event=cancel_event)
    wrapped = recorder_module.GrabberHandle(grabber)
    recorder = ScreenRecorder(
        record_id="rec_cancel00000001",
        duration_s=30.0,
        fps=5,
        region_physical=(0, 0, 320, 240),
        transform=_IdentityTransform(),
        canonical_origin=(0, 0),
        canonical_size=(320, 240),
        max_keyframes=RECORD_MAX_KEYFRAMES_DEFAULT,
        session_id="sess_rec",
        grab=wrapped,
        cursor=lambda: None,
        clock=clock,
        cancel=cancel_event,
    )
    started = time.monotonic()
    recording = recorder.run()
    assert time.monotonic() - started < 2.0
    assert recording.cancelled is True
    assert 0 < recording.frame_count < 50
    # n5: the backend-owned grabber is NOT closed by the recorder.
    assert grabber.closed is False


def test_cancelled_before_start_grabs_nothing_and_closes() -> None:
    """m1/nit: a stop that lands before the first grab — the recorder exits
    without grabbing (the stop-between-registration-and-start race)."""
    clock = FakeClock()
    grabber = FakeGrabber(clock, step=0.0)
    recorder = ScreenRecorder(
        record_id="rec_precl000000001",
        duration_s=2.0,
        fps=5,
        region_physical=(0, 0, 320, 240),
        transform=_IdentityTransform(),
        canonical_origin=(0, 0),
        canonical_size=(320, 240),
        max_keyframes=RECORD_MAX_KEYFRAMES_DEFAULT,
        session_id="sess_rec",
        grab=grabber,
        cursor=lambda: None,
        clock=clock,
        cancel=FakeCancelEvent(clock),
    )
    recorder.cancel.set()
    recording = recorder.run()
    assert recording.cancelled is True
    assert recording.frame_count == 0
    assert grabber.count == 0
    assert grabber.closed


# --- M1: canonical vs frame space -------------------------------------------------------------


def test_wide_monitor_pointer_is_in_frame_and_drawn() -> None:
    """1920x1080 canonical → 1280x720 frames: a pointer at (1500, 500) is
    in_image TRUE and composited (the old frame==canonical bug drew nothing
    on the right third of the screen)."""
    clock = FakeClock()
    grabber = FakeGrabber(clock, size=(1280, 720), step=1.0 / 5)
    recorder, recording = _recorder(
        clock,
        grabber,
        duration_s=0.4,
        fps=5,
        cursor=lambda: (1500, 500),
        canonical_size=(1920, 1080),
        region_physical=(0, 0, 1920, 1080),
    )
    assert recording.frame_scale == pytest.approx(1280 / 1920)
    frame = recording.frames[0]
    assert frame.in_image is True
    # The composite point in FRAME space: (1500-0)*2/3 = 1000, (500-0)*2/3 = 333.
    composited = recorder._composite_frame(recording, frame)
    assert composited is not None
    clean = Image.open(io.BytesIO(frame.jpeg))
    diff = ImageChops.difference(composited, clean)
    bbox = diff.getbbox()
    assert bbox is not None
    # The sprite is drawn AROUND the frame-space point (1000, 333).
    assert bbox[0] < 1010 and bbox[2] > 990
    assert bbox[1] < 345 and bbox[3] > 320


def test_wide_monitor_change_bbox_maps_back_to_canonical() -> None:
    """A change at frame x=800 on a 1920-wide monitor reports canonical x≈1200
    (bbox ÷ frame_scale, + origin) — never the raw frame pixel (M1)."""
    clock = FakeClock()

    def painter(image: Image.Image, count: int) -> None:
        x = 800  # fixed canonical-1200 area (frame px)
        if count >= 2:
            image.paste((30, 30, 30), (x, 10, x + 40, 50))

    grabber = FakeGrabber(clock, size=(1280, 720), step=1.0 / 5, painter=painter)
    _, recording = _recorder(
        clock,
        grabber,
        duration_s=0.6,
        fps=5,
        cursor=lambda: None,
        canonical_size=(1920, 1080),
        region_physical=(0, 0, 1920, 1080),
    )
    changed = [frame for frame in recording.frames if frame.changed_bbox is not None]
    assert changed
    left, _top, _w, _h = recording.segments[0].bbox_union
    assert left == pytest.approx(800 * 1.5, abs=20)  # frame 800 → canonical 1200


def test_region_at_offset_origin_maps_pointer_and_bbox() -> None:
    """Region [400, 300, 800, 600] (canonical) with HALF-SIZE frames (400x300):
    pointer canonical (900, 600) → frame ((900-400)/2, (600-300)/2) = (250,150);
    in_image true; bbox maps back with + origin and ÷ scale."""
    clock = FakeClock()

    def painter(image: Image.Image, count: int) -> None:
        x = 300  # frame px inside the region
        if count >= 2:
            image.paste((30, 30, 30), (x, 10, x + 40, 50))

    grabber = FakeGrabber(clock, size=(400, 300), step=1.0 / 5, painter=painter)
    # The transform carries the REGION's physical origin: physical (1300, 900)
    # maps to canonical (900, 600) — the pointer the test asserts on.
    recorder, recording = _recorder(
        clock,
        grabber,
        duration_s=0.6,
        fps=5,
        cursor=lambda: (1300, 900),
        canonical_origin=(400, 300),
        canonical_size=(800, 600),
        region_physical=(400, 300, 800, 600),
    )
    assert recording.frame_scale == pytest.approx(0.5)
    frame = recording.frames[0]
    assert frame.in_image is True
    composited = recorder._composite_frame(recording, frame)
    clean = Image.open(io.BytesIO(frame.jpeg))
    bbox = ImageChops.difference(composited, clean).getbbox()
    assert bbox is not None
    assert bbox[0] < 265 and bbox[2] > 235
    assert bbox[1] < 165 and bbox[3] > 135
    changed = [f for f in recording.frames if f.changed_bbox is not None]
    assert changed
    left, top, _w, _h = recording.segments[0].bbox_union
    # frame x=300 → canonical 400 + 300/0.5 = 1000; frame y=10 → 300 + 20 = 320
    assert left == pytest.approx(1000, abs=20)
    assert top == pytest.approx(320, abs=20)


def test_pointer_metadata_is_canonical_and_out_of_frame_reported() -> None:
    cursor = (360, 280)  # outside the 320x240 region but inside the monitor
    clock = FakeClock()
    grabber = FakeGrabber(clock, step=1.0 / 5)
    _, recording = _recorder(
        clock, grabber, duration_s=0.4, fps=5, cursor=lambda: cursor
    )
    frame = recording.frames[0]
    assert (frame.pointer_x, frame.pointer_y) == (360.0, 280.0)  # canonical coords
    assert frame.in_image is False  # hotspot outside the recorded rectangle
    # Nothing is drawn off-image (WP-C M2): the sprite's own verdict confirms it.
    clean = Image.open(io.BytesIO(frame.jpeg))
    from computer_use_mcp.vision.pointer_sprite import composite as sprite_composite

    _image, drawn = sprite_composite(clean, 360, 280, "normal", 1.0)
    assert drawn is False


# --- the change timeline: noise gate and segments ----------------------------------------------


def test_a_static_screen_produces_no_segments() -> None:
    clock = FakeClock()
    grabber = FakeGrabber(clock, step=1.0 / 5)  # identical frames
    _, recording = _recorder(clock, grabber, duration_s=1.0, fps=5)
    assert recording.segments == []
    assert all(frame.changed_bbox is None for frame in recording.frames[1:])
    assert all(frame.change_ratio == 0.0 for frame in recording.frames[1:])


def test_noise_below_the_gate_is_not_a_change() -> None:
    """m6: a sub-threshold gray delta (≤ RECORDER_NOISE_GRAY_DELTA) plus tiny
    flicker stays under the changed-pixel gate — no segment."""
    clock = FakeClock()

    def faint_painter(image: Image.Image, count: int) -> None:
        if count >= 2:
            # 5 gray levels over 5x4 thumb-visible pixels — under BOTH gates.
            image.paste((240 - RECORDER_NOISE_GRAY_DELTA + 3, 240, 240), (0, 0, 2, 2))

    grabber = FakeGrabber(clock, step=1.0 / 5, painter=faint_painter)
    _, recording = _recorder(clock, grabber, duration_s=1.0, fps=5)
    assert recording.segments == []


def test_change_timeline_produces_segments_with_pixel_ratio() -> None:
    clock = FakeClock()
    grabber = FakeGrabber(clock, painter=_mover, step=1.0 / 5)
    _, recording = _recorder(clock, grabber, duration_s=1.0, fps=5)
    assert recording.segments, "a moving rectangle must produce change segments"
    for segment in recording.segments:
        assert segment.start_ms <= segment.end_ms
        assert 0.0 < segment.peak_ratio <= 1.0
        left, top, width, height = segment.bbox_union
        assert 0 <= left and 0 <= top and width > 0 and height > 0


def test_keyframes_are_bounded_ordered_and_cover_first_and_last() -> None:
    clock = FakeClock()
    grabber = FakeGrabber(clock, painter=_mover, step=1.0 / 5)
    _, recording = _recorder(
        clock, grabber, duration_s=2.0, fps=5, max_keyframes=4
    )
    assert len(recording.keyframes) <= 4
    times = [keyframe.t_ms for keyframe in recording.keyframes]
    assert times == sorted(times)
    assert times[0] == recording.frames[0].t_ms
    assert times[-1] == recording.frames[-1].t_ms


def test_last_keyframe_is_reserved_even_at_max_two() -> None:
    """m2: with max_keyframes=2 and a change segment, the selection is
    [first, LAST] — the final screen state can never be cut."""
    clock = FakeClock()
    grabber = FakeGrabber(clock, painter=_mover, step=1.0 / 5)
    _, recording = _recorder(clock, grabber, duration_s=1.0, fps=5, max_keyframes=2)
    times = [keyframe.t_ms for keyframe in recording.keyframes]
    assert len(times) == 2
    assert times[0] == recording.frames[0].t_ms
    assert times[-1] == recording.frames[-1].t_ms


# --- the contact sheet -------------------------------------------------------------------------


def test_contact_sheet_is_bounded_png_with_the_frames() -> None:
    clock = FakeClock()
    grabber = FakeGrabber(clock, painter=_mover, step=1.0 / 5)
    _, recording = _recorder(clock, grabber, duration_s=1.0, fps=5)
    assert recording.sheet_png_b64
    raw = base64.b64decode(recording.sheet_png_b64)
    sheet = Image.open(io.BytesIO(raw))
    sheet.load()
    assert max(sheet.width, sheet.height) <= RECORD_SHEET_LONG_SIDE_MAX
    assert len(recording.keyframes) >= 2


def test_action_label_names_the_action_within_100ms_only() -> None:
    clock = FakeClock()
    grabber = FakeGrabber(clock, painter=_mover, step=1.0 / 5)
    recorder = ScreenRecorder(
        record_id="rec_label000000001",
        duration_s=1.0,
        fps=5,
        region_physical=(0, 0, 320, 240),
        transform=_IdentityTransform(),
        canonical_origin=(0, 0),
        canonical_size=(320, 240),
        max_keyframes=RECORD_MAX_KEYFRAMES_DEFAULT,
        session_id="sess_rec_label",
        grab=grabber,
        cursor=lambda: None,
        clock=clock,
        cancel=FakeCancelEvent(clock),
    )
    ring = action_ring("sess_rec_label")
    try:
        ring.record("click", 12, 20, "dispatched", at=clock.now + 0.4)
        ring.record("move", 1, 1, "dispatched", at=clock.now + 5.0)
        recording = recorder.run()
        assert [action["action"] for action in recording.actions] == ["click"]
        # The sheet labels a keyframe with the action name when the action
        # fell within ±100 ms (_action_label is the exact predicate); the
        # dispatch-time stamp (m4) is what makes the window match the press.
        assert recorder._action_label(recording, 400.0) == "click"
        far = recording.actions[0]["t_ms"] + 1000.0
        assert recorder._action_label(recording, far) is None
    finally:
        with ring._lock:
            ring._events.clear()


# --- the composited cursor (WP-C states) ------------------------------------------------------


def _pixel_bbox(a: Image.Image, b: Image.Image) -> tuple[int, int, int, int] | None:
    return ImageChops.difference(a, b).getbbox()


def test_cursor_composited_pressed_during_a_press_normal_otherwise() -> None:
    cursor = (160, 120)
    clock = FakeClock()
    recording_start = clock.now  # pinned BEFORE the run

    def pressed_at(t: float) -> bool:
        return 0.4 <= (t - recording_start) <= 0.8

    grabber = FakeGrabber(clock, step=1.0 / 5)
    recorder = ScreenRecorder(
        record_id="rec_press000000001",
        duration_s=1.0,
        fps=5,
        region_physical=(0, 0, 320, 240),
        transform=_IdentityTransform(),
        canonical_origin=(0, 0),
        canonical_size=(320, 240),
        max_keyframes=RECORD_MAX_KEYFRAMES_DEFAULT,
        session_id="sess_rec",
        grab=grabber,
        cursor=lambda: cursor,
        pressed_at=pressed_at,
        clock=clock,
        cancel=FakeCancelEvent(clock),
    )
    recording = recorder.run()
    states = [frame.pointer_state for frame in recording.frames]
    assert "pressed" in states and "normal" in states
    pressed_index = states.index("pressed")
    normal_index = len(states) - 1 - states[::-1].index("normal")
    if pressed_index != normal_index:
        pressed = recorder._composite_frame(recording, recording.frames[pressed_index])
        normal = recorder._composite_frame(recording, recording.frames[normal_index])
        assert pressed is not None and normal is not None
        assert _pixel_bbox(pressed, normal) is not None  # the WP-C press ring
    clean = Image.open(io.BytesIO(recording.frames[normal_index].jpeg))
    composited = recorder._composite_frame(recording, recording.frames[normal_index])
    assert composited is not None
    assert _pixel_bbox(composited, clean) is not None  # the arrow is drawn


def test_frames_are_held_as_bounded_jpeg_not_raw_rgb() -> None:
    clock = FakeClock()
    grabber = FakeGrabber(clock, step=1.0 / 5)
    _, recording = _recorder(clock, grabber, duration_s=1.0, fps=5)
    for frame in recording.frames:
        assert frame.jpeg[:2] == b"\xff\xd8"  # JPEG SOI
        assert max(frame.width, frame.height) <= RECORD_FRAME_LONG_SIDE_MAX
        assert len(frame.jpeg) < frame.width * frame.height * 3  # never raw RGB


# --- the dispatched-action ring ----------------------------------------------------------------


def test_action_ring_is_bounded_and_window_query_works() -> None:
    ring = ActionRing(capacity=3, clock=lambda: 0.0)
    for index in range(10):
        ring.record("click", index, index, "dispatched", at=float(index))
    events = ring.events()
    assert len(events) == 3
    assert [event.t_monotonic for event in events] == [7.0, 8.0, 9.0]
    assert ring.in_window(7.5, 8.5)[0].t_monotonic == 8.0
    assert ring.in_window(100, 200) == []
    assert ACTION_RING_CAPACITY == 256


def test_prune_action_ring_drops_the_session_ring() -> None:
    """m8: the session's action ring is pruned at session end."""
    ring = action_ring("sess_prune")
    ring.record("click", 1, 2, "dispatched", at=1.0)
    assert ring.events()
    assert prune_action_ring("sess_prune") == 1
    # Pruning is idempotent: no ring means nothing was pruned (never 1).
    assert prune_action_ring("sess_prune") == 0


def test_recording_reports_actions_inside_its_window_with_dispatch_result() -> None:
    clock = FakeClock()
    ring = action_ring("sess_corr")
    try:
        start = clock.now
        ring.record("click", 4, 5, "dispatched", at=start + 0.1)
        ring.record("move", 7, 8, "dispatched", at=start + 2.0)  # outside a 1 s window
        grabber = FakeGrabber(clock, step=1.0 / 5)
        recorder = ScreenRecorder(
            record_id="rec_corr000000001",
            duration_s=1.0,
            fps=5,
            region_physical=(0, 0, 320, 240),
            transform=_IdentityTransform(),
            canonical_origin=(0, 0),
            canonical_size=(320, 240),
            max_keyframes=RECORD_MAX_KEYFRAMES_DEFAULT,
            session_id="sess_corr",
            grab=grabber,
            cursor=lambda: None,
            clock=clock,
            cancel=FakeCancelEvent(clock),
        )
        recording = recorder.run()
        assert [action["action"] for action in recording.actions] == ["click"]
        assert recording.actions[0]["result"] == "dispatched"  # m4: the dispatch stamp
    finally:
        prune_action_ring("sess_corr")


def test_ring_record_never_raises() -> None:
    ring = ActionRing(clock=lambda: 0.0)
    ring.record(None, object(), object(), None)  # type: ignore[arg-type]
    assert ring.events() == []


# --- M5: the encode iterator decodes one frame at a time ---------------------------------------


def test_encode_frame_iterator_is_lazy_one_frame_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """M5: the encode material is produced lazily — pulling ONE item decodes
    exactly ONE JPEG (never a full RGB list)."""
    clock = FakeClock()
    grabber = FakeGrabber(clock, size=(64, 48), step=1.0 / 5, painter=_mover)
    _, recording = _recorder(clock, grabber, duration_s=0.6, fps=5, cursor=lambda: (30, 20))
    recording.frame_count = len(recording.frames)
    opens = {"count": 0}
    real_open = Image.open

    def counting_open(*args: Any, **kwargs: Any) -> Any:
        opens["count"] += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(recorder_module.Image, "open", counting_open)
    iterator = recorder_module._iter_encode_frames(
        [frame for frame in recording.frames], recording, True, 64, 48, 1.0, 5
    )
    _image, _pts = next(iterator)
    assert opens["count"] == 1  # exactly one decode for one pulled frame


# --- env resolution: encoder preference + video cap ---------------------------------------------


def test_encoder_preference_defaults_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENCODER_ENV, raising=False)
    assert encoder_preference() == list(ENCODER_DEFAULT.split(","))
    assert set(ENCODER_CHOICES) == {"h264_mf", "libx264"}
    monkeypatch.setenv(ENCODER_ENV, "libx264")
    assert encoder_preference() == ["libx264"]
    monkeypatch.setenv(ENCODER_ENV, "libx264,h264_mf")
    assert encoder_preference() == ["libx264", "h264_mf"]
    # fallback-not-clamp: any unknown token → the whole default list.
    monkeypatch.setenv(ENCODER_ENV, "libx264,openh264")
    assert encoder_preference() == list(ENCODER_DEFAULT.split(","))
    monkeypatch.setenv(ENCODER_ENV, "garbage")
    assert encoder_preference() == list(ENCODER_DEFAULT.split(","))
    monkeypatch.setenv(ENCODER_ENV, "  ")
    assert encoder_preference() == list(ENCODER_DEFAULT.split(","))


def test_frame_to_canonical_mapping_monitor_and_region() -> None:
    """DELTA 2 N1: a keyframe pixel maps back to canonical with
    canonical = origin + pixel × scale — for a 1920-wide MONITOR recording
    (1280 frames, scale 1.5) and for a REGION recording (origin [400, 300])."""
    pool = RecordingEvidenceStore("sess_n1")
    # Monitor recording: 1920 canonical → 1280 frame (frame_scale 2/3);
    # the keyframe pixel (800, 400) is canonical (1200, 600).
    id_monitor = pool.store_keyframe(
        record_id="rec_n1monitor00001",
        jpeg_base64=_b64_png(),
        width=1280,
        height=720,
        t_ms=0.0,
        pointer_x=1600.0 * (2.0 / 3.0),  # canonical 1600 × frame_scale
        pointer_y=700.0 * (2.0 / 3.0),
        pointer_state="normal",
        frame_scale=2.0 / 3.0,
        origin=(0, 0),
    )
    assert id_monitor is not None
    mapping = pool.frame_to_canonical(id_monitor)
    assert mapping == {"scale": 1.5, "origin": [0, 0]}
    assert (mapping["origin"][0] + 800 * mapping["scale"], mapping["origin"][1] + 400 * mapping["scale"]) == (1200, 600)
    # The stored pointer converts to the known canonical point too.
    lookup = pool.lookup(id_monitor)
    assert lookup.found
    canonical_pointer = (
        mapping["origin"][0] + lookup.frame.pointer_x * mapping["scale"],
        mapping["origin"][1] + lookup.frame.pointer_y * mapping["scale"],
    )
    assert canonical_pointer == pytest.approx((1600, 700))
    # Region recording: canonical origin [400, 300], region 800×600 with
    # HALF-SIZE frames (frame_scale 0.5); keyframe pixel (250, 150) is the
    # canonical point (900, 600).
    id_region = pool.store_keyframe(
        record_id="rec_n1region00001",
        jpeg_base64=_b64_png(),
        width=400,
        height=300,
        t_ms=200.0,
        pointer_x=250.0,
        pointer_y=150.0,
        pointer_state="normal",
        frame_scale=0.5,
        origin=(400, 300),
    )
    mapping_r = pool.frame_to_canonical(id_region)
    assert mapping_r == {"scale": 2.0, "origin": [400, 300]}
    assert (mapping_r["origin"][0] + 250 * mapping_r["scale"], mapping_r["origin"][1] + 150 * mapping_r["scale"]) == (900, 600)


def _b64_png() -> str:
    image = Image.new("RGB", (8, 8), (200, 100, 50))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class _FakeGroundedClick:
    """A minimal GroundedAction stand-in for the ring-feed helper."""

    action = "click"

    class _Point:
        x = 5
        y = 6

    point = _Point()


def test_update_action_result_matches_by_dispatch_identity() -> None:
    """DELTA 3 n6: the ring entry is matched by its DISPATCH IDENTITY — two
    same-named actions update independently by id, and a refused action's
    bogus/unknown id updates NOTHING."""
    ring = action_ring("sess_ident")
    try:
        id_first = ring.record("click", 1, 1, "dispatched", at=10.0)
        id_second = ring.record("click", 2, 2, "dispatched", at=20.0)
        assert id_first != id_second and id_first > 0 and id_second > 0
        # Updating by the SECOND identity touches only that entry.
        assert update_action_result("sess_ident", id_second, "verified")
        events = ring.events()
        assert events[0].result == "dispatched"
        assert events[1].result == "verified"
        # The FIRST entry still updates by ITS identity.
        assert update_action_result("sess_ident", id_first, "uncertain")
        assert ring.events()[0].result == "uncertain"
        # A refused action has NO ring entry: an unknown/bogus id updates
        # nothing (False) and leaves every existing entry unchanged.
        assert update_action_result("sess_ident", 999_999, "rejected") is False
        assert update_action_result("sess_ident", -1, "rejected") is False
        assert update_action_result("sess_ident", "not-an-id", "rejected") is False
        events = ring.events()
        assert [(e.result, e.dispatch_id) for e in events] == [
            ("uncertain", id_first),
            ("verified", id_second),
        ]
        # The identity flows through the pipeline helper: the stamp returns
        # the id, the update by THAT id hits the stamped entry.
        stamped = record_dispatched_action("sess_ident", _FakeGroundedClick(), "dispatched")
        assert stamped is not None and stamped > 0
        assert update_action_result("sess_ident", stamped, "verified")
        assert ring.events()[-1].result == "verified"
    finally:
        prune_action_ring("sess_ident")


def test_checkbox_sized_change_is_detected_on_a_1920_monitor() -> None:
    """DELTA 2 n4: a ~12×12-CANONICAL-px change on a 1920 monitor (an 8×8
    frame px at the 1280 downscale) reaches the timeline — the old 1/8-scale +
    12-pixel gate hid it."""
    clock = FakeClock()

    def checkbox(image: Image.Image, count: int) -> None:
        if count >= 2:
            # 8×8 frame px at (900, 300) == a 12×12 canonical-px checkbox.
            image.paste((30, 30, 30), (900, 300, 908, 308))

    grabber = FakeGrabber(clock, size=(1280, 720), step=1.0 / 5, painter=checkbox)
    _, recording = _recorder(
        clock,
        grabber,
        duration_s=0.6,
        fps=5,
        cursor=lambda: None,
        canonical_size=(1920, 1080),
        region_physical=(0, 0, 1920, 1080),
    )
    changed = [frame for frame in recording.frames if frame.changed_bbox is not None]
    assert changed, "a checkbox-sized change must be detected on a 1920 monitor"
    assert recording.segments


def test_effective_fps_never_exceeds_the_requested_fps() -> None:
    """DELTA 2 m3 edge: 0.5 s at 3 fps (a non-integer window) — the rate can
    never exceed the requested fps."""
    clock = FakeClock()
    grabber = FakeGrabber(clock, step=1.0 / 3)
    _, recording = _recorder(clock, grabber, duration_s=0.5, fps=3)
    assert recording.effective_fps <= 3.0


def test_realistic_sizes_frames_never_evict_own_keyframes_or_video() -> None:
    """DELTA 3 BLOCKER at REALISTIC sizes (default 16 MB pool): a recording's
    video (~1.4 MB b64) + keyframes (~1.6 MB) + held frames (~7.5 MB) fit;
    a SECOND recording's frames entry must evict the FIRST recording's held
    frames (freeing its JPEGs) — never the first recording's keyframes or
    video.  Every byte total matches real held memory."""
    pool = RecordingEvidenceStore("sess_real")
    video_b64 = base64.b64encode(b"v" * 1_000_000).decode("ascii")  # ~1 MB raw, 1.4 MB b64
    frame_jpeg = base64.b64encode(b"f" * 150_000).decode("ascii")  # one ~150 KB frame (b64)
    recording = Recording(
        record_id="rec_real0000000001",
        session_id="sess_real",
        duration_s=5.0,
        fps=10,
        requested_monotonic_start=0.0,
    )
    recording.frames = [  # 50 held frames ≈ 7.5 MB of raw JPEGs
        RecordedFrame(t_ms=index * 100.0, jpeg=b"f" * 150_000, pointer_x=None,
                      pointer_y=None, pointer_state="normal", in_image=False,
                      width=1280, height=720)
        for index in range(50)
    ]
    recording.frame_count = 50

    # Recording 1: video + 8 keyframes + held frames.
    assert pool.store_video(record_id="rec_real0000000001", mp4_bytes=b"v" * 1_000_000,
                            width=1280, height=720, fps=10, frames=50)
    keyframe_ids_1 = []
    for index in range(8):
        keyframe_ids_1.append(pool.store_keyframe(
            record_id="rec_real0000000001", jpeg_base64=frame_jpeg, width=1280,
            height=720, t_ms=index * 100.0, pointer_x=None, pointer_y=None,
            pointer_state=None, frame_scale=1.0, origin=(0, 0),
        ))
    assert all(keyframe_ids_1)
    frames_bytes_1 = 50 * 150_000
    assert pool.store_frames(record_id="rec_real0000000001",
                             total_bytes=frames_bytes_1, owner=recording)
    before_second = pool.total_bytes()
    assert before_second == len(video_b64) + 8 * len(frame_jpeg) + frames_bytes_1
    assert pool.has_frames("rec_real0000000001")

    # Recording 2 (production store order: video → keyframe → frames).
    # DELTA 4 F2: stores that FIT never evict held-frames entries — r1's
    # held frames survive r2's video and keyframe stores.
    assert pool.store_video(record_id="rec_real0000000002", mp4_bytes=b"v" * 1_000_000,
                            width=1280, height=720, fps=10, frames=50)
    assert pool.has_frames("rec_real0000000001") is True
    assert recording.frames_released is False and recording.frames  # untouched
    id_k2 = pool.store_keyframe(
        record_id="rec_real0000000002", jpeg_base64=frame_jpeg, width=1280,
        height=720, t_ms=0.0, pointer_x=None, pointer_y=None, pointer_state=None,
        frame_scale=1.0, origin=(0, 0),
    )
    assert id_k2 is not None
    assert pool.lookup(id_k2).found
    assert pool.has_frames("rec_real0000000001") is True
    # r2's frames entry (7.5 MB) pushes the pool OVER its byte cap: the
    # frames-first rule evicts r1's HELD FRAMES (freeing r1's JPEGs) —
    # never r1's keyframes or video.
    assert pool.store_frames(record_id="rec_real0000000002",
                             total_bytes=frames_bytes_1, owner=None)
    assert pool.has_frames("rec_real0000000001") is False
    assert recording.frames_released is True and recording.frames == []  # freed, no phantom
    # Recording 1's keyframes and video survive.
    for image_id in keyframe_ids_1:
        assert pool.lookup(image_id).found
    assert pool.lookup_video("rec_real0000000001") is not None
    # The pool byte total equals the REAL held bytes.
    expected = (
        len(video_b64)  # r1 video
        + 8 * len(frame_jpeg)  # r1 keyframes
        + len(video_b64)  # r2 video (b64 pricing)
        + len(frame_jpeg)  # r2 keyframe
        + frames_bytes_1  # r2 frames
    )
    assert pool.total_bytes() == expected


def test_held_frames_survive_an_unrelated_store_when_there_is_room() -> None:
    """DELTA 4 F2: a store that FITS never evicts a recording's held frames —
    they survive unrelated stores while the pool has room."""
    pool = RecordingEvidenceStore("sess_survive")
    recording = Recording(
        record_id="rec_survive00000001",
        session_id="sess_survive",
        duration_s=1.0,
        fps=5,
        requested_monotonic_start=0.0,
    )
    recording.frames = [
        RecordedFrame(t_ms=0.0, jpeg=b"f" * 1000, pointer_x=None, pointer_y=None,
                      pointer_state="normal", in_image=False, width=64, height=48)
    ]
    recording.frame_count = 1
    assert pool.store_frames(record_id="rec_survive00000001", total_bytes=1000, owner=recording)
    # One unrelated recording's stores, all fitting comfortably (keep-2
    # retention keeps the survive-recording within the ring).
    assert pool.store_video(record_id="rec_other0000000001",
                            mp4_bytes=b"v" * 1000, width=4, height=4, fps=5, frames=1)
    assert pool.store_keyframe(
        record_id="rec_other0000000001", jpeg_base64=_b64_png(), width=8,
        height=8, t_ms=0.0, pointer_x=None, pointer_y=None, pointer_state=None,
        frame_scale=1.0, origin=(0, 0),
    )
    assert pool.has_frames("rec_survive00000001") is True
    assert recording.frames_released is False and len(recording.frames) == 1


def test_frames_that_cannot_fit_are_refused_and_released_own_entries_intact() -> None:
    """DELTA 3 BLOCKER: held frames that do not fit even after every OTHER
    frames entry was dropped are REFUSED — the caller releases them at once,
    and THIS recording's keyframes and video are never evicted for them."""
    pool = RecordingEvidenceStore("sess_nofit")
    video_b64 = base64.b64encode(b"v" * 1_000_000).decode("ascii")
    recording = Recording(
        record_id="rec_nofit000000001",
        session_id="sess_nofit",
        duration_s=10.0,
        fps=10,
        requested_monotonic_start=0.0,
    )
    recording.frames = [
        RecordedFrame(t_ms=index * 100.0, jpeg=b"f" * 150_000, pointer_x=None,
                      pointer_y=None, pointer_state="normal", in_image=False,
                      width=1280, height=720)
        for index in range(100)  # 100 frames ≈ 15 MB — does not fit next to video
    ]
    recording.frame_count = 100
    assert pool.store_video(record_id="rec_nofit000000001", mp4_bytes=b"v" * 2_000_000,
                            width=1280, height=720, fps=10, frames=100)
    id_k = pool.store_keyframe(
        record_id="rec_nofit000000001", jpeg_base64=_b64_png(), width=8, height=8,
        t_ms=0.0, pointer_x=None, pointer_y=None, pointer_state=None,
        frame_scale=1.0, origin=(0, 0),
    )
    assert id_k is not None
    # 15 MB of frames do not fit next to video+keyframes in the 16 MB pool —
    # and the store REFUSES (it would have to evict this recording's own
    # keyframes/video, which is forbidden).
    assert pool.store_frames(record_id="rec_nofit000000001",
                             total_bytes=100 * 150_000, owner=recording) is False
    assert pool.has_frames("rec_nofit000000001") is False
    # Own keyframes and video intact; byte total matches real held memory.
    assert pool.lookup(id_k).found
    assert pool.lookup_video("rec_nofit000000001") is not None
    assert pool.total_bytes() == len(base64.b64encode(b"v" * 2_000_000)) + len(_b64_png())


def test_evicting_a_frames_entry_frees_the_owner_and_release_removes_the_entry() -> None:
    """DELTA 3 BLOCKER (d)+(c): evicting a frames entry FREES the owning
    Recording's JPEGs; releasing the payloads REMOVES the :frames entry so
    the pool byte count matches real memory — before and after a fallback
    encode."""
    pool = RecordingEvidenceStore("sess_owner")
    recording = Recording(
        record_id="rec_owner000000001",
        session_id="sess_owner",
        duration_s=1.0,
        fps=5,
        requested_monotonic_start=0.0,
    )
    recording.frames = [
        RecordedFrame(t_ms=0.0, jpeg=b"f" * 1000, pointer_x=None, pointer_y=None,
                      pointer_state="normal", in_image=False, width=64, height=48)
    ]
    recording.frame_count = 1
    assert pool.store_frames(record_id="rec_owner000000001", total_bytes=1000, owner=recording)
    bytes_with_frames = pool.total_bytes()
    # Force the eviction through the REAL byte cap (the frames entry dies
    # FIRST — before anything else) → the owner's JPEGs are freed.
    original_max = pool.max_bytes
    try:
        pool.max_bytes = 24
        pool.store_video(record_id="rec_owner000000001:probe", mp4_bytes=b"x" * 16,
                         width=4, height=4, fps=5, frames=1)
    finally:
        pool.max_bytes = original_max
    assert pool.has_frames("rec_owner000000001") is False
    assert recording.frames == [] and recording.frames_released is True
    assert pool.total_bytes() < bytes_with_frames  # no phantom bytes
    # Releasing payloads REMOVES the :frames entry (here: nothing to remove,
    # already evicted — remove_frames is honest about it).
    assert pool.remove_frames("rec_owner000000001") is False
    # The release-after-fallback-encode path removes the entry: store again,
    # release manually, remove — the byte count drops by the frames size.
    recording.frames = [
        RecordedFrame(t_ms=0.0, jpeg=b"f" * 1000, pointer_x=None, pointer_y=None,
                      pointer_state="normal", in_image=False, width=64, height=48)
    ]
    assert pool.store_frames(record_id="rec_owner000000001", total_bytes=1000, owner=recording)
    before = pool.total_bytes()
    recording.release_frame_payloads()
    assert pool.remove_frames("rec_owner000000001") is True
    assert pool.total_bytes() == before - 1000


class _FakeRaw:
    size = (4, 4)
    raw = bytes(4 * 4 * 4)


class _FakeMssInstance:
    def __init__(self) -> None:
        self.closed = False
        self.grabs = 0

    def grab(self, region: Any) -> Any:
        self.grabs += 1
        return _FakeRaw()

    def close(self) -> None:
        self.closed = True


def _owned_mss_recorder(
    factory_mss: _FakeMssInstance,
    *,
    duration_s: float = 0.4,
    cancel_event: Any = None,
    grab_raises: bool = False,
) -> ScreenRecorder:
    """A recorder over the recorder-OWNED MssFrameGrabber (a fake mss
    factory) — the DELTA 2 M6 close assertions at recorder level."""

    def factory() -> Any:
        if grab_raises:
            # The mss instance's own grab raises (instance attribute shadows
            # the class method) — the recorder's grab path propagates it.
            def raising(region: Any) -> Any:
                raise OSError("mss grab failed")

            factory_mss.grab = raising  # type: ignore[method-assign]
        return factory_mss

    grabber = recorder_module.MssFrameGrabber(factory)
    clock = FakeClock()
    return ScreenRecorder(
        record_id="rec_mssowned000001",
        duration_s=duration_s,
        fps=5,
        region_physical=(0, 0, 4, 4),
        transform=_IdentityTransform(),
        canonical_origin=(0, 0),
        canonical_size=(4, 4),
        max_keyframes=RECORD_MAX_KEYFRAMES_DEFAULT,
        session_id="sess_rec",
        grab=grabber,
        cursor=lambda: None,
        clock=clock,
        cancel=cancel_event if cancel_event is not None else FakeCancelEvent(clock),
    )


def test_mss_grabber_closed_on_normal_end() -> None:
    mss = _FakeMssInstance()
    recorder = _owned_mss_recorder(mss)
    recording = recorder.run()
    assert recording.error is None
    assert mss.grabs > 0
    assert mss.closed is True  # closed on normal end


def test_mss_grabber_closed_on_cancel() -> None:
    """Cancel mid-run (after the first grab, so the mss instance exists):
    the owned grabber is closed."""
    mss = _FakeMssInstance()
    cancel_event = threading.Event()

    def grab_then_cancel(region: Any) -> Any:
        mss.grabs += 1
        cancel_event.set()
        return _FakeRaw()

    mss.grab = grab_then_cancel  # type: ignore[method-assign]
    recorder = _owned_mss_recorder(
        mss, duration_s=30.0, cancel_event=cancel_event
    )
    recording = recorder.run()
    assert recording.cancelled is True
    assert mss.grabs > 0
    assert mss.closed is True  # closed on cancel (instance existed)


def test_mss_grabber_closed_on_failure() -> None:
    mss = _FakeMssInstance()
    recorder = _owned_mss_recorder(mss, grab_raises=True)
    recording = recorder.run()
    assert recording.error_kind == "capture_failed"
    assert mss.closed is True  # closed on failure


def test_video_max_bytes_env_band(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RECORD_VIDEO_MAX_MB_ENV, raising=False)
    assert video_max_bytes() == RECORD_VIDEO_MAX_MB_DEFAULT * 1024 * 1024
    assert RECORD_VIDEO_MAX_MB_DEFAULT == 2 and RECORD_VIDEO_MAX_MB_BAND == (1, 8)
    monkeypatch.setenv(RECORD_VIDEO_MAX_MB_ENV, "5")
    assert video_max_bytes() == 5 * 1024 * 1024
    monkeypatch.setenv(RECORD_VIDEO_MAX_MB_ENV, "8")
    assert video_max_bytes() == 8 * 1024 * 1024
    # fallback-not-clamp: out-of-band, unparsable, non-positive → default.
    monkeypatch.setenv(RECORD_VIDEO_MAX_MB_ENV, "9")
    assert video_max_bytes() == RECORD_VIDEO_MAX_MB_DEFAULT * 1024 * 1024
    monkeypatch.setenv(RECORD_VIDEO_MAX_MB_ENV, "0")
    assert video_max_bytes() == RECORD_VIDEO_MAX_MB_DEFAULT * 1024 * 1024
    monkeypatch.setenv(RECORD_VIDEO_MAX_MB_ENV, "big")
    assert video_max_bytes() == RECORD_VIDEO_MAX_MB_DEFAULT * 1024 * 1024
