"""WP1 / §1.1 D-3 (ORVEX-2026-108) — ONE scale key, MEASURED, on every image-bearing reply.

Ported from the dev copy's ``test_image_scale_published.py`` (F1 fix 4.1) and adapted
to the unified reply contract's decision D-3: ``image_scale`` is DELETED, not
duplicated — "no compatibility flags" forbids two keys for one fact. The single key
``image_outbound_scale`` carries the scale the outbound ladder actually applied
(``1.0`` when the delivered copy kept its source dimensions; ``None`` where no image
was served), on the default observe path, the zoom path, and every execute path.
``visual_view_scale`` keeps its own, different meaning (the outbound scale of an
explicitly requested DERIVED view) and is asserted unchanged here.
"""

from __future__ import annotations

import base64
import io
import json
import os
import random
from typing import Any

import pytest
from reply_contract import validate_reply
from test_controller_integration import FAST_LIMITS, ScriptedBackend, make_session

from computer_use_mcp import server
from computer_use_mcp.vision.visual_views import (
    DEFAULT_GRID_DENSITY,
    outbound_view_scale,
    resolve_view,
)

#: The ONLY scale key on an image-bearing reply (§1.1 D-3). Deliberately NOT
#: ``visual_view_scale``: that key belongs to AL-002 and means "the scale of the
#: derived VIEW the host explicitly asked for" (None for a pass-through raw view).
IMAGE_OUTBOUND_SCALE_KEY = "image_outbound_scale"


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Module-local copy of the shared ``fresh_server`` fixture (same body)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", server.SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    yield server


