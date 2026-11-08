"""WP-E (ORVEX-2026-109) field fixes — E1/E2/E3 on FAKE sessions only.

E1: the ``record`` object on ``computer_execute`` starts the background
recording JUST BEFORE the call's first input (a separate-call recording always
ended before the model's next tool call — field session sess_a59fef27).  E2:
``video_delivery`` "file" (default) writes the MP4 under the recordings folder
instead of travelling as a blob most hosts truncate; "inline" keeps the legacy
EmbeddedResource.  E3: a batch reply's attached image is the LAST executed
item's post-action frame (``image_after_item`` names it).

Everything runs through the fake backend (its recording grabber, its fake
cursor); the real desktop and the real clipboard are never touched, and the
recording files land under the fresh_server fixture's tmp_path audit dir.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, PngImagePlugin

import computer_use_mcp.server.execute as srv_server_execute
import computer_use_mcp.server.app as srv_server_app
import computer_use_mcp.server.lifecycle as srv_server_lifecycle
import computer_use_mcp.server.record as srv_server_record
import computer_use_mcp.server.record_machine as srv_record_machine
import computer_use_mcp.vision.recorder as recorder_module
from computer_use_mcp.core.models import WindowInfo
from computer_use_mcp.vision.evidence import evidence_store
from computer_use_mcp.vision.visual_views import VisualViewError
from test_controller_integration import (  # noqa: F401 - pytest fixture import
    FAST_LIMITS,
    ScriptedBackend,
    execute_payload,
    fresh_server,  # noqa: F401
    make_session,
)


@pytest.fixture(autouse=True)
def _recordings_under_tmp_path(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """DELTA 1 M1: every test here points the data-dir resolver at its own
    tmp audit dir — no recording file can ever land in the real user dir."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))


# --- harness ------------------------------------------------------------------------------------


def payload_of(result: Any) -> dict[str, Any]:
    """The JSON of a reply — either a plain dict or blocks (TextContent first)."""
    if isinstance(result, list):
        return json.loads(result[0].text)
    return result


def resource_blocks(result: Any) -> list[Any]:
    return [block for block in result if getattr(block, "type", None) == "resource"]


async def _start(session_id: str, **kwargs: Any) -> dict[str, Any]:
    reply = await srv_server_record.computer_record(session_id, **kwargs)
    assert isinstance(reply, dict), reply
    return reply


async def _collect(session_id: str, record_id: str, **kwargs: Any) -> Any:
    return await srv_server_record.computer_record_result(session_id, record_id, **kwargs)


def _png_for(marker: str) -> str:
    image = Image.new("RGB", (16, 16), (32, 64, 96))
    info = PngImagePlugin.PngInfo()
    info.add_text("marker", marker)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", pnginfo=info)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _png_marker(data_b64: str) -> str:
    image = Image.open(io.BytesIO(base64.b64decode(data_b64)))
    return str(image.info.get("marker", ""))


def _tag_observations(backend: Any, flip_identity_at: int | None = None) -> list[str]:
    """Serve marker-tagged captures (obs-1, obs-2, ...) so a test can tell
    WHICH frame a reply carries.  ``flip_identity_at`` swaps the fake foreground
    window right BEFORE capture N+1 (the digest-surprise driver)."""
    markers: list[str] = []
    original = backend.observe

    def tagged() -> Any:
        if flip_identity_at is not None and len(markers) == flip_identity_at:
            backend.set_active_window(
                WindowInfo(hwnd=2, pid=11, process_name="app.exe", title="Other")
            )
        observation = original()
        markers.append(f"obs-{len(markers) + 1}")
        observation.image_base64 = _png_for(markers[-1])
        return observation

    backend.observe = tagged
    return markers


def _with_window(backend: Any) -> None:
    """Give the fake backend a foreground window so the queue premise check is
    identity-based (pixel changes from earlier items never stop a batch)."""
    backend.set_active_window(WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="App"))


# --- E1: record around an action, in the same call ----------------------------------------------


