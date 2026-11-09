"""The screen-recording tools (LAYER 4): computer_record + computer_record_result (machinery in record_machine.py, LAYER 2; the recorder core in vision.recorder, LAYER 2).

WP-R (ORVEX-2026-109) + DELTA 1 (VAL-WP-R-01): the agent records 0.5–10 s of
the screen IN THE BACKGROUND while it keeps calling any tool, then collects
the result in three forms — a change timeline plus the window's Cortex
actions, a contact sheet of timestamped keyframes (each fetchable at full
size by ``image_id``), and on request the FULL video — as a written MP4 file
path (the ``"file"`` delivery default, WP-E E2) or as an MP4
``EmbeddedResource`` (``"inline"``, only for hosts known to pass video to
the model).

Both tools dispatch NO input, so they stay ALLOWED while a plan lease is
active (recording a plan's effect is a main use — see ``plan_surface``'s
allow side).

DELTA 1 load-bearing behaviour:

* **One id end to end (B1)**: the tool mints the ``record_id`` and the
  recorder, the reply payload, the resource URI, the saved filenames and the
  pool keys all carry exactly that id.
* **Start flags persist (B2)**: ``include_video``/``save`` are stored on the
  finished Recording, so collecting AFTER the recording ended still produces
  the video and the files; an explicit ``include_video`` on the result call
  still wins.
* **Failures surface (B3)**: a capture error or any worker exception files a
  terminal ``result:"failed"`` outcome with reasons
  ``["capture_failed" | "record_worker_failed"]`` — a blocked caller wakes
  and sees it; exactly one terminal audit event, result ``"failed"``.
* **Client budget (M3)**: the video is encoded in the worker right after
  capture (the result call usually only fetches); a fallback encode carries a
  deadline (client budget minus elapsed, minus a safety margin) and returns
  ``video {"omitted": "over_budget"}`` when the next ladder rung cannot fit.
* **One heavy worker (M4)**: the sheet ladder, the blob base64, the save AND
  any backend observe run in ONE ``asyncio.to_thread`` worker — never on the
  loop.
* **Per-reply video entry (M7)**: ``"included"`` appears only when a blob is
  attached to THIS reply; no rung produced bytes ⇒ ``encode_failed``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import tempfile
import threading
import time
import uuid
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.types import BlobResourceContents, EmbeddedResource, ImageContent, TextContent

from ..core.limits import (
    RECORD_DURATION_BAND,
    RECORD_FPS_BAND,
    RECORD_FPS_DEFAULT,
    RECORD_MAX_KEYFRAMES_BAND,
    RECORD_MAX_KEYFRAMES_DEFAULT,
)
from ..desktop.client_budget import client_timeout_seconds
from ..vision.evidence import recording_store
from ..vision.recorder import (
    GrabberHandle,
    MssFrameGrabber,
    ScreenRecorder,
    VIDEO_FINAL_OMISSIONS,
    VIDEO_OMITTED_FRAMES_RELEASED,
    VIDEO_OMITTED_NOT_REQUESTED,
    VIDEO_OMITTED_WRITE_FAILED,
    RECORD_STATUS_ENCODING,
    encode_video,
)
from .instance import logger, mcp
from .lifecycle import _get_live_bundle, _session_scoped
from .pointer_reply import session_show_pointer
from .record_machine import (
    MAX_FINISHED_RECORDINGS_PER_SESSION,
    MAX_RECORDINGS_PER_PROCESS,
    MAX_RECORDINGS_PER_SESSION,
    _EXPIRED_RECORDS,
    _FINISHED_RECORDINGS,
    _RECORDINGS_IN_FLIGHT,
    _RecordHandle,
    _record_iso_utc,
    _record_now,
    _record_rejection,
    _record_verification,
    _session_alive,
)
from .replies import IMAGE_OUTBOUND_SCALE_KEY, _bound_outbound_image
from .schemas import (
    NullableBool,
    NullableInt,
    NullableRegion,
    NullableStr,
    RecordDuration,
    _coerce_tolerant_int,
)
from .state import _error_response, _lock, _StoppedSession, _UnknownSession
from .wait_machine import (
    _await_wait_wake,
    _signal_loop_waiters,
    _wait_segment_seconds,
)

#: M3: the safety margin subtracted from the client budget for the fallback
#: encode deadline (on top of the elapsed time of the call itself).
RECORD_RESULT_SAFETY_MARGIN_SECONDS = 5.0

#: WP-E E1: the PRE-ROLL captured before the execute call's first input is
#: dispatched.  The ``record`` object on ``computer_execute`` starts the
#: recorder thread just before the action runs; this bounded sleep (part of the
#: recording window, never added on top of ``duration_s``) guarantees the
#: "before" state reaches the frames, so the timeline shows what the action
#: CHANGED rather than only its aftermath.  Field evidence (sess_a59fef27,
#: chess on Chess.com): a recording started in a SEPARATE call always ended
#: 15–30 s before the model's next tool call — recording around the action, in
#: the same call, is the only shape that works with real models.
RECORD_PRE_ROLL_SECONDS = 0.3

#: WP-E E2: the closed ``video_delivery`` set ("file" is the DEFAULT — most
#: hosts truncate large tool results, so the MP4 travels as a written file
#: path; "inline" is the legacy embedded-resource blob, only for hosts known
#: to pass video to the model).
VIDEO_DELIVERY_CHOICES = ("file", "inline")
VIDEO_DELIVERY_DEFAULT = "file"

#: WP-E DELTA 1 (m7): the closed set of ``started: false`` reasons the execute
#: surface's ``record`` block can carry.  docs/REPLY-CONTRACT.md lists exactly
#: this set (the truth test derives it from here):
#: - ``recording_limit``: a recording cap (1 per session / 2 per process) was
#:   already occupied — the ACTION still ran.
#: - ``text_session_no_screen``: image_delivery=text has no screen to record.
#: - ``invalid_region``: the region could not be resolved at launch time (a
#:   monitor/coordinate race after the pre-validation).
#: - ``session_stopped``: the session stopped while the recording was starting.
#: - ``record_start_failed``: any unexpected launch error (m5: the action still
#:   ran — the launch is guarded end to end).
#: - ``no_action`` (n7): ``plan_action="stop"`` or a continue past the walk's
#:   end — the call dispatches no input, so nothing is recorded.
EXECUTE_RECORD_NOT_STARTED_REASONS = (
    "recording_limit",
    "text_session_no_screen",
    "invalid_region",
    "session_stopped",
    "record_start_failed",
    "no_action",
)

#: The allowed keys inside the execute surface's ``record`` object (DELTA 1
#: n4/m3: an unknown key is a typed rejection — consistent with the
#: unknown-argument rule — instead of a silent ignore).
EXECUTE_RECORD_ALLOWED_KEYS = (
    "duration_s",
    "region",
    "fps",
    "max_keyframes",
    "include_video",
    "save",
    "video_delivery",
)


def _valid_video_delivery(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value in VIDEO_DELIVERY_CHOICES)


def recordings_root() -> Path:
    """Where ``save=True`` writes recordings: a ``recordings/`` subfolder of the
    audit-log directory (the ONE place Cortex writes files by convention)."""
    override = os.getenv("COMPUTER_USE_MCP_LOG_DIR")
    base = Path(override) if override else Path(tempfile.gettempdir()) / "cortex" / "logs"
    return base / "recordings"


def _record_marker_reply(
    *,
    session_id: str,
    handle: "_RecordHandle",
    message: str,
    extra: dict[str, Any] | None = None,
    status: str = "recording",
) -> dict[str, object]:
    now = _record_now()
    started_monotonic = handle.deadline_monotonic - handle.duration_s
    elapsed = max(0.0, now - started_monotonic)
    remaining = max(0.0, handle.deadline_monotonic - now)
    reply: dict[str, object] = {
        "ok": True,
        "result": "marker",
        "session_id": session_id,
        "record_id": handle.record_id,
        "status": status,
        "duration_s": handle.duration_s,
        "fps": handle.fps,
        "ends_at": handle.ends_at,
        "elapsed_seconds": round(elapsed, 3),
        "remaining_seconds": round(remaining, 3),
        "message": message,
        "verification": _record_verification(),
    }
    if extra:
        reply.update(extra)
    return reply


# --- grabber/pointer wiring -------------------------------------------------------------------


def _resolve_grabber(backend: Any) -> tuple[Any, Any]:
    """``(grabber, cursor)`` for the session's backend.

    DELTA 1 M6: the recorder receives a CLOSEABLE grabber OBJECT (never a bare
    bound method, whose ``close`` lookup finds nothing and leaks the mss GDI
    resources).  A backend that supplies its own recording grabber (the fake
    backend) is wrapped in :class:`GrabberHandle` — fake sessions record
    through the fake grabber, never the real desktop.  Otherwise the worker
    builds its OWN mss instance (:class:`MssFrameGrabber`, closed on every end
    path) and reads the pointer with the same read-only ``GetCursorPos`` every
    observation uses.
    """
    grab = getattr(backend, "record_frame_grab", None)
    if callable(grab):
        grabber = GrabberHandle(grab)
    else:
        grabber = MssFrameGrabber(getattr(backend, "_mss_factory", None))
    cursor = getattr(backend, "record_cursor_position", None)
    if not callable(cursor):
        from ..desktop.windows import get_cursor_position

        cursor = get_cursor_position
    return grabber, cursor


def _invalid_region(message: str) -> dict[str, object]:
    """The typed ``invalid_region`` boundary error (A1.3's existing shape)."""
    return {"ok": False, "error": "invalid_region", "message": message}


def _region_shape_error(region: Any) -> str | None:
    """Pre-flight shape validation (mirrors the zoom region's, fail-closed)."""
    if region is None or isinstance(region, (str, bytes, dict)) or not isinstance(
        region, (list, tuple)
    ):
        return (
            "region must be a [left, top, width, height] sequence of 4 ints in canonical "
            f"screenshot space; got {region!r}."
        )
    if len(region) != 4:
        return (
            "region must be a [left, top, width, height] sequence of exactly 4 ints; "
            f"got {len(region)} entries: {region!r}."
        )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in region):
        return (
            "region entries must all be ints in canonical screenshot space; got "
            f"{region!r}. Recording never coerces a guess."
        )
    _left, _top, width, height = region
    if width <= 0 or height <= 0:
        return f"region width and height must be > 0; got {region!r}."
    return None


