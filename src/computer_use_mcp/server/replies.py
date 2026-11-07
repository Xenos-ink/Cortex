"""The outbound reply surface: redaction sinks, the outbound image budget ladder, and the executed-response content blocks (LAYER 1)."""

from __future__ import annotations

import base64
import io
import json
import os
from typing import Any

from mcp.types import ImageContent, TextContent
from PIL import Image

from ..planning import plan as plan_layer
from ..security.redaction import redact_text
from ..vision.visual_views import outbound_view_scale
from .instance import logger



def _redact_result_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """F2: the MCP response path is redacted too (defense in depth).

    The audit sink and the provider payload are already redaction-enforced; until now
    the tool RESPONSE was the one unredacted surface (action text/reason, result
    messages, verification notes/evidence echo provider-proposed strings verbatim).
    The calling client supplied the goal, so this is same-party data — but the
    response path must not be the surface that bypasses redaction.
    """
    if payload.get("message"):
        payload["message"] = redact_text(str(payload["message"]))[0]
    action = payload.get("action")
    if isinstance(action, dict):
        if action.get("text"):
            action["text"] = redact_text(str(action["text"]))[0]
        if action.get("reason"):
            action["reason"] = redact_text(str(action["reason"]))[0]
    verification = payload.get("verification")
    if isinstance(verification, dict):
        if verification.get("note"):
            verification["note"] = redact_text(str(verification["note"]))[0]
        evidence = verification.get("evidence")
        if isinstance(evidence, list):
            verification["evidence"] = [redact_text(str(item))[0] for item in evidence]
    return payload


def _redact_queue_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """F2 defense-in-depth for PERF-004 C7 queue entries: redact text at the sink.

    Delegates to the plan layer's ``redact_queue_entry`` so the flat-queue and plan
    sinks cannot drift.
    """
    return plan_layer.redact_queue_entry(entry)


# --- REM-A outbound image budget + content-block parity (master-mission Phase 2) ---------------

#: Default budget for ONE outbound image block, in KB (H7). The host inline limit is
#: ~200KB; the default keeps the encoded image comfortably under it.
RESULT_IMAGE_MAX_KB_DEFAULT = 180

#: Env knob name: ``CORTEX_RESULT_IMAGE_MAX_KB`` bounds the OUTBOUND image only —
#: observe and execute paths share the same budget; internal captures,
#: verification pixel-diff, and checkpoints keep PNG exactly as today.
RESULT_IMAGE_MAX_KB_ENV = "CORTEX_RESULT_IMAGE_MAX_KB"

#: Cap on ``ocr_text``/``ui_elements`` entries serialized into the observe metadata
#: text (H8): the internal Observation model keeps the full lists.
OBSERVE_METADATA_ELEMENT_CAP = 20


def _result_image_max_bytes() -> int:
    """Resolve the outbound image budget in BYTES; invalid values fall back to default."""
    raw = os.environ.get(RESULT_IMAGE_MAX_KB_ENV, "").strip()
    try:
        kb = float(raw) if raw else RESULT_IMAGE_MAX_KB_DEFAULT
    except (TypeError, ValueError):
        kb = RESULT_IMAGE_MAX_KB_DEFAULT
    if kb <= 0 or kb != kb:  # non-positive or NaN -> fail-safe default
        kb = RESULT_IMAGE_MAX_KB_DEFAULT
    kb = min(kb, 100_000.0)
    return int(kb * 1024)


def _outbound_image_scale(
    served_width: int, served_height: int, source_width: int, source_height: int
) -> float | None:
    """The linear scale an OUTBOUND copy was served at (§1.1 D-3, measured).

    Mirrors the AL-002 reference implementation
    (:func:`computer_use_mcp.visual_views.outbound_view_scale`) — the same ``min`` of
    the two axis ratios and the same 4-decimal rounding. §1.1 (MINOR-b, Commander
    decision): ``None`` means NO downscale was applied (raw pass-through, bytes served
    as captured); a float is returned only for a real downscale (always < 1). No pixel
    decode: the caller passes the dimensions the ladder already had in hand.
    """
    if source_width <= 0 or source_height <= 0:
        return None
    ratio = round(min(served_width / source_width, served_height / source_height), 4)
    return None if ratio >= 1.0 else ratio


