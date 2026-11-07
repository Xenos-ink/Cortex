"""The observation family: computer_observe, computer_screenshot, computer_zoom, and the AL-002 view/zoom helpers (LAYER 4)."""

from __future__ import annotations

import base64
import io
import json
import time
from typing import TYPE_CHECKING, Any

from mcp.types import ImageContent, TextContent
from PIL import Image

from ..core.models import CoordinateSpace
from ..desktop.coordinates import CoordinateTransform
from ..vision.evidence import register_frame
from ..vision.observation import observation_text_summary
from ..vision.pointer_sprite import composite
from ..vision.visual_views import (
    DEFAULT_GRID_DENSITY,
    InvalidVisualViewError,
    VisualViewError,
    ViewDeriveError,
    crop_observation_frame,
    encode_png_b64,
    outbound_view_scale,
    render_crop_grid,
    resolve_density,
    resolve_view,
    spatial_text_of,
)
from .instance import logger, mcp
from .lifecycle import _get_live_bundle, _session_scoped
from .plan_surface import _active_plan_tool_rejection
from .pointer_reply import call_show_pointer, pointer_reply_fields
from .replies import (
    IMAGE_OUTBOUND_SCALE_KEY,
    OBSERVE_METADATA_ELEMENT_CAP,
    _base64_decoded_byte_size,
    _bound_outbound_image,
    _result_image_max_bytes,
)
from .schemas import NullableBool, NullableRegion, NullableStr
from .state import (
    IMAGE_DELIVERY_TEXT_NOTE,
    _error_response,
    _session_image_delivery,
    _StoppedSession,
    _UnknownSession,
)

if TYPE_CHECKING:
    from .state import _SessionBundle



def _bound_observe_lists(dump: dict[str, Any]) -> dict[str, Any]:
    """H8: cap ``ocr_text``/``ui_elements`` in the SERIALIZED observe metadata only.

    The internal :class:`~computer_use_mcp.models.Observation` keeps its full lists;
    only the host-facing metadata text is capped, with an additive
    ``<field>_omitted_count`` marker so nothing is silently lost.
    """
    for field in ("ocr_text", "ui_elements"):
        value = dump.get(field)
        if isinstance(value, list) and len(value) > OBSERVE_METADATA_ELEMENT_CAP:
            omitted = len(value) - OBSERVE_METADATA_ELEMENT_CAP
            dump[field] = value[:OBSERVE_METADATA_ELEMENT_CAP]
            dump[f"{field}_omitted_count"] = omitted
    return dump


# --- AL-002 adaptive visual representations (additive; MISSION-CRF-AVR-008 W2) ---------------
# A per-call, fail-closed view selector on the observe/execute paths: an ABSENT request
# ships byte-identical results with NO new keys and no new work beyond the None-check
# (H19/I-1); an EXPLICIT request (raw OR grid) is a view selection by the model — the
# spatial text layer accompanies EVERY selected representation (owner directive Phase 5,
# Commander correction 2026-09-17). Unknown names are typed ``invalid_visual_view``
# errors; impossible derivations are typed ``view_derive_failed`` errors — never a silent
# raw substitution (I-7). The providers/registry/grid renderer/OCR-once cache live in
# ``visual_views.py`` (models+stdlib+PIL only, never agent/server/verification).


def _resolve_view_param(visual_view: str | None) -> Any:
    """Resolve an explicit ``visual_view`` request (fail-closed, AL-002 I-7).

    ``None`` (the default path) returns None WITHOUT touching the registry — the
    None-check is the ONLY added work on the default path (H19). Invalid names raise
    :class:`InvalidVisualViewError`; callers translate that into the typed
    ``invalid_visual_view`` error dict naming the available views.
    """
    if visual_view is None:
        return None
    return resolve_view(visual_view)


def _spatial_text_for(
    observation: Any, bundle: "_SessionBundle", *, pixel_evidence: bool = False
) -> Any:
    """Build/reuse the observation's spatial-text block with instrumentation (AL-002).

    Counter ``spatial_text_compute`` + latency ``spatial_text_ms`` ride the
    create-on-first-use metrics seams (audit.py:200-208); both fire exactly once per
    actual computation (the OCR-once cache, I-2). Cap = ``OBSERVE_METADATA_ELEMENT_CAP``
    — the SAME serialized-metadata cap the observe path already applies (I-9), passed
    through so the lockstep with ``visual_views.SPATIAL_TEXT_CAP`` is structural.
    ``pixel_evidence`` (A2, owner decision D2) rides the single build: OFF (default)
    omits the per-region field and skips all sigma computation; ON computes the frozen
    A1.1 formula inside the SAME build (the flag never changes the computation count).
    """
    def _on_compute(elapsed_ms: float) -> None:
        bundle.metrics.incr("spatial_text_compute")
        bundle.metrics.record_latency("spatial_text_ms", elapsed_ms)

    try:
        return spatial_text_of(
            observation,
            on_compute=_on_compute,
            cap=OBSERVE_METADATA_ELEMENT_CAP,
            pixel_evidence=pixel_evidence,
        )
    except Exception as exc:  # noqa: BLE001 - attach failure is typed, never stock-breaking
        raise ViewDeriveError(f"spatial-text attach failed: {type(exc).__name__}: {exc}") from exc


def _view_derive_failed(message: str) -> dict[str, object]:
    """The typed ``view_derive_failed`` boundary error (AL-002; no silent raw fallback)."""
    return {"ok": False, "error": "view_derive_failed", "message": message}


def _invalid_visual_view(exc: InvalidVisualViewError) -> dict[str, object]:
    """The typed ``invalid_visual_view`` boundary error (mirrors ``invalid_image_delivery``)."""
    return {"ok": False, "error": "invalid_visual_view", "message": str(exc)}