async def test_execute_click_with_record_returns_block_and_collects_timeline(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1: a click with ``record`` — the reply carries the record block (started,
    record_id, status "recording", ends_at, duration_s, fps, region, the collect
    hint), the execute returns AT ONCE (it does not wait for the recording), and
    the collected timeline contains this call's own click and the change segments
    the fake backend produced."""
    session_id, _bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    reply = await srv_server_execute.computer_execute(
        session_id, "click", x=100, y=100, approved=True, record={"duration_s": 1.2}
    )
    body = payload_of(reply)
    block = body.get("record")
    assert isinstance(block, dict) and block["started"] is True
    record_id = str(block["record_id"])
    assert record_id.startswith("rec_")
    assert block["status"] == "recording"
    assert block["duration_s"] == 1.2
    assert isinstance(block["ends_at"], float)
    assert block["fps"] == 5
    assert "collect with computer_record_result" in block["message"]
    assert body.get("result") in ("verified", "uncertain", "marker", "failed", "dry_run")
    # The execute returned while the recording is still running (it did NOT
    # block for the recording's end).
    peek = await _collect(session_id, record_id, block=False)
    assert peek["status"] in ("recording", "encoding")
    result = await _collect(session_id, record_id)
    collected = payload_of(result)
    assert collected["result"] == "verified"
    actions = [action["action"] for action in collected["actions"]]
    assert "click" in actions
    assert collected["actions"][-1]["result"] != "dispatched"
    # The fake grabber changes pixels every frame, so the change timeline is
    # non-empty (the click's own change is inside the window).
    assert len(collected["timeline"]) >= 1


async def test_execute_follow_ups_batch_with_record(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1: a follow_ups batch with ``record`` — both clicks land inside the
    recording window and appear in the collected actions."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    reply = await srv_server_execute.computer_execute(
        session_id,
        "click",
        x=100,
        y=100,
        approved=True,
        follow_ups=[{"action": "click", "x": 150, "y": 120}],
        record={"duration_s": 1.5},
    )
    block = payload_of(reply)["record"]
    assert block["started"] is True
    record_id = str(block["record_id"])
    result = await _collect(session_id, record_id)
    collected = payload_of(result)
    assert collected["result"] == "verified"
    actions = [action["action"] for action in collected["actions"]]
    assert actions.count("click") == 2


async def test_execute_plan_step_with_record(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1: the ``record`` object is accepted on the plan submission call — the
    recording starts just before the step's input and its actions contain the
    step's move + click."""
    from plan_explicitness import complete_plan

    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    reply = await srv_server_execute.computer_execute(
        session_id,
        "done",
        approved=True,
        plan=complete_plan(
            {
                "transition_duration_ms": 0,
                "points": [{"x": 60, "y": 40, "actions": [{"action": "click"}]}],
            }
        ),
        record={"duration_s": 2.0},
    )
    block = payload_of(reply)["record"]
    assert block["started"] is True
    record_id = str(block["record_id"])
    result = await _collect(session_id, record_id)
    collected = payload_of(result)
    assert collected["result"] == "verified"
    actions = [action["action"] for action in collected["actions"]]
    assert "move" in actions and "click" in actions


async def test_execute_record_cap_reached_action_still_runs(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1: the session recording cap (1 active) reached — the ACTION still runs
    and the reply's record block says {"started": false, "reason":
    "recording_limit"}.  A recording problem never refuses the action."""
    session_id, _bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    first = await _start(session_id, duration_s=5.0)
    try:
        reply = await srv_server_execute.computer_execute(
            session_id, "click", x=100, y=100, approved=True, record={"duration_s": 1.0}
        )
        body = payload_of(reply)
        block = body.get("record")
        assert isinstance(block, dict)
        assert block["started"] is False
        assert block["reason"] == "recording_limit"
        # The action itself executed.
        assert body.get("ok") is True
        assert [action.action.value for action in backend.executed] == ["click"]
    finally:
        await _collect(session_id, str(first["record_id"]), cancel=True)


async def test_execute_invalid_record_band_rejects_before_any_input(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1: an out-of-band record argument is a typed rejection of the whole
    call BEFORE any input — zero items dispatch (the same codes computer_record
    uses, through the same validation code path)."""
    session_id, _bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    reply = await srv_server_execute.computer_execute(
        session_id, "click", x=100, y=100, approved=True, record={"duration_s": 99}
    )
    body = payload_of(reply)
    assert body["ok"] is False and body["result"] == "rejected"
    assert body["reasons"] == ["invalid_record_duration"]
    assert backend.executed == []
    # An out-of-bounds region is the same fail-closed story (typed
    # invalid_region before any input).
    reply = await srv_server_execute.computer_execute(
        session_id,
        "click",
        x=100,
        y=100,
        approved=True,
        record={"duration_s": 1.0, "region": [5000, 0, 100, 100]},
    )
    body = payload_of(reply)
    assert body["ok"] is False and body["result"] == "rejected"
    assert body["reasons"] == ["invalid_region"]
    assert backend.executed == []


async def test_execute_record_text_session_reports_started_false(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1: a text-mode session has no screen to record — the block says
    {"started": false, "reason": "text_session_no_screen"} and the action still
    runs."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, image_delivery="text", limits=FAST_LIMITS
    )
    reply = await srv_server_execute.computer_execute(
        session_id, "click", x=100, y=100, approved=True, record={"duration_s": 1.0}
    )
    body = payload_of(reply)
    block = body.get("record")
    assert isinstance(block, dict)
    assert block["started"] is False
    assert block["reason"] == "text_session_no_screen"
    assert [action.action.value for action in backend.executed] == ["click"]


async def test_stop_session_joins_an_execute_started_recording_cleanly(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1 + DELTA 1 m8: stop_session while a recording started by
    ``computer_execute`` is running — the stop returns cleanly, the recorder
    THREAD is no longer alive (a real join, not just an empty registry), and
    no registry entry outlives it."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    reply = await srv_server_execute.computer_execute(
        session_id, "click", x=100, y=100, approved=True, record={"duration_s": 3.0}
    )
    block = payload_of(reply)["record"]
    assert block["started"] is True
    record_id = str(block["record_id"])
    with srv_record_machine._lock:
        handle = srv_record_machine._RECORDINGS_IN_FLIGHT[session_id][record_id]
    stop = await srv_server_lifecycle.stop_session(session_id)
    assert stop["ok"] is True
    assert handle.thread is not None
    assert not handle.thread.is_alive()
    assert handle.done.is_set()
    assert not srv_record_machine._RECORDINGS_IN_FLIGHT.get(session_id)


# --- E2: video as a file by default ---------------------------------------------------------------


async def test_default_delivery_writes_a_decodable_file_with_no_blob(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """E2: the DEFAULT delivery is "file" — the reply's video entry carries the
    absolute path of a decodable MP4 under the recordings folder (the fresh_server
    fixture's tmp audit dir, never the real user dir) and NO blob is attached."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    start = await _start(session_id, duration_s=0.6, include_video=True)
    record_id = str(start["record_id"])
    await asyncio.sleep(0.9)
    result = await _collect(session_id, record_id)
    payload = payload_of(result)
    video = payload["video"]
    assert video["included"] is True
    assert video["delivery"] == "file"
    for key in ("bytes", "width", "height", "fps", "frames", "codec", "encoder"):
        assert key in video, key
    path = Path(str(video["path"]))
    assert path.is_absolute() and path.exists()
    assert str(path) == payload["video_path"]
    assert str(path) == str(tmp_path / "audit" / "recordings" / f"{record_id}.mp4")
    info = recorder_module._decode_video_info(path.read_bytes())
    assert info["frames"] >= 1 and info["width"] > 0 and info["height"] > 0
    assert not resource_blocks(result)


async def test_inline_delivery_returns_the_blob_as_before(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2: "inline" keeps today's EmbeddedResource behaviour, unchanged."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    start = await _start(
        session_id, duration_s=0.6, include_video=True, video_delivery="inline"
    )
    record_id = str(start["record_id"])
    await asyncio.sleep(0.9)
    result = await _collect(session_id, record_id)
    payload = payload_of(result)
    assert payload["video"]["included"] is True
    assert "delivery" not in payload["video"]
    assert len(resource_blocks(result)) == 1


async def test_write_failed_is_a_closed_omission(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """E2: a failing file writer answers the closed ``write_failed`` omission
    (added to the set) with the produced size — and no file, no blob."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)

    def broken_writer(path: Any, data: bytes) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(srv_server_record, "_write_video_file", broken_writer)
    start = await _start(session_id, duration_s=0.6, include_video=True)
    record_id = str(start["record_id"])
    await asyncio.sleep(0.9)
    result = await _collect(session_id, record_id)
    payload = payload_of(result)
    assert payload["video"]["omitted"] == "write_failed"
    assert payload["video"]["bytes"] > 0
    assert not (tmp_path / "audit" / "recordings" / f"{record_id}.mp4").exists()
    assert not resource_blocks(result)
    # NOT final: the write is retried on a later collect (writer restored).
    monkeypatch.setattr(
        srv_server_record, "_write_video_file", lambda path, data: Path(path).write_bytes(data)
    )
    retry = await _collect(session_id, record_id)
    retry_payload = payload_of(retry)
    assert retry_payload["video"]["included"] is True
    assert Path(str(retry_payload["video"]["path"])).exists()


async def test_invalid_video_delivery_is_typed_rejected(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2: a bogus ``video_delivery`` is a typed rejection on BOTH tools."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    start = await _start(session_id, duration_s=1.0, video_delivery="bogus")
    assert start["ok"] is False and start["reasons"] == ["invalid_video_delivery"]
    live = await _start(session_id, duration_s=1.0)
    result = await _collect(
        session_id, str(live["record_id"]), video_delivery="somewhere_else"
    )
    body = payload_of(result)
    assert body["ok"] is False and body["reasons"] == ["invalid_video_delivery"]
    await _collect(session_id, str(live["record_id"]), cancel=True)


# --- E3: a batch reply shows the state after the last executed item ------------------------------


async def test_batch_click_click_wait_shows_the_after_wait_frame(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E3: click + click + wait — the reply's attached image (and the frame the
    evidence store keeps) is the WAIT's own post-wait capture; the reply names
    ``image_after_item == 2`` and the trailing wait shows Normal at the
    captured cursor."""
    session_id, bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    _with_window(backend)
    markers = _tag_observations(backend)
    reply = await srv_server_execute.computer_execute(
        session_id,
        "click",
        x=100,
        y=100,
        approved=True,
        follow_ups=[{"action": "click", "x": 120, "y": 140}, {"action": "wait", "delta": 1}],
    )
    body = payload_of(reply)
    # Item 0's own verdict governs ok (a click without a stated effect verifies
    # uncertain here — the tagged frames prove pixels changed, not intent);
    # uncertain does NOT stop the batch.
    assert body.get("result") in ("verified", "uncertain"), body.get("message")
    assert body["image_after_item"] == 2
    assert body["follow_ups_stopped_reason"] is None
    # The carried frame is the LAST capture the backend served (the wait's
    # post_action observe), not item 0's.  The SERVED copy is a half-res JPEG
    # (the marker chunk cannot survive it), so the honest check is the frame
    # the reply carried — registered in the evidence store pre-ladder (below)
    # — plus the reply serving exactly one image block.
    assert isinstance(reply, list) and len(reply) == 2  # JSON + one image block
    # A trailing wait is not a click: Normal at the captured cursor.
    assert body["pointer"]["state"] == "normal"
    # The evidence store holds THAT frame (the pre-ladder bytes the reply
    # carries).
    image_id = str(body["image_id"])
    lookup = evidence_store(bundle.extra).lookup(image_id)
    assert lookup.found
    assert _png_marker(str(lookup.frame.image_base64)) == markers[-1]
    # The verdict fields keep item 0's meaning (its own click verdict — an
    # effect-less click on the tagged fake verifies uncertain; uncertain never
    # stops the batch and never changes what the image shows).
    assert body["result"] == "uncertain"
    assert body["action"]["point"]["x"] == 100


async def test_batch_stopped_at_item_1_shows_item_1_frame(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E3: a batch stopped BEFORE item 2 dispatches (its premise went stale:
    the attached window's identity changed) — the image is the last item that
    actually executed (item 1, a click): ``image_after_item == 1`` and Pressed
    at that click's point."""
    session_id, _bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    _with_window(backend)
    # Deterministic capture order (the premise-reuse window is disabled): 1
    # direct, 2 validate(item0), 3 post(item0), 4 validate(item1), 5
    # post(item1), 6 validate(item2) — flip the window identity BEFORE capture
    # 6 so item 2's premise check fails and the batch stops there (zero
    # dispatch for item 2).
    import computer_use_mcp.pipeline.agent as srv_pipeline_agent

    monkeypatch.setattr(srv_pipeline_agent, "_validate_reuse_window_ms", lambda: 0.0)
    _tag_observations(backend, flip_identity_at=5)
    reply = await srv_server_execute.computer_execute(
        session_id,
        "click",
        x=100,
        y=100,
        approved=True,
        follow_ups=[{"action": "click", "x": 120, "y": 140}, {"action": "click", "x": 200, "y": 200}],
    )
    body = payload_of(reply)
    assert body["image_after_item"] == 1
    assert body["follow_ups_stopped_reason"] == "digest_surprise"
    # Item 2 never dispatched (its entry carries the digest_surprise stop kind).
    assert body["follow_up_results"][2]["kind"] == "digest_surprise"
    assert [action.action.value for action in backend.executed] == ["click", "click"]
    # Item 1 is a click and the frame is its own post-click frame: Pressed.
    assert body["pointer"]["state"] == "pressed"
    assert (body["pointer"]["x"], body["pointer"]["y"]) == (120, 140)


async def test_single_action_reply_is_unchanged_no_image_after_item_key(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E3: the single-action path is unchanged — no ``image_after_item`` key,
    and the image is the flat action's own post-action frame (Pressed at its
    click point, the pre-E3 rule)."""
    session_id, _bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    _with_window(backend)
    _tag_observations(backend)
    reply = await srv_server_execute.computer_execute(
        session_id, "click", x=100, y=100, approved=True
    )
    body = payload_of(reply)
    assert "image_after_item" not in body
    assert body["pointer"]["state"] == "pressed"
    assert (body["pointer"]["x"], body["pointer"]["y"]) == (100, 100)


async def test_plan_step_reply_carries_image_after_item(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E3: a plan step (move + click) shows the LAST executed item's frame —
    ``image_after_item == 1`` and Pressed at the point's click (the deliberate
    E3 change of the DELTA-2-N1 normal rule; pinned in
    test_pointer_in_replies.py too)."""
    from plan_explicitness import complete_plan

    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    reply = await srv_server_execute.computer_execute(
        session_id,
        "done",
        approved=True,
        plan=complete_plan(
            {
                "transition_duration_ms": 0,
                "points": [{"x": 60, "y": 40, "actions": [{"action": "click"}]}],
            }
        ),
    )
    body = payload_of(reply)
    assert body.get("image_after_item") == 1
    assert body["pointer"]["state"] == "pressed"


# ===================================================================================
# DELTA 1 (VAL-WP-E-01 fixes) — m1/m2/m3/m4/m5/m6/m8 + the closed reason truth
# ===================================================================================


def _static_until_click_grabber(backend: Any) -> dict[str, Any]:
    """A fake recording grabber that is pixel-STATIC until the backend has
    executed at least one action, then changes every frame — the change
    timeline can therefore only be produced by the click itself (m8)."""
    import threading

    from PIL import ImageDraw

    state = {"post_click_frames": 0}
    lock = threading.Lock()

    def grab(left: int, top: int, width: int, height: int) -> Any:
        width = max(1, int(width))
        height = max(1, int(height))
        with lock:
            if not backend.executed:
                return Image.new("RGB", (width, height), (250, 250, 250))
            state["post_click_frames"] += 1
            image = Image.new("RGB", (width, height), (250, 250, 250))
            ImageDraw.Draw(image).rectangle(
                (0, 0, min(width, 8 + state["post_click_frames"] * 6) - 1, 8),
                fill=(20, 20, 20),
            )
            return image

    backend.record_frame_grab = grab
    return state


async def test_record_pre_roll_click_timing_and_timeline_start_at_the_click(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m8: the fake grabber changes pixels ONLY after the click dispatches —
    the click's action t_ms is at least the pre-roll (minus a small
    tolerance), every timeline change segment starts at or after the click's
    t_ms, and there IS at least one segment (the click's own change)."""
    session_id, _bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    _static_until_click_grabber(backend)
    reply = await srv_server_execute.computer_execute(
        session_id, "click", x=100, y=100, approved=True, record={"duration_s": 1.5}
    )
    block = payload_of(reply)["record"]
    assert block["started"] is True
    result = await _collect(session_id, str(block["record_id"]))
    collected = payload_of(result)
    actions = collected["actions"]
    assert [action["action"] for action in actions] == ["click"]
    click_t_ms = float(actions[0]["t_ms"])
    assert click_t_ms >= srv_server_record.RECORD_PRE_ROLL_SECONDS * 1000.0 - 80.0, (
        "the click dispatched before the pre-roll elapsed"
    )
    assert len(collected["timeline"]) >= 1
    for segment in collected["timeline"]:
        assert segment["start_ms"] >= click_t_ms - 120.0, (
            "a pixel change preceded the click — the static-until-click "
            "grabber makes that impossible unless the pre-roll leaked"
        )


async def test_record_object_strict_band_and_unknown_key_rejections(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m3/n4 + DELTA 2 N1/N2: booleans, non-numeric strings, lists and dicts
    for fps/max_keyframes/duration_s, wrong-typed include_video/save and a
    bogus video_delivery are TYPED rejections with ZERO input dispatched;
    values that round OUT of band after coercion are rejected too. An unknown
    key inside the record object is invalid_record_param naming the allowed
    keys."""
    session_id, _bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    cases = [
        ({"duration_s": 1.0, "fps": True}, "invalid_record_fps"),
        ({"duration_s": 1.0, "fps": "abc"}, "invalid_record_fps"),
        ({"duration_s": 1.0, "fps": [5]}, "invalid_record_fps"),
        ({"duration_s": 1.0, "fps": {}}, "invalid_record_fps"),
        ({"duration_s": 1.0, "fps": "99"}, "invalid_record_fps"),  # out of band AFTER rounding
        ({"duration_s": 1.0, "max_keyframes": False}, "invalid_record_keyframes"),
        ({"duration_s": 1.0, "max_keyframes": "nope"}, "invalid_record_keyframes"),
        ({"duration_s": 1.0, "max_keyframes": [2]}, "invalid_record_keyframes"),
        ({"duration_s": 1.0, "max_keyframes": {}}, "invalid_record_keyframes"),
        ({"duration_s": 1.0, "max_keyframes": 99}, "invalid_record_keyframes"),
        ({"duration_s": True}, "invalid_record_duration"),
        ({"duration_s": "abc"}, "invalid_record_duration"),
        ({"duration_s": 1.0, "include_video": "yes"}, "invalid_record_param"),
        ({"duration_s": 1.0, "save": 1}, "invalid_record_param"),
        ({"duration_s": 1.0, "video_delivery": "bogus"}, "invalid_video_delivery"),
    ]
    for record_overrides, code in cases:
        reply = await srv_server_execute.computer_execute(
            session_id, "click", x=100, y=100, approved=True, record=record_overrides
        )
        body = payload_of(reply)
        assert body["result"] == "rejected" and body["reasons"] == [code], (
            (record_overrides, code, body)
        )
    assert backend.executed == []  # every rejection dispatched NOTHING
    # Unknown keys are typed rejections naming the allowed keys (n4).
    reply = await srv_server_execute.computer_execute(
        session_id,
        "click",
        x=100,
        y=100,
        approved=True,
        record={"duration_s": 1.0, "bogus": 1},
    )
    body = payload_of(reply)
    assert body["result"] == "rejected" and body["reasons"] == ["invalid_record_param"]
    assert "Allowed keys" in str(body["message"]) and "bogus" in str(body["message"])
    assert backend.executed == []


async def test_execute_record_launch_failure_never_refuses_the_action(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m5: the launch helper is guarded end to end — ANY exception inside it
    becomes {"started": false, "reason": "record_start_failed"} and the
    action STILL runs."""
    session_id, _bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)

    def broken_launch(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("grabber construction exploded")

    monkeypatch.setattr(srv_server_record, "_launch_recording", broken_launch)
    reply = await srv_server_execute.computer_execute(
        session_id, "click", x=100, y=100, approved=True, record={"duration_s": 1.0}
    )
    body = payload_of(reply)
    block = body["record"]
    assert block["started"] is False
    assert block["reason"] == "record_start_failed"
    assert body["ok"] is True
    assert [action.action.value for action in backend.executed] == ["click"]


async def test_cancelled_error_after_start_cancels_the_recording(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m4: a call that dies after the recording started (CancelledError here,
    any BaseException in general) CANCELS the recording before propagating —
    the cancel flag is set and the recorder thread exits, freeing the slot."""
    session_id, bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    captured: dict[str, Any] = {}

    async def failing_run(*args: Any, **kwargs: Any) -> Any:
        with srv_record_machine._lock:
            registry = srv_record_machine._RECORDINGS_IN_FLIGHT.get(session_id, {})
            captured["handle"] = next(iter(registry.values()), None)
        raise asyncio.CancelledError()

    monkeypatch.setattr(bundle.agent, "run_single", failing_run)
    with pytest.raises(asyncio.CancelledError):
        await srv_server_execute.computer_execute(
            session_id, "click", x=100, y=100, approved=True, record={"duration_s": 5.0}
        )
    handle = captured["handle"]
    assert handle is not None
    assert handle.cancel.is_set()
    for _ in range(100):
        if not handle.thread.is_alive():
            break
        await asyncio.sleep(0.05)
    assert not handle.thread.is_alive()
    assert not srv_record_machine._RECORDINGS_IN_FLIGHT.get(session_id)


async def test_zero_input_refusal_cancels_the_recording_and_marks_the_block(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """n6: a PRE-DISPATCH refusal (a safety denial here) cancels the recording
    the call started and the block gains cancelled:"action_refused" — the
    slot never holds for a screen nothing changed."""
    from computer_use_mcp.pipeline.agent import SingleActionOutcome

    session_id, bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)

    async def refusing_run(*args: Any, **kwargs: Any) -> Any:
        return SingleActionOutcome(kind="safety_denied", message="the safety gate refused")

    monkeypatch.setattr(bundle.agent, "run_single", refusing_run)
    reply = await srv_server_execute.computer_execute(
        session_id, "click", x=100, y=100, approved=True, record={"duration_s": 5.0}
    )
    body = payload_of(reply)
    assert body["result"] == "rejected"
    block = body["record"]
    assert block["started"] is True
    assert block.get("cancelled") == "action_refused"
    for _ in range(100):
        if not srv_record_machine._RECORDINGS_IN_FLIGHT.get(session_id):
            break
        await asyncio.sleep(0.05)
    assert not srv_record_machine._RECORDINGS_IN_FLIGHT.get(session_id)


async def test_text_mode_batch_reply_carries_no_image_after_item(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m1: a text-mode batch reply serves no image — no ``image_after_item``
    key either."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, image_delivery="text", limits=FAST_LIMITS
    )
    reply = await srv_server_execute.computer_execute(
        session_id,
        "click",
        x=100,
        y=100,
        approved=True,
        follow_ups=[{"action": "wait", "delta": 1}],
    )
    body = payload_of(reply)
    assert body.get("image_delivery") == "text"
    assert "image_after_item" not in body
    assert body["follow_ups_stopped_reason"] is None


async def test_view_derive_failure_reply_carries_no_image_after_item(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m1: a view-derive failure serves no image — no ``image_after_item``
    key either; the typed view marker rides instead."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)

    class _BrokenProvider:
        id = "grid"
        passthrough = False

        def derive(self, observation: Any) -> Any:
            raise VisualViewError("forced derive failure")

    monkeypatch.setattr(srv_server_execute, "_resolve_view_param", lambda value: _BrokenProvider())
    reply = await srv_server_execute.computer_execute(
        session_id,
        "click",
        x=100,
        y=100,
        approved=True,
        follow_ups=[{"action": "wait", "delta": 1}],
        visual_view="grid",
    )
    body = payload_of(reply)
    assert str(body.get("visual_view_error", "")).startswith("view_derive_failed")
    assert "image_after_item" not in body


async def test_batch_of_two_stopped_before_item_1_reports_image_after_item_0(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m2: on a multi-item batch the key is ALWAYS present when an image is
    attached — including the value 0, here when the batch stopped before
    item 1 dispatched (its premise went stale) so item 0 is the last executed
    item."""
    session_id, _bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    _with_window(backend)
    # Deterministic capture order (reuse window disabled): 1 direct, 2
    # validate(item0), 3 post(item0), 4 validate(item1) — flip the window
    # identity BEFORE capture 4 so item 1's premise check fails.
    import computer_use_mcp.pipeline.agent as srv_pipeline_agent

    monkeypatch.setattr(srv_pipeline_agent, "_validate_reuse_window_ms", lambda: 0.0)
    _tag_observations(backend, flip_identity_at=3)
    reply = await srv_server_execute.computer_execute(
        session_id,
        "click",
        x=100,
        y=100,
        approved=True,
        follow_ups=[{"action": "click", "x": 150, "y": 120}],
    )
    body = payload_of(reply)
    assert body["image_after_item"] == 0
    assert body["follow_ups_stopped_reason"] == "digest_surprise"
    assert body["follow_up_results"][1]["kind"] == "digest_surprise"
    # The shown item IS item 0's click: Pressed at its point.
    assert body["pointer"]["state"] == "pressed"
    assert (body["pointer"]["x"], body["pointer"]["y"]) == (100, 100)


async def test_concurrent_file_delivery_collects_write_once(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """m6: two concurrent collects with file delivery — the MP4 is written
    ATOMICALLY and exactly ONCE, both replies carry the SAME path, and the
    file decodes."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    calls = {"writes": 0}
    real_write = srv_server_record._write_video_file

    def counting_write(path: Any, data: bytes) -> None:
        calls["writes"] += 1
        real_write(path, data)

    monkeypatch.setattr(srv_server_record, "_write_video_file", counting_write)
    start = await _start(session_id, duration_s=0.6, include_video=True)
    record_id = str(start["record_id"])
    await asyncio.sleep(0.9)
    results = await asyncio.gather(
        _collect(session_id, record_id),
        _collect(session_id, record_id),
    )
    payloads = [payload_of(result) for result in results]
    paths = [str(payload["video"]["path"]) for payload in payloads]
    assert paths[0] == paths[1]
    assert calls["writes"] == 1
    info = recorder_module._decode_video_info(Path(paths[0]).read_bytes())
    assert info["frames"] >= 1


async def test_save_true_with_file_delivery_writes_the_mp4_exactly_once(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """m6: save=true together with the default file delivery — ONE MP4 write
    at the shared path, reported in BOTH payload fields, plus the sheet."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    calls = {"writes": 0}
    real_write = srv_server_record._write_video_file

    def counting_write(path: Any, data: bytes) -> None:
        calls["writes"] += 1
        real_write(path, data)

    monkeypatch.setattr(srv_server_record, "_write_video_file", counting_write)
    start = await _start(session_id, duration_s=0.6, include_video=True, save=True)
    record_id = str(start["record_id"])
    await asyncio.sleep(0.9)
    result = await _collect(session_id, record_id)
    payload = payload_of(result)
    assert payload["video"]["included"] is True
    assert payload["video"]["delivery"] == "file"
    assert str(payload["video"]["path"]) == str(payload["video_path"])
    assert Path(str(payload["video_path"])).exists()
    assert calls["writes"] == 1  # one MP4 write, shared by both fields
    assert Path(str(payload["sheet_path"])).exists()


async def test_file_delivery_never_writes_outside_the_data_dir(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """M1 guard: a file-delivery collect writes ONLY under the tmp_path audit
    dir — nothing appears under the DEFAULT resolver root in the real user
    temp dir."""
    import tempfile

    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    start = await _start(session_id, duration_s=0.6, include_video=True)
    record_id = str(start["record_id"])
    await asyncio.sleep(0.9)
    result = await _collect(session_id, record_id)
    payload = payload_of(result)
    written = Path(str(payload["video"]["path"]))
    assert tmp_path in written.parents or str(tmp_path) in str(written)
    default_root = Path(tempfile.gettempdir()) / "cortex" / "logs" / "recordings"
    assert not (default_root / f"{record_id}.mp4").exists()


def test_started_false_reason_set_is_closed_and_documented() -> None:
    """m7: the closed ``started:false`` reason set is exactly the documented
    set — every reason the code can emit is listed in docs/REPLY-CONTRACT.md
    and vice versa."""
    reasons = set(srv_server_record.EXECUTE_RECORD_NOT_STARTED_REASONS)
    assert reasons == {
        "recording_limit",
        "text_session_no_screen",
        "invalid_region",
        "session_stopped",
        "record_start_failed",
        "no_action",
    }
    docs = Path(__file__).resolve().parent.parent / "docs" / "REPLY-CONTRACT.md"
    text = docs.read_text(encoding="utf-8")
    for reason in sorted(reasons):
        assert f"`{reason}`" in text, f"REPLY-CONTRACT.md never names {reason!r}"


# ===================================================================================
# DELTA 2 (VAL-WP-E-02 fixes) — N1/N2 coercion parity, N4 write-once save, n6
# ring check on both paths, N7 test gaps
# ===================================================================================


async def test_coercion_parity_tolerant_values_on_both_tools(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1/N2: ONE tolerant rule on BOTH tools — fps 2.7 rounds to 3,
    "5.0" gives 5, "1e1" gives 10; max_keyframes "4" gives 4; duration_s "4"
    gives 4.0.  The effective values are asserted on the replies."""
    # computer_record: the marker reply reports the COERCED values.
    session_a, _bundle_a, _backend_a, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    start = await _start(
        session_a, duration_s="4", fps=2.7, max_keyframes="4"
    )
    try:
        assert start["status"] == "recording"
        assert start["duration_s"] == 4.0
        assert start["fps"] == 3  # _coerce_tolerant_int rounds 2.7 exactly like the boundary
        assert start["max_keyframes"] == 4
    finally:
        await _collect(session_a, str(start["record_id"]), cancel=True)
    # computer_record again: the numeric-string fps "1e1" coerces to 10.
    session_b, _bundle_b, _backend_b, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    start_b = await _start(session_b, duration_s=0.6, fps="1e1")
    try:
        assert start_b["fps"] == 10
    finally:
        await _collect(session_b, str(start_b["record_id"]), cancel=True)
    # computer_execute: the same shapes through the record object.
    session_c, _bundle_c, _backend_c, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    reply = await srv_server_execute.computer_execute(
        session_c,
        "click",
        x=100,
        y=100,
        approved=True,
        record={"duration_s": "4", "fps": "5.0", "max_keyframes": "4"},
    )
    block = payload_of(reply)["record"]
    assert block["started"] is True
    assert block["duration_s"] == 4.0
    assert block["fps"] == 5
    await _collect(session_c, str(block["record_id"]), cancel=True)
    # computer_execute: "1e1" coerces to 10 there too.
    session_d, _bundle_d, _backend_d, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    reply = await srv_server_execute.computer_execute(
        session_d,
        "click",
        x=100,
        y=100,
        approved=True,
        record={"duration_s": 0.6, "fps": "1e1"},
    )
    block = payload_of(reply)["record"]
    assert block["started"] is True and block["fps"] == 10
    await _collect(session_d, str(block["record_id"]), cancel=True)


async def test_result_none_path_carries_the_record_block(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N7 (m4): the defensive `result is None` envelope carries the record
    block — the agent never loses the record_id."""
    from computer_use_mcp.pipeline.agent import SingleActionOutcome

    session_id, bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)

    async def no_result_run(*args: Any, **kwargs: Any) -> Any:
        return SingleActionOutcome(kind="executed", result=None)

    monkeypatch.setattr(bundle.agent, "run_single", no_result_run)
    reply = await srv_server_execute.computer_execute(
        session_id, "click", x=100, y=100, approved=True, record={"duration_s": 2.0}
    )
    body = payload_of(reply)
    assert body["result"] == "failed" and body.get("error") == "action_error"
    block = body.get("record")
    assert isinstance(block, dict) and block["started"] is True
    await _collect(session_id, str(block["record_id"]), cancel=True)


def _parked_plan() -> Any:
    """A one-point plan whose click cannot verify — the step parks the walk."""
    from plan_explicitness import complete_plan

    return complete_plan(
        {
            "transition_duration_ms": 0,
            "points": [
                {
                    "x": 60,
                    "y": 40,
                    "actions": [{"action": "click", "expected_effect": "the impossible effect"}],
                }
            ],
        }
    )


async def test_no_action_on_plan_stop_and_walk_end(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N7 (n7): a valid ``record`` on ``plan_action=\"stop\"`` and on a
    continue past the walk's end answers {"started": false, "reason":
    "no_action"} — never silently ignored."""
    # (a) stop: park the walk (the step cannot verify), then stop with record.
    session_a, _bundle_a, _backend_a, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    step = payload_of(
        await srv_server_execute.computer_execute(
            session_a, "done", approved=True, plan=_parked_plan()
        )
    )
    assert step.get("blocked") is True
    stop = payload_of(
        await srv_server_execute.computer_execute(
            session_a,
            "done",
            plan_action="stop",
            record={"duration_s": 1.0},
        )
    )
    block = stop.get("record")
    assert isinstance(block, dict)
    assert block["started"] is False and block["reason"] == "no_action"
    # (b) walk end: park, then continue with record — the walk is over, so
    # nothing dispatches and the walk-end reply carries no_action too.
    session_b, _bundle_b, _backend_b, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    step = payload_of(
        await srv_server_execute.computer_execute(
            session_b, "done", approved=True, plan=_parked_plan()
        )
    )
    assert step.get("blocked") is True
    walk_end = payload_of(
        await srv_server_execute.computer_execute(
            session_b,
            "done",
            plan_action="continue",
            record={"duration_s": 1.0},
        )
    )
    block = walk_end.get("record")
    assert isinstance(block, dict)
    assert block["started"] is False and block["reason"] == "no_action"


async def test_plan_path_cancelled_error_cancels_the_recording(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N7 (m4): the plan path's BaseException handler cancels the recording
    the step started — the cancel flag is set, the thread exits, the slot
    frees — and the error still propagates."""
    session_id, bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    captured: dict[str, Any] = {}

    async def failing_run(*args: Any, **kwargs: Any) -> Any:
        with srv_record_machine._lock:
            registry = srv_record_machine._RECORDINGS_IN_FLIGHT.get(session_id, {})
            captured["handle"] = next(iter(registry.values()), None)
        raise asyncio.CancelledError()

    monkeypatch.setattr(bundle.agent, "run_single", failing_run)
    with pytest.raises(asyncio.CancelledError):
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            approved=True,
            plan=_parked_plan(),
            record={"duration_s": 5.0},
        )
    handle = captured["handle"]
    assert handle is not None and handle.cancel.is_set()
    for _ in range(100):
        if not handle.thread.is_alive():
            break
        await asyncio.sleep(0.05)
    assert not handle.thread.is_alive()
    assert not srv_record_machine._RECORDINGS_IN_FLIGHT.get(session_id)


async def test_plan_step_refusal_cancels_the_recording_action_refused(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """n6: the plan step has the SAME zero-input detect-and-cancel — a
    refusal whose dispatch-ring window is empty cancels the recording and the
    step reply's block gains cancelled:"action_refused"."""
    from computer_use_mcp.pipeline.agent import SingleActionOutcome

    session_id, bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)

    async def refusing_run(*args: Any, **kwargs: Any) -> Any:
        return SingleActionOutcome(
            kind="rejected", reasons=["Grounding failed: forced"], message="forced refusal"
        )

    monkeypatch.setattr(bundle.agent, "run_single", refusing_run)
    reply = await srv_server_execute.computer_execute(
        session_id,
        "done",
        approved=True,
        plan=_parked_plan(),
        record={"duration_s": 5.0},
    )
    body = payload_of(reply)
    block = body.get("record")
    assert isinstance(block, dict) and block["started"] is True
    assert block.get("cancelled") == "action_refused"
    for _ in range(100):
        if not srv_record_machine._RECORDINGS_IN_FLIGHT.get(session_id):
            break
        await asyncio.sleep(0.05)
    assert not srv_record_machine._RECORDINGS_IN_FLIGHT.get(session_id)


async def test_dispatch_ring_check_true_after_a_dispatched_action(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """n6: the exact ring check — after an action actually dispatched (this
    click), ``execute_record_input_dispatched`` is True, so the cancel can
    never fire for a dispatched action."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    reply = await srv_server_execute.computer_execute(
        session_id, "click", x=100, y=100, approved=True, record={"duration_s": 2.0}
    )
    block = payload_of(reply)["record"]
    assert block["started"] is True
    assert srv_server_record.execute_record_input_dispatched(session_id, block) is True
    await _collect(session_id, str(block["record_id"]), cancel=True)


async def test_save_collected_twice_inline_then_file_writes_once(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """N4: save=true collected twice — first "inline", then the default "file"
    — writes the MP4 through the write-once path in BOTH cases: exactly ONE
    write, and both replies report the same path."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    calls = {"writes": 0}
    real_write = srv_server_record._write_video_file

    def counting_write(path: Any, data: bytes) -> None:
        calls["writes"] += 1
        real_write(path, data)

    monkeypatch.setattr(srv_server_record, "_write_video_file", counting_write)
    start = await _start(session_id, duration_s=0.6, include_video=True, save=True)
    record_id = str(start["record_id"])
    await asyncio.sleep(0.9)
    first = payload_of(await _collect(session_id, record_id, video_delivery="inline"))
    assert first["video_path"] is not None
    assert calls["writes"] == 1
    second = payload_of(await _collect(session_id, record_id))
    assert second["video"]["included"] is True
    assert str(second["video"]["path"]) == str(first["video_path"])
    assert calls["writes"] == 1  # the memo reused the path — no second write


async def test_mp4_write_failure_keeps_sheet_path(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """N4: a failing MP4 write no longer drops the sheet — ``sheet_path`` is
    still reported while the video honestly answers ``write_failed``."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)

    def broken_writer(path: Any, data: bytes) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(srv_server_record, "_write_video_file", broken_writer)
    start = await _start(session_id, duration_s=0.6, include_video=True, save=True)
    record_id = str(start["record_id"])
    await asyncio.sleep(0.9)
    result = await _collect(session_id, record_id)
    payload = payload_of(result)
    assert payload["video"]["omitted"] == "write_failed"
    assert payload.get("video_path") is None
    # DELTA 3 M-A: the honest save verdict — the MP4 was NOT saved.
    assert payload["saved"] is False
    assert payload.get("save_error")
    assert payload.get("sheet_path") is not None
    assert Path(str(payload["sheet_path"])).exists()


async def test_inline_save_with_failing_mp4_write_reports_saved_false(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """M-A: on the inline+save path a failed MP4 write reports ``saved`` is
    False WITH ``save_error`` (never the contradictory saved:true +
    save_error) while the sheet is still written."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)

    def broken_writer(path: Any, data: bytes) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(srv_server_record, "_write_video_file", broken_writer)
    start = await _start(
        session_id, duration_s=0.6, include_video=True, save=True, video_delivery="inline"
    )
    record_id = str(start["record_id"])
    await asyncio.sleep(0.9)
    result = await _collect(session_id, record_id)
    payload = payload_of(result)
    assert len(resource_blocks(result)) == 1  # the inline blob still attaches
    assert payload["saved"] is False
    assert payload.get("save_error")
    assert payload.get("sheet_path") is not None


async def test_stale_tmp_files_are_swept_on_the_first_write(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """NIT: the first MP4 write best-effort removes stale ``.tmp-*`` leftovers
    older than one hour — never any other file (a fresh tmp leftover and a
    normal MP4 survive)."""
    import os
    import time as time_module

    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    recordings = tmp_path / "audit" / "recordings"
    recordings.mkdir(parents=True, exist_ok=True)
    stale = recordings / "rec_stale.mp4.tmp-deadbeef"
    stale.write_bytes(b"stale")
    os.utime(stale, (time_module.time() - 7200.0, time_module.time() - 7200.0))
    fresh_tmp = recordings / "rec_fresh.mp4.tmp-cafebabe"
    fresh_tmp.write_bytes(b"fresh")
    keep = recordings / "keep.mp4"
    keep.write_bytes(b"keep")
    # DELTA 3 NIT: the sweep pattern is Cortex's OWN temp names
    # (``rec_*.mp4.tmp-*``) — a foreign stale ``*.tmp-*`` file survives.
    foreign = recordings / "notes.tmp-old"
    foreign.write_bytes(b"foreign")
    os.utime(foreign, (time_module.time() - 7200.0, time_module.time() - 7200.0))
    start = await _start(session_id, duration_s=0.6, include_video=True)
    record_id = str(start["record_id"])
    await asyncio.sleep(0.9)
    result = await _collect(session_id, record_id)
    payload = payload_of(result)
    assert payload["video"]["delivery"] == "file"  # the first write ran the sweep
    assert not stale.exists()
    assert fresh_tmp.exists()
    assert keep.exists()
    assert foreign.exists()


def test_started_false_reason_set_is_bidirectional_with_the_docs() -> None:
    """m7/N7: BOTH directions — every reason in the docs is in the code set,
    and every reason in the code set is in the docs."""
    import re

    reasons = set(srv_server_record.EXECUTE_RECORD_NOT_STARTED_REASONS)
    assert reasons == {
        "recording_limit",
        "text_session_no_screen",
        "invalid_region",
        "session_stopped",
        "record_start_failed",
        "no_action",
    }
    docs = Path(__file__).resolve().parent.parent / "docs" / "REPLY-CONTRACT.md"
    text = docs.read_text(encoding="utf-8")
    for reason in sorted(reasons):
        assert f"`{reason}`" in text, f"REPLY-CONTRACT.md never names {reason!r}"
    match = re.search(r"`server/record\.py`\): (.*?)\(", text, re.S)
    assert match is not None, "the REPLY-CONTRACT reason list moved; restate the truth test"
    documented = set(re.findall(r"`([a-z_]+)`", match.group(1)))
    assert documented == reasons, (
        "REPLY-CONTRACT.md names reasons the code cannot emit: "
        f"{sorted(documented - reasons)}"
    )


# ===================================================================================
# DELTA 3 (VAL-WP-E-03) — M-B dispatched-then-refused never cancels, the
# bool-duration MCP check, the memo-deleted rewrite
# ===================================================================================


async def test_dispatched_then_refused_flat_is_never_cancelled(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-B (flat): an action that DISPATCHED input (the stub stamps the
    dispatch ring, exactly like agent.py does before ``_backend_execute``)
    and then reports a rejection is NEVER cancelled — the reply's record
    block has no ``cancelled`` key and the recording is still in flight.
    This test FAILS if the ring condition is removed from execute.py's
    detect-and-cancel (confirmed by reasoning: without ``not
    execute_record_input_dispatched(...)`` the kind+partial_delivery filter
    alone matches this outcome and the cancel fires, putting ``cancelled`` on
    the block and retiring the recording)."""
    from computer_use_mcp.pipeline.agent import SingleActionOutcome

    session_id, bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)

    async def stamp_then_refuse(state: Any, action: Any, **kwargs: Any) -> Any:
        recorder_module.record_dispatched_action(session_id, action, "dispatched")
        return SingleActionOutcome(
            kind="rejected", reasons=["forced refusal"], message="forced refusal"
        )

    monkeypatch.setattr(bundle.agent, "run_single", stamp_then_refuse)
    reply = await srv_server_execute.computer_execute(
        session_id, "click", x=100, y=100, approved=True, record={"duration_s": 3.0}
    )
    body = payload_of(reply)
    block = body.get("record")
    assert isinstance(block, dict) and block["started"] is True
    assert "cancelled" not in block
    record_id = str(block["record_id"])
    assert record_id in srv_record_machine._RECORDINGS_IN_FLIGHT.get(session_id, {})
    await _collect(session_id, record_id, cancel=True)


async def test_dispatched_then_refused_plan_step_is_never_cancelled(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-B (plan): the SAME dispatched-then-refused stub on the plan step —
    the step reply's record block has no ``cancelled`` key and the recording
    is still in flight.  FAILS if the ring condition is removed from
    plan_surface.py's detect-and-cancel (same reasoning as the flat test:
    the kind+partial_delivery filter alone matches, so the cancel would fire
    and the block would carry ``cancelled``)."""
    from computer_use_mcp.pipeline.agent import SingleActionOutcome

    session_id, bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)

    async def stamp_then_refuse(state: Any, action: Any, **kwargs: Any) -> Any:
        recorder_module.record_dispatched_action(session_id, action, "dispatched")
        return SingleActionOutcome(
            kind="rejected", reasons=["forced refusal"], message="forced refusal"
        )

    monkeypatch.setattr(bundle.agent, "run_single", stamp_then_refuse)
    reply = await srv_server_execute.computer_execute(
        session_id,
        "done",
        approved=True,
        plan=_parked_plan(),
        record={"duration_s": 3.0},
    )
    body = payload_of(reply)
    block = body.get("record")
    assert isinstance(block, dict) and block["started"] is True
    assert "cancelled" not in block
    record_id = str(block["record_id"])
    assert record_id in srv_record_machine._RECORDINGS_IN_FLIGHT.get(session_id, {})
    await _collect(session_id, record_id, cancel=True)
    await srv_server_execute.computer_execute(session_id, "done", plan_action="stop")


async def test_bool_duration_s_is_not_accepted_on_the_mcp_path(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NIT: ``computer_record(duration_s=True)`` through the REAL MCP gate.
    pydantic's lax float coerces True to 1.0 (verified live), so computer_record
    itself rejects bools with the SAME typed code the record object uses
    (``invalid_record_duration``) — one rule for both tools; no recording
    starts."""
    import mcp as mcp_sdk

    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    request = mcp_sdk.types.CallToolRequest(
        params=mcp_sdk.types.CallToolRequestParams(
            name="computer_record",
            arguments={"session_id": session_id, "duration_s": True},
        )
    )
    handler = srv_server_app.mcp._mcp_server.request_handlers[mcp_sdk.types.CallToolRequest]
    server_result = await handler(request)
    result = server_result.root
    # The boundary rejects the boolean BEFORE any coercion: either a protocol
    # error carrying the message, or (defensively) the tool's own typed
    # rejection — either way NO recording starts.
    detail = ""
    started = False
    if result.isError:
        detail = result.content[0].text if result.content else ""
    else:
        body = json.loads(result.content[0].text)
        started = body.get("status") == "recording" and bool(body.get("record_id"))
        detail = str(body.get("message", ""))
    assert started is False, "a boolean duration_s started a recording on the MCP path"
    assert "duration_s" in detail, detail
    assert session_id not in srv_record_machine._RECORDINGS_IN_FLIGHT


async def test_memo_path_deleted_between_collects_is_rewritten(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """NIT: the memo path is deleted between two collects — the second
    collect REWRITES the MP4, the file exists again and decodes."""
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    calls = {"writes": 0}
    real_write = srv_server_record._write_video_file

    def counting_write(path: Any, data: bytes) -> None:
        calls["writes"] += 1
        real_write(path, data)

    monkeypatch.setattr(srv_server_record, "_write_video_file", counting_write)
    start = await _start(session_id, duration_s=0.6, include_video=True)
    record_id = str(start["record_id"])
    await asyncio.sleep(0.9)
    first = payload_of(await _collect(session_id, record_id))
    video_path = Path(str(first["video"]["path"]))
    assert video_path.exists() and calls["writes"] == 1
    video_path.unlink()  # the file vanishes under the memo
    second = payload_of(await _collect(session_id, record_id))
    assert second["video"]["included"] is True
    assert str(second["video"]["path"]) == str(video_path)
    assert calls["writes"] == 2  # the existence recheck forced the rewrite
    info = recorder_module._decode_video_info(video_path.read_bytes())
    assert info["frames"] >= 1