def _resolve_recording_space(
    bundle: Any, region: Any
) -> tuple[dict[str, Any], tuple[int, int, int, int], tuple[int, int, int, int], Any, bool] | dict[str, object]:
    """Resolve the canonical/physical rectangles for the recording.

    Returns ``(context_info, region_physical, canonical_rect, transform,
    region_requested)`` — or a typed ``invalid_region`` error dict.  A
    canonical region is mapped to physical through the ONE transform
    (``physical = origin + screenshot * scale``); malformed, out-of-bounds or
    UNVERIFIABLE input is refused fail-closed — in BOTH monitor and region
    mode (DELTA 1 m7).  The capture-context observe (needed only when the
    session was never observed) runs OFF the loop via the caller (M4).
    """
    backend = bundle.backend
    context = getattr(backend, "_active_context", None)
    if context is None:
        # No capture context yet (nothing observed this session): one observe
        # establishes the monitor + verified scale the recording is mapped by.
        try:
            backend.observe()
        except Exception as exc:  # noqa: BLE001 - structured error, no traceback
            return _error_response(exc)
        context = getattr(backend, "_active_context", None)
    if context is None:
        return _invalid_region(
            "the backend provides no capture context; recording cannot establish "
            "the coordinate transform (fail-closed)."
        )
    monitor = context.monitor
    verdict = context.verdict
    transform = context.transform
    if getattr(verdict.space, "name", "") == "UNVERIFIABLE":
        # m7: BOTH modes refuse an unverifiable space — the pointer mapping and
        # the region mapping are equally untrustworthy there.
        return _invalid_region(
            "the observation's coordinate space is unverifiable; recording "
            "coordinates cannot be trusted (the same refusal doctrine as "
            "coordinate actions)."
        )
    screenshot_w = int(round(verdict.input_width / max(verdict.scale_x, 1e-6)))
    screenshot_h = int(round(verdict.input_height / max(verdict.scale_y, 1e-6)))
    if region is None:
        canonical_rect = (0, 0, screenshot_w, screenshot_h)
        physical = tuple(monitor.bounds)
        info: dict[str, Any] = {
            "region": None,
            "monitor_bounds": list(monitor.bounds),
            "canonical_size": [screenshot_w, screenshot_h],
        }
        return info, physical, canonical_rect, transform, False
    shape_error = _region_shape_error(region)
    if shape_error is not None:
        return _invalid_region(shape_error)
    left, top, width, height = (int(value) for value in region)
    if (
        left < 0
        or top < 0
        or left + width > screenshot_w
        or top + height > screenshot_h
    ):
        return _invalid_region(
            f"region {list(region)} is not fully inside the monitor screenshot space "
            f"{screenshot_w}x{screenshot_h}; recording never clips and never falls "
            "back to a full frame."
        )
    physical_left, physical_top = transform.to_physical(left, top)
    physical_w = max(1, int(round(width * verdict.scale_x)))
    physical_h = max(1, int(round(height * verdict.scale_y)))
    info = {
        "region": [left, top, width, height],
        "monitor_bounds": list(monitor.bounds),
        "canonical_size": [screenshot_w, screenshot_h],
    }
    return (
        info,
        (physical_left, physical_top, physical_w, physical_h),
        (left, top, width, height),
        transform,
        True,
    )


# --- the reply builders -----------------------------------------------------------------------


def _resolve_flags(
    recording: Any,
    handle: "_RecordHandle | None",
    include_video: bool | None,
    video_delivery: str | None = None,
) -> tuple[bool, bool, str]:
    """B2 + WP-E E2: the result call's explicit ``include_video`` wins; otherwise
    the flags persisted from the START call (on the handle while in flight, on the
    finished Recording afterwards) decide.  ``save`` has no result-call
    parameter — it always comes from the start call.  ``video_delivery``
    resolves the same way (explicit > start call > the "file" default)."""
    if include_video is not None:
        want_video = bool(include_video)
    elif handle is not None:
        want_video = bool(handle.include_video)
    else:
        want_video = bool(getattr(recording, "want_video", False))
    if handle is not None:
        save = bool(handle.save)
    else:
        save = bool(getattr(recording, "save", False))
    if video_delivery in VIDEO_DELIVERY_CHOICES:
        delivery = video_delivery
    elif handle is not None and getattr(handle, "video_delivery", None) in VIDEO_DELIVERY_CHOICES:
        delivery = str(handle.video_delivery)
    else:
        delivery = str(getattr(recording, "video_delivery", VIDEO_DELIVERY_DEFAULT) or VIDEO_DELIVERY_DEFAULT)
    return want_video, save, delivery


def _video_entry(
    recording: Any,
    *,
    blob_attached: bool,
    want_video: bool,
) -> dict[str, Any]:
    """M7: the JSON ``video`` entry is built PER REPLY — ``"included"`` appears
    only when a blob is attached to THIS reply.  When video was wanted but no
    bytes exist, the encode's own omission reason rides (the closed set);
    when video was never wanted, ``not_requested``."""
    if blob_attached:
        info = dict(getattr(recording, "video", None) or {})
        info["included"] = True
        info.setdefault("codec", "h264")
        return info
    if not want_video:
        return {"omitted": VIDEO_OMITTED_NOT_REQUESTED, "bytes": 0}
    info = dict(getattr(recording, "video", None) or {})
    if info.get("omitted") in (
        "too_large",
        "encoder_unavailable",
        "encode_failed",
        "over_budget",
        "frames_released",
        "write_failed",
    ):
        return info
    return {"omitted": "encode_failed", "bytes": 0}


def _finished_payload(
    session_id: str,
    recording: Any,
    *,
    video_entry: dict[str, Any],
) -> dict[str, Any]:
    """The finished recording's JSON metadata (no sheet bytes, no blob)."""
    return {
        "record_id": recording.record_id,
        "session_id": session_id,
        "duration_ms": round(recording.actual_duration_s * 1000.0, 1),
        "effective_fps": recording.effective_fps,
        "dropped_frames": recording.dropped_frames,
        "frames": recording.frame_count,
        "region": recording.region,
        "monitor_bounds": recording.monitor_bounds,
        "cancelled": bool(recording.cancelled),
        "timeline": recording.timeline(),
        "actions": recording.actions,
        "keyframes": [
            {"image_id": keyframe.image_id, "t_ms": round(keyframe.t_ms, 1)}
            for keyframe in recording.keyframes
            if keyframe.image_id
        ],
        "video": video_entry,
        "fetch_note": (
            "Fetch any keyframe at full size with computer_fetch_image(session_id, "
            "image_id); the cursor sprite is composited onto the served copy "
            "(recording keyframes are stored in FRAME space — the sprite lands "
            "exactly where it was drawn at capture)."
        ),
    }


def _stop_check_for(bundle: Any, recording: Any) -> Callable[[], bool]:
    """N4: the fallback encode's stop check — session gone means abort."""

    def _check() -> bool:
        return not _session_alive(str(getattr(recording, "session_id", "")))

    return _check


