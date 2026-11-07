"""The Cortex pointer sprite compositor (ORVEX-2026-109 WP-C3, DELTA 1).

Draws the bundled Cortex cursor INTO the outbound images the agent receives —
and nowhere else: this module is pure Python + Pillow, it never touches the OS
cursor, never opens a window, and performs no user32 call of any kind. The
verification/digest path never imports it; the evidence store keeps the CLEAN
capture and composites on output (see ``server/fetch.py``).

``composite(image, x, y, state, scale)`` pastes the sprite with an alpha MASK
so its HOTSPOT (the arrow tip, baked into ``cursor.json`` at build time) lands
on ``(round(x), round(y))`` — coordinates in the image's OWN pixel space (i.e.
after any outbound scaling). ``scale`` is the outbound scale of that image
(``image_outbound_scale``; ``None``/non-positive treated as 1.0): the sprite
represents a constant amount of SCREEN content — its target height is
``TARGET_SPRITE_PX * scale`` IMAGE pixels (nearest shipped asset, floor 16) —
so a half-res image carries a 16 px arrow, not a 48 px one. ``in_image`` is
True ONLY when the hotspot itself lies inside the image: outside it, NOTHING
is drawn (not even a clipped partial sprite); inside it, the sprite is clipped
at the edges with the hotspot placement kept. The input image is never
mutated, and the pastes are mask-based — the frame never round-trips through a
full-frame RGBA convert.

A ``pressed`` sprite additionally gets a thin light/dark double-stroke ring
(radius ``PRESS_RING_RADIUS_PX * scale``, floor 6) centred on the click point,
drawn on a SMALL LOCAL RGBA patch and pasted with its own mask — never a
full-frame overlay.
"""

from __future__ import annotations

import io
import json
import threading
from typing import Any

from PIL import Image, ImageDraw

#: Package-relative asset directory (built by ``tools/cursor/build_cursor.py``).
_ASSET_PACKAGE = "computer_use_mcp"
_ASSET_DIR = ("assets", "cursor")
_MANIFEST_NAME = "cursor.json"

#: The sprite target height in IMAGE px at outbound scale 1.0 — i.e. the amount
#: of SCREEN content the arrow represents. Scaled by ``scale`` (24 * scale),
#: nearest shipped asset size, never below the 16 px floor (DELTA 1 M1).
TARGET_SPRITE_PX = 24.0

#: The pressed-state ring radius in IMAGE px at scale 1.0 — ``10 * scale``,
#: floor 6 (DELTA 1 M1).
PRESS_RING_RADIUS_PX = 10.0

#: Ring stroke colors — dark + light double stroke so it reads on any background.
_RING_DARK = (10, 10, 10, 210)
_RING_LIGHT = (255, 255, 255, 230)

_LOCK = threading.Lock()
_CACHE: dict[str, Any] | None = None


