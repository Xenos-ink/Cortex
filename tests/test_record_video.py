"""WP-R + DELTA 1 (VAL-WP-R-01) — the MP4 video path.

Real PyAV encodes decoded back (frame count, size, fps, duration, REAL
timestamps), the EmbeddedResource shape, the cap ladder (quality before
resolution, deadline → over_budget, no rung produced → encode_failed), the
license-aware encoder preference (h264_mf first, libx264 fallback — selection
logic with fakes, one real encode per available encoder), the 2 MiB default
cap with the CORTEX_RECORD_VIDEO_MAX_MB band, and the M5 single-flight /
replace accounting.  The ``av`` import seam (``vision.recorder._import_av``)
and the encode worker (``vision.recorder._encode_one``) are monkeypatched —
never sys.modules — for the fake cases.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

import computer_use_mcp.vision.recorder as recorder_module
from computer_use_mcp.vision.evidence import (
    RECORDING_STORE_KEY,
    RecordingEvidenceStore,
)
from computer_use_mcp.vision.recorder import (
    VIDEO_OMITTED_ENCODE_FAILED,
    VIDEO_OMITTED_ENCODER_UNAVAILABLE,
    VIDEO_OMITTED_OVER_BUDGET,
    VIDEO_OMITTED_TOO_LARGE,
    Recording,
    RecordedFrame,
    _decode_video_info,
    encode_video,
    encoder_preference,
)
import computer_use_mcp.server.record as server_record


# --- harness ---------------------------------------------------------------------------------


def _jpeg_frame(index: int, size: tuple[int, int] = (160, 120), t_ms: float | None = None) -> RecordedFrame:
    image = Image.new("RGB", size, (240, 240, 240))
    x = index * 10 % (size[0] - 20)
    image.paste((30, 30, 30), (x, 10, x + 20, 30))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=70)
    return RecordedFrame(
        t_ms=index * 200.0 if t_ms is None else t_ms,
        jpeg=buffer.getvalue(),
        pointer_x=10.0,
        pointer_y=10.0,
        pointer_state="normal" if index % 2 else "pressed",
        in_image=True,
        width=size[0],
        height=size[1],
    )


def _recording(count: int = 4, fps: int = 5) -> Recording:
    recording = Recording(
        record_id="rec_video00000000",
        session_id="sess_video",
        duration_s=count / fps,
        fps=fps,
        requested_monotonic_start=1000.0,
        region=[0, 0, 160, 120],
        monitor_bounds=[0, 0, 160, 120],
        canonical_origin=(0, 0),
        canonical_size=(160, 120),
    )
    recording.frames = [_jpeg_frame(index) for index in range(count)]
    recording.frame_count = count
    recording.actual_duration_s = count / fps
    sheet = Image.new("RGB", (32, 16), (10, 10, 10))
    buffer = io.BytesIO()
    sheet.save(buffer, format="PNG")
    recording.sheet_png_b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
    return recording


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _json(block: Any) -> dict[str, Any]:
    return json.loads(block.text)


class _FakeStream:
    def __init__(self, fake: "_FakeAvModule", codec: str, rate: int) -> None:
        self.width = 0
        self.height = 0
        self.pix_fmt = "yuv420p"
        self.options: dict[str, str] = {}
        self.bit_rate = 0
        self.rate = rate
        self.codec = codec

    def encode(self, frame: Any = None) -> list[Any]:
        return []


class _FakeContainer:
    def __init__(self, fake: "_FakeAvModule", buffer: Any) -> None:
        self.fake = fake
        self.buffer = buffer
        self.stream: _FakeStream | None = None

    def add_stream(self, codec: str, rate: int) -> Any:
        if codec in self.fake.failing_encoders:
            self.fake.failures.append((codec, "add_stream"))
            raise RuntimeError(f"encoder {codec} cannot open (fake)")
        self.stream = _FakeStream(self.fake, codec, rate)
        return self.stream

    def mux(self, packet: Any) -> None:
        pass

    def close(self) -> None:
        self.buffer.write(self.fake.blob)


class _FakeAvModule:
    """A fake ``av`` module: per-codec failure injection + a fixed blob."""

    def __init__(self, blob: bytes, failing_encoders: set[str]) -> None:
        self.blob = blob
        self.failing_encoders = failing_encoders
        self.failures: list[tuple[str, str]] = []

    def open(self, buffer: Any, mode: str, format: str) -> _FakeContainer:
        return _FakeContainer(self, buffer)

    def VideoFrame(self, width: int, height: int, fmt: str) -> Any:
        frame = SimpleNamespace(planes=[], pts=None)
        # rgb24: one plane, unpadded linesize.
        frame.planes.append(
            SimpleNamespace(line_size=width * 3, buffer_size=width * height * 3, update=lambda data: None)
        )
        return frame


class _EncodeCounter:
    """A fake rung worker that counts encodes and (optionally) sleeps."""

    def __init__(self, blob: bytes, delay: float = 0.0) -> None:
        self.blob = blob
        self.delay = delay
        self.count = 0
        self.lock = threading.Lock()

    def __call__(self, av_module, recording, snapshot, fps, width, height, encoder, quality, composite, scale, should_abort=None):
        with self.lock:
            self.count += 1
        if self.delay:
            import time as _time

            _time.sleep(self.delay)
        return self.blob


# --- the REAL encode, decoded back -------------------------------------------------------------


def test_real_tiny_encode_round_trips_through_av() -> None:
    recording = _recording(count=4, fps=5)
    data, info = encode_video(recording, fps=5)
    assert data is not None
    assert info["included"] is True
    assert info["codec"] == "h264"
    assert info["fps"] == 5
    assert info["frames"] == 4
    assert info["width"] == 160 and info["height"] == 120
    assert info["encoder"] in encoder_preference()  # the encoder actually used
    assert len(data) <= recorder_module.video_max_bytes()
    decoded = _decode_video_info(data)
    assert decoded["frames"] == 4
    assert decoded["width"] == 160 and decoded["height"] == 120
    assert decoded["fps"] == pytest.approx(5.0)


def test_even_dimension_enforcement_crops_not_resizes() -> None:
    """DELTA 1 nit: parity is a CROP (never a distorting resize) — the odd
    161x121 source is cropped to even."""
    recording = _recording(count=3, fps=5)
    recording.frames = [_jpeg_frame(index, size=(161, 121)) for index in range(3)]
    recording.frame_count = 3
    recording.canonical_size = (161, 121)
    data, info = encode_video(recording, fps=5)
    assert data is not None
    assert info["width"] % 2 == 0 and info["height"] % 2 == 0
    assert info["width"] == 160 and info["height"] == 120  # cropped, not scaled


def test_real_timestamps_keep_duration_through_drops() -> None:
    """m3: the video carries REAL timestamps — two dropped slots make the
    decoded duration ≈ real time (1.4 s), not frames/fps (1.0 s)."""
    recording = _recording(count=0)
    recording.frames = [
        _jpeg_frame(0, t_ms=0.0),
        _jpeg_frame(1, t_ms=200.0),
        _jpeg_frame(2, t_ms=400.0),
        _jpeg_frame(3, t_ms=1000.0),  # 2 dropped slots (400→1000 ms)
        _jpeg_frame(4, t_ms=1200.0),
    ]
    recording.frame_count = 5
    data, info = encode_video(recording, fps=5)
    assert data is not None
    decoded = _decode_video_info(data)
    assert decoded["frames"] == 5
    # 5 frames at a nominal 5 fps would be 1.0 s; real time is 1.4 s.
    assert decoded["duration_s"] == pytest.approx(1.4, abs=0.15)
    assert decoded["duration_s"] > 1.2


def test_real_h264_mf_encode_or_clean_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """DELTA 1 item 11: one REAL encode per available encoder.  h264_mf
    (Media Foundation, LGPL-safe) is tried first; N editions without MF skip
    cleanly."""
    monkeypatch.setenv(recorder_module.ENCODER_ENV, "h264_mf")
    recording = _recording(count=3, fps=5)
    data, info = encode_video(recording, fps=5)
    if data is None and info.get("omitted") in (
        VIDEO_OMITTED_ENCODER_UNAVAILABLE,
        VIDEO_OMITTED_ENCODE_FAILED,
    ):
        pytest.skip("h264_mf cannot open on this machine (N edition / no Media Foundation)")
    assert data is not None
    assert info["included"] is True
    assert info["encoder"] == "h264_mf"
    decoded = _decode_video_info(data)
    assert decoded["frames"] == 3


def test_real_libx264_encode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(recorder_module.ENCODER_ENV, "libx264")
    recording = _recording(count=3, fps=5)
    data, info = encode_video(recording, fps=5)
    assert data is not None
    assert info["encoder"] == "libx264"
    assert _decode_video_info(data)["frames"] == 3


# --- encoder selection logic (fakes) -------------------------------------------------------------


def test_encoder_falls_back_to_libx264_when_h264_mf_cannot_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blob = b"\x01" * 1024  # fits any target
    fake = _FakeAvModule(blob, failing_encoders={"h264_mf"})
    monkeypatch.setattr(recorder_module, "_import_av", lambda: fake)
    recording = _recording(count=2, fps=5)
    data, info = encode_video(recording, fps=5)
    assert data == blob
    assert info["included"] is True
    assert info["encoder"] == "libx264"  # the fallback was used
    assert fake.failures and fake.failures[0][0] == "h264_mf"


def test_no_encoder_opens_is_encoder_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """DELTA 1 M7/item 11: if NO rung produced bytes (every encoder failed to
    open), the reason is encoder_unavailable."""
    fake = _FakeAvModule(b"", failing_encoders={"h264_mf", "libx264"})
    monkeypatch.setattr(recorder_module, "_import_av", lambda: fake)
    recording = _recording(count=2, fps=5)
    data, info = encode_video(recording, fps=5)
    assert data is None
    assert info["omitted"] == VIDEO_OMITTED_ENCODER_UNAVAILABLE


def test_every_rung_raises_is_encoder_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """DELTA 1 M7: encoders OPEN but every rung raises → no rung produced
    bytes → encoder_unavailable (the tightened answer; the old test accepted
    too_large here)."""
    monkeypatch.setattr(recorder_module, "_import_av", lambda: object())

    def exploding_encode(av_module, recording, snapshot, fps, width, height, encoder, quality, composite, scale, should_abort=None):
        raise RuntimeError("encoder exploded")

    monkeypatch.setattr(recorder_module, "_encode_one", exploding_encode)
    recording = _recording(count=2, fps=5)
    data, info = encode_video(recording, fps=5)
    assert data is None
    assert info["omitted"] == VIDEO_OMITTED_ENCODER_UNAVAILABLE


# --- the cap ladder --------------------------------------------------------------------------


def test_cap_ladder_ends_in_too_large_with_no_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    oversized = b"\x00" * (recorder_module.video_max_bytes() + 1024)
    counter = _EncodeCounter(oversized)
    monkeypatch.setattr(recorder_module, "_import_av", lambda: object())
    monkeypatch.setattr(recorder_module, "_encode_one", counter)
    recording = _recording(count=3, fps=5)
    data, info = encode_video(recording, fps=5)
    assert data is None
    assert info["omitted"] == VIDEO_OMITTED_TOO_LARGE
    assert info["bytes"] == len(oversized)
    # The frames and the timeline are ALWAYS returned regardless.
    assert recording.frame_count == 3
    assert recording.timeline() == []
    # DELTA 1 nit: quality is stepped BEFORE resolution — every attempted
    # rung of the first quality level happens at scale 1.0 (160x120).
    assert counter.count >= 3


def test_ladder_stops_at_the_480_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    resolutions: list[tuple[int, int]] = []

    def fake_encode_one(av_module, recording, snapshot, fps, width, height, encoder, quality, composite, scale, should_abort=None):
        resolutions.append((width, height))
        return b"\x00" * (recorder_module.video_max_bytes() + 1)

    monkeypatch.setattr(recorder_module, "_encode_one", fake_encode_one)
    monkeypatch.setattr(recorder_module, "_import_av", lambda: object())
    recording = _recording(count=2, fps=5)  # 160x120 source: already below the floor
    encode_video(recording, fps=5)
    # The source is below the 480 floor at scale 1.0, so the resolution ladder
    # never descends: every rung stays at the source size.
    assert resolutions and set(resolutions) == {(160, 120)}


def test_ladder_descends_resolution_and_steps_quality_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELTA 2 weak-test fix: the fake's output only fits at a SMALLER size —
    the ladder actually DESCENDS — and the attempt order proves quality is
    stepped BEFORE resolution (all CRF rungs at 1280 before any 960 rung)."""
    attempts: list[tuple[int, int, int | None]] = []

    def size_dependent_encode(av_module, recording, snapshot, fps, width, height, encoder, quality, composite, scale, should_abort=None):
        attempts.append((width, height, quality))
        # Only the LAST size above the 480 floor (540×304) at the loosest CRF
        # fits the tiny cap — the ladder must actually DESCEND to reach it.
        if max(width, height) <= 540 and quality == 35:
            return b"\x01" * 512
        return b"\x00" * (recorder_module.video_max_bytes() + 1)

    monkeypatch.setattr(recorder_module, "_encode_one", size_dependent_encode)
    monkeypatch.setattr(recorder_module, "_import_av", lambda: object())
    monkeypatch.setenv(recorder_module.ENCODER_ENV, "libx264")
    recording = _recording(count=2, fps=5)
    recording.frames = [_jpeg_frame(index, size=(1280, 720)) for index in range(2)]
    recording.frame_count = 2
    data, info = encode_video(recording, fps=5)
    assert data == b"\x01" * 512
    # The winning rung is the LAST size above the 480 floor (540×304), at
    # the loosest quality.
    assert (info["width"], info["height"]) == (540, 304)
    # Quality BEFORE resolution: the FIRST attempt is the default quality at
    # the full 1280 size, and all three 1280 rungs precede the first 960.
    assert attempts[0] == (1280, 720, None)
    first_downsize = next(index for index, a in enumerate(attempts) if a[0] != 1280)
    assert [a[2] for a in attempts[:first_downsize]] == [None, 28, 35]
    assert attempts[first_downsize][0] == 960
    monkeypatch.delenv(recorder_module.ENCODER_ENV, raising=False)