#: The ONLY outbound-truth scale key on every image-bearing reply (ORVEX-2026-108
#: §1.1 D-3 — one key, one meaning; the legacy ``image_scale`` key is deleted). It
#: carries the scale the image ladder actually applied to the delivered copy, so a
#: caller can map a coordinate read off the served picture back to the capture.
#: Deliberately NOT ``visual_view_scale``: that key belongs to AL-002 and means
#: "the scale of the DERIVED VIEW the host explicitly asked for" (``None`` for a
#: pass-through raw view) — reusing it here would change an existing key's meaning.
IMAGE_OUTBOUND_SCALE_KEY = "image_outbound_scale"


def _bound_outbound_image(data_b64: str, frame: Any = None) -> tuple[str, str, float | None]:
    """Return ``(base64, mimeType, appliedScale)`` for the OUTBOUND copy, under the
    size budget (§1.1 D-3: the scale is REPORTED, not discarded).

    H7: the outbound PNG travels as-is while it fits the budget; an oversized PNG is
    re-encoded as JPEG (quality 85, no alpha channel) and, if still over budget,
    progressively downscaled by 0.85 steps until it fits. INTERNAL pipeline bytes are
    never touched: captures, verification pixel-diff, and checkpoints keep PNG exactly
    as today. Degradation (PIL failure) returns the original — an oversized real image
    beats none.

    The ladder's arithmetic and the bytes it produces are UNCHANGED by the third return
    value: the scale is derived from the dimensions the loop already resizes through
    (no new maths, no second encode, no pixel decode). Third value semantics
    (§1.1 MINOR-b + N3): ``float | None`` — ``None`` when the bytes traveled untouched
    (raw pass-through, no downscale applied), a float < 1 only when the ladder actually
    downscaled. No path returns ``1.0``.

    R-5 (W4): ``frame`` may carry the observation's capture-time RGB frame (the
    backend's private stash). When the PNG is over budget the JPEG ladder then starts
    from that frame instead of re-decoding the PNG that was just encoded; the ladder's
    outputs are byte-identical (same source pixels, same qualities/scales), and a
    PNG-under-budget payload is returned untouched on both paths. The ``frame``
    argument never changes the INTERNAL bytes — only the outbound copy's encode cost.
    """
    try:
        decoded = base64.b64decode(data_b64, validate=True)
    except Exception:  # noqa: BLE001 - unusable environment is fail-closed data
        return data_b64, "image/png", None
    if len(decoded) <= _result_image_max_bytes():
        # §1.1 MINOR-b: the bytes travel untouched — a raw PASS-THROUGH, so the
        # honest report is None (no downscale applied), never 1.0.
        return data_b64, "image/png", None
    try:
        if (
            frame is not None
            and isinstance(frame, Image.Image)
            and frame.mode in ("RGB", "L")
        ):
            image = frame.copy()  # independent object: the ladder mutates via resize
        else:
            image = Image.open(io.BytesIO(decoded))
            image.load()
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
        source_size = (image.width, image.height)
        # Ladder: JPEG q85, then progressive 0.85 downscale; after exhausting the
        # scale steps, quality descent (85 -> 70 -> 55 -> 40 -> 25) on the smallest
        # frame. The ladder terminates well before pixel collapse; the floor path
        # only exists so a pathological image still yields SOMETHING legible.
        qualities = (85, 70, 55, 40, 25)
        last_bytes = b""
        for step in range(17):  # 12 scale steps (0.85^12 ≈ 0.14x area) + quality descent
            quality = qualities[0] if step < 12 else qualities[min(step - 11, len(qualities) - 1)]
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality)
            last_bytes = buffer.getvalue()
            if len(last_bytes) <= _result_image_max_bytes():
                break
            if step < 12:
                image = image.resize(  # progressive 0.85 downscale until it fits
                    (max(1, int(image.width * 0.85)), max(1, int(image.height * 0.85))),
                    Image.LANCZOS,
                )
        applied_scale = _outbound_image_scale(image.width, image.height, *source_size)
        return base64.b64encode(last_bytes).decode("ascii"), "image/jpeg", applied_scale
    except Exception:  # noqa: BLE001 - degradation: deliver the original image
        logger.debug("outbound image re-encode failed; delivering original PNG", exc_info=True)
        return data_b64, "image/png", None