def _load_assets() -> dict[str, Any]:
    """Load sprites + manifest once (lazy; lock-free fast path, DELTA 1 nit)."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    with _LOCK:
        if _CACHE is not None:
            return _CACHE
        from importlib import resources

        root = resources.files(_ASSET_PACKAGE).joinpath(*_ASSET_DIR)
        manifest: dict[str, Any] = json.loads(root.joinpath(_MANIFEST_NAME).read_bytes())
        sizes = tuple(int(size) for size in manifest["sizes"])
        hotspots = {
            int(size): (int(hx), int(hy))
            for size, (hx, hy) in (
                (key, value) for key, value in manifest["hotspot"].items()
            )
        }
        sprites: dict[str, dict[int, Image.Image]] = {"normal": {}, "pressed": {}}
        for size in sizes:
            for state in ("normal", "pressed"):
                raw = root.joinpath(f"{state}_{size}.png").read_bytes()
                sprite = Image.open(io.BytesIO(raw))
                sprite.load()
                sprites[state][size] = sprite.convert("RGBA")
        _CACHE = {"sizes": sizes, "hotspots": hotspots, "sprites": sprites}
        return _CACHE


def pick_size(scale: float | None) -> int:
    """The asset size for one outbound scale: ``TARGET_SPRITE_PX * scale`` image
    px (a constant amount of screen content), nearest available asset size,
    never below 16 (DELTA 1 M1)."""
    assets = _load_assets()
    available = assets["sizes"]
    effective = float(scale) if scale is not None and scale > 0 else 1.0
    wanted = max(TARGET_SPRITE_PX * effective, 16.0)
    return min(available, key=lambda size: (abs(size - wanted), size))


def _ring_radius(scale: float | None) -> int:
    effective = float(scale) if scale is not None and scale > 0 else 1.0
    return max(6, round(PRESS_RING_RADIUS_PX * effective))


def _paste_masked(image: Image.Image, piece: Image.Image, box: tuple[int, int]) -> Image.Image:
    """Paste ``piece`` (RGBA) onto ``image`` with its alpha as the MASK — no
    full-frame RGBA round-trip; ``image`` is a copy owned by the caller."""
    if image.mode == "RGBA" and piece.mode == "RGBA":
        image.alpha_composite(piece, dest=box)
    else:
        image.paste(piece, box, piece)
    return image


def _draw_ring_patched(
    image: Image.Image, cx: int, cy: int, radius: int
) -> None:
    """Light/dark double-stroke circle centred on ``(cx, cy)``, drawn on a SMALL
    LOCAL RGBA patch and pasted with its own alpha mask (DELTA 1 M6: never a
    full-frame RGBA overlay). ``image`` is a copy owned by the caller."""
    dark_radius = radius + 1
    bounds = image.size
    if (
        cx + dark_radius < 0
        or cy + dark_radius < 0
        or cx - dark_radius >= bounds[0]
        or cy - dark_radius >= bounds[1]
    ):
        return
    pad = 2
    patch_l = max(0, cx - dark_radius - pad)
    patch_t = max(0, cy - dark_radius - pad)
    patch_r = min(bounds[0], cx + dark_radius + pad + 1)
    patch_b = min(bounds[1], cy + dark_radius + pad + 1)
    if patch_r <= patch_l or patch_b <= patch_t:
        return
    patch = Image.new("RGBA", (patch_r - patch_l, patch_b - patch_t), (0, 0, 0, 0))
    draw = ImageDraw.Draw(patch)
    local_cx = cx - patch_l
    local_cy = cy - patch_t
    draw.ellipse(
        (local_cx - dark_radius, local_cy - dark_radius,
         local_cx + dark_radius, local_cy + dark_radius),
        outline=_RING_DARK,
        width=2,
    )
    draw.ellipse(
        (local_cx - radius, local_cy - radius, local_cx + radius, local_cy + radius),
        outline=_RING_LIGHT,
        width=1,
    )
    _paste_masked(image, patch, (patch_l, patch_t))


def composite(
    image: Image.Image,
    x: float,
    y: float,
    state: str = "normal",
    scale: float | None = 1.0,
) -> tuple[Image.Image, bool]:
    """Paste the pointer sprite so its HOTSPOT lands on ``(round(x), round(y))``.

    ``(x, y)`` are in ``image``'s own pixel space (after outbound scaling);
    ``scale`` is that image's outbound scale (drives the sprite size AND the
    ring radius; ``None`` means 1.0). Returns ``(image, in_image)``:

    - ``in_image`` is True ONLY when the hotspot itself lies inside the image
      (``0 <= round(x) < width`` and ``0 <= round(y) < height``). Outside it,
      NOTHING is drawn — the original image comes back untouched, not even a
      clipped partial sprite (DELTA 1 M2/M3).
    - Inside it, the sprite is clipped at the edges with the hotspot placement
      kept (a sprite hanging over the border draws its visible part).

    ``state="pressed"`` additionally draws the small double-stroke ring centred
    on the click point. The input image is never mutated; all pastes are
    alpha-mask based on the copy, so the frame never round-trips through a
    full-frame RGBA convert (DELTA 1 M6).
    """
    assets = _load_assets()
    anchor_x = round(float(x))
    anchor_y = round(float(y))
    if not (0 <= anchor_x < image.width and 0 <= anchor_y < image.height):
        return image, False
    size = pick_size(scale)
    sprite = assets["sprites"]["pressed" if state == "pressed" else "normal"][size]
    hx, hy = assets["hotspots"][size]
    left = anchor_x - hx
    top = anchor_y - hy
    # Clip the sprite to the canvas; the crop offset keeps the hotspot fixed.
    crop_left = max(0, -left)
    crop_top = max(0, -top)
    paste_left = max(0, left)
    paste_top = max(0, top)
    visible_w = min(sprite.width - crop_left, image.width - paste_left)
    visible_h = min(sprite.height - crop_top, image.height - paste_top)
    out = image.copy()
    piece = sprite.crop(
        (crop_left, crop_top, crop_left + visible_w, crop_top + visible_h)
    )
    _paste_masked(out, piece, (paste_left, paste_top))
    if state == "pressed":
        _draw_ring_patched(out, anchor_x, anchor_y, _ring_radius(scale))
    return out, True
