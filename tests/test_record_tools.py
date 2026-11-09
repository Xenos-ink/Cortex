"""WP-R + DELTA 1 (VAL-WP-R-01) — the ``computer_record`` /
``computer_record_result`` tools on a FAKE session: never the real desktop.

Everything runs through the fake backend's recording grabber
(``record_frame_grab``) and the fake cursor (``record_cursor_position``); no
mss instance is built and no real pointer is read.  DELTA 1 coverage: B1 (one
id end to end), B2 (start flags persist), B3 (failures surface), M4 (one
to_thread worker), M5 (encode-once), m1 (stop races), m4 (dispatch-time
action stamps), m7 (UNVERIFIABLE refusal), and protocol-level unknown-argument
checks for BOTH tools through the instance gate.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import threading
import time
from collections import OrderedDict
from typing import Any

import pytest
from PIL import Image, ImageChops

import computer_use_mcp.server.app as srv_server_app
import computer_use_mcp.server.fetch as srv_server_fetch
import computer_use_mcp.server.lifecycle as srv_server_lifecycle
import computer_use_mcp.server.observe as srv_server_observe
import computer_use_mcp.server.plan_surface as srv_plan_surface
import computer_use_mcp.server.record as srv_server_record
import computer_use_mcp.server.record_machine as srv_record_machine
import computer_use_mcp.vision.recorder as recorder_module
from computer_use_mcp.vision.evidence import evidence_store, recording_store
from test_controller_integration import (  # noqa: F401 - pytest fixture import
    FAST_LIMITS,
    ScriptedBackend,
    audit_events,
    fresh_server,  # noqa: F401 - re-exported fixture
    make_session,
)


@pytest.fixture(autouse=True)
def _recordings_under_tmp_path(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """DELTA 1 M1: NO test in this file may write outside tmp_path — the
    data-dir resolver (``COMPUTER_USE_MCP_LOG_DIR``) points at the per-test
    tmp audit dir, so even a test that forgets the override (the default
    "file" delivery writes MP4s on collect) can never touch the real user
    dir."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))


# --- harness ---------------------------------------------------------------------------------


def payload_of(result: Any) -> dict[str, Any]:
    """The JSON of a reply — either a plain dict or blocks (TextContent first)."""
    if isinstance(result, list):
        return json.loads(result[0].text)
    return result


def image_blocks(result: Any) -> list[Any]:
    return [block for block in result if getattr(block, "type", None) == "image"]


def resource_blocks(result: Any) -> list[Any]:
    return [block for block in result if getattr(block, "type", None) == "resource"]


async def _start(session_id: str, **kwargs: Any) -> dict[str, Any]:
    reply = await srv_server_record.computer_record(session_id, **kwargs)
    assert isinstance(reply, dict), reply
    return reply


async def _collect(session_id: str, record_id: str, **kwargs: Any) -> Any:
    return await srv_server_record.computer_record_result(session_id, record_id, **kwargs)


async def _run_short(session_id: str, **kwargs: Any) -> tuple[dict[str, Any], Any]:
    """A full short recording: start, wait past the duration, collect."""
    start = await _start(session_id, duration_s=0.6, **kwargs)
    assert start["status"] == "recording"
    await asyncio.sleep(0.9)
    result = await _collect(session_id, str(start["record_id"]))
    return start, result


def _png_for(marker: str) -> str:
    from PIL import Image, PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("marker", marker)
    image = Image.new("RGB", (8, 8), (32, 64, 96))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", pnginfo=info)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


# --- B1: ONE record_id end to end ---------------------------------------------------------------