def _invalid_region(message: str) -> dict[str, object]:
    """The typed ``invalid_region`` boundary error (A1.3 item 2; never a clipped guess).

    Every ``computer_zoom`` region violation class — malformed shape, non-int entries,
    non-positive width/height, out-of-bounds, UNVERIFIABLE coordinate space — returns
    this shape naming the bounds. NO clipping, NO silent full-frame fallback.
    """
    return {"ok": False, "error": "invalid_region", "message": message}


def _invalid_pixel_evidence(value: Any) -> dict[str, object]:
    """The typed ``invalid_pixel_evidence`` boundary error (A2; the same fail-closed
    family as ``invalid_visual_view`` / ``invalid_region``).

    ``pixel_evidence`` is a STRICT boolean (A2, owner decision D2): absent/None and
    False mean OFF, True computes the frozen A1.1 scores; any other value is rejected
    here rather than coerced — direct callers bypass MCP validation, so the tool
    re-checks at the boundary (defense in depth, the ``via`` precedent).
    """
    return {
        "ok": False,
        "error": "invalid_pixel_evidence",
        "message": (
            "pixel_evidence must be a boolean (true = compute the per-region A1.1 "
            "pixel-evidence scores for this request; false/absent = the field is "
            f"omitted entirely); got {value!r} ({type(value).__name__})."
        ),
    }


# --- WP-C3 (ORVEX-2026-109; DELTA 1): the pointer sprite on OUTBOUND copies only ------------


def _invalid_show_pointer(value: Any) -> dict[str, object]:
    """The typed ``invalid_show_pointer`` boundary error (DELTA 1 MINOR 5; the
    same fail-closed family as ``invalid_pixel_evidence``).

    ``show_pointer`` is a STRICT boolean-or-None: non-bool values are rejected
    here rather than coerced — direct callers bypass MCP validation, so the
    tool re-checks at the boundary (defense in depth, the ``pixel_evidence``
    precedent).
    """
    return {
        "ok": False,
        "error": "invalid_show_pointer",
        "message": (
            "show_pointer must be a boolean or null (true = draw the Cortex "
            "pointer sprite on this reply's image; false = the clean image; "
            f"null = the session default); got {value!r} ({type(value).__name__})."
        ),
    }


def _composited_capture_frame(
    data_b64: str, frame_stash: Any, x: float, y: float, state: str
) -> tuple[Image.Image | None, bool]:
    """Composite the pointer sprite onto ONE copy of the capture.

    DELTA 1 M6: prefers the capture-time RGB ``_frame`` stash (a COPY — the
    verification/diff pixels are never touched) and decodes the PNG exactly
    once only when there is no stash. DELTA 2 NIT: the hotspot is checked
    BEFORE the frame is copied (the copy in ``composite`` never happens for an
    off-image pointer). Returns ``(composited_pil | None, in_image)``;
    ``None`` means "serve the clean capture" (hotspot outside the image, or
    any composite failure — the sprite is additive and must never break a
    reply). The caller runs the outbound ladder ONCE on the result.
    """
    try:
        if isinstance(frame_stash, Image.Image):
            # DELTA 2 NIT: the hotspot check BEFORE copying the frame — an
            # off-image pointer never pays for the copy (composite re-checks).
            if not (
                0 <= round(float(x)) < frame_stash.width
                and 0 <= round(float(y)) < frame_stash.height
            ):
                return None, False
            base = frame_stash.copy()
        else:
            base = Image.open(io.BytesIO(base64.b64decode(data_b64, validate=True)))
            base.load()
        composited, in_image = composite(base, x, y, state, 1.0)
        if not in_image:
            return None, False
        return composited, True
    except Exception:  # noqa: BLE001 - additive decoration, never a reply-breaker
        logger.debug("pointer composite failed; serving the clean capture", exc_info=True)
        return None, False


def _zoom_region_shape_error(region: Any) -> str | None:
    """Pre-capture shape validation for a zoom region (A1.3 item 2, fail-closed economy).

    Checks everything that needs NO observation (4-sequence of ints, width > 0,
    height > 0) so a malformed request wastes no capture; the bounds and the
    coordinate-space doctrine are checked against the FRESH observation by
    :func:`_zoom_region_bounds_error` after the capture. Returns the error message, or
    ``None`` when the shape is acceptable.
    """
    if region is None or isinstance(region, (str, bytes, dict)) or not isinstance(
        region, (list, tuple)
    ):
        return (
            "region must be a [left, top, width, height] sequence of 4 ints in canonical "
            f"screenshot space; got {region!r}."
        )
    if len(region) != 4:
        return (
            "region must be a [left, top, width, height] sequence of exactly 4 ints; "
            f"got {len(region)} entries: {region!r}."
        )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in region):
        return (
            "region entries must all be ints in canonical screenshot space; got "
            f"{region!r}. Zoom never coerces a guess."
        )
    left, top, width, height = region
    if width <= 0 or height <= 0:
        return (
            f"region width and height must be > 0; got {region!r}."
        )
    return None


def _zoom_region_bounds_error(region: Any, observation: Any) -> str | None:
    """Post-capture bounds/space validation for a zoom region (A1.3 item 2).

    The region must be FULLY inside the fresh observation's frame
    (``0 <= left``, ``0 <= top``, ``left+width <= observation.width``,
    ``top+height <= observation.height``) and the observation's coordinate space must
    NOT be ``UNVERIFIABLE`` (the same refusal doctrine as coordinate actions). Returns
    the error message, or ``None`` when the region is acceptable.
    """
    left, top, width, height = region
    if (
        left < 0
        or top < 0
        or left + width > observation.width
        or top + height > observation.height
    ):
        return (
            f"region {list(region)} is not fully inside the observation frame "
            f"{observation.width}x{observation.height} (bounds: 0 <= left, 0 <= top, "
            f"left+width <= {observation.width}, top+height <= {observation.height}); "
            "zoom never clips and never falls back to a full frame."
        )
    if observation.coordinate_space is CoordinateSpace.UNVERIFIABLE:
        return (
            "the observation's coordinate space is unverifiable; zoom coordinates cannot "
            "be trusted (the same refusal doctrine as coordinate actions)."
        )
    return None