def _write_video_file(path: Path, data: bytes) -> None:
    """WP-E E2 seam: write the delivered MP4 file.  DELTA 1 m6: the write is
    ATOMIC — a temp file in the SAME folder, then ``os.replace`` — so a host
    reading the path can never observe a truncated MP4.  Tests patch THIS to
    force the closed ``write_failed`` omission without touching the real
    filesystem semantics."""
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _sweep_stale_tmp_files(root: Path, *, max_age_seconds: float = 3600.0) -> int:
    """DELTA 2 NIT + DELTA 3 NIT: best-effort remove stale ``.tmp-*``
    leftovers of a hard kill in the recordings folder (older than
    ``max_age_seconds``).  The pattern is narrowed to CORTEX'S OWN temp names
    — ``rec_*.mp4.tmp-*``, exactly how :func:`_write_video_file` names them —
    so a foreign ``*.tmp-*`` file is never touched; never raises."""
    removed = 0
    try:
        now = time.time()
        for candidate in root.glob("rec_*.mp4.tmp-*"):
            try:
                if now - candidate.stat().st_mtime > max_age_seconds:
                    candidate.unlink()
                    removed += 1
            except OSError:
                continue
    except Exception:  # noqa: BLE001 - best-effort housekeeping
        return removed
    return removed


#: DELTA 3 NIT: the fallback lock for a non-``Recording`` object (production
#: always passes a Recording whose lock is created at build time — this only
#: keeps the defensive ``getattr`` path from ever raising).
_FALLBACK_VIDEO_FILE_LOCK = threading.Lock()


def _write_delivered_video_once(recording: Any, video_bytes: bytes) -> str | None:
    """DELTA 1 m6 + DELTA 2 N3/N4: write the delivered MP4 ONCE per recording
    and reuse the path on every later collect.

    Concurrency (N3): the lock is the Recording's OWN ``_video_file_lock``,
    created WHEN THE RECORDING IS BUILT (``vision/recorder.py``) — never a
    check-then-set inside this worker, where two concurrent collects could
    each create their own lock and both write.  The memo
    (``recording._written_video_path``) is read and written under that lock;
    before reusing a memo path its file is checked to still EXIST — a deleted
    file is written again (NIT).  The first write also best-effort sweeps
    stale ``.tmp-*`` leftovers older than one hour (never any other file).
    Returns the absolute path, or ``None`` when the write failed (the caller
    answers ``write_failed``; a later collect may retry because the memo
    stays unset)."""
    # DELTA 3 NIT: defensive attribute access — production always passes a
    # Recording (whose lock is created at build time), but a non-Recording
    # must never raise here; a module-level lock is the fallback.
    file_lock = getattr(recording, "_video_file_lock", None) or _FALLBACK_VIDEO_FILE_LOCK
    with file_lock:
        written = getattr(recording, "_written_video_path", None)
        if written is not None and Path(str(written)).exists():
            return str(written)
        try:
            root = recordings_root()
            root.mkdir(parents=True, exist_ok=True)
            _sweep_stale_tmp_files(root)
            video_path = root / f"{recording.record_id}.mp4"
            _write_video_file(video_path, video_bytes)
        except Exception as exc:  # noqa: BLE001 - the write failure is the honest omission
            logger.debug("recording MP4 file write failed", exc_info=True)
            recording._written_video_error = f"{type(exc).__name__}: {exc}"
            return None
        recording._written_video_path = str(video_path)
        return str(video_path)


def _save_recording(
    recording: Any,
    video_bytes: bytes | None,
    *,
    written_video_path: str | None = None,
    delivery_write_error: str | None = None,
) -> dict[str, Any]:
    """``save=True``: write the MP4 + the contact-sheet PNG under the Cortex
    recordings directory (runs in the to_thread worker — M4).

    WP-E E2: with the default file delivery the MP4 was ALREADY written at the
    shared path — ``written_video_path`` reports it without a second write (one
    MP4 on disk, no double write).  DELTA 2 N4: EVERY MP4 write goes through
    the SAME write-once path (:func:`_write_delivered_video_once`) in every
    case — an ``inline``/``include_video=false`` save collect reuses the memo
    instead of re-running ``os.replace`` over a file a reader may hold open.
    The sheet write has its OWN try: a video failure keeps ``sheet_path`` and
    a sheet failure keeps ``video_path``.  DELTA 3 M-A: ``delivery_write_error``
    (the worker's file-delivery failure) lands as ``save_error`` here, and
    ``saved`` is honestly False whenever ``save_error`` or ``sheet_error`` is
    present — never ``saved: true`` without the file."""
    saved: dict[str, Any] = {}
    root = recordings_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001 - a failed save is reported, never a crash
        return {"saved": False, "save_error": f"{type(exc).__name__}: {exc}"}
    if written_video_path is not None:
        saved["video_path"] = written_video_path
    elif video_bytes is not None:
        video_written = _write_delivered_video_once(recording, video_bytes)
        if video_written is not None:
            saved["video_path"] = video_written
        else:
            saved["save_error"] = str(
                getattr(recording, "_written_video_error", "") or "the MP4 write failed"
            )
    if delivery_write_error is not None:
        saved["save_error"] = delivery_write_error
    if recording.sheet_png_b64:
        try:
            sheet_path = root / f"{recording.record_id}_sheet.png"
            sheet_path.write_bytes(base64.b64decode(recording.sheet_png_b64))
            saved["sheet_path"] = str(sheet_path)
        except Exception as exc:  # noqa: BLE001 - a sheet failure never drops video_path
            saved["sheet_error"] = f"{type(exc).__name__}: {exc}"
    # DELTA 3 M-A: the honest verdict — any error in the save means NOT saved.
    saved["saved"] = not ({"save_error", "sheet_error"} & saved.keys())
    return saved


def _finished_reply_worker(
    session_id: str,
    recording: Any,
    *,
    blob_attached: bool,
    want_video: bool,
    save: bool,
    video_bytes: bytes | None,
    delivery: str = VIDEO_DELIVERY_DEFAULT,
) -> tuple[dict[str, Any], tuple[str, str, float | None] | None, str | None]:
    """M4: EVERY heavy piece of the finished reply in ONE synchronous worker —
    the payload build, the sheet's outbound ladder, the sheet PNG's outbound
    re-encode, the MP4's base64 blob and the ``save`` file writes.  Returns
    ``(payload, (sheet_b64, mime, scale), blob_b64)``; the caller assembles the
    MCP blocks on the loop (cheap dict/argument work only).

    WP-E E2: with the DEFAULT ``delivery="file"`` a produced MP4 is WRITTEN
    under the recordings folder (the same place save=true uses — one file, no
    double write) and the video entry carries its absolute ``path``; NO blob is
    attached and the video never enters the tool result.  Most hosts truncate
    large tool results, so "file" is the default; "inline" (the legacy
    EmbeddedResource) is only for hosts known to pass video to the model.  A
    failed file write answers the closed ``write_failed`` omission (retryable
    on a later collect).  DELTA 1 m6: the write goes through
    :func:`_write_delivered_video_once` (atomic, once per recording, later
    collects reuse the path) and a ``write_failed`` entry is built LOCALLY —
    the shared ``recording.video`` is never mutated here (the heavy worker
    runs outside the per-recording lock, so a parallel collect must never see
    a half-updated entry)."""
    written_video_path: str | None = None
    write_failed_entry: dict[str, Any] | None = None
    delivery_write_error: str | None = None
    if delivery == "file" and want_video and video_bytes is not None:
        written_video_path = _write_delivered_video_once(recording, video_bytes)
        if written_video_path is None:
            delivery_write_error = str(
                getattr(recording, "_written_video_error", "") or "the MP4 write failed"
            )
            write_failed_entry = {
                "omitted": VIDEO_OMITTED_WRITE_FAILED,
                "bytes": len(video_bytes),
                "detail": delivery_write_error,
            }
            video_bytes = None
    if write_failed_entry is not None:
        video_entry = write_failed_entry
    elif delivery == "file" and written_video_path is not None and want_video:
        info = dict(getattr(recording, "video", None) or {})
        info.update(
            {
                "included": True,
                "delivery": "file",
                "path": written_video_path,
            }
        )
        info.setdefault("codec", "h264")
        video_entry = info
    else:
        video_entry = _video_entry(recording, blob_attached=blob_attached, want_video=want_video)
    payload = _finished_payload(session_id, recording, video_entry=video_entry)
    if video_entry.get("included") and video_entry.get("uri") is None:
        video_entry["uri"] = f"cortex://recording/{recording.record_id}.mp4"
    if written_video_path is not None:
        payload["video_path"] = written_video_path
    blob_b64: str | None = None
    if blob_attached and video_bytes is not None:
        blob_b64 = base64.b64encode(video_bytes).decode("ascii")
    sheet_tuple: tuple[str, str, float | None] | None = None
    sheet_b64 = recording.sheet_png_b64
    if sheet_b64:
        bounded_b64, mime, scale = _bound_outbound_image(sheet_b64)
        sheet_tuple = (bounded_b64, mime, scale)
    if save:
        # DELTA 2 M7: an existing MP4 is written even when this reply attaches
        # no blob (save=true + explicit include_video=false writes the file).
        # WP-E E2: with the file delivery the MP4 is already on disk at the
        # shared path — report it, never write it twice.  DELTA 3 M-A: a
        # delivery write failure rides into the save dict as ``save_error``,
        # so ``saved`` is honestly False (never saved:true without a file).
        payload.update(
            _save_recording(
                recording,
                video_bytes,
                written_video_path=written_video_path,
                delivery_write_error=delivery_write_error,
            )
        )
    return payload, sheet_tuple, blob_b64