# --- default action-response image weight ---------------------------------------------------

#: Env knob name: ``CORTEX_ACTION_IMAGE_FULL`` — set to exactly ``1`` to restore the
#: FULL-resolution default action-response image (the pre-0.5.7 behavior). Read lazily
#: (test-toggleable like ``CORTEX_DIFF_FAST``). Scoped to the executed
#: ``computer_execute`` response DEFAULT image only: ``computer_observe`` /
#: ``computer_screenshot`` keep their full-resolution budget path, an explicit
#: ``include_screenshot_after=true`` always ships full resolution, and D1 text-mode
#: semantics are untouched (no images either way).
ACTION_IMAGE_FULL_ENV = "CORTEX_ACTION_IMAGE_FULL"

#: Half-resolution scale and JPEG quality for the DEFAULT executed-response image
#: (action-image): 0.5 -> 960x540 at 1920x1080, quality ~60 — target <= ~60KB typical so a
#: driving model's per-action context stops growing by 150-230KB PNG per action.
_ACTION_IMAGE_SCALE = 0.5
_ACTION_IMAGE_JPEG_QUALITY = 60


def _action_image_full() -> bool:
    """Resolve the action-image full-res escape hatch (only the exact string ``1`` enables)."""
    return os.environ.get(ACTION_IMAGE_FULL_ENV, "").strip() == "1"


def _half_res_action_image(data_b64: str, frame: Any = None) -> tuple[str, str, float | None] | None:
    """Half-resolution JPEG encode of the executed response's default image.

    Downscale 0.5 (LANCZOS) then JPEG quality 60 — 960x540 at 1920x1080. Prefers the
    capture-time RGB ``frame`` (R-5 W4 stash) over re-decoding the PNG. The REM-A H7
    outbound budget still governs: an over-budget half-res result runs through
    :func:`_bound_outbound_image` (which keeps it JPEG and bounded). Returns ``None``
    on any encode failure so the caller falls back to the full-resolution budget
    ladder — a degraded real image beats none. The third value is the MEASURED applied
    scale (§1.1 D-3): the half-res ratio while the half-res encode fits the budget,
    the ladder's measured ratio when the budget ladder fires on top of it.
    """
    try:
        if frame is not None and isinstance(frame, Image.Image) and frame.mode in ("RGB", "L"):
            image = frame.copy()  # independent object: resize must not mutate the stash
        else:
            decoded = base64.b64decode(data_b64, validate=True)
            image = Image.open(io.BytesIO(decoded))
            image.load()
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        source_size = (image.width, image.height)
        image = image.resize(
            (
                max(1, int(image.width * _ACTION_IMAGE_SCALE)),
                max(1, int(image.height * _ACTION_IMAGE_SCALE)),
            ),
            Image.LANCZOS,
        )
        buffer = io.BytesIO()
        # optimize=True: optimal Huffman tables — free ~5-8% on real frames at the
        # same quality level (context bytes ARE the product here).
        image.save(buffer, format="JPEG", quality=_ACTION_IMAGE_JPEG_QUALITY, optimize=True)
        raw = buffer.getvalue()
        encoded = base64.b64encode(raw).decode("ascii")
        if len(raw) <= _result_image_max_bytes():
            scale = _outbound_image_scale(image.width, image.height, *source_size)
            return encoded, "image/jpeg", scale
        # H7 budget still governs the outbound copy even at half resolution; the
        # applied scale composes: half-res step x the budget ladder's own scale,
        # measured against the capture the encode started from. MINOR-N3
        # (VAL-WP1-02): None-safe composition — a None factor means "no downscale on
        # that step" (factor 1.0); the product reports None when NO downscale was
        # applied overall (e.g. a 1x1 source where the half-res step is a no-op).
        bounded_b64, mime, ladder_scale = _bound_outbound_image(encoded)
        half_scale = _outbound_image_scale(image.width, image.height, *source_size)
        composed = (half_scale or 1.0) * (ladder_scale or 1.0)
        return bounded_b64, mime, None if composed >= 1.0 else round(composed, 4)
    except Exception:  # noqa: BLE001 - degradation: fall back to the budget ladder
        logger.debug("half-res action-image encode failed; using the budget ladder", exc_info=True)
        return None


