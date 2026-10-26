"""AL-002 Adaptive Visual Representations: view providers, grid renderer, OCR-once spatial text.

Additive observation layer (MISSION-CRF-AVR-008, algorithm AL-002, implemented in wave W2).
Layering rule (the ``observation.py`` precedent): this module imports ONLY ``models`` +
stdlib + PIL — NEVER ``agent``/``server``/``verification``. The server (``server.py``) wires
the tool surface; grounding/validation consumers are untouched by design (non-interference
is a tested guarantee, AL-002 I-8, not a code change here).

Components (spec AL-002):

- :class:`RepresentationProvider` protocol + module registry: ``raw`` (pass-through,
  byte-identical default, I-1 by construction) and ``grid`` (PIL ImageDraw coordinate-grid
  renderer on a DERIVED copy, Component 3). Extensibility (R2): register one new provider
  object; no protocol redesign.
- :func:`spatial_text_of` — the OCR-once spatial-text cache (Component 2): lazy,
  idempotent, observation-scoped (I-2), stored in Observation ``PrivateAttr``s (the
  ``_frame`` precedent, I-3: never serialized, never digested). Honest degradation: a
  degraded/disabled UIA read (``ocr_text is None``) yields NO block (``None``), which the
  server ships as ``spatial_text: null`` plus the additive ``spatial_text_available:
  false`` marker; an empty region list is a truthful ``region_count: 0`` block, NOT
  degraded.
- :func:`resolve_view` — fail-closed view-name resolution (I-7): unknown/invalid values
  raise :class:`InvalidVisualViewError` naming the available views; never a silent
  substitution with ``raw``.

Amendment A1 additions (AL-002 "## AMENDMENT A1", owner addendum 2026-09-18):

- :func:`pixel_evidence_for` — the pre-registered A1.1 measured-evidence formula: a
  region bbox is cropped from the frame at native resolution, grayscaled, resized to
  64x32 (LANCZOS), and scored by the population standard deviation of the 2048 gray
  values, clamped into 0..1 by the FROZEN constants :data:`PIXEL_EVIDENCE_SIGMA_LO`
  / :data:`PIXEL_EVIDENCE_SIGMA_HI` (EXP-020.1 freeze, 2026-09-18). The
  field is EVIDENCE THAT PIXELS LOOK TEXT-BEARING — never a correctness probability and
  never a replacement for the UIA ``confidence`` truth (which stays ``null``).
- :func:`resolve_density` — A1.2 closed-enum resolution for ``visual_view_density``
  (``None`` -> :data:`DEFAULT_GRID_DENSITY`; unknown -> typed error naming the enum).
- :func:`render_crop_grid` / :func:`crop_observation_frame` — A1.3 zoom support: a
  native-resolution crop of the observation frame (no downscale, no upscale) and the
  grid rendered ON the crop with CANONICAL screenshot-space label VALUES (the crop
  origin is baked into the label numbers — A1.3 item 4 — unlike the full-frame view
  where the origin ships as metadata only).

Amendment A3 additions (AL-002 "## AMENDMENT A3" — PROPOSAL of record:
``research/AVR009-substrate-design.md``, owner directive D-3, MISSION-CRF-AVR-009):

- the pluggable text-substrate seam (``text_substrates.substrate_pass``) runs INSIDE the
  single OCR-once build below: the UIA needle's regions are the always-run base, and an
  optionally INSTALLED side substrate (a well-known package name — zero config, the
  install IS the opt-in) is auto-detected and merged AFTER them with honest provenance
  (``substrate`` / ``substrate_merged`` / ``substrate_deduped`` / ``substrate_ms`` /
  ``substrate_error``). Without a side package the block differs from the A2 baseline by
  exactly the one additive ``"substrate": "uia"`` key; the absent-request path performs
  ZERO new work (H19); a side substrate that fails or violates the contract fails OPEN
  to the default UIA regions with an honest error record — the observation never fails.

Coordinate doctrine (AL-002 I-6 / pipeline invariant F1): every coordinate served here is
CANONICAL SCREENSHOT space. The crop origin (``monitor.bounds[0:2]``) ships as block-level
provenance (``spatial_text.crop_origin``) and is NEVER baked into labels, regions, or grid
lines; ``ComputerBackend._map_to_physical`` remains the ONLY screenshot->physical
transform. ``spatial_text`` regions keep the screenshot-local ``TextRegion`` coordinates
(models.py convention) verbatim — passed through, never rewritten.
"""

