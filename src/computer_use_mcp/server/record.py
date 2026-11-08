"""The screen-recording tools (LAYER 4): computer_record + computer_record_result (machinery in record_machine.py, LAYER 2; the recorder core in vision.recorder, LAYER 2).

WP-R (ORVEX-2026-109) + DELTA 1 (VAL-WP-R-01): the agent records 0.5–10 s of
the screen IN THE BACKGROUND while it keeps calling any tool, then collects
the result in three forms — a change timeline plus the window's Cortex
actions, a contact sheet of timestamped keyframes (each fetchable at full
size by ``image_id``), and on request the FULL video as an MP4
``EmbeddedResource``.

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
    RECORD_STATUS_ENCODING,
    encode_video,
)
from .instance import logger, mcp
from .lifecycle import _get_live_bundle, _session_scoped
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
from .schemas import NullableBool, NullableInt, NullableRegion
from .state import _error_response, _lock, _StoppedSession, _UnknownSession
from .wait_machine import (
    _await_wait_wake,
    _signal_loop_waiters,
    _wait_segment_seconds,
)

#: M3: the safety margin subtracted from the client budget for the fallback
#: encode deadline (on top of the elapsed time of the call itself).
RECORD_RESULT_SAFETY_MARGIN_SECONDS = 5.0


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
) -> tuple[bool, bool]:
    """B2: the result call's explicit ``include_video`` wins; otherwise the
    flags persisted from the START call (on the handle while in flight, on the
    finished Recording afterwards) decide.  ``save`` has no result-call
    parameter — it always comes from the start call."""
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
    return want_video, save


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


def _save_recording(recording: Any, video_bytes: bytes | None) -> dict[str, Any]:
    """``save=True``: write the MP4 + the contact-sheet PNG under the Cortex
    recordings directory (runs in the to_thread worker — M4)."""
    saved: dict[str, Any] = {"saved": True}
    try:
        root = recordings_root()
        root.mkdir(parents=True, exist_ok=True)
        if video_bytes is not None:
            video_path = root / f"{recording.record_id}.mp4"
            video_path.write_bytes(video_bytes)
            saved["video_path"] = str(video_path)
        if recording.sheet_png_b64:
            sheet_path = root / f"{recording.record_id}_sheet.png"
            sheet_path.write_bytes(base64.b64decode(recording.sheet_png_b64))
            saved["sheet_path"] = str(sheet_path)
    except Exception as exc:  # noqa: BLE001 - a failed save is reported, never a crash
        saved = {"saved": False, "save_error": f"{type(exc).__name__}: {exc}"}
    return saved


def _finished_reply_worker(
    session_id: str,
    recording: Any,
    *,
    blob_attached: bool,
    want_video: bool,
    save: bool,
    video_bytes: bytes | None,
) -> tuple[dict[str, Any], tuple[str, str, float | None] | None, str | None]:
    """M4: EVERY heavy piece of the finished reply in ONE synchronous worker —
    the payload build, the sheet's outbound ladder, the sheet PNG's outbound
    re-encode, the MP4's base64 blob and the ``save`` file writes.  Returns
    ``(payload, (sheet_b64, mime, scale), blob_b64)``; the caller assembles the
    MCP blocks on the loop (cheap dict/argument work only)."""
    video_entry = _video_entry(recording, blob_attached=blob_attached, want_video=want_video)
    payload = _finished_payload(session_id, recording, video_entry=video_entry)
    if video_entry.get("included") and video_entry.get("uri") is None:
        video_entry["uri"] = f"cortex://recording/{recording.record_id}.mp4"
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
        payload.update(_save_recording(recording, video_bytes))
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
    started_monotonic: float | None = None,
) -> Any:
    """The verified/stopped reply: TextContent JSON + ImageContent contact sheet
    (+ the MP4 ``EmbeddedResource`` when video is attached to THIS reply).

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
    want_video, save = _resolve_flags(recording, handle, include_video)
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
    blob_attached = bool(video_bytes is not None and want_video)
    payload, sheet_tuple, blob_b64 = await asyncio.to_thread(
        _finished_reply_worker,
        session_id,
        recording,
        blob_attached=blob_attached,
        want_video=want_video,
        save=save,
        video_bytes=video_bytes,
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
        started_monotonic=started_monotonic,
    )


