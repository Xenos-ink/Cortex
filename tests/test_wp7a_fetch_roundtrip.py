"""VAL-WP5-02 carry-over N1/N2 (executed with WP7a per the WP7a contract).

**N1 — the default-execute fetch round-trip.**  A DEFAULT ``computer_execute``
reply (``include_screenshot_after`` omitted) ships the HALF-RESOLUTION JPEG
(0.5x, q60) while the evidence store retains the raw post-action PNG under the
published ``image_id``.  This file proves the full loop: the reply publishes an
id, a fetch of that id serves the SAME picture at the fetch's OWN disclosed
``image_outbound_scale`` with the STORED width/height, and the fetch reply now
SAYS that a fetched frame may be served at a different resolution than the
reply that published it (the N1 sentence, pinned).

**N2 — ``visual_view`` normalized on fetch replies.**  A raw capture is stored
with ``visual_view=None`` and the fetch reply carries that normalized None —
never the string ``"raw"``.  Pinned for the raw path (the derived-grid path is
already pinned in tests/test_w2_evidence_store.py).
"""

from __future__ import annotations

import base64
import io
from typing import Any

import pytest
from PIL import Image

from reply_contract import validate_reply
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    execute_payload,
    make_session,
)
from test_w2_evidence_store import fetch_metadata, image_blocks

import computer_use_mcp.server.lifecycle as srv_server_lifecycle
import computer_use_mcp.server.wait_machine as srv_server_wait_machine
import computer_use_mcp.server.wait as srv_server_wait
import computer_use_mcp.server.observe as srv_server_observe
import computer_use_mcp.server.execute as srv_server_execute
import computer_use_mcp.server.fetch as srv_server_fetch
from computer_use_mcp.core.state import SessionRegistry


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh bounded registry/bundles + per-test audit dir (same as the controller suite)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(srv_server_lifecycle, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(srv_server_lifecycle, "_bundles", {})
    monkeypatch.setattr(srv_server_wait_machine, "_bundles", srv_server_lifecycle._bundles)
    monkeypatch.setattr(srv_server_wait, "_bundles", srv_server_lifecycle._bundles)
    return None


def asyncio_run_tool(coroutine: Any) -> Any:
    """Run one async tool coroutine on a fresh loop (the established sync-test bridge)."""
    import asyncio

    return asyncio.run(coroutine)


def _decode(block: Any) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(block.data))).convert("RGB")


def _mean_abs_error(first: Image.Image, second: Image.Image) -> float:
    """Mean absolute per-channel difference after resizing ``first`` to ``second``."""
    resized = first.resize(second.size)
    bytes_a = resized.tobytes()
    bytes_b = second.tobytes()
    total = sum(abs(a - b) for a, b in zip(bytes_a, bytes_b))
    return total / len(bytes_b)