from __future__ import annotations

import base64
import io
import statistics
import time
from typing import Any, Callable, NamedTuple, Protocol

from PIL import Image, ImageDraw

from .models import DEFAULT_VISUAL_VIEW, VISUAL_VIEWS, Observation
from .text_substrates import substrate_pass

#: Cap on the regions served in one spatial-text block (AL-002 I-9): the SAME
#: serialized-metadata cap the observe path already applies to ``ocr_text``/``ui_elements``
#: (``server.OBSERVE_METADATA_ELEMENT_CAP``). Mirrored here because the layering rule
#: forbids importing ``server``; the server passes its own constant through
#: :func:`spatial_text_of` (``cap=``) so the lockstep is structural, and a test pins the
#: equality. Dropped regions are reported via ``omitted_count`` — nothing is silently lost.
SPATIAL_TEXT_CAP = 20

#: Closed grid-density enum (AL-002 I-10, Component 3): density name ->
#: ``(minor_step_px, major_every)``. Free integers are never accepted; label count and
#: payload are bounded by construction. Default ``coarse`` (W2.1 Commander adjudication
#: of the EXP-020 variant gate, 2026-09-17: full-coordinate labels at major
#: intersections serve the grounding mission; the L3 abbreviated-label ambiguity class
#: is rejected outright; +3.6% cost vs L3 is inside every bar — provenance
#: experiments/EXP-020-results/). The palette/legibility tuning is EXP-020's evidence —
#: this spec freezes the MECHANISM, not the tuned values.
GRID_DENSITIES: dict[str, tuple[int, int]] = {
    "coarse": (128, 4),
    "standard": (64, 4),
    "fine": (32, 4),
}

#: Default grid density (W2.1 adjudicated; all three densities remain callable).
DEFAULT_GRID_DENSITY = "coarse"

#: Constant label/line palette (pre-EXP-020 constants; contrast scored in EXP-020 before
#: any bar freezes). PIL ImageDraw on an RGB frame takes solid fills: the minor lines use
#: a light gray that reads as low-alpha against typical UI content, majors a darker gray,
#: labels a high-contrast red for legibility.
MINOR_COLOR = (160, 160, 160)
MAJOR_COLOR = (80, 80, 80)
LABEL_COLOR = (178, 34, 34)

#: Label offset from a major-line intersection, in px (AL-002 defaults table: 3 px).
LABEL_OFFSET_PX = 3

#: A1.1 pixel-evidence normalization floor, in grayscale population-sigma units.
#: FROZEN by EXP-020.1 (2026-09-18): σ_lo=P90 negative class, σ_hi=P10 positive class,
#: no overlap (max neg 5.70 < min pos 25.74), accuracy 234/234 at 0.5; source
#: experiments/EXP-020-results/exp020_1-pixel-evidence.json; scope: negatives
#: flat-by-definition, live transfer checked at EXP-021.
PIXEL_EVIDENCE_SIGMA_LO = 0.0
#: A1.1 pixel-evidence normalization ceiling (see :data:`PIXEL_EVIDENCE_SIGMA_LO`).
#: FROZEN by EXP-020.1 (2026-09-18): σ_lo=P90 negative class, σ_hi=P10 positive class,
#: no overlap (max neg 5.70 < min pos 25.74), accuracy 234/234 at 0.5; source
#: experiments/EXP-020-results/exp020_1-pixel-evidence.json; scope: negatives
#: flat-by-definition, live transfer checked at EXP-021.
PIXEL_EVIDENCE_SIGMA_HI = 32.535

#: The A1.1 downsample target (pre-registered formula constant): the region crop is
#: resized to exactly this many gray values before the population sigma is measured.
PIXEL_EVIDENCE_RESIZE = (64, 32)


class VisualViewError(ValueError):
    """Base class for AL-002 view errors (typed, fail-closed; never silent fallbacks)."""


class InvalidVisualViewError(VisualViewError):
    """Unknown/invalid ``visual_view`` value (AL-002 I-7): a typed error, never raw."""


class ViewDeriveError(VisualViewError):
    """View derivation impossible (AL-002): no ``_frame`` stash AND undecodable payload.

    Never a silent ``raw`` substitution and never a recapture — the canonical observation
    path (raw serving, validation, execution) remains usable after this error.
    """


