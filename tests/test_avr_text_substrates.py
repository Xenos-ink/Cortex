"""AL-002 Amendment A3 (PROPOSAL) — the pluggable text-substrate seam (W-A3, AVR-009).

Owner directive D-3: the spatial-text substrate is FIXABLE via an OPTIONAL side-install
(a well-known package name, ``cortex_text_ocr``); UIA stays the default; the MCP
auto-detects an installed substrate; installing it is the user's implicit acceptance of
the added latency. Design of record: ``research/AVR009-substrate-design.md``; LAB
implementation: ``src/computer_use_mcp/text_substrates.py`` wired into
``visual_views.spatial_text_of`` (the single OCR-once build).

The six pins:

1. default path unchanged — no side package: block = the A2 baseline + EXACTLY the one
   additive ``substrate: "uia"`` key; A2's OFF canary holds (the frame is never
   fetched/decoded); detection honestly reports unavailable.
2. a FAKE side package (per-test tmp fixture, never shipped) is detected: regions
   merged AFTER the UIA's, positive-area dedupe (UIA wins), provenance
   ``ocr:cortex_text_ocr``, merge counts, latency ``substrate_ms``, real confidence
   passed through, contract arguments actually received (monitor bounds, verdict
   space, the 50 ms-class budget, the pixel frame).
3. the fake violating the contract (non-list result) fails CLOSED to the UIA regions
   with an honest ``substrate_error`` record (+ elapsed ms); the observation never
   fails.
4. latency/provenance ride the EXISTING seam: the side cost is inside the single
   build's ``on_compute`` callback (``spatial_text_ms`` upstream), recorded exactly
   once, alongside the in-block ``substrate_ms``.
5. H21' counter semantics unchanged: the merge happens INSIDE the single OCR-once
   build — two block requests, counter == 1, the side entrypoint called exactly once,
   the second request served the memoized block.
6. the PYTHONPATH side-directory probe (A3 §1 probe 2): the package found via the
   ``PYTHONPATH`` environment alone (NOT via ``sys.path``) is detected and merged.

Every fixture package is written to the test's tmp dir and cleaned from ``sys.path``/
``sys.modules``; no fixture code ships in the patch.
"""

from __future__ import annotations

import base64
import io
import os
import sys
from typing import Any

import pytest
from PIL import Image

from computer_use_mcp import text_substrates, visual_views
from computer_use_mcp.models import CoordinateSpace, MonitorInfo, Observation, TextRegion
from computer_use_mcp.text_substrates import (
    SIDE_SUBSTRATE_BUDGET_SECONDS,
    SIDE_SUBSTRATE_PACKAGE,
    SubstrateVerdict,
    boxes_overlap,
    detect_side_substrate,
)
from computer_use_mcp.visual_views import spatial_text_of

#: The A2-baseline spatial-text block keys (pre-A3, the W2.4 state — the delta A3 adds
#: is the provenance keys, nothing else).
A2_BASELINE_KEYS = {
    "observation_id",
    "coordinate_space",
    "coordinate_scale",
    "crop_origin",
    "region_count",
    "omitted_count",
    "pixel_evidence_mode",
    "pixel_evidence_sigma",
    "regions",
}


# --- helpers ----------------------------------------------------------------------------------


def _png_b64(size: tuple[int, int] = (64, 48)) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _observation(
    ocr_regions: list[TextRegion] | None = None,
    monitor: MonitorInfo | None = None,
) -> Observation:
    """A minimal observe-shaped observation (verified passthrough, optional UIA text)."""
    return Observation(
        image_base64=_png_b64(),
        width=64,
        height=48,
        coordinate_space=CoordinateSpace.VERIFIED_PASSTHROUGH,
        coordinate_scale_x=1.0,
        coordinate_scale_y=1.0,
        monitor=monitor,
        ocr_text=ocr_regions,
    )


@pytest.fixture()
def no_side_package():
    """Default-world isolation: the well-known side package exists before OR after."""
    sys.modules.pop(SIDE_SUBSTRATE_PACKAGE, None)
    yield
    sys.modules.pop(SIDE_SUBSTRATE_PACKAGE, None)


def _install_fake_package(monkeypatch: pytest.MonkeyPatch, directory: os.PathLike[str], source: str) -> None:
    """Write a tiny ``cortex_text_ocr.py`` fixture package and put it on ``sys.path``."""
    package_file = directory / "cortex_text_ocr.py"
    package_file.write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(directory))


FAKE_PACKAGE_OK = '''
CALLS = []

def regions(monitor, verdict, timeout_budget, *, frame=None):
    CALLS.append({
        "monitor": None if monitor is None else list(monitor.bounds),
        "space": getattr(verdict, "space", None),
        "scales": (getattr(verdict, "scale_x", None), getattr(verdict, "scale_y", None)),
        "timeout_budget": timeout_budget,
        "has_frame": frame is not None,
    })
    return [
        {"text": "dup", "x": 0, "y": 0, "width": 10, "height": 10, "confidence": None},
        {"text": "novel", "x": 200, "y": 200, "width": 30, "height": 12, "confidence": 0.87},
    ]
'''

