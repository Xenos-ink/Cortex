"""AL-002 Adaptive Visual Representations — W2 unit suite (MISSION-CRF-AVR-008).

Maps the owner directive's Phase-13 test items 1-15 onto the additive layer:

  1. raw compatibility (byte-identical absent-request shape, both tools, both modes)
  2. grid coordinate correctness (lines/labels at expected px, closed density enum)
  3. crop coordinate correctness (crop_origin is metadata, never baked in)
  4. scale/DPI correctness (metadata reported; nothing pre-scales; stock F1 transform)
  5. OCR coordinate correctness (regions round-trip unchanged)
  6. OCR caching (OCR-once per observation; no cross-observation hit)
  7. same observation -> multiple views -> one computation
  8. observation identity preservation (id/digest/fields; cache invisible to dumps)
  9. stale observation rejection (queue premise unchanged with views off AND on)
  10. representation request validation (casefold; typed errors; never silent raw)
  11. invalid representation handling (typed view_derive_failed; canonical path usable)
  12. existing safety behavior (safety/queue shapes unchanged with the layer in flight)
  13. existing grounding behavior (text-anchor fail-closed holds; spatial_text never
      fires a grounding strategy)
  14. existing action behavior (stock markers intact; execute-path visual_view)
  15. performance regression thresholds (loose fake-path sanity guard only; the REAL
      bars freeze at EXP-020 close (W3) and are asserted at EXP-021 (W4) — placeholder
      skip below)

Every cross-tree byte claim (H19 falsifier (a)) is additionally proven by the committed
EXP-021-prep byte-identity driver against the pristine pinned tree; these tests pin the
shape/behavior contract inside this repo.
"""

from __future__ import annotations

import base64
import io
import json
import statistics
import time
from typing import Any

import pytest
from PIL import Image, ImageDraw

from computer_use_mcp import server
from computer_use_mcp.models import CoordinateSpace, MonitorInfo, Observation, TextRegion
from computer_use_mcp.observation import observation_digest
from computer_use_mcp.state import SessionRegistry
from computer_use_mcp.visual_views import (
    DEFAULT_GRID_DENSITY,
    GRID_DENSITIES,
    LABEL_COLOR,
    LABEL_OFFSET_PX,
    MAJOR_COLOR,
    MINOR_COLOR,
    PIXEL_EVIDENCE_SIGMA_HI,
    PIXEL_EVIDENCE_SIGMA_LO,
    InvalidVisualViewError,
    GridOverlayProvider,
    RawPassThroughProvider,
    encode_png_b64,
    label_anchors,
    label_text,
    pixel_evidence_for,
    render_crop_grid,
    render_grid,
    resolve_density,
    resolve_view,
    spatial_text_of,
)
from computer_use_mcp.visual_views import SPATIAL_TEXT_CAP
import test_controller_integration as tci

FAST_LIMITS = {"min_screenshot_interval_ms": 0}

#: The stock observe metadata key sets (the pre-AL-002 shapes; H19 pins these exactly).
STOCK_IMAGE_KEYS = [
    "observation",
    "digest",
    "observation_id",
    "active_app",
    "image_format",
    "text_summary",
]
STOCK_TEXT_KEYS = [
    "observation",
    "digest",
    "observation_id",
    "active_app",
    "image_format",
    "image_delivery",
    "image_delivery_note",
    "text_summary",
]
#: The stock Observation serialized keys (private stash/cache attrs never appear — I-3).
STOCK_OBSERVATION_KEYS = [
    "width",
    "height",
    "active_window",
    "cursor_x",
    "cursor_y",
    "input_width",
    "input_height",
    "coordinate_scale_x",
    "coordinate_scale_y",
    "coordinate_space_verified",
    "redactions_applied",
    "observation_id",
    "timestamp",
    "coordinate_space",
    "monitor",
    "active_window_info",
    "ocr_text",
    "ui_elements",
]


# --- fakes ------------------------------------------------------------------------------------


class SameObservationBackend(tci.ScriptedBackend):
    """Returns the SAME Observation object on every observe() call.

    Simulates the payload-cache world (the R-5 one-entry cache semantics) where
    consecutive tool calls serve one observation — the tool-surface shape the OCR-once
    invariant (H21) needs: N view requests of one Observation, exactly 1 computation.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fixed: Observation | None = None

    def observe(self) -> Any:
        if self._fixed is None:
            self._fixed = super().observe()
        return self._fixed


class FreshObservationBackend(tci.ScriptedBackend):
    """One fixed (region-carrying) observation, then FRESH captures (no-cross-hit test)."""

    def __init__(self, fixed_regions: list[TextRegion] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fixed_regions = fixed_regions
        self._fixed: Any = None
        self._gone_fixed = False

    def observe(self) -> Any:
        if not self._gone_fixed:
            observation = tci.ScriptedBackend.observe(self)
            if self._fixed_regions is not None:
                observation.ocr_text = list(self._fixed_regions)
            self._fixed = observation
            self._gone_fixed = True
            return observation
        observation = tci.ScriptedBackend.observe(self)  # a NEW observation, own regions
        observation.ocr_text = [TextRegion(text="new-screen", x=1, y=2, width=3, height=4)]
        return observation


class CorruptPayloadBackend(tci.ScriptedBackend):
    """Serves an undecodable payload with no frame stash (fail-closed derive input)."""

    def observe(self) -> Any:
        observation = super().observe()
        observation.image_base64 = "!!!not-a-payload!!!"
        return observation


class ShiftingScreenBackend(tci.ScriptedBackend):
    """Every observe() call returns a DIFFERENT image (the Phase-10 screen-churn fake)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._observe_count = 0

    def observe(self) -> Any:
        self._observe_count += 1
        observation = super().observe()
        image = Image.new("RGB", (64, 48), ("white", "red", "blue")[self._observe_count % 3])
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        observation.image_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
        return observation


def _png_b64(color: str = "white", size: tuple[int, int] = (64, 48)) -> str:
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _region_entries(block: dict[str, Any]) -> list[dict[str, Any]]:
    return block["regions"]


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh bounded registry/bundles + per-test audit dir (full session isolation)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    monkeypatch.setattr(server, "_stopped_sessions", {})
    return server


def make_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    backend: Any = None,
    provider: Any = None,
    **start_kwargs: Any,
) -> tuple[str, Any, Any]:
    """Start one session through the tool with injected fake backend/provider."""
    backend = backend if backend is not None else tci.ScriptedBackend()
    provider = provider if provider is not None else tci.ScriptedProvider([])
    monkeypatch.setattr(server, "_backend_factory", lambda: backend)
    monkeypatch.setattr(server, "_provider_factory", lambda: provider)
    response = server.start_session(
        dry_run=False, require_approval=False, limits=FAST_LIMITS, **start_kwargs
    )
    assert response.get("session_id"), response
    session_id = str(response["session_id"])
    return session_id, server._get_bundle(session_id), backend


def observe_metadata(blocks: Any) -> dict[str, Any]:
    return json.loads(blocks[0].text)


# --- item 1: raw representation compatibility (H19) ---------------------------------------------