def test_mid_encode_failure_falls_back_to_libx264_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELTA 2 n7: h264_mf OPENS but fails MID-encode — the ladder falls back
    to libx264 ONCE (h264_mf is attempted exactly one rung, not all of its
    rungs)."""
    encode_calls: dict[str, int] = {"h264_mf": 0, "libx264": 0}

    def failing_mid_encode(av_module, recording, snapshot, fps, width, height, encoder, quality, composite, scale, should_abort=None):
        encode_calls[encoder] += 1
        if encoder == "h264_mf":
            raise RuntimeError("mf device lost mid-encode")
        return b"\x02" * 256

    monkeypatch.setattr(recorder_module, "_encode_one", failing_mid_encode)
    monkeypatch.setattr(recorder_module, "_import_av", lambda: object())
    recording = _recording(count=2, fps=5)
    data, info = encode_video(recording, fps=5)
    assert data == b"\x02" * 256
    assert info["encoder"] == "libx264"
    assert encode_calls["h264_mf"] == 1  # ONE attempt, then the fallback
    assert encode_calls["libx264"] == 1


def test_open_failure_is_encoder_unavailable_but_mid_encode_is_encode_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELTA 2 n7: every encoder failing to OPEN is encoder_unavailable; an
    encoder OPENING but failing mid-encode with no bytes is encode_failed."""
    monkeypatch.setattr(recorder_module, "_import_av", lambda: object())

    def failing_open(av_module, recording, snapshot, fps, width, height, encoder, quality, composite, scale, should_abort=None):
        raise recorder_module.EncoderOpenError(f"{encoder} cannot open")

    monkeypatch.setattr(recorder_module, "_encode_one", failing_open)
    recording = _recording(count=2, fps=5)
    data, info = encode_video(recording, fps=5)
    assert info["omitted"] == "encoder_unavailable"

    def failing_mid(av_module, recording, snapshot, fps, width, height, encoder, quality, composite, scale, should_abort=None):
        return None  # mid-encode failure, no bytes ever

    monkeypatch.setattr(recorder_module, "_encode_one", failing_mid)
    data, info = encode_video(recording, fps=5)
    assert info["omitted"] == "encode_failed"