def _noise_png_b64(width: int, height: int, seed: int) -> str:
    """A photorealistically incompressible frame: its PNG really exceeds the budget."""
    from PIL import Image

    rnd = random.Random(seed)
    image = Image.new("RGB", (width, height))
    image.putdata(
        [(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(width * height)]
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _small_png_b64(width: int, height: int) -> str:
    """A compressible frame whose PNG stays well under the outbound budget."""
    from PIL import Image

    image = Image.new("RGB", (width, height), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _metadata(blocks: Any) -> dict[str, Any]:
    return json.loads(blocks[0].text)


def _served_size(blocks: Any) -> tuple[int, int]:
    from PIL import Image

    image = Image.open(io.BytesIO(base64.b64decode(blocks[1].data, validate=True)))
    image.load()
    return image.size


def _session(monkeypatch: pytest.MonkeyPatch, png_b64: str, width: int, height: int) -> Any:
    backend = ScriptedBackend(flip=False, width=width, height=height)
    backend.observe_override_png = png_b64
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    return session_id


def _derived_ratio(served: tuple[int, int], source: tuple[int, int]) -> float:
    """What the caller can measure from the bytes it received (no product code)."""
    return min(served[0] / source[0], served[1] / source[1])


# --- the default observe path: the scale the ladder applied --------------------------------------


async def test_default_observe_publishes_the_measured_outbound_scale(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An over-budget 1920x1080 capture: the default (no ``visual_view``) observe
    response names the scale that was applied, to 4 decimal places, and the
    AL-002 view keys stay absent (the scale key does not become a view key)."""
    png = _noise_png_b64(1920, 1080, seed=11)
    assert len(base64.b64decode(png, validate=True)) > server._result_image_max_bytes()
    session_id = _session(monkeypatch, png, 1920, 1080)

    blocks = server.computer_observe(session_id)
    metadata = _metadata(blocks)
    served = _served_size(blocks)

    assert metadata["image_format"] == "image/jpeg"  # the ladder really ran
    assert served[0] < 1920, served
    assert IMAGE_OUTBOUND_SCALE_KEY in metadata, sorted(metadata)
    assert round(metadata[IMAGE_OUTBOUND_SCALE_KEY], 4) == round(
        _derived_ratio(served, (1920, 1080)), 4
    ), (metadata[IMAGE_OUTBOUND_SCALE_KEY], served)
    # the ladder's OWN arithmetic: 0.85**n steps of the capture, never an upscale
    assert 0.0 < metadata[IMAGE_OUTBOUND_SCALE_KEY] < 1.0
    # an absent view request is still absent: no view key, no view-derived work
    assert "visual_view" not in metadata
    assert "visual_view_scale" not in metadata
    # the deleted legacy marker key stays gone (§1.1 D-3: one key, one meaning)
    assert "image_scale" not in metadata
    # the observation block keeps advertising the CAPTURE dims (unchanged contract)
    assert metadata["observation"]["width"] == 1920


async def test_under_budget_png_is_byte_identical_and_reports_pass_through_none(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PNG that fits the budget travels untouched. MINOR-b (Commander decision):
    a raw PASS-THROUGH reports None — no downscale was applied; 1.0 would be a
    scale without a scale event."""
    png = _small_png_b64(640, 480)
    assert len(base64.b64decode(png, validate=True)) <= server._result_image_max_bytes()
    session_id = _session(monkeypatch, png, 640, 480)

    blocks = server.computer_observe(session_id)
    metadata = _metadata(blocks)

    assert blocks[1].data == png, "an in-budget PNG must stay byte-identical"
    assert blocks[1].mimeType == "image/png"
    assert metadata["image_format"] == "image/png"
    assert metadata[IMAGE_OUTBOUND_SCALE_KEY] is None, metadata


# --- the execute path: the measured key only ------------------------------------------------------


async def test_execute_path_publishes_the_measured_scale_when_the_ladder_fires(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The historical lie (a constant ``image_scale: 0.5`` while the served image was
    0.19x the capture) is gone twice over: the key is deleted AND the surviving key is
    the ladder's measurement."""
    monkeypatch.delenv("CORTEX_ACTION_IMAGE_FULL", raising=False)
    monkeypatch.setenv("CORTEX_RESULT_IMAGE_MAX_KB", "40")  # the half-res encode trips too
    png = _noise_png_b64(2560, 1440, seed=3)
    session_id = _session(monkeypatch, png, 2560, 1440)

    response = await server.computer_execute(session_id, "click", x=10, y=10)
    payload = json.loads(response[0].text)
    served = _served_size(response)

    assert payload["image_format"] == "image/jpeg"
    assert "image_scale" not in payload, "the deleted key must never return"
    assert round(payload[IMAGE_OUTBOUND_SCALE_KEY], 4) == round(
        _derived_ratio(served, (2560, 1440)), 4
    ), (payload[IMAGE_OUTBOUND_SCALE_KEY], served)


async def test_execute_default_path_publishes_the_true_half_scale(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No regression on the common case: a 1920x1080 capture whose half-res JPEG fits
    the budget is served at exactly 0.5 — now carried by the ONE measured key."""
    monkeypatch.delenv("CORTEX_ACTION_IMAGE_FULL", raising=False)
    monkeypatch.delenv("CORTEX_RESULT_IMAGE_MAX_KB", raising=False)
    session_id = _session(monkeypatch, _noise_png_b64(1920, 1080, seed=5), 1920, 1080)

    response = await server.computer_execute(session_id, "click", x=10, y=10)
    payload = json.loads(response[0].text)
    served = _served_size(response)

    assert "image_scale" not in payload
    assert payload[IMAGE_OUTBOUND_SCALE_KEY] == 0.5, payload
    assert served == (960, 540), served


async def test_execute_full_resolution_path_publishes_the_measured_scale(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``include_screenshot_after=true`` is the full-res path: it never claimed the
    half-res marker (that key would lie) and now reports its own measured scale."""
    monkeypatch.delenv("CORTEX_ACTION_IMAGE_FULL", raising=False)
    monkeypatch.delenv("CORTEX_RESULT_IMAGE_MAX_KB", raising=False)
    session_id = _session(monkeypatch, _noise_png_b64(1920, 1080, seed=7), 1920, 1080)

    response = await server.computer_execute(
        session_id, "click", x=10, y=10, include_screenshot_after=True
    )
    payload = json.loads(response[0].text)
    served = _served_size(response)

    assert "image_scale" not in payload
    assert round(payload[IMAGE_OUTBOUND_SCALE_KEY], 4) == round(
        _derived_ratio(served, (1920, 1080)), 4
    ), (payload[IMAGE_OUTBOUND_SCALE_KEY], served)


# --- the grid path: visual_view_scale keeps its own, different meaning ---------------------------


async def test_grid_path_visual_view_scale_is_unchanged(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``visual_view_scale`` still reports the scale of the DERIVED VIEW (AL-002), and
    the measured key rides beside it rather than replacing it."""
    from computer_use_mcp.core.models import Observation

    monkeypatch.delenv("CORTEX_RESULT_IMAGE_MAX_KB", raising=False)
    png = _noise_png_b64(1920, 1080, seed=13)
    session_id = _session(monkeypatch, png, 1920, 1080)

    blocks = server.computer_observe(session_id, visual_view="grid")
    metadata = _metadata(blocks)
    served = _served_size(blocks)

    # recompute the AL-002 datum independently: the derived view is the grid render of
    # the same capture, and the served bytes are the ones that came back.
    derived = resolve_view("grid").derive(
        Observation(image_base64=png, width=1920, height=1080), density=DEFAULT_GRID_DENSITY
    )
    assert derived.image is not None
    assert metadata["visual_view"] == "grid"
    # the pre-port value of this key, pinned exactly: a full-size grid render of a
    # 1920x1080 capture trips the ladder to 722x406
    assert metadata["visual_view_scale"] == 0.3759, metadata
    assert metadata["visual_view_scale"] == outbound_view_scale(derived.image, blocks[1].data)
    assert round(metadata["visual_view_scale"], 4) == round(
        _derived_ratio(served, derived.image.size), 4
    ), (metadata["visual_view_scale"], served)
    # the measured key rides BESIDE it, never in place of it
    assert IMAGE_OUTBOUND_SCALE_KEY in metadata, sorted(metadata)
    assert metadata[IMAGE_OUTBOUND_SCALE_KEY] == metadata["visual_view_scale"]


async def test_execute_grid_path_visual_view_scale_is_unchanged(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The execute surface's pre-existing grid pins are untouched: the derived 64x48
    view is served at the stock half-res 0.5 under the measured key."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(
        session_id, "click", x=20, y=20, visual_view="grid"
    )
    payload = json.loads(response[0].text)
    validate_reply(payload, "flat_execute")
    assert payload["visual_view"] == "grid"
    assert payload["visual_view_scale"] == 0.5, payload
    assert "image_scale" not in payload
    assert payload[IMAGE_OUTBOUND_SCALE_KEY] == 0.5, payload


# --- the ladder's own contract: bytes and arithmetic unchanged -----------------------------------


def test_ladder_reports_a_scale_without_changing_a_single_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_bound_outbound_image`` returns the applied scale beside the payload; the
    encode ladder, the byte budget and the returned image are untouched."""
    from PIL import Image

    monkeypatch.delenv("CORTEX_RESULT_IMAGE_MAX_KB", raising=False)
    frame = Image.frombytes("RGB", (1920, 1080), os.urandom(1920 * 1080 * 3))
    buffer = io.BytesIO()
    frame.save(buffer, format="PNG", optimize=False)
    png = base64.b64encode(buffer.getvalue()).decode("ascii")

    b64, mime, scale = server._bound_outbound_image(png)
    assert mime == "image/jpeg"
    assert len(base64.b64decode(b64, validate=True)) <= server._result_image_max_bytes()
    served = Image.open(io.BytesIO(base64.b64decode(b64, validate=True)))
    served.load()
    assert scale == round(_derived_ratio(served.size, (1920, 1080)), 4), (scale, served.size)

    # in-budget: untouched bytes, ratio 1.0
    small = Image.new("RGB", (64, 48), "white")
    small_buffer = io.BytesIO()
    small.save(small_buffer, format="PNG", optimize=False)
    small_b64 = base64.b64encode(small_buffer.getvalue()).decode("ascii")
    assert server._bound_outbound_image(small_b64) == (small_b64, "image/png", None)