class DerivedView(NamedTuple):
    """The output of one view derivation (AL-002 Component 1).

    ``image`` is the derived RGB frame (a fresh PIL Image — the canonical observation's
    stash/payload is never mutated, I-5). For the pass-through ``raw`` view there ARE no
    derived pixels (honoring "no decode, no re-encode", I-1), so ``image`` is None and
    ``produced_by`` records the pass-through provenance.
    """

    image: Image.Image | None
    produced_by: str


class RepresentationProvider(Protocol):
    """AL-002 Component 1: one registered visual view of the current observation.

    ``id`` is the unique registry key (and the served ``visual_view`` metadata value).
    ``passthrough`` is the wiring discriminator: True = serve the observation's own
    payload bytes untouched (no derive call on the hot path — this is what makes raw
    byte-identity hold by construction); False = a derived view produced by
    :meth:`derive`. ``derive`` consumes ONLY the observation's stash/fields; it never
    captures, never mutates (I-4/I-5), and raises :class:`ViewDeriveError` (fail-closed)
    when derivation is impossible.
    """

    id: str
    passthrough: bool

    def derive(
        self, observation: Observation, density: str | None = None
    ) -> DerivedView:
        """Derive this view from the observation (no capture, no mutation).

        ``density`` is the A1.2 per-call ``visual_view_density`` (already resolved
        through the closed enum by the caller, or None for the default). It is
        meaningful only for views that render density-dependent annotations (the grid);
        pass-through views accept and ignore it (A1.2: "raw requests ignore the
        parameter silently").
        """
        ...


class RawPassThroughProvider:
    """The ``raw`` view: the observation's own payload, byte-identical (I-1).

    The wiring serves raw bytes via the pass-through branch (``passthrough=True``) and
    never calls :meth:`derive` on the hot path — honoring "no decode, no re-encode"
    means there is no PIL image to return for raw. :meth:`derive` exists for protocol
    conformance and introspection only.
    """

    id = "raw"
    passthrough = True

    def derive(
        self, observation: Observation, density: str | None = None
    ) -> DerivedView:
        """Protocol conformance only: no pixels are derived for the pass-through view.

        ``density`` is accepted and silently ignored (A1.2: density is meaningless for
        a pass-through; raw never renders anything).
        """
        return DerivedView(image=None, produced_by="raw:passthrough")


class GridOverlayProvider:
    """The ``grid`` view: a coordinate-grid annotation on a DERIVED copy (Component 3).

    The grid is a grounding aid, never a coordinate frame (I-6): lines and labels are
    drawn in canonical screenshot space at the served pixels' own geometry, and the crop
    origin ships as ``spatial_text.crop_origin`` metadata (``displayed + crop_origin =
    screen`` is a reader-side fact, never a rendering transform).
    """

    id = "grid"
    passthrough = False

    def derive(
        self, observation: Observation, density: str | None = None
    ) -> DerivedView:
        """Render the grid for one observation (stash-copy or exactly one payload decode).

        ``density`` (A1.2) selects the closed-enum density; ``None`` renders
        :data:`DEFAULT_GRID_DENSITY`.
        """
        return render_grid(observation, density=density or DEFAULT_GRID_DENSITY)


#: The module registry (AL-002 Component 1). R2 extensibility: register a new provider
#: object here (and extend ``models.VISUAL_VIEWS``); no protocol redesign. The keys are
#: pinned equal to ``VISUAL_VIEWS`` by a test.
_VIEW_REGISTRY: dict[str, RepresentationProvider] = {
    RawPassThroughProvider.id: RawPassThroughProvider(),
    GridOverlayProvider.id: GridOverlayProvider(),
}


def resolve_view(visual_view: str | None) -> RepresentationProvider:
    """Resolve a per-call view request to its provider (AL-002 Component 1, fail-closed).

    ``None`` resolves to :data:`~computer_use_mcp.models.DEFAULT_VISUAL_VIEW` (I-1: the
    caller only reaches this with a non-None request — the absent request is the
    untouched default path). Names are trimmed + casefolded (weak-model tolerance, the
    ``image_delivery`` precedent); anything unknown raises
    :class:`InvalidVisualViewError` naming the available views — NEVER a silent
    substitution with ``raw`` (I-7).
    """
    if visual_view is None:
        visual_view = DEFAULT_VISUAL_VIEW
    key = str(visual_view).strip().casefold()
    provider = _VIEW_REGISTRY.get(key)
    if provider is None:
        raise InvalidVisualViewError(
            f"visual_view must be one of {sorted(_VIEW_REGISTRY)}; got {visual_view!r}."
        )
    return provider