def test_encode_deadline_floors_at_zero_and_first_rung_estimate_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELTA 3 n2: `_encode_deadline` floors at ZERO (never a guaranteed
    second), and the FIRST rung's budget check uses the documented per-frame
    estimate — a rung that cannot fit the remaining budget is SKIPPED."""
    from computer_use_mcp.server.record import (
        RECORD_RESULT_SAFETY_MARGIN_SECONDS,
        _encode_deadline,
        _record_now,
    )
    from computer_use_mcp.desktop.client_budget import client_timeout_seconds

    monkeypatch.delenv("CORTEX_CLIENT_TIMEOUT_SECONDS", raising=False)
    started = _record_now() - 600.0  # the call "spent" 10 minutes already
    deadline = _encode_deadline(started)
    # Floor 0: the deadline is NOW (no budget), never now + 1 s.
    assert deadline <= _record_now() + 0.05
    assert client_timeout_seconds() - 600.0 - RECORD_RESULT_SAFETY_MARGIN_SECONDS < 0
    # The first rung's estimate: frames × RECORDER_PER_FRAME_ENCODE_ESTIMATE.
    recording = _recording(count=4, fps=5)  # 4 frames × 0.02 = 0.08 s estimate
    estimate = 4 * recorder_module.RECORDER_PER_FRAME_ENCODE_ESTIMATE_SECONDS
    clock = {"now": 100.0}
    # Budget smaller than the first rung's estimate → skipped entirely.
    data, info = encode_video(
        recording, fps=5, deadline=clock["now"] + estimate - 0.01, clock=lambda: clock["now"]
    )
    assert data is None and info["omitted"] == "over_budget"
    # Budget larger than the estimate → the first rung starts.
    data, info = encode_video(recording, fps=5, target_bytes=8 * 1024 * 1024)
    assert data is not None  # no deadline: the real ladder runs


def test_concurrent_eviction_between_rungs_never_zero_frame_encodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELTA 4 F1: the frames SNAPSHOT is taken once by encode_video — a
    concurrent pool eviction (the owner callback emptying recording.frames)
    between rungs can never make a later rung encode zero frames.  The fake
    rung evicts the owner's frames on its FIRST call; the SECOND call still
    sees 3 frames in its snapshot and its result is the one reported
    (included, frames: 3)."""
    recording = _recording(count=3, fps=5)
    pool = RecordingEvidenceStore("sess_f1")
    pool.store_frames(record_id=recording.record_id, total_bytes=3000, owner=recording)
    calls: list[int] = []

    def snapshot_aware_encode(av_module, rec, snapshot, fps, width, height, encoder, quality, composite, scale, should_abort=None):
        calls.append(len(snapshot))
        if len(calls) == 1:
            rec.release_frame_payloads()  # the concurrent eviction lands between rungs
            return b"\x00" * (recorder_module.video_max_bytes() + 1)  # oversize → next rung
        return b"\x01" * 512  # fits — and the snapshot STILL holds 3 frames

    monkeypatch.setattr(recorder_module, "_encode_one", snapshot_aware_encode)
    monkeypatch.setattr(recorder_module, "_import_av", lambda: object())
    monkeypatch.setenv(recorder_module.ENCODER_ENV, "libx264")
    data, info = encode_video(recording, fps=5)
    assert data == b"\x01" * 512
    assert info["included"] is True
    # Both rungs saw the FULL snapshot — the post-eviction second rung still
    # had 3 frames (never a zero-frame encode).
    assert calls == [3, 3]
    assert info["frames"] == 3
    monkeypatch.delenv(recorder_module.ENCODER_ENV, raising=False)


