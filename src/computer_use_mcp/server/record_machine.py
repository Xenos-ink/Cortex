"""The background-recording machinery (LAYER 2): registries, handle, worker, cancel/join - no tool registrations here (the two tools live in record.py, LAYER 4).

WP-R (ORVEX-2026-109) + DELTA 1 (VAL-WP-R-01).  The registry/tombstone/cap
discipline is the wait family's port (``wait_machine.py``): an entry in
``_RECORDINGS_IN_FLIGHT`` exists IFF a worker is alive; finished recordings
live in the bounded ``_FINISHED_RECORDINGS`` ring; ids that fell out of it are
remembered in the bounded ``_EXPIRED_RECORDS`` tombstone ring so a stale id
answers ``expired`` rather than pretending never to have existed.  The worker
holds the session's WP-S in-flight mark through a WEAK reference to the
context, so a recording session is never idle-reaped, and retires its terminal
state BEFORE signalling (the F2 ordering).  This layer imports no server
sibling above it — exactly like ``wait_machine``, so the session-end funnel
(``lifecycle``) can import it without a cycle.

DELTA 1 changes:

* **B1**: the worker passes the TOOL's ``record_id`` into the recorder — the
  reply payload, resource URI, saved filenames and pool keys all carry it.
* **B3**: a capture error (``recording.error``) or ANY worker exception files
  a terminal FAILED outcome (``result:"failed"``, reasons
  ``["capture_failed" | "record_worker_failed"]`` plus the error text), one
  terminal audit event with result ``"failed"`` (never ``"ok"``), and wakes
  blocked callers — nobody ever sees ``verified`` for a broken recording.
* **M3/B2**: the encode runs in the worker right after capture (joinable,
  inside the busy mark) when the start call asked for video; the flags are
  persisted on the finished Recording.
* **m1**: a stop seen between registration and thread start makes the worker
  exit without grabbing; the encode is inside the worker, so nothing outlives
  the join.
* **m8**: the frames' JPEG payloads are released once the keyframes and the
  video are stored (the finished ring never holds ~10-20 MB of JPEGs outside
  any cap), and the session's action ring is pruned at teardown.
"""

from __future__ import annotations

import base64
import threading
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .instance import logger
from .state import _bundles, _lock
from .wait_machine import _signal_loop_waiters
from ..vision.recorder import (
    RECORDER_JOIN_TIMEOUT_SECONDS,
    prune_action_ring,
)
from ..vision.recorder import encode_video as _encode_video

if TYPE_CHECKING:  # annotation-only imports (the no-bare-reexport gate)
    from ..vision.evidence import RecordingEvidenceStore
    from ..vision.recorder import Recording, ScreenRecorder

#: session_id -> record_id -> live handle. An entry exists IFF a worker is alive.
_RECORDINGS_IN_FLIGHT: dict[str, dict[str, "_RecordHandle"]] = {}

#: session_id -> ordered record_id -> finished Recording (the reply source).
#: Bounded at MAX_FINISHED_RECORDINGS_PER_SESSION, oldest evicted first.  The
#: recordings held here carry NO frame JPEG payloads (released once the
#: keyframes and the video are stored — DELTA 1 m8).
_FINISHED_RECORDINGS: dict[str, "OrderedDict[str, Recording]"] = {}

#: session_id -> ordered record_id -> expiry reason. The bounded tombstone ring:
#: without it, "no longer retained" is indistinguishable from "never existed".
_EXPIRED_RECORDS: dict[str, "OrderedDict[str, str]"] = {}

#: Caps: 1 active recording per session, 2 per process.
MAX_RECORDINGS_PER_SESSION = 1
MAX_RECORDINGS_PER_PROCESS = 2

#: Finished recordings + expired tombstones retained per session.
MAX_FINISHED_RECORDINGS_PER_SESSION = 4
RECORDING_TOMBSTONES_PER_SESSION = 4


def _record_now() -> float:
    """The monotonic clock every recording decision reads (injectable seam)."""
    return time.monotonic()


def _record_iso_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_rejection(code: str, message: str) -> dict[str, object]:
    """The typed rejection envelope for the record family (unified contract §1.3)."""
    return {"ok": False, "result": "rejected", "reasons": [code], "message": message}


def _record_verification() -> dict[str, Any]:
    """A recording is a non-screen claim: verified by construction (the marker
    semantics of the flat ``done``/``wait`` and the wait family)."""
    return {
        "outcome": "verified",
        "changed": False,
        "confidence": 1.0,
        "evidence": ["no_op_marker: computer_record bookkeeping, no screen claim"],
        "verification_method": "no_op_marker",
        "note": (
            "A recording dispatches no input and asserts no screen transition; "
            "the capture itself is the product."
        ),
    }