def resolve_density(density: str | None) -> str:
    """Resolve a ``visual_view_density`` request through the closed enum (A1.2, I-10).

    ``None`` resolves to :data:`DEFAULT_GRID_DENSITY` (``"coarse"`` per the W2.1
    adjudication); names are trimmed + casefolded (the ``resolve_view`` precedent);
    anything unknown raises :class:`InvalidVisualViewError` — the SAME typed
    ``invalid_visual_view``-class error family as view names (A1.2: "the same typed
    ``invalid_visual_view``-class error naming the enum"). Raw requests never reach
    this function: density is meaningless for a pass-through and is ignored silently
    there (A1.2), so the flags-off default path never resolves it either (H19).
    """
    if density is None:
        return DEFAULT_GRID_DENSITY
    key = str(density).strip().casefold()
    if key not in GRID_DENSITIES:
        raise InvalidVisualViewError(
            f"visual_view_density must be one of {sorted(GRID_DENSITIES)}; got {density!r}."
        )
    return key


def _decode_payload(image_base64: str) -> Image.Image:
    """Decode one base64 payload to RGB (fail-closed: :class:`ViewDeriveError`)."""
    try:
        decoded = base64.b64decode(image_base64, validate=True)
        image = Image.open(io.BytesIO(decoded))
        image.load()
    except Exception as exc:  # noqa: BLE001 - fail-closed: undecodable payload, no raw fallback
        raise ViewDeriveError(
            f"view derivation impossible: the observation payload is undecodable "
            f"({type(exc).__name__}) and no capture-time frame stash exists."
        ) from exc
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    return image


def payload_image_size(image_base64: str) -> tuple[int, int]:
    """Read a payload's pixel dimensions from the image header (no pixel decode)."""
    try:
        with Image.open(io.BytesIO(base64.b64decode(image_base64, validate=True))) as probe:
            return probe.size
    except Exception as exc:  # noqa: BLE001 - fail-closed: undecodable payload
        raise ViewDeriveError(
            f"view derivation impossible: the payload is undecodable ({type(exc).__name__})."
        ) from exc


def pixel_evidence_for(
    frame: Image.Image | None, x: int, y: int, width: int, height: int
) -> float | None:
    """The A1.1 measured pixel-evidence score for one region bbox (pre-registered formula).

    EXACTLY the spec formula, no tuning: crop the bbox from ``frame`` (clamped to the
    frame; a bbox outside the frame or degenerate — width/height <= 0 after clamping —
    is UNMEASURABLE and scores ``None``, never 0.0), convert to grayscale, resize to
    64x32 :data:`PIXEL_EVIDENCE_RESIZE` with ``Image.LANCZOS``, take the POPULATION
    standard deviation of the gray values, and clamp
    ``(sigma - PIXEL_EVIDENCE_SIGMA_LO) / (PIXEL_EVIDENCE_SIGMA_HI - PIXEL_EVIDENCE_SIGMA_LO)``
    into 0..1.

    Semantics (A1.1): this is EVIDENCE THAT PIXELS LOOK TEXT-BEARING (stroke-driven
    variance) — NOT a correctness probability; no consumer may read it as OCR accuracy.
    ``confidence`` stays the UIA truth (``None`` passthrough) — this field never
    replaces it. ``frame=None`` (no pixels obtainable) scores ``None``: unmeasurable is
    not zero.
    """
    if not isinstance(frame, Image.Image):
        return None
    left = max(0, int(x))
    top = max(0, int(y))
    right = min(frame.width, int(x + width))
    bottom = min(frame.height, int(y + height))
    if right - left <= 0 or bottom - top <= 0:
        return None  # degenerate/out-of-frame: unmeasurable, never a silent 0.0
    crop = frame.crop((left, top, right, bottom))
    gray = crop.convert("L").resize(PIXEL_EVIDENCE_RESIZE, Image.LANCZOS)
    # ``tobytes()`` on an L-mode image IS the gray-value sequence (one byte per pixel,
    # 2048 values) — avoids the Pillow-14 ``getdata`` deprecation.
    sigma = statistics.pstdev(gray.tobytes())  # population sigma over the 2048 values
    span = PIXEL_EVIDENCE_SIGMA_HI - PIXEL_EVIDENCE_SIGMA_LO
    return max(0.0, min(1.0, (sigma - PIXEL_EVIDENCE_SIGMA_LO) / span))