def _failed_reply(recording: Any) -> dict[str, object]:
    """B3: the terminal reply for a broken recording (capture or worker)."""
    kind = getattr(recording, "error_kind", None) or "record_worker_failed"
    detail = str(getattr(recording, "error", None) or "the recording failed")
    return {
        "ok": False,
        "result": "failed",
        "record_id": recording.record_id,
        "session_id": recording.session_id,
        "frames": recording.frame_count,
        "dropped_frames": recording.dropped_frames,
        "reasons": [kind],
        "message": (
            f"Recording {recording.record_id} failed ({kind}): {detail[:300]} "
            f"({recording.frame_count} frames captured before the failure)."
        ),
    }


async def _finished_reply(
    session_id: str,
    bundle: Any,
    recording: Any,
    *,
    result_token: str,
    handle: "_RecordHandle | None" = None,
    include_video: bool | None = None,
    video_delivery: str | None = None,
    started_monotonic: float | None = None,
) -> Any:
    """The verified/stopped reply: TextContent JSON + ImageContent contact sheet
    (+ the MP4 as a written file path for the "file" delivery default, or as an
    ``EmbeddedResource`` for "inline", when video is attached to THIS reply).

    DELTA 1: the encode is single-flight per recording (M5) and carries the
    client-budget deadline (M3); every heavy piece runs in ONE to_thread
    worker (M4); the video entry says what happened for THIS reply (M7).
    DELTA 2: N3 (the held frames stay until the pool evicts them, so video on
    the RESULT call alone works; eviction ⇒ ``frames_released``), n3 (final
    omissions are reused, only ``over_budget`` retried), N4 (the encode
    carries a stop check)."""
    if getattr(recording, "error", None):
        return _failed_reply(recording)
    pool = recording_store(bundle.extra)
    want_video, save, delivery = _resolve_flags(recording, handle, include_video, video_delivery)
    video_bytes: bytes | None = None
    if want_video or save:
        # M5: ONE encode per recording at a time — concurrent result calls
        # serialize here and the second reuses the stored result.
        lock = getattr(recording, "_result_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            recording._result_lock = lock  # noqa: SLF001 - the recording is the guard's owner
        async with lock:
            # N3: eviction honesty — if the pool EVICTED the accounted held
            # frames (the byte cap), release them on the Recording and answer
            # ``frames_released`` for any later video request.  Unit-level
            # recordings (never accounted) are untouched.
            if (
                pool is not None
                and recording.frames
                and recording.frames_accounted
                and not pool.has_frames(recording.record_id)
            ):
                recording.release_frame_payloads()
            retained = pool.lookup_video(recording.record_id) if pool is not None else None
            if retained is not None:
                video_bytes = retained.video_bytes
                if recording.video is None or not recording.video.get("included"):
                    recording.video = {
                        "included": True,
                        "bytes": retained.byte_size,
                        "width": retained.width,
                        "height": retained.height,
                        "fps": retained.fps,
                        "frames": recording.frame_count,
                        "codec": "h264",
                    }
            elif recording.frames_released or not recording.frames:
                # N3: the held frames were evicted under the pool's byte cap
                # before any video was stored — the honest omission.
                recording.video = {
                    "omitted": VIDEO_OMITTED_FRAMES_RELEASED,
                    "bytes": 0,
                    "detail": "the held frames were evicted under the pool's byte cap "
                    "before any video was stored",
                }
            else:
                stored = recording.video or {}
                stored_omission = stored.get("omitted")
                # DELTA 2 n3 + DELTA 3 n3: a FINAL omission is reused on every
                # later call instead of re-running the ladder — but an encode
                # ABORTED by cancel/stop is NOT final: a later result call in
                # a live session retries while the frames are held.  Only
                # `over_budget` was already retryable; `aborted` joins it.
                is_aborted = bool(stored.get("aborted"))
                if stored_omission in VIDEO_FINAL_OMISSIONS and not is_aborted:
                    pass
                else:
                    deadline = _encode_deadline(started_monotonic)
                    # N4: the stop check is armed ONLY for a live session — a
                    # unit-level call without a session registry encodes
                    # unchecked.
                    stop_check = None
                    if _session_alive(session_id):
                        stop_check = _stop_check_for(bundle, recording)
                    data, info = await asyncio.to_thread(
                        encode_video,
                        recording,
                        fps=recording.fps,
                        # VAL-WP-D-01 M2: the session's show_pointer default
                        # rides the Recording — OFF composites no sprite.
                        composite=bool(getattr(recording, "composite_pointer", True)),
                        deadline=deadline,
                        should_abort=stop_check,
                    )
                    recording.video = info
                    if data is not None:
                        if pool is not None:
                            pool.store_video(
                                record_id=recording.record_id,
                                mp4_bytes=data,
                                width=int(info.get("width", 0)),
                                height=int(info.get("height", 0)),
                                fps=recording.fps,
                                frames=int(info.get("frames", recording.frame_count)),
                            )
                        video_bytes = data
                        # m8/DELTA 3: the products are stored — release the
                        # JPEGs AND drop their pool accounting entry, so the
                        # pool's byte count matches real held memory (no
                        # phantom bytes).
                        recording.release_frame_payloads()
                        if pool is not None:
                            pool.remove_frames(recording.record_id)
    # WP-E E2: the MP4 attaches as a blob ONLY for the "inline" delivery; the
    # default "file" delivery writes the file inside the reply worker instead.
    blob_attached = bool(video_bytes is not None and want_video and delivery == "inline")
    payload, sheet_tuple, blob_b64 = await asyncio.to_thread(
        _finished_reply_worker,
        session_id,
        recording,
        blob_attached=blob_attached,
        want_video=want_video,
        save=save,
        video_bytes=video_bytes,
        delivery=delivery,
    )
    payload["result"] = result_token
    payload["ok"] = result_token == "verified"
    payload["message"] = (
        (
            f"Recording {recording.record_id} finished: {recording.frame_count} frames, "
            f"{payload['duration_ms']} ms, effective fps {recording.effective_fps}, "
            f"{recording.dropped_frames} dropped."
        )
        if result_token == "verified"
        else (
            f"Recording {recording.record_id} was cancelled early: "
            f"{recording.frame_count} frames captured."
        )
    )
    blocks: list[Any] = [
        TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))
    ]
    if sheet_tuple is not None:
        bounded_b64, mime, scale = sheet_tuple
        payload[IMAGE_OUTBOUND_SCALE_KEY] = scale
        blocks = [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
        blocks.append(ImageContent(type="image", data=bounded_b64, mimeType=mime))
    if blob_b64 is not None:
        blocks.append(
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri=f"cortex://recording/{recording.record_id}.mp4",
                    mimeType="video/mp4",
                    blob=blob_b64,
                ),
            )
        )
    return blocks


def _encode_deadline(started_monotonic: float | None) -> float:
    """M3/n2: the fallback encode's deadline — the client budget minus the
    time this call already spent, minus a safety margin, floored at ZERO
    (never a guaranteed second: with no budget left, no rung starts)."""
    started = started_monotonic if started_monotonic is not None else _record_now()
    remaining = (
        client_timeout_seconds() - (_record_now() - started) - RECORD_RESULT_SAFETY_MARGIN_SECONDS
    )
    return _record_now() + max(0.0, remaining)


async def _resolve_finished(
    session_id: str,
    bundle: Any,
    record_id: str,
    *,
    result_token: str,
    include_video: bool | None,
    video_delivery: str | None = None,
    handle: "_RecordHandle | None",
    started_monotonic: float | None = None,
) -> Any:
    """The finished-recording reply (verified / stopped / failed)."""
    with _lock:
        recording = _FINISHED_RECORDINGS.get(session_id, {}).get(record_id)
    if recording is None:
        return _record_rejection(
            "unknown_record",
            "No recording with this record_id exists on this session: record ids "
            "are session-scoped and a finished recording is retained only briefly.",
        )
    if getattr(recording, "error", None):
        return _failed_reply(recording)
    return await _finished_reply(
        session_id,
        bundle,
        recording,
        result_token=result_token,
        handle=handle,
        include_video=include_video,
        video_delivery=video_delivery,
        started_monotonic=started_monotonic,
    )


# --- shared parameter validation + the launch path (computer_record AND the
# --- execute surface's ``record`` object, WP-E E1: ONE code path, same bands,
# --- same typed rejection codes) -------------------------------------------------


@dataclass
class _ResolvedRecordArgs:
    """DELTA 2 N1/N2: the COERCED record values the shared validator returns on
    success — no caller ever runs a second ``int()``/``float()`` on raw
    values.  ``region`` rides through verbatim (its shape/bounds are
    validated separately, by ``_resolve_recording_space`` on the execute
    surface and at launch on computer_record)."""

    duration_s: float
    fps: int
    max_keyframes: int
    include_video: bool | None
    save: bool | None
    video_delivery: str
    region: Any = None