def _session_alive(session_id: str) -> bool:
    """Liveness under the module lock, read through THIS module's ``_bundles``
    binding (the one the test fixtures re-point — a tool must never keep its
    own binding of the bundle map)."""
    with _lock:
        return session_id in _bundles


def reset_registries() -> None:
    """DELTA 1 m11 (test isolation): drop every registry — in-flight handles,
    finished recordings, expired tombstones.  Called by the ``fresh_server``
    fixture; production teardown uses ``_cancel_all_recordings`` instead."""
    with _lock:
        _RECORDINGS_IN_FLIGHT.clear()
        _FINISHED_RECORDINGS.clear()
        _EXPIRED_RECORDS.clear()


@dataclass
class _RecordHandle:
    """One in-flight recording.  NO strong reference to the session bundle —
    only a WEAK reference to its ``SessionContext`` (the WP-S in-flight mark),
    so a recording can never keep a stopped session's memory alive."""

    record_id: str
    session_id: str
    duration_s: float
    fps: int
    max_keyframes: int
    region_requested: bool
    include_video: bool
    save: bool
    started_at: str
    started_epoch: float
    deadline_monotonic: float
    recorder: "ScreenRecorder | None" = None
    context_ref: "weakref.ref[Any] | None" = None
    loop: Any | None = None
    waiters: "set[Any]" = field(default_factory=set)
    waiters_lock: threading.Lock = field(default_factory=threading.Lock)
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    outcome: Any = None  # vision.recorder.Recording once finished
    # DELTA 2 n1: True while the worker's post-capture encode runs — the
    # reply's status is "encoding" (never "recording, 0.0 s remaining").
    encoding: bool = False
    #: WP-E E2: the start call's video delivery ("file" default | "inline"),
    #: persisted onto the finished Recording like include_video/save (B2).
    video_delivery: str = "file"
    # The session's recording pool + a best-effort audit emitter, captured at
    # start (the worker may outlive the bundle).
    pool: "RecordingEvidenceStore | None" = None
    audit: Any = None

    @property
    def ends_at(self) -> float:
        return self.started_epoch + self.duration_s


def _retire_record(handle: _RecordHandle) -> None:
    """THE single registry-exit for a live recording (removed in one place)."""
    with _lock:
        registry = _RECORDINGS_IN_FLIGHT.get(handle.session_id)
        if registry is not None:
            registry.pop(handle.record_id, None)
            if not registry:
                _RECORDINGS_IN_FLIGHT.pop(handle.session_id, None)


def _tombstone_finished(session_id: str, record_id: str, recording: "Recording") -> None:
    """Move a finished recording into the bounded finished ring (only while the
    session is live — a stopped session performs no further work of any kind)."""
    with _lock:
        if session_id not in _bundles:
            return
        ring = _FINISHED_RECORDINGS.setdefault(session_id, OrderedDict())
        ring[record_id] = recording
        while len(ring) > MAX_FINISHED_RECORDINGS_PER_SESSION:
            evicted_id, _ = ring.popitem(last=False)
            expired = _EXPIRED_RECORDS.setdefault(session_id, OrderedDict())
            expired[evicted_id] = "retention_cap"
            while len(expired) > RECORDING_TOMBSTONES_PER_SESSION:
                expired.popitem(last=False)


def _file_failed_outcome(
    handle: _RecordHandle, recording: "Recording | None", kind: str, detail: str
) -> "Recording":
    """B3: file a TERMINAL FAILED outcome for a broken recording.

    The outcome is tombstoned like any finished recording, so a caller already
    blocked on the recording WAKES and sees ``result:"failed"`` (never a
    ``marker`` claiming the recording is still running, and never
    ``unknown_record`` afterwards).  ``kind`` is ``"capture_failed"`` (the
    grabber raised) or ``"record_worker_failed"`` (any worker exception)."""
    if recording is None:
        from ..vision.recorder import Recording

        recording = Recording(
            record_id=handle.record_id,
            session_id=handle.session_id,
            duration_s=handle.duration_s,
            fps=handle.fps,
            requested_monotonic_start=handle.deadline_monotonic - handle.duration_s,
        )
    recording.error = detail
    recording.error_kind = kind
    _tombstone_finished(handle.session_id, handle.record_id, recording)
    handle.outcome = recording
    return recording


