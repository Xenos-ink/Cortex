"""WP-C3 (ORVEX-2026-109; DELTA 1): the pointer sprite in the OUTBOUND replies only.

Pinned here: observe/zoom/execute/plan/fetch composite the Cortex cursor onto
the copy the agent receives (Pressed + ring for the frame's own click action);
the additive ``pointer`` / ``last_click`` fields ride image-bearing replies
with the REAL in_image verdict; show_pointer off (per call or per session)
restores the pre-WP-C bytes exactly; the internal captures, digests, the
``_frame`` stash and the evidence store's held frames stay CLEAN regardless of
the setting; fetch describes the FRAME (stored canonical last_click snapshot,
all PIL work off the loop); and the advertised schemas/parameters stay plain
(tool count unchanged at 10).
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import io
import json
import statistics
import threading
import time
from typing import Any

import pytest
from PIL import Image, ImageChops
from test_controller_integration import FAST_LIMITS, ScriptedBackend, make_session

import computer_use_mcp.server.app as srv_server_app
import computer_use_mcp.server.execute as srv_server_execute
import computer_use_mcp.server.fetch as srv_server_fetch
import computer_use_mcp.server.lifecycle as srv_server_lifecycle
import computer_use_mcp.server.observe as srv_server_observe
import computer_use_mcp.server.replies as srv_server_replies
from computer_use_mcp.core.state import SessionRegistry
from computer_use_mcp.desktop.pointer_ring import PointerRing
from computer_use_mcp.server.pointer_reply import (
    call_show_pointer,
    resolve_show_pointer,
)
from computer_use_mcp.vision.evidence import evidence_store


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh bounded registry/bundles + per-test audit dir (suite-standard)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(srv_server_lifecycle, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(srv_server_lifecycle, "_bundles", {})
    return None


def _metadata(blocks: Any) -> dict[str, Any]:
    return json.loads(blocks[0].text)


def _white_png_b64(width: int = 640, height: int = 480) -> str:
    """A real-size blank capture: ScriptedBackend's default PNG is 64x48, which
    would put the pointer outside the SERVED image (an honest in_image=false);
    the reply tests need a capture whose pixels match the observation dims."""
    override = Image.new("RGB", (width, height), "white")
    buffer = io.BytesIO()
    override.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


async def _payload(response: Any) -> dict[str, Any]:
    if isinstance(response, list):
        return json.loads(response[0].text)
    return response


def _png_bytes(data_b64: str) -> bytes:
    return base64.b64decode(data_b64, validate=True)


def _image(data_b64: str) -> Image.Image:
    image = Image.open(io.BytesIO(_png_bytes(data_b64)))
    image.load()
    return image


def _diff_bbox(before_b64: str, after_b64: str, *, threshold: int = 0) -> tuple[int, int, int, int] | None:
    before = _image(before_b64).convert("RGB")
    after = _image(after_b64).convert("RGB")
    if before.size != after.size:
        return (-1, -1, -1, -1)  # sentinel: dimensions changed
    difference = ImageChops.difference(before, after).convert("L")
    return difference.point(lambda value: 255 if value > threshold else 0).getbbox()


def _image_blocks(blocks: Any) -> list[Any]:
    return [block for block in blocks if getattr(block, "type", "") == "image"]


async def _pointer_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cursor: tuple[int, int] | None = (100, 100),
    **start_kwargs: Any,
) -> tuple[str, Any, ScriptedBackend]:
    backend = ScriptedBackend(width=640, height=480, cursor=cursor, flip=False)
    backend.observe_override_png = _white_png_b64(640, 480)
    session_id, _bundle, backend, _provider = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
        **start_kwargs,
    )
    bundle = srv_server_lifecycle._get_bundle(session_id)
    return session_id, bundle, backend


# --- observe --------------------------------------------------------------------------------------------


async def test_observe_draws_the_pointer_and_reports_fields(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, bundle, backend = await _pointer_session(monkeypatch)
    blocks = srv_server_observe.computer_observe(session_id)
    meta = _metadata(blocks)
    pointer = meta["pointer"]
    assert pointer["x"] == 100 and pointer["y"] == 100
    assert pointer["state"] == "normal" and pointer["in_image"] is True
    assert meta["last_click"] is None  # nothing pressed yet
    clean = backend.observed[-1].image_base64
    assert meta["image_stored"] is True
    store = evidence_store(bundle.extra)
    assert store is not None
    stored = store.frames()[-1]
    assert stored.image_base64 == clean, "the STORE keeps the clean capture"
    assert stored.pointer_x == 100 and stored.pointer_y == 100
    assert stored.pointer_state == "normal"
    bbox = _diff_bbox(clean, _image_blocks(blocks)[0].data)
    assert bbox and bbox != (-1, -1, -1, -1)
    assert abs(bbox[0] - 100) <= 1 and abs(bbox[1] - 100) <= 1, "the tip sits on the cursor"


async def test_observe_show_pointer_false_call_is_byte_identical_old_path(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, backend = await _pointer_session(monkeypatch)
    blocks = srv_server_observe.computer_observe(session_id, show_pointer=False)
    meta = _metadata(blocks)
    assert "pointer" not in meta and "last_click" not in meta
    assert _image_blocks(blocks)[0].data == backend.observed[-1].image_base64


async def test_session_show_pointer_false_restores_the_old_reply(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, backend = await _pointer_session(
        monkeypatch, show_pointer=False
    )
    blocks = srv_server_observe.computer_observe(session_id)
    meta = _metadata(blocks)
    assert "pointer" not in meta and "last_click" not in meta
    assert _image_blocks(blocks)[0].data == backend.observed[-1].image_base64


async def test_observe_without_a_known_pointer_stays_clean(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cursor=None (nothing known): no sprite, no additive keys, old bytes."""
    session_id, _bundle, backend = await _pointer_session(monkeypatch, cursor=None)  # type: ignore[arg-type]
    blocks = srv_server_observe.computer_observe(session_id)
    meta = _metadata(blocks)
    assert "pointer" not in meta
    assert _image_blocks(blocks)[0].data == backend.observed[-1].image_base64


# --- zoom ------------------------------------------------------------------------------------------------