def _pixel_evidence_frame(observation: Observation) -> Image.Image | None:
    """The full frame for A1.1 measurement: the stash, else ONE payload decode per block.

    The OCR-once block build is the ONLY caller (A1.1: the decode happens at most once
    per observation and is reused for every region of that build). Returns ``None`` when
    no pixels are obtainable (absent stash AND undecodable payload): every region then
    measures ``pixel_evidence: None`` (unmeasurable) while the UIA text itself is still
    served — the measured field degrades, never the truth and never the block.
    """
    frame = getattr(observation, "_frame", None)  # I-4: the stash, never a recapture
    if isinstance(frame, Image.Image):
        return frame
    try:
        return _decode_payload(observation.image_base64)
    except ViewDeriveError:
        return None


def observation_for_payload(image_base64: str, frame: Any = None) -> Observation:
    """Build a THROWAWAY Observation for view derivation on the execute path (AL-002).

    The executed response carries an ``ExecutionResult`` (payload + the ``_frame``
    stash), not an Observation, but the providers derive from Observations; the adapter
    lets the SAME provider code run on both paths. The adapter is never served, never
    cached, never digested (I-3/I-5 untouched — the canonical post-action result stays
    exactly as the agent produced it).
    """
    if isinstance(frame, Image.Image):
        width, height = frame.size
    else:
        width, height = payload_image_size(image_base64)
    observation = Observation(image_base64=image_base64, width=width, height=height)
    if isinstance(frame, Image.Image):
        observation._frame = frame
    return observation


def label_anchors(width: int, height: int, minor: int, major_every: int) -> list[tuple[int, int]]:
    """Major-line intersection anchors (canonical screenshot space; bounded by the enum).

    Pure geometry: no crop origin, no observation — the grid NEVER reads monitor bounds,
    so two observations differing only in ``monitor.bounds`` render byte-identically
    (I-6: the crop origin is metadata, never a rendering transform).
    """
    major = minor * major_every
    return [(x, y) for x in range(0, width, major) for y in range(0, height, major)]


def label_text(x: int, y: int) -> str:
    """The label shown at one anchor: SCREENSHOT-space coordinates (I-6), nothing else."""
    return f"{x},{y}"


def _density_steps(density: str) -> tuple[int, int]:
    """The closed-enum lookup ``(minor_step_px, major_every)`` (I-10; fail-closed)."""
    try:
        return GRID_DENSITIES[density]
    except (KeyError, TypeError):
        raise ValueError(
            f"grid density must be one of {sorted(GRID_DENSITIES)}; got {density!r}."
        ) from None


def _draw_grid(
    base: Image.Image, minor: int, major_every: int, origin_x: int = 0, origin_y: int = 0
) -> None:
    """Draw the grid ON ``base`` in place (shared by the full-frame and crop renderers).

    Identical geometry for both callers: minor lines 1 px, major lines 2 px, labels at
    major intersections with the 3 px offset. The ONLY origin-aware part is the label
    VALUE: the full-frame view labels ``(x, y)`` (origin 0,0 — canonical screenshot
    space, I-6); the A1.3 crop view labels ``(origin_x + x, origin_y + y)`` — the crop
    origin IS baked into the label VALUES there (A1.3 item 4), while the crop origin
    still ships as block metadata so both readings stay derivable.
    """
    draw = ImageDraw.Draw(base)
    major = minor * major_every
    for x in range(0, base.width, minor):  # minor lines, 1 px, light gray
        draw.line([(x, 0), (x, base.height - 1)], fill=MINOR_COLOR, width=1)
    for y in range(0, base.height, minor):
        draw.line([(0, y), (base.width - 1, y)], fill=MINOR_COLOR, width=1)
    for x in range(0, base.width, major):  # major lines, 2 px, darker gray
        draw.line([(x, 0), (x, base.height - 1)], fill=MAJOR_COLOR, width=2)
    for y in range(0, base.height, major):
        draw.line([(0, y), (base.width - 1, y)], fill=MAJOR_COLOR, width=2)
    for (x, y) in label_anchors(base.width, base.height, minor, major_every):
        draw.text(
            (x + LABEL_OFFSET_PX, y + LABEL_OFFSET_PX),
            text=label_text(origin_x + x, origin_y + y),
            fill=LABEL_COLOR,
        )  # canonical screenshot-space label VALUES (I-6 / A1.3 item 4)