# --- the tools --------------------------------------------------------------------------------


@mcp.tool()
@_session_scoped
async def computer_record(
    session_id: str,
    duration_s: float,
    region: NullableRegion = None,
    fps: NullableInt = None,
    max_keyframes: NullableInt = None,
    include_video: NullableBool = None,
    save: NullableBool = None,
) -> Any:
    """Start recording 0.5–10 s of the screen IN THE BACKGROUND and return AT ONCE.

    Returns a marker reply: ``record_id`` ("rec_" + 16 hex), ``status:
    "recording"``, ``ends_at`` (epoch seconds) and the effective fps/region.
    KEEP CALLING TOOLS while it records — the whole point.  Collect the product
    with computer_record_result(session_id, record_id): a change timeline plus
    the Cortex actions dispatched inside the window, a contact sheet of
    timestamped keyframes (each fetchable at full size by ``image_id`` through
    computer_fetch_image, the Cortex cursor composited), and on request the
    FULL video as an MP4 EmbeddedResource.

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
    pool's byte cap before any video was stored).  ``save=true`` writes the
    MP4 + the contact-sheet PNG under the Cortex recordings directory and
    returns ``video_path`` / ``sheet_path`` (it implies video production).
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
    if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)):
        return _record_rejection(
            "invalid_record_duration",
            f"duration_s must be a number of seconds; got {duration_s!r}.",
        )
    requested = float(duration_s)
    if not math.isfinite(requested) or not (
        RECORD_DURATION_BAND[0] <= requested <= RECORD_DURATION_BAND[1]
    ):
        return _record_rejection(
            "invalid_record_duration",
            f"duration_s must be between {RECORD_DURATION_BAND[0]} and "
            f"{RECORD_DURATION_BAND[1]} seconds; got {duration_s!r}. Nothing was started.",
        )
    resolved_fps = RECORD_FPS_DEFAULT if fps is None else int(fps)
    if not RECORD_FPS_BAND[0] <= resolved_fps <= RECORD_FPS_BAND[1]:
        return _record_rejection(
            "invalid_record_fps",
            f"fps must be between {RECORD_FPS_BAND[0]} and {RECORD_FPS_BAND[1]} "
            f"(default {RECORD_FPS_DEFAULT}); got {fps!r}. Nothing was started.",
        )
    resolved_keyframes = (
        RECORD_MAX_KEYFRAMES_DEFAULT if max_keyframes is None else int(max_keyframes)
    )
    if not RECORD_MAX_KEYFRAMES_BAND[0] <= resolved_keyframes <= RECORD_MAX_KEYFRAMES_BAND[1]:
        return _record_rejection(
            "invalid_record_keyframes",
            f"max_keyframes must be between {RECORD_MAX_KEYFRAMES_BAND[0]} and "
            f"{RECORD_MAX_KEYFRAMES_BAND[1]} (default {RECORD_MAX_KEYFRAMES_DEFAULT}); "
            f"got {max_keyframes!r}. Nothing was started.",
        )
    if include_video is not None and not isinstance(include_video, bool):
        return _record_rejection(
            "invalid_record_param",
            f"include_video must be a boolean or null; got {include_video!r}.",
        )
    if save is not None and not isinstance(save, bool):
        return _record_rejection(
            "invalid_record_param",
            f"save must be a boolean or null; got {save!r}.",
        )
    # N2 (Commander decision): the capture NEVER runs on an executor thread —
    # the backend's mss instance and payload cache are thread-bound, and this
    # is the same on-the-loop capture every other tool performs.  Only the
    # PIL/base64/save reply work moves into the to_thread worker (M4).
    resolved = _resolve_recording_space(bundle, region)
    if isinstance(resolved, dict):
        return resolved
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
        include_video=bool(include_video) or bool(save),
        save=bool(save),
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
            return refusal.detail
        return _record_rejection(refusal.reason, refusal.message)
    try:
        thread.start()
    except Exception as exc:  # noqa: BLE001 - no phantom registration
        from .record_machine import _retire_record

        _retire_record(handle)
        return {
            "ok": False,
            "result": "failed",
            "error": "record_failed",
            "session_id": session_id,
            "record_id": record_id,
            "message": (
                f"The recording could not be started ({type(exc).__name__}: {exc}); "
                "nothing is registered and no slot is consumed."
            ),
        }
    _audit(
        "record_start",
        result="ok",
        metadata={
            "record_id": record_id,
            "duration_s": requested,
            "fps": resolved_fps,
            "region": context_info.get("region"),
            "include_video": handle.include_video,
        },
    )
    return _record_marker_reply(
        session_id=session_id,
        handle=handle,
        message=(
            f"Recording {record_id} started ({requested} s at {resolved_fps} fps). "
            "Keep calling tools; collect with computer_record_result(session_id, "
            f'record_id="{record_id}") — it blocks until the recording ends (≤ 10 s '
            "plus the encode) or peeks with block=false."
        ),
        extra={
            "region": context_info.get("region"),
            "monitor_bounds": context_info.get("monitor_bounds"),
            "max_keyframes": resolved_keyframes,
            "include_video": handle.include_video,
            "started_at": handle.started_at,
        },
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
) -> Any:
    """Collect a recording: block until it ends (≤ 10 s plus the encode), peek,
    or cancel early.

    ``block=true`` (the default) waits until the recording ends and returns
    result ``verified`` with blocks: a TextContent carrying the JSON
    (``record_id``, ``duration_ms``, ``effective_fps``, ``dropped_frames``,
    ``region``, the timeline segments, the window's ``actions``, and
    ``keyframes`` — each with its fetchable ``image_id``) plus the contact
    sheet as an ImageContent (the Cortex cursor composited into every cell),
    plus the MP4 as an EmbeddedResource when video is attached to THIS reply
    (``video.included`` with ``bytes``/``width``/``height``/``fps``/``codec``/
    ``encoder``, or ``video.omitted`` naming the closed reason — ALL of them:
    not_requested / too_large / encoder_unavailable / encode_failed /
    over_budget (retried on a later call) / frames_released).  An
    `encode_failed` carrying `aborted: true` (a stop/cancel aborted the
    encode) is NOT final — a later collect retries while the frames are
    held.  A peek may
    report ``status:`` ``"recording"`` or ``"encoding"`` (the capture
    finished, the video encode is still running).  Fetched keyframes publish
    ``frame_to_canonical`` (``{"scale", "origin"}``) that maps the STORED
    keyframe pixels: a copy served downscaled must first be divided by its
    ``image_outbound_scale``.

    The video is encoded right after capture (in the recording worker), so the
    blocking call usually only fetches; a fallback encode is bounded by the
    client budget and returns ``over_budget`` when the next ladder rung cannot
    fit.  The result call's explicit ``include_video`` wins over the start
    call's; ``save`` (start only) keeps writing the files.

    ``block=false`` peeks a RUNNING recording: result ``marker``, status
    ``recording``, with ``elapsed`` and ``remaining``.  ``cancel=true`` stops
    the recording early and returns what was captured (result ``stopped``).

    If the capture failed, the reply is ``result:"failed"`` with reasons
    ``["capture_failed" | "record_worker_failed"]`` and the error text.

    An unknown or foreign-session ``record_id`` is a typed ``unknown_record``
    rejection; a finished recording that fell out of the retention ring answers
    ``record_expired``.  The video stays retrievable later by ``record_id``
    (block=false, include_video=true) while the recording is retained.
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