async def test_one_record_id_end_to_end(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """B1: the id the START reply gave is the id in the payload, the resource
    URI, the saved filenames, the pool keys (keyframes AND video) and the id
    the re-fetch uses."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    session_id, bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    start = await _start(session_id, duration_s=0.6, include_video=True, save=True)
    record_id = str(start["record_id"])
    assert record_id.startswith("rec_")
    await asyncio.sleep(0.9)
    # WP-E E2 CHANGED PIN: the DEFAULT delivery is now "file" (no blob); this
    # test pins the one-id-end-to-end contract including the resource URI, so
    # it requests the "inline" delivery explicitly.
    result = await _collect(session_id, record_id, video_delivery="inline")
    payload = payload_of(result)
    # The payload id is the id the agent was given (B1 — it used to differ).
    assert payload["record_id"] == record_id
    # The resource URI carries the SAME id.
    resources = resource_blocks(result)
    assert len(resources) == 1
    assert str(resources[0].resource.uri) == f"cortex://recording/{record_id}.mp4"
    # The saved filenames carry the SAME id.
    assert str(tmp_path / "audit" / "recordings" / f"{record_id}.mp4") == payload["video_path"]
    assert str(
        tmp_path / "audit" / "recordings" / f"{record_id}_sheet.png"
    ) == payload["sheet_path"]
    assert (tmp_path / "audit" / "recordings" / f"{record_id}.mp4").exists()
    # The pool keys (keyframes AND video) carry the SAME id, and the re-fetch
    # by the payload's keyframe id works.
    pool = recording_store(bundle.extra)
    assert pool is not None
    retained = pool.lookup_video(record_id)
    assert retained is not None
    entries = pool._entries
    assert entries and all(entry.record_id == record_id for entry in entries.values()), (
        f"pool entries carry foreign record ids: "
        f"{sorted({e.record_id for e in entries.values()})}"
    )
    for keyframe in payload["keyframes"]:
        fetched = await srv_server_fetch.computer_fetch_image(session_id, keyframe["image_id"])
        assert payload_of(fetched)["ok"] is True


# --- start returns at once + bands ----------------------------------------------------------------


async def test_start_returns_at_once_with_marker_contract(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    began = time.monotonic()
    reply = await _start(session_id, duration_s=2.0)
    returned_within = time.monotonic() - began
    assert reply["ok"] is True
    assert reply["result"] == "marker"
    assert reply["status"] == "recording"
    assert reply["record_id"].startswith("rec_") and len(reply["record_id"]) == 20
    assert reply["ends_at"] > time.time()
    assert reply["fps"] == 5
    assert reply["region"] is None
    assert reply["remaining_seconds"] > 0
    assert returned_within < 1.5
    await _collect(session_id, str(reply["record_id"]), cancel=True)


async def test_start_rejects_out_of_band_parameters(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    for kwargs, token in (
        ({"duration_s": 0.1}, "invalid_record_duration"),
        ({"duration_s": 11.0}, "invalid_record_duration"),
        ({"duration_s": 1.0, "fps": 0}, "invalid_record_fps"),
        ({"duration_s": 1.0, "fps": 11}, "invalid_record_fps"),
        ({"duration_s": 1.0, "max_keyframes": 1}, "invalid_record_keyframes"),
        ({"duration_s": 1.0, "max_keyframes": 13}, "invalid_record_keyframes"),
        ({"duration_s": 1.0, "include_video": "yes"}, "invalid_record_param"),
    ):
        reply = await _start(session_id, **kwargs)
        assert reply["ok"] is False and reply["result"] == "rejected", (kwargs, reply)
        assert reply["reasons"] == [token], (kwargs, reply)


async def test_invalid_region_is_typed_and_never_clipped(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    for region in ([10, 10], "left,top", [0, 0, 0, 10], [0, 0, 10, 0], [0, 0, 2000, 10], [1200, 700, 200, 200], [0, 0, 10.5, 10]):
        reply = await _start(session_id, duration_s=1.0, region=region)  # type: ignore[arg-type]
        assert reply.get("error") == "invalid_region", (region, reply)


async def test_initial_observe_runs_on_the_loop_thread(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 2 N2 (Commander decision): the capture NEVER runs on an executor
    thread — the one backend.observe() that computer_record needs (when the
    session was never observed) runs ON THE LOOP, exactly like every other
    tool.  Only the PIL/base64/save reply work is off-loop."""
    session_id, _bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    observe_threads: list[threading.Thread] = []
    real_observe = backend.observe

    def spying_observe(*args: Any, **kwargs: Any) -> Any:
        observe_threads.append(threading.current_thread())
        return real_observe(*args, **kwargs)

    monkeypatch.setattr(backend, "observe", spying_observe)
    reply = await _start(session_id, duration_s=0.6)
    assert reply["ok"] is True
    assert observe_threads, "the recording never established its capture context"
    # The observe ran on the LOOP thread (the thread running this coroutine) —
    # never on a to_thread executor worker.
    assert observe_threads[0] is threading.current_thread()
    await asyncio.sleep(0.9)
    result = await _collect(session_id, str(reply["record_id"]))
    assert payload_of(result)["result"] == "verified"


async def test_realistic_frame_sizes_keyframes_and_previous_recording_survive(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 3 BLOCKER at REALISTIC sizes (~150 KB+ noise JPEGs, 50 frames,
    the default 16 MB pool): (a) every keyframe image_id in the reply is
    fetchable right after the result; (b) the previous recording's keyframes
    AND video are still retained after a second recording — a second
    recording's held frames evict the FIRST recording's held frames (freeing
    its JPEGs), never its keyframes or video."""
    session_id, bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    # One pre-encoded noise JPEG ≈ 150-300 KB — the recorder re-encodes at
    # q70, so every held frame lands in that band.
    noise = Image.effect_noise((1280, 720), 48).convert("RGB")
    buffer = io.BytesIO()
    noise.save(buffer, format="JPEG", quality=70)
    noise_jpeg = buffer.getvalue()
    assert len(noise_jpeg) > 100_000  # realistically large frames

    def big_noise_grab(left: int, top: int, width: int, height: int) -> Image.Image:
        return Image.open(io.BytesIO(noise_jpeg))

    monkeypatch.setattr(backend, "record_frame_grab", big_noise_grab)
    pool = recording_store(bundle.extra)
    assert pool is not None

    # Recording 1: 50 frames at 10 fps, WITH video.
    start1 = await _start(session_id, duration_s=5.0, fps=10, include_video=True)
    id1 = str(start1["record_id"])
    await asyncio.sleep(5.6)
    result1 = await _collect(session_id, id1)
    payload1 = payload_of(result1)
    assert payload1["result"] == "verified"
    keyframes1 = payload1["keyframes"]
    assert keyframes1, "realistic noise frames must produce keyframes"
    # (a) EVERY keyframe id handed out is fetchable right after the result.
    for keyframe in keyframes1:
        fetched = await srv_server_fetch.computer_fetch_image(session_id, keyframe["image_id"])
        assert payload_of(fetched)["ok"] is True

    # Recording 2: its frames entry must evict recording 1's HELD FRAMES
    # (freeing their JPEGs) — never recording 1's keyframes or video.
    start2 = await _start(session_id, duration_s=0.6, fps=10, include_video=True)
    id2 = str(start2["record_id"])
    await asyncio.sleep(1.2)
    result2 = await _collect(session_id, id2, include_video=True)
    payload2 = payload_of(result2)
    assert payload2["result"] == "verified"
    # (b) recording 1's keyframes and video are STILL retained.
    for keyframe in keyframes1:
        fetched = await srv_server_fetch.computer_fetch_image(session_id, keyframe["image_id"])
        assert payload_of(fetched)["ok"] is True
    retained = pool.lookup_video(id1)
    assert retained is not None
    # (c) the pool byte total equals the real held bytes (sum of entries).
    assert pool.total_bytes() == sum(
        entry.byte_size for entry in pool._entries.values()
    )


async def test_realistic_frames_over_the_cap_are_released_at_once(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 3 BLOCKER (e): held frames too big for the pool (100 realistic
    frames ≈ >16 MB with video) are REFUSED and released AT ONCE — the
    reply's keyframes and video still work, and a later include_video is
    impossible (frames_released) because the frames are already gone."""
    session_id, bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    noise = Image.effect_noise((1280, 720), 48).convert("RGB")
    buffer = io.BytesIO()
    noise.save(buffer, format="JPEG", quality=70)
    noise_jpeg = buffer.getvalue()

    def big_noise_grab(left: int, top: int, width: int, height: int) -> Image.Image:
        return Image.open(io.BytesIO(noise_jpeg))

    monkeypatch.setattr(backend, "record_frame_grab", big_noise_grab)
    start = await _start(session_id, duration_s=10.0, fps=10, include_video=True)
    record_id = str(start["record_id"])
    await asyncio.sleep(10.8)
    result = await _collect(session_id, record_id, include_video=True)
    payload = payload_of(result)
    assert payload["result"] == "verified"
    # 100 × ~200 KB frames + video + keyframes exceed the 16 MB pool: the
    # frames were refused and released at once (never holding the pool
    # hostage), while video and keyframes survived.
    assert payload["video"]["included"] is True
    for keyframe in payload["keyframes"]:
        fetched = await srv_server_fetch.computer_fetch_image(session_id, keyframe["image_id"])
        assert payload_of(fetched)["ok"] is True


async def test_aborted_encode_is_not_a_final_omission(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 3 n3: an encode ABORTED by cancel/stop is NOT a final omission —
    a later result call (in the live session) retries while the frames are
    held, and the video becomes available."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    real_worker_encode = srv_record_machine._encode_video
    real_reply_encode = srv_server_record.encode_video

    def aborting_encode(*args: Any, **kwargs: Any) -> Any:
        # Simulate a stop-aborted encode: the frames are STILL held.
        return None, {
            "omitted": "encode_failed",
            "bytes": 0,
            "detail": "aborted",
            "aborted": True,
        }

    # Patch BOTH the worker's encode and the fallback reply encode: the first
    # collect is fully aborted (no video stored anywhere).
    monkeypatch.setattr(srv_record_machine, "_encode_video", aborting_encode)
    monkeypatch.setattr(srv_server_record, "encode_video", aborting_encode)
    start = await _start(session_id, duration_s=0.6, include_video=True)
    record_id = str(start["record_id"])
    await asyncio.sleep(0.9)
    first = await _collect(session_id, record_id)
    first_payload = payload_of(first)
    assert first_payload["video"]["omitted"] == "encode_failed"
    assert first_payload["video"].get("aborted") is True
    assert not resource_blocks(first)
    # Clear: the aborted encode was NOT final — the retry (frames still
    # held) produces the video.
    monkeypatch.setattr(srv_record_machine, "_encode_video", real_worker_encode)
    monkeypatch.setattr(srv_server_record, "encode_video", real_reply_encode)
    # WP-E E2 CHANGED PIN: the DEFAULT delivery is now "file" (no blob); this
    # test pins the abort-then-retry contract, so it requests "inline".
    second = await _collect(
        session_id, record_id, include_video=True, video_delivery="inline"
    )
    second_payload = payload_of(second)
    assert second_payload["video"]["included"] is True
    assert len(resource_blocks(second)) == 1


async def test_unverifiable_space_is_refused_in_both_modes(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m7: monitor mode refuses an UNVERIFIABLE space exactly like the region
    path.  A 1280x720 fake screenshot on a 1920x1080 monitor (DPI 1.0) makes
    the measured scale unverifiable."""
    from computer_use_mcp.core.models import MonitorInfo
    from test_controller_integration import ScriptedBackend as _SB

    backend = _SB()
    backend.set_monitors(
        [MonitorInfo(id="m0", index=0, bounds=(0, 0, 1920, 1080), is_primary=True)]
    )
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, limits=FAST_LIMITS
    )
    for region in (None, [0, 0, 100, 100]):
        reply = await _start(session_id, duration_s=1.0, region=region)
        assert reply.get("error") == "invalid_region", (region, reply)
        assert "unverifiable" in reply["message"].lower()


async def test_a_valid_region_maps_through_the_transform(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    reply = await _start(session_id, duration_s=0.6, region=[0, 0, 640, 360])
    assert reply["ok"] is True
    assert reply["region"] == [0, 0, 640, 360]
    result = await _collect(session_id, str(reply["record_id"]))
    payload = payload_of(result)
    assert payload["region"] == [0, 0, 640, 360]
    assert payload["frames"] >= 2


# --- other tools work while recording ------------------------------------------------------------


async def test_agent_can_call_other_tools_while_recording(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    start = await _start(session_id, duration_s=2.0)
    record_id = str(start["record_id"])
    observation = srv_server_observe.computer_observe(session_id)
    assert isinstance(observation, list) and observation[0].type == "text"
    peek = await _collect(session_id, record_id, block=False)
    assert peek["status"] == "recording" and peek["result"] == "marker"
    await _collect(session_id, record_id, cancel=True)


# --- B2: start flags persist ---------------------------------------------------------------------


async def test_start_flags_survive_until_a_late_collection(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """B2: start with include_video+save, spend 3 tool calls while it runs and
    AFTER it ends, then collect — the video and the files are there (the old
    bug dropped both flags with the handle)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    start = await _start(session_id, duration_s=0.6, include_video=True, save=True)
    record_id = str(start["record_id"])
    # Three other tool calls after the recording already ended.
    await asyncio.sleep(0.9)
    srv_server_observe.computer_observe(session_id)
    srv_server_observe.computer_observe(session_id)
    srv_server_observe.computer_observe(session_id)
    # WP-E E2 CHANGED PIN: the DEFAULT delivery is now "file" (no blob); this
    # test pins the start-flags-persist contract, so it requests "inline".
    result = await _collect(session_id, record_id, video_delivery="inline")
    payload = payload_of(result)
    assert payload["result"] == "verified"
    assert payload["video"]["included"] is True
    assert len(resource_blocks(result)) == 1
    assert payload["saved"] is True
    assert (tmp_path / "audit" / "recordings" / f"{record_id}.mp4").exists()
    assert (tmp_path / "audit" / "recordings" / f"{record_id}_sheet.png").exists()


async def test_video_requested_only_on_the_result_call_after_end(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 2 N3 (a): include_video on the RESULT call alone — collected
    after the recording ended — still produces the MP4 (the frames are kept
    under the pool's byte cap; the m8 regression is gone)."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    start = await _start(session_id, duration_s=0.6)  # NO video at start
    record_id = str(start["record_id"])
    await asyncio.sleep(0.9)  # the recording ended
    # WP-E E2 CHANGED PIN: the DEFAULT delivery is now "file" (no blob); this
    # test pins the result-call-alone video contract, so it requests "inline".
    result = await _collect(
        session_id, record_id, include_video=True, video_delivery="inline"
    )
    payload = payload_of(result)
    assert payload["result"] == "verified"
    assert payload["video"]["included"] is True
    assert len(resource_blocks(result)) == 1


async def test_video_requested_only_on_the_result_call_while_running(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 2 N3 (b): include_video on the RESULT call alone — the blocking
    collect that runs PAST the recording's end — produces the MP4."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    start = await _start(session_id, duration_s=0.6)  # NO video at start
    record_id = str(start["record_id"])
    # The blocking collect starts while the recording runs and blocks past it.
    # WP-E E2 CHANGED PIN: the DEFAULT delivery is now "file" (no blob); this
    # test pins the blocking-collect video contract, so it requests "inline".
    result = await _collect(
        session_id, record_id, include_video=True, video_delivery="inline"
    )
    payload = payload_of(result)
    assert payload["video"]["included"] is True
    assert len(resource_blocks(result)) == 1


async def test_collect_while_recording_is_encoding_reports_encoding_status(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 2 n1: a caller that peeks while the worker is still ENCODING
    gets status ``"encoding"`` — never "recording, 0.0 s remaining"."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    real_encode = srv_record_machine._encode_video

    def slow_encode(
        recording: Any, *, fps: int, composite: bool = True, should_abort: Any = None
    ) -> Any:
        time.sleep(1.2)  # the encode is still running when the peek lands
        return real_encode(recording, fps=fps, should_abort=should_abort)

    monkeypatch.setattr(srv_record_machine, "_encode_video", slow_encode)
    start = await _start(session_id, duration_s=0.5, include_video=True)
    record_id = str(start["record_id"])
    await asyncio.sleep(0.7)  # capture done; the slow encode is running
    peek = await _collect(session_id, record_id, block=False)
    assert peek["status"] == "encoding", peek
    result = await asyncio.wait_for(_collect(session_id, record_id), timeout=15.0)
    assert payload_of(result)["video"]["included"] is True


async def test_stop_session_during_the_encode_aborts_promptly(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 2 N4: the stop lands DURING the encode.  The fake encoder blocks
    INDEFINITELY until the encode's stop check fires (an encode far longer
    than the 5 s join budget); stop_session must return promptly, the worker
    thread must have exited, and no orphan remains."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    real_encode = srv_record_machine._encode_video
    encode_started = threading.Event()

    def unbounded_encode(
        recording: Any, *, fps: int, composite: bool = True, should_abort: Any = None
    ) -> Any:
        encode_started.set()
        # Would run far longer than the 5 s join budget — ONLY the stop check
        # (consulted between rungs/frames by the real ladder) ends it.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if should_abort is not None and should_abort():
                return None, {"omitted": "encode_failed", "bytes": 0, "detail": "aborted"}
            time.sleep(0.05)
        return None, {"omitted": "encode_failed", "bytes": 0}

    monkeypatch.setattr(srv_record_machine, "_encode_video", unbounded_encode)
    start = await _start(session_id, duration_s=0.5, include_video=True)
    record_id = str(start["record_id"])
    assert encode_started.wait(timeout=5.0), "the worker never reached the encode"
    began = time.monotonic()
    stop = await srv_server_lifecycle.stop_session(session_id)
    stop_seconds = time.monotonic() - began
    assert stop["ok"] is True
    assert stop_seconds < 5.0, f"stop_session took {stop_seconds:.2f} s (join blocked?)"
    # The worker aborts within ~0.05 s of the stop; give the exit a moment.
    orphan_deadline = time.monotonic() + 2.0
    alive = [t for t in threading.enumerate() if t.name.startswith("cortex-record-")]
    while alive and time.monotonic() < orphan_deadline:
        time.sleep(0.05)
        alive = [t for t in threading.enumerate() if t.name.startswith("cortex-record-")]
    assert alive == []  # the worker aborted and exited — no orphan


async def test_backend_close_is_never_called_by_a_recording(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 2 n5: GrabberHandle NEVER closes the backend — a backend with
    ``record_frame_grab`` AND ``close()`` keeps its close untouched after a
    recording (the recorder only closes grabbers it created itself)."""
    session_id, bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    close_calls = {"count": 0}

    def counting_close() -> None:
        close_calls["count"] += 1

    monkeypatch.setattr(backend, "close", counting_close, raising=False)
    _start_reply, result = await _run_short(session_id)
    assert payload_of(result)["result"] == "verified"
    assert close_calls["count"] == 0


async def test_mss_grabber_is_closed_on_stop_session(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 2 M6: a recording on the REAL grabber path (no backend
    ``record_frame_grab`` → the worker's own MssFrameGrabber over a FAKE mss
    factory) closes the mss instance when stop_session lands."""
    session_id, _bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)

    class _FakeRaw:
        size = (4, 4)
        raw = bytes(4 * 4 * 4)

    class _FakeMss:
        def __init__(self) -> None:
            self.closed = False

        def grab(self, region: Any) -> Any:
            return _FakeRaw()

        def close(self) -> None:
            self.closed = True

    fake = _FakeMss()
    monkeypatch.setattr(backend, "_mss_factory", lambda: fake, raising=False)
    # Force the REAL grabber path: an instance-level None makes
    # `_resolve_grabber` fall through to the recorder's own MssFrameGrabber.
    monkeypatch.setattr(backend, "record_frame_grab", None)
    start = await _start(session_id, duration_s=5.0)
    stop = await srv_server_lifecycle.stop_session(session_id)
    assert stop["ok"] is True
    assert fake.closed is True  # the recorder's OWN grabber is closed


async def test_collect_while_still_running_then_collect(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2: a result call that runs while the recording is still in flight
    (block=false peek) does not lose the flags — the later collection has the
    video."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    start = await _start(session_id, duration_s=2.0, include_video=True, save=True)
    record_id = str(start["record_id"])
    peek = await _collect(session_id, record_id, block=False)
    assert peek["status"] == "recording"
    await asyncio.sleep(2.3)
    # WP-E E2 CHANGED PIN: the DEFAULT delivery is now "file" (no blob); this
    # test pins the peek-then-collect contract, so it requests "inline".
    result = await _collect(session_id, record_id, video_delivery="inline")
    payload = payload_of(result)
    assert payload["video"]["included"] is True
    assert len(resource_blocks(result)) == 1


async def test_result_explicit_include_video_false_wins_but_save_writes(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """DELTA 1 M7: an explicit include_video=false on the result call wins —
    NO blob is attached — while save (start) still writes the file it can
    already produce.  The collection happens AFTER the recording finished."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    start = await _start(session_id, duration_s=0.6, include_video=True, save=True)
    record_id = str(start["record_id"])
    await asyncio.sleep(0.9)
    result = await _collect(session_id, record_id, include_video=False)
    payload = payload_of(result)
    assert payload["video"] == {"omitted": "not_requested", "bytes": 0}
    assert not resource_blocks(result)
    # DELTA 2 M7: save=true still WRITES the MP4 file even though this reply
    # attaches no blob (the video was produced at start; the file exists).
    assert payload.get("saved") is True
    assert (tmp_path / "audit" / "recordings" / f"{record_id}.mp4").exists()
    assert (tmp_path / "audit" / "recordings" / f"{record_id}.mp4").stat().st_size > 0


# --- B3: failures surface -------------------------------------------------------------------------


def _failing_grabber(raise_on_frame: int):
    """A grab callable that raises on ``raise_on_frame`` (1-based)."""

    def grab(left: int, top: int, width: int, height: int) -> Image.Image:
        grab.count += 1
        if grab.count == raise_on_frame:
            raise OSError("simulated capture failure (secure desktop)")
        image = Image.new("RGB", (max(1, width // 4), max(1, height // 4)), (240, 240, 240))
        return image

    grab.count = 0
    return grab


async def test_capture_failure_on_the_first_frame_files_failed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3(a): mss fails on the FIRST frame — result ``failed`` with reasons
    ``["capture_failed"]`` (never a silent empty verified)."""
    session_id, bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    monkeypatch.setattr(backend, "record_frame_grab", _failing_grabber(raise_on_frame=1))
    start = await _start(session_id, duration_s=2.0)
    record_id = str(start["record_id"])
    await asyncio.sleep(0.3)
    result = await _collect(session_id, record_id)
    payload = payload_of(result)
    assert payload["ok"] is False
    assert payload["result"] == "failed"
    assert payload["reasons"] == ["capture_failed"]
    assert "simulated capture failure" in payload["message"]
    assert payload["frames"] == 0
    # Exactly one terminal audit event, result "failed" (never "ok").
    events = [event for event in audit_events(bundle, session_id) if event["event_type"].startswith("record_")]
    terminal = [event for event in events if event["event_type"] in ("record_complete", "record_cancelled")]
    assert len(terminal) == 1
    assert terminal[0]["result"] == "failed"


async def test_capture_failure_mid_recording_is_failed_with_partial_count(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3 (documented decision): a grab that raises AFTER some frames is ALSO
    ``failed`` — a partially captured timeline is untrustworthy — and the
    message counts the frames captured before the failure."""
    session_id, bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)

    def slow_then_raise(left: int, top: int, width: int, height: int) -> Image.Image:
        slow_then_raise.count += 1
        if slow_then_raise.count >= 3:
            raise OSError("simulated mid-recording capture failure")
        time.sleep(0.15)
        return Image.new("RGB", (320, 240), (240 - slow_then_raise.count * 8, 240, 240))

    slow_then_raise.count = 0
    monkeypatch.setattr(backend, "record_frame_grab", slow_then_raise)
    start = await _start(session_id, duration_s=3.0)
    record_id = str(start["record_id"])
    await asyncio.sleep(0.8)
    result = await _collect(session_id, record_id)
    payload = payload_of(result)
    assert payload["result"] == "failed"
    assert payload["reasons"] == ["capture_failed"]
    assert payload["frames"] >= 1  # partial frames are counted, not hidden


async def test_worker_exception_in_store_keyframe_files_failed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3(b): a worker exception inside the finalize path files a terminal
    ``record_worker_failed`` outcome; a caller already blocked WAKES and sees
    it (never a marker claiming the recording is still running)."""
    session_id, bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    pool = recording_store(bundle.extra)
    assert pool is not None

    def exploding_store_keyframe(**kwargs: Any) -> str:
        raise RuntimeError("simulated pool explosion")

    monkeypatch.setattr(pool, "store_keyframe", exploding_store_keyframe)
    start = await _start(session_id, duration_s=2.0)
    record_id = str(start["record_id"])
    # The blocked caller: collect BEFORE the worker finishes.
    collect_task = asyncio.ensure_future(_collect(session_id, record_id))
    await asyncio.sleep(0.3)
    assert not collect_task.done() or payload_of(collect_task.result()).get("result") in ("failed",)
    result = await asyncio.wait_for(collect_task, timeout=5.0)
    payload = payload_of(result)
    assert payload["result"] == "failed"
    assert payload["reasons"] == ["record_worker_failed"]
    assert "simulated pool explosion" in payload["message"]
    events = [event for event in audit_events(bundle, session_id) if event["event_type"] in ("record_complete", "record_cancelled")]
    assert len(events) == 1
    assert events[0]["result"] == "failed"


async def test_worker_crash_in_the_recorder_run_is_failed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3(b): an exception INSIDE ScreenRecorder.run (not a grab failure — a
    genuine worker fault) is a typed ``record_worker_failed`` outcome too."""
    session_id, bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)

    def exploding_run(self: Any) -> Any:
        raise RuntimeError("simulated recorder crash")

    monkeypatch.setattr(recorder_module.ScreenRecorder, "run", exploding_run)
    start = await _start(session_id, duration_s=1.0)
    record_id = str(start["record_id"])
    await asyncio.sleep(0.3)
    result = await _collect(session_id, record_id)
    payload = payload_of(result)
    assert payload["result"] == "failed"
    assert payload["reasons"] == ["record_worker_failed"]
    assert "simulated recorder crash" in payload["message"]


# --- M1/M2 at tool level: fetch places the sprite exactly ----------------------------------------


async def test_fetch_places_the_recording_sprite_exactly(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2: a recording keyframe fetched by id composites the cursor at the
    stored FRAME-space point — the served image differs from the clean stored
    JPEG only around the pointer, and the reported pointer is the frame-space
    point (640x360 frames on a 1280x720 fake: canonical (640,360) → frame
    (320,180) at scale 0.5)."""
    session_id, bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)

    def half_size_grab(left: int, top: int, width: int, height: int) -> Image.Image:
        return Image.new("RGB", (max(1, width // 2), max(1, height // 2)), (240, 240, 240))

    monkeypatch.setattr(backend, "record_frame_grab", half_size_grab)
    backend.set_cursor((640, 360))  # canonical == physical here (scale 1)
    start = await _start(session_id, duration_s=0.6)
    await asyncio.sleep(0.9)
    result = await _collect(session_id, str(start["record_id"]))
    payload = payload_of(result)
    keyframe = payload["keyframes"][0]
    pool = recording_store(bundle.extra)
    entry = pool._entries[keyframe["image_id"]]
    clean = Image.open(io.BytesIO(base64.b64decode(entry.image_base64)))
    fetched = await srv_server_fetch.computer_fetch_image(session_id, keyframe["image_id"])
    meta = payload_of(fetched)
    assert meta["ok"] is True and meta["source"] == "computer_record"
    # The stored pointer IS frame space: (640-0)*0.5, (360-0)*0.5.
    # DELTA 2 N1: the pointer block reports CANONICAL coordinates now:
    # frame (320,180) with scale 0.5 and origin (0,0) → canonical (640,360).
    assert meta["pointer"]["x"] == pytest.approx(640, abs=1)
    assert meta["pointer"]["y"] == pytest.approx(360, abs=1)
    served = Image.open(io.BytesIO(base64.b64decode(image_blocks(fetched)[0].data)))
    diff = ImageChops.difference(served, clean.convert("RGB")).getbbox()
    assert diff is not None  # the sprite is drawn
    assert diff[0] >= 280 and diff[1] >= 140  # ONLY around the pointer
    # DELTA 2 N1: the fetch publishes the frame→canonical mapping and reports
    # the pointer in CANONICAL coordinates (canonical = origin + frame px ×
    # scale): frame (320,180), scale 2.0 canonical-px-per-frame-px, origin
    # (0,0) → canonical (640,360).
    mapping = meta.get("frame_to_canonical")
    assert mapping is not None
    assert mapping["scale"] == pytest.approx(2.0)  # canonical px per frame px
    assert mapping["origin"] == [0, 0]
    canonical_x = mapping["origin"][0] + 320 * mapping["scale"]
    canonical_y = mapping["origin"][1] + 180 * mapping["scale"]
    assert (canonical_x, canonical_y) == (640, 360)
    assert (meta["pointer"]["x"], meta["pointer"]["y"]) == (640, 360)


# --- M4: one heavy to_thread worker ----------------------------------------------------------------


async def test_heavy_reply_work_runs_in_one_worker_and_the_loop_stays_free(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4: while the finished-reply worker is busy (the sheet ladder, the blob
    base64, the save), ANOTHER tool call on the loop completes — the fetch
    DELTA-1 pattern extended to the record family."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    start = await _start(session_id, duration_s=0.6, include_video=True, save=True)
    record_id = str(start["record_id"])
    await asyncio.sleep(0.9)
    gate = threading.Event()
    real_worker = srv_server_record._finished_reply_worker

    def blocking_worker(*args: Any, **kwargs: Any) -> Any:
        gate.wait(timeout=5.0)
        return real_worker(*args, **kwargs)

    monkeypatch.setattr(srv_server_record, "_finished_reply_worker", blocking_worker)
    collect_task = asyncio.ensure_future(_collect(session_id, record_id))
    await asyncio.sleep(0.1)
    assert not collect_task.done()
    # The loop is NOT blocked: another tool call completes while the worker runs.
    observation = srv_server_observe.computer_observe(session_id)
    assert observation[0].type == "text"
    gate.set()
    result = await asyncio.wait_for(collect_task, timeout=5.0)
    assert payload_of(result)["result"] == "verified"


# --- M5: one encode per recording at tool level ----------------------------------------------------


async def test_double_collection_encodes_once_in_the_worker(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M5: the worker encodes once right after capture; two concurrent result
    calls reuse the stored result — the encode counter never exceeds 1."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    counter = {"encodes": 0}
    real_encode = srv_record_machine._encode_video

    def counting_encode(
        recording: Any, *, fps: int, composite: bool = True, should_abort: Any = None
    ) -> Any:
        counter["encodes"] += 1
        return real_encode(recording, fps=fps)

    monkeypatch.setattr(srv_record_machine, "_encode_video", counting_encode)
    start = await _start(session_id, duration_s=0.6, include_video=True)
    record_id = str(start["record_id"])
    await asyncio.sleep(0.9)
    assert counter["encodes"] == 1  # encoded in the worker, once
    # WP-E E2 CHANGED PIN: the DEFAULT delivery is now "file" (no blob); this
    # test pins the encode-once contract, so it requests "inline".
    results = await asyncio.gather(
        _collect(session_id, record_id, video_delivery="inline"),
        _collect(session_id, record_id, video_delivery="inline"),
    )
    for result in results:
        payload = payload_of(result)
        assert payload["video"]["included"] is True
        assert len(resource_blocks(result)) == 1
    assert counter["encodes"] == 1


# --- m1: stop races --------------------------------------------------------------------------------


async def test_stop_session_during_the_worker_encode_gives_session_stopped(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m1: stop_session while the WORKER encode runs — the blocked caller gets
    ``session_stopped`` (never a verified reply for a dead session), and no
    recording thread outlives the join (the encode lives inside the worker)."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    real_encode = srv_record_machine._encode_video

    def slow_encode(
        recording: Any, *, fps: int, composite: bool = True, should_abort: Any = None
    ) -> Any:
        time.sleep(0.6)  # the encode runs while the stop lands
        return real_encode(recording, fps=fps)

    monkeypatch.setattr(srv_record_machine, "_encode_video", slow_encode)
    start = await _start(session_id, duration_s=0.5, include_video=True)
    record_id = str(start["record_id"])
    collect_task = asyncio.ensure_future(_collect(session_id, record_id))
    await asyncio.sleep(0.15)  # the worker is inside its encode
    stop = await srv_server_lifecycle.stop_session(session_id)
    assert stop["ok"] is True
    result = await asyncio.wait_for(collect_task, timeout=10.0)
    assert isinstance(result, dict)
    assert result.get("error") == "session_stopped", result
    alive = [t for t in threading.enumerate() if t.name.startswith("cortex-record-")]
    assert alive == []


async def test_stop_session_joins_the_recording_cleanly(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    start = await _start(session_id, duration_s=5.0)
    record_id = str(start["record_id"])
    stop = await srv_server_lifecycle.stop_session(session_id)
    assert stop["ok"] is True
    alive = [t for t in threading.enumerate() if t.name.startswith("cortex-record-")]
    assert alive == []
    assert not srv_record_machine._RECORDINGS_IN_FLIGHT.get(session_id)
    reply = await _collect(session_id, record_id)
    assert reply["ok"] is False and reply["error"] == "session_stopped"


async def test_idle_reaper_never_reaps_a_recording_session(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    start = await _start(session_id, duration_s=5.0)
    await asyncio.sleep(0.2)
    context = bundle.context
    with context._activity_lock:
        context.last_activity_monotonic = time.monotonic() - 10**6
    reaped = srv_server_lifecycle._sweep_idle_sessions()
    assert session_id not in reaped
    assert session_id in srv_server_lifecycle._bundles
    await _collect(session_id, str(start["record_id"]), cancel=True)


# --- m4: the action ring is stamped at DISPATCH ------------------------------------------------------


async def test_recording_actions_carry_the_dispatch_stamp(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m4: an action executed during a recording lands in the recording's
    actions with result ``dispatched`` — stamped at the dispatch choke point
    through a REAL computer_execute (flat path)."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    start = await _start(session_id, duration_s=2.0)
    record_id = str(start["record_id"])
    from computer_use_mcp.server.execute import computer_execute

    payload = await computer_execute(session_id, "click", x=100, y=100, approved=True)
    body = payload if isinstance(payload, dict) else json.loads(payload[0].text)
    assert body.get("result") in ("verified", "uncertain", "marker", "failed", "dry_run")
    await asyncio.sleep(2.3)
    result = await _collect(session_id, record_id)
    actions = payload_of(result)["actions"]
    assert [action["action"] for action in actions] == ["click"]
    # DELTA 2 n6: the dispatch-time stamp is UPDATED with the final §1 token
    # once the action settles — no longer stuck at "dispatched".
    assert actions[0]["result"] != "dispatched"
    assert actions[0]["result"] in ("verified", "uncertain", "failed", "dry_run")


# --- caps / ids / registry --------------------------------------------------------------------------


async def test_session_cap_names_the_active_record_id(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    first = await _start(session_id, duration_s=5.0)
    second = await _start(session_id, duration_s=1.0)
    assert second["ok"] is False and second["reasons"] == ["recording_limit"]
    assert str(first["record_id"]) in second["message"]
    await _collect(session_id, str(first["record_id"]), cancel=True)


async def test_process_cap_across_sessions(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_a, _bundle_a, _backend_a, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    session_b, _bundle_b, _backend_b, _ = make_session(
        monkeypatch, backend=ScriptedBackend(), limits=FAST_LIMITS
    )
    session_c, _bundle_c, _backend_c, _ = make_session(
        monkeypatch, backend=ScriptedBackend(), limits=FAST_LIMITS
    )
    first = await _start(session_a, duration_s=5.0)
    second = await _start(session_b, duration_s=5.0)
    assert second["ok"] is True  # two concurrent recordings are allowed
    try:
        third = await _start(session_c, duration_s=1.0)
        assert third["ok"] is False and third["reasons"] == ["recording_limit"]
        assert "across all sessions" in third["message"]
    finally:
        await _collect(session_a, str(first["record_id"]), cancel=True)
        await _collect(session_b, str(second["record_id"]), cancel=True)


async def test_unknown_record_id_is_typed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    reply = await _collect(session_id, "rec_0000000000000000")
    assert reply["ok"] is False and reply["result"] == "rejected"
    assert reply["reasons"] == ["unknown_record"]


async def test_expired_record_id_says_it_expired(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    srv_server_record._EXPIRED_RECORDS.setdefault(session_id, OrderedDict())[
        "rec_deadbeefdeadbeef"
    ] = "retention_cap"
    reply = await _collect(session_id, "rec_deadbeefdeadbeef")
    assert reply["reasons"] == ["record_expired"]


async def test_finished_ring_is_bounded_and_evicts_to_expired(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    ring = srv_server_record._FINISHED_RECORDINGS.setdefault(session_id, OrderedDict())
    for index in range(5):
        ring[f"rec_old{index}00000000"] = object()
        srv_record_machine._tombstone_finished(
            session_id,
            f"rec_new{index}00000000",
            recorder_module.Recording(
                record_id=f"rec_new{index}00000000",
                session_id=session_id,
                duration_s=1.0,
                fps=5,
                requested_monotonic_start=0.0,
            ),
        )
    assert len(ring) <= srv_server_record.MAX_FINISHED_RECORDINGS_PER_SESSION
    assert srv_server_record._EXPIRED_RECORDS.get(session_id)


async def test_unknown_argument_is_rejected_fail_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    with pytest.raises(TypeError):
        await srv_server_record.computer_record(
            session_id, duration_s=1.0, __bogus_wp_r_probe__=1  # type: ignore[call-arg]
        )


async def test_protocol_level_unknown_argument_for_both_tools(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NIT fix: the unknown-argument rejection is checked at the PROTOCOL
    level through the instance gate for BOTH record tools (the wp13 pattern)."""
    import mcp as mcp_sdk

    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    for name, arguments in (
        ("computer_record", {"session_id": session_id, "duration_s": 1.0, "__bogus_wp_r_probe__": 1}),
        ("computer_record_result", {"session_id": session_id, "record_id": "rec_x", "__bogus_wp_r_probe__": 1}),
    ):
        request = mcp_sdk.types.CallToolRequest(
            params=mcp_sdk.types.CallToolRequestParams(name=name, arguments=arguments)
        )
        handler = srv_server_app.mcp._mcp_server.request_handlers[mcp_sdk.types.CallToolRequest]
        server_result = await handler(request)
        result = server_result.root
        assert result.isError is False, (name, result)
        body = json.loads(result.content[0].text)
        assert "unknown_argument" in body.get("reasons", []), (name, body)


async def test_text_session_rejection(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, limits=FAST_LIMITS, image_delivery="text"
    )
    reply = await _start(session_id, duration_s=1.0)
    assert reply["ok"] is False and reply["result"] == "rejected"
    assert reply["reasons"] == ["text_session_no_screen"]


async def test_plan_lease_allows_both_recording_tools(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    monkeypatch.setattr(srv_plan_surface._plan_guard, "active_lease", lambda bundle: object())
    start = await _start(session_id, duration_s=0.6)
    assert start["ok"] is True
    assert "plan_lease" not in json.dumps(start)
    await asyncio.sleep(0.9)
    result = await _collect(session_id, str(start["record_id"]))
    body = payload_of(result)
    assert body["result"] == "verified"
    assert "plan_lease" not in json.dumps(body)


# --- audit + evidence -------------------------------------------------------------------------------


async def test_audit_events_emitted_once_each(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    _start_reply, result = await _run_short(session_id)
    assert payload_of(result)["result"] == "verified"
    events = audit_events(bundle, session_id)
    types = [event["event_type"] for event in events]
    assert types.count("record_start") == 1
    assert types.count("record_complete") == 1
    assert types.count("record_cancelled") == 0
    session_id2, bundle2, _backend2, _ = make_session(
        monkeypatch, backend=ScriptedBackend(), limits=FAST_LIMITS
    )
    start = await _start(session_id2, duration_s=5.0)
    await _collect(session_id2, str(start["record_id"]), cancel=True)
    await asyncio.sleep(0.2)
    events2 = audit_events(bundle2, session_id2)
    types2 = [event["event_type"] for event in events2]
    assert types2.count("record_cancelled") == 1
    assert types2.count("record_complete") == 0


async def test_fetch_serves_a_recording_keyframe_by_image_id(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    _start_reply, result = await _run_short(session_id)
    keyframe = payload_of(result)["keyframes"][0]
    fetched = await srv_server_fetch.computer_fetch_image(session_id, keyframe["image_id"])
    meta = payload_of(fetched)
    assert meta["ok"] is True and meta["result"] == "verified"
    assert meta["source"] == "computer_record"
    assert meta["coordinate_space"] == "recording_frame"
    assert len(image_blocks(fetched)) == 1


async def test_recordings_never_evict_observation_frames(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    store = evidence_store(bundle.extra)
    stored = store.store(
        _png_for("observation-frame"),
        source="computer_observe",
        attached=True,
        width=8,
        height=8,
        digest="d" * 64,
    )
    assert stored["image_stored"] is True
    _start_reply, result = await _run_short(session_id)
    assert payload_of(result)["result"] == "verified"
    assert store.lookup(str(stored["image_id"])).found


async def test_frames_accounted_and_eviction_answers_frames_released(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 2 N3: the held JPEGs are KEPT after the worker finishes
    (video on the RESULT call alone must work) but ACCOUNTED under the
    pool's byte cap.  When the pool evicts them, a later include_video
    answers ``frames_released``."""
    session_id, bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    start = await _start(session_id, duration_s=0.6)
    record_id = str(start["record_id"])
    await asyncio.sleep(0.9)
    pool = recording_store(bundle.extra)
    assert pool is not None
    assert pool.has_frames(record_id) is True  # accounted, NOT released
    settled = srv_server_record._FINISHED_RECORDINGS[session_id][record_id]
    assert settled.frames_released is False
    assert settled.frame_count >= 2
    # Simulate the pool evicting the frames entry (the byte cap): force the
    # eviction through the REAL byte cap, then collect with video.
    entry_id = f"{record_id}:frames"
    original_max = pool.max_bytes
    try:
        # 24 = the exact base64 length of the 16-byte probe: small enough to
        # pass the size check, big enough that EVERYTHING is evicted to fit.
        pool.max_bytes = 24
        pool.store_video(
            record_id=f"{record_id}:probe",
            mp4_bytes=b"x" * 16,
            width=4,
            height=4,
            fps=5,
            frames=1,
        )
    finally:
        pool.max_bytes = original_max
    assert pool.has_frames(record_id) is False  # evicted
    result = await _collect(session_id, record_id, include_video=True)
    payload = payload_of(result)
    assert payload["video"] == {
        "omitted": "frames_released",
        "bytes": 0,
        "detail": "the held frames were evicted under the pool's byte cap "
        "before any video was stored",
    }
    assert not resource_blocks(result)
    assert settled.frames_released is True  # released on the eviction check


# --- VAL-WP-D-01 M2: show_pointer=false turns the cursor off in recordings ------------------------


def _scripted_frame_grab(clean_jpegs: list[bytes]) -> Any:
    """A DETERMINISTIC recording grabber: every produced frame is remembered
    (its q90 JPEG bytes), so the tests can decode the MP4 and compare the
    hotspot neighbourhood against the CLEAN frame — noise frames would make
    that comparison meaningless."""

    def grab(left: int, top: int, width: int, height: int) -> Image.Image:
        image = Image.new("RGB", (320, 240), (200, 200, 200))
        bar_x = 20 + (len(clean_jpegs) * 17) % 200
        image.paste((10, 10, 10), (bar_x, 20, bar_x + 40, 60))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90)
        clean_jpegs.append(buffer.getvalue())
        return Image.open(io.BytesIO(buffer.getvalue()))

    return grab


def _mean_luma_diff(a: Image.Image, b: Image.Image, box: tuple[int, int, int, int]) -> float:
    """Mean per-pixel luma difference of two same-size images inside ``box``."""
    diff = ImageChops.difference(a.crop(box), b.crop(box)).convert("L")
    histogram = diff.histogram()
    total = sum(histogram)
    return sum(level * count for level, count in enumerate(histogram)) / total


def _decode_last_mp4_frame(mp4_bytes: bytes) -> Image.Image:
    """Decode the LAST frame of a held MP4 with the real av (imported lazily —
    an ImportError here must fail the test, never pass it vacuously)."""
    import av  # type: ignore[import-not-found]  # lazy: test-only cost

    container = av.open(io.BytesIO(mp4_bytes))
    frames = list(container.decode(container.streams.video[0]))
    container.close()
    assert frames, "the MP4 decoded to ZERO frames"
    return frames[-1].to_image().convert("RGB")


async def test_show_pointer_off_session_records_no_sprite_anywhere(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VAL-WP-D-01 M2 (+DELTA 2 MP4 leg): with the session's show_pointer
    default OFF, a recording composites NO cursor anywhere — not in the
    contact sheet, not in the MP4 (zero sprite-composite calls), the stored
    keyframes carry NO pointer metadata, and the DECODED MP4's hotspot
    neighbourhood is UNCHANGED against the clean frame.  The MP4 decode runs
    with the real av: if the encode produces nothing the test FAILS (the
    ``video.included`` assert), never passes vacuously.  A show_pointer-on
    control recording proves the spy intercepts the composite path."""
    import computer_use_mcp.vision.pointer_sprite as sprite_module
    import computer_use_mcp.vision.recorder as recorder_module

    session_id, bundle, backend, _ = make_session(
        monkeypatch, limits=FAST_LIMITS, show_pointer=False
    )
    monkeypatch.setattr(backend, "record_cursor_position", lambda: (600, 400))
    clean_jpegs: list[bytes] = []
    monkeypatch.setattr(backend, "record_frame_grab", _scripted_frame_grab(clean_jpegs))

    composite_calls: list[tuple[float, float, str]] = []
    real_composite = sprite_module.composite

    def spying_composite(image: Any, x: float, y: float, state: str, scale: float) -> Any:
        composite_calls.append((x, y, state))
        return real_composite(image, x, y, state, scale)

    monkeypatch.setattr(sprite_module, "composite", spying_composite)

    start = await _start(session_id, duration_s=0.8, fps=5, include_video=True)
    record_id = str(start["record_id"])
    await asyncio.sleep(1.2)
    result = await _collect(session_id, record_id, include_video=True)
    payload = payload_of(result)
    assert payload["result"] == "verified"
    # The encode produced a real MP4 on this box — the checks below are never
    # vacuous.
    assert payload["video"]["included"] is True, payload["video"]
    # NO sprite anywhere: the sheet build AND the video encode made zero
    # composite calls.
    assert composite_calls == [], composite_calls
    # The stored keyframes carry NO pointer metadata — nothing for fetch to
    # composite even if the default were misread.
    pool = recording_store(bundle.extra)
    assert pool is not None
    keyframe_entries = [
        entry
        for entry in pool._entries.values()
        if entry.kind == "keyframe" and getattr(entry, "pointer_state", "?") != "?"
    ]
    assert keyframe_entries, "the recording stored keyframe entries"
    assert all(entry.pointer_state is None for entry in keyframe_entries)
    assert payload["keyframes"], "keyframes were produced"
    for keyframe in payload["keyframes"]:
        fetched = await srv_server_fetch.computer_fetch_image(session_id, keyframe["image_id"])
        fetched_payload = payload_of(fetched)
        assert fetched_payload["ok"] is True
        assert "pointer" not in fetched_payload and "last_click" not in fetched_payload
    # DELTA 2 MP4 leg: decode the video with the real av and prove the hotspot
    # neighbourhood is UNCHANGED against the clean frame (codec noise only).
    retained = pool.lookup_video(record_id)
    assert retained is not None and retained.video_bytes
    entry = keyframe_entries[0]
    scale, origin = pool._frame_meta[entry.entry_id]
    hotspot_x = (600 - origin[0]) * scale
    hotspot_y = (400 - origin[1]) * scale
    decoded = _decode_last_mp4_frame(retained.video_bytes)
    clean = Image.open(io.BytesIO(clean_jpegs[-1])).convert("RGB")
    assert decoded.size == clean.size, (decoded.size, clean.size)
    box = (
        max(0, int(hotspot_x) - 14),
        max(0, int(hotspot_y) - 14),
        min(decoded.width, int(hotspot_x) + 14),
        min(decoded.height, int(hotspot_y) + 14),
    )
    far = (decoded.width - 32, decoded.height - 24, decoded.width - 2, decoded.height - 2)
    hotspot_diff = _mean_luma_diff(decoded, clean, box)
    far_diff = _mean_luma_diff(decoded, clean, far)
    assert hotspot_diff <= 4.0, (
        f"a sprite moved the hotspot neighbourhood of the OFF recording's MP4 "
        f"(mean luma diff {hotspot_diff:.2f}; far {far_diff:.2f})"
    )
    assert far_diff <= 4.0, f"unexpected codec drift far from the pointer: {far_diff:.2f}"


async def test_show_pointer_on_session_still_composites_the_cursor(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VAL-WP-D-01 M2 positive control (+DELTA 2 MP4 leg): the SAME recording
    on a default (ON) session composites the cursor into the sheet AND the
    video — at least one composite call comes from the encode path, and the
    DECODED MP4's hotspot neighbourhood is CHANGED against the clean frame
    (the sprite is really in the video, proven with the real av decode)."""
    import computer_use_mcp.vision.pointer_sprite as sprite_module
    import computer_use_mcp.vision.recorder as recorder_module

    session_id, bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    monkeypatch.setattr(backend, "record_cursor_position", lambda: (600, 400))
    clean_jpegs: list[bytes] = []
    monkeypatch.setattr(backend, "record_frame_grab", _scripted_frame_grab(clean_jpegs))

    composite_calls: list[tuple[float, float, str]] = []
    real_composite = sprite_module.composite

    def spying_composite(image: Any, x: float, y: float, state: str, scale: float) -> Any:
        composite_calls.append((x, y, state))
        return real_composite(image, x, y, state, scale)

    monkeypatch.setattr(sprite_module, "composite", spying_composite)

    # Count ENCODE-path composite chances separately from the sheet's: each
    # encode rung drives exactly one _iter_encode_frames call.
    encode_rungs: list[int] = []
    real_iter = recorder_module._iter_encode_frames

    def counting_iter(*args: Any, **kwargs: Any) -> Any:
        encode_rungs.append(1)
        return real_iter(*args, **kwargs)

    monkeypatch.setattr(recorder_module, "_iter_encode_frames", counting_iter)

    start = await _start(session_id, duration_s=0.8, fps=5, include_video=True)
    record_id = str(start["record_id"])
    await asyncio.sleep(1.2)
    result = await _collect(session_id, record_id, include_video=True)
    payload = payload_of(result)
    assert payload["result"] == "verified"
    assert payload["video"]["included"] is True, payload["video"]
    assert composite_calls, "the ON session never composites — the spy is broken"
    assert encode_rungs, "the encode path never ran — the MP4 leg is unproven"
    pool = recording_store(bundle.extra)
    assert pool is not None
    keyframe_entries = [
        entry
        for entry in pool._entries.values()
        if entry.kind == "keyframe" and getattr(entry, "pointer_state", "?") != "?"
    ]
    pressed_entry = next((e for e in keyframe_entries if e.pointer_state is not None), None)
    assert pressed_entry is not None, "the ON session stored no pointer metadata"
    # DELTA 2 MP4 leg: the sprite is REALLY in the decoded video — the hotspot
    # neighbourhood of the decoded last frame differs strongly from the clean
    # frame, while far regions stay at codec-noise level.
    retained = pool.lookup_video(record_id)
    assert retained is not None and retained.video_bytes
    scale, origin = pool._frame_meta[pressed_entry.entry_id]
    hotspot_x = (600 - origin[0]) * scale
    hotspot_y = (400 - origin[1]) * scale
    decoded = _decode_last_mp4_frame(retained.video_bytes)
    clean = Image.open(io.BytesIO(clean_jpegs[-1])).convert("RGB")
    assert decoded.size == clean.size, (decoded.size, clean.size)
    box = (
        max(0, int(hotspot_x) - 14),
        max(0, int(hotspot_y) - 14),
        min(decoded.width, int(hotspot_x) + 14),
        min(decoded.height, int(hotspot_y) + 14),
    )
    far = (decoded.width - 32, decoded.height - 24, decoded.width - 2, decoded.height - 2)
    hotspot_diff = _mean_luma_diff(decoded, clean, box)
    far_diff = _mean_luma_diff(decoded, clean, far)
    assert hotspot_diff > 8.0, (
        f"no sprite found in the ON recording's decoded MP4 at the hotspot "
        f"(mean luma diff {hotspot_diff:.2f}; far {far_diff:.2f})"
    )
    assert hotspot_diff > far_diff * 3, (
        f"the hotspot change is not sprite-shaped (hot {hotspot_diff:.2f} vs "
        f"far {far_diff:.2f})"
    )
