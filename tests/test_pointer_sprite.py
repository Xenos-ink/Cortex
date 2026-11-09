"""WP-C3 (ORVEX-2026-109): the pointer-sprite compositor unit contract.

``vision/pointer_sprite.composite`` pastes the bundled Cortex cursor so its
HOTSPOT lands on the requested pixel (<= 1 px error at any outbound scale),
sizes the sprite from the scale (~24 screen px, never below 16), clips a
partly off-edge sprite, draws nothing for an off-image point (``in_image`` is
False), adds the pressed-state double-stroke ring, and never mutates the
input image.
"""

from __future__ import annotations

import io

from PIL import Image, ImageChops

from computer_use_mcp.vision.pointer_sprite import composite, pick_size


def _canvas(width: int = 600, height: int = 400, color: str = "white") -> Image.Image:
    return Image.new("RGB", (width, height), color)


def _bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _diff_bbox(before: Image.Image, after: Image.Image) -> tuple[int, int, int, int]:
    """The bbox of pixels the composite changed (exact top-left = the sprite tip)."""
    difference = ImageChops.difference(
        before.convert("RGB"), after.convert("RGB")
    ).convert("L")
    return difference.point(lambda value: 255 if value > 0 else 0).getbbox()


def test_hotspot_placement_error_within_one_px_at_all_scales() -> None:
    """The tip (topmost opaque sprite pixel) lands on (round(x), round(y)) within
    1 px at scales 1.0 / 0.5 / 0.37 / 2.0 (the brief's scale set)."""
    for scale in (1.0, 0.5, 0.37, 2.0):
        for anchor in ((200, 150), (77, 333), (412, 96)):
            before = _canvas()
            after, in_image = composite(before, anchor[0], anchor[1], "normal", scale)
            assert in_image, f"anchor {anchor} scale {scale} must draw"
            bbox = _diff_bbox(before, after)
            assert bbox is not None
            tip_x, tip_y = bbox[0], bbox[1]
            assert abs(tip_x - round(anchor[0])) <= 1 and abs(tip_y - round(anchor[1])) <= 1, (
                f"tip {(tip_x, tip_y)} != anchor {anchor} at scale {scale}"
            )


def test_pressed_differs_from_normal_and_adds_the_ring() -> None:
    """Pressed = the sprite PLUS the double-stroke ring: strictly more changed
    pixels than the normal sprite at the same anchor/scale."""
    before = _canvas()
    normal, in_normal = composite(before, 200, 150, "normal", 1.0)
    pressed, in_pressed = composite(before, 200, 150, "pressed", 1.0)
    assert in_normal and in_pressed
    assert _bytes(normal) != _bytes(pressed)
    normal_bbox = _diff_bbox(before, normal)
    pressed_bbox = _diff_bbox(before, pressed)
    assert pressed_bbox is not None and normal_bbox is not None
    # The ring (radius ~10) extends the changed area strictly UP-LEFT of the
    # anchor (the sprite itself grows down-right from the tip, and the pressed
    # source art has a slightly different opaque extent), so the ring is the
    # only explanation for the extra up-left reach; the bytes also differ.
    assert pressed_bbox[0] < normal_bbox[0] and pressed_bbox[1] < normal_bbox[1]


def test_off_image_draws_nothing_and_reports_in_image_false() -> None:
    """A hotspot outside the image: no draw AT ALL (not even a clipped partial
    sprite), in_image=False, untouched bytes (DELTA 1 M2/M3)."""
    before = _canvas(width=100, height=100)
    for anchor in ((10000, 10000), (-500, -500), (250, -50), (-50, 50), (100, 50), (50, 100), (0, -1), (-1, 0)):
        after, in_image = composite(before, anchor[0], anchor[1], "normal", 1.0)
        assert in_image is False, f"hotspot {anchor} is outside the image"
        assert _bytes(after) == _bytes(before), f"hotspot {anchor} must not draw"


def test_hotspot_just_outside_each_edge_skips_even_when_the_sprite_would_overlap() -> None:
    """DELTA 1 M3: the arrow body extends down-right from the tip, so a hotspot
    just LEFT of or ABOVE the image would still overlap — nothing may draw."""
    before = _canvas(width=100, height=100)
    for anchor in ((-1, 30), (30, -1), (100, 30), (30, 100)):
        after, in_image = composite(before, anchor[0], anchor[1], "pressed", 1.0)
        assert in_image is False, f"hotspot {anchor} just outside the edge"
        assert _bytes(after) == _bytes(before), f"hotspot {anchor} must not draw"


def test_partly_off_edge_sprite_is_clipped_and_keeps_the_hotspot() -> None:
    """A sprite hanging over the edge is clipped; the visible tip stays on the
    anchor (the hotspot never drifts)."""
    before = _canvas(width=100, height=100)
    for anchor in ((3, 3), (0, 50)):
        after, in_image = composite(before, anchor[0], anchor[1], "normal", 1.0)
        assert in_image is True
        bbox = _diff_bbox(before, after)
        assert bbox is not None
        assert abs(bbox[0] - round(anchor[0])) <= 1 and abs(bbox[1] - round(anchor[1])) <= 1, (
            f"clipped tip {(bbox[0], bbox[1])} != anchor {anchor}"
        )


def test_pick_size_scale_rules() -> None:
    """DELTA 1 M1: the sprite represents a constant amount of SCREEN content —
    the target height is 24 * scale IMAGE px (nearest shipped asset, floor 16)."""
    assert pick_size(1.0) == 24
    assert pick_size(None) == 24  # None means 1.0
    assert pick_size(0.5) == 16  # 24 * 0.5 = 12 -> clamped to the 16 px floor
    assert pick_size(0.37) == 16  # 24 * 0.37 ~ 8.9 -> clamped to 16
    assert pick_size(2.0) == 48  # 24 * 2 = 48 -> the biggest shipped asset
    assert pick_size(4.0) == 48  # 96 -> nearest available is the 48 px cap
    assert pick_size(0.0) == 24  # degenerate scale treated as 1.0


def test_ring_radius_scales_with_the_image() -> None:
    """DELTA 1 M1: the pressed ring is 10 * scale image px (floor 6)."""
    from computer_use_mcp.vision.pointer_sprite import _ring_radius

    assert _ring_radius(1.0) == 10
    assert _ring_radius(0.5) == 6  # 10 * 0.5 = 5 -> floor 6
    assert _ring_radius(2.0) == 20
    assert _ring_radius(None) == 10


def test_composite_never_mutates_the_input_image() -> None:
    """The caller's bytes are untouched (the outbound copy is a new object)."""
    before = _canvas()
    snapshot = _bytes(before)
    composite(before, 200, 150, "pressed", 1.0)
    composite(before, 0, 0, "normal", 1.0)
    composite(before, 9999, 9999, "normal", 1.0)
    assert _bytes(before) == snapshot


def test_composite_preserves_the_input_mode() -> None:
    """An RGB canvas stays RGB; an L canvas stays L (mode-faithful outbound copy)."""
    rgb = composite(_canvas(), 100, 100, "normal", 1.0)[0]
    assert rgb.mode == "RGB"
    gray = Image.new("L", (200, 200), 128)
    out, in_image = composite(gray, 100, 100, "pressed", 1.0)
    assert in_image and out.mode == "L"