def test_item1_absent_request_observe_image_mode_byte_identical_shape(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent visual_view: stock keys ONLY, stock image bytes, no spatial-text work."""
    session_id, bundle, backend = make_session(monkeypatch)
    blocks = server.computer_observe(session_id)
    assert len(blocks) == 2
    metadata = observe_metadata(blocks)
    assert list(metadata.keys()) == STOCK_IMAGE_KEYS
    assert list(metadata["observation"].keys()) == STOCK_OBSERVATION_KEYS
    observation = backend.observed[-1]
    expected_b64, expected_mime = server._bound_outbound_image(
        observation.image_base64, frame=getattr(observation, "_frame", None)
    )
    assert blocks[1].data == expected_b64  # stock outbound bytes, untouched
    assert blocks[1].mimeType == expected_mime
    counters = bundle.metrics.snapshot()["counters"]
    assert counters.get("spatial_text_compute", 0) == 0
    latencies = bundle.metrics.snapshot()["latencies"]
    assert latencies.get("view_derive_ms", {"count": 0})["count"] == 0


def test_item1_absent_request_observe_text_mode_byte_identical_shape(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent visual_view in text mode: the stock single-text-block shape exactly."""
    session_id, bundle, _backend = make_session(monkeypatch, image_delivery="text")
    blocks = server.computer_observe(session_id)
    assert len(blocks) == 1
    metadata = observe_metadata(blocks)
    assert list(metadata.keys()) == STOCK_TEXT_KEYS
    assert metadata["image_format"] == "none"
    counters = bundle.metrics.snapshot()["counters"]
    assert counters.get("spatial_text_compute", 0) == 0


async def test_item1_absent_request_execute_byte_identical_shape(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent visual_view on execute: stock result payload (no view keys), stock image."""
    session_id, bundle, _backend = make_session(monkeypatch)
    response = await server.computer_execute(session_id, "click", x=10, y=10)
    payload = tci.execute_payload(response)
    assert payload["ok"] is True
    assert "visual_view" not in payload
    assert "visual_view_scale" not in payload
    assert "spatial_text" not in payload
    assert payload["image_scale"] == 0.5  # the stock half-res default marker, untouched
    counters = bundle.metrics.snapshot()["counters"]
    assert counters.get("spatial_text_compute", 0) == 0


def test_item1_explicit_raw_serves_stock_bytes_plus_additive_keys(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit raw: SAME outbound bytes as the absent request + the additive keys
    (degraded UIA read: spatial_text null + the availability marker)."""
    session_id, _bundle, _backend = make_session(monkeypatch)
    absent = server.computer_observe(session_id)
    absent_meta = observe_metadata(absent)
    explicit = server.computer_observe(session_id, visual_view="raw")
    raw_meta = observe_metadata(explicit)
    # same image bytes (a fresh fake capture is pixel-identical, so the payload matches)
    assert explicit[1].data == absent[1].data
    assert explicit[1].mimeType == absent[1].mimeType
    # additive keys ONLY: stock keys unchanged and in order, then the additions
    assert list(raw_meta.keys()) == STOCK_IMAGE_KEYS + [
        "visual_view",
        "visual_view_scale",
        "spatial_text",
        "spatial_text_available",  # degraded UIA read (ScriptedBackend carries no regions)
    ]
    assert raw_meta["visual_view"] == "raw"
    assert raw_meta["visual_view_scale"] is None  # raw adds no view transform
    assert raw_meta["spatial_text"] is None
    assert raw_meta["spatial_text_available"] is False
    assert list(raw_meta["observation"].keys()) == list(absent_meta["observation"].keys())


# --- item 2: grid coordinate correctness ---------------------------------------------------------


def _grid_observation(size: tuple[int, int] = (256, 256)) -> Observation:
    frame = Image.new("RGB", size, "white")
    return Observation(
        image_base64=encode_png_b64(frame),
        width=size[0],
        height=size[1],
    )  # type: ignore[arg-type]


def test_item2_grid_lines_at_expected_pixels_and_labels_screenshot_space() -> None:
    """Lines land at the enum's px positions; labels show screenshot-space anchors.

    Renders at the SERVED default density (W2.1: coarse — minor every 128 px, major
    every 512 px; on a 256x256 frame the only major line is x/y == 0)."""
    derived = render_grid(_grid_observation((256, 256)))
    assert derived.produced_by == "grid:decode"
    image = derived.image
    assert image is not None and image.size == (256, 256)
    pixels = image.load()
    assert pixels is not None
    assert pixels[128, 32] == MINOR_COLOR  # vertical minor line x=128
    assert pixels[32, 128] == MINOR_COLOR  # horizontal minor line y=128
    assert pixels[1, 1] == MAJOR_COLOR  # major line at 0, 2 px wide
    assert pixels[64, 64] == (255, 255, 255)  # off-line background untouched (coarse: no line at 64)
    # the label at anchor (0, 0) + the 3 px offset paints reddish (label) pixels nearby
    # (antialias-tolerant: any pixel visibly redder than the gray palette / white bg)
    label_zone = [pixels[x, y] for x in range(3, 24) for y in range(3, 14)]
    assert any(p[0] > p[1] + 30 and p[0] > p[2] + 30 for p in label_zone)
    # labels are pure screenshot-space functions of the anchor: no crop origin, no scale
    assert label_text(256, 512) == "256,512"
    assert label_anchors(256, 256, 64, 4) == [(0, 0)]


def test_item2_grid_density_enum_bounds_label_count() -> None:
    """Closed density enum (I-10): deterministic, bounded, monotone label counts.

    W2.1 (Commander adjudication of the EXP-020 variant gate): the SERVED default is
    ``coarse`` with full-coordinate labels; all three densities stay callable."""
    assert sorted(GRID_DENSITIES) == ["coarse", "fine", "standard"]
    assert GRID_DENSITIES == {"coarse": (128, 4), "standard": (64, 4), "fine": (32, 4)}
    assert DEFAULT_GRID_DENSITY == "coarse"  # the W2.1 adjudicated default
    counts = {
        name: len(label_anchors(1920, 1080, minor, major))
        for name, (minor, major) in GRID_DENSITIES.items()
    }
    assert counts["coarse"] < counts["standard"] < counts["fine"]
    assert counts["fine"] == 15 * 9  # 1920/128 x 1080/128 major intersections, bounded
    with pytest.raises(ValueError):
        render_grid(_grid_observation(), density="free-integer-8")


def test_item2_stash_is_consumed_and_never_mutated() -> None:
    """I-4/I-5: the stashed frame is consumed (no decode) and never mutated."""
    frame = Image.new("RGB", (64, 64), "white")
    observation = Observation(image_base64=_png_b64(size=(64, 64)), width=64, height=64)
    observation._frame = frame
    stash_before = frame.tobytes()
    derived = render_grid(observation)
    assert derived.produced_by == "grid:stash"
    assert derived.image is not None and derived.image is not frame  # a copy, not the stash
    assert frame.tobytes() == stash_before  # the canonical stash is untouched
    assert derived.image.getpixel((0, 0)) == MAJOR_COLOR  # the copy carries the grid


# --- item 3: crop coordinate correctness ---------------------------------------------------------


def test_item3_crop_origin_is_metadata_not_baked_into_labels_or_regions() -> None:
    """crop_origin ships as block provenance; labels/regions stay screenshot-local."""
    regions = [TextRegion(text="Save", x=12, y=34, width=50, height=16)]
    observation = Observation(
        image_base64=_png_b64(size=(64, 64)),
        width=64,
        height=64,
        monitor=MonitorInfo(id="m", index=0, bounds=(100, 200, 640, 480), is_primary=True),
        ocr_text=regions,
    )
    block = spatial_text_of(observation, pixel_evidence=True)
    assert block is not None
    assert block["crop_origin"] == [100, 200]
    # A1: the region dict gains the measured ``pixel_evidence`` field (and the block
    # the sigma constants); the coordinates themselves stay screenshot-local, unwritten.
    # A2: the evidence field is OPT-IN — this is the explicit-on form of the pin.
    assert _region_entries(block) == [
        {**regions[0].model_dump(), "pixel_evidence": block["regions"][0]["pixel_evidence"]}
    ]
    assert block["pixel_evidence_sigma"] == [
        PIXEL_EVIDENCE_SIGMA_LO,
        PIXEL_EVIDENCE_SIGMA_HI,
    ]  # the EXP-020.1-frozen constants, reported as block provenance
    # the rendered grid NEVER reads the crop origin: identical pixels without it
    with_origin = render_grid(observation)
    plain = render_grid(_grid_observation((64, 64)))
    assert with_origin.image is not None and plain.image is not None
    assert with_origin.image.tobytes() == plain.image.tobytes()
    assert label_anchors(640, 480, *GRID_DENSITIES[DEFAULT_GRID_DENSITY]) == label_anchors(
        640, 480, *GRID_DENSITIES[DEFAULT_GRID_DENSITY]
    )


# --- item 4: scale/DPI correctness ---------------------------------------------------------------


def test_item4_scale_metadata_reported_no_prescale_of_labels() -> None:
    """SCALED space: the block reports the recorded scale; nothing pre-scales labels."""
    observation = Observation(
        image_base64=_png_b64(size=(64, 64)),
        width=64,
        height=64,
        coordinate_scale_x=1.5,
        coordinate_scale_y=1.5,
        coordinate_space="scaled",
        ocr_text=[TextRegion(text="zoomed", x=8, y=8, width=20, height=10)],
    )
    block = spatial_text_of(observation)
    assert block is not None
    assert block["coordinate_space"] == "scaled"
    assert block["coordinate_scale"] == [1.5, 1.5]
    assert _region_entries(block)[0]["x"] == 8  # screenshot-local, never re-scaled
    # grid output is independent of the recorded scale (labels are screenshot-space)
    scaled = render_grid(observation)
    unscaled = render_grid(_grid_observation((64, 64)))
    assert scaled.image is not None and unscaled.image is not None
    assert scaled.image.tobytes() == unscaled.image.tobytes()


async def test_item4_click_grounded_on_grid_view_executes_through_stock_transform(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: a click grounded on a GRID-view observation goes through the ONE stock
    transform (origin + screenshot*scale) — never a re-scale (H20 falsifier (c))."""
    from computer_use_mcp.backend import FakeComputerBackend

    backend = FakeComputerBackend(
        640,
        480,
        monitors=[
            MonitorInfo(
                id="m", index=0, bounds=(100, 200, 1280, 960), is_primary=True,
                dpi_scale_x=2.0, dpi_scale_y=2.0,
            )
        ],
    )
    session_id, _bundle, _ = make_session(monkeypatch, backend=backend)
    blocks = server.computer_observe(session_id, visual_view="grid")  # GRID view in flight
    assert observe_metadata(blocks)["visual_view"] == "grid"
    response = await server.computer_execute(session_id, "click", x=100, y=50)
    payload = tci.execute_payload(response)
    assert backend.executed, "the grounded action executed (the click dispatched)"
    assert payload["action"]["grounding"]["strategy"] == "coordinate"
    # physical = monitor origin (100, 200) + screenshot (100, 50) * scale 2.0 = (300, 300)
    assert backend._cursor_physical == (300.0, 300.0)
    # and NOT the double-scaled point (origin + screenshot * scale**2)
    assert backend._cursor_physical != (500.0, 500.0)
    assert backend.executed[0].point is not None
    assert (backend.executed[0].point.x, backend.executed[0].point.y) == (100, 50)


# --- item 5: OCR coordinate correctness ----------------------------------------------------------


def test_item5_ocr_regions_round_trip_unchanged() -> None:
    """Injected TextRegions round-trip into the block verbatim (confidence=None passthrough)."""
    regions = [
        TextRegion(text="File", x=0, y=0, width=30, height=14),
        TextRegion(text="编辑", x=120, y=48, width=44, height=16, confidence=0.95),
        TextRegion(text="no-confidence", x=7, y=70, width=90, height=12, confidence=None),
    ]
    observation = Observation(image_base64=_png_b64(), width=1280, height=720, ocr_text=regions)
    block = spatial_text_of(observation, pixel_evidence=True)
    assert block is not None
    assert block["region_count"] == 3
    assert block["omitted_count"] == 0
    # A1: each region dict = the verbatim TextRegion dump + the measured
    # ``pixel_evidence`` field (the coordinates/confidence never rewritten).
    # A2: the evidence field is OPT-IN — this is the explicit-on form of the pin.
    assert _region_entries(block) == [
        {**region.model_dump(), "pixel_evidence": entry["pixel_evidence"]}
        for region, entry in zip(regions, _region_entries(block))
    ]
    assert _region_entries(block)[2]["confidence"] is None  # reality, never an invented 0.0


# --- item 6: OCR caching (H21) -------------------------------------------------------------------


def test_item6_ocr_once_same_observation_two_view_requests(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two explicit view requests on ONE observation: exactly 1 computation, tool-level."""
    session_id, bundle, backend = make_session(monkeypatch, backend=SameObservationBackend())
    first = observe_metadata(server.computer_observe(session_id, visual_view="raw"))
    second = observe_metadata(server.computer_observe(session_id, visual_view="grid"))
    observation = backend.observed[0]
    assert observation._spatial_text_compute_count == 1  # H21: 1 for N>=2 views
    assert bundle.metrics.snapshot()["counters"]["spatial_text_compute"] == 1
    assert first["spatial_text"] == second["spatial_text"]  # the SAME block, reused
    assert first["observation_id"] == second["observation_id"]


def test_item6_second_observation_gets_own_computation_no_cross_hit(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new observation computes its own block; the first keeps its own text."""
    backend = FreshObservationBackend(
        fixed_regions=[TextRegion(text="old-screen", x=5, y=5, width=9, height=9)]
    )
    session_id, bundle, _ = make_session(monkeypatch, backend=backend)
    first = observe_metadata(server.computer_observe(session_id, visual_view="grid"))
    second = observe_metadata(server.computer_observe(session_id, visual_view="grid"))
    assert first["observation_id"] != second["observation_id"]
    assert first["spatial_text"]["regions"][0]["text"] == "old-screen"
    assert second["spatial_text"]["regions"][0]["text"] == "new-screen"  # no foreign text
    assert bundle.metrics.snapshot()["counters"]["spatial_text_compute"] == 2


# --- item 7: same observation -> multiple views -> one computation -------------------------------


def test_item7_raw_grid_raw_same_observation_one_computation(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """raw -> grid -> raw on one observation: the computation count stays 1."""
    session_id, bundle, backend = make_session(monkeypatch, backend=SameObservationBackend())
    for view in ("raw", "grid", "raw"):
        blocks = server.computer_observe(session_id, visual_view=view)
        assert observe_metadata(blocks)["visual_view"] == view
    assert backend.observed[0]._spatial_text_compute_count == 1
    assert bundle.metrics.snapshot()["counters"]["spatial_text_compute"] == 1
    # direct-layer idempotence: repeated spatial_text_of calls never recompute
    observation = backend.observed[0]
    before = observation._spatial_text_compute_count
    assert spatial_text_of(observation) is spatial_text_of(observation)
    assert observation._spatial_text_compute_count == before


# --- item 8: observation identity preservation ---------------------------------------------------


def test_item8_observation_identity_and_digest_unchanged_by_view_requests(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """View requests never mutate the canonical observation (I-5) nor its digest."""
    backend = SameObservationBackend()
    session_id, _bundle, _ = make_session(monkeypatch, backend=backend)
    _blocks = server.computer_observe(session_id)  # absent request
    observation = backend.observed[0]
    baseline = {
        "observation_id": observation.observation_id,
        "digest": observation_digest(observation),
        "image": observation.image_base64,
        "width": observation.width,
        "height": observation.height,
        "ocr": observation.ocr_text,
    }
    for view in ("raw", "grid", "raw"):
        server.computer_observe(session_id, visual_view=view)
    assert observation.observation_id == baseline["observation_id"]
    assert observation_digest(observation) == baseline["digest"]  # digest over image_base64
    assert observation.image_base64 == baseline["image"]
    assert (observation.width, observation.height) == (baseline["width"], baseline["height"])
    assert observation.ocr_text == baseline["ocr"]


def test_item8_cache_invisible_to_serialization() -> None:
    """I-3: the OCR-once cache never appears in any serialization (the _frame property)."""
    observation = Observation(image_base64=_png_b64(), width=64, height=64)
    observation._frame = Image.new("RGB", (64, 64), "white")
    spatial_text_of(observation)
    dump = observation.model_dump(mode="json", exclude={"image_base64"})
    assert list(dump.keys()) == STOCK_OBSERVATION_KEYS  # exactly the stock fields
    assert "_spatial_text" not in dump and "_spatial_text_derived" not in dump
    assert "_frame" not in dump
    assert observation_digest(observation) == observation_digest(
        Observation.model_construct(image_base64=observation.image_base64)
    )  # the digest reads ONLY image_base64


# --- item 9: stale observation rejection (H20 / Phase-10 at unit level) --------------------------


async def _queue_scenario(backend: Any) -> tuple[Any, list[Any], int]:
    """One queued click after a screen change: (response payload, item kinds, executed)."""
    server._registry = SessionRegistry(max_sessions=8)
    server._bundles = {}
    server._stopped_sessions = {}
    server._backend_factory = lambda: backend
    server._provider_factory = lambda: tci.ScriptedProvider([])
    start = server.start_session(dry_run=False, require_approval=False, limits=FAST_LIMITS)
    session_id = str(start["session_id"])
    response = await server.computer_execute(
        session_id, "click", x=10, y=10, follow_ups=[{"action": "click", "x": 20, "y": 20}]
    )
    payload = tci.execute_payload(response)
    kinds = [entry.get("kind") for entry in payload.get("follow_up_results", [])]
    executed = len(backend.executed)
    return payload, kinds, executed


async def test_item9_stale_queue_rejection_identical_without_and_with_grid_view(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase-10 (unit level): the O123->O124 queue stop behaves IDENTICALLY with the
    view layer flags-off AND with an explicit grid request in flight."""
    backend_off = ShiftingScreenBackend()
    payload_off, kinds_off, executed_off = await _queue_scenario(backend_off)

    backend_grid = ShiftingScreenBackend()
    # premise O123 in hand; an EXPLICIT grid view request rides the observation, the
    # screen then changes and O124 exists when the queued item re-grounds.
    server._registry = SessionRegistry(max_sessions=8)
    server._bundles = {}
    server._stopped_sessions = {}
    server._backend_factory = lambda: backend_grid
    server._provider_factory = lambda: tci.ScriptedProvider([])
    start = server.start_session(dry_run=False, require_approval=False, limits=FAST_LIMITS)
    grid_blocks = server.computer_observe(str(start["session_id"]), visual_view="grid")
    grid_meta = observe_metadata(grid_blocks)
    assert grid_meta["visual_view"] == "grid"
    premise_id = grid_meta["observation_id"]
    payload_grid, kinds_grid, executed_grid = await _queue_scenario(backend_grid)

    # the stale premise never re-serves: the queue's fresh captures are new observations
    assert premise_id != backend_grid.observed[-1].observation_id
    # BYTE-IDENTICAL gate/stop behavior, flags-off AND flags-on (H20):
    assert payload_grid["follow_ups_stopped_reason"] == payload_off["follow_ups_stopped_reason"]
    assert payload_grid["follow_ups_stopped_reason"] is not None  # a true stop happened
    assert kinds_grid == kinds_off
    assert executed_grid == executed_off == 1  # the queued action never ran on O124


# --- item 10: representation request validation --------------------------------------------------


def test_item10_view_request_validation_casefold_and_typed_errors(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'GRID' resolves (casefold); unknown/empty are typed errors naming the views."""
    session_id, _bundle, _ = make_session(monkeypatch)
    blocks = server.computer_observe(session_id, visual_view="GRID")
    assert observe_metadata(blocks)["visual_view"] == "grid"  # casefold resolves
    for bad in ("3d", ""):
        result = server.computer_observe(session_id, visual_view=bad)
        assert result["ok"] is False
        assert result["error"] == "invalid_visual_view"
        assert "'grid'" in result["message"] and "'raw'" in result["message"]  # teaches views
    # layer level: same fail-closed semantics
    assert resolve_view(" Raw ").id == "raw"
    with pytest.raises(InvalidVisualViewError):
        resolve_view("3d")
    with pytest.raises(InvalidVisualViewError):
        resolve_view("")


async def test_item10_invalid_view_on_execute_never_dispatches(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalid view name on execute fails BEFORE anything runs — no input dispatched."""
    session_id, _bundle, backend = make_session(monkeypatch)
    result = await server.computer_execute(session_id, "click", x=10, y=10, visual_view="3d")
    assert result["ok"] is False
    assert result["error"] == "invalid_visual_view"
    assert backend.executed == []  # nothing dispatched, fail-closed


# --- item 11: invalid representation handling ----------------------------------------------------


def test_item11_derive_failure_typed_error_and_canonical_path_still_usable(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupt payload + no frame stash -> typed view_derive_failed, no raw substitute;
    the canonical observe path keeps working afterwards."""
    session_id, _bundle, _ = make_session(monkeypatch, backend=CorruptPayloadBackend())
    result = server.computer_observe(session_id, visual_view="grid")
    assert result["ok"] is False
    assert result["error"] == "view_derive_failed"
    assert "undecodable" in result["message"]
    # NOT a silent raw substitution: no content blocks were served under the error
    assert not isinstance(result, list)
    # the canonical path remains usable (the layer can never break stock observe)
    blocks = server.computer_observe(session_id)
    assert len(blocks) == 2
    assert blocks[1].mimeType == "image/png"  # the stock degradation served the payload


async def test_item11_execute_path_derive_failure_keeps_executed_result(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Execute-path backstop: a derive failure NEVER hides the executed result and
    NEVER substitutes raw — the typed marker rides, no image block ships."""
    from computer_use_mcp.visual_views import ViewDeriveError

    session_id, _bundle, backend = make_session(monkeypatch)

    def _explode(self: Any, observation: Any) -> Any:
        raise ViewDeriveError("synthetic derive failure")

    monkeypatch.setattr(GridOverlayProvider, "derive", _explode)
    response = await server.computer_execute(session_id, "click", x=10, y=10, visual_view="grid")
    payload = tci.execute_payload(response)
    assert payload["ok"] is True  # the action RAN; its outcome is not hidden
    assert backend.executed, "the action physically dispatched"
    assert "visual_view_error" in payload
    assert payload["visual_view_error"].startswith("view_derive_failed")
    assert payload["visual_view"] == "grid"
    assert isinstance(response, dict)  # no image block, and no silent raw bytes


# --- item 12: existing safety behavior -----------------------------------------------------------


async def test_item12_safety_and_queue_shapes_unchanged_with_view_layer(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safety denial and queue shapes are untouched by explicit view requests."""
    backend = tci.ScriptedBackend()
    backend.set_input_blocked(True)
    session_id, _bundle, _ = make_session(monkeypatch, backend=backend)
    server.computer_observe(session_id, visual_view="grid")  # the layer is in flight
    denied = await server.computer_execute(session_id, "click", x=5, y=5, visual_view="grid")
    assert denied["ok"] is False
    assert denied["message"]  # the stock safety denial shape (plain dict, no image)
    assert "screenshot_after_base64" not in denied

    backend.set_input_blocked(False)
    queued = await server.computer_execute(
        session_id,
        "click",
        x=10,
        y=10,
        follow_ups=[{"action": "click", "x": 20, "y": 20}],
        visual_view="grid",
    )
    payload = tci.execute_payload(queued)
    assert payload["ok"] is True
    assert payload["follow_ups_stopped_reason"] is None  # the batch ran to completion
    assert len(payload["follow_up_results"]) == 2  # both items audited per item
    assert len(backend.executed) == 2


# --- item 13: existing grounding behavior --------------------------------------------------------


async def test_item13_grounding_noninterference_text_anchor_fail_closed_holds(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spatial_text block NEVER makes grounding strategies fire: with a grid view
    (and its attached text) in flight, a point action still grounds via the coordinate
    strategy, and text-anchor's fail-closed contract is untouched."""
    from computer_use_mcp.grounding import TextAnchorGroundingStrategy, UnsupportedGroundingError

    # ocr_text ABSENT: the observe response ships spatial_text: null + the marker
    session_id, _bundle, _ = make_session(monkeypatch)
    blocks = server.computer_observe(session_id, visual_view="raw")
    metadata = observe_metadata(blocks)
    assert metadata["spatial_text"] is None
    assert metadata["spatial_text_available"] is False
    response = await server.computer_execute(session_id, "click", x=30, y=40, visual_view="grid")
    payload = tci.execute_payload(response)
    assert payload["ok"] is True
    assert payload["action"]["grounding"]["strategy"] == "coordinate"  # none fired by text

    # direct layer: text-anchor stays fail-closed on absent ocr_text
    action = _grounded_click()
    with pytest.raises(UnsupportedGroundingError):
        TextAnchorGroundingStrategy().ground(
            action, Observation(image_base64=_png_b64(), width=64, height=64), target="anything"
        )

    # ocr_text PRESENT (regions served in the block): a point action STILL coordinates
    session_id2, _bundle2, _ = make_session(
        monkeypatch,
        backend=FreshObservationBackend(
            fixed_regions=[TextRegion(text="label", x=0, y=0, width=10, height=10)]
        ),
    )
    blocks2 = server.computer_observe(session_id2, visual_view="grid")
    block = observe_metadata(blocks2)["spatial_text"]
    assert block is not None and block["region_count"] == 1  # the block is attached
    response2 = await server.computer_execute(session_id2, "click", x=30, y=40)
    payload2 = tci.execute_payload(response2)
    assert payload2["action"]["grounding"]["strategy"] == "coordinate"  # metadata never routes


def _grounded_click() -> Any:
    from computer_use_mcp.models import ActionType, GroundedAction

    return GroundedAction(action=ActionType.CLICK, confidence=1.0)


# --- item 14: existing action behavior + execute-path visual_view --------------------------------


async def test_item14_stock_action_markers_intact_with_views_absent(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stock action behavior with the layer absent: half-res marker, opt-out, shapes."""
    session_id, _bundle, _ = make_session(monkeypatch)
    default = tci.execute_payload(
        await server.computer_execute(session_id, "click", x=10, y=10)
    )
    assert default["ok"] is True and default["image_scale"] == 0.5  # stock default image
    assert "visual_view" not in default
    opt_out = tci.execute_payload(
        await server.computer_execute(session_id, "click", x=10, y=10,
                                      include_screenshot_after=False)
    )
    assert "screenshot_after_base64" not in opt_out  # stock opt-out untouched
    assert "image_format" not in opt_out


async def test_item14_execute_path_visual_view_grid(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Execute path: an explicit grid request derives the view ONCE and honors it on
    the default (half-res) returned image, with the honest scale reported."""
    session_id, bundle, _ = make_session(monkeypatch)
    response = await server.computer_execute(
        session_id, "click", x=20, y=20, visual_view="grid"
    )
    payload = tci.execute_payload(response)
    assert payload["ok"] is True
    assert payload["visual_view"] == "grid"
    assert payload["visual_view_scale"] == 0.5  # the stock half-res policy, reported
    assert payload["image_scale"] == 0.5  # the stock marker coexists
    assert payload["image_format"] == "image/jpeg"
    assert len(response) == 2  # TextContent + the GRID ImageContent
    served = Image.open(io.BytesIO(base64.b64decode(response[1].data)))
    assert served.size == (32, 24)  # half of the derived 64x48 grid view
    assert bundle.metrics.snapshot()["latencies"]["view_derive_ms"]["count"] == 1
    # the served grid bytes differ from a STOCK payload's bytes (a real view, not raw):
    # identical non-flipping captures make the stock and grid images comparable
    session_id2, _bundle2, _ = make_session(
        monkeypatch, backend=tci.ScriptedBackend(flip=False)
    )
    stock_response = await server.computer_execute(session_id2, "click", x=20, y=20)
    grid_response = await server.computer_execute(
        session_id2, "click", x=20, y=20, visual_view="grid"
    )
    assert grid_response[1].data != stock_response[1].data  # not a silent raw substitute


async def test_item14_execute_path_visual_view_raw_same_bytes(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Execute path: explicit raw ships the SAME image bytes as the absent request."""
    session_id, _bundle, _ = make_session(monkeypatch, backend=tci.ScriptedBackend(flip=False))
    absent = await server.computer_execute(session_id, "click", x=10, y=10)
    explicit = await server.computer_execute(session_id, "click", x=10, y=10, visual_view="raw")
    again = await server.computer_execute(session_id, "click", x=10, y=10)
    assert tci.execute_payload(explicit)["visual_view"] == "raw"
    assert tci.execute_payload(explicit)["visual_view_scale"] is None
    # identical stock image BLOCK bytes on every execute (a non-flipping fake captures
    # identical pixels, and the half-res encode is a pure function of them)
    assert len(absent) == len(explicit) == len(again) == 2
    assert explicit[1].data == absent[1].data == again[1].data
    assert explicit[1].mimeType == absent[1].mimeType


# --- item 15: performance regression thresholds --------------------------------------------------


def test_item15_absent_request_latency_sanity_loose_guard(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOOSE sanity guard only: the absent-request observe path stays in the recorded
    fake-path band (W1 EXP-019 a2b mean 11.42 ms; a 20 ms ceiling absorbs CI jitter
    while still catching accidental view/encode work on the default path) and the
    default path performs ZERO spatial-text work. No cost claim is made — the measured
    value is recorded, and the REAL bars freeze at EXP-020 (W3) for EXP-021 (W4)."""
    session_id, bundle, _ = make_session(monkeypatch)
    server.computer_observe(session_id)  # priming rep (declared, untimed)
    samples = []
    for _ in range(30):
        clock = time.perf_counter()
        server.computer_observe(session_id)
        samples.append((time.perf_counter() - clock) * 1000.0)
    mean_ms = statistics.mean(samples)
    assert mean_ms < 20.0, f"absent-request observe mean {mean_ms:.2f} ms exceeded the loose band"
    counters = bundle.metrics.snapshot()["counters"]
    assert counters.get("spatial_text_compute", 0) == 0
    latencies = bundle.metrics.snapshot()["latencies"]
    assert latencies.get("view_derive_ms", {"count": 0})["count"] == 0


@pytest.mark.skip(reason="bars freeze at EXP-020 close (W3); enabled at EXP-021 (W4)")
def test_item15_real_bars_frozen_at_exp020() -> None:
    """Placeholder so the real-bar slot exists: H19 falsifier (c) medians/p95s and the
    H22 grid/text cost bars (<T:3..5>) are asserted here once EXP-020 freezes them."""


# --- text-mode view request + honest degradation -------------------------------------------------


def test_text_mode_view_request_keys_ride_single_text_block(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit view in text mode: metadata keys ride the ONE text block; image_format
    stays 'none'; absent request stays byte-identical."""
    backend = SameObservationBackend()
    backend.observe()  # pre-create the fixed observation and give it regions
    assert backend._fixed is not None
    backend._fixed.ocr_text = [TextRegion(text="meta", x=2, y=3, width=4, height=5)]
    session_id, bundle, _ = make_session(monkeypatch, backend=backend, image_delivery="text")
    absent = server.computer_observe(session_id)
    assert list(observe_metadata(absent).keys()) == STOCK_TEXT_KEYS
    blocks = server.computer_observe(session_id, visual_view="grid")
    assert len(blocks) == 1  # never an image block in text mode
    metadata = observe_metadata(blocks)
    assert list(metadata.keys()) == STOCK_TEXT_KEYS + [
        "visual_view",
        "visual_view_scale",
        "spatial_text",
    ]
    assert metadata["visual_view"] == "grid"
    assert metadata["visual_view_scale"] is None  # no outbound image, honest by construction
    assert metadata["image_format"] == "none"
    assert metadata["spatial_text"]["regions"][0]["text"] == "meta"
    assert "image_delivery" in metadata
    latencies = bundle.metrics.snapshot()["latencies"]
    assert latencies.get("view_derive_ms", {"count": 0})["count"] == 0  # no pixels derived


def test_spatial_text_degradation_marker_and_truthful_empty(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ocr_text None -> spatial_text null + spatial_text_available: false; an empty list
    is a truthful region_count-0 block (NOT degraded). The memo also proves I-2: the
    degraded result is computed ONCE — later field mutation cannot recompute it."""
    backend = SameObservationBackend()
    session_id, bundle, _ = make_session(monkeypatch, backend=backend)
    degraded = observe_metadata(server.computer_observe(session_id, visual_view="raw"))
    assert degraded["spatial_text"] is None
    assert degraded["spatial_text_available"] is False  # the AL-002 truth marker
    assert backend._fixed is not None
    backend._fixed.ocr_text = []  # a later mutation can NEVER re-open the memo (I-2)
    still_degraded = observe_metadata(server.computer_observe(session_id, visual_view="grid"))
    assert still_degraded["spatial_text"] is None
    assert bundle.metrics.snapshot()["counters"].get("spatial_text_compute", 0) == 1

    # truthful empty on a FRESH observation whose read returned [] before any computation
    backend2 = SameObservationBackend()
    backend2.observe()  # pre-create the fixed observation, then make its read empty
    assert backend2._fixed is not None
    backend2._fixed.ocr_text = []
    session_id2, _bundle2, _ = make_session(monkeypatch, backend=backend2)
    empty = observe_metadata(server.computer_observe(session_id2, visual_view="raw"))
    block = empty["spatial_text"]
    assert block is not None
    assert block["region_count"] == 0
    assert block["omitted_count"] == 0
    assert block["regions"] == []
    assert "spatial_text_available" not in empty


def test_spatial_text_cap_reports_omitted_count() -> None:
    """I-9: regions beyond the observe-metadata cap are dropped WITH omitted_count."""
    regions = [
        TextRegion(text=f"region-{i:02d}", x=i, y=i, width=10, height=10) for i in range(30)
    ]
    observation = Observation(image_base64=_png_b64(), width=1280, height=720, ocr_text=regions)
    block = spatial_text_of(observation, cap=server.OBSERVE_METADATA_ELEMENT_CAP)
    assert block is not None
    assert SPATIAL_TEXT_CAP == server.OBSERVE_METADATA_ELEMENT_CAP  # structural lockstep
    assert block["region_count"] == 20
    assert block["omitted_count"] == 10
    assert len(block["regions"]) == 20


def test_registry_matches_models_constant() -> None:
    """The registry keys and models.VISUAL_VIEWS agree (R2 extensibility contract)."""
    from computer_use_mcp.models import VISUAL_VIEWS

    assert sorted(VISUAL_VIEWS) == ["grid", "raw"]
    assert isinstance(resolve_view("raw"), RawPassThroughProvider)
    assert isinstance(resolve_view("grid"), GridOverlayProvider)
    assert resolve_view("raw").passthrough is True
    assert resolve_view("grid").passthrough is False


def test_visual_view_is_trailing_optional_none_default_on_both_tools() -> None:
    """Regression pin for the documented signature changes (AL-002 Component 4 + A1.2
    + A2): ``visual_view`` (Component 4), ``visual_view_density`` (A1.2), and
    ``pixel_evidence`` (A2, observe/zoom only) join computer_observe /
    computer_execute as trailing optionals with None defaults (None == byte-identical
    legacy behavior). The pre-AL-002 signature pins (test_p5_redteam RT7,
    test_perf004) pin only computer_execute and only through ``visual_view``, so they
    stay untouched by the observe-side additions."""
    import inspect

    observe_params = list(inspect.signature(server.computer_observe).parameters.values())
    assert [p.name for p in observe_params] == [
        "session_id", "visual_view", "visual_view_density", "pixel_evidence",
    ]
    assert all(p.default is None for p in observe_params[1:])
    zoom_params = list(inspect.signature(server.computer_zoom).parameters.values())
    assert [p.name for p in zoom_params] == [
        "session_id", "region", "visual_view", "visual_view_density", "pixel_evidence",
    ]
    assert zoom_params[-1].default is None
    execute_params = list(inspect.signature(server.computer_execute).parameters.values())
    assert execute_params[-1].name == "visual_view"  # A2: execute does NOT take the flag
    assert execute_params[-1].default is None
    assert [p.name for p in execute_params][:12] == [
        "session_id", "action", "x", "y", "text", "keys", "delta", "approved",
        "expected_effect", "x2", "y2", "target",
    ]  # the pre-perf-004 order up front is untouched


# ==============================================================================================
# --- Amendment A1 (owner addendum 2026-09-18): pixel_evidence, density, computer_zoom ----------
# ==============================================================================================


# --- A1 fakes ---------------------------------------------------------------------------------


def _png_b64_image(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _unique_color_frame(width: int, height: int, offset: int = 0) -> Image.Image:
    """A synthetic frame with a distinct color at (almost) every position: any crop
    origin/size error shows up as different pixels (the H26 geometry falsifier)."""
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = ((x + offset) % 256, (y + offset) % 256, (x // 256 + 3 * y + offset) % 256)
    return image


class SyntheticFrameBackend(tci.ScriptedBackend):
    """Serves a per-position-unique-color frame agreeing with the observation's dims.

    The ``_frame`` stash, the payload, and width/height all match (the live-path
    contract), so zoom crops compare pixel-exactly against the backend's frame.
    """

    def __init__(self, width: int = 320, height: int = 240, **kwargs: Any) -> None:
        # width/height go through the base constructor so the default monitor bounds
        # match the screenshot (a post-hoc set_screenshot_size would desync them and
        # classify the space UNVERIFIABLE — the zoom refusal class).
        super().__init__(width=width, height=height, **kwargs)
        self.frame = _unique_color_frame(width, height)

    def observe(self) -> Any:
        observation = super().observe()
        observation.image_base64 = _png_b64_image(self.frame)
        observation._frame = self.frame  # the capture-time stash (live-path shape)
        return observation


class RegionFrameBackend(SyntheticFrameBackend):
    """A flat frame with ONE rendered-text patch + injected UIA regions.

    The text patch makes ``pixel_evidence`` location-discriminating: a region over the
    patch scores > 0, flat areas score 0.0 — so a zoom block computed on the WRONG
    frame space (crop-local instead of full-frame) produces different numbers.
    """

    def __init__(self, regions: list[TextRegion], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._regions = regions
        frame = Image.new("RGB", (self.frame.width, self.frame.height), (235, 235, 235))
        draw = ImageDraw.Draw(frame)
        draw.text((79, 57), "Hi", fill=(20, 20, 20))  # glyphs land inside the "in" region
        self.frame = frame

    def observe(self) -> Any:
        observation = super().observe()
        observation.ocr_text = list(self._regions)
        return observation


class UnverifiableSpaceBackend(SyntheticFrameBackend):
    """Every capture reports an UNVERIFIABLE coordinate space (the zoom refusal class)."""

    def observe(self) -> Any:
        observation = super().observe()
        observation.coordinate_space = CoordinateSpace.UNVERIFIABLE
        return observation


class ShiftingSyntheticFrameBackend(SyntheticFrameBackend):
    """A geometry-MATCHING screen shifter: the frame changes every capture while frame,
    payload, and declared dims all agree (the live-path contract).

    W5.1 (R-12): the divergent-geometry ``ShiftingScreenBackend`` no longer serves zoom
    crops (the crop geometry guard fails closed on any frame-vs-declared-dims
    divergence), so the zoom-premise staleness test rides this fake instead — the
    tested property (stock queue staleness binding with a zoom in flight) is unchanged.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._shift = 0

    def observe(self) -> Any:
        self._shift += 1
        self.frame = _unique_color_frame(self.width, self.height, offset=self._shift * 13)
        return super().observe()


# --- A1.1: pixel_evidence -----------------------------------------------------------------------


def test_a1_pixel_evidence_constants_are_the_exp020_1_frozen_values() -> None:
    """Regression pin for the EXP-020.1 freeze (2026-09-18, Commander W2.3
    transcription): the A1.1 normalization constants are FROZEN at sigma_lo=0.0
    (P90 negative class) / sigma_hi=32.535 (P10 positive class) — no class overlap
    (max neg 5.70 < min pos 25.74), 234/234 accuracy at the 0.5 threshold; source
    experiments/EXP-020-results/exp020_1-pixel-evidence.json. Any further change is a
    FORCED, recorded edit (scope: negatives flat-by-definition; live transfer checked
    at EXP-021)."""
    assert PIXEL_EVIDENCE_SIGMA_LO == 0.0
    assert PIXEL_EVIDENCE_SIGMA_HI == 32.535


def test_a1_pixel_evidence_flat_region_is_zero() -> None:
    """A flat/uniform bbox: sigma 0 -> the formula clamps to exactly 0.0."""
    frame = Image.new("RGB", (200, 100), (180, 180, 180))
    assert pixel_evidence_for(frame, 10, 10, 80, 40) == 0.0


def test_a1_pixel_evidence_text_region_is_positive() -> None:
    """A rendered-text bbox: stroke-driven variance scores > 0 (evidence, not accuracy)."""
    frame = Image.new("RGB", (200, 100), (255, 255, 255))
    draw = ImageDraw.Draw(frame)
    for row in range(4):
        draw.text((8, 6 + row * 18), "Rendered Text 0123", fill=(10, 10, 10))
    assert pixel_evidence_for(frame, 4, 4, 120, 80) > 0.0


def test_a1_pixel_evidence_degenerate_and_out_of_frame_are_none() -> None:
    """Fully-out-of-frame bboxes are UNMEASURABLE (None, never a silent 0.0); a
    partially-out-of-frame bbox is clamped and measured on its intersection."""
    frame = Image.new("RGB", (100, 80), (60, 60, 60))
    assert pixel_evidence_for(frame, 500, 500, 40, 20) is None
    assert pixel_evidence_for(frame, 90, 70, 100, 100) == 0.0  # clamped 10x10, flat
    assert pixel_evidence_for(None, 0, 0, 10, 10) is None  # no pixels obtainable at all


def test_a1_pixel_evidence_reported_in_block_with_sigma_constants() -> None:
    """With evidence ON (A2 opt-in), every served region carries ``pixel_evidence``;
    the block records the sigma constants (provenance, not state) and the mode."""
    regions = [TextRegion(text="Save", x=12, y=34, width=50, height=16)]
    observation = Observation(image_base64=_png_b64(), width=1280, height=720, ocr_text=regions)
    block = spatial_text_of(observation, pixel_evidence=True)
    assert block is not None
    assert block["pixel_evidence_mode"] == "on"
    assert block["pixel_evidence_sigma"] == [PIXEL_EVIDENCE_SIGMA_LO, PIXEL_EVIDENCE_SIGMA_HI]
    assert block["regions"][0]["pixel_evidence"] == 0.0  # the fake payload is flat white


def test_a1_pixel_evidence_one_decode_per_block_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """A1.1 decode discipline (A2 opt-in form): with evidence ON and absent ``_frame``
    -> the payload is decoded EXACTLY once per block build and reused for every region;
    the OCR-once cache never rebuilds."""
    from computer_use_mcp import visual_views as vv

    regions = [TextRegion(text=f"r{i}", x=2 + i, y=2, width=8, height=6) for i in range(4)]
    observation = Observation(image_base64=_png_b64(), width=1280, height=720, ocr_text=regions)
    decodes: list[int] = []
    original = vv._decode_payload

    def _counting_decode(image_base64: str) -> Any:
        decodes.append(1)
        return original(image_base64)

    monkeypatch.setattr(vv, "_decode_payload", _counting_decode)
    block = vv.spatial_text_of(observation, pixel_evidence=True)
    assert block is not None
    assert len(decodes) == 1  # ONE decode for the whole build (4 regions)
    assert all(entry["pixel_evidence"] == 0.0 for entry in block["regions"])
    assert vv.spatial_text_of(observation) is block  # OCR-once: reuse, no second decode
    assert len(decodes) == 1


def test_a1_pixel_evidence_unmeasurable_frame_is_null_not_crash() -> None:
    """Undecodable payload + no stash (evidence ON, A2 opt-in form): the UIA text is
    never lost to a measurement failure — the block still builds and unmeasurable
    regions carry None (not 0.0)."""
    regions = [TextRegion(text="t", x=2, y=2, width=8, height=6)]
    observation = Observation(
        image_base64="!!!not-a-payload!!!", width=100, height=80, ocr_text=regions
    )
    block = spatial_text_of(observation, pixel_evidence=True)
    assert block is not None
    assert block["regions"][0]["pixel_evidence"] is None
    assert block["regions"][0]["text"] == "t"


# --- A1.2: visual_view_density ------------------------------------------------------------------


def test_a1_density_defaults_to_coarse_and_enum_resolves(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent density == explicit "coarse" (the W2.1-adjudicated default); names are
    trimmed/casefolded; both serve byte-identical grid pixels."""
    session_id, _, _ = make_session(monkeypatch, backend=SyntheticFrameBackend(320, 240))
    default_blocks = server.computer_observe(session_id, visual_view="grid")
    coarse_blocks = server.computer_observe(
        session_id, visual_view="grid", visual_view_density="coarse"
    )
    casefold_blocks = server.computer_observe(
        session_id, visual_view="grid", visual_view_density="  COARSE "
    )
    assert observe_metadata(default_blocks)["visual_view"] == "grid"
    assert default_blocks[1].data == coarse_blocks[1].data == casefold_blocks[1].data
    assert resolve_density(None) == DEFAULT_GRID_DENSITY == "coarse"
    assert resolve_density(" Fine ") == "fine"


def test_a1_density_unknown_is_typed_error_raw_ignores_silently(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown density on a grid request: the typed ``invalid_visual_view``-class error
    naming the enum, BEFORE any capture. Raw requests IGNORE the parameter silently
    (A1.2); an ABSENT view request never even resolves it (H19 byte-identity)."""
    session_id, bundle, _ = make_session(monkeypatch)
    before = bundle.metrics.snapshot()["counters"]["screenshot_count"]
    result = server.computer_observe(session_id, visual_view="grid", visual_view_density="ultra")
    assert result["ok"] is False
    assert result["error"] == "invalid_visual_view"
    assert "'coarse'" in result["message"] and "'fine'" in result["message"]
    assert "visual_view_density" in result["message"]
    assert bundle.metrics.snapshot()["counters"]["screenshot_count"] == before  # no capture
    raw_blocks = server.computer_observe(
        session_id, visual_view="raw", visual_view_density="ultra"
    )
    raw_meta = observe_metadata(raw_blocks)
    assert raw_meta["visual_view"] == "raw"  # served anyway, density ignored silently
    absent_meta = observe_metadata(
        server.computer_observe(session_id, visual_view=None, visual_view_density="ultra")
    )
    assert list(absent_meta.keys()) == STOCK_IMAGE_KEYS  # byte-identical default shape
    with pytest.raises(InvalidVisualViewError):
        resolve_density("ultra")


# --- A1.3: computer_zoom ------------------------------------------------------------------------


def test_a1_zoom_metadata_shape(fresh_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The zoom result mirrors computer_observe's explicit-request shape + ``crop_region``."""
    backend = RegionFrameBackend(regions=[TextRegion(text="in", x=80, y=60, width=20, height=10)])
    session_id, _, _ = make_session(monkeypatch, backend=backend)
    blocks = server.computer_zoom(session_id, [8, 8, 32, 16])
    assert isinstance(blocks, list) and len(blocks) == 2
    meta = observe_metadata(blocks)
    assert list(meta.keys()) == [
        "observation", "digest", "observation_id", "active_app", "image_format",
        "text_summary", "crop_region", "visual_view", "visual_view_scale", "spatial_text",
    ]
    assert meta["visual_view"] == "raw"  # the zoom default view
    assert meta["visual_view_scale"] == 1.0  # under the outbound budget: no ladder scale
    assert meta["image_format"] == "image/png"
    assert meta["crop_region"] == [8, 8, 32, 16]


def test_a1_zoom_geometry_crop_equals_requested_region(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H26 falsifier (a): the served crop IS the requested region of the CITED capture —
    per-position-unique pixels, exact equality, and a changed screen produces a fresh,
    different crop under a NEW observation_id (no frame caching across requests)."""
    backend = SyntheticFrameBackend(320, 240)
    session_id, bundle, _ = make_session(monkeypatch, backend=backend)
    before = bundle.metrics.snapshot()["counters"]["screenshot_count"]
    blocks = server.computer_zoom(session_id, [64, 48, 128, 96])
    meta = observe_metadata(blocks)
    served = Image.open(io.BytesIO(base64.b64decode(blocks[1].data))).convert("RGB")
    served.load()
    expected = backend.frame.crop((64, 48, 192, 144))
    assert served.size == (128, 96)  # native resolution: no downscale, no upscale
    assert served.tobytes() == expected.tobytes()
    assert bundle.metrics.snapshot()["counters"]["screenshot_count"] == before + 1
    # the screen changes -> the next zoom serves the FRESH capture's region, new id
    backend.frame = _unique_color_frame(320, 240, offset=97)
    blocks2 = server.computer_zoom(session_id, [64, 48, 128, 96])
    meta2 = observe_metadata(blocks2)
    served2 = Image.open(io.BytesIO(base64.b64decode(blocks2[1].data))).convert("RGB")
    served2.load()
    assert meta2["observation_id"] != meta["observation_id"]
    assert served2.tobytes() == backend.frame.crop((64, 48, 192, 144)).tobytes()
    assert served2.tobytes() != served.tobytes()


def test_a1_zoom_grid_labels_show_canonical_values(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1.3 item 4: the crop grid's labels show CANONICAL screenshot-space VALUES —
    a label at crop-local (0, 0) reads "64,48" (crop origin baked into the VALUES),
    rendered per the spec recipe on the exact requested crop."""
    backend = SyntheticFrameBackend(320, 240)
    session_id, _, _ = make_session(monkeypatch, backend=backend)
    blocks = server.computer_zoom(
        session_id, [64, 48, 128, 96], visual_view="grid", visual_view_density="fine"
    )
    meta = observe_metadata(blocks)
    assert meta["visual_view"] == "grid"
    served = Image.open(io.BytesIO(base64.b64decode(blocks[1].data))).convert("RGB")
    served.load()
    plain = backend.frame.crop((64, 48, 192, 144))
    # the spec's rendering recipe, written out independently: lines + the canonical
    # label VALUE at the (0, 0) major anchor (fine: minor 32, major every 4th = 128).
    expected = plain.copy()
    draw = ImageDraw.Draw(expected)
    minor, major_every = GRID_DENSITIES["fine"]
    major = minor * major_every
    for x in range(0, plain.width, minor):
        draw.line([(x, 0), (x, plain.height - 1)], fill=MINOR_COLOR, width=1)
    for y in range(0, plain.height, minor):
        draw.line([(0, y), (plain.width - 1, y)], fill=MINOR_COLOR, width=1)
    for x in range(0, plain.width, major):
        draw.line([(x, 0), (x, plain.height - 1)], fill=MAJOR_COLOR, width=2)
    for y in range(0, plain.height, major):
        draw.line([(0, y), (plain.width - 1, y)], fill=MAJOR_COLOR, width=2)
    draw.text((LABEL_OFFSET_PX, LABEL_OFFSET_PX), label_text(64, 48), fill=LABEL_COLOR)
    assert served.tobytes() == expected.tobytes()
    # NOT the crop-local label values (a full-frame-style "0,0" label renders differently)
    wrong = plain.copy()
    wrong_draw = ImageDraw.Draw(wrong)
    wrong_draw.text((LABEL_OFFSET_PX, LABEL_OFFSET_PX), label_text(0, 0), fill=LABEL_COLOR)
    assert served.tobytes() != wrong.tobytes()
    # the crop pixels themselves are untouched away from lines/labels
    assert served.getpixel((100, 50)) == plain.getpixel((100, 50))


def test_a1_zoom_spatial_text_filtered_canonical_and_full_frame_evidence(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1.3 item 5 (A2 opt-in form): with ``pixel_evidence=True``, only
    crop-INTERSECTING regions are served, with canonical coordinates UNCHANGED (never
    re-based) and pixel_evidence computed on the FULL frame; region_count/
    omitted_count report every unserved region (nothing silent)."""
    regions = [
        TextRegion(text="inside", x=80, y=60, width=20, height=10),  # over the text patch
        TextRegion(text="partial", x=180, y=140, width=20, height=10),  # corner overlap
        TextRegion(text="outside", x=0, y=0, width=20, height=10),  # flat area, no overlap
        TextRegion(text="edge", x=40, y=60, width=24, height=10),  # zero-area touch at x=64
    ]
    backend = RegionFrameBackend(regions=regions)
    session_id, _, _ = make_session(monkeypatch, backend=backend)
    blocks = server.computer_zoom(session_id, [64, 48, 128, 96], pixel_evidence=True)
    meta = observe_metadata(blocks)
    block = meta["spatial_text"]
    assert block is not None
    assert block["crop_region"] == [64, 48, 128, 96]
    assert block["crop_origin"] == [0, 0]  # monitor origin provenance, unchanged
    assert [entry["text"] for entry in block["regions"]] == ["inside", "partial"]
    inside = block["regions"][0]
    assert (inside["x"], inside["y"], inside["width"], inside["height"]) == (80, 60, 20, 10)
    assert block["region_count"] == 2
    assert block["omitted_count"] == 2  # outside + the zero-area edge touch
    # pixel_evidence is the FULL-FRAME measurement (the "inside" region covers the
    # rendered-text patch -> > 0; a crop-local recomputation would score flat pixels)
    assert inside["pixel_evidence"] == pixel_evidence_for(backend.frame, 80, 60, 20, 10)
    assert inside["pixel_evidence"] > 0.0
    assert block["regions"][1]["pixel_evidence"] == 0.0  # flat corner region


def test_a1_zoom_fail_closed_region_matrix(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1.3 item 2: every violation class returns the typed ``invalid_region`` error
    naming the bounds — malformed shapes waste NO capture; out-of-bounds is refused
    after the (honest) capture; never a clipped guess, never a full-frame fallback."""
    backend = SyntheticFrameBackend(320, 240)
    session_id, bundle, _ = make_session(monkeypatch, backend=backend)
    baseline = bundle.metrics.snapshot()["counters"]["screenshot_count"]
    pre_capture_bad: list[Any] = [
        None,
        "not-a-region",
        "0,0,10,10",
        [1, 2, 3],
        [1, 2, 3, 4, 5],
        [1, 2, 3, "4"],
        [1.5, 2, 3, 4],
        [10, 10, 0, 10],
        [10, 10, 10, 0],
        [10, 10, -1, 10],
    ]
    for bad in pre_capture_bad:
        result = server.computer_zoom(session_id, bad)
        assert result["ok"] is False and result["error"] == "invalid_region"
        assert isinstance(result["message"], str) and result["message"]
        assert not isinstance(result, list)  # no content blocks under the error
    assert bundle.metrics.snapshot()["counters"]["screenshot_count"] == baseline
    for bad in (
        [-1, 0, 10, 10],
        [0, -1, 10, 10],
        [300, 200, 100, 100],
        [0, 0, 321, 10],
        [0, 0, 10, 241],
    ):
        result = server.computer_zoom(session_id, bad)
        assert result["ok"] is False and result["error"] == "invalid_region"
        assert "never clips" in result["message"]  # the bounds are named, no fallback
    assert bundle.metrics.snapshot()["counters"]["screenshot_count"] > baseline


def test_a1_zoom_refuses_unverifiable_coordinate_space(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An UNVERIFIABLE coordinate space refuses the zoom with the typed error — the
    same doctrine as coordinate actions (A1.3 item 2)."""
    session_id, _, _ = make_session(monkeypatch, backend=UnverifiableSpaceBackend())
    result = server.computer_zoom(session_id, [0, 0, 32, 24])
    assert result["ok"] is False
    assert result["error"] == "invalid_region"
    assert "unverifiable" in result["message"]
    assert not isinstance(result, list)


def test_a1_zoom_text_mode_single_text_block(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Text-mode sessions get the observe-family D1 treatment: ONE text block with
    ``image_format: "none"``, crop provenance, and the (possibly null) spatial text."""
    session_id, _, _ = make_session(
        monkeypatch, backend=SyntheticFrameBackend(320, 240), image_delivery="text"
    )
    blocks = server.computer_zoom(session_id, [10, 10, 40, 30])
    assert isinstance(blocks, list) and len(blocks) == 1
    meta = observe_metadata(blocks)
    assert meta["image_format"] == "none"
    assert meta["image_delivery"] == "text"
    assert meta["crop_region"] == [10, 10, 40, 30]
    assert meta["visual_view"] == "raw"
    assert meta["visual_view_scale"] is None  # no pixels served, nothing claimed
    assert meta["spatial_text"] is None  # the fake's UIA read is degraded (ScriptedBackend)
    assert meta["spatial_text_available"] is False  # the honest degraded marker


def test_a1_zoom_fresh_observation_audit_and_metrics(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1.3 items 1/7: every zoom is a FRESH capture (new observation_id), bumps
    ``screenshot_count``, and emits the ``observation`` audit event with the
    ``{"source": "computer_zoom"}`` metadata mirroring the observe event shape."""
    backend = RegionFrameBackend(regions=[TextRegion(text="in", x=4, y=4, width=8, height=6)])
    session_id, bundle, _ = make_session(monkeypatch, backend=backend)
    recorded: list[tuple[str, dict[str, Any]]] = []
    original_emit = bundle.auditor.emit

    def _recording_emit(event: str, *args: Any, **kwargs: Any) -> Any:
        recorded.append((event, kwargs))
        return original_emit(event, *args, **kwargs)

    bundle.auditor.emit = _recording_emit
    before = bundle.metrics.snapshot()["counters"]["screenshot_count"]
    first = observe_metadata(server.computer_zoom(session_id, [0, 0, 32, 24]))
    second = observe_metadata(server.computer_zoom(session_id, [0, 0, 32, 24]))
    assert first["observation_id"] != second["observation_id"]  # fresh capture per call
    assert bundle.metrics.snapshot()["counters"]["screenshot_count"] == before + 2
    assert bundle.extra["last_observation_digest"] == second["digest"]
    events = [kwargs for event, kwargs in recorded if event == "observation"]
    assert events, "the observation audit event fired"
    assert events[-1]["metadata"] == {"source": "computer_zoom"}
    assert events[-1]["observation_id"] == second["observation_id"]


async def test_a1_zoom_staleness_binding_queue_premise_stock_path(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H26 falsifier (c) (item-9 pattern): a zoom premise binds through the STOCK
    queue path — after the screen shifts, the queued action stops on the stale
    premise IDENTICALLY to the zoom-less baseline, and the zoom observation_id is
    never re-served.

    W5.1 (R-12): the zoom premise rides the geometry-MATCHING
    ``ShiftingSyntheticFrameBackend`` — the previously used divergent-geometry fake
    (64x48 payload on 1280x720 dims) is now correctly refused by the crop geometry
    guard (``view_derive_failed``), so it can no longer serve a zoom premise at all."""
    backend_off = ShiftingScreenBackend()
    payload_off, kinds_off, executed_off = await _queue_scenario(backend_off)

    backend_zoom = ShiftingSyntheticFrameBackend(width=320, height=240)
    server._registry = SessionRegistry(max_sessions=8)
    server._bundles = {}
    server._stopped_sessions = {}
    server._backend_factory = lambda: backend_zoom
    server._provider_factory = lambda: tci.ScriptedProvider([])
    start = server.start_session(dry_run=False, require_approval=False, limits=FAST_LIMITS)
    zoom_blocks = server.computer_zoom(str(start["session_id"]), [0, 0, 32, 24])
    premise_id = observe_metadata(zoom_blocks)["observation_id"]
    payload_zoom, kinds_zoom, executed_zoom = await _queue_scenario(backend_zoom)

    # the zoom premise never re-serves: the queue's fresh captures are new observations
    assert premise_id != backend_zoom.observed[-1].observation_id
    # BYTE-IDENTICAL gate/stop behavior, zoom in flight or not (H20/H26):
    assert payload_zoom["follow_ups_stopped_reason"] == payload_off["follow_ups_stopped_reason"]
    assert payload_zoom["follow_ups_stopped_reason"] is not None  # a true stop happened
    assert kinds_zoom == kinds_off
    assert executed_zoom == executed_off == 1  # the queued action never ran on a stale id


def test_a1_zoom_density_grid_served_and_raw_ignores(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zoom grid renders at the requested density via the shared crop renderer; zoom
    raw ignores a bogus density silently (A1.2) and still serves the crop + block."""
    backend = SyntheticFrameBackend(320, 240)
    session_id, _, _ = make_session(monkeypatch, backend=backend)
    grid_blocks = server.computer_zoom(
        session_id, [16, 16, 96, 64], visual_view="grid", visual_view_density="fine"
    )
    grid_meta = observe_metadata(grid_blocks)
    assert grid_meta["visual_view"] == "grid"
    served = Image.open(io.BytesIO(base64.b64decode(grid_blocks[1].data))).convert("RGB")
    served.load()
    reference = render_crop_grid(
        backend.frame.crop((16, 16, 112, 80)), origin_x=16, origin_y=16, density="fine"
    )
    assert served.tobytes() == reference.tobytes()
    raw_blocks = server.computer_zoom(
        session_id, [16, 16, 96, 64], visual_view="raw", visual_view_density="ultra"
    )
    raw_meta = observe_metadata(raw_blocks)
    assert raw_meta["visual_view"] == "raw"  # density ignored, zoom served anyway
    assert raw_meta["crop_region"] == [16, 16, 96, 64]


def test_a1_zoom_crop_from_payload_when_stash_absent(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy/fake path (no ``_frame`` stash): zoom crops the decoded payload
    exactly once and still serves the exact region (I-4's recorded-decode branch)."""
    backend = SyntheticFrameBackend(320, 240)
    original_observe = SyntheticFrameBackend.observe

    def _no_stash(self: Any) -> Any:
        observation = original_observe(self)
        observation._frame = None
        return observation

    monkeypatch.setattr(SyntheticFrameBackend, "observe", _no_stash)
    session_id, _, _ = make_session(monkeypatch, backend=backend)
    blocks = server.computer_zoom(session_id, [64, 48, 128, 96])
    meta = observe_metadata(blocks)
    assert meta["image_format"] == "image/png"
    served = Image.open(io.BytesIO(base64.b64decode(blocks[1].data))).convert("RGB")
    served.load()
    # the payload IS the frame here, so the crop equality must still hold exactly
    assert served.tobytes() == backend.frame.crop((64, 48, 192, 144)).tobytes()


def test_a1_zoom_undecodable_payload_typed_error(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No stash + undecodable payload: the typed ``view_derive_failed`` error — never a
    silent full-frame fallback (the AL-002 failure table row, zoom branch)."""
    backend = SyntheticFrameBackend(320, 240)
    original_observe = SyntheticFrameBackend.observe

    def _corrupt(self: Any) -> Any:
        observation = original_observe(self)
        observation._frame = None
        observation.image_base64 = "!!!not-a-payload!!!"
        return observation

    monkeypatch.setattr(SyntheticFrameBackend, "observe", _corrupt)
    session_id, _, _ = make_session(monkeypatch, backend=backend)
    result = server.computer_zoom(session_id, [64, 48, 128, 96])
    assert result["ok"] is False
    assert result["error"] == "view_derive_failed"
    assert "undecodable" in result["message"]
    assert not isinstance(result, list)


# ==============================================================================================
# --- W5.1 red-team fixes (R-RED findings R-10 HIGH + R-12 MEDIUM; research/AVR008-red-team.md) --
# ==============================================================================================


class LazyFrameRegionsBackend(tci.ScriptedBackend):
    """The R-10 repro world: a PIL-hostile LAZY ``_frame`` (a TRUNCATED PNG — the
    header parses, ``load()`` raises) stashed alongside non-empty ``ocr_text``, so the
    A1.1 pixel-evidence measurement inside ``spatial_text_of`` raises mid-build."""

    def __init__(self, regions: list[TextRegion], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._regions = regions

    def observe(self) -> Any:
        observation = super().observe()
        raw = base64.b64decode(_png_b64("white"))
        lazy = Image.open(io.BytesIO(raw[: len(raw) // 3]))  # header parses; load() fails
        observation._frame = lazy
        observation.ocr_text = list(self._regions)
        return observation


def test_w51_r10_attach_failure_is_typed_not_nameerror_both_modes(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W5.1/R-10 (HIGH): a PIL-hostile lazy ``_frame`` + non-empty ``ocr_text`` makes
    the spatial-text build raise inside ``spatial_text_of``; the attach handler
    re-raises ``ViewDeriveError`` — which MUST be imported in server.py (pre-fix it
    raised NameError, escaping every typed guard) — and the text-mode branch MUST be
    guarded like the image branch. BOTH modes degrade to the typed
    ``view_derive_failed`` error per the termination table, never an untyped crash,
    and the canonical default path remains usable afterwards.

    W2.4/A2 note: the evidence computation is now OPT-IN, so the repro requests
    ``pixel_evidence=True`` — the lazy frame is only touched when the A1.1 formula
    measures it (which is exactly the attach path R-10 attacked)."""
    regions = [TextRegion(text="T", x=4, y=4, width=10, height=10)]
    # image mode: the guarded attach (pre-fix: NameError escaped `except VisualViewError`)
    session_id, _, _ = make_session(monkeypatch, backend=LazyFrameRegionsBackend(regions))
    result = server.computer_observe(session_id, visual_view="raw", pixel_evidence=True)
    assert result["ok"] is False
    assert result["error"] == "view_derive_failed"
    assert "NameError" not in str(result["message"])
    assert "spatial-text attach failed" in str(result["message"])
    assert not isinstance(result, list)  # the typed error, not half-served blocks
    blocks = server.computer_observe(session_id)  # canonical path still usable
    assert len(blocks) == 2
    # text mode: the previously UNGUARDED branch (pre-fix: NameError escaped uncaught)
    session_id2, _, _ = make_session(
        monkeypatch, backend=LazyFrameRegionsBackend(regions), image_delivery="text"
    )
    result2 = server.computer_observe(session_id2, visual_view="raw", pixel_evidence=True)
    assert result2["ok"] is False
    assert result2["error"] == "view_derive_failed"
    assert "NameError" not in str(result2["message"])
    assert not isinstance(result2, list)


class DivergentFrameBackend(tci.ScriptedBackend):
    """The R-12 repro world: a 64x48 frame stash on an observation CLAIMING 1280x720
    (the fake/legacy divergent-geometry world; the payload matches the frame, the
    declared dims do not)."""

    def observe(self) -> Any:
        observation = super().observe()
        observation._frame = Image.new("RGB", (64, 48), (250, 250, 250))
        return observation


def test_w51_r12_zoom_geometry_divergence_fails_closed_never_fabricates(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W5.1/R-12 (MEDIUM): a crop from a frame whose geometry diverges from the
    observation's declared dims is NOT provably the requested region — PIL would
    silently zero-pad out-of-bounds boxes (fabricated black pixels served with
    ``visual_view_scale: 1.0`` and in-frame ``crop_region`` metadata). The guard
    refuses with the typed ``view_derive_failed`` error naming BOTH geometries — for
    the out-of-frame repro region AND for an in-frame one (the coordinate space itself
    is unproven) — on the stash path AND the recorded-decode path. Zero-padded black
    is never served, and the canonical observe path stays usable."""
    session_id, _, _ = make_session(monkeypatch, backend=DivergentFrameBackend())
    for region in ([1100, 640, 160, 64], [0, 0, 32, 24]):  # the repro + an in-frame region
        for view in ("raw", "grid"):  # the crop (and its guard) precedes any rendering
            result = server.computer_zoom(session_id, region, visual_view=view)
            assert result["ok"] is False
            assert result["error"] == "view_derive_failed"
            assert "64x48" in str(result["message"]) and "1280x720" in str(result["message"])
            assert not isinstance(result, list)  # no fabricated image block, ever
    # the recorded-decode path (no stash) hits the same guard
    original_observe = DivergentFrameBackend.observe

    def _no_stash(self: Any) -> Any:
        observation = original_observe(self)
        observation._frame = None
        return observation

    monkeypatch.setattr(DivergentFrameBackend, "observe", _no_stash)
    session_id2, _, _ = make_session(monkeypatch, backend=DivergentFrameBackend())
    result2 = server.computer_zoom(session_id2, [0, 0, 32, 24])
    assert result2["ok"] is False and result2["error"] == "view_derive_failed"
    assert "64x48" in str(result2["message"]) and "1280x720" in str(result2["message"])
    # the guard is zoom-crop-only: the canonical observe path remains usable
    blocks = server.computer_observe(session_id)
    assert len(blocks) == 2


# ==============================================================================================
# --- W2.4: A2 opt-in pixel_evidence (owner decision D2, 2026-09-18; AL-002 Amendment A2) --------
# ==============================================================================================


def test_w24_default_evidence_off_field_absent_mode_off_no_sigma_work(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2/D2 default (evidence OFF): the per-region ``pixel_evidence`` field is OMITTED
    entirely (absent, never null-fabricated), the block records
    ``"pixel_evidence_mode": "off"``, and NO sigma computation runs — the A1.1 formula
    is never invoked (the canary would explode if it were), on observe AND zoom."""
    from computer_use_mcp import visual_views as vv

    def _must_not_compute(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("pixel_evidence computation ran while the flag is OFF")

    monkeypatch.setattr(vv, "pixel_evidence_for", _must_not_compute)
    backend = RegionFrameBackend(regions=[TextRegion(text="Save", x=12, y=34, width=50, height=16)])
    session_id, _, _ = make_session(monkeypatch, backend=backend)
    meta = observe_metadata(server.computer_observe(session_id, visual_view="raw"))
    block = meta["spatial_text"]
    assert block is not None
    assert block["pixel_evidence_mode"] == "off"
    assert block["region_count"] == 1
    assert "pixel_evidence" not in block["regions"][0]  # absent, not null
    assert block["regions"][0]["text"] == "Save"  # the UIA text itself is untouched
    # zoom default: the same opt-in shape (the flag rides the single zoom build)
    zmeta = observe_metadata(server.computer_zoom(session_id, [0, 0, 100, 80]))
    zblock = zmeta["spatial_text"]
    assert zblock is not None
    assert zblock["pixel_evidence_mode"] == "off"
    assert all("pixel_evidence" not in entry for entry in zblock["regions"])


def test_w24_evidence_on_field_present_bounded_mode_on(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2 explicit ``pixel_evidence=True``: every served region carries its measured
    ``pixel_evidence`` (in [0, 1] or None for unmeasurable), the block records
    ``"pixel_evidence_mode": "on"`` plus the frozen sigma constants."""
    backend = RegionFrameBackend(
        regions=[TextRegion(text="Save", x=12, y=34, width=50, height=16)]
    )
    session_id, _, _ = make_session(monkeypatch, backend=backend)
    meta = observe_metadata(
        server.computer_observe(session_id, visual_view="raw", pixel_evidence=True)
    )
    block = meta["spatial_text"]
    assert block is not None
    assert block["pixel_evidence_mode"] == "on"
    assert block["pixel_evidence_sigma"] == [PIXEL_EVIDENCE_SIGMA_LO, PIXEL_EVIDENCE_SIGMA_HI]
    assert block["regions"]
    for entry in block["regions"]:
        assert "pixel_evidence" in entry
        value = entry["pixel_evidence"]
        assert value is None or 0.0 <= value <= 1.0


def test_w24_non_bool_pixel_evidence_typed_error_before_capture(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2 fail-closed: non-boolean ``pixel_evidence`` values are rejected with the typed
    ``invalid_pixel_evidence`` error BEFORE any capture on BOTH tools (zero captures
    wasted — strict bool, the ``via`` boundary precedent)."""
    backend = RegionFrameBackend(regions=[TextRegion(text="Save", x=12, y=34, width=50, height=16)])
    session_id, bundle, _ = make_session(monkeypatch, backend=backend)
    baseline = bundle.metrics.snapshot()["counters"]["screenshot_count"]
    for bad in ("yes", 1, 0, 3.14, "true"):
        result = server.computer_observe(session_id, visual_view="raw", pixel_evidence=bad)
        assert result["ok"] is False
        assert result["error"] == "invalid_pixel_evidence"
        assert "must be a boolean" in result["message"]
        zoomed = server.computer_zoom(session_id, [0, 0, 32, 24], pixel_evidence=bad)
        assert zoomed["ok"] is False
        assert zoomed["error"] == "invalid_pixel_evidence"
        assert not isinstance(result, list) and not isinstance(zoomed, list)
    assert bundle.metrics.snapshot()["counters"]["screenshot_count"] == baseline


def test_w24_h21_prime_counter_unchanged_by_flag_and_zoom_honors_flag(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H21' (A2): the OCR-once counter is 1 per observation REGARDLESS of the flag —
    the flag shapes WHAT the single build computes (the first build's mode is
    memoized; a later request with the other flag reuses that block). Zoom honors the
    flag both ways (default off / explicit on)."""
    regions = [TextRegion(text="Save", x=12, y=34, width=50, height=16)]
    # OFF first: the memoized block stays mode-off even when a later request asks ON.
    backend = SameObservationBackend()
    backend.observe()
    assert backend._fixed is not None
    backend._fixed.ocr_text = list(regions)
    session_id, bundle, _ = make_session(monkeypatch, backend=backend)
    off_meta = observe_metadata(server.computer_observe(session_id, visual_view="raw"))
    assert off_meta["spatial_text"]["pixel_evidence_mode"] == "off"
    on_request = observe_metadata(
        server.computer_observe(session_id, visual_view="raw", pixel_evidence=True)
    )
    assert on_request["spatial_text"]["pixel_evidence_mode"] == "off"  # the cached build
    assert backend._fixed._spatial_text_compute_count == 1  # H21': never a second build
    assert bundle.metrics.snapshot()["counters"]["spatial_text_compute"] == 1
    # ON first: the single build computes the evidence; the count still stays 1.
    backend2 = SameObservationBackend()
    backend2.observe()
    assert backend2._fixed is not None
    backend2._fixed.ocr_text = list(regions)
    session_id2, bundle2, _ = make_session(monkeypatch, backend=backend2)
    on_meta = observe_metadata(
        server.computer_observe(session_id2, visual_view="raw", pixel_evidence=True)
    )
    assert on_meta["spatial_text"]["pixel_evidence_mode"] == "on"
    assert "pixel_evidence" in on_meta["spatial_text"]["regions"][0]
    assert backend2._fixed._spatial_text_compute_count == 1
    assert bundle2.metrics.snapshot()["counters"]["spatial_text_compute"] == 1
    # zoom honors the flag both ways on a fresh matching-geometry session
    backend3 = RegionFrameBackend(regions=list(regions))
    session_id3, _, _ = make_session(monkeypatch, backend=backend3)
    z_off = observe_metadata(server.computer_zoom(session_id3, [0, 0, 100, 80]))
    assert z_off["spatial_text"]["pixel_evidence_mode"] == "off"
    assert "pixel_evidence" not in z_off["spatial_text"]["regions"][0]
    z_on = observe_metadata(
        server.computer_zoom(session_id3, [0, 0, 100, 80], pixel_evidence=True)
    )
    assert z_on["spatial_text"]["pixel_evidence_mode"] == "on"
    assert "pixel_evidence" in z_on["spatial_text"]["regions"][0]
