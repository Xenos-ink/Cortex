"""WP-C1 (ORVEX-2026-109): the bundled pointer sprites and their manifest.

The generated assets live in ``computer_use_mcp/assets/cursor/`` (built by
``tools/cursor/build_cursor.py`` from the READ-ONLY designer originals in
``D:/Cortex/Cursor``). These tests pin the shipping contract: every size for
both states, coincident hotspots, an opaque tip at the hotspot, transparent
non-arrow regions, and a manifest that matches the files.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.resources
import io
import json
from typing import Any

import pytest
from desktop_tripwire import LIVE_DESKTOP
from PIL import Image

EXPECTED_SIZES = (16, 20, 24, 32, 48)
STATES = ("normal", "pressed")

#: Alpha strictly above this counts as opaque (build_cursor.py's threshold).
ALPHA_THRESHOLD = 8


def _cursor_dir() -> Any:
    """The asset directory as an importlib.resources Traversable (DELTA 1
    MINOR 7: never wrapped in ``Path(...)``, so a zipped install reads too)."""
    return importlib.resources.files("computer_use_mcp").joinpath("assets", "cursor")


def _load_manifest() -> dict:
    return json.loads((_cursor_dir() / "cursor.json").read_text(encoding="utf-8"))


def _load_sprite(state: str, size: int) -> Image.Image:
    sprite = Image.open(_cursor_dir() / f"{state}_{size}.png")
    sprite.load()
    assert sprite.mode == "RGBA"
    return sprite


def _assert_tip_opaque(sprite: Image.Image, hx: int, hy: int) -> None:
    """The hotspot pixel or its 1-px neighbourhood has a fully opaque pixel."""
    pixels = sprite.load()
    width, height = sprite.size
    neighbourhood = [
        (hx + dx, hy + dy)
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        if 0 <= hx + dx < width and 0 <= hy + dy < height
    ]
    assert neighbourhood, f"hotspot {(hx, hy)} is outside the {sprite.size} sprite"
    assert any(pixels[x, y][3] > ALPHA_THRESHOLD for x, y in neighbourhood), (
        f"no opaque pixel at/around the hotspot {(hx, hy)} in a {sprite.size} sprite"
    )


def _assert_top_row_outside_arrow_transparent(sprite: Image.Image, hx: int) -> None:
    """The top row is transparent except a small run at the tip (the arrow slopes
    away down-right; the tip is the TOPMOST opaque pixel, so the top row must be
    opaque only in the tip's immediate neighbourhood)."""
    top = sprite.crop((0, 0, sprite.width, 1))
    alpha = top.split()[3].load()
    opaque_columns = [x for x in range(sprite.width) if alpha[x, 0] > ALPHA_THRESHOLD]
    assert opaque_columns, "the top row has no opaque tip pixel"
    assert opaque_columns[-1] - opaque_columns[0] <= 4, (
        f"the top row is opaque across {opaque_columns[-1] - opaque_columns[0]} px — "
        "non-arrow pixels at the top-left region are not transparent"
    )
    assert hx in range(opaque_columns[0], opaque_columns[-1] + 1), (
        "the reported hotspot column is not inside the top-row tip"
    )


def test_every_size_exists_for_both_states() -> None:
    manifest = _load_manifest()
    assert sorted(manifest["sizes"]) == sorted(EXPECTED_SIZES)
    for state in STATES:
        for size in EXPECTED_SIZES:
            sprite = _load_sprite(state, size)
            assert sprite.height == size, (
                f"{state}_{size}.png must be {size} px tall, got {sprite.height}"
            )


def test_hotspot_identical_for_both_states_at_every_size() -> None:
    manifest = _load_manifest()
    for size in EXPECTED_SIZES:
        entry = manifest["hotspot"][str(size)]
        assert isinstance(entry, list) and len(entry) == 2
        hx, hy = int(entry[0]), int(entry[1])
        assert 0 <= hx and 0 <= hy
        # Both states share ONE manifest hotspot (coincident by construction);
        # each sprite's own topmost-leftmost opaque pixel must sit there.
        for state in STATES:
            sprite = _load_sprite(state, size)
            alpha = sprite.split()[3]
            topmost = None
            for y in range(sprite.height):
                alpha_row = alpha.load()
                columns = [
                    x for x in range(sprite.width) if alpha_row[x, y] > ALPHA_THRESHOLD
                ]
                if columns:
                    topmost = (columns[0], y)
                    break
            assert topmost == (hx, hy), (
                f"{state}_{size}.png tip {topmost} != manifest hotspot {(hx, hy)}"
            )


def test_hotspot_opaque_and_non_arrow_top_left_transparent() -> None:
    manifest = _load_manifest()
    for size in EXPECTED_SIZES:
        hx, hy = manifest["hotspot"][str(size)]
        for state in STATES:
            sprite = _load_sprite(state, size)
            _assert_tip_opaque(sprite, hx, hy)
            _assert_top_row_outside_arrow_transparent(sprite, hx)


def test_manifest_matches_the_files() -> None:
    manifest = _load_manifest()
    directory = _cursor_dir()
    assert sorted(manifest["hotspot"]) == [str(size) for size in EXPECTED_SIZES]
    expected_files = {
        *(f"{state}_{size}.png" for state in STATES for size in EXPECTED_SIZES),
        "cursor.json",
    }
    present = {p.name for p in directory.iterdir() if p.is_file()}
    assert present == expected_files, (
        f"asset directory holds {sorted(present)}, expected {sorted(expected_files)}"
    )
    # Every listed file decodes as a PNG of the right height (manifest sizes
    # agree with the bytes on disk, not just the names).
    for size in EXPECTED_SIZES:
        for state in STATES:
            raw = (directory / f"{state}_{size}.png").read_bytes()
            image = Image.open(io.BytesIO(raw))
            image.load()
            assert image.format == "PNG"
            assert image.height == size
            assert image.mode == "RGBA"


def test_manifest_source_hashes_match_the_originals() -> None:
    """The recorded source hashes match the designer originals — SKIPPED on any
    machine where the source folder is absent (the hashes pin the BUILD input,
    not the shipping assets)."""
    from pathlib import Path

    source = Path("D:/Cortex/Cursor")
    if not source.is_dir():
        pytest.skip(f"{source} is absent; source-hash pinning is a build-machine check")
    manifest = _load_manifest()
    assert set(manifest["source_sha256"]) == {
        "Cortex Cursor Normal.png",
        "Cortex Cursor Pressed.png",
    }
    for name, recorded in manifest["source_sha256"].items():
        actual = hashlib.sha256((source / name).read_bytes()).hexdigest()
        assert recorded == actual, f"{name}: manifest hash does not match the original"


def test_sprites_are_valid_png_payloads_for_the_compositor() -> None:
    """The compositor loads sprites from package resources; every sprite must be
    a decodable RGBA PNG reachable through importlib.resources (not the CWD)."""
    resource = importlib.resources.files("computer_use_mcp").joinpath(
        "assets", "cursor", "normal_24.png"
    )
    raw = resource.read_bytes()
    image = Image.open(io.BytesIO(raw))
    image.load()
    assert image.mode == "RGBA"
    assert image.height == 24
    # Sanity: the bytes really are a PNG (the compositor decodes base64-free).
    assert base64.b64encode(raw)[:4] == base64.b64encode(b"\x89PNG")[:4]


@pytest.mark.slow
@LIVE_DESKTOP  # skipped offline: the tripwire forbids subprocess launches (see below)
def test_wheel_build_ships_the_cursor_assets(tmp_path: Any) -> None:
    """DELTA 1 MINOR 7 (verified, not plausible): ``uv build --wheel`` into a
    temp dir, and the wheel's file list contains every cursor sprite plus
    cursor.json.

    Gated behind the shared LIVE_DESKTOP marker: the hermetic tripwire forbids
    subprocess launches in the offline suite (an allow-list we must not touch),
    so this smoke test SKIPS in every offline gate run and runs only under
    ``CUMCP_RUN_E2E=1`` on a build machine. Also skipped when uv is absent."""
    import shutil
    import subprocess
    import zipfile
    from pathlib import Path

    if shutil.which("uv") is None:
        pytest.skip("uv is absent; the wheel smoke test is a build-machine check")
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(Path(tmp_path).glob("*.whl"))
    assert wheels, "uv built no wheel"
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
    cursor_entries = [name for name in names if "/assets/cursor/" in name]
    assert any(name.endswith("assets/cursor/cursor.json") for name in cursor_entries)
    pngs = {name.rsplit("/", 1)[-1] for name in cursor_entries if name.endswith(".png")}
    expected = {
        f"{state}_{size}.png" for state in STATES for size in EXPECTED_SIZES
    }
    assert expected <= pngs, f"missing sprites in the wheel: {sorted(expected - pngs)}"
