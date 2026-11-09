"""The screen-recording core (ORVEX-2026-109 WP-R1 + DELTA 1, LAYER 2).

One sentence first: *a background worker captures 0.5–10 s of the screen on a
fixed schedule, holds the frames as bounded JPEG bytes in memory, and reduces
them to a change timeline, timestamped keyframes and a contact sheet — while
the agent keeps calling tools.*

DELTA 1 (VAL-WP-R-01) load-bearing fixes:

**One id end to end (B1).**  The tool mints the ``record_id`` and passes it
into :class:`ScreenRecorder`; the reply payload, the resource URI, the saved
filenames and the pool keys all read THAT id.

**Canonical vs frame space (M1/M2).**  Canonical screenshot space (the space
actions are grounded in, physical pixels) is NOT frame space: frames are
downscaled to at most 1280 px.  The recorder keeps a per-recording
``frame_scale`` (frame px per canonical px) and the region origin;
``in_image`` and the composite point are computed in FRAME space, and
``changed_bbox`` is mapped BACK to canonical (divide by the scale, add the
origin).  Keyframe pointer metadata is stored in FRAME space (fetch composites
at scale 1.0 on the frame's own pixels, exactly right).

**The encoder is license-aware (item 11).**  Preference list
``h264_mf`` (Windows Media Foundation) first, then ``libx264``, configurable
via ``CORTEX_RECORD_ENCODER`` (fallback-not-clamp to ``"h264_mf,libx264"``);
the video entry reports the encoder actually used.  License note: the av wheel
bundles FFmpeg, which self-reports LGPL-3.0-or-later, plus GPL-2.0-or-later
libx264/libx265; avcodec is linked against those GPL libraries and FFmpeg's
LICENSE.md makes the GPL apply to that combination, so for redistribution the
av.libs set must be treated as GPL (in practice GPL-3.0-or-later). Cortex's
own MIT source distribution is unaffected. The
components are loaded whichever encoder runs — preferring ``h264_mf`` only
keeps Cortex itself from encoding through libx264.

**The encode respects a deadline (M3).**  ``encode_video(deadline=...)`` —
the ladder checks the clock before every rung and returns
``{"omitted": "over_budget"}`` when the next rung cannot fit.

**Memory (M5).**  The encoder decodes → composites → pushes ONE frame at a
time (a generator, never a full RGB list).

**Noise threshold (m6).**  A thumbnail difference counts as a change only
past a per-pixel gray delta AND a minimum changed-pixel count; ``change_ratio``
is changed pixels / total, and bboxes come from the thresholded image.

**Real-time ceiling (m5-minor).**  The capture loop also holds a HARD wall
deadline (``time.monotonic() + duration + RECORDER_WALL_MARGIN_SECONDS``) the
injected clock cannot defeat — an injected clock that never advances ends the
recording instead of looping forever.

**Exact schedule (m3).**  Frames are captured only strictly inside the
window (``fps × duration`` frames exactly), ``dropped_frames`` counts only
missed slots inside the window, and video frames carry real timestamps (pts)
so dropped frames keep real-time duration.
"""

from __future__ import annotations

import base64
import io
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageChops, ImageDraw

from ..core.limits import (
    RECORD_FRAME_LONG_SIDE_MAX,
    RECORD_JPEG_QUALITY,
    RECORD_SHEET_LONG_SIDE_MAX,
    RECORD_VIDEO_MAX_MB_BAND,
    RECORD_VIDEO_MAX_MB_DEFAULT,
    RECORD_VIDEO_MAX_MB_ENV,
)

#: Cancellation granularity of the capture loop: the worker re-checks its
#: deadline and its cancel event at least every ``RECORDER_SLICE_SECONDS`` of
#: REAL waiting time (cancel latency ≤ 100 ms, the wait-worker precedent).
RECORDER_SLICE_SECONDS = 0.1

#: Grayscale thumbnail divisor for the change timeline (DELTA 1 n4: 1/4 — a
#: ~12×12-px change on a 1920 monitor must reach the timeline).
RECORDER_THUMB_DIVISOR = 4

#: m6 noise threshold: a thumbnail pixel counts as changed only when its gray
#: delta reaches this level (8/255 — caret antialiasing and dithering noise
#: stay under it).
RECORDER_NOISE_GRAY_DELTA = 8

#: m6 noise threshold: at least this many THUMBNAIL pixels must pass the delta
#: gate before a frame counts as changed (DELTA 2 n4: 4 at 1/4 scale = a
#: ~16×16-frame-px / ~24×24-canonical-px area at most — a checkbox tick on a
#: 1920 monitor is seen; JPEG dither noise still is not).
RECORDER_MIN_CHANGED_PIXELS = 4

#: m5-minor: the HARD wall ceiling the injected clock cannot defeat — a
#: recording ends at most ``duration + RECORDER_WALL_MARGIN_SECONDS`` of REAL
#: time after it starts, whatever the injected clock says.
RECORDER_WALL_MARGIN_SECONDS = 5.0

#: The Cortex pointer sprite composited into keyframes/sheet/video is sized
#: for the FRAME's own scale (WP-C rule): a constant amount of SCREEN content.

#: Bounded join budget for the teardown path (the wait family's S-16 shape).
RECORDER_JOIN_TIMEOUT_SECONDS = 5.0

#: m6: the sheet label height / gap (decorative).
_SHEET_LABEL_HEIGHT = 18
_SHEET_GAP = 6

#: DELTA 1 item 12: the default blob cap is 2 MiB raw (band 1..8 MiB, env
#: ``CORTEX_RECORD_VIDEO_MAX_MB`` fallback-not-clamp — the constants live in
#: ``core/limits.py`` next to the other bands).
VIDEO_MAX_MB_ENV = RECORD_VIDEO_MAX_MB_ENV

#: DELTA 1 item 11: the closed encoder choice set and the fallback-not-clamp
#: default preference.  License note: the av wheel bundles FFmpeg, which
#: self-reports LGPL-3.0-or-later, plus GPL-2.0-or-later libx264/libx265;
#: avcodec is linked against those GPL libraries, and FFmpeg's LICENSE.md
#: makes the GPL apply to that combination, so the av.libs set must be
#: treated as GPL for redistribution — in practice GPL-3.0-or-later. Cortex's
#: own MIT source distribution is unaffected. The components are loaded
#: whichever encoder runs —
#: ``h264_mf`` (Windows Media Foundation) is preferred so Cortex itself never
#: ENCODES through GPL libx264; the preference does not unload them.
ENCODER_ENV = "CORTEX_RECORD_ENCODER"
ENCODER_CHOICES = ("h264_mf", "libx264")
ENCODER_DEFAULT = "h264_mf,libx264"
#: h264_mf takes a BITRATE rather than a CRF; the cap ladder sizes its rungs
#: by bitrate (scaled by the resolution step squared).
H264_MF_BITRATE_RUNGS = (4_000_000, 2_000_000, 1_000_000)
#: libx264 CRF rungs (None = the library default, then looser).
LIBX264_CRF_RUNGS = (None, 28, 35)