def _record_worker(handle: _RecordHandle) -> None:
    """The recording thread: captures, encodes (when asked), finalizes.

    Holds the session's WP-S in-flight mark (via the weak-referenced context)
    for the WHOLE recording, so a recording session is never idle-reaped.  The
    terminal state is tombstoned BEFORE anything wakes a blocked caller (the
    wait family's F2 ordering).  DELTA 1 M3: the encode runs HERE — inside the
    busy mark and the bounded join — so the result call usually only fetches.
    """
    context = handle.context_ref() if handle.context_ref is not None else None
    if context is not None:
        context.touch_activity()
        context.mark_busy()
    recording: "Recording | None" = None
    outcome_kind = "record_worker_failed"
    try:
        assert handle.recorder is not None
        # m1: a stop seen between registration and thread start — the cancel
        # event is already set; the recorder exits WITHOUT grabbing.
        recording = handle.recorder.run()
        # The reply's region honesty: monitor-mode recordings report no region.
        if not handle.region_requested:
            recording.region = None
        # B2: persist the START call's flags on the finished Recording — a
        # later result call honours them after the handle is gone.
        recording.want_video = handle.include_video
        recording.save = handle.save
        # WP-E E2: the delivery flag persists the same way (a late result call
        # honours the START call's choice when it passes none of its own).
        recording.video_delivery = str(getattr(handle, "video_delivery", "file") or "file")
        if recording.error:
            # B3(a): the capture failed — the product is untrustworthy.
            outcome_kind = "capture_failed"
        pool = handle.pool
        if pool is not None and not recording.error:
            video_stored = False
            # N4: BEFORE encoding, skip when a stop landed, the pool is
            # already disposed (teardown), or the session is gone — a dead
            # session never pays for an encode.
            encode_allowed = (
                recording.want_video
                and not handle.cancel.is_set()
                and not pool.disposed
                and _session_alive(handle.session_id)
            )
            handle.encoding = encode_allowed
            if encode_allowed:
                # M3 (preferred shape): encode right after capture, inside the
                # worker — the join budget covers it; the result call only
                # fetches.  N4: the encode itself carries a stop check
                # (consulted between rungs AND frames), so a stop aborts it
                # promptly.
                data, info = _encode_video(
                    recording,
                    fps=recording.fps,
                    # VAL-WP-D-01 M2: the session's show_pointer default rides
                    # the Recording — OFF composites no sprite into the MP4.
                    composite=bool(getattr(recording, "composite_pointer", True)),
                    should_abort=lambda: (
                        handle.cancel.is_set()
                        or pool.disposed
                        or not _session_alive(handle.session_id)
                    ),
                )
                recording.video = info
                if data is not None:
                    video_stored = pool.store_video(
                        record_id=handle.record_id,
                        mp4_bytes=data,
                        width=int(info.get("width", 0)),
                        height=int(info.get("height", 0)),
                        fps=recording.fps,
                        frames=int(info.get("frames", recording.frame_count)),
                    )
            handle.encoding = False
            for keyframe in recording.keyframes:
                frame = recording.frames[keyframe.index]
                jpeg_b64 = base64.b64encode(frame.jpeg).decode("ascii")
                # DELTA 1 M2 + DELTA 2 N1: the pointer is stored in FRAME
                # space, with the frame→canonical metadata (scale + origin)
                # — fetch composites at exactly those pixels and reports the
                # pointer in CANONICAL coordinates via the published mapping.
                # VAL-WP-D-01 M2: with the session's show_pointer OFF no
                # pointer metadata is stored at all (no sprite will ever be
                # drawn for this recording — fetch respects the same default).
                scale = recording.frame_scale if recording.frame_scale > 0 else 1.0
                composite_pointer = bool(getattr(recording, "composite_pointer", True))
                pointer_fx = pointer_fy = None
                if composite_pointer and frame.pointer_x is not None and frame.pointer_y is not None:
                    pointer_fx = (frame.pointer_x - recording.canonical_origin[0]) * scale
                    pointer_fy = (frame.pointer_y - recording.canonical_origin[1]) * scale
                image_id = pool.store_keyframe(
                    record_id=handle.record_id,
                    jpeg_base64=jpeg_b64,
                    width=frame.width,
                    height=frame.height,
                    t_ms=round(frame.t_ms, 1),
                    pointer_x=pointer_fx,
                    pointer_y=pointer_fy,
                    pointer_state=(
                        frame.pointer_state if composite_pointer and frame.in_image else None
                    ),
                    frame_scale=scale,
                    origin=recording.canonical_origin,
                )
                keyframe.image_id = image_id or ""
            # DELTA 2 N3: the held JPEGs are KEPT (include_video on the RESULT
            # call alone must work) but ACCOUNTED under the pool's byte cap —
            # bounded memory, honest eviction.  They are released only when
            # the pool evicts them (see record.py's has_frames check), which
            # makes a later include_video answer ``frames_released``.
            total_frame_bytes = sum(len(frame.jpeg) for frame in recording.frames)
            if total_frame_bytes > 0:
                # DELTA 3 BLOCKER: the frames entry carries a WEAK owner
                # reference — pool eviction FREES the Recording's JPEGs (no
                # phantom memory).  If the frames do not fit even after
                # other held-frames entries were dropped (this recording's
                # own keyframes and video are never evicted for them), the
                # frames are RELEASED at once and the recording is marked
                # ``frames_released`` (frames_accounted stays False — the
                # honest "not accounted" state).
                if pool.store_frames(
                    record_id=handle.record_id,
                    total_bytes=total_frame_bytes,
                    owner=recording,
                ):
                    recording.frames_accounted = True
                else:
                    recording.release_frame_payloads()
        _tombstone_finished(handle.session_id, handle.record_id, recording)
        handle.outcome = recording
        outcome_kind = None  # a healthy (or honestly cancelled) recording
    except Exception as exc:  # noqa: BLE001 - B3(b): a worker fault is a typed outcome
        logger.warning("recording worker failed", exc_info=True)
        recording = _file_failed_outcome(
            handle, recording, outcome_kind, f"{type(exc).__name__}: {exc}"
        )
        outcome_kind = None  # the failed outcome IS the terminal event
    finally:
        if handle.audit is not None and recording is not None:
            try:
                if recording.error:
                    # B3: exactly one terminal audit event, result "failed".
                    handle.audit(
                        "record_complete",
                        result="failed",
                        metadata={
                            "record_id": handle.record_id,
                            "reason": recording.error_kind or "record_worker_failed",
                            "error": str(recording.error or "")[:300],
                            "cancelled": bool(recording.cancelled),
                        },
                    )
                elif recording.cancelled:
                    handle.audit(
                        "record_cancelled",
                        result="cancelled",
                        metadata={
                            "record_id": handle.record_id,
                            "frames": recording.frame_count,
                            "dropped_frames": recording.dropped_frames,
                            "effective_fps": recording.effective_fps,
                        },
                    )
                else:
                    handle.audit(
                        "record_complete",
                        result="ok",
                        metadata={
                            "record_id": handle.record_id,
                            "frames": recording.frame_count,
                            "dropped_frames": recording.dropped_frames,
                            "effective_fps": recording.effective_fps,
                        },
                    )
            except Exception:  # noqa: BLE001 - audit failures never break the reply path
                logger.debug("record audit failed", exc_info=True)
        try:
            _retire_record(handle)
        finally:
            if context is not None:
                context.touch_activity()
                context.mark_idle()
            handle.done.set()
            _signal_loop_waiters(handle)


