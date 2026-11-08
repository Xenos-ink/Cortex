"""The evidence store's server seam: reply-capture hooks and computer_fetch_image (LAYER 4)."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from typing import Any

from mcp.types import ImageContent, TextContent
from PIL import Image

from ..vision.evidence import (
    EVIDENCE_REFUSE_DISPOSED,
    FETCH_FRESHNESS_NOTE,
    EvidenceLookup,
    EvidenceStore,
    RETIRE_AGE_CAP,
    RETIRE_BYTE_CAP,
    RETIRE_COUNT_CAP,
    evidence_store,
    recording_store,
)
from ..vision.pointer_sprite import composite
from ..vision.visual_views import encode_png_b64
from .instance import mcp
from .lifecycle import _get_live_bundle, _session_scoped
from .pointer_reply import session_show_pointer
from .replies import IMAGE_OUTBOUND_SCALE_KEY, _bound_outbound_image
from .state import _error_response, _StoppedSession, _UnknownSession



# --- WP5: the evidence store's server seam ---------------------------------------------------
# Capture hooks + ``computer_fetch_image``.  The retention rule for the stable tree
# is ONE sentence: **a frame is retained iff THIS reply carries it.**  A reply that
# carries no image (text mode, an opt-out, a rejection, a failure return) stores
# nothing and carries no evidence keys — "no image in this reply" IS "nothing kept".
# Every hook runs at the point where the reply is already committed to carrying the
# image, after every early return that ends in an image-less reply.


# evidence-by-backend seam — WIRED: ``_plan_evidence`` (in the plan surface
# section above) builds the plan-step/terminal ``evidence_by_backend`` block from
# ``EvidenceStore.captures()`` (the store read API, filled by the reply hooks — real
# on BOTH backends, no fake-only ``executed``/``observed`` reads, S-03) plus the
# lease's retained repositions, and publishes it on every plan step and terminal
# plan reply. The dev copy's fake-only block is gone by design (decision D-2).


#: Rejection wording for ``computer_fetch_image`` (unified contract §1.3: a fetch
#: refusal is a ``rejected`` reply whose ``reasons`` name the typed error).
_EVIDENCE_NOT_FOUND_MESSAGE = (
    "No stored image with this image_id exists on this session. Image ids are "
    "session-scoped, live in memory only, and a frame is dropped when it falls "
    "outside the session's retention bounds — so a wrong id, another session's id, "
    "and an id whose frame was already retired are indistinguishable by design."
)

_EVIDENCE_STORE_DISPOSED_MESSAGE = (
    "This session's evidence store is no longer available: the session is being "
    "torn down or was already stopped, so no stored frame can be served. Start a "
    "new session and capture again if you need the screen."
)

_EVIDENCE_FRAME_TOO_LARGE_MESSAGE = (
    "The stored frame exceeds this session's evidence byte ceiling, so it cannot "
    "be served. This is a fail-closed guard; capture a fresh observation instead."
)

#: Which retention policy retired a frame, in the words the model reads.
_EVIDENCE_RETIRE_POLICIES = {
    RETIRE_COUNT_CAP: "the frame count cap",
    RETIRE_BYTE_CAP: "the byte cap",
    RETIRE_AGE_CAP: "the age cap",
}


def _evidence_rejection(error: str, message: str, **extra: Any) -> dict[str, Any]:
    """The fetch rejection envelope (§1.1): ok:false, result:"rejected", a non-empty
    ``reasons`` list naming the typed error, and NO ``error`` key."""
    payload: dict[str, Any] = {
        "ok": False,
        "result": "rejected",
        "message": message,
        "reasons": [error],
    }
    payload.update(extra)
    return payload


def _fetch_outbound_worker(
    image_base64: str,
    pointer: tuple[float, float] | None,
    pointer_state: str | None,
    crop_region: Any,
    sprite_scale: float = 1.0,
) -> tuple[str, str, float | None, bool]:
    """DELTA 1 M5: ALL fetch PIL work — decode, composite, PNG encode AND the
    outbound ladder — in ONE synchronous worker, run via ``asyncio.to_thread``
    (the loop never touches pixels). Returns ``(bounded_b64, mime, scale,
    pointer_in_image)``; without a drawable pointer this is the plain ladder of
    the clean stored bytes.

    DELTA 2 NIT: ``sprite_scale`` sizes the sprite for the FRAME it is drawn
    on — recording keyframes carry their capture-time frame scale, so the
    cursor looks the same size there as in the sheet and the video (never
    oversized on a downscaled keyframe).
    """
    composited: Image.Image | None = None
    in_image = False
    serving_b64 = image_base64
    if pointer is not None and pointer_state in ("normal", "pressed"):
        try:
            image = Image.open(io.BytesIO(base64.b64decode(image_base64, validate=True)))
            image.load()
            x_local = float(pointer[0])
            y_local = float(pointer[1])
            if crop_region:
                x_local -= crop_region[0]
                y_local -= crop_region[1]
            composited, in_image = composite(
                image, x_local, y_local, pointer_state, sprite_scale
            )
            if in_image:
                serving_b64 = encode_png_b64(composited)
        except Exception:  # noqa: BLE001 - additive decoration, never a reply-breaker
            composited = None
            in_image = False
    frame_arg = composited if composited is not None and composited.mode in ("RGB", "L") else None
    bounded_b64, mime_type, applied_scale = _bound_outbound_image(serving_b64, frame_arg)
    return bounded_b64, mime_type, applied_scale, in_image


async def _fetch_image_response(store: EvidenceStore, image_id: str, bundle: Any = None) -> Any:
    """Resolve one id against THIS session's store and shape the reply.

    The outcomes are distinguishable answers: a frame that is still here is served
    (``verified`` + the image + ``image_outbound_scale``); a frame a retention policy
    retired answers ``image_expired`` naming WHICH policy; a store disposed under the
    lookup answers ``store_disposed`` ATOMICALLY (VAL-WP5 F3 — the disposal check
    shares the store lock, so a fetch racing the teardown can never answer
    ``image_not_found`` for a frame that existed a moment ago); and an id that never
    existed — a malformed id, and another session's id alike — answers
    ``image_not_found``, byte-identical in those forms, so a wrong id can never probe
    which ids exist in another session.
    """
    lookup: EvidenceLookup = store.lookup(image_id)
    if lookup.outcome == "not_found":
        # Byte-identical for a malformed id, an id that never existed, and another
        # session's id.  The message deliberately does NOT echo the value and does
        # NOT distinguish the three cases.
        return _evidence_rejection("image_not_found", _EVIDENCE_NOT_FOUND_MESSAGE)
    if lookup.outcome == "disposed":
        # Atomic with the frame read under the store lock (VAL-WP5 F3).
        return _evidence_rejection(
            EVIDENCE_REFUSE_DISPOSED, _EVIDENCE_STORE_DISPOSED_MESSAGE
        )
    if lookup.outcome == "expired":
        # Name WHICH policy retired it and the session's bounds, so the model can
        # tell "ask sooner" from "ask less".
        policy = _EVIDENCE_RETIRE_POLICIES.get(lookup.reason or "", "a retention policy")
        retention = store.retention()
        return _evidence_rejection(
            "image_expired",
            f"This frame is no longer available: it was retired by {policy}. This "
            f"session's retention bounds are {retention['max_frames']} frames, "
            f"{retention['max_bytes']} bytes, {retention['max_age_seconds']} s. "
            "Capture a new observation if you need to see the screen again.",
            retired_by=lookup.reason,
            evidence_retention=retention,
        )
    frame = lookup.frame
    assert frame is not None  # lookup() only returns "found" with a frame
    if frame.byte_size > store.max_bytes:
        # Fail-closed guard: a frame larger than the session's byte ceiling can never
        # have been stored through ``store()``; refuse rather than serve oversize.
        return _evidence_rejection(
            "frame_too_large",
            _EVIDENCE_FRAME_TOO_LARGE_MESSAGE,
            evidence_retention=store.retention(),
        )
    # The PRE-ladder original is re-bounded NOW, so the scale reported is the scale
    # of the bytes actually being returned and a frame stored under the budget is
    # served at its original resolution.  VAL-WP5 F5: the PIL re-encode ladder runs
    # OFF the event-loop thread via asyncio.to_thread — the loop stays responsive
    # while a large frame is encoded (pinned by a blocked-encode test).
    # WP-C3 (ORVEX-2026-109; DELTA 1 M4/M5): the STORED bytes stay clean (never
    # drawn on); the pointer sprite is composited onto THIS outbound copy from the
    # StoredFrame's capture-time pointer metadata (canonical → frame space:
    # subtract the crop origin for a zoom crop, else 1:1), and ``last_click`` is
    # served from the CANONICAL capture-time snapshot — the fetch describes the
    # FRAME, never the ring "now", never physical coordinates. DELTA 1 M5: the
    # decode, composite, PNG encode AND the ladder all run in ONE
    # ``asyncio.to_thread`` worker call — no PIL work on the event loop.
    show = session_show_pointer(bundle)
    pointer_x = frame.pointer_x
    pointer_y = frame.pointer_y
    pointer_state = frame.pointer_state
    stored_last_click: dict[str, Any] | None = None
    if frame.last_click_x is not None or frame.last_click_button is not None:
        stored_last_click = {
            "x": frame.last_click_x,
            "y": frame.last_click_y,
            "button": frame.last_click_button,
            "age_ms": frame.last_click_age_ms,
        }
    pointer_block: dict[str, Any] | None = None
    if not (
        show
        and pointer_state in ("normal", "pressed")
        and pointer_x is not None
        and pointer_y is not None
    ):
        pointer_x = pointer_y = pointer_state = None
    # DELTA 2 N1: recording keyframes carry their frame→canonical mapping —
    # the sprite is drawn at the keyframe's own sprite scale (NIT: never
    # oversized on a downscaled keyframe) and the reply's pointer block is
    # reported in CANONICAL coordinates (canonical = origin + frame × scale).
    frame_to_canonical: dict[str, Any] | None = None
    sprite_scale = 1.0
    if frame.source == "computer_record" and hasattr(store, "frame_to_canonical"):
        try:
            frame_to_canonical = store.frame_to_canonical(frame.image_id)
        except Exception:  # noqa: BLE001 - additive metadata, never a reply-breaker
            frame_to_canonical = None
        if frame_to_canonical is not None and frame_to_canonical.get("scale", 0) > 0:
            sprite_scale = round(1.0 / float(frame_to_canonical["scale"]), 6)
        else:
            frame_to_canonical = None
    (
        bounded_b64,
        mime_type,
        applied_scale,
        pointer_in_image,
    ) = await asyncio.to_thread(
        _fetch_outbound_worker,
        frame.image_base64,
        None if pointer_x is None or pointer_y is None else (pointer_x, pointer_y),
        pointer_state,
        frame.crop_region,
        sprite_scale,
    )
    if pointer_x is not None and pointer_state is not None:
        # The additive pointer block describes the FRAME.  DELTA 2 N1: for
        # RECORDING keyframes it reports CANONICAL coordinates — the same
        # space observation fetches use — via the published frame→canonical
        # mapping (canonical = origin + frame px × scale); the sprite itself
        # was drawn at the stored FRAME-space point, at the keyframe's own
        # sprite scale.  For observation frames the stored pointer is already
        # canonical and nothing changes.
        block_x, block_y = pointer_x, pointer_y
        if frame_to_canonical is not None:
            origin = frame_to_canonical["origin"]
            block_x = origin[0] + pointer_x * frame_to_canonical["scale"]
            block_y = origin[1] + pointer_y * frame_to_canonical["scale"]
        pointer_block = {
            "pointer": {
                "x": block_x,
                "y": block_y,
                "state": pointer_state,
                "in_image": bool(pointer_in_image),
            },
            "last_click": stored_last_click,
        }
    metadata: dict[str, Any] = {
        "ok": True,
        "result": "verified",
        "message": (
            "Stored frame served (a stored capture is not necessarily current). "
            "A fetched frame may be served at a different resolution than the reply "
            "that published it — the reply that published the image_id may have "
            "carried a downscaled/JPEG copy, while this tool re-bounds the stored "
            "pre-ladder original and discloses its own image_outbound_scale."
        ),
        "session_id": frame.session_id,
        "image_id": frame.image_id,
        "image_format": mime_type,
        IMAGE_OUTBOUND_SCALE_KEY: applied_scale,
        "captured_at": frame.captured_at,
        "age_seconds": store.age_seconds(frame.image_id),
        "width": frame.width,
        "height": frame.height,
        "bytes": frame.byte_size,
        "source": frame.source,
        "observation_id": frame.observation_id,
        "digest": frame.digest,
        "coordinate_scale_x": frame.coordinate_scale_x,
        "coordinate_scale_y": frame.coordinate_scale_y,
        "coordinate_space": frame.coordinate_space,
        "crop_region": list(frame.crop_region) if frame.crop_region else None,
        # VAL-WP5 F1: the derived view this frame shows (grid etc.) — a fetched
        # image is self-describing; None for a raw capture.
        "visual_view": frame.visual_view,
        "redactions_applied": frame.redactions_applied,
        "evidence_retention": store.retention(),
        "freshness_note": FETCH_FRESHNESS_NOTE,
    }
    # DELTA 2 N1: recording keyframes publish their frame→canonical mapping —
    # a keyframe pixel maps back with canonical = origin + pixel × scale.
    # (The inverted coordinate_scale_* is deliberately omitted for recording
    # frames; observation frames keep their documented direction.)
    if frame.source == "computer_record" and frame_to_canonical is not None:
        metadata["frame_to_canonical"] = frame_to_canonical
    # WP-C3: the additive pointer block rides the verified image reply only, and
    # describes the FRAME (capture-time snapshot; DELTA 1 M4).
    if pointer_block is not None:
        metadata.update(pointer_block)
    return [
        TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)),
        ImageContent(type="image", data=bounded_b64, mimeType=mime_type),
    ]


@mcp.tool()
@_session_scoped
async def computer_fetch_image(session_id: str, image_id: str) -> Any:
    """Re-serve a frame this session captured earlier, by id.

    Every capture THIS SESSION'S REPLIES actually carry (computer_observe,
    computer_screenshot, computer_zoom, computer_execute) is retained under a unique
    ``image_id`` published on that same reply — EXACTLY the bytes the reply carried
    (for a derived visual view, the derived image with its labels, not the raw
    capture).  This tool hands that exact frame back to you as a real image block,
    with its metadata.  It never auto-sends: nothing is fetched until you ask, by id.

    THE MANDATORY SCENARIO: capture -> read the ``image_id`` off the reply -> decide
    you must look at that frame again -> call this tool.  This is how you re-examine
    something you skimmed past, or re-check a step's evidence after doing something
    else.

    A FETCHED IMAGE IS NOT NECESSARILY CURRENT.  Read ``captured_at`` and
    ``age_seconds`` before you treat it as present truth — it is a picture of the
    screen at the moment it was captured, and anything you have done since then is
    not in it.  When you need to know what is on screen NOW, call
    ``computer_observe``; use this tool to look back, never to look forward.

    The verified reply carries ``image_id``, ``captured_at``, ``age_seconds``, the
    stored ``width``/``height``, ``image_format``, the ``image_outbound_scale`` of
    the copy you are being returned NOW, the source ``observation_id``/``digest``,
    the ``visual_view`` when the frame is a derived view, the ``crop_region`` when
    the frame came from ``computer_zoom``, and the session's retention bounds.

    RETENTION: 16 frames / 24 MB / 3600 s per session, in memory only.  The oldest
    frame is dropped when a cap is exceeded and every frame is dropped when the
    session ends (stop_session, the idle timeout, or the server restarting).

    REJECTED REPLIES (ok:false, result:"rejected", ``reasons`` names the error):
    ``image_not_found`` — the id does not exist on THIS session.  An id belonging to
    a different session and a malformed id return the BYTE-IDENTICAL reply, so a
    wrong id can never probe another session's images.  ``image_expired`` — the
    frame was retired by the count, byte or age cap; ``retired_by`` names which.
    ``store_disposed`` — the session's store is already gone (teardown).  A stopped
    session returns ``session_stopped`` and an unknown one ``unknown_session``.

    This tool can never return a ``RAW:`` placeholder: a text-delivery session
    (``start_session(image_delivery="text")``) has no image bytes at all, so no id
    is ever issued on one.
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    # S-04 (WP7b, wired): a fetch is a READ-ONLY retrieval of evidence this session
    # already holds — it stays ALLOWED while a plan lease is active. There is
    # deliberately NO lease refusal on this surface (the observe/zoom refusal does
    # not extend here); the mid-lease fetch behavior is pinned by
    # tests/test_wp7b_plan_surface.py::test_a_fetch_succeeds_mid_lease.
    store = evidence_store(bundle.extra)
    if store is None:
        # Fail-closed: a live session always has a store (written at bundle
        # construction), so reaching here means the bundle is mid-teardown — the
        # exactly-once disposal already popped the key.  The answer is the typed
        # disposed refusal, never a partial image and never a resurrection; a store
        # disposed BETWEEN this check and the lookup is caught atomically by the
        # lookup itself (VAL-WP5 F3 — same lock as the frame read).
        return _evidence_rejection(
            EVIDENCE_REFUSE_DISPOSED, _EVIDENCE_STORE_DISPOSED_MESSAGE
        )
    # WP-R (ORVEX-2026-109): recording keyframes live in the SEPARATE recording
    # pool.  An id is resolved against the observation pool FIRST (unchanged for
    # observation ids — including their expiry answers); only a clean
    # not_found consults the recording pool, so a retired observation frame
    # still answers image_expired naming ITS policy.  Recording keyframes keep
    # the img_ id format and composite the cursor from the stored pointer
    # exactly like observation frames (WP-C).
    lookup: EvidenceLookup = store.lookup(image_id)
    if lookup.outcome == "not_found":
        recording_pool = recording_store(bundle.extra)
        if recording_pool is not None:
            recording_lookup = recording_pool.lookup(image_id)
            if recording_lookup.outcome != "not_found":
                return await _fetch_image_response(recording_pool, image_id, bundle)
    return await _fetch_image_response(store, image_id, bundle)