def render_grid(observation: Observation, density: str = DEFAULT_GRID_DENSITY) -> DerivedView:
    """Render the coordinate grid for one observation (AL-002 Component 3).

    Consumes the stashed capture-time frame when present (I-4: no re-decode on the local
    path); absent (fakes/legacy) decodes ``image_base64`` exactly once. The canonical
    observation is never mutated (I-5): drawing happens on a fresh copy. Density comes
    ONLY from the closed :data:`GRID_DENSITIES` enum (I-10).

    Grid geometry note: the lines/labels cover the SERVED pixels' own geometry (the
    stash/payload size). On the live path this equals ``observation.width/height`` by
    construction (the stash IS the capture-time frame of those dims); only the repo's
    fakes can diverge (the documented payload-quirk), and the served pixels are the
    honest ground for the drawn lattice.
    """
    minor, major_every = _density_steps(density)
    frame = getattr(observation, "_frame", None)  # I-4: the stash, never a recapture
    if isinstance(frame, Image.Image):
        base = frame.copy()  # independent object: the drawing must never touch the stash (I-5)
        produced_by = "grid:stash"
    else:
        base = _decode_payload(observation.image_base64)
        produced_by = "grid:decode"
    if base.mode not in ("RGB", "L"):
        base = base.convert("RGB")
    _draw_grid(base, minor, major_every)  # origin (0, 0): full-frame labels are (x, y)
    return DerivedView(image=base, produced_by=produced_by)


def render_crop_grid(
    crop: Image.Image,
    origin_x: int,
    origin_y: int,
    density: str = DEFAULT_GRID_DENSITY,
) -> Image.Image:
    """Render the grid ON a zoom crop with CANONICAL screenshot-space labels (A1.3 item 4).

    A label at crop-local position ``(x, y)`` displays the screenshot-space coordinate
    ``(origin_x + x, origin_y + y)`` — the crop origin IS baked into the label VALUES
    (unlike the full-frame view, where the origin ships as metadata only). The lines and
    geometry are identical to :func:`render_grid` at the requested closed-enum density,
    drawn on a fresh copy (the caller's crop image is never mutated, I-5).
    """
    minor, major_every = _density_steps(density)
    base = crop.copy() if crop.mode in ("RGB", "L") else crop.convert("RGB").copy()
    _draw_grid(base, minor, major_every, origin_x=origin_x, origin_y=origin_y)
    return base


def crop_observation_frame(
    observation: Observation, left: int, top: int, width: int, height: int
) -> tuple[Image.Image, str]:
    """Native-resolution crop of the observation's frame (A1.3 item 3; no rescale).

    Consumes the stashed capture-time frame when present (I-4: no decode on the local
    path); absent (fakes/legacy) decodes ``image_base64`` exactly once — recorded in the
    returned ``(crop, produced_by)`` provenance (``"zoom:stash"`` / ``"zoom:decode"``).
    NO downscale and NO upscale: the crop itself is the zoom (a region served at native
    pixels carries more effective resolution per UI element). The crop is a NEW image
    (``load()`` materializes it); the stash is never mutated (I-5). Fail-closed: no
    pixels obtainable raises :class:`ViewDeriveError` — never a silent full-frame
    fallback.

    W5.1 (R-12, fail-closed geometry guard): the source frame's size MUST equal the
    observation's declared ``width``/``height``. A crop from a divergent frame is NOT
    provably the requested region — PIL silently zero-pads out-of-bounds boxes, which
    would serve fabricated pixels with ``visual_view_scale: 1.0`` (a silent substitution,
    exactly what I-7 forbids). The live path always matches by construction (the stash IS
    the capture-time frame of the declared dims); divergence is a fake/legacy/edge world
    and is REFUSED with :class:`ViewDeriveError` naming both geometries — never a
    fabricated image, in-bounds region or not.
    """
    frame = getattr(observation, "_frame", None)  # I-4: the stash, never a recapture
    if isinstance(frame, Image.Image):
        base = frame
        produced_by = "zoom:stash"
    else:
        base = _decode_payload(observation.image_base64)
        produced_by = "zoom:decode"
    if (base.width, base.height) != (observation.width, observation.height):
        raise ViewDeriveError(
            f"zoom crop refused: the source frame is {base.width}x{base.height} but the "
            f"observation declares {observation.width}x{observation.height}; region "
            f"[{int(left)}, {int(top)}, {int(width)}, {int(height)}] cannot be proven to "
            f"lie inside the served pixels — no fabricated (zero-padded) image is served."
        )
    crop = base.crop((int(left), int(top), int(left) + int(width), int(top) + int(height)))
    crop.load()  # materialize: the crop owns its pixels; the stash is never touched (I-5)
    return crop, produced_by