def _cancel_all_recordings(session_id: str) -> list[_RecordHandle]:
    """Flag every live recording of a session for cancellation.  SET ONLY, NEVER
    JOINS — called from ``_close_stopped_bundle`` INSIDE the locked teardown; the
    join belongs outside the lock (``_join_record_threads``, the K-5 invariant).
    The session's finished and expired rings are dropped here too (a stopped
    session performs no further work of any kind), and the action ring is
    pruned (DELTA 1 m8)."""
    with _lock:
        registry = _RECORDINGS_IN_FLIGHT.pop(session_id, {})
        handles = list(registry.values())
        for handle in handles:
            handle.cancel.set()
            _signal_loop_waiters(handle)
        _FINISHED_RECORDINGS.pop(session_id, None)
        _EXPIRED_RECORDS.pop(session_id, None)
    prune_action_ring(session_id)
    return handles


def _join_record_threads(
    handles: list[_RecordHandle], timeout: float = RECORDER_JOIN_TIMEOUT_SECONDS
) -> None:
    """Join retired recording workers.  MUST be called OUTSIDE the module lock
    (the K-5 invariant, mechanically guarded like ``_join_wait_threads``)."""
    if getattr(_lock, "_is_owned", lambda: False)():
        raise RuntimeError(
            "recording join attempted while holding the module lock (K-5 deadlock)"
        )
    for handle in handles:
        thread = handle.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