FAKE_PACKAGE_BAD_SHAPE = '''
def regions(monitor, verdict, timeout_budget, *, frame=None):
    return {"not": "a list"}
'''

FAKE_PACKAGE_BOOM = '''
def regions(monitor, verdict, timeout_budget, *, frame=None):
    raise RuntimeError("engine exploded")
'''


# --- 1: default path unchanged (no side package) -----------------------------------------------


def test_a3_no_side_package_block_is_a2_baseline_plus_substrate_uia(
    no_side_package, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a side package: exactly ONE additive ``substrate: "uia"`` key on the A2
    block; the A2 OFF canary holds (the frame is never fetched/decoded); detection
    honestly reports unavailable; probe 2 stays inert with no PYTHONPATH."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    assert detect_side_substrate().available() is False  # honest absence (A3 §1 probe 3)
    observation = _observation(ocr_regions=[TextRegion(text="t", x=0, y=0, width=4, height=4)])

    def _boobytrap(observation: Observation) -> Image.Image | None:
        raise AssertionError("the A2 OFF path fetched/decoded the frame")

    monkeypatch.setattr(visual_views, "_pixel_evidence_frame", _boobytrap)
    block = spatial_text_of(observation)
    assert block is not None
    assert set(block) == A2_BASELINE_KEYS | {"substrate"}  # exactly one additive key
    assert block["substrate"] == "uia"
    assert block["region_count"] == 1
    assert [entry["text"] for entry in block["regions"]] == ["t"]
    assert block["pixel_evidence_mode"] == "off"
    assert "substrate_error" not in block
    assert "substrate_merged" not in block and "substrate_ms" not in block
    assert observation._spatial_text_compute_count == 1  # H21' untouched


# --- 2: fake side package detected, merged, provenance -----------------------------------------


def test_a3_fake_side_package_detected_merged_with_provenance(
    no_side_package, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The fake package is auto-detected: its regions merge AFTER the UIA's (positive-
    area dedupe, UIA wins), provenance + counts + latency ship, real confidence passes
    through, and the contract arguments are actually received (monitor bounds, verdict,
    the 50 ms-class budget, the pixel frame)."""
    _install_fake_package(monkeypatch, tmp_path, FAKE_PACKAGE_OK)
    monitor = MonitorInfo(id="m", index=0, bounds=(100, 200, 640, 480))
    observation = _observation(
        ocr_regions=[TextRegion(text="uia1", x=0, y=0, width=10, height=10)], monitor=monitor
    )
    block = spatial_text_of(observation)
    assert block["substrate"] == "ocr:cortex_text_ocr"
    assert block["substrate_merged"] == 1  # "novel" kept
    assert block["substrate_deduped"] == 1  # "dup" overlaps uia1 -> dropped (UIA wins)
    assert isinstance(block["substrate_ms"], float) and block["substrate_ms"] >= 0.0
    assert [entry["text"] for entry in block["regions"]] == ["uia1", "novel"]
    assert block["regions"][1]["confidence"] == 0.87  # REAL confidence, passed through
    assert block["regions"][0]["confidence"] is None  # UIA truth untouched
    assert block["region_count"] == 2 and block["omitted_count"] == 0
    assert list(block.keys()) == [
        "observation_id", "coordinate_space", "coordinate_scale", "crop_origin",
        "region_count", "omitted_count", "substrate", "substrate_merged",
        "substrate_deduped", "substrate_ms", "pixel_evidence_mode",
        "pixel_evidence_sigma", "regions",
    ]  # deterministic A3 shipping order
    call = sys.modules[SIDE_SUBSTRATE_PACKAGE].CALLS[0]
    assert call["monitor"] == [100, 200, 640, 480]  # crop origin provenance argument
    assert call["space"] == CoordinateSpace.VERIFIED_PASSTHROUGH.value
    assert call["scales"] == (1.0, 1.0)
    assert call["timeout_budget"] == SIDE_SUBSTRATE_BUDGET_SECONDS  # the 50 ms class
    assert call["has_frame"] is True  # the pixel source was supplied to the OCR arm


# --- 3: contract violation fails closed to UIA + honest error record ---------------------------


@pytest.mark.parametrize(
    "source, fragment",
    [
        (FAKE_PACKAGE_BAD_SHAPE, "contract requires a list"),
        (FAKE_PACKAGE_BOOM, "RuntimeError: engine exploded"),
    ],
)
def test_a3_contract_violation_fails_closed_to_uia_with_error_record(
    no_side_package, monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
    source: str, fragment: str,
) -> None:
    """A side substrate that violates the contract (non-list result) or explodes is
    degraded: the served regions are the UIA truth, provenance stays "uia", and an
    honest ``substrate_error`` names the violation. The observation never fails."""
    _install_fake_package(monkeypatch, tmp_path, source)
    observation = _observation(ocr_regions=[TextRegion(text="uia1", x=0, y=0, width=10, height=10)])
    block = spatial_text_of(observation)  # must not raise
    assert block is not None
    assert block["substrate"] == "uia"  # fail-open to the default
    assert [entry["text"] for entry in block["regions"]] == ["uia1"]
    assert "substrate_ms" in block
    assert fragment in block["substrate_error"]
    assert "substrate_merged" not in block and "substrate_deduped" not in block
    assert block["pixel_evidence_mode"] == "off"  # A2 semantics untouched


# --- 4: latency/provenance ride the EXISTING seam ----------------------------------------------


def test_a3_side_cost_rides_the_existing_on_compute_seam_once(
    no_side_package, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The side-substrate cost is INSIDE the single OCR-once build: the existing
    ``on_compute`` callback (``spatial_text_ms`` upstream, ``server._spatial_text_for``)
    fires exactly once for the whole build (merge included), next to the in-block
    ``substrate_ms``; the budget passed down is the 50 ms-class constant."""
    _install_fake_package(monkeypatch, tmp_path, FAKE_PACKAGE_OK)
    observation = _observation(ocr_regions=[TextRegion(text="t", x=0, y=0, width=4, height=4)])
    computes: list[float] = []
    block = spatial_text_of(observation, on_compute=computes.append)
    assert len(computes) == 1  # ONE build, ONE instrumentation callback (H21')
    assert computes[0] >= 0.0 and computes[0] >= block["substrate_ms"]  # build >= side call
    assert SIDE_SUBSTRATE_BUDGET_SECONDS == 0.05  # the UIA_READ_BUDGET_SECONDS class
    assert block["substrate_ms"] >= 0.0  # cost REPORTED, never gated (A3 §5)


# --- 5: H21' — the merge happens INSIDE the single OCR-once build -------------------------------


def test_a3_h21_counter_unchanged_side_entrypoint_called_once(
    no_side_package, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Two block requests on one observation: counter stays 1, the side entrypoint is
    called exactly once, and the second request serves the memoized (cached) block —
    the seam shapes WHAT the single build computes, never the count (H21')."""
    _install_fake_package(monkeypatch, tmp_path, FAKE_PACKAGE_OK)
    observation = _observation(ocr_regions=[TextRegion(text="t", x=0, y=0, width=4, height=4)])
    first = spatial_text_of(observation)
    second = spatial_text_of(observation)
    assert observation._spatial_text_compute_count == 1
    assert len(sys.modules[SIDE_SUBSTRATE_PACKAGE].CALLS) == 1  # no re-derivation
    assert second is first  # the memoized block object itself (I-2)
    assert second["substrate"] == "ocr:cortex_text_ocr"


# --- 6: the PYTHONPATH side-directory probe (A3 §1 probe 2) -------------------------------------


def test_a3_pythonpath_side_directory_probe_detects_and_merges(
    no_side_package, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A drop-in side-directory found ONLY via the ``PYTHONPATH`` environment (NOT via
    ``sys.path`` — the embedded/frozen-launch case) is auto-detected and merged by
    probe 2; probe 1 (``find_spec`` over ``sys.path``) must miss it first."""
    package_file = tmp_path / "cortex_text_ocr.py"
    package_file.write_text(FAKE_PACKAGE_OK, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))  # probe 2's ONLY source
    assert str(tmp_path) not in sys.path  # probe 1 (find_spec over sys.path) must miss
    observation = _observation(ocr_regions=[TextRegion(text="t", x=0, y=0, width=4, height=4)])
    block = spatial_text_of(observation)
    assert block["substrate"] == "ocr:cortex_text_ocr"
    assert block["substrate_merged"] == 1 and block["substrate_deduped"] == 1
    assert [entry["text"] for entry in block["regions"]] == ["t", "novel"]


# --- merge primitive unit pins (the §4 semantics behind tests 2/3/6) ---------------------------


def test_a3_merge_dedupe_semantics_and_verdict_record() -> None:
    """Positive-area overlap dedupes (zero-area touch does NOT — the W5.1 precedent);
    UIA order is preserved and side regions append after; the verdict record duck-types
    the backend's CoordinateVerdict shape (backend.py:742-749)."""
    uia = [TextRegion(text="a", x=0, y=0, width=10, height=10)]
    touching = TextRegion(text="touch", x=10, y=0, width=10, height=10)  # shares only an edge
    overlapping = TextRegion(text="dup", x=5, y=5, width=10, height=10)
    disjoint = TextRegion(text="new", x=50, y=50, width=4, height=4)
    assert boxes_overlap(uia[0], overlapping) and not boxes_overlap(uia[0], touching)
    merged, deduped = text_substrates.merge_substrate_regions(
        uia, [touching, overlapping, disjoint]
    )
    assert [region.text for region in merged] == ["a", "touch", "new"]
    assert deduped == 1
    verdict = SubstrateVerdict(space="verified_passthrough", scale_x=1.5, scale_y=1.5)
    assert (verdict.space, verdict.scale_x, verdict.scale_y) == (
        "verified_passthrough", 1.5, 1.5
    )