def _zoom_spatial_block(
    block: dict[str, Any] | None, left: int, top: int, width: int, height: int
) -> dict[str, Any] | None:
    """Filter the OCR-once spatial-text block to the zoom crop (A1.3 item 5).

    Regions whose bboxes INTERSECT the crop are included with their canonical
    screenshot-space coordinates UNCHANGED (never re-based to crop-local — one
    coordinate space everywhere, I-6), each with its ``pixel_evidence`` computed on the
    FULL frame by the cached block build (A1.1). The block gains ``crop_region`` next to
    the usual provenance (``crop_origin`` stays the MONITOR origin — screen =
    ``crop_origin + screenshot``; screenshot = ``crop_region[:2] + crop-local``).
    ``region_count``/``omitted_count`` semantics are preserved: ``omitted_count``
    reports every known-but-unserved region (the cap-dropped AND the non-intersecting)
    — nothing silently lost (I-9). The cached full block is never mutated (I-2/I-5).
    """
    if block is None:
        return None
    regions = block.get("regions") or []
    kept = [
        dict(entry)
        for entry in regions
        if entry["x"] < left + width
        and entry["y"] < top + height
        and entry["x"] + entry["width"] > left
        and entry["y"] + entry["height"] > top
    ]
    zoom_block = dict(block)  # the cached OCR-once block stays untouched (I-2)
    zoom_block["crop_region"] = [left, top, width, height]
    zoom_block["region_count"] = len(kept)
    zoom_block["omitted_count"] = int(block.get("omitted_count", 0)) + (
        len(regions) - len(kept)
    )
    zoom_block["regions"] = kept
    return zoom_block