def _validate_record_arguments(
    duration_s: Any,
    fps: Any,
    max_keyframes: Any,
    include_video: Any,
    save: Any,
    video_delivery: Any = None,
    region: Any = None,
) -> "tuple[str, str, _ResolvedRecordArgs | None]":
    """The SHARED validation + coercion.  Returns ``(code, message, resolved)``:
    on success ``(None, None, _ResolvedRecordArgs)`` with the COERCED values;
    on the first violation ``(code, message, None)``.  computer_record maps
    the pair onto its typed ``rejected`` envelope; the execute surface maps
    the SAME pair onto the §1.1 execute rejection envelope — same codes, same
    messages, one path, and BOTH tools consume the same resolved values.

    DELTA 2 N1/N2 (Commander decision): ONE rule for both tools — the
    TOLERANT rule.  ``fps`` / ``max_keyframes`` go through EXACTLY the same
    coercion as computer_record's ``NullableInt`` boundary —
    :func:`~computer_use_mcp.server.schemas._coerce_tolerant_int` is REUSED,
    never copied: non-integral floats and numeric strings ROUND the same way
    (``2.7`` → 3, ``"5.0"`` → 5, ``"1e1"`` → 10); booleans, lists, dicts and
    non-numeric strings raise the typed ``invalid_record_fps`` /
    ``invalid_record_keyframes``.  ``duration_s`` accepts the same shapes
    pydantic's lax float accepts for ``computer_record(duration_s)`` —
    numbers and numeric strings (``"4"`` → 4.0); a bool or a non-numeric
    string is the typed ``invalid_record_duration``.  Band checks run AFTER
    coercion, for both tools, with the same codes."""
    if isinstance(duration_s, bool):
        return (
            "invalid_record_duration",
            f"duration_s must be a number of seconds; got {duration_s!r}.",
            None,
        )
    try:
        requested = float(duration_s) if duration_s is not None else None
    except (TypeError, ValueError):
        return (
            "invalid_record_duration",
            f"duration_s must be a number of seconds; got {duration_s!r}.",
            None,
        )
    if requested is None:
        return (
            "invalid_record_duration",
            f"duration_s must be a number of seconds; got {duration_s!r}.",
            None,
        )
    if not math.isfinite(requested) or not (
        RECORD_DURATION_BAND[0] <= requested <= RECORD_DURATION_BAND[1]
    ):
        return (
            "invalid_record_duration",
            f"duration_s must be between {RECORD_DURATION_BAND[0]} and "
            f"{RECORD_DURATION_BAND[1]} seconds; got {duration_s!r}. Nothing was started.",
            None,
        )
    try:
        resolved_fps = (
            RECORD_FPS_DEFAULT
            if fps is None
            else _coerce_tolerant_int(fps, field="fps")
        )
    except ValueError as exc:
        return (
            "invalid_record_fps",
            f"fps must be an integer between {RECORD_FPS_BAND[0]} and "
            f"{RECORD_FPS_BAND[1]} (default {RECORD_FPS_DEFAULT}); {exc} "
            "Nothing was started.",
            None,
        )
    if not RECORD_FPS_BAND[0] <= resolved_fps <= RECORD_FPS_BAND[1]:
        return (
            "invalid_record_fps",
            f"fps must be between {RECORD_FPS_BAND[0]} and {RECORD_FPS_BAND[1]} "
            f"(default {RECORD_FPS_DEFAULT}); got {fps!r}. Nothing was started.",
            None,
        )
    try:
        resolved_keyframes = (
            RECORD_MAX_KEYFRAMES_DEFAULT
            if max_keyframes is None
            else _coerce_tolerant_int(max_keyframes, field="max_keyframes")
        )
    except ValueError as exc:
        return (
            "invalid_record_keyframes",
            f"max_keyframes must be an integer between {RECORD_MAX_KEYFRAMES_BAND[0]} and "
            f"{RECORD_MAX_KEYFRAMES_BAND[1]} (default {RECORD_MAX_KEYFRAMES_DEFAULT}); "
            f"{exc} Nothing was started.",
            None,
        )
    if not RECORD_MAX_KEYFRAMES_BAND[0] <= resolved_keyframes <= RECORD_MAX_KEYFRAMES_BAND[1]:
        return (
            "invalid_record_keyframes",
            f"max_keyframes must be between {RECORD_MAX_KEYFRAMES_BAND[0]} and "
            f"{RECORD_MAX_KEYFRAMES_BAND[1]} (default {RECORD_MAX_KEYFRAMES_DEFAULT}); "
            f"got {max_keyframes!r}. Nothing was started.",
            None,
        )
    if include_video is not None and not isinstance(include_video, bool):
        return (
            "invalid_record_param",
            f"include_video must be a boolean or null; got {include_video!r}.",
            None,
        )
    if save is not None and not isinstance(save, bool):
        return (
            "invalid_record_param",
            f"save must be a boolean or null; got {save!r}.",
            None,
        )
    if not _valid_video_delivery(video_delivery):
        return (
            "invalid_video_delivery",
            "video_delivery must be \"file\" or \"inline\" (null = the default "
            f"\"file\"); got {video_delivery!r}.",
            None,
        )
    return (
        None,
        None,
        _ResolvedRecordArgs(
            duration_s=requested,
            fps=int(resolved_fps),
            max_keyframes=int(resolved_keyframes),
            include_video=include_video,
            save=save,
            video_delivery=(
                video_delivery if video_delivery in VIDEO_DELIVERY_CHOICES else VIDEO_DELIVERY_DEFAULT
            ),
            region=region,
        ),
    )


def _launch_recording(
    bundle: Any,
    session_id: str,
    *,
    requested: float,
    resolved_fps: int,
    resolved_keyframes: int,
    region: Any,
    include_video: bool,
    save: bool,
    video_delivery: str,
    audit_source: str,
) -> tuple["_RecordHandle | None", "dict[str, Any] | None", "dict[str, Any] | None"]:
    """The ONE launch path (WP-E E1): resolve the recording space, build the
    handle + recorder, check the caps under the insert lock, start the thread.

    Returns ``(handle, refusal, context_info)``.  ``refusal`` is ``None`` on
    success; otherwise ``{"reason": <typed code>, "message": str, "detail":
    <the raw reply the computer_record surface returns verbatim>}`` — the
    execute surface NEVER refuses the action for a recording problem, it only
    reads ``reason``/``message`` for its ``{"started": false}`` block."""
    resolved = _resolve_recording_space(bundle, region)
    if isinstance(resolved, dict):
        return None, {
            "reason": "invalid_region",
            "message": str(resolved.get("message", "invalid recording region.")),
            "detail": resolved,
        }, None
    (
        context_info,
        region_physical,
        canonical_rect,
        transform,
        region_requested,
    ) = resolved
    canonical_origin = (canonical_rect[0], canonical_rect[1])
    canonical_size = (canonical_rect[2], canonical_rect[3])
    grabber, cursor = _resolve_grabber(bundle.backend)
    ring = getattr(bundle.backend, "pointer_ring", None)
    pressed_at = getattr(ring, "pressed_at", None)
    record_id = "rec_" + uuid.uuid4().hex[:16]
    started_epoch = time.time()
    handle = _RecordHandle(
        record_id=record_id,
        session_id=session_id,
        duration_s=requested,
        fps=resolved_fps,
        max_keyframes=resolved_keyframes,
        region_requested=region_requested,
        include_video=include_video,
        save=save,
        video_delivery=video_delivery,
        started_at=_record_iso_utc(),
        started_epoch=started_epoch,
        deadline_monotonic=_record_now() + requested,
        context_ref=weakref.ref(bundle.context),
        loop=asyncio.get_running_loop(),
        pool=recording_store(bundle.extra),
    )
    task_id = bundle.context.task.task_id
    auditor = bundle.auditor

    def _audit(event_type: str, *, result: str, metadata: dict[str, Any]) -> None:
        try:
            auditor.emit(
                event_type,
                session_id,
                task_id=task_id,
                result=result,
                metadata=metadata,
            )
        except Exception:  # noqa: BLE001 - audit failures never break the reply path
            logger.debug("record audit failed", exc_info=True)

    handle.audit = _audit
    handle.recorder = ScreenRecorder(
        record_id=record_id,  # B1: ONE id end to end
        duration_s=requested,
        fps=resolved_fps,
        region_physical=region_physical,
        transform=transform,
        canonical_origin=canonical_origin,
        canonical_size=canonical_size,
        max_keyframes=resolved_keyframes,
        session_id=session_id,
        grab=grabber,  # M6: a closeable grabber OBJECT
        cursor=cursor,
        pressed_at=pressed_at,
        cancel=handle.cancel,
        # VAL-WP-D-01 M2: the session's show_pointer default — OFF composites
        # no cursor into the sheet, the keyframes or the MP4.
        composite_pointer=session_show_pointer(bundle),
    )
    thread = threading.Thread(
        target=_record_worker_from(handle),
        name=f"cortex-record-{record_id}",
        daemon=True,
    )

    def _start() -> None:
        handle.thread = thread
        with _lock:
            # The caps are checked BEFORE any insert — a refused recording
            # leaves nothing behind (the wait family's S-15 shape).
            registry = _RECORDINGS_IN_FLIGHT.get(session_id, {})
            if len(registry) >= MAX_RECORDINGS_PER_SESSION:
                active = next(iter(registry), None)
                raise _LimitRefusal(
                    "recording_limit",
                    f"this session already has an active recording ({active}); "
                    "collect or cancel it with computer_record_result first "
                    f"(MAX_RECORDINGS_PER_SESSION = {MAX_RECORDINGS_PER_SESSION}).",
                )
            live = sum(len(entries) for entries in _RECORDINGS_IN_FLIGHT.values())
            if live >= MAX_RECORDINGS_PER_PROCESS:
                raise _LimitRefusal(
                    "recording_limit",
                    f"{live} recordings are in flight across all sessions "
                    f"(MAX_RECORDINGS_PER_PROCESS = {MAX_RECORDINGS_PER_PROCESS}); "
                    "collect one first.",
                )
            # Re-check liveness AT registration, under the same lock as the
            # insert (the wait family's F7 shape) — through record_machine's
            # binding (never a tool-local binding of the bundle map).
            if not _session_alive(session_id):
                raise _LimitRefusal(
                    "session_stopped",
                    "the session stopped while the recording was starting; "
                    "nothing is registered.",
                    detail=_error_response(_StoppedSession(session_id)),
                )
            _RECORDINGS_IN_FLIGHT.setdefault(session_id, {})[record_id] = handle

    try:
        _start()
    except _LimitRefusal as refusal:
        if refusal.reason == "session_stopped":
            return None, {
                "reason": refusal.reason,
                "message": refusal.message,
                "detail": refusal.detail,
            }, None
        return None, {"reason": refusal.reason, "message": refusal.message, "detail": None}, None
    try:
        thread.start()
    except Exception as exc:  # noqa: BLE001 - no phantom registration
        from .record_machine import _retire_record

        _retire_record(handle)
        return None, {
            "reason": "record_start_failed",
            "message": (
                f"The recording could not be started ({type(exc).__name__}: {exc}); "
                "nothing is registered and no slot is consumed."
            ),
            "detail": {
                "ok": False,
                "result": "failed",
                "error": "record_failed",
                "session_id": session_id,
                "record_id": record_id,
                "message": (
                    f"The recording could not be started ({type(exc).__name__}: {exc}); "
                    "nothing is registered and no slot is consumed."
                ),
            },
        }, None
    _audit(
        "record_start",
        result="ok",
        metadata={
            "record_id": record_id,
            "duration_s": requested,
            "fps": resolved_fps,
            "region": context_info.get("region"),
            "include_video": handle.include_video,
            "source": audit_source,
        },
    )
    return handle, None, context_info