async def test_zoom_maps_the_pointer_into_crop_space(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, bundle, _backend = await _pointer_session(monkeypatch)
    blocks = srv_server_observe.computer_zoom(session_id, [40, 40, 80, 80])
    meta = _metadata(blocks)
    pointer = meta["pointer"]
    assert pointer["x"] == 100 and pointer["y"] == 100  # CANONICAL, not crop-local
    assert pointer["in_image"] is True
    # Clean reference: the SAME zoom on a show_pointer=False session (identical
    # fake captures) — the sprite is the ONLY difference, tip at crop-local
    # (100-40, 100-40) = (60, 60).
    off_id, _b2, _b3 = await _pointer_session(monkeypatch, show_pointer=False)
    clean_blocks = srv_server_observe.computer_zoom(off_id, [40, 40, 80, 80])
    store = evidence_store(bundle.extra)
    assert store is not None
    stored = store.frames()[-1]
    assert stored.crop_region == (40, 40, 80, 80)
    bbox = _diff_bbox(_image_blocks(clean_blocks)[0].data, _image_blocks(blocks)[0].data)
    assert bbox and bbox != (-1, -1, -1, -1)
    assert abs(bbox[0] - 60) <= 1 and abs(bbox[1] - 60) <= 1  # crop-local tip = 100-40


async def test_zoom_outside_crop_reports_in_image_false_and_stays_clean(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend = await _pointer_session(monkeypatch)
    blocks = srv_server_observe.computer_zoom(session_id, [200, 200, 60, 60])
    meta = _metadata(blocks)
    assert meta["pointer"]["in_image"] is False
    assert meta["pointer"]["x"] == 100 and meta["pointer"]["y"] == 100
    # The clean comparison: the SAME zoom on a show_pointer=False session is
    # byte-identical (identical fake captures, nothing drawn either way).
    off_id, _b2, _b3 = await _pointer_session(monkeypatch, show_pointer=False)
    clean_blocks = srv_server_observe.computer_zoom(off_id, [200, 200, 60, 60])
    assert _image_blocks(blocks)[0].data == _image_blocks(clean_blocks)[0].data


# --- execute ---------------------------------------------------------------------------------------------


async def test_execute_click_shows_pressed_with_ring_and_last_click(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, bundle, backend = await _pointer_session(monkeypatch)
    response = await srv_server_execute.computer_execute(
        session_id, "click", x=60, y=40, approved=True,
        # Full-res opt-in: the reply image is then the same-resolution PNG the
        # diff below compares against (the DEFAULT reply is a half-res JPEG).
        include_screenshot_after=True,
    )
    payload = await _payload(response)
    assert payload["result"] in {"verified", "uncertain"}, payload.get("result")
    pointer = payload["pointer"]
    assert pointer["state"] == "pressed", "the call's LAST pointer action was a click"
    assert pointer["x"] == 60 and pointer["y"] == 40
    assert pointer["in_image"] is True
    last_click = payload["last_click"]
    assert last_click is not None
    assert last_click["x"] == 60 and last_click["y"] == 40
    assert last_click["button"] == "left" and last_click["age_ms"] is not None
    # backend ring parity (physical coords = canonical here: 1:1 monitor)
    assert backend.last_click()["x"] == 60
    store = evidence_store(bundle.extra)
    assert store is not None
    stored = store.frames()[-1]
    assert stored.pointer_state == "pressed"
    assert stored.image_base64 == backend.observed[-1].image_base64, (
        "the store keeps the clean post-action capture"
    )
    bbox = _diff_bbox(stored.image_base64, _image_blocks(response)[0].data)
    assert bbox and bbox != (-1, -1, -1, -1), "the reply image carries the sprite"
    # Pressed = sprite + ring: the changed area's left/top edge is the RING's
    # outer stroke (~radius 11 from the click point), not the tip itself.
    assert 60 - 12 <= bbox[0] <= 60 - 8, f"ring left edge at x={bbox[0]}"
    assert 40 - 12 <= bbox[1] <= 40 - 8, f"ring top edge at y={bbox[1]}"


async def test_execute_off_image_pointer_reports_in_image_false(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 2 NIT (M2 completion): an execute whose capture-time pointer lies
    OUTSIDE the served image draws nothing and reports ``pointer.in_image``
    FALSE — never the placeholder true."""
    # The coordinate gate refuses points outside the OBSERVATION, so the
    # off-image case is reached the real way: the served capture is SMALLER
    # than the observation (the plain ScriptedBackend PNG is 64x48 while the
    # observation claims 640x480). A click at (70, 45) is inside the
    # observation but OUTSIDE the served image — after it, the cursor is
    # off-image. Nothing may be drawn: the reply is byte-identical to the old
    # (pointer-free) half-res ladder output.
    backend = ScriptedBackend(width=640, height=480, cursor=(100, 100), flip=False)
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    bundle = srv_server_lifecycle._get_bundle(session_id)
    response = await srv_server_execute.computer_execute(
        session_id, "click", x=70, y=45, approved=True
    )
    payload = await _payload(response)
    assert payload["result"] in {"verified", "uncertain"}, payload.get("result")
    pointer = payload["pointer"]
    assert pointer["x"] == 70 and pointer["y"] == 45
    assert pointer["state"] == "pressed"
    assert pointer["in_image"] is False, "the hotspot is outside the served image"
    # The stored frame records the off-image pointer; nothing was drawn — the
    # reply is byte-identical to the old (pointer-free) half-res path.
    store = evidence_store(bundle.extra)
    assert store is not None
    stored = store.frames()[-1]
    assert stored.pointer_x == 70 and stored.pointer_state == "pressed"
    expected = srv_server_replies._half_res_action_image(stored.image_base64)
    assert expected is not None
    expected_b64, expected_mime, _scale, _in_image = expected
    assert _image_blocks(response)[0].mimeType == expected_mime
    assert _image_blocks(response)[0].data == expected_b64


async def test_execute_type_shows_normal_at_the_cursor(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend = await _pointer_session(monkeypatch)
    response = await srv_server_execute.computer_execute(
        session_id, "type", text="hello", approved=True
    )
    payload = await _payload(response)
    pointer = payload["pointer"]
    assert pointer["state"] == "normal", "type is not a pointer action"
    assert pointer["x"] == 100 and pointer["y"] == 100  # the capture-time cursor


async def test_execute_show_pointer_false_session_has_no_pointer_fields(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, bundle, _backend = await _pointer_session(monkeypatch, show_pointer=False)
    response = await srv_server_execute.computer_execute(
        session_id, "click", x=60, y=40, approved=True
    )
    payload = await _payload(response)
    assert "pointer" not in payload and "last_click" not in payload
    store = evidence_store(bundle.extra)
    assert store is not None
    stored = store.frames()[-1]
    assert stored.pointer_state is None
    assert _image_blocks(response)[0].data is not None


# --- fetch -----------------------------------------------------------------------------------------------


async def test_fetch_composites_from_the_stored_pointer_and_the_store_stays_clean(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, bundle, backend = await _pointer_session(monkeypatch)
    blocks = srv_server_observe.computer_observe(session_id)
    meta = _metadata(blocks)
    image_id = meta["image_id"]
    fetched = await srv_server_fetch.computer_fetch_image(session_id, image_id)
    fetched_meta = _metadata(fetched)
    pointer = fetched_meta["pointer"]
    assert pointer["x"] == 100 and pointer["y"] == 100
    assert pointer["state"] == "normal" and pointer["in_image"] is True
    # Deterministic recomposition: fetched outbound == the publishing reply's outbound.
    assert _image_blocks(fetched)[0].data == _image_blocks(blocks)[0].data
    # The held frame is STILL the clean capture bytes.
    store = evidence_store(bundle.extra)
    assert store is not None
    assert store.frames()[-1].image_base64 == backend.observed[-1].image_base64


async def test_fetch_on_a_session_with_pointer_off_stays_clean(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, backend = await _pointer_session(monkeypatch, show_pointer=False)
    blocks = srv_server_observe.computer_observe(session_id)
    image_id = _metadata(blocks)["image_id"]
    fetched = await srv_server_fetch.computer_fetch_image(session_id, image_id)
    fetched_meta = _metadata(fetched)
    assert "pointer" not in fetched_meta
    assert _image_blocks(fetched)[0].data == backend.observed[-1].image_base64


# --- verification / digest cleanliness ---------------------------------------------------------------------


async def test_verification_and_digest_captures_are_byte_identical_with_pointer_on_and_off(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pixels the verification diff/digest consume never contain the sprite:
    an identical click run with show_pointer on vs off produces byte-identical
    INTERNAL captures and byte-identical STORED frames."""
    captures: dict[str, list[str]] = {}
    stored: dict[str, list[str]] = {}
    for label, kwargs in (("on", {}), ("off", {"show_pointer": False})):
        session_id, bundle, backend = await _pointer_session(monkeypatch, **kwargs)
        await srv_server_execute.computer_execute(
            session_id, "click", x=60, y=40, approved=True
        )
        captures[label] = [obs.image_base64 for obs in backend.observed]
        store = evidence_store(bundle.extra)
        assert store is not None
        stored[label] = [frame.image_base64 for frame in store.frames()]
    assert captures["on"] == captures["off"], "internal capture bytes unaffected"
    assert stored["on"] == stored["off"], "stored evidence bytes unaffected"


# --- schemas / surface -------------------------------------------------------------------------------------


def test_show_pointer_advertised_plain_schema_with_none_default() -> None:
    """``show_pointer`` joins computer_observe / start_session as a trailing
    optional; the advertised shape is the plain boolean|null type-array
    (no anyOf), default null."""
    observe_props = srv_server_app.mcp._tool_manager.get_tool(
        "computer_observe"
    ).fn_metadata.arg_model.model_json_schema()["properties"]
    assert observe_props["show_pointer"]["type"] == ["boolean", "null"]
    assert observe_props["show_pointer"]["default"] is None
    assert "anyOf" not in observe_props["show_pointer"]
    start_props = srv_server_app.mcp._tool_manager.get_tool(
        "start_session"
    ).fn_metadata.arg_model.model_json_schema()["properties"]
    assert start_props["show_pointer"]["type"] == ["boolean", "null"]
    assert start_props["show_pointer"]["default"] is None
    assert "anyOf" not in start_props["show_pointer"]
    observe_params = list(inspect.signature(srv_server_observe.computer_observe).parameters)
    assert observe_params[-1] == "show_pointer"


async def test_tool_count_stays_10(fresh_server: Any) -> None:
    """WP-C adds NO new tool: the stdio surface stays exactly 10 tools."""
    tools = await srv_server_app.mcp.list_tools()
    assert len(tools) == 10
    names = {tool.name for tool in tools}
    assert "computer_fetch_image" in names and "computer_observe" in names


async def test_unknown_argument_on_observe_still_rejected(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend = await _pointer_session(monkeypatch)
    with pytest.raises(TypeError):
        srv_server_observe.computer_observe(session_id, bogus_pointer=1)  # type: ignore[call-arg]


# ==============================================================================================
# --- DELTA 1 (VAL-WP-C-01): sizing, in_image, frame fidelity, fetch snapshot/off-loop, --------
# --- plan steps, env parse, encode-count/cost pins, MCP unknown-argument gate -----------------
# ==============================================================================================


class FrameBearingBackend(ScriptedBackend):
    """A backend that carries a REAL ``_frame`` stash like the desktop backend —
    the byte-identity spy below needs the frame path, which the plain fakes
    never exercise (DELTA 1 M9). DELTA 2 N3: the stash IS the decoded payload,
    so the stash pixels equal the served payload and sprite assertions are
    real (not green-vs-noise trivialities)."""

    def __init__(self, *args: Any, override_png: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        width, height = self.width, self.height
        payload = override_png or _white_png_b64(width, height)
        self.observe_override_png = payload
        stash = Image.open(io.BytesIO(base64.b64decode(payload, validate=True)))
        stash.load()
        self._frame_constant = stash.convert("RGB")

    def observe(self) -> Any:
        observation = super().observe()
        observation._frame = self._frame_constant  # the SAME object every capture
        return observation


def _deterministic_noise_png(width: int = 1920, height: int = 1080, seed: int = 7) -> str:
    """A seeded noise frame: big enough to exceed the outbound budget (the real
    ladder path) and byte-identical across sessions (digests comparable)."""
    import random

    payload = random.Random(seed).randbytes(width * height)
    image = Image.frombytes("L", (width, height), payload).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _deterministic_gradient_png(width: int = 1280, height: int = 720) -> str:
    """A seeded-free SMOOTH gradient frame (DELTA 3): deterministic across
    sessions, and JPEG-friendly — two q85 encodes of near-identical gradients
    differ only around the sprite, so strong-threshold diffs stay local."""
    gradient = Image.linear_gradient("L").resize((width, height)).convert("RGB")
    buffer = io.BytesIO()
    gradient.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


async def _frame_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cursor: tuple[int, int] | None = (100, 100),
    width: int = 1920,
    height: int = 1080,
    override_png: str | None = None,
    **start_kwargs: Any,
) -> tuple[str, Any, FrameBearingBackend]:
    backend = FrameBearingBackend(
        width=width, height=height, cursor=cursor, flip=False, override_png=override_png
    )
    session_id, _bundle, backend, _provider = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
        **start_kwargs,
    )
    bundle = srv_server_lifecycle._get_bundle(session_id)
    return session_id, bundle, backend


def test_show_pointer_env_parse_garbage_falls_back_zero_is_off_param_beats_env(
    monkeypatch: pytest.MonkeyPatch, fresh_server: Any
) -> None:
    """DELTA 1 M9: the CORTEX_SHOW_POINTER parse — garbage falls back to the
    default (on), ``0`` is off, and an explicit parameter beats the env."""
    monkeypatch.delenv("CORTEX_SHOW_POINTER", raising=False)
    assert resolve_show_pointer(None) is True  # default
    monkeypatch.setenv("CORTEX_SHOW_POINTER", "0")
    assert resolve_show_pointer(None) is False
    monkeypatch.setenv("CORTEX_SHOW_POINTER", "garbage")
    assert resolve_show_pointer(None) is True, "fallback-not-clamp"
    monkeypatch.setenv("CORTEX_SHOW_POINTER", "0")
    assert resolve_show_pointer(True) is True, "the parameter beats the env"
    assert resolve_show_pointer(False) is False
    # Session layer: the param (None = env) and the stored session default.
    monkeypatch.setenv("CORTEX_SHOW_POINTER", "0")
    session_id, bundle, _backend = _pointer_session_sync(monkeypatch)  # show off via env
    assert call_show_pointer(bundle, None) is False
    assert call_show_pointer(bundle, True) is True


def _pointer_session_sync(monkeypatch: pytest.MonkeyPatch) -> tuple[str, Any, Any]:
    backend = ScriptedBackend(width=640, height=480, cursor=(100, 100), flip=False)
    backend.observe_override_png = _white_png_b64(640, 480)
    session_id, _bundle, backend, _provider = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    bundle = srv_server_lifecycle._get_bundle(session_id)
    return session_id, bundle, backend


async def test_show_pointer_non_bool_is_typed_rejected(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 MINOR 5: show_pointer accepts only a real bool or None — the
    typed ``invalid_show_pointer`` rejection, before any capture."""
    session_id, _bundle, _backend = await _pointer_session(monkeypatch)
    result = srv_server_observe.computer_observe(session_id, show_pointer="false")
    assert result["ok"] is False and result["error"] == "invalid_show_pointer"
    started = srv_server_lifecycle.start_session(show_pointer="yes")
    assert started["ok"] is False and started["error"] == "invalid_show_pointer"


async def test_observe_grid_view_draws_the_pointer(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 M9: the observe GRID view composites after the labels — the tip
    sits on the cursor in grid (capture) coordinates."""
    session_id, _bundle, _backend = await _pointer_session(monkeypatch)
    blocks = srv_server_observe.computer_observe(session_id, visual_view="grid")
    meta = _metadata(blocks)
    assert meta["visual_view"] == "grid"
    assert meta["pointer"]["in_image"] is True
    assert meta["pointer"]["x"] == 100 and meta["pointer"]["y"] == 100
    off_id, _b2, _b3 = await _pointer_session(monkeypatch, show_pointer=False)
    clean = srv_server_observe.computer_observe(off_id, visual_view="grid")
    bbox = _diff_bbox(_image_blocks(clean)[0].data, _image_blocks(blocks)[0].data)
    assert bbox and bbox != (-1, -1, -1, -1)
    assert abs(bbox[0] - 100) <= 1 and abs(bbox[1] - 100) <= 1


async def test_execute_grid_reply_shows_pressed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 M9 + DELTA 2 NIT: the execute GRID reply composites AFTER the
    0.5 resize (scale 0.5), so the sprite keeps its screen-content size —
    pinned by PIXELS (tip within 1 px of (x/2, y/2) on the served grid), not
    only metadata."""
    session_id, _bundle, _backend = await _pointer_session(monkeypatch)
    response = await srv_server_execute.computer_execute(
        session_id, "click", x=60, y=40, approved=True, visual_view="grid"
    )
    payload = await _payload(response)
    assert payload["visual_view"] == "grid"
    pointer = payload["pointer"]
    assert pointer["state"] == "pressed"
    assert pointer["in_image"] is True  # the REAL verdict from the blocks builder
    assert pointer["x"] == 60 and pointer["y"] == 40
    # Pixel placement: the clean grid of a show-off session vs this reply.
    # Geometry (DELTA 3 NIT): the composite runs AFTER the 0.5 resize, so the
    # click point lands at (30, 20); the pressed ring at scale 0.5 has radius
    # max(6, round(10*0.5)) = 6 with the dark outer stroke at radius 7, so the
    # changed area's left/top edge is EXACTLY (30-7, 20-7) = (23, 13) — the
    # assertion window is ≤1 px around that geometric edge (grid replies are
    # lossless PNG pass-throughs, so there is no codec noise to absorb).
    off_id, _b2, _b3 = await _pointer_session(monkeypatch, show_pointer=False)
    clean = await srv_server_execute.computer_execute(
        off_id, "click", x=60, y=40, approved=True, visual_view="grid"
    )
    bbox = _diff_bbox(
        _image_blocks(clean)[0].data, _image_blocks(response)[0].data, threshold=64
    )
    assert bbox and bbox != (-1, -1, -1, -1), "the served grid carries the sprite"
    assert abs(bbox[0] - (30 - 7)) <= 1, f"ring left edge at x={bbox[0]} (expected 23 ± 1)"
    assert abs(bbox[1] - (20 - 7)) <= 1, f"ring top edge at y={bbox[1]} (expected 13 ± 1)"


async def test_half_res_reply_places_the_tip_within_1px(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 M9: the DEFAULT half-res execute reply's sprite tip lands within
    1 px of (x/2, y/2) on the served copy."""
    on_id, _b1, _b2 = await _pointer_session(monkeypatch)
    off_id, _b3, _b4 = await _pointer_session(monkeypatch, show_pointer=False)
    reply_on = await srv_server_execute.computer_execute(on_id, "type", text="hi", approved=True)
    reply_off = await srv_server_execute.computer_execute(off_id, "type", text="hi", approved=True)
    payload_on = await _payload(reply_on)
    assert payload_on["pointer"]["state"] == "normal"
    assert payload_on["pointer"]["in_image"] is True
    assert payload_on["image_outbound_scale"] == 0.5  # the default half-res JPEG
    bbox = _diff_bbox(_image_blocks(reply_off)[0].data, _image_blocks(reply_on)[0].data, threshold=64)
    assert bbox and bbox != (-1, -1, -1, -1)
    assert abs(bbox[0] - 50) <= 1 and abs(bbox[1] - 50) <= 1  # (100, 100) at scale 0.5
    # The strong-change threshold ignores the q60 JPEG artifacts around the
    # arrow; a 1 px placement error on the served copy is the pin.


async def test_execute_show_pointer_false_reply_equals_the_old_path(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 M9: show_pointer=False gives the reply the OLD path would have —
    the exact half-res ladder output over the clean capture."""
    session_id, _bundle, backend = await _pointer_session(monkeypatch, show_pointer=False)
    reply = await srv_server_execute.computer_execute(
        session_id, "type", text="hi", approved=True
    )
    payload = await _payload(reply)
    assert "pointer" not in payload
    clean = backend.observed[-1].image_base64
    expected = srv_server_replies._half_res_action_image(clean)
    assert expected is not None
    expected_b64, expected_mime, _scale, _in_image = expected
    assert _image_blocks(reply)[0].mimeType == expected_mime
    assert _image_blocks(reply)[0].data == expected_b64


async def test_frame_bearing_backend_verifier_and_digest_inputs_identical(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 M9 / DELTA 2 N3: with a REAL ``_frame`` stash whose pixels ARE
    the served payload, what the verifier and the digest consume is
    byte-identical with the pointer on and off — the internal captures, the
    stash bytes (``_frame.tobytes()``) and the digests never change. The
    sprite assertion is REAL: the served copy differs from the capture at the
    hotspot neighbourhood and NOWHERE far from it (exact pixels — the
    under-budget reply is a lossless PNG pass-through)."""
    results: dict[str, Any] = {}
    noise_png = _deterministic_noise_png(1920, 1080)
    for label, kwargs in (("on", {}), ("off", {"show_pointer": False})):
        session_id, bundle, backend = await _frame_session(
            monkeypatch, override_png=noise_png, **kwargs
        )
        blocks = srv_server_observe.computer_observe(session_id)
        meta = _metadata(blocks)
        observation = backend.observed[-1]
        results[label] = {
            "digest": meta["digest"],
            "frame_bytes": observation._frame.tobytes(),
            "internal_png": observation.image_base64,
            "stored_png": evidence_store(bundle.extra).frames()[-1].image_base64,
        }
    assert results["on"]["frame_bytes"] == results["off"]["frame_bytes"]
    assert results["on"]["digest"] == results["off"]["digest"]
    assert results["on"]["internal_png"] == results["off"]["internal_png"]
    assert results["on"]["stored_png"] == results["off"]["stored_png"]

    # The REAL sprite check, on exact pixels: an under-budget session's reply is
    # the composited PNG (lossless), the capture is the same payload clean.
    session_id, _bundle, backend = await _frame_session(
        monkeypatch, override_png=_white_png_b64(640, 480), width=640, height=480
    )
    blocks = srv_server_observe.computer_observe(session_id)
    observation = backend.observed[-1]
    clean = _image(observation.image_base64).convert("RGB")
    served = _image(_image_blocks(blocks)[0].data).convert("RGB")
    assert served.size == clean.size
    diff = ImageChops.difference(clean, served).convert("L")
    neighbourhood = diff.crop((60, 60, 160, 160))  # hotspot (100, 100) ± 60
    assert neighbourhood.getbbox() is not None, "the sprite differs at the hotspot"
    for box in ((0, 0, 640, 55), (0, 155, 640, 480), (0, 0, 55, 640), (155, 0, 640, 640)):
        far = diff.crop(box)
        assert far.getbbox() is None, (
            f"pixels changed FAR from the hotspot (region {box}) — the composite "
            "must touch the hotspot neighbourhood only"
        )


async def test_execute_frame_inputs_identical_and_sprite_local(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 2 N3: the EXECUTE variant of the frame spy — with a real ``_frame``
    stash, what the verifier consumes (the internal captures, the stash bytes)
    is identical with the pointer on and off, the digests match, and the served
    full-res copy differs from the clean capture ONLY around the click point."""
    results: dict[str, Any] = {}
    replies: dict[str, Any] = {}
    for label, kwargs in (("on", {}), ("off", {"show_pointer": False})):
        session_id, bundle, backend = await _frame_session(
            monkeypatch, override_png=_white_png_b64(640, 480), width=640, height=480, **kwargs
        )
        reply = await srv_server_execute.computer_execute(
            session_id, "click", x=60, y=40, approved=True, include_screenshot_after=True
        )
        payload = await _payload(reply)
        observation = backend.observed[-1]
        store = evidence_store(bundle.extra)
        assert store is not None
        stored = store.frames()[-1]
        results[label] = {
            "stored_digest": stored.digest,
            "frame_bytes": observation._frame.tobytes(),
            "internal_png": observation.image_base64,
            "stored_png": stored.image_base64,
            "result": payload["result"],
        }
        replies[label] = _image_blocks(reply)[0].data
    assert results["on"]["result"] == results["off"]["result"]
    assert results["on"]["frame_bytes"] == results["off"]["frame_bytes"]
    assert results["on"]["internal_png"] == results["off"]["internal_png"]
    assert results["on"]["stored_png"] == results["off"]["stored_png"]
    assert results["on"]["stored_digest"] == results["off"]["stored_digest"]
    # Exact-pixel sprite locality: the click point is (60, 40); the pressed
    # sprite + ring may differ only inside its neighbourhood box.
    clean = _image(results["off"]["internal_png"]).convert("RGB")
    served = _image(replies["on"]).convert("RGB")
    diff = ImageChops.difference(clean, served).convert("L")
    neighbourhood = diff.crop((20, 10, 120, 110))  # click point (60, 40) ± ~50
    assert neighbourhood.getbbox() is not None, "the pressed sprite is drawn"
    for box in ((0, 0, 640, 8), (0, 112, 640, 480), (0, 0, 18, 640), (122, 0, 640, 640)):
        far = diff.crop(box)
        assert far.getbbox() is None, (
            f"pixels changed FAR from the click point (region {box})"
        )


async def test_pointer_on_observe_encode_count_and_cost_pin(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 M6 + DELTA 2 N4: ONE composite + ONE outbound ladder per
    pointer-on observe — and the full-res PNG encode happens ONLY on the
    under-budget branch (an over-budget capture skips it and the JPEG ladder
    runs straight from the composited frame). Also measures the observe cost
    with the pointer on vs off on a 1920x1080 synthetic frame (printed for the
    mission notes; the assertion is a loose sanity bound)."""
    encodes: dict[str, int] = {}
    ladders: dict[str, int] = {}
    timings: dict[str, float] = {}
    noise_png = _deterministic_noise_png(1920, 1080)
    scenarios = (
        ("on-over-budget", {}, noise_png),
        ("off-over-budget", {"show_pointer": False}, noise_png),
        ("on-under-budget", {}, _white_png_b64(640, 480)),
    )
    for label, kwargs, override in scenarios:
        width, height = (1920, 1080) if override is noise_png else (640, 480)
        session_id, _bundle, _backend = await _frame_session(
            monkeypatch, override_png=override, width=width, height=height, **kwargs
        )
        srv_server_observe.computer_observe(session_id)  # warm the asset cache once
        real_encode = srv_server_observe.encode_png_b64
        real_ladder = srv_server_observe._bound_outbound_image
        counters = {"encode": 0, "ladder": 0}

        def counting_encode(image: Any, _real=real_encode, _counters=counters) -> str:
            _counters["encode"] += 1
            return _real(image)

        def counting_ladder(data: str, frame: Any = None, _real=real_ladder, _counters=counters) -> Any:
            _counters["ladder"] += 1
            return _real(data, frame=frame)

        monkeypatch.setattr(srv_server_observe, "encode_png_b64", counting_encode)
        monkeypatch.setattr(srv_server_observe, "_bound_outbound_image", counting_ladder)
        samples = []
        for _ in range(3):
            clock = time.perf_counter()
            srv_server_observe.computer_observe(session_id)
            samples.append((time.perf_counter() - clock) * 1000.0)
        encodes[label] = counters["encode"]
        ladders[label] = counters["ladder"]
        timings[label] = round(statistics.mean(samples), 2)
        monkeypatch.setattr(srv_server_observe, "encode_png_b64", real_encode)
        monkeypatch.setattr(srv_server_observe, "_bound_outbound_image", real_ladder)
    print(
        f"observe cost on a 1920x1080 synthetic frame: pointer ON "
        f"{timings['on-over-budget']} ms (encodes={encodes['on-over-budget']}, "
        f"ladders={ladders['on-over-budget']}, over-budget: NO full-res PNG "
        f"encode — DELTA 2 N4), pointer OFF {timings['off-over-budget']} ms "
        f"(encodes={encodes['off-over-budget']}, ladders={ladders['off-over-budget']}); "
        f"under-budget pointer ON: encodes={encodes['on-under-budget']}, "
        f"ladders={ladders['on-under-budget']} (3 observes)"
    )
    # DELTA 2 N4: over budget — the ladder runs once per observe and the
    # full-res PNG encode is SKIPPED entirely (both on and off).
    assert encodes["on-over-budget"] == 0 and ladders["on-over-budget"] == 3
    assert encodes["off-over-budget"] == 0 and ladders["off-over-budget"] == 3
    # Under budget — the composited PNG is the delivered copy: one encode + one
    # (pass-through) ladder PER observe (3 observes ran).
    assert encodes["on-under-budget"] == 3 and ladders["on-under-budget"] == 3
    assert timings["on-over-budget"] < timings["off-over-budget"] * 4 + 200.0, (
        "the pointer path stays cheap"
    )


async def test_observe_budget_band_base64_over_decoded_under(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 3 (blocking fix, band A): a capture whose DECODED size is UNDER
    the outbound budget while its base64 LENGTH is OVER it must take the PNG
    path — the old ``len(carried_b64)`` check classed this band over budget,
    served the clean PNG without the sprite, and still reported
    ``pointer.in_image: true``. Pinned: the sprite is in the served PNG
    (exact pixels differ at the hotspot, unchanged elsewhere), exactly ONE
    encode, and the REAL in_image verdict. (A NOISE payload: the sprite must
    add almost nothing to the encoded size, so a gradient would flip the
    composited PNG over the budget — the documented MINOR 6 format flip — and
    break the band.)"""
    from computer_use_mcp.server.replies import _base64_decoded_byte_size

    png = _deterministic_noise_png(400, 300)
    decoded = _base64_decoded_byte_size(png)
    base64_len = len(png)
    assert decoded < base64_len
    # A budget strictly BETWEEN the decoded size and the base64 length — the
    # band the old check misclassified (with 2 bytes of rounding slack).
    budget_bytes = (decoded + base64_len) // 2
    assert decoded < budget_bytes - 2 and base64_len > budget_bytes + 2
    monkeypatch.setenv("CORTEX_RESULT_IMAGE_MAX_KB", f"{budget_bytes / 1024:.6f}")

    session_id, bundle, backend = await _frame_session(
        monkeypatch, override_png=png, width=400, height=300
    )
    srv_server_observe.computer_observe(session_id)  # warm the asset cache once
    real_encode = srv_server_observe.encode_png_b64
    real_ladder = srv_server_observe._bound_outbound_image
    counters = {"encode": 0, "ladder": 0}

    def counting_encode(image: Any, _real=real_encode, _counters=counters) -> str:
        _counters["encode"] += 1
        return _real(image)

    def counting_ladder(data: str, frame: Any = None, _real=real_ladder, _counters=counters) -> Any:
        _counters["ladder"] += 1
        return _real(data, frame=frame)

    monkeypatch.setattr(srv_server_observe, "encode_png_b64", counting_encode)
    monkeypatch.setattr(srv_server_observe, "_bound_outbound_image", counting_ladder)
    blocks = srv_server_observe.computer_observe(session_id)
    monkeypatch.setattr(srv_server_observe, "encode_png_b64", real_encode)
    monkeypatch.setattr(srv_server_observe, "_bound_outbound_image", real_ladder)

    meta = _metadata(blocks)
    pointer = meta["pointer"]
    assert pointer["in_image"] is True, "the hotspot is inside the capture"
    assert meta["image_format"] == "image/png", "decoded-under-budget stays PNG"
    assert counters == {"encode": 1, "ladder": 1}, "one encode + one ladder"

    # Exact pixels: the served PNG differs from the capture at the hotspot
    # neighbourhood and NOWHERE far from it.
    clean = _image(backend.observed[-1].image_base64).convert("RGB")
    served = _image(_image_blocks(blocks)[0].data).convert("RGB")
    assert served.size == clean.size
    diff = ImageChops.difference(clean, served).convert("L")
    neighbourhood = diff.crop((45, 45, 155, 155))  # hotspot (100, 100) ± 55
    assert neighbourhood.getbbox() is not None, "the sprite differs at the hotspot"
    for box in ((0, 0, 400, 45), (0, 155, 400, 300), (0, 0, 45, 300), (155, 0, 400, 300)):
        far = diff.crop(box)
        assert far.getbbox() is None, f"pixels changed FAR from the hotspot ({box})"


async def test_observe_budget_band_decoded_over_budget_ladder_from_composited(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 3 (blocking fix, band B mirror): a capture whose DECODED size is
    OVER the budget hands the JPEG ladder the COMPOSITED frame — never the
    clean stash — with exactly ZERO full-res PNG encodes and
    ``pointer.in_image`` true. The frame ARGUMENT is pinned on exact pixels
    (the sprite differs from the stash at the hotspot, nowhere else); whether
    the sprite also survives the STOCK ladder's own downscale/quality descent
    depends on how far over budget the capture is, so the served-JPEG visual
    check runs only when the ladder delivered full resolution."""
    from computer_use_mcp.server.replies import _base64_decoded_byte_size

    png = _deterministic_gradient_png(1920, 1080)
    decoded = _base64_decoded_byte_size(png)
    # HALF the decoded size: the payload is guaranteed over budget.
    monkeypatch.setenv("CORTEX_RESULT_IMAGE_MAX_KB", f"{(decoded // 2) / 1024:.6f}")

    counters_by_label: dict[str, dict[str, int]] = {}
    ladder_frames: dict[Any, Any] = {}
    replies: dict[str, Any] = {}
    for label, kwargs in (("on", {}), ("off", {"show_pointer": False})):
        session_id, bundle, backend = await _frame_session(
            monkeypatch, override_png=png, width=1920, height=1080, **kwargs
        )
        real_encode = srv_server_observe.encode_png_b64
        real_ladder = srv_server_observe._bound_outbound_image
        counters = {"encode": 0, "ladder": 0}

        def counting_encode(image: Any, _real=real_encode, _counters=counters) -> str:
            _counters["encode"] += 1
            return _real(image)

        def counting_ladder(
            data: str, frame: Any = None, _real=real_ladder, _counters=counters,
            _label=label, _frames=ladder_frames,
        ) -> Any:
            _counters["ladder"] += 1
            _frames[_label] = frame
            return _real(data, frame=frame)

        monkeypatch.setattr(srv_server_observe, "encode_png_b64", counting_encode)
        monkeypatch.setattr(srv_server_observe, "_bound_outbound_image", counting_ladder)
        blocks = srv_server_observe.computer_observe(session_id)
        monkeypatch.setattr(srv_server_observe, "encode_png_b64", real_encode)
        monkeypatch.setattr(srv_server_observe, "_bound_outbound_image", real_ladder)
        counters_by_label[label] = counters
        replies[label] = blocks

    meta_on = _metadata(replies["on"])
    pointer = meta_on["pointer"]
    assert pointer["in_image"] is True
    assert meta_on["image_format"] == "image/jpeg", "decoded-over-budget ladders to JPEG"
    assert counters_by_label["on"] == {"encode": 0, "ladder": 1}, "no full-res PNG encode"
    assert counters_by_label["off"] == {"encode": 0, "ladder": 1}

    # The ladder's FRAME ARGUMENT (exact pixels): the ON call received a frame
    # that differs from the clean stash at the hotspot and NOWHERE else —
    # i.e. the JPEG is encoded from the composited copy, never the stash.
    clean_stash = backend.observed[-1]._frame.convert("RGB")
    on_frame = ladder_frames["on"]
    assert on_frame is not None and on_frame.size == clean_stash.size
    diff = ImageChops.difference(clean_stash, on_frame.convert("RGB")).convert("L")
    neighbourhood = diff.crop((60, 60, 160, 160))  # hotspot (100, 100) ± 60
    assert neighbourhood.getbbox() is not None, "the ladder frame carries the sprite"
    for box in ((0, 0, 1920, 55), (0, 155, 1920, 1080), (0, 0, 55, 1080), (155, 0, 1920, 1080)):
        far = diff.crop(box)
        assert far.getbbox() is None, f"the ladder frame changed FAR from the hotspot ({box})"
    # The OFF call received the clean stash (no sprite anywhere).
    off_frame = ladder_frames["off"]
    assert off_frame is not None
    assert ImageChops.difference(
        clean_stash, off_frame.convert("RGB")
    ).getbbox() is None, "the pointer-off ladder frame must be the clean stash"

    # When the stock ladder delivered FULL resolution (no downscale), the
    # sprite is also visible in the served JPEG.
    applied_scale = meta_on.get("image_outbound_scale")
    if applied_scale is None:
        on_image = _image(_image_blocks(replies["on"])[0].data).convert("RGB")
        off_image = _image(_image_blocks(replies["off"])[0].data).convert("RGB")
        diff = ImageChops.difference(off_image, on_image).convert("L")
        bbox = diff.point(lambda value: 255 if value > 64 else 0).getbbox()
        assert bbox is not None, "the sprite survives the full-resolution JPEG"


async def test_fetch_of_a_zoom_frame_composites_into_crop_space(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 M9: a fetch of a ZOOM frame composites at the crop-local point."""
    session_id, bundle, _backend = await _pointer_session(monkeypatch)
    blocks = srv_server_observe.computer_zoom(session_id, [40, 40, 80, 80])
    image_id = _metadata(blocks)["image_id"]
    fetched = await srv_server_fetch.computer_fetch_image(session_id, image_id)
    fetched_meta = _metadata(fetched)
    pointer = fetched_meta["pointer"]
    assert pointer["in_image"] is True
    assert pointer["x"] == 100 and pointer["y"] == 100  # canonical, not crop-local
    # The fetched copy shows the tip at crop-local (60, 60) — compare against
    # the clean zoom reply of a pointer-off session.
    off_id, _b2, _b3 = await _pointer_session(monkeypatch, show_pointer=False)
    clean = srv_server_observe.computer_zoom(off_id, [40, 40, 80, 80])
    bbox = _diff_bbox(_image_blocks(clean)[0].data, _image_blocks(fetched)[0].data)
    assert bbox and bbox != (-1, -1, -1, -1)
    assert abs(bbox[0] - 60) <= 1 and abs(bbox[1] - 60) <= 1
    # The stored frame is STILL the clean crop.
    store = evidence_store(bundle.extra)
    assert store is not None
    stored = store.frames()[-1]
    assert stored.crop_region == (40, 40, 80, 80)
    clean_crop = Image.open(io.BytesIO(base64.b64decode(stored.image_base64, validate=True)))
    clean_crop.load()
    assert clean_crop.size == (80, 80)


async def test_fetch_of_a_pressed_execute_frame_shows_pressed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 M9 + M4: a fetch of a PRESSED execute frame composites the
    pressed sprite + ring from the stored pointer, and serves the stored
    canonical last_click snapshot (never a ring read at fetch time)."""
    session_id, bundle, _backend = await _pointer_session(monkeypatch)
    reply = await srv_server_execute.computer_execute(
        session_id, "click", x=60, y=40, approved=True, include_screenshot_after=True
    )
    image_id = (await _payload(reply))["image_id"]
    fetched = await srv_server_fetch.computer_fetch_image(session_id, image_id)
    fetched_meta = _metadata(fetched)
    pointer = fetched_meta["pointer"]
    assert pointer["state"] == "pressed" and pointer["in_image"] is True
    assert pointer["x"] == 60 and pointer["y"] == 40
    last_click = fetched_meta["last_click"]
    assert last_click is not None
    assert (last_click["x"], last_click["y"], last_click["button"]) == (60, 40, "left")
    assert last_click["age_ms"] is not None
    store = evidence_store(bundle.extra)
    assert store is not None
    stored = store.frames()[-1]
    assert stored.last_click_x == 60 and stored.last_click_button == "left"
    # The served copy shows the ring (pressed): the changed area starts at the
    # ring's left edge, ~radius 6..12 left of the click point.
    bbox = _diff_bbox(stored.image_base64, _image_blocks(fetched)[0].data)
    assert bbox and bbox != (-1, -1, -1, -1)
    assert 60 - 13 <= bbox[0] <= 60 - 5


async def test_plan_step_shows_normal_at_the_captured_cursor_and_fetch_matches(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 2 N1: a plan step's image is the post-MOVE frame (captured BEFORE
    the point's click), so the step shows NORMAL at the captured cursor — never
    Pressed for the declared click (which may have been rejected). The ring
    still reports the click through ``last_click`` (from the ring), and a fetch
    of that step's image shows the same normal pointer."""
    from plan_explicitness import complete_plan

    # ONE point whose declared action is a click: the whole point runs as ONE
    # step (transition move + click), the walk ENDS on it, and the walk-ending
    # STEP reply (the blocked handshake reply) carries the frame + image_id.
    # The served fake capture is 64x48 and the click point (60, 40) stays
    # inside it. The lease is released with an explicit stop afterwards.
    backend = ScriptedBackend(width=640, height=480, cursor=(100, 100), flip=True)
    session_id, bundle, _backend, _provider = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    step = await _payload(
        await srv_server_execute.computer_execute(
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
    )
    pointer = step.get("pointer")
    assert pointer is not None, (
        f"the image-bearing step reply carries the pointer block; step summary: "
        f"result={step.get('result')} failure_reason={step.get('failure_reason')} "
        f"blocked={step.get('blocked')} next_step={step.get('next_step')} "
        f"follow_up_results={json.dumps(step.get('follow_up_results'))[:400]}"
    )
    assert pointer["state"] == "normal", (
        "the step image is the post-MOVE frame — Pressed for the declared "
        "click would lie (DELTA 2 N1)"
    )
    assert pointer["x"] == 60 and pointer["y"] == 40  # the cursor after the move
    assert pointer["in_image"] is True
    last_click = step.get("last_click")
    assert last_click is not None, "the click still reports through last_click (the ring)"
    assert (last_click["x"], last_click["y"], last_click["button"]) == (60, 40, "left")
    image_id = step.get("image_id")
    assert isinstance(image_id, str) and image_id.startswith("img_")
    fetched = await srv_server_fetch.computer_fetch_image(session_id, image_id)
    fetched_meta = _metadata(fetched)
    fetched_pointer = fetched_meta["pointer"]
    assert fetched_pointer["state"] == "normal" and fetched_pointer["in_image"] is True
    assert fetched_pointer["x"] == 60 and fetched_pointer["y"] == 40
    assert fetched_meta["last_click"] is not None
    await srv_server_execute.computer_execute(session_id, "done", plan_action="stop")


async def test_plan_step_on_a_show_pointer_false_session_has_no_pointer_keys(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 2 N2: the opt-out is airtight on plan steps — a show_pointer=False
    session's image-bearing step reply carries NO pointer/last_click keys, and
    the stored frame keeps neither the pointer nor the last_click snapshot."""
    from plan_explicitness import complete_plan

    backend = ScriptedBackend(width=640, height=480, cursor=(100, 100), flip=True)
    session_id, bundle, _backend, _provider = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
        show_pointer=False,
    )
    step = await _payload(
        await srv_server_execute.computer_execute(
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
    )
    assert "pointer" not in step and "last_click" not in step
    assert step.get("image_id"), "the step reply is image-bearing"
    store = evidence_store(bundle.extra)
    assert store is not None
    stored = store.frames()[-1]
    assert stored.pointer_state is None and stored.pointer_x is None
    assert stored.last_click_x is None and stored.last_click_button is None
    await srv_server_execute.computer_execute(session_id, "done", plan_action="stop")


async def test_mcp_unknown_argument_gate_rejects_bogus_show_pointer(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 M9: the MCP-level unknown-argument gate (instance.py) rejects an
    argument the tool does not declare — the typed ``unknown_argument`` reasons."""
    session_id, _bundle, _backend = await _pointer_session(monkeypatch)
    reply = await srv_server_app.mcp.call_tool(
        "computer_observe", {"session_id": session_id, "bogus_pointer": 1}
    )
    bodies: list[dict[str, Any]] = []
    entries = reply[0] if isinstance(reply, tuple) and len(reply) == 2 else reply
    for entry in entries or []:
        text = getattr(entry, "text", None)
        if text:
            try:
                bodies.append(json.loads(text))
            except ValueError:
                pass
    assert bodies, "the rejection came back through the tool's own result shape"
    rejection = bodies[0]
    assert rejection["ok"] is False and rejection["result"] == "rejected"
    assert rejection["reasons"][0] == "unknown_argument"


async def test_fetch_composite_runs_off_the_event_loop(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 M5: the fetch decode/composite/encode worker runs OFF the loop —
    a composite that blocks never blocks the loop, and it never ran on it."""
    session_id, _bundle, _backend = await _pointer_session(monkeypatch)
    blocks = srv_server_observe.computer_observe(session_id)
    image_id = _metadata(blocks)["image_id"]
    loop_thread = threading.current_thread()
    seen_threads: list[threading.Thread] = []
    real_composite = srv_server_fetch.composite

    def thread_recording_composite(*args: Any, **kwargs: Any) -> Any:
        seen_threads.append(threading.current_thread())
        return real_composite(*args, **kwargs)

    monkeypatch.setattr(srv_server_fetch, "composite", thread_recording_composite)
    worker_entered = threading.Event()
    release = threading.Event()

    real_worker = srv_server_fetch._fetch_outbound_worker

    def blocking_worker(*args: Any, **kwargs: Any) -> Any:
        worker_entered.set()  # the PIL work is running (off the loop)
        # DELTA 2 NIT: genuinely blocked until the LOOP-side assertions below
        # have run — the second fetch must complete while THIS worker waits.
        release.wait(timeout=10)
        return real_worker(*args, **kwargs)

    monkeypatch.setattr(srv_server_fetch, "_fetch_outbound_worker", blocking_worker)
    task = asyncio.create_task(srv_server_fetch.computer_fetch_image(session_id, image_id))
    for _ in range(100):
        if worker_entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert worker_entered.is_set(), "the fetch never reached its worker"
    # The worker is GENUINELY blocked now — prove the loop stays responsive by
    # driving a full observe on the loop before releasing it.
    blocks_mid = srv_server_observe.computer_observe(session_id)
    assert _metadata(blocks_mid)["image_id"] != image_id
    release.set()
    await asyncio.wait({task}, timeout=10.0)
    assert seen_threads, "the pointer composite never ran"
    assert all(thread is not loop_thread for thread in seen_threads), (
        "the fetch composite ran ON the event loop"
    )
    monkeypatch.setattr(srv_server_fetch, "composite", real_composite)