@mcp.tool()
@_session_scoped
def computer_observe(
    session_id: str,
    visual_view: NullableStr = None,
    visual_view_density: NullableStr = None,
    pixel_evidence: NullableBool = None,
    show_pointer: NullableBool = None,
) -> Any:
    """Return the current observation state used for grounding: screenshot, dimensions, window, and cursor.

    On success this returns MCP content blocks: one TextContent carrying the
    observation metadata (dimensions, active window, digest, coordinate scale,
    plus an additive ``text_summary`` one-liner: window title/process, cursor,
    focused-control hint when the backend supplies ui_elements, and
    changed/unchanged versus the previous observation of this session — never the
    raw base64) and one ImageContent carrying the screenshot itself, so
    vision-capable clients receive it as a real image rather than as text.
    In a text-mode session (``start_session(image_delivery="text")`` /
    ``CORTEX_IMAGE_DELIVERY=text``) this returns ONE text block only (metadata +
    ``image_delivery`` keys) — never an image block — so non-vision models stay
    alive; the delivery mode is fixed at start_session (pass image_delivery="text"
    there if your inputs are text-only). Error paths still return the structured
    error dict.

    Visual view (AL-002, trailing optional): ``visual_view`` selects the
    representation of THIS observation — "raw" (the untouched screenshot) or "grid"
    (a coordinate-grid annotation aid; labels show canonical screenshot-space
    coordinates, ``displayed + crop_origin = screen`` is a reader-side fact). An
    ABSENT request ships byte-identical results with no new keys and no new work
    (the default never changes). An EXPLICIT request — raw OR grid — also attaches
    the ``spatial_text`` metadata block (the OCR-once UIA-derived text regions with
    provenance: observation_id, coordinate space/scale, crop_origin; ``null`` plus
    ``spatial_text_available: false`` when the read degraded, a truthful
    ``region_count: 0`` when it read nothing). The server never auto-selects a view;
    unknown values are rejected fail-closed (``invalid_visual_view``) and impossible
    derivations with ``view_derive_failed`` — never a silent raw substitution.

    ``visual_view_density`` (A1.2, trailing optional): the grid density for a
    ``visual_view="grid"`` request — closed enum ``coarse|standard|fine`` (default
    ``coarse``). Unknown values are rejected with the same typed
    ``invalid_visual_view``-class error naming the enum. Raw/absent view requests
    IGNORE the parameter silently (density is meaningless for a pass-through), and
    cost is REPORTED (visual_view_scale + instrumentation), never gated.

    ``pixel_evidence`` (A2, owner decision D2, trailing optional): OPT-IN measured
    evidence. ``true`` = every served region in the ``spatial_text`` block carries its
    A1.1 ``pixel_evidence`` score (frozen formula, σ 0.0/32.535) and the block records
    ``"pixel_evidence_mode": "on"``. ``false``/absent (the default) = the field is
    OMITTED entirely (absent, never null), NO sigma computation runs, and the block
    records ``"pixel_evidence_mode": "off"``. Non-boolean values are rejected
    fail-closed (``invalid_pixel_evidence``) before any capture. The OCR-once cache
    semantics are unchanged (H21'): the flag shapes WHAT the single per-observation
    build computes, never the computation count.

    ``show_pointer`` (ORVEX-2026-109 WP-C3, trailing optional): ``None`` (the
    default) means the session default (``start_session(show_pointer=...)``, env
    ``CORTEX_SHOW_POINTER``, default ON). ON draws the Cortex cursor sprite INTO
    the served image at the capture-time pointer position — an OUTBOUND-only
    decoration: the capture, digest, verification diff, and the stored evidence
    frame keep the clean pixels. The additive ``pointer`` /
    ``last_click`` keys ride image-bearing replies only.
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    # WP7b (observe/zoom-during-lease decision, dev parity): an INDEPENDENT
    # observation while a plan lease is waiting would desynchronize the lease's
    # grounding premise — refused with the typed ``plan_lease`` rejection before
    # any capture. computer_fetch_image stays allowed (S-04: it serves evidence
    # the session already holds and captures nothing).
    lease_rejection = _active_plan_tool_rejection(bundle)
    if lease_rejection is not None:
        return lease_rejection
    # D1: text mode suppresses the OUTBOUND image block only — a non-vision model
    # receiving one ImageContent anywhere in its history gets the whole provider
    # request rejected with a 400 and the session dies permanently.
    image_mode = _session_image_delivery(bundle)
    # AL-002: resolve an explicit view request BEFORE any capture (fail-closed — a bad
    # name wastes no capture); the absent request adds only this None-check (H19).
    try:
        view_provider = _resolve_view_param(visual_view)
    except InvalidVisualViewError as exc:
        return _invalid_visual_view(exc)
    # A1.2: density resolves ONLY for a derived-view (grid) request — raw/absent
    # requests ignore it silently (A1.2), so the default path never resolves it (H19).
    density: str | None = None
    if view_provider is not None and not view_provider.passthrough:
        try:
            density = resolve_density(visual_view_density)
        except InvalidVisualViewError as exc:
            return _invalid_visual_view(exc)
    # A2 (owner decision D2): pixel_evidence is a STRICT boolean — absent/None and
    # False mean OFF; any non-bool value is rejected fail-closed BEFORE any capture
    # (the same typed-error family as the other boundary params; direct callers bypass
    # MCP validation, so the boundary re-check is defense in depth).
    if pixel_evidence is not None and not isinstance(pixel_evidence, bool):
        return _invalid_pixel_evidence(pixel_evidence)
    evidence_requested = pixel_evidence is True
    # DELTA 1 MINOR 5 (observe surface): ``show_pointer`` is a STRICT boolean
    # or None — rejected fail-closed BEFORE any capture (the pixel_evidence
    # precedent; direct callers bypass MCP validation).
    if show_pointer is not None and not isinstance(show_pointer, bool):
        return _invalid_show_pointer(show_pointer)
    try:
        observation, digest = bundle.agent.observation.capture_with_digest()
    except Exception as exc:  # noqa: BLE001 - structured error, no traceback
        return _error_response(exc)
    info = observation.active_window_info
    # PERF-004 C8: additive structured summary line (previous digest tracked per
    # session; the FIRST observation reports "first observation" — never invented).
    previous_digest = bundle.extra.get("last_observation_digest")
    text_summary = observation_text_summary(observation, previous_digest=previous_digest)
    bundle.extra["last_observation_digest"] = digest
    try:
        bundle.auditor.emit(
            "observation",
            session_id,
            task_id=bundle.context.task.task_id,
            observation_id=observation.observation_id,
            active_app=info.process_name if info is not None else observation.active_window,
            result="ok",
            metadata={"source": "computer_observe"},
        )
    except Exception:
        logger.debug("observation audit failed", exc_info=True)
    bundle.metrics.incr("screenshot_count")
    observation_dump = observation.model_dump(mode="json", exclude={"image_base64"})
    # REM-E (live-test gap): the OUTBOUND ImageContent rides the SAME budget as
    # the execute path (`_bound_outbound_image`, CORTEX_RESULT_IMAGE_MAX_KB —
    # observe and execute). The live probe shipped a 1.37 MB PNG block and blew
    # the provider request; the INTERNAL Observation stays PNG untouched below,
    # so pixel-diff, staleness digests, and checkpoints are unaffected.
    if image_mode == "text":
        # D1 text mode: ONE text block only (metadata + mode keys), never an
        # ImageContent. Internal capture is UNCHANGED — digest, staleness,
        # pixel-diff, text_summary, metrics, and audit all keep working above.
        metadata = {
            # §1.1: ok + message on every reply (text mode: metadata-only, no image).
            "ok": True,
            "message": "Observation served (text mode: no image block).",
            "observation": _bound_observe_lists(observation_dump),
            "digest": digest,
            "observation_id": observation.observation_id,
            "active_app": info.process_name if info is not None else observation.active_window,
            "image_format": "none",  # truthful: no image block was emitted
            "image_delivery": "text",
            # §1.1 D-3: no image was served, so no scale was applied — honest None.
            IMAGE_OUTBOUND_SCALE_KEY: None,
            "image_delivery_note": IMAGE_DELIVERY_TEXT_NOTE,
            # PERF-004 C8 (additive): bounded one-line grounding text for weak models.
            "text_summary": text_summary,
        }
        if view_provider is not None:
            # AL-002 (explicit request, text mode): the view keys ride the single text
            # block; image_format stays "none" and no pixels are derived (there is no
            # outbound image — visual_view_scale stays None, honest by construction).
            # W5.1 (R-10): the attach is guarded here EXACTLY like the image branch —
            # an attach failure degrades to the typed ``view_derive_failed`` error per
            # the termination table ("any exception inside derive/attach -> typed
            # error"), never an untyped crash of the text-mode observe.
            metadata["visual_view"] = view_provider.id
            metadata["visual_view_scale"] = None
            try:
                metadata["spatial_text"] = _spatial_text_for(
                    observation, bundle, pixel_evidence=evidence_requested
                )
            except VisualViewError as exc:
                return _view_derive_failed(str(exc))
            if metadata["spatial_text"] is None:
                metadata["spatial_text_available"] = False  # degraded UIA read (honest)
        return [TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False))]
    # AL-002: ABSENT request or an explicit RAW request → today's bytes EXACTLY
    # (the pass-through branch is the stock call, untouched — I-1 by construction);
    # an explicit derived-view request (grid) → render + one PNG encode, then the
    # UNCHANGED stock ladder.
    view_scale: float | None = None
    # WP-C3: the per-call show_pointer resolution (None = session default). The
    # sprite is drawn on a COPY of the served image only — the capture bytes,
    # the digest, the verification diff, and the stored evidence frame stay clean.
    show = call_show_pointer(bundle, show_pointer)
    cursor_xy: tuple[int, int] | None = None
    if observation.cursor_x is not None and observation.cursor_y is not None:
        cursor_xy = (int(observation.cursor_x), int(observation.cursor_y))
    pointer_fields: dict[str, Any] | None = None
    if view_provider is None or view_provider.passthrough:
        # WP5 (VAL-WP5 F1): the RAW capture is what this reply carries — the
        # bytes the evidence store keeps (pre-ladder, so a fetch re-runs the
        # ladder). DELTA 1 M6: with the pointer ON the ladder runs EXACTLY
        # ONCE — on the composited copy — never on the clean capture too.
        # DELTA 2 N4: when the clean capture is ALREADY over the outbound
        # budget, the full-res PNG encode is SKIPPED entirely (the ladder
        # replaces it with JPEG anyway) and the JPEG ladder runs straight from
        # the composited frame; the PNG path is kept only for captures that can
        # still be delivered as PNG within the budget.
        carried_b64 = observation.image_base64
        frame_stash = getattr(observation, "_frame", None)
        composited: Image.Image | None = None
        pointer_in_image = False
        # DELTA 3 (blocking fix): compare DECODED bytes against the byte budget
        # — the base64 LENGTH is ~4/3 of the decoded size, so the old
        # ``len(carried_b64)`` check classed captures at 0.75x-1x of the budget
        # as over budget and silently served the clean PNG (dropping the
        # sprite) while reporting in_image true. The helper computes the same
        # measure the ladder's own under-budget check uses, without a decode.
        clean_over_budget = _base64_decoded_byte_size(carried_b64) > _result_image_max_bytes()
        if show and cursor_xy is not None:
            composited, pointer_in_image = _composited_capture_frame(
                carried_b64, frame_stash, cursor_xy[0], cursor_xy[1], "normal"
            )
        if composited is not None and pointer_in_image and not clean_over_budget:
            serving_b64 = encode_png_b64(composited)
            outbound_b64, outbound_mime, image_scale = _bound_outbound_image(
                serving_b64, frame=composited if composited.mode in ("RGB", "L") else None
            )
        elif composited is not None and pointer_in_image:
            # DELTA 2 N4: over-budget capture — the ladder's JPEG comes straight
            # from the composited frame (data can never pass through: its
            # DECODED size is over budget, so the ladder always encodes from
            # the frame). DELTA 3: a non-RGB/L composited copy is CONVERTED to
            # RGB — falling back to the clean stash here would drop the sprite
            # while reporting in_image true.
            frame_arg = composited
            if composited.mode not in ("RGB", "L"):
                frame_arg = composited.convert("RGB")
            outbound_b64, outbound_mime, image_scale = _bound_outbound_image(
                carried_b64, frame=frame_arg
            )
        else:
            outbound_b64, outbound_mime, image_scale = _bound_outbound_image(
                # R-5 (W4): the capture-time frame (backend stash) skips the PNG
                # re-decode on the JPEG ladder; absent (fakes/legacy) the path
                # decodes exactly as before.
                observation.image_base64, frame=frame_stash
            )
        if show and cursor_xy is not None:
            pointer_fields = pointer_reply_fields(
                bundle,
                x=cursor_xy[0],
                y=cursor_xy[1],
                state="normal",
                in_image=pointer_in_image,
                transform=CoordinateTransform.from_observation(observation),
            )
    else:
        try:
            started = time.perf_counter()
            # A1.2: the resolved density rides the derive call (raw ignores it).
            derived = view_provider.derive(observation, density=density)  # stash-copy or ONE payload decode (I-4)
            encoded_b64 = encode_png_b64(derived.image)
            bundle.metrics.record_latency(
                "view_derive_ms", (time.perf_counter() - started) * 1000.0
            )
            serving_b64 = encoded_b64
            serving_image = derived.image
            in_image = False
            if show and cursor_xy is not None:
                composited, in_image = composite(
                    derived.image, cursor_xy[0], cursor_xy[1], "normal", 1.0
                )
                if in_image:
                    # WP-C3: the GRID view (labels and all) is what the sprite is
                    # drawn ON — composite after the grid, never into the store.
                    serving_image = composited
                    serving_b64 = encode_png_b64(composited)
                pointer_fields = pointer_reply_fields(
                    bundle,
                    x=cursor_xy[0],
                    y=cursor_xy[1],
                    state="normal",
                    in_image=in_image,
                    transform=CoordinateTransform.from_observation(observation),
                )
            outbound_b64, outbound_mime, image_scale = _bound_outbound_image(serving_b64, frame=serving_image)
            view_scale = outbound_view_scale(derived.image, outbound_b64)
        except VisualViewError as exc:  # fail-closed: typed error, never a raw substitution
            return _view_derive_failed(str(exc))
        except Exception as exc:  # noqa: BLE001 - fail-closed: typed error, no traceback
            return _view_derive_failed(f"{type(exc).__name__}: {exc}")
        # WP5 (VAL-WP5 F1): the DERIVED view (grid labels and all) is what this reply
        # carries — the store keeps exactly these bytes, never the raw capture.
        # WP-C3 exception: the STORE keeps the CLEAN grid bytes (the pointer sprite
        # is an outbound-only decoration; a fetch re-composites from the stored
        # pointer metadata), so ``carried_b64`` stays the pre-sprite encode.
        carried_b64 = encoded_b64
    metadata = {
        # §1.1 (ORVEX-2026-108): ok + message on EVERY reply — here ok means "served
        # as requested" (a read-only surface carries no result token, §1.3).
        "ok": True,
        "message": "Observation served.",
        "observation": _bound_observe_lists(observation_dump),
        "digest": digest,
        "observation_id": observation.observation_id,
        "active_app": info.process_name if info is not None else observation.active_window,
        "image_format": outbound_mime,
        # §1.1 D-3 (ORVEX-2026-108): the ONLY scale key on an image-bearing reply —
        # the measured applied downscale (1.0 when the copy kept its dimensions).
        IMAGE_OUTBOUND_SCALE_KEY: image_scale,
        # PERF-004 C8 (additive): bounded one-line grounding text for weak models.
        "text_summary": text_summary,
    }
    # WP-C3: the additive pointer block rides ONLY image-bearing replies (never
    # text mode, never a refusal), and only when the pointer layer is ON.
    if pointer_fields is not None:
        metadata.update(pointer_fields)
    if view_provider is not None:
        # AL-002 (explicit request): the spatial text layer accompanies EVERY selected
        # representation (raw or grid — owner directive Phase 5, Commander correction
        # 2026-09-17); the additive keys ship only on explicit requests (absent request
        # = byte-identical metadata, H19).
        try:
            spatial_block = _spatial_text_for(
                observation, bundle, pixel_evidence=evidence_requested
            )
        except VisualViewError as exc:
            return _view_derive_failed(str(exc))
        metadata["visual_view"] = view_provider.id
        metadata["visual_view_scale"] = view_scale
        metadata["spatial_text"] = spatial_block
        if spatial_block is None:
            metadata["spatial_text_available"] = False  # degraded UIA read (honest)
    # WP5: THIS reply is committed to carrying the image — the hook runs HERE, after
    # every late failure return above (a ``_view_derive_failed`` from the spatial-text
    # block stores nothing — VAL-WP5 F2), so the frame retained is EXACTLY the bytes
    # the reply carried (raw capture or derived view, pre-ladder — VAL-WP5 F1), and
    # the reply gains the additive image_id keys naming it.
    metadata.update(
        _record_observation_evidence(
            bundle,
            observation,
            source="computer_observe",
            digest=digest,
            payload=carried_b64,
            visual_view=view_provider.id if view_provider is not None else None,
            pointer_x=cursor_xy[0] if show and cursor_xy else None,
            pointer_y=cursor_xy[1] if show and cursor_xy else None,
            pointer_state="normal" if show and cursor_xy else None,
            last_click=pointer_fields.get("last_click") if pointer_fields else None,
        )
    )
    return [
        TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)),
        ImageContent(type="image", data=outbound_b64, mimeType=outbound_mime),
    ]


@mcp.tool()
@_session_scoped
def computer_screenshot(session_id: str) -> Any:
    """Compatibility alias for the raw screenshot observation."""
    return computer_observe(session_id)


@mcp.tool()
@_session_scoped
def computer_zoom(
    session_id: str,
    region: NullableRegion = None,
    visual_view: NullableStr = "raw",
    visual_view_density: NullableStr = None,
    pixel_evidence: NullableBool = None,
) -> Any:
    """Capture a FRESH observation cropped to a region: the zoom view (AL-002 A1.3).

    session_id is REQUIRED on every call: copy it from start_session's result and
    reuse it for the whole session. ``region`` is ``[left, top, width, height]`` in
    canonical SCREENSHOT space (the same space coordinates are grounded in — mss
    convention, matching MonitorInfo.bounds).

    Behavior (spec A1.3 items 1-7):
    1. FRESH capture through the existing lifecycle (the same path computer_observe
       uses): a new ``observation_id`` every call, no frame caching across requests;
       ``screenshot_count`` increments and an ``observation`` audit event with
       ``metadata={"source": "computer_zoom"}`` is emitted.
    2. FAIL-CLOSED region validation: the region must be a 4-int sequence, width > 0,
       height > 0, fully inside the captured frame, and the observation's coordinate
       space must NOT be unverifiable. Any violation returns the typed
       ``invalid_region`` error naming the bounds — NEVER a clipped guess, NEVER a
       silent full-frame fallback.
    3. The served image is the NATIVE-RESOLUTION crop (no downscale, no upscale — the
       crop itself is the zoom); the outbound budget ladder still governs the served
       bytes, and ``visual_view_scale`` reports any ladder-applied scale.
    4. ``visual_view="grid"`` renders the coordinate grid ON THE CROP at the requested
       density; labels show CANONICAL SCREENSHOT-space VALUES — a label at crop-local
       ``(x, y)`` displays ``(left + x, top + y)`` (the crop origin is baked into the
       label VALUES, unlike the full-frame grid view).
    5. The ``spatial_text`` block is ALWAYS attached (observe-family rule): UIA text
       regions whose bboxes intersect the crop, canonical coordinates UNCHANGED; the
       block carries ``crop_region`` + ``crop_origin`` provenance so a reader can
       convert (screenshot = crop_region[:2] + crop-local). The result metadata also
       carries top-level ``crop_region``. The per-region ``pixel_evidence`` score is
       OPT-IN (A2): pass ``pixel_evidence=true`` to compute it (frozen A1.1 formula,
       ``"pixel_evidence_mode": "on"``); the default OFF omits the field entirely and
       records ``"pixel_evidence_mode": "off"`` (no sigma computation, no decode).
    6. OBSERVATION-ONLY: no action surface, no queue interaction; coordinates read
       from a zoom are used through the stock ground -> validate -> safety -> execute
       pipeline exactly like any other observation.
    7. Result shape mirrors computer_observe's: one TextContent (metadata) + one
       bounded ImageContent; text-mode sessions get the single-text-block treatment
       (``image_format: "none"``), never an image block.

    ``visual_view`` accepts "raw" (default) or "grid"; ``visual_view_density``
    (trailing optional, A1.2) selects the grid density — closed enum
    ``coarse|standard|fine``, default ``coarse``; raw requests ignore it silently.
    Unknown view/density values are rejected fail-closed
    (``invalid_visual_view``-class) BEFORE any capture. ``pixel_evidence`` (A2) is a
    STRICT boolean: non-boolean values are rejected fail-closed
    (``invalid_pixel_evidence``) BEFORE any capture.
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    # WP7b: zoom is refused during a plan lease exactly like observe (dev parity;
    # same rationale — an independent capture would desynchronize the lease).
    lease_rejection = _active_plan_tool_rejection(bundle)
    if lease_rejection is not None:
        return lease_rejection
    image_mode = _session_image_delivery(bundle)
    # Fail-closed view/density resolution BEFORE any capture (a bad name wastes no
    # capture; the observe-path precedent). Raw/absent views ignore density silently.
    try:
        view_provider = _resolve_view_param(visual_view)
    except InvalidVisualViewError as exc:
        return _invalid_visual_view(exc)
    density: str | None = None
    if view_provider is None or view_provider.passthrough:
        view_id = "raw"
    else:
        view_id = view_provider.id
        try:
            density = resolve_density(visual_view_density)
        except InvalidVisualViewError as exc:
            return _invalid_visual_view(exc)
    # A2 (owner decision D2): pixel_evidence is a STRICT boolean — rejected fail-closed
    # BEFORE any capture (the same typed-error family; the boundary re-check covers
    # direct callers, the ``via`` precedent).
    if pixel_evidence is not None and not isinstance(pixel_evidence, bool):
        return _invalid_pixel_evidence(pixel_evidence)
    evidence_requested = pixel_evidence is True
    # A1.3 item 2: pre-capture region shape validation (fail-closed economy — a
    # malformed region wastes no capture); bounds/space need the fresh observation.
    shape_error = _zoom_region_shape_error(region)
    if shape_error is not None:
        return _invalid_region(shape_error)
    try:
        observation, digest = bundle.agent.observation.capture_with_digest()
    except Exception as exc:  # noqa: BLE001 - structured error, no traceback
        return _error_response(exc)
    info = observation.active_window_info
    # PERF-004 C8: the same additive summary/digest tracking as computer_observe (a
    # zoom IS an observation request; the next observe's changed/unchanged line
    # compares against the freshest capture of the session). Recorded BEFORE the
    # region-bounds verdict: the capture genuinely happened, so the audit event and
    # the ``screenshot_count`` increment are owed regardless of the outcome
    # (A1.3 item 7: "screenshot_count increments (a capture happened)").
    previous_digest = bundle.extra.get("last_observation_digest")
    text_summary = observation_text_summary(observation, previous_digest=previous_digest)
    bundle.extra["last_observation_digest"] = digest
    try:
        bundle.auditor.emit(
            "observation",
            session_id,
            task_id=bundle.context.task.task_id,
            observation_id=observation.observation_id,
            active_app=info.process_name if info is not None else observation.active_window,
            result="ok",
            metadata={"source": "computer_zoom"},
        )
    except Exception:
        logger.debug("observation audit failed", exc_info=True)
    bundle.metrics.incr("screenshot_count")
    # A1.3 item 2 (continued): the bounds + coordinate-space verdict against the FRESH
    # observation. A refusal here still leaves the capture honestly recorded above.
    bounds_error = _zoom_region_bounds_error(region, observation)
    if bounds_error is not None:
        return _invalid_region(bounds_error)
    left, top, width, height = (int(value) for value in region)
    observation_dump = observation.model_dump(mode="json", exclude={"image_base64"})
    # A1.3 item 5: the spatial text layer is ALWAYS attached on the observe family.
    # The OCR-once full block is built/reused here (A2: per-region pixel_evidence only
    # when the caller opted in), then filtered to the crop for THIS result (the cache
    # stays full — I-2).
    try:
        full_block = _spatial_text_for(
            observation, bundle, pixel_evidence=evidence_requested
        )
    except VisualViewError as exc:
        return _view_derive_failed(str(exc))
    zoom_block = _zoom_spatial_block(full_block, left, top, width, height)
    # D1 text mode: ONE text block only (metadata + mode keys), never an ImageContent.
    # No pixels are derived for the outbound image (there is none), so
    # visual_view_scale stays None — honest by construction (the observe precedent).
    if image_mode == "text":
        metadata = {
            # §1.1: ok + message on every reply (text mode: metadata-only, no image).
            "ok": True,
            "message": "Zoom served (text mode: no image block).",
            "observation": _bound_observe_lists(observation_dump),
            "digest": digest,
            "observation_id": observation.observation_id,
            "active_app": info.process_name if info is not None else observation.active_window,
            "image_format": "none",  # truthful: no image block was emitted
            "image_delivery": "text",
            # §1.1 D-3: no image was served, so no scale was applied — honest None.
            IMAGE_OUTBOUND_SCALE_KEY: None,
            "image_delivery_note": IMAGE_DELIVERY_TEXT_NOTE,
            "text_summary": text_summary,
            "crop_region": [left, top, width, height],
            "visual_view": view_id,
            "visual_view_scale": None,
            "spatial_text": zoom_block,
        }
        if zoom_block is None:
            metadata["spatial_text_available"] = False  # degraded UIA read (honest)
        return [TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False))]
    # A1.3 item 3: native-resolution crop (stash or ONE recorded payload decode);
    # item 4: grid views render ON the crop with canonical-value labels. The UNCHANGED
    # stock ladder then governs the served bytes exactly as for full-frame views.
    # WP-C3: the pointer sprite is composited into the CROP copy (crop-local
    # coordinates) on the outbound copy only — the stored crop stays clean and a
    # fetch re-composites from the stored pointer metadata.
    show = call_show_pointer(bundle, None)
    cursor_xy: tuple[int, int] | None = None
    if observation.cursor_x is not None and observation.cursor_y is not None:
        cursor_xy = (int(observation.cursor_x), int(observation.cursor_y))
    pointer_fields: dict[str, Any] | None = None
    try:
        started = time.perf_counter()
        crop, _crop_provenance = crop_observation_frame(observation, left, top, width, height)
        served = crop
        if view_provider is not None and not view_provider.passthrough:
            served = render_crop_grid(
                crop, origin_x=left, origin_y=top, density=density or DEFAULT_GRID_DENSITY
            )
        serving_image = served
        in_image = False
        if show and cursor_xy is not None:
            composited, in_image = composite(
                served,
                cursor_xy[0] - left,
                cursor_xy[1] - top,
                "normal",
                1.0,
            )
            if in_image:
                serving_image = composited
            pointer_fields = pointer_reply_fields(
                bundle,
                x=cursor_xy[0],
                y=cursor_xy[1],
                state="normal",
                in_image=in_image,
                transform=CoordinateTransform.from_observation(observation),
            )
        encoded_b64 = encode_png_b64(served)
        serving_b64 = encode_png_b64(serving_image) if in_image else encoded_b64
        bundle.metrics.record_latency(
            "view_derive_ms", (time.perf_counter() - started) * 1000.0
        )
        outbound_b64, outbound_mime, image_scale = _bound_outbound_image(serving_b64, frame=serving_image)
        view_scale = outbound_view_scale(served, outbound_b64)
    except VisualViewError as exc:  # fail-closed: typed error, never a raw substitution
        return _view_derive_failed(str(exc))
    except Exception as exc:  # noqa: BLE001 - fail-closed: typed error, no traceback
        return _view_derive_failed(f"{type(exc).__name__}: {exc}")
    metadata = {
        # §1.1 (ORVEX-2026-108): ok + message on EVERY reply (read-only surface: no
        # result token, §1.3).
        "ok": True,
        "message": "Zoom served.",
        "observation": _bound_observe_lists(observation_dump),
        "digest": digest,
        "observation_id": observation.observation_id,
        "active_app": info.process_name if info is not None else observation.active_window,
        "image_format": outbound_mime,
        # §1.1 D-3 (ORVEX-2026-108): the ONLY scale key — the measured applied
        # downscale the outbound budget ladder ran on the served crop.
        IMAGE_OUTBOUND_SCALE_KEY: image_scale,
        "text_summary": text_summary,
        "crop_region": [left, top, width, height],
        "visual_view": view_id,
        "visual_view_scale": view_scale,
        "spatial_text": zoom_block,
    }
    # WP-C3: the additive pointer block rides ONLY image-bearing replies.
    if pointer_fields is not None:
        metadata.update(pointer_fields)
    # WP5: the CROP this reply carries is what is retained (pre-ladder, so a later
    # fetch re-runs the outbound ladder honestly), and the crop region rides along so
    # the frame's own coordinates are never mistaken for full-screen coordinates.
    # The text-mode branch above stores nothing — that reply carries no image.
    metadata.update(
        _record_observation_evidence(
            bundle,
            observation,
            source="computer_zoom",
            digest=digest,
            payload=encoded_b64,
            crop_region=(left, top, width, height),
            visual_view=view_id,
            pointer_x=cursor_xy[0] if show and cursor_xy else None,
            pointer_y=cursor_xy[1] if show and cursor_xy else None,
            pointer_state="normal" if show and cursor_xy else None,
            last_click=pointer_fields.get("last_click") if pointer_fields else None,
        )
    )
    if zoom_block is None:
        metadata["spatial_text_available"] = False  # degraded UIA read (honest)
    return [
        TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)),
        ImageContent(type="image", data=outbound_b64, mimeType=outbound_mime),
    ]