def test_a_default_execute_reply_publishes_an_id_whose_fetch_serves_the_same_picture(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1: the default (half-res JPEG) reply publishes an id; the fetch serves the
    SAME picture at its OWN disclosed image_outbound_scale + the STORED width/height."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch,
        backend=ScriptedBackend(flip=True),
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    result = asyncio_run_tool(
        srv_server_execute.computer_execute(session_id, "click", x=10, y=10, approved=True)
    )
    payload = execute_payload(result)
    assert payload["ok"] is True, payload
    image_id = payload["image_id"]
    assert image_id.startswith("img_")
    # the reply's own copy is the HALF-RES JPEG at a disclosed scale
    reply_blocks = image_blocks(result)
    assert len(reply_blocks) == 1
    reply_image = _decode(reply_blocks[0])
    assert reply_image.size == (32, 24)  # the half-res JPEG copy
    assert payload["image_format"] == "image/jpeg"
    assert payload["image_outbound_scale"] == 0.5

    fetched = asyncio_run_tool(srv_server_fetch.computer_fetch_image(session_id, image_id))
    meta = fetch_metadata(fetched)
    validate_reply(meta, "fetch_image")
    fetched_blocks = image_blocks(fetched)
    assert len(fetched_blocks) == 1
    fetched_image = _decode(fetched_blocks[0])
    # the STORED dimensions are disclosed and the bytes decode at exactly them
    assert meta["width"] == fetched_image.width
    assert meta["height"] == fetched_image.height
    # the fetch's OWN scale disclosure is honest: None iff the bytes are full-size
    assert (meta["image_outbound_scale"] is None) == (
        fetched_image.size == (meta["width"], meta["height"])
    )
    # the SAME picture: the half-res JPEG and the fetched copy agree pixel-wise
    error = _mean_abs_error(reply_image, fetched_image)
    assert error < 16.0, f"mean abs error {error} — the fetched frame is not the same picture"
    # the N1 sentence: the reply itself teaches the resolution divergence
    assert "different resolution" in meta["message"]


def test_a_raw_capture_fetches_with_a_normalized_visual_view_of_none(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N2: a raw capture is stored with visual_view None and the fetch reply carries
    the normalized None — never the string "raw" (default execute path)."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch,
        backend=ScriptedBackend(flip=True),
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    result = asyncio_run_tool(
        srv_server_execute.computer_execute(session_id, "click", x=10, y=10, approved=True)
    )
    image_id = execute_payload(result)["image_id"]
    meta = fetch_metadata(asyncio_run_tool(srv_server_fetch.computer_fetch_image(session_id, image_id)))
    assert meta["ok"] is True
    assert meta["visual_view"] is None


def _visual_view_rule_session(
    monkeypatch: pytest.MonkeyPatch, **observe_kwargs: Any
) -> tuple[Any, dict[str, Any]]:
    """One session + one default observe; returns (session_id, its metadata)."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch,
        backend=ScriptedBackend(flip=True),
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    reply = srv_server_observe.computer_observe(session_id, **observe_kwargs)
    from test_w2_evidence_store import observe_metadata

    return session_id, observe_metadata(reply)


def test_an_explicit_raw_observe_stores_visual_view_none(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N2 rule (chosen: None = raw, everywhere, normalized at the ONE storage point
    evidence.register_frame): an EXPLICIT ``visual_view="raw"`` observe request is a
    pass-through capture. The REPLY keeps the caller-facing view marker ("raw"),
    but the STORED frame normalizes to None — the fetch reply carries None, never
    the string "raw"."""
    session_id, meta = _visual_view_rule_session(monkeypatch, visual_view="raw")
    assert meta["visual_view"] == "raw"  # the reply's view marker (caller-facing)
    image_id = meta["image_id"]
    fetched = fetch_metadata(
        asyncio_run_tool(srv_server_fetch.computer_fetch_image(session_id, image_id))
    )
    assert fetched["ok"] is True
    assert fetched["visual_view"] is None  # the STORED frame: normalized None


def test_a_default_observe_stores_visual_view_none(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, meta = _visual_view_rule_session(monkeypatch)
    image_id = meta["image_id"]
    fetched = fetch_metadata(
        asyncio_run_tool(srv_server_fetch.computer_fetch_image(session_id, image_id))
    )
    assert fetched["ok"] is True
    assert fetched["visual_view"] is None


def test_a_default_zoom_stores_visual_view_none(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zoom crop (default raw view) is stored None like every other raw
    capture — the single normalization point covers the zoom path. The REPLY keeps
    the caller-facing "raw" marker; the STORED frame (and thus the fetch) is None."""
    from test_avr_visual_views import RegionFrameBackend
    from test_w2_evidence_store import observe_metadata

    backend = RegionFrameBackend(regions=[])
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    reply = srv_server_observe.computer_zoom(session_id, [8, 8, 32, 16])
    zoom_meta = observe_metadata(reply)
    image_id = zoom_meta.get("image_id")
    assert image_id, zoom_meta
    assert zoom_meta["visual_view"] == "raw"  # the reply's caller-facing marker
    fetched = fetch_metadata(
        asyncio_run_tool(srv_server_fetch.computer_fetch_image(session_id, image_id))
    )
    assert fetched["ok"] is True
    assert fetched["visual_view"] is None
