"""Build the Cortex pointer sprites from the designer originals (ORVEX-2026-109 WP-C1).

Run as a script (Pillow only):

    python tools/cursor/build_cursor.py <source-folder> <output-folder>

Expects ``Cortex Cursor Normal.png`` and ``Cortex Cursor Pressed.png`` (the
READ-ONLY designer originals, 1164x1351 RGBA) in the source folder. For each
state it:

1. trims the image to its opaque bbox (alpha > ``ALPHA_THRESHOLD``);
2. derives the hotspot — the TOPMOST opaque pixel, then the LEFTMOST among
   those (the arrow tip);
3. places Normal and Pressed on shared canvases aligned so their hotspots
   coincide (each canvas is exactly as large as the union needs);
4. emits the sizes ``SIZES`` (sprite HEIGHT in px) with LANCZOS resampling as
   ``normal_<n>.png`` / ``pressed_<n>.png``, plus ``cursor.json``:

   ``{"sizes": [...], "hotspot": {"<n>": [hx, hy]}, "source_sha256": {...}}``

``hotspot[n]`` is the hotspot position INSIDE the ``<n>``-high sprites of BOTH
states (identical by construction), measured from the sprite's top-left.

The originals are opened read-only and never written.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from PIL import Image

#: Alpha strictly above this counts as opaque (per WP-C1).
ALPHA_THRESHOLD = 8

#: Sprite heights (the whole sprite is scaled to this height, width follows
#: the aspect ratio).
SIZES = (16, 20, 24, 32, 48)

NORMAL_FILE = "Cortex Cursor Normal.png"
PRESSED_FILE = "Cortex Cursor Pressed.png"

ALPHA_CHANNEL = 3


def _opaque_bbox(image: Image.Image) -> tuple[int, int, int, int]:
    """The (left, top, right, bottom) bbox of pixels with alpha > ALPHA_THRESHOLD."""
    alpha = image.split()[ALPHA_CHANNEL]
    mask = alpha.point(lambda value: 255 if value > ALPHA_THRESHOLD else 0)
    return mask.getbbox()


def _hotspot(image: Image.Image) -> tuple[int, int]:
    """The topmost opaque pixel, then the leftmost among those (the arrow tip)."""
    alpha = image.split()[ALPHA_CHANNEL]
    width, height = alpha.size
    pixels = alpha.load()
    for y in range(height):
        for x in range(width):
            if pixels[x, y] > ALPHA_THRESHOLD:
                return x, y
    raise ValueError("no opaque pixel found; cannot derive a hotspot")


def _load_trimmed(path: Path) -> tuple[Image.Image, tuple[int, int]]:
    """Load one original, trim to the opaque bbox, return (sprite, hotspot-in-sprite)."""
    original = Image.open(path)
    original.load()
    sprite = original.convert("RGBA")
    box = _opaque_bbox(sprite)
    if box is None:
        raise ValueError(f"{path.name}: no opaque pixels")
    sprite = sprite.crop(box)
    hx, hy = _hotspot(sprite)
    return sprite, (hx, hy)


def build(source_folder: Path, output_folder: Path) -> dict:
    """Build every sprite + ``cursor.json``; returns the manifest written."""
    normal, normal_hotspot = _load_trimmed(source_folder / NORMAL_FILE)
    pressed, pressed_hotspot = _load_trimmed(source_folder / PRESSED_FILE)

    # Shared canvases: both states aligned so their hotspots coincide. The
    # canvas origin is the common hotspot; each sprite's top-left is its
    # hotspot minus the canvas origin.
    canvas_size = (
        max(normal.width - normal_hotspot[0], pressed.width - pressed_hotspot[0])
        + max(normal_hotspot[0], pressed_hotspot[0]),
        max(normal.height - normal_hotspot[1], pressed.height - pressed_hotspot[1])
        + max(normal_hotspot[1], pressed_hotspot[1]),
    )
    states = {
        "normal": (normal, normal_hotspot),
        "pressed": (pressed, pressed_hotspot),
    }

    output_folder.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"sizes": list(SIZES), "hotspot": {}, "source_sha256": {}}
    for size in SIZES:
        # Scale factor from the shared canvas height to the target sprite
        # height — the SAME factor for both states so the shared canvas scales
        # uniformly and the hotspots stay coincident.
        factor = size / canvas_size[1]
        scaled_size = (max(1, round(canvas_size[0] * factor)), size)
        hotspot = (round(max(normal_hotspot[0], pressed_hotspot[0]) * factor), round(max(normal_hotspot[1], pressed_hotspot[1]) * factor))
        manifest["hotspot"][str(size)] = [hotspot[0], hotspot[1]]
        for state, (sprite, sprite_hotspot) in states.items():
            canvas = Image.new("RGBA", scaled_size, (0, 0, 0, 0))
            scaled_sprite = sprite.resize(
                (max(1, round(sprite.width * factor)), max(1, round(sprite.height * factor))),
                Image.LANCZOS,
            )
            paste_at = (
                hotspot[0] - round(sprite_hotspot[0] * factor),
                hotspot[1] - round(sprite_hotspot[1] * factor),
            )
            canvas.alpha_composite(scaled_sprite, dest=paste_at)
            name = f"{state}_{size}.png"
            canvas.save(output_folder / name, format="PNG")

    manifest["source_sha256"] = {
        NORMAL_FILE: _sha256(source_folder / NORMAL_FILE),
        PRESSED_FILE: _sha256(source_folder / PRESSED_FILE),
    }
    (output_folder / "cursor.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: python tools/cursor/build_cursor.py <source-folder> <output-folder>",
            file=sys.stderr,
        )
        return 2
    manifest = build(Path(argv[1]), Path(argv[2]))
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