def _execute_record_block(
    session_id: str, handle: "_RecordHandle", context_info: "dict[str, Any] | None"
) -> dict[str, Any]:
    """WP-E E1: the reply's ``record`` block for a recording started around an
    execute call — the marker facts plus the collect hint (the agent collects
    with computer_record_result, which blocks until the end)."""
    record_id = handle.record_id
    return {
        "started": True,
        "record_id": record_id,
        "status": "recording",
        "ends_at": handle.ends_at,
        "duration_s": handle.duration_s,
        "fps": handle.fps,
        "region": (context_info or {}).get("region"),
        "message": (
            f"Recording {record_id} started around this action (a "
            f"{RECORD_PRE_ROLL_SECONDS}s pre-roll is part of the window so the "
            '"before" state is captured); collect with computer_record_result('
            f'session_id, record_id="{record_id}") — it blocks until the '
            "recording ends and its timeline contains THIS call's actions."
        ),
    }


def _execute_record_not_started(reason: str, message: str) -> dict[str, Any]:
    """WP-E E1: the ``record`` block when no recording started.  The ACTION
    still runs — a recording problem never refuses an execute call."""
    return {
        "started": False,
        "reason": reason,
        "message": message,
    }


def _text_session_record_block() -> dict[str, Any]:
    """WP-E E1: the ``record`` block for a text-mode session — the recording
    reports {"started": false} and the action still runs."""
    return _execute_record_not_started(
        "text_session_no_screen",
        "This session is in image_delivery=text mode (non-vision-safe): there "
        "are no screen images to record. The action still runs.",
    )


def start_execute_recording(
    bundle: Any, session_id: str, record: "dict[str, Any] | None"
) -> "dict[str, Any] | None":
    """WP-E E1: launch the execute call's recording.  Call AFTER the record
    argument has been validated (DELTA 2 N1/N2: ``record`` is the COERCED
    ``_ResolvedRecordArgs``-shaped dict the shared validator returned — the
    values are final, there is NO second ``int()``/``float()`` on raw input)
    and the region pre-resolved (the typed rejections happened earlier); this
    helper NEVER raises and NEVER refuses the action — every launch problem
    becomes ``{"started": false, ...}`` in the reply's ``record`` block.
    DELTA 1 m5: the whole launch is guarded — ANY exception before or inside
    it (grabber/recorder construction, context attributes, ...) becomes
    ``{"started": false, "reason": "record_start_failed"}`` and the action
    still runs.  ``None`` (no ``record`` argument) returns ``None`` so the
    reply stays byte-identical."""
    if record is None:
        return None
    if bundle.extra.get("image_delivery") == "text":
        return _text_session_record_block()
    try:
        handle, refusal, context_info = _launch_recording(
            bundle,
            session_id,
            requested=float(record.duration_s),
            resolved_fps=int(record.fps),
            resolved_keyframes=int(record.max_keyframes),
            region=record.region,
            include_video=bool(record.include_video) or bool(record.save),
            save=bool(record.save),
            video_delivery=(
                record.video_delivery
                if record.video_delivery in VIDEO_DELIVERY_CHOICES
                else VIDEO_DELIVERY_DEFAULT
            ),
            audit_source="computer_execute",
        )
    except Exception as exc:  # noqa: BLE001 - m5: the launch NEVER refuses the action
        logger.warning("execute record launch failed", exc_info=True)
        return _execute_record_not_started(
            "record_start_failed",
            f"The recording could not be launched ({type(exc).__name__}: {exc}); "
            "the action still runs.",
        )
    if refusal is not None:
        return _execute_record_not_started(refusal["reason"], refusal["message"])
    assert handle is not None
    return _execute_record_block(session_id, handle, context_info)


def _no_action_record_block() -> dict[str, Any]:
    """DELTA 1 n7: the ``record`` block when the call carries a valid ``record``
    but dispatches no input at all (``plan_action="stop"``; a continue past the
    walk's end) — nothing is recorded and the closed ``no_action`` reason says
    so (in ``EXECUTE_RECORD_NOT_STARTED_REASONS`` and the docs)."""
    return _execute_record_not_started(
        "no_action",
        "This call dispatches no input (plan stop, or the walk already ended), "
        "so there is nothing to record; the record object was not ignored.",
    )


def cancel_execute_recording(session_id: str, record_block: Any) -> bool:
    """DELTA 1 m4: cancel a recording started around this execute call — the
    cancel flag plus the loop wake, EXACTLY like ``cancel=true`` — so the
    session's recording slot frees immediately instead of holding for up to
    ``duration_s``.  Best-effort (False when there is nothing to cancel or the
    recording already retired); never raises."""
    try:
        if not isinstance(record_block, dict) or not record_block.get("started"):
            return False
        record_id = record_block.get("record_id")
        with _lock:
            handle = _RECORDINGS_IN_FLIGHT.get(session_id, {}).get(record_id)
        if handle is None:
            return False
        handle.cancel.set()
        _signal_loop_waiters(handle)
        return True
    except Exception:  # noqa: BLE001 - best-effort, never a gate
        return False


def execute_record_input_dispatched(session_id: str, record_block: Any) -> bool:
    """DELTA 2 n6: the EXACT zero-input test for the detect-and-cancel rule —
    True when at least one Cortex action was stamped in the session's DISPATCH
    RING since this recording started.  The ring is stamped at the ONE
    dispatch choke point immediately before ``_backend_execute``
    (``pipeline/agent.py``), so an empty window between the recording's start
    and now PROVES nothing was dispatched, whatever the outcome kind says —
    the cancel can never fire after input left the process (the
    unsupported-key-after-dispatch envelope included).  Best-effort ``False``
    on any failure (never gates)."""
    try:
        if not isinstance(record_block, dict) or not record_block.get("started"):
            return False
        with _lock:
            handle = _RECORDINGS_IN_FLIGHT.get(session_id, {}).get(
                record_block.get("record_id")
            )
        if handle is None:
            return False
        started_monotonic = handle.deadline_monotonic - handle.duration_s
        from ..vision.recorder import peek_actions_in_window

        return bool(
            peek_actions_in_window(session_id, started_monotonic, _record_now() + 1.0)
        )
    except Exception:  # noqa: BLE001 - best-effort, never a gate
        return False