def test_zero_frame_rung_is_never_included(monkeypatch: pytest.MonkeyPatch) -> None:
    """DELTA 4 F1 guard: a rung that encodes ZERO frames (every JPEG fails to
    decode — `_iter_encode_frames` yields nothing) is never reported as
    included: the rung fails, the ladder falls through, and the answer is
    encode_failed."""
    monkeypatch.setattr(recorder_module, "_import_av", lambda: object())
    monkeypatch.setenv(recorder_module.ENCODER_ENV, "libx264")

    monkeypatch.setattr(recorder_module, "_import_av", lambda: _FakeAvModule(b"\x00" * 16, failing_encoders=set()))

    def yielding_nothing(frames, rec, composite, width, height, scale, fps):
        return iter([])

    monkeypatch.setattr(recorder_module, "_iter_encode_frames", yielding_nothing)
    recording = _recording(count=2, fps=5)
    data, info = encode_video(recording, fps=5)
    assert data is None
    assert info["omitted"] == "encode_failed"
    assert not info.get("included")
    monkeypatch.delenv(recorder_module.ENCODER_ENV, raising=False)


def test_ladder_reuses_a_final_omission_and_retries_over_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELTA 2 n3 (at the reply level, where the rule lives): a stored FINAL
    omission (too_large) is REUSED on later collections without re-running
    the ladder; only `over_budget` is retried."""
    recording = _recording(count=2, fps=5)
    pool = RecordingEvidenceStore("sess_reuse")
    bundle = SimpleNamespace(extra={RECORDING_STORE_KEY: pool})
    counter = _EncodeCounter(b"\x00" * (recorder_module.video_max_bytes() + 1))
    monkeypatch.setattr(recorder_module, "_import_av", lambda: object())
    monkeypatch.setattr(recorder_module, "_encode_one", counter)
    first = _run(
        server_record._finished_reply(
            "sess_reuse", bundle, recording, result_token="verified", include_video=True
        )
    )
    first_payload = _json([block for block in first if block.type == "text"][0])
    assert first_payload["video"] == {"omitted": "too_large", "bytes": len(counter.blob)}
    # The ladder ran its h264_mf bitrate rungs (3), all oversize.
    assert counter.count == 3
    # The FINAL omission is reused — no second ladder run.
    assert (
        recording.frames_released is False and len(recording.frames) == 2
    ), f"frames_released={recording.frames_released} frames={len(recording.frames)} accounted={recording.frames_accounted}"
    second = _run(
        server_record._finished_reply(
            "sess_reuse", bundle, recording, result_token="verified", include_video=True
        )
    )
    second_payload = _json([block for block in second if block.type == "text"][0])
    assert second_payload["video"] == first_payload["video"]
    assert counter.count == 3  # reused — NO further ladder run
    # over_budget is RETRIED: a fresh recording whose stored omission is
    # over_budget re-runs the ladder (a later call has a fresh budget).
    recording2 = _recording(count=2, fps=5)
    recording2.video = {"omitted": "over_budget", "bytes": 0}
    counter2 = _EncodeCounter(b"\x05" * 128)
    monkeypatch.setattr(recorder_module, "_encode_one", counter2)
    third = _run(
        server_record._finished_reply(
            "sess_reuse", bundle, recording2, result_token="verified", include_video=True
        )
    )
    assert counter2.count == 1  # the retry actually ran
    third_payload = _json([block for block in third if block.type == "text"][0])
    assert third_payload["video"]["included"] is True


def test_deadline_stops_the_ladder_with_over_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """M3: the ladder checks the clock before every rung — an expired budget
    returns over_budget (the NEW closed omission reason) with no blob."""
    calls = {"rungs": 0}

    def slow_encode(av_module, recording, snapshot, fps, width, height, encoder, quality, composite, scale, should_abort=None):
        calls["rungs"] += 1
        return b"\x00" * (recorder_module.video_max_bytes() + 1)

    monkeypatch.setattr(recorder_module, "_encode_one", slow_encode)
    monkeypatch.setattr(recorder_module, "_import_av", lambda: object())
    recording = _recording(count=3, fps=5)
    clock = {"now": 100.0}
    data, info = encode_video(recording, fps=5, deadline=100.0, clock=lambda: clock["now"])
    assert data is None
    assert info["omitted"] == VIDEO_OMITTED_OVER_BUDGET
    assert calls["rungs"] == 0  # the very first rung was refused
    # A budget that fits SOME rungs stops mid-ladder: advance the fake clock
    # past the deadline after one rung.
    clock["now"] = 90.0

    def advancing_encode(av_module, recording, snapshot, fps, width, height, encoder, quality, composite, scale, should_abort=None):
        calls["rungs"] += 1
        clock["now"] = 200.0  # the next check is past the deadline
        return b"\x00" * (recorder_module.video_max_bytes() + 1)

    monkeypatch.setattr(recorder_module, "_encode_one", advancing_encode)
    data, info = encode_video(recording, fps=5, deadline=100.0, clock=lambda: clock["now"])
    assert data is None
    assert info["omitted"] == VIDEO_OMITTED_OVER_BUDGET
    assert calls["rungs"] >= 1  # it encoded at least one rung first


# --- the EmbeddedResource reply shape -----------------------------------------------------------


def test_finished_reply_carries_the_mp4_embedded_resource() -> None:
    recording = _recording(count=3, fps=5)
    pool = RecordingEvidenceStore("sess_video")
    bundle = SimpleNamespace(extra={RECORDING_STORE_KEY: pool})
    blocks = _run(
        server_record._finished_reply(
            "sess_video", bundle, recording, result_token="verified", include_video=True
        )
    )
    texts = [block for block in blocks if block.type == "text"]
    resources = [block for block in blocks if block.type == "resource"]
    images = [block for block in blocks if block.type == "image"]
    assert len(texts) == 1 and len(resources) == 1 and len(images) == 1
    payload = _json(texts[0])
    assert payload["video"]["included"] is True
    assert payload["video"]["codec"] == "h264"
    assert payload["video"]["encoder"] in encoder_preference()
    assert payload["result"] == "verified" and payload["ok"] is True
    resource = resources[0]
    assert resource.resource.mimeType == "video/mp4"
    # B1: the URI carries the TOOL's record id.
    assert str(resource.resource.uri) == f"cortex://recording/{recording.record_id}.mp4"
    raw = base64.b64decode(resource.resource.blob)
    assert len(raw) > 0
    assert _decode_video_info(raw)["frames"] == 3
    # The held MP4 is priced into the recording pool's byte cap (b64 pricing).
    assert pool.total_bytes() > 0


def test_video_entry_says_what_happened_without_a_request() -> None:
    recording = _recording(count=2, fps=5)
    bundle = SimpleNamespace(extra={RECORDING_STORE_KEY: RecordingEvidenceStore("sess_video")})
    blocks = _run(
        server_record._finished_reply(
            "sess_video", bundle, recording, result_token="verified", include_video=False
        )
    )
    payload = _json([block for block in blocks if block.type == "text"][0])
    # M7: the entry is built PER REPLY — "included" never lies about a blob.
    assert payload["video"] == {"omitted": "not_requested", "bytes": 0}
    assert not [block for block in blocks if block.type == "resource"]


def test_video_entry_is_per_reply_after_a_stored_encode() -> None:
    """M7: after one encode, a reply that attaches no blob says not_requested
    (the stale bug copied {"included": true} with no resource block)."""
    recording = _recording(count=2, fps=5)
    pool = RecordingEvidenceStore("sess_video")
    bundle = SimpleNamespace(extra={RECORDING_STORE_KEY: pool})
    _run(
        server_record._finished_reply(
            "sess_video", bundle, recording, result_token="verified", include_video=True
        )
    )
    assert recording.video is not None and recording.video.get("included")
    blocks = _run(
        server_record._finished_reply(
            "sess_video", bundle, recording, result_token="verified", include_video=False
        )
    )
    payload = _json([block for block in blocks if block.type == "text"][0])
    assert payload["video"] == {"omitted": "not_requested", "bytes": 0}
    assert not [block for block in blocks if block.type == "resource"]


def test_video_stays_retrievable_by_record_id_while_retained() -> None:
    recording = _recording(count=2, fps=5)
    pool = RecordingEvidenceStore("sess_video")
    bundle = SimpleNamespace(extra={RECORDING_STORE_KEY: pool})
    _run(
        server_record._finished_reply(
            "sess_video", bundle, recording, result_token="verified", include_video=True
        )
    )
    retained = pool.lookup_video(recording.record_id)
    assert retained is not None and retained.kind == "video"
    assert retained.mime == "video/mp4"
    # A later result call reuses the stored result — NO second encode (the
    # fixed reuse test: nothing is reset, the pool is the source of truth).
    blocks = _run(
        server_record._finished_reply(
            "sess_video", bundle, recording, result_token="verified", include_video=True
        )
    )
    resource = [block for block in blocks if block.type == "resource"][0]
    assert base64.b64decode(resource.resource.blob) == retained.video_bytes


def test_store_video_replace_accounting_and_pricing_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    """M5 + NIT: replacing a video subtracts the old entry's size (no phantom
    bytes), and video is priced like keyframes (base64 length)."""
    pool = RecordingEvidenceStore("sess_replace")
    first = b"a" * 1000
    assert pool.store_video(
        record_id="rec_replace000001", mp4_bytes=first, width=160, height=120, fps=5, frames=2
    )
    after_first = pool.total_bytes()
    expected_first = len(base64.b64encode(first))  # b64 pricing parity
    assert after_first == expected_first
    second = b"b" * 500
    assert pool.store_video(
        record_id="rec_replace000001", mp4_bytes=second, width=160, height=120, fps=5, frames=2
    )
    assert pool.total_bytes() == len(base64.b64encode(second))
    assert pool.lookup_video("rec_replace000001").video_bytes == second


# --- M5: single-flight (one encode per recording at a time) --------------------------------------


def test_concurrent_double_result_encodes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """M5: two concurrent result calls serialize on the per-recording lock and
    the second reuses the stored result — exactly ONE encode."""
    counter = _EncodeCounter(b"\x02" * 512)
    monkeypatch.setattr(recorder_module, "_import_av", lambda: object())
    monkeypatch.setattr(recorder_module, "_encode_one", counter)
    recording = _recording(count=2, fps=5)
    pool = RecordingEvidenceStore("sess_flight")
    bundle = SimpleNamespace(extra={RECORDING_STORE_KEY: pool})

    async def _double() -> list[Any]:
        return await asyncio.gather(
            server_record._finished_reply(
                "sess_flight", bundle, recording, result_token="verified", include_video=True
            ),
            server_record._finished_reply(
                "sess_flight", bundle, recording, result_token="verified", include_video=True
            ),
        )

    blocks_pair = _run(_double())
    assert counter.count == 1  # the second call reused the stored result
    for blocks in blocks_pair:
        resources = [block for block in blocks if block.type == "resource"]
        assert len(resources) == 1
        assert base64.b64decode(resources[0].resource.blob) == b"\x02" * 512


# --- unavailability / unreadable frames ----------------------------------------------------------


def test_encoder_unavailable_when_av_import_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_import() -> Any:
        raise ImportError("forced: av is unavailable in this environment")

    monkeypatch.setattr(recorder_module, "_import_av", broken_import)
    recording = _recording(count=2, fps=5)
    data, info = encode_video(recording, fps=5)
    assert data is None
    assert info == {"omitted": VIDEO_OMITTED_ENCODER_UNAVAILABLE, "bytes": 0}


def test_released_frames_give_the_frames_released_omission(monkeypatch: pytest.MonkeyPatch) -> None:
    """DELTA 2 N3: after the payloads are evicted there is nothing to encode —
    the honest omission is its own token, ``frames_released``."""
    monkeypatch.setattr(recorder_module, "_import_av", lambda: object())
    recording = _recording(count=2, fps=5)
    recording.release_frame_payloads()
    data, info = encode_video(recording, fps=5)
    assert data is None
    assert info["omitted"] == "frames_released"
    assert "evicted" in info.get("detail", "")