#: DELTA 1 M3: the closed ``video`` omission reasons (the docs list them —
#: docs/REPLY-CONTRACT.md, the record family section).  ``over_budget``:
#: the encode ladder stopped because the client budget could not fit the next
#: rung.  DELTA 2 N3: ``frames_released`` — the held frames were evicted
#: under the pool's byte cap before any video was stored, so video can no
#: longer be produced for this recording.
VIDEO_OMITTED_NOT_REQUESTED = "not_requested"
VIDEO_OMITTED_TOO_LARGE = "too_large"
VIDEO_OMITTED_ENCODER_UNAVAILABLE = "encoder_unavailable"
VIDEO_OMITTED_ENCODE_FAILED = "encode_failed"
VIDEO_OMITTED_OVER_BUDGET = "over_budget"
VIDEO_OMITTED_FRAMES_RELEASED = "frames_released"
#: WP-E (ORVEX-2026-109 E2): with the default ``video_delivery="file"`` the MP4
#: is written under the recordings folder instead of travelling as a blob (most
#: hosts truncate large tool results — a 196-562 KB blob never reached the
#: model).  A FAILED FILE WRITE is its own closed omission reason; it is NOT
#: final — a later collect retries the write from the retained/stored video.
VIDEO_OMITTED_WRITE_FAILED = "write_failed"
VIDEO_OMISSION_REASONS = (
    VIDEO_OMITTED_NOT_REQUESTED,
    VIDEO_OMITTED_TOO_LARGE,
    VIDEO_OMITTED_ENCODER_UNAVAILABLE,
    VIDEO_OMITTED_ENCODE_FAILED,
    VIDEO_OMITTED_OVER_BUDGET,
    VIDEO_OMITTED_FRAMES_RELEASED,
    VIDEO_OMITTED_WRITE_FAILED,
)

#: DELTA 2 n3: omissions that are FINAL for the recording — later result
#: calls reuse them instead of re-running the ladder.  Only ``over_budget``
#: is retried (a later call has a fresh budget).
VIDEO_FINAL_OMISSIONS = (
    VIDEO_OMITTED_TOO_LARGE,
    VIDEO_OMITTED_ENCODER_UNAVAILABLE,
    VIDEO_OMITTED_ENCODE_FAILED,
    VIDEO_OMITTED_FRAMES_RELEASED,
)

#: DELTA 2 n7: raised when an encoder cannot be OPENED (av stream/container
#: creation failed).  Everything else a rung does wrong is a mid-encode
#: failure (None), which falls back to the NEXT encoder once — never
#: retried across every rung of the broken one.
class EncoderOpenError(RuntimeError):
    pass


class _EncodeAborted(Exception):
    """Internal: the encode was aborted by the stop check (N4)."""


#: DELTA 2 n1: the documented recording statuses.
RECORD_STATUS_RECORDING = "recording"
RECORD_STATUS_ENCODING = "encoding"

#: DELTA 3 n2: the documented PER-FRAME encode cost estimate used for the
#: FIRST rung's budget check (before any real rung has been measured): an
#: h264_mf/libx264 720p-class rung costs roughly 20 ms per frame on the
#: gating hardware.  The first rung is SKIPPED when the remaining client
#: budget cannot fit ``frames × this constant``; later rungs use the
#: measured previous-rung cost instead.
RECORDER_PER_FRAME_ENCODE_ESTIMATE_SECONDS = 0.02

#: DELTA 1 item 12: the resolution ladder floor (never below this on the long
#: side) and the resolution step.
VIDEO_RESOLUTION_STEP = 0.75
VIDEO_MIN_LONG_SIDE = 480


def _new_record_id() -> str:
    """``rec_`` + 64 bits of ``uuid4().hex`` — unguessable, session-scoped."""
    return "rec_" + uuid.uuid4().hex[:16]


def video_max_bytes() -> int:
    """The blob cap in raw bytes: ``CORTEX_RECORD_VIDEO_MAX_MB`` MiB, band
    1..8 (fallback-not-clamp to the 2 MiB default — a typo can never arm an
    oversize blob)."""
    raw = os.environ.get(RECORD_VIDEO_MAX_MB_ENV, "").strip()
    if not raw:
        return RECORD_VIDEO_MAX_MB_DEFAULT * 1024 * 1024
    try:
        value = float(raw)
    except ValueError:
        return RECORD_VIDEO_MAX_MB_DEFAULT * 1024 * 1024
    if value != value or value <= 0:  # NaN or non-positive
        return RECORD_VIDEO_MAX_MB_DEFAULT * 1024 * 1024
    if not (RECORD_VIDEO_MAX_MB_BAND[0] <= value <= RECORD_VIDEO_MAX_MB_BAND[1]):
        return RECORD_VIDEO_MAX_MB_DEFAULT * 1024 * 1024
    return int(value * 1024 * 1024)


def encoder_preference() -> list[str]:
    """The encoder preference list: ``CORTEX_RECORD_ENCODER`` is a
    comma-separated list from the closed set {h264_mf, libx264}; fallback-not-
    clamp to ``"h264_mf,libx264"`` on unset, empty, unknown names or any
    invalid token (a typo can never silently reorder the license preference)."""
    raw = os.environ.get(ENCODER_ENV, "").strip()
    if not raw:
        return list(ENCODER_DEFAULT.split(","))
    tokens = [token.strip().casefold() for token in raw.split(",")]
    tokens = [token for token in tokens if token]
    if not tokens or any(token not in ENCODER_CHOICES for token in tokens):
        return list(ENCODER_DEFAULT.split(","))
    return tokens


# --- the per-session dispatched-action ring (WP-R2) -------------------------------------------

#: Ring depth: the last 256 dispatched Cortex actions per session.
ACTION_RING_CAPACITY = 256


@dataclass(frozen=True)
class ActionEvent:
    """One dispatched Cortex action, stamped at the DISPATCH choke point.

    ``t_monotonic`` is the SAME monotonic clock the recorder's window is
    measured on, so ``actions_in_window`` correlation is a plain range query.
    ``x``/``y`` are canonical screenshot coordinates of the action's point
    (``None`` for actions without one); ``result`` is ``"dispatched"`` at the
    dispatch moment (DELTA 1 m4: the dispatch instant is what the ±100 ms
    sheet labels correlate against) or a §1 token where the pipeline already
    knows one.  ``dispatch_id`` (DELTA 3 n6) is the per-ring identity the
    finalize path uses to update THIS entry's result — matching by action
    NAME would let a refused action overwrite a previous dispatched one.
    """

    t_monotonic: float
    action: str
    x: float | None
    y: float | None
    result: str
    dispatch_id: int = -1