# --- the tools --------------------------------------------------------------------------------


@mcp.tool()
@_session_scoped
async def computer_record(
    session_id: str,
    duration_s: RecordDuration,
    region: NullableRegion = None,
    fps: NullableInt = None,
    max_keyframes: NullableInt = None,
    include_video: NullableBool = None,
    save: NullableBool = None,
    video_delivery: NullableStr = None,
) -> Any:
    """Start recording 0.5–10 s of the screen IN THE BACKGROUND and return AT ONCE.

    Returns a marker reply: ``record_id`` ("rec_" + 16 hex), ``status:
    "recording"``, ``ends_at`` (epoch seconds) and the effective fps/region.
    KEEP CALLING TOOLS while it records — the whole point.  Collect the product
    with computer_record_result(session_id, record_id): a change timeline plus
    the Cortex actions dispatched inside the window, a contact sheet of
    timestamped keyframes (each fetchable at full size by ``image_id`` through
    computer_fetch_image, the Cortex cursor composited), and on request the
    FULL video — as a written MP4 file path (the ``"file"`` delivery default)
    or as an MP4 EmbeddedResource (``"inline"``).

    Bands (fail-closed, never silently shortened): ``duration_s`` 0.5..10 s;
    ``fps`` 1..10 (default 5); ``max_keyframes`` 2..12 (default 8).  ``region``
    is ``[left, top, width, height]`` in CANONICAL screenshot space (the space
    actions are grounded in); with no region the session's monitor is recorded
    (the same monitor choice computer_observe uses).  Malformed, out-of-bounds
    or unverifiable coordinates are refused with the typed ``invalid_region``
    error (monitor mode included).

    ``include_video=true`` also encodes an H.264 MP4 right after capture (in
    the recording worker — the result call usually only fetches it; the default
    blob cap is 2 MiB raw, ``CORTEX_RECORD_VIDEO_MAX_MB`` band 1..8, encoder
    preference ``CORTEX_RECORD_ENCODER``, default h264_mf then libx264).  A
    larger/budgeted-out encode is honestly omitted: ``video.omitted`` names
    the closed reason — ALL of them: ``not_requested`` / ``too_large`` /
    ``encoder_unavailable`` / ``encode_failed`` / ``over_budget`` (the client
    budget could not fit the next ladder rung; retried on a later call) /
    ``frames_released`` (the held frames were evicted under the recording
    pool's byte cap before any video was stored) / ``write_failed`` (WP-E E2:
    the default file delivery could not write the MP4; retried on a later
    collect).  ``save=true`` writes the MP4 + the contact-sheet PNG under the
    Cortex recordings directory and returns ``video_path`` / ``sheet_path``
    (it implies video production).
    ``video_delivery`` (trailing optional, WP-E E2): "file" (the DEFAULT) or
    "inline".  Most hosts TRUNCATE large tool results — a 196-562 KB MP4 blob
    never reached the model in the field — so "file" is the default: the MP4
    is written under the recordings folder and the reply carries its absolute
    ``path`` instead of a blob.  "inline" is only for hosts known to pass
    video to the model (the legacy EmbeddedResource).  The result call's
    explicit ``video_delivery`` wins over this start flag.
    An ``encode_failed`` carrying ``aborted: true`` (a stop/cancel aborted
    the encode) is NOT final — a later collect retries while the frames are
    held.  Peek replies carry ``status`` ``"recording"`` or ``"encoding"``
    (the capture finished, the video encode is still running).  Fetched
    keyframes publish ``frame_to_canonical`` (``{"scale", "origin"}``) that
    maps the STORED keyframe pixels: a copy served downscaled must first be
    divided by its ``image_outbound_scale``.

    If the screen capture fails (or the worker dies), the result reply is
    ``result:"failed"`` with reasons ``["capture_failed" | "record_worker_failed"]``
    — never a silent empty ``verified``.  A capture error after some frames is
    still ``failed``: a partially captured timeline is untrustworthy; the
    frames captured before the failure are counted in the message.

    Both recording tools stay ALLOWED while a plan lease is active (they
    dispatch no input; recording a plan's effect is a main use).  Caps: 1
    active recording per session, 2 per process (typed ``recording_limit``).

    Example: {"session_id": "S", "duration_s": 6} -> "record_id" + status "recording"; collect with computer_record_result.
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    # DELIBERATELY no plan-lease refusal here: recording a plan's effect is a
    # main use of the surface (plan_surface's allow side lists both tools).
    # Text sessions have no screen images — typed rejection before anything starts.
    if bundle.extra.get("image_delivery") == "text":
        return _record_rejection(
            "text_session_no_screen",
            "This session is in image_delivery=text mode (non-vision-safe): there "
            "are no screen images to record. Start an image-mode session to record.",
        )
    # DELTA 3 NIT: the MCP boundary's lax float would coerce True/False to
    # 1.0/0.0 (verified live through the instance gate), but the
    # RecordDuration boundary validator already refuses the boolean BEFORE
    # that coercion — over MCP this isinstance check is unreachable and the
    # host sees a pydantic isError naming duration_s (NOT this typed code).
    # It exists for DIRECT (non-MCP) callers, who bypass the boundary and get
    # the typed invalid_record_duration rejection here.
    if isinstance(duration_s, bool):
        return _record_rejection(
            "invalid_record_duration",
            f"duration_s must be a number of seconds; got {duration_s!r}.",
        )
    code, message, resolved = _validate_record_arguments(
        duration_s, fps, max_keyframes, include_video, save, video_delivery, region
    )
    if code is not None or resolved is None:
        return _record_rejection(code or "invalid_record_param", message or "invalid record arguments.")
    requested = resolved.duration_s
    resolved_fps = resolved.fps
    resolved_keyframes = resolved.max_keyframes
    delivery = resolved.video_delivery
    # N2 (Commander decision): the capture NEVER runs on an executor thread —
    # the backend's mss instance and payload cache are thread-bound, and this
    # is the same on-the-loop capture every other tool performs.  Only the
    # PIL/base64/save reply work moves into the to_thread worker (M4).
    handle, refusal, context_info = _launch_recording(
        bundle,
        session_id,
        requested=requested,
        resolved_fps=resolved_fps,
        resolved_keyframes=resolved_keyframes,
        region=region,
        include_video=bool(include_video) or bool(save),
        save=bool(save),
        video_delivery=delivery,
        audit_source="computer_record",
    )
    if refusal is not None:
        # The launch path hands back the SAME typed replies computer_record has
        # always returned for these conditions (invalid_region error dict,
        # session_stopped error dict, record_failed failure dict) or the typed
        # ``recording_limit`` rejection.
        if refusal.get("detail") is not None:
            return refusal["detail"]
        return _record_rejection(refusal["reason"], refusal["message"])
    assert handle is not None
    _record_marker_reply_extra = {
        "region": context_info.get("region"),
        "monitor_bounds": context_info.get("monitor_bounds"),
        "max_keyframes": resolved_keyframes,
        "include_video": handle.include_video,
        "started_at": handle.started_at,
        "video_delivery": delivery,
    }
    return _record_marker_reply(
        session_id=session_id,
        handle=handle,
        message=(
            f"Recording {handle.record_id} started ({requested} s at {resolved_fps} fps). "
            "Keep calling tools; collect with computer_record_result(session_id, "
            f'record_id="{handle.record_id}") — it blocks until the recording ends (≤ 10 s '
            "plus the encode) or peeks with block=false."
        ),
        extra=_record_marker_reply_extra,
    )


class _LimitRefusal(Exception):
    """Internal control flow: the caps/liveness check under the insert lock."""

    def __init__(self, reason: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.detail = detail


def _record_worker_from(handle: "_RecordHandle") -> Any:
    from .record_machine import _record_worker

    return lambda: _record_worker(handle)


@mcp.tool()
@_session_scoped
async def computer_record_result(
    session_id: str,
    record_id: str,
    block: bool = True,
    cancel: bool = False,
    include_video: NullableBool = None,
    video_delivery: NullableStr = None,
) -> Any:
    """Collect a recording: block until it ends (≤ 10 s plus the encode), peek,
    or cancel early.

    ``block=true`` (the default) waits until the recording ends and returns
    result ``verified`` with blocks: a TextContent carrying the JSON
    (``record_id``, ``duration_ms``, ``effective_fps``, ``dropped_frames``,
    ``region``, the timeline segments, the window's ``actions``, and
    ``keyframes`` — each with its fetchable ``image_id``) plus the contact
    sheet as an ImageContent (the Cortex cursor composited into every cell),
    plus the MP4 video when it is attached to THIS reply — as a written file
    path (the ``"file"`` delivery default; the entry carries ``delivery:
    "file"`` and the absolute ``path``, plus ``video_path`` on the payload) or
    as an EmbeddedResource (``"inline"``, the legacy blob) —
    (``video.included`` with ``bytes``/``width``/``height``/``fps``/``codec``/
    ``encoder``, or ``video.omitted`` naming the closed reason — ALL of them:
    not_requested / too_large / encoder_unavailable / encode_failed /
    over_budget (retried on a later call) / frames_released / write_failed).
    Most hosts truncate large tool results, so the file path is the default;
    ``video_delivery="inline"`` is ONLY for hosts known to pass video to the
    model.  An
    `encode_failed` carrying `aborted: true` (a stop/cancel aborted the
    encode) is NOT final — a later collect retries while the frames are held.
    A peek may
    report ``status:`` ``"recording"`` or ``"encoding"`` (the capture
    finished, the video encode is still running).  Fetched keyframes publish
    ``frame_to_canonical`` (``{"scale", "origin"}``) that maps the STORED
    keyframe pixels: a copy served downscaled must first be divided by its
    ``image_outbound_scale``.

    The video is encoded right after capture (in the recording worker), so the
    blocking call usually only fetches (or, with the default file delivery,
    only writes the file); a fallback encode is bounded by the
    client budget and returns ``over_budget`` when the next ladder rung cannot
    fit.  The result call's explicit ``include_video`` and ``video_delivery``
    win over the start call's; ``save`` (start only) keeps writing the files.

    ``block=false`` peeks a RUNNING recording: result ``marker``, status
    ``recording``, with ``elapsed`` and ``remaining``.  ``cancel=true`` stops
    the recording early and returns what was captured (result ``stopped``).

    If the capture failed, the reply is ``result:"failed"`` with reasons
    ``["capture_failed" | "record_worker_failed"]`` and the error text.

    An unknown or foreign-session ``record_id`` is a typed ``unknown_record``
    rejection; a finished recording that fell out of the retention ring answers
    ``record_expired``.  The video stays retrievable later by ``record_id``
    (block=false, include_video=true) while the recording is retained.

    Example: {"session_id": "S", "record_id": "R"} — or {"block": false} to peek, {"cancel": true} to stop early.
    """
    call_started = _record_now()
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    # Allowed during a plan lease (see computer_record).
    if not isinstance(record_id, str) or not record_id:
        return _record_rejection(
            "unknown_record",
            "record_id must be a non-empty string as returned by computer_record.",
        )
    if not _valid_video_delivery(video_delivery):
        return _record_rejection(
            "invalid_video_delivery",
            "video_delivery must be \"file\" or \"inline\" (null = the recording's "
            f"start flag, default \"file\"); got {video_delivery!r}.",
        )
    with _lock:
        handle = _RECORDINGS_IN_FLIGHT.get(session_id, {}).get(record_id)
        recording = _FINISHED_RECORDINGS.get(session_id, {}).get(record_id)
        expired = _EXPIRED_RECORDS.get(session_id, {}).get(record_id)
    if handle is None and recording is None:
        if expired is not None:
            return _record_rejection(
                "record_expired",
                "This recording is no longer retained: its finished result fell "
                "out of the per-session retention ring (the last "
                f"{MAX_FINISHED_RECORDINGS_PER_SESSION} recordings are kept). Start "
                "a new recording if you need the screen again.",
            )
        # Byte-identical unknown_record for a wrong id and a foreign-session id.
        return _record_rejection(
            "unknown_record",
            "No recording with this record_id exists on this session: record ids "
            "are session-scoped, are not persisted across a server restart, and a "
            "finished recording is remembered only briefly.",
        )
    if handle is None:
        # Already finished (or failed): the terminal reply (explicit
        # include_video wins; the persisted start flags fill the rest).
        reply = await _resolve_finished(
            session_id,
            bundle,
            record_id,
            result_token="verified",
            include_video=include_video,
            video_delivery=video_delivery,
            handle=None,
            started_monotonic=call_started,
        )
        # m1: a stop_session that landed during a fallback encode — the caller
        # gets session_stopped, never a verified reply for a dead session.
        try:
            _get_live_bundle(session_id)
        except (_StoppedSession, _UnknownSession) as exc:
            return _error_response(exc)
        return reply
    if cancel:
        handle.cancel.set()
        _signal_loop_waiters(handle)
        await _await_wait_wake(handle, 5.0)
        return await _after_wake(
            session_id,
            record_id,
            result_token="stopped",
            include_video=include_video,
            video_delivery=video_delivery,
            handle=handle,
            started_monotonic=call_started,
            cancel_requested=True,
        )
    segment = _wait_segment_seconds()
    # The duration is ≤ 10 s plus the (worker-side) encode — one wait segment;
    # still bounded by the client budget (the segment derives from the host's
    # client timeout).
    if block:
        await _await_wait_wake(handle, segment)
        return await _after_wake(
            session_id,
            record_id,
            result_token="verified",
            include_video=include_video,
            video_delivery=video_delivery,
            handle=handle,
            started_monotonic=call_started,
            cancel_requested=False,
        )
    now = _record_now()
    # DELTA 2 n1: a caller that peeks while the worker is still ENCODING gets
    # status "encoding" — never "recording, 0.0 s remaining".
    status = RECORD_STATUS_ENCODING if handle.encoding else "recording"
    return _record_marker_reply(
        session_id=session_id,
        handle=handle,
        status=status,
        message=(
            (
                "The recording is still ENCODING (the capture finished; the "
                "video encode is running). Call computer_record_result("
                f'session_id, record_id="{record_id}") again to collect it, or '
                "cancel=true to end it early."
            )
            if handle.encoding
            else (
                "The recording is still running "
                f"({round(max(0.0, handle.deadline_monotonic - now), 3)} s remaining). "
                "Call computer_record_result(session_id, "
                f'record_id="{record_id}") again to block for it, or cancel=true to '
                "end it early."
            )
        ),
        extra={
            "elapsed_seconds": round(
                max(0.0, now - (handle.deadline_monotonic - handle.duration_s)), 3
            ),
            "remaining_seconds": round(max(0.0, handle.deadline_monotonic - now), 3),
        },
    )


async def _after_wake(
    session_id: str,
    record_id: str,
    *,
    result_token: str,
    include_video: bool | None,
    video_delivery: str | None = None,
    handle: "_RecordHandle",
    started_monotonic: float,
    cancel_requested: bool,
) -> Any:
    """Resolve the reply after a blocking segment or a cancel (m1 race-safe).

    Resolution order: session liveness (``session_stopped`` — a stopped
    session performs no further work, including no encode), a FAILED outcome
    (B3: the caller wakes and sees it), the tombstoned recording (F2: the
    terminal state is always readable here), the cancel-flag fallback, else
    the still-running marker."""
    try:
        _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    with _lock:
        settled = _FINISHED_RECORDINGS.get(session_id, {}).get(record_id)
    if settled is not None:
        if getattr(settled, "error", None):
            # B3: the caller wakes and sees the failed outcome.
            return _failed_reply(settled)
        bundle = _get_live_bundle(session_id)
        return await _finished_reply(
            session_id,
            bundle,
            settled,
            result_token=result_token,
            handle=handle,
            include_video=include_video,
            video_delivery=video_delivery,
            started_monotonic=started_monotonic,
        )
    if getattr(handle, "outcome", None) is not None and getattr(handle.outcome, "error", None):
        # The worker filed a FAILED outcome but the session's ring already
        # dropped (stop raced the finish): answer from the outcome.
        return _failed_reply(handle.outcome)
    if handle.cancel.is_set():
        # m1: a stopped session never answers an ok:true marker.
        try:
            _get_live_bundle(session_id)
        except (_StoppedSession, _UnknownSession) as exc:
            return _error_response(exc)
        return _record_marker_reply(
            session_id=session_id,
            handle=handle,
            message=(
                "Cancellation was requested; the worker had not yet reported "
                "retirement. The recording is retired either way — call "
                "computer_record_result again to confirm."
            ),
            extra={"cancel_requested": True},
        )
    now = _record_now()
    # DELTA 2 n1: a caller that wakes while the worker is still ENCODING gets
    # status "encoding" — never "recording, 0.0 s remaining".
    status = RECORD_STATUS_ENCODING if handle.encoding else "recording"
    return _record_marker_reply(
        session_id=session_id,
        handle=handle,
        status=status,
        message=(
            (
                f"The recording is still ENCODING (the capture finished; the "
                f"video encode is running). Call computer_record_result("
                f'session_id, record_id="{record_id}") again to collect it, or '
                "cancel=true to end it early."
            )
            if handle.encoding
            else (
                "The recording is still running "
                f"({round(max(0.0, handle.deadline_monotonic - now), 3)} s remaining). "
                "Call computer_record_result(session_id, "
                f'record_id="{record_id}") again to block for it, or cancel=true to '
                "end it early."
            )
        ),
        extra={
            "elapsed_seconds": round(
                max(0.0, now - (handle.deadline_monotonic - handle.duration_s)), 3
            ),
            "remaining_seconds": round(max(0.0, handle.deadline_monotonic - now), 3),
            "waited_after_wake": cancel_requested,
        },
    )