def encode_png_b64(image: Image.Image) -> str:
    """Encode a derived view to base64 PNG (the ``_grab_png`` default: optimize=False)."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def outbound_view_scale(derived: Image.Image, outbound_b64: str) -> float | None:
    """Effective outbound downscale of a DERIVED view (AL-002 I-11 honesty datum).

    Header parse only (no pixel decode). Returns 1.0 when the outbound copy kept the
    derived dimensions, the actual ratio (< 1.0) when the existing budget ladder
    downscaled it, and None when the scale cannot be determined (the degradation is then
    simply not claimed rather than misreported).
    """
    try:
        served_w, served_h = payload_image_size(outbound_b64)
    except ViewDeriveError:
        return None
    if derived.width <= 0 or derived.height <= 0:
        return None
    return round(min(served_w / derived.width, served_h / derived.height), 4)


def spatial_text_of(
    observation: Observation,
    *,
    on_compute: Callable[[float], None] | None = None,
    cap: int = SPATIAL_TEXT_CAP,
    pixel_evidence: bool = False,
) -> dict[str, Any] | None:
    """Build (once) and return the observation's bounded spatial-text block (Component 2).

    Lazy, idempotent, observation-scoped (I-2): the UIA read itself is NOT re-run (it
    already happened inside ``observe()``); "OCR-once" means the derived BLOCK is built
    once per observation and reused by every view of that observation. The cache lives in
    the Observation's ``PrivateAttr``s (I-3: never serialized, never digested); lifetime
    = the Observation object's lifetime — there is deliberately NO cross-observation
    store (ST-03 boundary).

    ``on_compute`` (optional) is the server's instrumentation seam (create-on-first-use
    ``metrics.incr``/``record_latency`` — ``audit.py`` is intentionally NOT imported
    here): invoked exactly once per actual computation with the build's elapsed
    milliseconds. ``cap`` is the region cap (the server passes
    ``OBSERVE_METADATA_ELEMENT_CAP`` so the lockstep is structural); dropped regions are
    reported in ``omitted_count`` (I-9) — nothing silently lost.

    Honest degradation: ``ocr_text is None`` (the UIA read degraded or was disabled —
    stock Cortex has NO available-marker, so this layer provides its own) yields
    ``None``; the server ships ``spatial_text: null`` plus ``spatial_text_available:
    false``. An empty region list yields a truthful ``region_count: 0`` block — empty is
    NOT degraded. Region ``confidence`` passes through as-is (UIA regions carry
    ``confidence=None``; the block reports reality, never an invented 0.0).

    A2 (owner decision D2, 2026-09-18): ``pixel_evidence`` is OPT-IN. OFF (the default):
    NO sigma computation runs — the frame is never fetched or decoded — and the
    per-region entries OMIT the ``pixel_evidence`` field entirely (absent, never a null
    fabrication); the block honestly records ``"pixel_evidence_mode": "off"``. ON: every
    served region carries the MEASURED ``pixel_evidence`` (:func:`pixel_evidence_for` —
    the frozen A1.1 formula, σ_lo=0.0 / σ_hi=32.535, over the region's bbox on the full
    frame), computed ONCE per region during this single build; when the ``_frame`` stash
    is absent the payload is decoded AT MOST ONCE per build and reused for every region.
    The block carries ``"pixel_evidence_mode": "on"`` and the frozen normalization
    constants under ``pixel_evidence_sigma`` (static provenance, no cost, present in
    both modes). Unmeasurable regions (degenerate/out-of-frame bbox, or no pixels
    obtainable at all) carry ``None`` — unmeasurable is not zero, and the UIA text is
    never lost to a measurement failure. H21' (A2): the flag shapes WHAT the single
    build computes, never the count — the first build memoizes its mode, and later
    requests with the other flag reuse the cached block unchanged (counter stays 1).

    A3 (owner directive D-3, MISSION-CRF-AVR-009; design of record
    ``research/AVR009-substrate-design.md``): the pluggable text-substrate seam runs
    INSIDE this single build (:func:`text_substrates.substrate_pass`). The UIA
    needle's regions are the ALWAYS-RUN base; an installed side substrate
    (``cortex_text_ocr`` — zero-config auto-detect; the install IS the opt-in for the
    added latency) is merged AFTER them (positive-area bbox dedupe, UIA wins) and the
    block records the provenance: ``substrate`` (``"uia"`` default |
    ``"ocr:<name>"`` | ``"dom:<name>"``) plus, when a side substrate ran,
    ``substrate_merged``/``substrate_deduped``/``substrate_ms`` — or, when it failed
    the contract, ``substrate: "uia"`` + an honest ``substrate_error`` (fail-open to
    the default; the observation never fails). Without a side package the block
    differs from the A2 baseline by exactly the one additive ``substrate`` key and
    the frame fetch below stays untouched (the lazy fetch is only consulted when
    evidence is on or a pixel-based side substrate actually runs). Cap and
    ``omitted_count`` apply AFTER the merge (I-9) — nothing silently lost. H21' is
    untouched: the seam shapes WHAT the single build computes, never the count.
    """
    if observation._spatial_text_derived:
        return observation._spatial_text  # I-2: reuse; no recompute
    started = time.perf_counter()
    regions = observation.ocr_text
    if regions is None:
        # Degraded/disabled UIA read: no block. The block shape has no honest way to
        # say "no data" (an empty regions list would claim a read happened) — the
        # server attaches the ``spatial_text_available: false`` marker instead.
        block: dict[str, Any] | None = None
    else:
        # A3: lazy frame fetch, memoized per build — evidence-on and a pixel-based
        # side substrate share AT MOST ONE fetch/decode per build; in the default
        # world (no side package, evidence off) it is never consulted.
        frame_cache: list[Image.Image | None] = []

        def _frame_once() -> Image.Image | None:
            if not frame_cache:
                frame_cache.append(_pixel_evidence_frame(observation))
            return frame_cache[0]

        uia_regions = list(regions)
        merged, substrate_keys = substrate_pass(observation, uia_regions, _frame_once)
        capped = merged[:cap]
        # A2: one frame for the whole build ONLY when evidence is requested (stash, or
        # at most ONE payload decode); OFF skips the fetch entirely (no cost, no
        # decode) unless the A3 side substrate already fetched it (memoized above).
        evidence_on = bool(pixel_evidence)
        frame = _frame_once() if (evidence_on and capped) else None
        block = {
            "observation_id": observation.observation_id,
            "coordinate_space": observation.coordinate_space.value,
            "coordinate_scale": [observation.coordinate_scale_x, observation.coordinate_scale_y],
            "crop_origin": list(observation.monitor.bounds[0:2]) if observation.monitor else [0, 0],
            "region_count": len(capped),
            "omitted_count": max(0, len(merged) - cap),
            **substrate_keys,
            "pixel_evidence_mode": "on" if evidence_on else "off",
            "pixel_evidence_sigma": [PIXEL_EVIDENCE_SIGMA_LO, PIXEL_EVIDENCE_SIGMA_HI],
            "regions": [
                (
                    {
                        **region.model_dump(),
                        "pixel_evidence": pixel_evidence_for(
                            frame, region.x, region.y, region.width, region.height
                        ),
                    }
                    if evidence_on
                    else region.model_dump()
                )
                for region in capped
            ],
        }
    observation._spatial_text = block
    observation._spatial_text_derived = True
    observation._spatial_text_compute_count += 1
    if on_compute is not None:
        on_compute((time.perf_counter() - started) * 1000.0)
    return block


def available_views() -> list[str]:
    """The registered view ids, sorted (teaching text for ``invalid_visual_view``)."""
    return sorted(_VIEW_REGISTRY)


def _assert_registry_consistency() -> None:
    """Import-time guard: the registry keys and ``models.VISUAL_VIEWS`` agree (R2)."""
    if tuple(sorted(_VIEW_REGISTRY)) != tuple(sorted(VISUAL_VIEWS)):
        raise RuntimeError(
            f"visual-view registry {sorted(_VIEW_REGISTRY)} disagrees with "
            f"models.VISUAL_VIEWS {sorted(VISUAL_VIEWS)}; register both or neither."
        )


_assert_registry_consistency()