def _execute_response_blocks(
    response: dict[str, object],
    frame: Any = None,
    *,
    full_resolution: bool = False,
    view_id: str | None = None,
    view_payload: tuple[str, Any] | None = None,
) -> list[Any]:
    """REM-A H3/H5: executed ``computer_execute`` results as MCP content blocks.

    Returns one TextContent (the result JSON with the image blob stripped and the
    outbound format noted) plus one ImageContent carrying the post-action screenshot
    as a real image block — parity with ``computer_observe``. When the caller opted
    out (``include_screenshot_after=False``, honored BEFORE this point) the response
    carries no blob and this yields the text block only — never an empty image.
    ``frame`` (R-5 W4, optional) is the capture-time RGB frame for the outbound JPEG
    ladder (skips re-decoding the PNG; byte-identical outputs).

    ``full_resolution=False`` (the DEFAULT — ``include_screenshot_after``
    omitted) ships the image as a HALF-RESOLUTION JPEG (0.5x, q60; target <= ~60KB
    typical) so driver context stops growing by a 150-230KB PNG per action.
    ``full_resolution=True`` (an explicit ``include_screenshot_after=true`` — the
    documented full-image opt-in — or ``CORTEX_ACTION_IMAGE_FULL=1``) keeps the
    full-resolution ``_bound_outbound_image`` budget path unchanged.

    AL-002 (additive, W2): when the caller requested a ``visual_view``, ``view_id``
    names the served view and ``view_payload`` is the pre-derived
    ``(encoded_view_b64, derived_frame)`` pair (derived ONCE by the caller — I-4: the
    derivation consumed the stash or decoded the payload exactly once). The derived
    bytes ride the UNCHANGED stock bounding branch below, and the additive
    ``visual_view``/``visual_view_scale`` keys report what was served
    (``visual_view_scale=None`` for the pass-through raw view — raw adds no view
    transform; a header-parse ratio for derived views, 1.0 = undegraded). With
    ``view_id`` set and ``view_payload=None`` (raw) the stock bytes travel untouched.
    ``view_id=None`` (the default path) is byte-identical to the pre-AL-002 builder.
    """
    payload = dict(response)
    data_b64 = payload.pop("screenshot_after_base64", None)
    blocks: list[Any] = []
    if data_b64 is not None:
        if view_payload is not None:
            # AL-002: the requested view was derived ONCE by the caller; the derived
            # bytes + frame ride the UNCHANGED stock bounding branch (I-11: the stock
            # ladder's degradation still applies and is reported via visual_view_scale).
            data_b64, frame = view_payload
        if full_resolution:
            bounded_b64, mime, outbound_scale = _bound_outbound_image(str(data_b64), frame=frame)
        else:
            halved = _half_res_action_image(str(data_b64), frame=frame)
            if halved is None:
                bounded_b64, mime, outbound_scale = _bound_outbound_image(str(data_b64), frame=frame)
            else:
                bounded_b64, mime, outbound_scale = halved
        payload["image_format"] = mime
        # §1.1 D-3 (ORVEX-2026-108): image_outbound_scale is the ONLY scale key —
        # the MEASURED scale the image ladder applied to the delivered copy (1.0
        # when nothing fired). The legacy ``image_scale`` constant-marker key is
        # deleted: one key, one meaning, and it can no longer lie when the budget
        # ladder fires on top of the half-res encode.
        payload[IMAGE_OUTBOUND_SCALE_KEY] = outbound_scale
        if view_id is not None:
            # AL-002 additive truth markers: which view was served, and its outbound
            # scale (None for raw pass-through; header-parsed ratio for derived views).
            payload["visual_view"] = view_id
            payload["visual_view_scale"] = (
                None
                if view_payload is None
                else outbound_view_scale(frame, bounded_b64)
            )
        blocks.append(ImageContent(type="image", data=bounded_b64, mimeType=mime))
    return [
        TextContent(type="text", text=json.dumps(payload, ensure_ascii=False)),
        *blocks,
    ]