class ActionRing:
    """A bounded, lock-protected ring of the last dispatched actions."""

    def __init__(
        self, capacity: int = ACTION_RING_CAPACITY, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._lock = threading.Lock()
        self._events: deque[ActionEvent] = deque(maxlen=max(1, int(capacity)))
        self._clock = clock
        self._next_dispatch_id = 0

    def record(
        self, action: str, x: float | None, y: float | None, result: str, *, at: float | None = None
    ) -> int:
        """Append one dispatched action; return its DISPATCH ID (the identity
        `update_action_result` matches on)."""
        with self._lock:
            self._next_dispatch_id += 1
            dispatch_id = self._next_dispatch_id
            try:
                event = ActionEvent(
                    t_monotonic=self._clock() if at is None else float(at),
                    action=str(action),
                    x=None if x is None else float(x),
                    y=None if y is None else float(y),
                    result=str(result),
                    dispatch_id=dispatch_id,
                )
                self._events.append(event)
            except Exception:  # noqa: BLE001 - the ring must never break the execute path
                dispatch_id = -1
        return dispatch_id

    def events(self) -> list[ActionEvent]:
        with self._lock:
            return list(self._events)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def in_window(self, start_monotonic: float, end_monotonic: float) -> list[ActionEvent]:
        """Events with ``start <= t <= end``, oldest first."""
        return [
            event for event in self.events() if start_monotonic <= event.t_monotonic <= end_monotonic
        ]


#: session_id -> ring.  Created lazily on first dispatch; pruned at session
#: end (DELTA 1 m8, ``prune_action_ring``).
_ACTION_RINGS: dict[str, ActionRing] = {}
_ACTION_RINGS_LOCK = threading.Lock()


def action_ring(session_id: str) -> ActionRing:
    """The session's action ring, created on first use (never raises)."""
    with _ACTION_RINGS_LOCK:
        ring = _ACTION_RINGS.get(session_id)
        if ring is None:
            ring = ActionRing()
            _ACTION_RINGS[session_id] = ring
        return ring


def prune_action_ring(session_id: str) -> int:
    """DELTA 1 m8: drop the session's action ring (session end)."""
    with _ACTION_RINGS_LOCK:
        ring = _ACTION_RINGS.pop(session_id, None)
    if ring is None:
        return 0
    ring.clear()
    return 1


def record_dispatched_action(
    session_id: Any, action: Any, result: str
) -> int:
    """Feed ONE dispatched action into the session's ring; return its
    DISPATCH ID (DELTA 3 n6) — the identity `update_action_result` matches
    on at finalize.  The ONE feed site is the pipeline's DISPATCH choke
    point.  Best-effort: any failure is swallowed and ``-1`` returned."""
    try:
        value = getattr(action, "action", None)
        name = getattr(value, "value", value)
        x = y = None
        point = getattr(action, "point", None)
        if point is not None:
            x, y = point.x, point.y
        else:
            to_point = getattr(action, "to_point", None)
            if to_point is not None:
                x, y = to_point.x, to_point.y
        return action_ring(str(session_id)).record(str(name), x, y, str(result))
    except Exception:  # noqa: BLE001 - best-effort, never a gate
        return -1


def update_action_result(session_id: str, dispatch_id: Any, result: str) -> bool:
    """DELTA 3 n6: update the ring entry matching this DISPATCH ID with the
    final §1 outcome token (the dispatch-time stamp stands).  Matching is by
    dispatch IDENTITY, never by action name — an action refused before
    dispatch has no ring entry, and a bogus/unknown id updates NOTHING (an
    action with the same name can never be mislabeled).  Best-effort;
    False when nothing matched."""
    if not isinstance(dispatch_id, int) or isinstance(dispatch_id, bool) or dispatch_id <= 0:
        return False
    with _ACTION_RINGS_LOCK:
        ring = _ACTION_RINGS.get(session_id)
    if ring is None:
        return False
    with ring._lock:
        for index in range(len(ring._events) - 1, -1, -1):
            event = ring._events[index]
            if event.dispatch_id == int(dispatch_id):
                ring._events[index] = ActionEvent(
                    t_monotonic=event.t_monotonic,
                    action=event.action,
                    x=event.x,
                    y=event.y,
                    result=str(result),
                    dispatch_id=event.dispatch_id,
                )
                return True
    return False


def actions_in_window(session_id: str, start_monotonic: float, end_monotonic: float) -> list[dict[str, Any]]:
    """The dispatched actions inside ``[start, end]``, as reply-shaped dicts."""
    return [
        {
            "t_monotonic": event.t_monotonic,
            "action": event.action,
            "x": event.x,
            "y": event.y,
            "result": event.result,
        }
        for event in action_ring(session_id).in_window(start_monotonic, end_monotonic)
    ]


def peek_actions_in_window(
    session_id: str, start_monotonic: float, end_monotonic: float
) -> list[ActionEvent]:
    """DELTA 2 NIT: like :func:`actions_in_window` but NEVER creates the
    session's ring — a worker finalizing after the session was torn down (the
    ring pruned) reads an empty window instead of resurrecting the ring.
    Returns raw :class:`ActionEvent` objects."""
    with _ACTION_RINGS_LOCK:
        ring = _ACTION_RINGS.get(session_id)
    if ring is None:
        return []
    return ring.in_window(start_monotonic, end_monotonic)


# --- frames, timeline, keyframes --------------------------------------------------------------


@dataclass(frozen=True)
class RecordedFrame:
    """One captured frame: bounded JPEG bytes + capture-time pointer metadata.

    ``pointer_x``/``pointer_y`` are CANONICAL screenshot coordinates (monitor
    screenshot space, the space actions are grounded in); ``in_image`` is
    whether the hotspot lies inside THIS frame's rectangle, computed in FRAME
    space (DELTA 1 M1: the canonical point is first mapped through the
    recording's region origin and ``frame_scale``).  ``pointer_state`` is
    ``"pressed"`` when the capture instant fell inside a press window of the
    backend's pointer ring (WP-C ``pressed_at``), else ``"normal"``.
    ``change_ratio`` is changed pixels / total (DELTA 1 m6); ``changed_bbox``
    is CANONICAL (origin + thresholded thumbnail bbox ÷ frame_scale).
    """

    t_ms: float
    jpeg: bytes
    pointer_x: float | None
    pointer_y: float | None
    pointer_state: str
    in_image: bool
    width: int
    height: int
    change_ratio: float = 0.0
    changed_bbox: tuple[int, int, int, int] | None = None  # canonical


@dataclass(frozen=True)
class ChangeSegment:
    """A run of consecutively changed frames, in recording-relative ms."""

    start_ms: float
    end_ms: float
    bbox_union: tuple[int, int, int, int]  # canonical
    peak_ratio: float


@dataclass
class Keyframe:
    """One selected keyframe with its reply metadata (``image_id`` is filled by
    the recording worker when the frame enters the evidence pool)."""

    image_id: str
    t_ms: float
    index: int


@dataclass
class Recording:
    """The finished (or stopped, or failed) product of one recording run.

    DELTA 1: ``record_id`` is the TOOL's id (B1); ``want_video``/``save``
    persist the start call's flags so a later result call honours them after
    the in-flight handle is gone (B2); ``frame_scale`` is the per-recording
    canonical↔frame scale (M1); ``frame_count`` survives
    :meth:`release_frame_payloads` (DELTA 1 m8 — the JPEGs are dropped once
    the keyframes and the video are stored, outside any byte cap).
    """

    record_id: str
    session_id: str
    duration_s: float
    fps: int
    requested_monotonic_start: float
    actual_duration_s: float = 0.0
    dropped_frames: int = 0
    cancelled: bool = False
    region: list[int] | None = None  # canonical [left, top, width, height]
    monitor_bounds: list[int] = field(default_factory=list)
    canonical_origin: tuple[int, int] = (0, 0)
    canonical_size: tuple[int, int] = (0, 0)
    frame_scale: float = 1.0  # frame px per canonical px (M1)
    frames: list[RecordedFrame] = field(default_factory=list)
    frame_count: int = 0
    frames_released: bool = False
    # DELTA 2 N3: True once the worker accounted the held frames in the pool
    # (store_frames) — only THEN can pool eviction mean the frames are gone.
    frames_accounted: bool = False
    segments: list[ChangeSegment] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    keyframes: list[Keyframe] = field(default_factory=list)
    sheet_png_b64: str | None = None
    video: dict[str, Any] | None = None
    want_video: bool = False  # B2: persisted from the start call
    save: bool = False  # B2: persisted from the start call
    #: WP-E E2: the start call's video delivery ("file" default | "inline") —
    #: persisted like want_video/save so a late result call honours it.
    video_delivery: str = "file"
    error: str | None = None
    error_kind: str | None = None  # "capture_failed" | "record_worker_failed"
    started_at_epoch: float = 0.0
    # VAL-WP-D-01 M2: the session's show_pointer default at record time — OFF
    # means no sprite in the sheet, the keyframes or the video, and no keyframe
    # pointer metadata (fetch draws nothing either way; this makes the worker's
    # encode and the fallback encode honour the session even without the handle).
    composite_pointer: bool = True
    # DELTA 2 N3: the per-recording video-file lock — created WHEN THE
    # RECORDING IS BUILT (never a check-then-set inside the reply worker, where
    # two concurrent collects could each create their own lock and both
    # write).
    _video_file_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    # DELTA 2 N4: the write-once memo for the delivered MP4 (set by
    # ``server.record._write_delivered_video_once`` under the lock above).
    _written_video_path: str | None = None
    _written_video_error: str | None = None

    @property
    def effective_fps(self) -> float:
        """Exact (m3) and NEVER above the requested fps (DELTA 2 m3 edge:
        duration×fps non-integer windows, e.g. 0.5 s at 3 fps)."""
        if self.actual_duration_s <= 0:
            return 0.0
        return round(min(self.frame_count / self.actual_duration_s, float(self.fps)), 3)

    def timeline(self) -> list[dict[str, Any]]:
        return [
            {
                "start_ms": round(segment.start_ms, 1),
                "end_ms": round(segment.end_ms, 1),
                "bbox_union": list(segment.bbox_union),
                "peak_ratio": round(segment.peak_ratio, 6),
            }
            for segment in self.segments
        ]

    def release_frame_payloads(self) -> int:
        """DELTA 1 m8: drop the held JPEG bytes once the keyframes and the
        video are stored (the finished registry must not hold ~10-20 MB of
        JPEGs outside any cap).  The count and every derived product survive."""
        released = sum(len(frame.jpeg) for frame in self.frames)
        self.frames = []
        self.frames_released = True
        return released


class MssFrameGrabber:
    """The production grabber: the worker thread's OWN mss instance.

    ``mss`` is thread-bound, so the instance is opened lazily ON the calling
    (recording worker) thread through the backend's ``_mss_factory`` and
    CLOSED on every end path — normal end, cancel, failure and session stop
    (DELTA 1 M6: the object itself is passed to the recorder, so the
    recorder's ``close`` lookup finds it; mss 10's GDI class has ``close()``
    and no ``__del__``).  ``grab`` returns a PIL RGB image for one physical
    ``[left, top, width, height]`` rectangle; failures raise (the recording
    worker turns them into a typed ``failed`` outcome, never a hang).
    """

    def __init__(self, mss_factory: Callable[[], Any] | None) -> None:
        self._factory = mss_factory
        self._capture: Any = None

    def grab(self, left: int, top: int, width: int, height: int) -> Image.Image:
        if self._capture is None:
            if self._factory is not None:
                self._capture = self._factory()
            else:
                import mss  # type: ignore[import-not-found]  # lazy: recorder-only cost

                self._capture = (getattr(mss, "MSS", None) or mss.mss)()
        raw = self._capture.grab({"left": left, "top": top, "width": width, "height": height})
        return Image.frombuffer("RGB", raw.size, raw.raw, "raw", "BGRX", 0, 1)

    def close(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            try:
                capture.close()
            except Exception:  # noqa: BLE001 - best-effort close
                pass

    __call__ = grab


class GrabberHandle:
    """Wraps an EXTERNAL grab callable so the recorder always holds a callable
    object with a ``close`` — but DELTA 2 n5: ``close`` is a DELIBERATE NO-OP.
    The wrapped grabber belongs to the BACKEND (often a bound method whose
    ``__self__`` is the backend); closing it would stop the backend's UIA
    owner thread after every recording.  Only grabbers the recorder itself
    created (:class:`MssFrameGrabber`) ever close."""

    def __init__(self, grab: Callable[..., Image.Image]) -> None:
        self._grab = grab

    def __call__(self, left: int, top: int, width: int, height: int) -> Image.Image:
        return self._grab(left, top, width, height)

    def close(self) -> None:
        return None  # n5: NEVER close the backend's own grabber.


class ScreenRecorder:
    """The bounded capture worker: fixed schedule, bounded frames, one run.

    Injectable seams: ``clock`` (every schedule decision), ``grab`` (a
    CLOSEABLE grabber object — one PIL RGB frame per call), ``cursor``
    (physical cursor position), ``pressed_at`` (the backend pointer ring's
    press-window classifier, WP-C).  Tests inject a fake clock AND a fake
    grabber together — the grabber advances the fake clock — while the HARD
    wall ceiling below stays on the real clock, so a stalled injected clock
    ends the recording instead of looping forever (DELTA 1 m5-minor).
    """

    def __init__(
        self,
        *,
        record_id: str,
        duration_s: float,
        fps: int,
        region_physical: tuple[int, int, int, int],
        transform: Any,
        canonical_origin: tuple[int, int],
        canonical_size: tuple[int, int],
        max_keyframes: int,
        session_id: str,
        grab: Callable[..., Image.Image],
        cursor: Callable[[], tuple[int, int] | None],
        pressed_at: Callable[[float], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        cancel: threading.Event | None = None,
        composite_pointer: bool = True,
    ) -> None:
        self.record_id = str(record_id)
        self.duration_s = float(duration_s)
        self.fps = int(fps)
        self.region_physical = region_physical
        self.transform = transform
        self.canonical_origin = canonical_origin
        self.canonical_size = canonical_size
        self.max_keyframes = int(max_keyframes)
        self.session_id = str(session_id)
        self._grab = grab
        self._cursor = cursor
        self._pressed_at = pressed_at
        self._clock = clock
        self.cancel = cancel or threading.Event()
        # VAL-WP-D-01 M2: the session's show_pointer default — OFF composites
        # NO cursor into the sheet cells or the video, and stores no keyframe
        # pointer metadata (the frames keep their canonical pointer coordinates
        # for the timeline; only the sprite is dropped).
        self.composite_pointer = bool(composite_pointer)

    # --- capture loop -----------------------------------------------------------------------

    def run(self) -> Recording:
        """Capture until the deadline (or cancel/failure); return the Recording."""
        # m1/nit: cancelled before start — no grab at all.
        start = self._clock()
        deadline = start + self.duration_s
        interval = 1.0 / self.fps
        # m3: the exact window — frames only strictly inside it.
        total_slots = int(round(self.duration_s * self.fps))
        # m5-minor: the hard REAL-time ceiling the injected clock cannot defeat.
        wall_deadline = time.monotonic() + self.duration_s + RECORDER_WALL_MARGIN_SECONDS
        recording = Recording(
            record_id=self.record_id,  # B1: the TOOL's id, end to end
            session_id=self.session_id,
            duration_s=self.duration_s,
            fps=self.fps,
            requested_monotonic_start=start,
            region=[self.canonical_origin[0], self.canonical_origin[1], *self.canonical_size],
            canonical_origin=self.canonical_origin,
            canonical_size=self.canonical_size,
        )
        # VAL-WP-D-01 M2: the flag rides the Recording so the worker's encode
        # and the result-call fallback encode honour it after the handle is gone.
        recording.composite_pointer = self.composite_pointer
        if self.cancel.is_set():
            recording.cancelled = True
            recording.actual_duration_s = 0.0
            self._close_grabber()
            return recording
        slot = 0
        wall_broken = False
        try:
            while True:
                # m5-minor: the wall ceiling is checked with the REAL clock.
                if time.monotonic() > wall_deadline:
                    wall_broken = True
                    recording.cancelled = True
                    break
                now = self._clock()
                if now >= deadline:
                    break
                target = start + slot * interval
                if target >= deadline:
                    break  # m3: never the deadline+1 frame
                if self.cancel.is_set():
                    recording.cancelled = True
                    break
                # Bounded wait: slices of RECORDER_SLICE_SECONDS on the cancel
                # event, computed from THIS module's clock only — and the HARD
                # wall ceiling is checked INSIDE the wait loop too (a stalled
                # injected clock can never spin here forever, m5-minor).
                while True:
                    if time.monotonic() > wall_deadline:
                        wall_broken = True
                        recording.cancelled = True
                        break
                    remaining = target - self._clock()
                    if remaining <= 0:
                        break
                    if self.cancel.wait(min(remaining, RECORDER_SLICE_SECONDS)):
                        recording.cancelled = True
                        break
                if recording.cancelled:
                    break
                frame = self._capture_one(recording, self._clock())
                if frame is not None:
                    recording.frames.append(frame)
                # A frame captured past its slot drops the SKIPPED slots — the
                # schedule pointer advances, never a catch-up burst — and only
                # slots INSIDE the window are counted (m3).
                actual = self._clock()
                skipped = int((actual - start) / interval) - slot
                skipped = max(0, min(skipped, total_slots - 1 - slot))
                if skipped > 0:
                    recording.dropped_frames += skipped
                    slot += skipped
                slot += 1
        except Exception as exc:  # noqa: BLE001 - capture failure: B3, a typed outcome
            recording.error = f"{type(exc).__name__}: {exc}"
            recording.error_kind = "capture_failed"
        # m3: the actual window is the SCHEDULED window for a natural end (the
        # frames sit on the schedule inside it) and the cut point for a
        # cancelled / wall-broken run — effective_fps is exact either way.
        if recording.cancelled:
            recording.actual_duration_s = max(
                0.0, min(self._clock(), deadline) - start
            )
        else:
            recording.actual_duration_s = self.duration_s
        if wall_broken and recording.error is None:
            recording.error = (
                "the recording hit the hard real-time ceiling "
                "(a stalled injected clock cannot extend it)"
            )
            recording.error_kind = "capture_failed"
        try:
            self._close_grabber()
        except Exception:  # noqa: BLE001 - best-effort
            pass
        self._finalize(recording, start)
        return recording

    def _close_grabber(self) -> None:
        # M6: the grabber OBJECT carries close() (GrabberHandle / MssFrameGrabber).
        close = getattr(self._grab, "close", None)
        if callable(close):
            close()

    # --- one frame --------------------------------------------------------------------------

    def _capture_one(self, recording: Recording, at: float) -> RecordedFrame | None:
        left, top, width, height = self.region_physical
        image = self._grab(left, top, width, height)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        # Downscale at once: longest side ≤ RECORD_FRAME_LONG_SIDE_MAX.
        long_side = max(image.width, image.height)
        if long_side > RECORD_FRAME_LONG_SIDE_MAX:
            scale = RECORD_FRAME_LONG_SIDE_MAX / long_side
            image = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.LANCZOS,
            )
        # M1: the per-recording canonical↔frame scale (frame px per canonical px).
        if recording.canonical_size[0] > 0 and recording.canonical_size[1] > 0:
            recording.frame_scale = min(
                image.width / recording.canonical_size[0],
                image.height / recording.canonical_size[1],
            )
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=RECORD_JPEG_QUALITY)
        px = py = None
        in_image = False
        try:
            cursor = self._cursor()
            if cursor is not None:
                cx, cy = self.transform.to_screenshot(float(cursor[0]), float(cursor[1]))
                px, py = float(cx), float(cy)
                # M1: in_image is decided in FRAME space (canonical-local →
                # frame-local through the region origin and the scale).
                local_x = (px - self.canonical_origin[0]) * recording.frame_scale
                local_y = (py - self.canonical_origin[1]) * recording.frame_scale
                in_image = (
                    0 <= round(local_x) < image.width and 0 <= round(local_y) < image.height
                )
        except Exception:  # noqa: BLE001 - pointer metadata is additive
            px = py = None
            in_image = False
        state = "pressed" if (self._pressed_at is not None and self._pressed_at(at)) else "normal"
        # The thumbnail diff compares LIKE with LIKE: both thumbnails come from
        # DECODED JPEG bytes (the held product), never from the pre-encode
        # image — a lossy-JPEG round trip would otherwise light up every frame.
        try:
            stored_image = Image.open(io.BytesIO(buffer.getvalue()))
            stored_image.load()
        except Exception:  # noqa: BLE001 - fall back to the pre-encode image
            stored_image = image
        previous = recording.frames[-1] if recording.frames else None
        change_ratio, changed_bbox = self._diff_against_previous(
            recording, stored_image, previous
        )
        return RecordedFrame(
            t_ms=(at - recording.requested_monotonic_start) * 1000.0,
            jpeg=buffer.getvalue(),
            pointer_x=px,
            pointer_y=py,
            pointer_state=state,
            in_image=in_image,
            width=image.width,
            height=image.height,
            change_ratio=change_ratio,
            changed_bbox=changed_bbox,
        )

    # --- timeline + segments + keyframes ----------------------------------------------------

    def _diff_against_previous(
        self,
        recording: Recording,
        image: Image.Image,
        previous: RecordedFrame | None,
    ) -> tuple[float, tuple[int, int, int, int] | None]:
        """1/8 grayscale thumbnail diff with the m6 noise gate:
        ``(change_ratio, canonical bbox)`` — ratio is changed pixels / total,
        the bbox comes from the THRESHOLDED image and is mapped back to
        canonical space (÷ frame_scale, + origin)."""
        try:
            thumb = image.resize(
                (
                    max(1, image.width // RECORDER_THUMB_DIVISOR),
                    max(1, image.height // RECORDER_THUMB_DIVISOR),
                ),
                Image.BILINEAR,
            ).convert("L")
            total = thumb.width * thumb.height
            if previous is None:
                return 0.0, None
            prev_image = Image.open(io.BytesIO(previous.jpeg))
            prev_image.load()
            prev_thumb = prev_image.resize(thumb.size, Image.BILINEAR).convert("L")
            diff = ImageChops.difference(thumb, prev_thumb)
            # m6: the per-pixel delta gate — antialiasing/dither noise stays 0.
            gated = diff.point(lambda p: 255 if p >= RECORDER_NOISE_GRAY_DELTA else 0)
            histogram = gated.histogram()
            # "L" histogram: 256 bins; bin 255 counts the fully-white
            # (threshold-passing) pixels.
            changed = histogram[255] if len(histogram) > 255 else sum(histogram)
            if changed < RECORDER_MIN_CHANGED_PIXELS:
                return 0.0, None
            bbox = gated.getbbox()
            if bbox is None:
                return 0.0, None
            ratio = changed / float(total)
            origin_x, origin_y = self.canonical_origin
            # M1: thumbnail px → frame px (×8) → canonical px (÷ frame_scale).
            scale = recording.frame_scale if recording.frame_scale > 0 else 1.0
            frame_w = (bbox[2] - bbox[0]) * RECORDER_THUMB_DIVISOR
            frame_h = (bbox[3] - bbox[1]) * RECORDER_THUMB_DIVISOR
            canonical = (
                int(origin_x + bbox[0] * RECORDER_THUMB_DIVISOR / scale),
                int(origin_y + bbox[1] * RECORDER_THUMB_DIVISOR / scale),
                int(frame_w / scale),
                int(frame_h / scale),
            )
            return ratio, canonical
        except Exception:  # noqa: BLE001 - the timeline is metadata, never a gate
            return 0.0, None

    def _build_segments(self, recording: Recording, frames: list[RecordedFrame]) -> list[ChangeSegment]:
        """Built ONCE per recording (DELTA 1 nit — no second pass)."""
        segments: list[ChangeSegment] = []
        current: dict[str, Any] | None = None
        for frame in frames:
            if frame.changed_bbox is None:
                if current is not None:
                    segments.append(self._segment(current))
                    current = None
                continue
            bx, by, bw, bh = frame.changed_bbox
            if current is None:
                current = {
                    "start_ms": frame.t_ms,
                    "end_ms": frame.t_ms,
                    "bbox": (bx, by, bw, bh),
                    "peak": frame.change_ratio,
                }
            else:
                x0, y0, w0, h0 = current["bbox"]
                nx = min(x0, bx)
                ny = min(y0, by)
                nw = max(x0 + w0, bx + bw) - nx
                nh = max(y0 + h0, by + bh) - ny
                current["end_ms"] = frame.t_ms
                current["bbox"] = (nx, ny, nw, nh)
                current["peak"] = max(current["peak"], frame.change_ratio)
        if current is not None:
            segments.append(self._segment(current))
        return segments

    @staticmethod
    def _segment(current: dict[str, Any]) -> ChangeSegment:
        return ChangeSegment(
            start_ms=current["start_ms"],
            end_ms=current["end_ms"],
            bbox_union=current["bbox"],
            peak_ratio=current["peak"],
        )

    def _select_keyframes(
        self, frames: list[RecordedFrame], segments: list[ChangeSegment]
    ) -> list[int]:
        """First, last, every segment onset + its settled end, then the largest
        changes — deduplicated, at most ``max_keyframes``, in time order.
        DELTA 1 m2: the FIRST and LAST frames are RESERVED before truncation —
        the final screen state can never be cut."""
        if not frames:
            return []
        reserved = {0, len(frames) - 1}
        chosen: set[int] = set(reserved)
        for segment in segments:
            chosen.add(self._frame_index_for_ms(frames, segment.start_ms))
            chosen.add(self._frame_index_for_ms(frames, segment.end_ms))
        ranked = sorted(
            range(len(frames)),
            key=lambda index: frames[index].change_ratio,
            reverse=True,
        )
        for index in ranked:
            if len(chosen) >= self.max_keyframes:
                break
            if frames[index].change_ratio > 0.0:
                chosen.add(index)
        # Reserve first/last BEFORE truncation: drop mid candidates first.
        ordered: list[int] = []
        if len(chosen) > self.max_keyframes:
            droppable = sorted(index for index in chosen if index not in reserved)
            keep = self.max_keyframes - len(reserved)
            chosen = set(droppable[:keep]) | reserved
        for index in sorted(chosen):
            ordered.append(index)
            if len(ordered) >= self.max_keyframes:
                break
        return ordered

    @staticmethod
    def _frame_index_for_ms(frames: list[RecordedFrame], t_ms: float) -> int:
        best = 0
        for index, frame in enumerate(frames):
            if frame.t_ms <= t_ms + 1e-6:
                best = index
            else:
                break
        return best

    def _finalize(self, recording: Recording, start: float) -> None:
        """Timeline, segments, actions-in-window, keyframes, contact sheet."""
        recording.frame_count = len(recording.frames)
        recording.segments = self._build_segments(recording, recording.frames)
        # DELTA 2 NIT: peek, never create — a worker finalizing after the
        # session was torn down (ring pruned) must not resurrect the ring.
        recording.actions = [
            {
                "t_ms": round((event.t_monotonic - start) * 1000.0, 1),
                "action": event.action,
                "x": event.x,
                "y": event.y,
                "result": event.result,
            }
            for event in peek_actions_in_window(
                self.session_id, start, start + self.duration_s
            )
        ]
        sheet, keyframes = self._build_sheet(recording)
        recording.sheet_png_b64 = sheet
        recording.keyframes = keyframes

    # --- output compositing -----------------------------------------------------------------

    def _frame_scale(self, recording: Recording) -> float:
        """The recording's canonical↔frame scale (sprite sizing, WP-C rule)."""
        return recording.frame_scale if recording.frame_scale > 0 else 1.0

    def _composite_frame(
        self, recording: Recording, frame: RecordedFrame
    ) -> Image.Image | None:
        """Decode one held JPEG and composite the Cortex pointer (WP-C).

        DELTA 1 M1: the composite point is computed in FRAME space — the
        canonical point minus the region origin, times the frame scale.
        VAL-WP-D-01 M2: with ``composite_pointer`` OFF (the session's
        show_pointer default) NO sprite is drawn — the cell is the clean
        decode."""
        try:
            from .pointer_sprite import composite

            image = Image.open(io.BytesIO(frame.jpeg))
            image.load()
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            if (
                not self.composite_pointer
                or frame.pointer_x is None
                or frame.pointer_y is None
                or not frame.in_image
            ):
                return image
            scale = self._frame_scale(recording)
            local_x = (frame.pointer_x - self.canonical_origin[0]) * scale
            local_y = (frame.pointer_y - self.canonical_origin[1]) * scale
            composited, _ = composite(image, local_x, local_y, frame.pointer_state, scale)
            return composited
        except Exception:  # noqa: BLE001 - decoration only, never a gate
            return None

    def _action_label(self, recording: Recording, t_ms: float) -> str | None:
        for action in recording.actions:
            if abs(float(action["t_ms"]) - t_ms) <= 100.0:
                return str(action["action"])
        return None

    def _build_sheet(
        self, recording: Recording
    ) -> tuple[str | None, list[Keyframe]]:
        """The contact sheet (longest side ≤ RECORD_SHEET_LONG_SIDE_MAX) and the
        keyframe list.  Pure-PIL; the labels read "t=1.40s" plus the action name
        when one fell within ±100 ms; the cursor is composited into every cell."""
        indices = self._select_keyframes(recording.frames, recording.segments)
        cells: list[Image.Image] = []
        labels: list[str] = []
        keyframes: list[Keyframe] = []
        for index in indices:
            frame = recording.frames[index]
            image = self._composite_frame(recording, frame)
            if image is None:
                continue
            label = f"t={frame.t_ms / 1000.0:.2f}s"
            action = self._action_label(recording, frame.t_ms)
            if action is not None:
                label += f" {action}"
            labels.append(label)
            cells.append(image)
            keyframes.append(Keyframe(image_id="", t_ms=frame.t_ms, index=index))
        if not cells:
            return None, []
        sheet = self._grid(cells, labels)
        buffer = io.BytesIO()
        sheet.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("ascii"), keyframes

    def _grid(self, cells: list[Image.Image], labels: list[str]) -> Image.Image:
        """Pack the keyframe cells into a bounded grid (longest side ≤ cap)."""
        columns = max(1, min(4, len(cells)))
        rows = (len(cells) + columns - 1) // columns
        cell_w = max(cell.width for cell in cells)
        cell_h = max(cell.height for cell in cells) + _SHEET_LABEL_HEIGHT
        sheet = Image.new("RGB", (columns * cell_w + (columns + 1) * _SHEET_GAP,
                                  rows * cell_h + (rows + 1) * _SHEET_GAP), (18, 18, 18))
        draw = ImageDraw.Draw(sheet)
        for position, (cell, label) in enumerate(zip(cells, labels)):
            row, column = divmod(position, columns)
            x = _SHEET_GAP + column * (cell_w + _SHEET_GAP)
            y = _SHEET_GAP + row * (cell_h + _SHEET_GAP)
            sheet.paste(cell, (x, y))
            draw.text((x + 2, y + cell.height + 2), label, fill=(240, 240, 240))
        long_side = max(sheet.width, sheet.height)
        if long_side > RECORD_SHEET_LONG_SIDE_MAX:
            scale = RECORD_SHEET_LONG_SIDE_MAX / long_side
            sheet = sheet.resize(
                (max(1, round(sheet.width * scale)), max(1, round(sheet.height * scale))),
                Image.LANCZOS,
            )
        return sheet


# --- the MP4 encode (include_video) ------------------------------------------------------------


def _import_av() -> Any:
    """The lazy PyAV import seam: tests force ``encoder_unavailable`` here."""
    import av  # type: ignore[import-not-found]

    return av


def _decode_video_info(mp4_bytes: bytes) -> dict[str, Any]:
    """Decode a held MP4 back (the tests' round-trip probe) — count, size, fps, duration."""
    av = _import_av()
    with av.open(io.BytesIO(mp4_bytes)) as container:
        stream = container.streams.video[0]
        frames = sum(1 for _ in container.decode(stream))
        return {
            "frames": frames,
            "width": stream.codec_context.width,
            "height": stream.codec_context.height,
            "fps": float(stream.average_rate),
            "duration_s": float(container.duration) / 1_000_000.0
            if container.duration is not None
            else 0.0,
        }


def _iter_encode_frames(
    frames: list[RecordedFrame],
    recording: Recording,
    composite: bool,
    width: int,
    height: int,
    scale: float,
    fps: int,
) -> Iterator[tuple[Image.Image, int]]:
    """DELTA 1 M5 + DELTA 4 F1: decode → composite → resize ONE frame at a
    time (a lazy generator — no full RGB list ever exists), iterating the
    SNAPSHOT list taken once by :func:`encode_video` — a concurrent pool
    eviction can never turn a later rung into a zero-frame encode.  Yields
    ``(image, pts)`` with REAL timestamps (DELTA 1 m3: pts = t_ms in the
    stream's 1/fps time base, so dropped frames keep real-time duration)."""
    for frame in frames:
        image = Image.open(io.BytesIO(frame.jpeg))
        image.load()
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        if composite and frame.pointer_x is not None and frame.in_image:
            from .pointer_sprite import composite as sprite_composite

            origin_x, origin_y = recording.canonical_origin
            frame_scale = recording.frame_scale if recording.frame_scale > 0 else 1.0
            local_x = (frame.pointer_x - origin_x) * frame_scale
            local_y = (frame.pointer_y - origin_y) * frame_scale
            composited, drawn = sprite_composite(
                image, local_x, local_y, frame.pointer_state, frame_scale
            )
            if drawn:
                image = composited
        if abs(scale - 1.0) > 1e-9:
            image = image.resize(
                (
                    max(2, round(image.width * scale)),
                    max(2, round(image.height * scale)),
                ),
                Image.LANCZOS,
            )
        # yuv420p needs even dimensions: CROP to even (DELTA 1 nit — never a
        # distorting resize at parity).
        even_w = width - (width % 2)
        even_h = height - (height % 2)
        if image.size != (even_w, even_h):
            image = image.crop((0, 0, min(image.width, even_w), min(image.height, even_h)))
        pts = int(round(frame.t_ms / 1000.0 * fps))
        yield image, pts


def _encode_one(
    av_module: Any,
    recording: Recording,
    frames_snapshot: list[RecordedFrame],
    fps: int,
    width: int,
    height: int,
    encoder: str,
    quality: float | None,
    composite: bool,
    scale: float,
    should_abort: Callable[[], bool] | None = None,
) -> bytes | None:
    """One encode rung: ``encoder`` at even ``width``x``height``, the given
    quality (CRF for libx264, BITRATE for h264_mf — DELTA 1 item 11), frames
    pushed ONE at a time from the SNAPSHOT list (DELTA 4 F1) through the
    video plane's ``update(bytes)`` — no
    numpy.
    numpy.

    DELTA 2 n7/N4 + VAL-WP-D-01 M1: an encoder that cannot OPEN raises the typed
    :class:`EncoderOpenError` (the ladder falls back to the next encoder
    immediately) — and because PyAV opens the codec lazily, a failure on the
    FIRST ``stream.encode(frame)`` counts as an open failure too; a MID-encode
    failure (after at least one frame encoded) returns ``None`` after closing
    the container (also a fallback signal); an abort (``should_abort`` — checked
    between frames, N4) raises :class:`_EncodeAborted` after best-effort
    cleanup."""
    container: Any = None
    stream: Any = None
    try:
        buffer = io.BytesIO()
        container = av_module.open(buffer, "w", format="mp4")
        stream = container.add_stream(encoder, rate=int(fps))
        stream.width = width
        stream.height = height
        if encoder == "h264_mf":
            # Media Foundation wants NV12 and a bitrate (no CRF); the rungs
            # are BITRATE values, scaled by the resolution step squared.
            stream.pix_fmt = "nv12"
            if quality is not None:
                stream.bit_rate = int(quality * scale * scale)
        else:
            stream.pix_fmt = "yuv420p"
            if quality is not None:
                stream.options = {"crf": str(int(quality))}
    except _EncodeAborted:
        raise
    except Exception as exc:  # noqa: BLE001 - typed: the encoder could not OPEN
        if container is not None:
            try:
                container.close()
            except Exception:  # noqa: BLE001 - best-effort
                pass
        raise EncoderOpenError(f"{encoder} cannot open: {type(exc).__name__}: {exc}") from exc
    try:
        pushed = 0  # DELTA 4 F1: a rung that encodes ZERO frames must never
        # be reported as included — it is a failure (falls back / encode_failed).
        for image, pts in _iter_encode_frames(
            frames_snapshot, recording, composite, width, height, scale, fps
        ):
            if should_abort is not None and should_abort():
                raise _EncodeAborted()
            frame = av_module.VideoFrame(image.width, image.height, "rgb24")
            plane = frame.planes[0]
            row = image.width * 3
            if plane.line_size == row:
                plane.update(image.tobytes())
            else:
                raw = image.tobytes()
                padded = bytearray(plane.buffer_size)
                for y in range(image.height):
                    padded[y * plane.line_size : y * plane.line_size + row] = raw[
                        y * row : (y + 1) * row
                    ]
                plane.update(bytes(padded))
            # m3: REAL timestamps — dropped frames keep real-time duration.
            frame.pts = pts
            try:
                packets = stream.encode(frame)
            except _EncodeAborted:
                raise
            except Exception as exc:  # noqa: BLE001 - VAL-WP-D-01 M1
                # PyAV opens the codec LAZILY, on the FIRST stream.encode(frame)
                # — a failure there is an OPEN failure (EncoderOpenError: the
                # ladder falls back to the next encoder; every encoder failing
                # here ends as encoder_unavailable).  A failure AFTER at least
                # one frame encoded stays a MID-encode failure (None).
                try:
                    container.close()
                except Exception:  # noqa: BLE001 - best-effort close
                    pass
                if pushed == 0:
                    raise EncoderOpenError(
                        f"{encoder} failed on the first frame encode (codec open): "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                return None
            pushed += 1
            for packet in packets:
                container.mux(packet)
        if pushed == 0:
            # DELTA 4 F1: close the container — never leak the muxer.
            container.close()
            return None
        for packet in stream.encode():
            container.mux(packet)
        container.close()
        return buffer.getvalue()
    except _EncodeAborted:
        # NIT: close the container on failure — never leak the muxer.
        try:
            container.close()
        except Exception:  # noqa: BLE001 - best-effort
            pass
        raise
    except EncoderOpenError:
        # VAL-WP-D-01 M1: raised by the FIRST-frame-encode open failure inside
        # the loop — re-raise through the generic mid-encode handler below
        # (never classified as a mid failure).
        try:
            container.close()
        except Exception:  # noqa: BLE001 - best-effort
            pass
        raise
    except Exception:  # noqa: BLE001 - a MID-encode failure: fallback signal
        try:
            container.close()
        except Exception:  # noqa: BLE001 - best-effort
            pass
        return None


def _h264_mf_bitrate_rungs(target_bytes: int, duration_s: float) -> list[float]:
    """DELTA 2 n8: derive the h264_mf BITRATE rungs from the cap and the
    duration — ``cap × 8 / duration × 0.85`` for the first rung (which should
    normally fit), then ×0.7 steps.  Falls back to fixed rungs when the
    duration is unknown."""
    if duration_s <= 0:
        return [float(value) for value in H264_MF_BITRATE_RUNGS]
    base = (target_bytes * 8.0 / duration_s) * 0.85
    return [base, base * 0.7, base * 0.49]


def encode_video(
    recording: Recording,
    *,
    fps: int,
    composite: bool = True,
    target_bytes: int | None = None,
    importer: Callable[[], Any] | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    should_abort: Callable[[], bool] | None = None,
) -> tuple[bytes | None, dict[str, Any]]:
    """Encode the held JPEG frames into an in-memory H.264 MP4 (≤ ``target_bytes``).

    The cursor is composited into every frame before encoding when ``composite``
    is True (VAL-WP-D-01 M2: the caller passes the session's show_pointer
    default — False composites NO sprite).  The size ladder steps QUALITY
    before resolution at each size (DELTA 1 nit):
    at every resolution step (1.0, 0.75, … down to the 480 px floor) the
    encoder's full quality rung list is tried before the next downscale.  The
    encoder preference list is tried in order; the first rung that fits wins
    and the info reports the ``encoder`` actually used (DELTA 1 item 11).

    DELTA 2:
    * ``should_abort`` (N4) is consulted between rungs AND between frames —
      an abort returns promptly with ``{"omitted": "encode_failed",
      "detail": "aborted"}``.
    * n2: the ladder estimates the next rung's cost from the previous rung
      and skips it when the remaining budget cannot fit it (the deadline is
      never floored into a rung that cannot finish).
    * n7: an encoder that cannot OPEN raises the typed
      :class:`EncoderOpenError` — the ladder falls back to the NEXT encoder
      immediately; a MID-encode failure also falls back once (never retried
      across every rung of the broken encoder).  ``encoder_unavailable``
      means ONLY an import failure or every encoder failing to open; a
      mid-encode failure with no bytes at all is ``encode_failed``.
    * n8: the h264_mf bitrate rungs derive from ``cap × 8 / duration × 0.85``
      (×0.7 steps) — the first rung should normally fit the cap."""
    target = video_max_bytes() if target_bytes is None else int(target_bytes)
    # DELTA 4 F1: ONE SNAPSHOT of the held frames, taken once and passed to
    # EVERY rung (and to the libx264 fallback) — a concurrent pool eviction
    # that empties recording.frames mid-ladder can never turn a later rung
    # into a zero-frame encode.
    frames = list(recording.frames)
    if not frames:
        # DELTA 2 N3: the held frames were evicted under the pool's byte cap
        # before any video was stored — the honest omission is its own token.
        if recording.frames_released:
            return None, {
                "omitted": VIDEO_OMITTED_FRAMES_RELEASED,
                "bytes": 0,
                "detail": "the held frames were evicted under the pool's byte cap "
                "before any video was stored",
            }
        return None, {"omitted": VIDEO_OMITTED_ENCODE_FAILED, "bytes": 0}
    try:
        av_module = (importer or _import_av)()
    except Exception:  # noqa: BLE001 - import failure is the typed unavailable state
        return None, {"omitted": VIDEO_OMITTED_ENCODER_UNAVAILABLE, "bytes": 0}
    base_width = frames[0].width
    base_height = frames[0].height
    any_bytes_produced = False
    last_bytes = 0
    opened_any_encoder = False
    mid_failure = False
    # DELTA 3 n2: the FIRST rung's budget check uses a documented per-frame
    # estimate (no measured rung exists yet); later rungs use the measured
    # previous-rung cost.
    last_rung_seconds = len(frames) * RECORDER_PER_FRAME_ENCODE_ESTIMATE_SECONDS
    for encoder in encoder_preference():
        if encoder == "h264_mf":
            quality_rungs: list[float | None] = _h264_mf_bitrate_rungs(
                target, recording.actual_duration_s or recording.duration_s
            )
        else:
            quality_rungs = list(LIBX264_CRF_RUNGS)
        encoder_opened = False
        encoder_failed = False
        scale = 1.0
        while True:
            width = max(2, round(base_width * scale))
            height = max(2, round(base_height * scale))
            # yuv420p/nv12 need even dimensions — the RUNG dims are even from
            # the start (the iterator crops the frames to match).
            width -= width % 2
            height -= height % 2
            if max(width, height) < VIDEO_MIN_LONG_SIDE and scale < 1.0:
                break  # below the floor: never attempted
            for quality in quality_rungs:
                # M3/n2: the ladder stops when the next rung cannot fit — the
                # estimate is the previous rung's measured cost (floored at 0).
                if deadline is not None:
                    now = clock()
                    if now >= deadline or (
                        last_rung_seconds > 0.0 and now + last_rung_seconds > deadline
                    ):
                        return None, {
                            "omitted": VIDEO_OMITTED_OVER_BUDGET,
                            "bytes": last_bytes,
                        }
                if should_abort is not None and should_abort():
                    # DELTA 3 n3: an ABORT is NOT a final omission — the
                    # `aborted` marker keeps later result calls (in a live
                    # session) able to retry while the frames are held.
                    return None, {
                        "omitted": VIDEO_OMITTED_ENCODE_FAILED,
                        "bytes": last_bytes,
                        "detail": "aborted",
                        "aborted": True,
                    }
                rung_started = clock()
                try:
                    data = _encode_one(
                        av_module,
                        recording,
                        frames,
                        fps,
                        width,
                        height,
                        encoder,
                        quality,
                        composite,
                        scale,
                        should_abort,
                    )
                except EncoderOpenError:
                    # n7: cannot OPEN — skip the encoder's remaining rungs and
                    # fall back to the NEXT encoder ONCE.
                    encoder_failed = True
                    break
                except _EncodeAborted:
                    return None, {
                        "omitted": VIDEO_OMITTED_ENCODE_FAILED,
                        "bytes": last_bytes,
                        "detail": "aborted",
                        "aborted": True,
                    }
                except Exception:
                    # n7: any OTHER rung failure is a MID-encode failure —
                    # fall back to the next encoder immediately (never retried
                    # across every rung).
                    encoder_failed = True
                    break
                last_rung_seconds = max(0.0, clock() - rung_started)
                if data is None:
                    # n7: a MID-encode failure — fall back to the next
                    # encoder immediately (never retried across every rung).
                    mid_failure = True
                    encoder_failed = True
                    break
                encoder_opened = True
                opened_any_encoder = True
                any_bytes_produced = True
                last_bytes = len(data)
                if len(data) <= target:
                    info = {
                        "included": True,
                        "bytes": len(data),
                        "width": width,
                        "height": height,
                        "fps": fps,
                        "frames": recording.frame_count or len(frames),
                        "codec": "h264",
                        "encoder": encoder,
                    }
                    return data, info
            if encoder_failed:
                break  # fall back to the next encoder ONCE
            # quality exhausted at THIS resolution → the next resolution step
            if max(width, height) <= VIDEO_MIN_LONG_SIDE:
                break
            next_scale = scale * VIDEO_RESOLUTION_STEP
            if next_scale < 0.05:
                break
            scale = next_scale
        if encoder_opened:
            opened_any_encoder = True
        if any_bytes_produced:
            # This encoder opened and produced (oversize) bytes; the cap is
            # unreachable for it — do not pretend another codec would fit.
            break
    if any_bytes_produced:
        return None, {"omitted": VIDEO_OMITTED_TOO_LARGE, "bytes": last_bytes}
    if opened_any_encoder or mid_failure:
        # n7: encoders OPENED but every attempted rung failed mid-encode —
        # encode_failed (encoder_unavailable is ONLY for open/import failures).
        return None, {"omitted": VIDEO_OMITTED_ENCODE_FAILED, "bytes": 0}
    # No encoder could open (n7: the ONLY encoder_unavailable cases are an
    # import failure and a failure to open every encoder).
    return None, {"omitted": VIDEO_OMITTED_ENCODER_UNAVAILABLE, "bytes": 0}