def _record_observation_evidence(
    bundle: "_SessionBundle",
    observation: Any,
    *,
    source: str,
    digest: str,
    payload: str | None = None,
    crop_region: Any = None,
    visual_view: str | None = None,
    pointer_x: int | float | None = None,
    pointer_y: int | float | None = None,
    pointer_state: str | None = None,
    last_click: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Retain the freshly captured frame an observe-family reply carries.

    ``payload`` overrides what is stored — and MUST be exactly the bytes the reply
    carries (VAL-WP5 F1): the zoom path stores the CROP it served, the observe path
    stores the carried derived view (grid) or the raw capture (raw/absent request).
    Both are the PRE-ladder bytes, never the post-ladder copy, so a later fetch can
    re-run the budget ladder and report an honest ``image_outbound_scale``.  The
    frame's width/height come from the Observation; the zoom's crop carries the
    crop region so its coordinates are never mistaken for full-screen ones, and
    ``visual_view`` records the derived view the frame shows (a fetched image is
    self-describing).

    WP-C3 (ORVEX-2026-109), a DELIBERATE refinement of the byte-equality rule: the
    stored payload is the CLEAN capture (the pointer sprite is an outbound-only
    decoration and is never stored), and the pointer position/state AT CAPTURE TIME
    ride the StoredFrame so ``computer_fetch_image`` composites them onto ITS
    outbound copy.
    """
    return register_frame(
        bundle.extra,
        payload=payload if payload is not None else observation.image_base64,
        source=source,
        attached=True,
        width=observation.width,
        height=observation.height,
        digest=digest,
        observation_id=observation.observation_id,
        coordinate_scale_x=observation.coordinate_scale_x,
        coordinate_scale_y=observation.coordinate_scale_y,
        coordinate_space=getattr(observation.coordinate_space, "value", None),
        crop_region=crop_region,
        visual_view=visual_view,
        redactions_applied=observation.redactions_applied,
        pointer_x=pointer_x,
        pointer_y=pointer_y,
        pointer_state=pointer_state,
        last_click=last_click,
    )
