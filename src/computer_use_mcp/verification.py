"""Semantic verification framework: did the intended state transition actually occur?

Layering (master-mission section 5): this module imports only ``models`` (and PIL for the
pixel-diff strategy). It must NEVER import ``provider`` — model-based visual verification
receives a :class:`ModelJudge` callback instead, breaking the provider->verification cycle.

Outcome doctrine (Goal.md section 7, P0-A): every verification produces
``outcome`` in ``{"verified", "failed", "uncertain"}``. ``uncertain`` is NEVER success —
strategies degrade to ``uncertain`` when they lack the data to decide, and the engine's
combined result for all-uncertain chains is itself ``uncertain``. No code path in this
module maps ``uncertain`` to ``verified``.

Tier-default doctrine (R-01): the built-in chain is ordered DETERMINISTIC-FIRST —
predicate, window, process, UI-control-text, focus signals — and the pixel-diff tier
(:class:`ScreenshotDiffStrategy`) is the AMBIGUOUS-BAND ESCALATION, consulted only
when every deterministic tier ahead of it is inconclusive (the engine returns the
first definitive verdict, so a deterministic ``verified``/``failed`` ends the chain
before pixels run). For ``type`` the default intent is ``expected_text`` decided by
:class:`UiControlTextStrategy` (window title + control text, needle = the TYPED
text — see the controller's ``_build_intent``); for ``keypress``/``hotkey`` the
deterministic default is the launch-prefix ``window_state`` promotion, and a stated
effect keeps the pixel band honest in the escalation role only: absent/sub-threshold
OR above-floor evidence alike degrades to ``uncertain`` (F1 fix 1.1 / E-03 — the
pixel tier measures "some pixels moved", which is not the described effect, so it
never claims one; the deterministic tiers are the verify path for a stated effect).
The focus-change transition signals stay
CLICK-scoped deliberately: a window/element identity change must never verify a
non-click effect (an unrelated foreground steal would become a false success).

Confidence doctrine (Goal.md section 6): verification confidence is reported separately
from model/grounding confidence; each strategy returns its own evidence-grounded value.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from PIL import Image, ImageChops

from .models import CaptureProvenance, Observation, VerificationResult

__all__ = [
    "DEFAULT_STRATEGY_CHAIN",
    "DIFF_FAST_ENV",
    "FOCUS_CHANGE_INTENT_FLAG",
    "OCR_TEXT_VERIFICATION_ENV",
    "DeterministicPredicateStrategy",
    "FocusChangeStrategy",
    "IDENTITY_ELEMENT_SCAN_LIMIT",
    "IDENTITY_MIN_TOKEN_CHARS",
    "IDENTITY_STOP_TOKENS",
    "ModelJudge",
    "ModelVisualStrategy",
    "ProcessStateStrategy",
    "ScreenshotDiffStrategy",
    "TextPredicateStrategy",
    "UiControlTextStrategy",
    "VerificationEngine",
    "VerificationIntent",
    "VerificationKind",
    "VerificationStrategy",
    "WindowStateStrategy",
    "ocr_text_verification_enabled",
    "stem",
]

#: Pixel mean-difference at or above this value counts as a visual change (legacy threshold).
DEFAULT_DIFF_THRESHOLD = 1.0

#: Per-pixel max-channel delta that counts as one strongly-changed pixel. A real UI
#: transition (a drawn stroke, a dialog, a toggled control) alters a compact set of pixels
#: by a large delta while the screen-wide mean barely moves, so the mean alone cannot
#: see it (e.g. a 200x2 px stroke moves the 1920x1080 mean by ~0.05).
STRONG_PIXEL_DELTA = 40

#: At least this many strongly-changed pixels count as a visual change even when the
#: screen-wide mean stays below ``DEFAULT_DIFF_THRESHOLD``. Below this the change is
#: treated as ambiguous flicker (caret, clock tick), never as proof of a transition.
STRONG_CHANGE_MIN_PIXELS = 50

#: Legacy confidence used when a stated expectation was not observed.
_EXPECTATION_FAILED_CONFIDENCE = 0.85

#: Confidence assigned when a stated stability expectation held.
_STABILITY_VERIFIED_CONFIDENCE = 0.95

#: Confidence for identity matches (window/process) backed by real observation fields.
_IDENTITY_MATCH_CONFIDENCE = 0.9

#: R-7 kill-switch: any value other than "0" keeps the fast diff path; the exact
#: string "0" restores the pre-R-7 full-frame computation for every diff.
DIFF_FAST_ENV = "CORTEX_DIFF_FAST"

#: R-7: the fast diff path is enabled unless the operator disables it via env.
_DIFF_FAST_DISABLED = None  # resolved lazily (module import must not read env eagerly)


def _diff_fast_enabled() -> bool:
    """R-7 kill-switch resolution: ``CORTEX_DIFF_FAST=0`` restores the legacy diff.

    Read lazily so tests (and import order) can toggle the env per-case; every
    other value — unset, garbage, "1" — leaves the fast path ON (fail-fast).
    """
    return os.getenv(DIFF_FAST_ENV, "").strip() != "0"

#: REM-B (H2c): confidence for a deterministic focus/window/digest change signal.
_DETERMINISTIC_CHANGE_CONFIDENCE = 0.85

#: REM-B (H2c): intent metadata key marking a focus-type click expectation (set by
#: ``ComputerUseAgent._build_intent``; the strategy only claims marked intents so
#: every other visual-change consumer keeps its exact existing semantics).
FOCUS_CHANGE_INTENT_FLAG = "focus_change_click"


class VerificationKind(StrEnum):
    """Kind of semantic question the controller wants answered about a transition."""

    EXPECTED_TEXT = "expected_text"
    WINDOW_STATE = "window_state"
    PROCESS_STATE = "process_state"
    PREDICATE = "predicate"
    VISUAL_CHANGE = "visual_change"
    MODEL_JUDGE = "model_judge"


@dataclass
class VerificationIntent:
    """What the controller expects to be true after an action, and how to check it.

    Exactly one primary criterion is usually set, matching ``kind``:
    ``expected_text`` -> :attr:`expected_text`; ``window_state`` ->
    :attr:`expected_window_title` / :attr:`require_bounds_change`; ``process_state`` ->
    :attr:`expected_process_name` / :attr:`expected_pid`; ``predicate`` -> :attr:`predicate`;
    ``visual_change`` -> :attr:`expected_change`; ``model_judge`` -> a configured
    :class:`ModelJudge`. ``expected_change`` is also honored as a *supporting* stability
    criterion by the screenshot-diff strategy for non-visual intents.
    """

    kind: str = VerificationKind.VISUAL_CHANGE.value
    expected_text: str | None = None
    expected_window_title: str | None = None
    window_title_match: str = "contains"  # "contains" | "equals"
    expected_process_name: str | None = None
    expected_pid: int | None = None
    expected_change: bool | None = None
    expected_effect: str | None = None
    predicate: Callable[[Observation, Observation], bool | None] | None = None
    predicate_name: str | None = None
    diff_threshold: float = DEFAULT_DIFF_THRESHOLD
    require_bounds_change: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class VerificationStrategy(Protocol):
    """Protocol every verification strategy implements."""

    @property
    def name(self) -> str:
        """Unique strategy name recorded in ``VerificationResult.verification_method``."""
        ...

    def can_verify(self, intent: VerificationIntent) -> bool:
        """Return True when this strategy applies to ``intent`` (without side effects)."""
        ...

    def verify(self, intent: VerificationIntent, before: Observation, after: Observation) -> VerificationResult:
        """Verify the intent; raise nothing — implementations must self-degrade."""
        ...


def _uncertain(
    strategy: str,
    note: str,
    evidence: list[str],
    confidence: float,
    changed: bool,
    capture_provenance: CaptureProvenance | None = None,
) -> VerificationResult:
    return VerificationResult(
        outcome="uncertain",
        changed=changed,
        note=note,
        confidence=confidence,
        evidence=evidence,
        verification_method=strategy,
        capture_provenance=capture_provenance,
    )


def _frame_identity_bytes(image: Image.Image) -> bytes | bytearray | None:
    """The exact byte identity of a diff frame's pixels (for capture provenance).

    Preference order (v0.7.1 Defect A): the producing backend's stashed raw capture
    buffer (``_frame_raw`` — the true capture bytes, zero re-encode cost), else the
    RGB pixel bytes of the decoded frame. ``None`` on any structural doubt (no
    pixels, non-RGB decoded frame) — provenance is best-effort additive evidence and
    must never break (or pretend to exist for) a verification.
    """
    raw = getattr(image, "_frame_raw", None)
    if isinstance(raw, (bytes, bytearray)):
        return raw
    try:
        if image.mode == "RGB":
            return image.tobytes()
    except Exception:  # noqa: BLE001 - a broken frame degrades to no provenance
        return None
    return None


def _capture_provenance(before_image: Image.Image, after_image: Image.Image) -> CaptureProvenance | None:
    """Hash identity for the two frames the visual tier is about to diff.

    sha256 over each side's frame identity bytes (see :func:`_frame_identity_bytes`)
    plus the byte sizes. Measured cost on the reference machine: ~1.4 ms per ~1.9 MB
    frame (OpenSSL SHA-NI); the considered ``blake2b(digest_size=16)`` measured
    ~4.1 ms per frame and missed the <= ~2 ms/frame budget, so sha256 ships. The
    result is pure diagnostics for the capture-source integrity guard — it NEVER
    changes a verdict, and any failure degrades to ``None``.
    """
    try:
        before_data = _frame_identity_bytes(before_image)
        after_data = _frame_identity_bytes(after_image)
        if before_data is None or after_data is None:
            return None
        return CaptureProvenance(
            before_sha256=hashlib.sha256(before_data).hexdigest(),
            after_sha256=hashlib.sha256(after_data).hexdigest(),
            before_bytes=len(before_data),
            after_bytes=len(after_data),
        )
    except Exception:  # noqa: BLE001 - provenance must never break verification
        return None


def _provenance_evidence_line(provenance: CaptureProvenance | None) -> str | None:
    """The single additive evidence line attesting the compared frames' identity."""
    if provenance is None:
        return None
    return (
        f"capture {provenance.algorithm} "
        f"before={provenance.before_sha256[:12]}… after={provenance.after_sha256[:12]}… "
        f"bytes={provenance.before_bytes}/{provenance.after_bytes}"
    )


def _frame_for(observation: Observation) -> Image.Image | None:
    """The observation's pixels as a decoded RGB frame, without re-encoding work.

    R-5 (W2) fast path: when the producing backend stashed its capture-time RGB frame
    (``Observation._frame``, private, never serialized), return it directly — the
    verification tier then skips a base64 decode + PNG decode of a frame the pipeline
    just encoded. No stash (tests, fakes, restored checkpoints, legacy backends)
    returns ``None`` and the caller falls back to decoding ``image_base64`` exactly as
    before, so the tier's semantics are identical on both paths.
    """
    frame = getattr(observation, "_frame", None)
    if frame is None:
        return None
    try:
        if frame.mode != "RGB":
            return None  # conservative: only a same-mode frame may fast-path
        return frame
    except Exception:  # noqa: BLE001 - a broken stash degrades to the decode path
        return None


def _diff_magnitude(before_image: Image.Image, after_image: Image.Image) -> tuple[float, int]:
    """(mean difference, strongly-changed pixel count) between two RGB frames.

    Bit-identical to the historical computation (proven equal to <1e-9 on live
    capture pairs): the mean is derived from the diff's own 768-bin histogram —
    ``sum(i * count) / pixels`` per band — instead of a SECOND full-frame pass via
    ``ImageStat.Stat``, and the strongly-changed count is the same
    ``max(R,G,B) >= STRONG_PIXEL_DELTA`` histogram tail the pixel tier always used.
    R-5 (W3): one histogram pass replaces two.

    R-7 fast path (PROVABLY verdict-preserving; pinned on a 23-pair recorded
    corpus + adversarial worst cases in tests/test_r7_diff_acceleration.py):
    the full-frame computation spends ~6 full-frame passes (~25 ms at 1920x1080)
    even when the two frames differ in a tiny region. Three exact reductions:

    1. **Identical-raw short-circuit.** When both frames carry the producing
       backend's stashed raw pixel buffer (``_frame_raw``; set only for frames
       built zero-copy over one readback buffer) and the buffers are equal
       bytes, the pixels are identical, so the diff is uniformly zero and the
       result is exactly ``(0.0, 0)`` — the same tuple the full computation
       returns for a frame against itself (pinned). The comparison is a C
       memcmp (~0.5 ms for 6 MB) and the result never diverges: equal bytes
       are equal pixels by construction.
    2. **bbox-crop reduction.** ``Image.getbbox()`` on the diff (any-band
       nonzero; ~0.6 ms) yields the tight bounding box of every non-zero diff
       byte. Outside that box every diff byte is exactly 0, so the per-band
       histograms there contribute only to the zero bin and the strong tail
       there is empty. Cropping the diff to the box and running the SAME
       per-band statistics on the crop with the FULL-frame pixel count as the
       mean denominator produces the identical mean (the zero-bin difference
       cancels in ``sum / pixels``) and the identical strongly-changed count
       (no pixel outside the box can reach ``STRONG_PIXEL_DELTA``). The crop is
       skipped when the box is the whole frame (no 6 MB copy for nothing) and
       the whole fast path is skipped for non-3-band images (the single-band
       branch keeps its exact historical form).

    ``CORTEX_DIFF_FAST=0`` restores the pre-R-7 full-frame computation exactly.
    """
    if _diff_fast_enabled():
        fast = _diff_magnitude_fast(before_image, after_image)
        if fast is not None:
            return fast
    diff = ImageChops.difference(before_image, after_image)
    pixels = diff.width * diff.height
    if pixels <= 0:
        return 0.0, 0
    bands = diff.split()
    if len(bands) == 1:
        histogram = diff.histogram()  # single band: histogram() is already the band's
        mean_difference = (
            sum(index * count for index, count in enumerate(histogram)) / pixels
        )
        max_band = bands[0]
    else:
        max_band = ImageChops.lighter(ImageChops.lighter(bands[0], bands[1]), bands[2])
        histogram = diff.histogram()
        means = [
            sum(index * count for index, count in enumerate(histogram[band * 256:(band + 1) * 256]))
            / pixels
            for band in range(3)
        ]
        mean_difference = sum(means) / 3.0
    strongly_changed = sum(max_band.histogram()[STRONG_PIXEL_DELTA:])
    return mean_difference, strongly_changed


def _diff_magnitude_fast(
    before_image: Image.Image, after_image: Image.Image
) -> tuple[float, int] | None:
    """R-7 exact fast path; ``None`` means "not applicable, run the legacy body".

    The reductions are pure equalities (see :func:`_diff_magnitude`); on any
    structural doubt (missing stash, weird mode, zero-size) it returns ``None``
    and the caller runs the untouched legacy computation — the fast path can
    only lose speed, never exactness.
    """
    # (1) identical-raw short-circuit: equal raw bytes are equal pixels.
    raw_before = getattr(before_image, "_frame_raw", None)
    raw_after = getattr(after_image, "_frame_raw", None)
    if (
        isinstance(raw_before, (bytes, bytearray))
        and isinstance(raw_after, (bytes, bytearray))
        and raw_before is not raw_after
        and len(raw_before) == len(raw_after)
        and bytes(raw_before) == bytes(raw_after)
    ):
        return 0.0, 0
    if before_image.mode != "RGB" or after_image.mode != "RGB":
        return None  # conservative: only plain RGB fast-paths. RGBA's getbbox is
        # alpha-blind (measured: a 190-delta pixel with unchanged alpha yields
        # bbox None), so RGB-adjacent modes keep the legacy body verbatim.
    if before_image.width != after_image.width or before_image.height != after_image.height:
        return None  # callers gate dimension equality, but never assume it
    pixels = before_image.width * before_image.height
    if pixels <= 0:
        return None
    # (2) bbox-crop reduction: statistics over the non-zero box only.
    diff = ImageChops.difference(before_image, after_image)
    bbox = diff.getbbox()
    if bbox is None:
        return 0.0, 0  # uniformly zero diff — identical to the legacy (0.0, 0)
    if bbox != (0, 0, diff.width, diff.height):
        diff = diff.crop(bbox)
    bands = diff.split()
    if len(bands) != 3:
        return None  # the legacy multi-band branch is RGB-shaped; RGB-adjacent modes
        # (e.g. RGBA, whose getbbox is alpha-blind) stay on the legacy body
    histogram = diff.histogram()
    if len(histogram) != 768:
        return None  # non-8-bit bands: legacy histogram semantics differ
    # Mean over the FULL frame's pixel count, with the legacy's EXACT arithmetic:
    # per-band ``sum(i*count) / pixels`` first, then the three band means averaged
    # (same division order — float results are bit-identical, not merely close).
    # Pixels outside the box contribute only zero-bin counts, so each crop band sum
    # equals the full-frame band sum.
    means = [
        sum(index * count for index, count in enumerate(histogram[band * 256:(band + 1) * 256]))
        / pixels
        for band in range(3)
    ]
    mean_difference = sum(means) / 3.0
    # Strongly-changed over the crop: no pixel outside the box has any non-zero
    # diff byte, so none can reach STRONG_PIXEL_DELTA; the crop's tail IS the
    # full frame's tail (same lighter-chain over R, G, B as the legacy body).
    max_band = ImageChops.lighter(ImageChops.lighter(bands[0], bands[1]), bands[2])
    strongly_changed = sum(max_band.histogram()[STRONG_PIXEL_DELTA:])
    return mean_difference, strongly_changed


def _definitive(
    strategy: str,
    outcome: str,
    note: str,
    evidence: list[str],
    confidence: float,
    changed: bool,
    capture_provenance: CaptureProvenance | None = None,
) -> VerificationResult:
    return VerificationResult(
        outcome=outcome,
        changed=changed,
        note=note,
        confidence=confidence,
        evidence=evidence,
        verification_method=strategy,
        capture_provenance=capture_provenance,
    )


class ScreenshotDiffStrategy:
    """Pixel-difference verification, upgraded with explicit uncertainty.

    Full positive+negative semantics for ``kind="visual_change"``:

    - dimensions changed -> ``failed`` (legacy-compatible note/confidence 0.5);
    - a screen counts as *changed* when the screen-wide mean difference reaches the
      threshold OR at least ``STRONG_CHANGE_MIN_PIXELS`` pixels changed by
      ``STRONG_PIXEL_DELTA`` or more (mean alone cannot see compact changes such as
      drawn strokes or small controls);
    - ``expected_change=True``: a BARE change expectation (no described effect):
      change detected -> ``verified``; none -> ``failed``. An intent carrying a
      stated ``expected_effect`` abstains in BOTH directions (F1 fix 1.1 / E-03):
      absent pixel evidence is not proof of absence, and a present change is only
      "some pixels moved" — not proof that the described effect happened — so both
      degrade to ``uncertain`` and this tier never names the effect; the
      deterministic tiers are the verify path for stated effects;
    - ``expected_change=False``: no change detected -> ``verified``; change ->
      ``failed``;
    - ``expected_change=None`` (no expectation stated): change detected ->
      ``verified`` (legacy compatibility); identical pixels or an ambiguous sub-threshold
      change -> ``uncertain`` (a changed screenshot alone must not be read as success).

    For every other intent kind the strategy acts only as a cheap *supporting* check:
    a pixel-identical ``after`` image definitively falsifies an intent that expected
    change, a changed image falsifies one that expected stability, and otherwise it
    abstains (``uncertain``).
    """

    @property
    def name(self) -> str:
        return "screenshot_diff"

    def can_verify(self, intent: VerificationIntent) -> bool:
        return True  # candidate for every intent; verify() decides whether it can be definitive

    def verify(self, intent: VerificationIntent, before: Observation, after: Observation) -> VerificationResult:
        if before.width != after.width or before.height != after.height:
            return _definitive(
                self.name,
                "failed",
                "Screen dimensions changed; semantic verification is required.",
                [f"Screenshot dimensions {before.width}x{before.height} -> {after.width}x{after.height}."],
                0.5,
                changed=True,
            )
        provenance: CaptureProvenance | None = None
        try:
            before_image, after_image = self._frames(before, after)
            # v0.7.1 Defect A: identity of the exact frames about to be diffed —
            # computed from bytes already in hand (no new capture, no sleep), so a
            # zero diff is always accompanied by evidence the frames were real and
            # distinct (or identical-by-hash from a frozen capture source).
            provenance = _capture_provenance(before_image, after_image)
            if before_image.size != after_image.size:
                return _uncertain(
                    self.name,
                    "Decoded image sizes disagree with observation metadata; cannot diff reliably.",
                    [f"Decoded sizes {before_image.size} vs {after_image.size}."],
                    0.2,
                    changed=True,
                    capture_provenance=provenance,
                )
            mean_difference, strongly_changed = _diff_magnitude(before_image, after_image)
        except Exception as exc:  # noqa: BLE001 - degrade, never escape as success
            return _uncertain(
                self.name,
                "Screenshot diff could not be computed; both observations are treated as unverifiable.",
                [f"{type(exc).__name__}: {exc}"],
                0.0,
                changed=False,
                capture_provenance=provenance,
            )

        threshold = max(intent.diff_threshold, 0.0)
        changed = (
            mean_difference >= threshold
            or strongly_changed >= STRONG_CHANGE_MIN_PIXELS
        )

        if intent.kind == VerificationKind.VISUAL_CHANGE:
            result = self._verify_visual_change(intent, mean_difference, changed, strongly_changed)
        else:
            result = self._verify_supporting(intent, mean_difference, changed, strongly_changed)
        # v0.7.1 Defect A: every verdict this tier emits rides with the compared
        # frames' identity — verdict text/confidence/outcome stay EXACTLY as computed
        # above; only additive diagnostics are attached (fail-open on the verdict,
        # fail-loud in diagnostics).
        if provenance is not None and result.capture_provenance is None:
            result.capture_provenance = provenance
            provenance_line = _provenance_evidence_line(provenance)
            if provenance_line is not None:
                result.evidence = [*result.evidence, provenance_line]
        return result

    def _verify_visual_change(
        self,
        intent: VerificationIntent,
        mean_difference: float,
        changed: bool,
        strongly_changed: int,
    ) -> VerificationResult:
        expected = intent.expected_change
        evidence = [
            f"Mean pixel difference {mean_difference:.6f} (threshold {intent.diff_threshold:.6g}).",
            (
                f"Strongly-changed pixels (delta >= {STRONG_PIXEL_DELTA}): {strongly_changed} "
                f"(change threshold {STRONG_CHANGE_MIN_PIXELS})."
            ),
        ]
        change_confidence = min(max(mean_difference / 32.0, strongly_changed / 400.0), 1.0)
        if expected is True:
            if changed:
                # F1 fix 1.1 (decision D-1(a), ORVEX-2026-108 WP4a / E-03): the stated
                # effect is authoritative WHEN PRESENT, and "some pixels changed" is
                # not it.  This tier compares two frames and nothing else — the effect
                # string is never read here — so a verified verdict would be a claim
                # the evidence cannot support, and the note used to assert the effect
                # out loud.  Absent change and present change are the SAME evidence
                # class for a DESCRIBED effect (both are a generic pixel measurement),
                # so they abstain identically.  A BARE change expectation (no
                # described effect) is untouched below: there a pixel change IS the
                # whole claim.
                if intent.expected_effect:
                    return _uncertain(
                        self.name,
                        (
                            "A visual change was observed (mean pixel difference "
                            f"{mean_difference:.6f}, {strongly_changed} strongly-changed "
                            f"pixels), which is NOT proof of the stated expectation; many "
                            "unrelated redraws (cursors, clocks, tooltips, another window) "
                            "move pixels too. This tier cannot compare the described "
                            "effect, so it abstains. Uncertain is never success."
                        ),
                        evidence,
                        0.4,
                        changed=True,
                    )
                return _definitive(
                    self.name,
                    "verified",
                    "Visual state changed.",
                    evidence,
                    change_confidence,
                    changed=True,
                )
            # a stated
            # expected_effect ("Hex input focused", "Enter commits the shape")
            # describes a transition the pixel tier often CANNOT observe — focusing a
            # field, committing a shape with thin dashed handles below the
            # STRONG_CHANGE_MIN_PIXELS floor, a slow dialog. A zero/sub-threshold diff
            # here is the strategy's no-data case, not proof of absence — the exact
            # evidence class this tier degrades to ``uncertain`` when no expectation is
            # stated (below). Emitting a definitive ``failed`` from absent evidence
            # false-failed the live Paint sessions (keypress Enter with 0.000000 diff
            # while the commit succeeded) and killed follow_ups queues. Doctrine: ANY
            # stated-effect visual-change intent degrades to ``uncertain`` (never
            # success; ok stays False). The strict legacy failure is kept ONLY for
            # OBSERVE-style bare change expectations (``expected_change=True`` with NO
            # described effect) — there, a pixel change is the whole claim, so absent
            # change IS the failure.
            if intent.expected_effect:
                note = (
                    "No pixel evidence of the stated expectation"
                    + f": {intent.expected_effect}"
                    + "; many real transitions (focus, caret, thin handles, slow "
                    "dialogs) are invisible or sub-threshold to the pixel diff."
                )
                return _uncertain(self.name, note + " Uncertain is never success.", evidence, 0.4, changed=False)
            return _definitive(
                self.name,
                "failed",
                "Expected change was not observed.",
                evidence,
                _EXPECTATION_FAILED_CONFIDENCE,
                changed=False,
            )
        if expected is False:
            if not changed:
                return _definitive(
                    self.name,
                    "verified",
                    "Visual state stable as expected.",
                    evidence,
                    _STABILITY_VERIFIED_CONFIDENCE,
                    changed=False,
                )
            return _definitive(
                self.name,
                "failed",
                "Expected no visual change, but the screen changed.",
                evidence,
                change_confidence,
                changed=True,
            )
        # No expectation stated: pixels alone cannot prove semantic success.
        if not changed:
            if mean_difference <= 0.0 and strongly_changed <= 0:
                return _uncertain(
                    self.name,
                    "No visual change detected and no change expectation was stated; visual identity "
                    "alone cannot prove the intended transition.",
                    evidence,
                    0.9,
                    changed=False,
                )
            return _uncertain(
                self.name,
                "Sub-threshold visual change is ambiguous without a stated expectation.",
                evidence,
                0.4,
                changed=False,
            )
        return _definitive(
            self.name,
            "verified",
            "Visual state changed.",
            evidence,
            change_confidence,
            changed=True,
        )

    def _verify_supporting(
        self,
        intent: VerificationIntent,
        mean_difference: float,
        changed: bool,
        strongly_changed: int,
    ) -> VerificationResult:
        evidence = [
            f"Mean pixel difference {mean_difference:.6f} (threshold {intent.diff_threshold:.6g}).",
            (
                f"Strongly-changed pixels (delta >= {STRONG_PIXEL_DELTA}): {strongly_changed} "
                f"(change threshold {STRONG_CHANGE_MIN_PIXELS})."
            ),
        ]
        if intent.expected_change is True and mean_difference <= 0.0 and strongly_changed <= 0:
            if intent.expected_effect:
                # a DESCRIBED effect on a non-visual intent is the same
                # absent-evidence class as the visual-change tier — a pixel-identical
                # screen is not proof the described transition did not occur (typed
                # text, focus moves, and thin UI changes are invisible to pixels).
                # Degrade to uncertain so the deterministic/judge tiers decide; the
                # legacy definitive failure is kept for bare change expectations
                # (no described effect) where pixels are the whole claim.
                return _uncertain(
                    self.name,
                    (
                        "The screen is pixel-identical, which is not proof of absence "
                        "for the stated expectation"
                        + (f": {intent.expected_effect}.")
                        + " The pixel tier defers; uncertain is never success."
                    ),
                    evidence,
                    0.4,
                    changed=False,
                )
            return _definitive(
                self.name,
                "failed",
                "Expected a change but the screen is pixel-identical; the intended transition did not occur.",
                evidence,
                0.9,
                changed=False,
            )
        if intent.expected_change is False and changed:
            return _definitive(
                self.name,
                "failed",
                "Expected stability but the screen changed.",
                evidence,
                min(mean_difference / 32.0, 1.0),
                changed=True,
            )
        return _uncertain(
            self.name,
            f"Screenshot diff cannot verify {intent.kind} semantics from pixels alone.",
            evidence,
            0.2,
            changed=changed,
        )

    @staticmethod
    def _decode(encoded: str) -> Image.Image:
        return Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGB")

    @staticmethod
    def _frames(before: Observation, after: Observation) -> tuple[Image.Image, Image.Image]:
        """Pixel frames for the diff, preferring the R-5 capture-time stash.

        The stash is only trusted when BOTH observations carry one, both are RGB, and
        each frame's size agrees with its observation's recorded dimensions; anything
        else falls back to the exact legacy base64+PNG decode of both sides.
        """
        before_frame = _frame_for(before)
        after_frame = _frame_for(after)
        if (
            before_frame is not None
            and after_frame is not None
            and before_frame.size == (before.width, before.height)
            and after_frame.size == (after.width, after.height)
        ):
            return before_frame, after_frame
        return (
            ScreenshotDiffStrategy._decode(before.image_base64),
            ScreenshotDiffStrategy._decode(after.image_base64),
        )


class WindowStateStrategy:
    """Verifies active-window identity using observation fields only.

    Criteria (all stated criteria must hold): the active window title matches
    ``intent.expected_window_title`` (``contains`` or ``equals``, case-insensitive), and —
    when ``intent.require_bounds_change`` is set — the active window's bounds changed
    between ``before`` and ``after``. Degrades to ``uncertain`` when the observation
    carries no window identity or no criteria are stated.
    """

    @property
    def name(self) -> str:
        return "window_state"

    def can_verify(self, intent: VerificationIntent) -> bool:
        return intent.kind == VerificationKind.WINDOW_STATE

    def verify(self, intent: VerificationIntent, before: Observation, after: Observation) -> VerificationResult:
        checks: list[tuple[bool, bool, list[str]]] = []  # (decided, ok, evidence)
        after_title = self._active_title(after)
        before_title = self._active_title(before)

        if intent.expected_window_title:
            if after_title is None:
                checks.append(
                    (
                        False,
                        False,
                        [f"Observation carries no window title; cannot verify {intent.expected_window_title!r}."],
                    )
                )
            else:
                needle = intent.expected_window_title.casefold()
                actual = after_title.casefold()
                if intent.window_title_match == "equals":
                    matched = actual == needle
                else:
                    matched = needle in actual
                evidence = [
                    (
                        f"Active window title {after_title!r} "
                        f"{'matches' if matched else 'does not match'} expected "
                        f"{intent.expected_window_title!r} (mode={intent.window_title_match})."
                    )
                ]
                if not matched and before_title is not None:
                    evidence.append(f"Previous active window title was {before_title!r}.")
                checks.append((True, matched, evidence))

        if intent.require_bounds_change:
            before_bounds = self._active_bounds(before)
            after_bounds = self._active_bounds(after)
            if before_bounds is None or after_bounds is None:
                checks.append(
                    (False, False, ["Window bounds unavailable; cannot verify a bounds change."])
                )
            else:
                moved = tuple(before_bounds) != tuple(after_bounds)
                checks.append(
                    (
                        True,
                        moved,
                        [
                            (
                                f"Window bounds {before_bounds} -> {after_bounds} "
                                f"({'changed' if moved else 'unchanged'})."
                            )
                        ],
                    )
                )

        if not checks:
            return _uncertain(
                self.name,
                "window_state intent states no criteria; nothing to verify.",
                [],
                0.0,
                changed=False,
            )
        undecided = [check for check in checks if not check[0]]
        if undecided:
            evidence = [item for check in undecided for item in check[2]]
            return _uncertain(
                self.name,
                "Window state could not be determined from the observation.",
                evidence,
                0.2,
                changed=False,
            )
        failed = [check for check in checks if not check[1]]
        evidence = [item for check in checks for item in check[2]]
        if failed:
            return _definitive(
                self.name, "failed", "Active window state does not match the intent.", evidence,
                _IDENTITY_MATCH_CONFIDENCE, changed=False,
            )
        return _definitive(
            self.name, "verified", "Active window state matches the intent.", evidence,
            _IDENTITY_MATCH_CONFIDENCE, changed=False,
        )

    @staticmethod
    def _active_title(observation: Observation) -> str | None:
        info = observation.active_window_info
        if info is not None and info.title:
            return info.title
        return observation.active_window or None

    @staticmethod
    def _active_bounds(observation: Observation) -> tuple[int, int, int, int] | None:
        info = observation.active_window_info
        if info is not None and info.bounds is not None:
            return tuple(info.bounds)
        return None


class ProcessStateStrategy:
    """Verifies foreground process identity from the ``after`` observation.

    Criteria: ``intent.expected_process_name`` matches the active window's process name or
    executable basename (case-insensitive, ``.exe``-tolerant so ``notepad`` matches
    ``notepad.exe``), and/or ``intent.expected_pid`` equals the active window pid. Degrades
    to ``uncertain`` when the observation carries no process identity or no criteria.
    """

    @property
    def name(self) -> str:
        return "process_state"

    def can_verify(self, intent: VerificationIntent) -> bool:
        return intent.kind == VerificationKind.PROCESS_STATE

    def verify(self, intent: VerificationIntent, before: Observation, after: Observation) -> VerificationResult:
        info = after.active_window_info
        checks: list[tuple[bool, bool, list[str]]] = []

        if intent.expected_process_name:
            if info is None or (not info.process_name and not info.exe_path):
                checks.append(
                    (
                        False,
                        False,
                        [
                            (
                                f"Observation carries no process identity; cannot verify "
                                f"{intent.expected_process_name!r}."
                            )
                        ],
                    )
                )
            else:
                matched = False
                seen: list[str] = []
                if info.process_name:
                    seen.append(info.process_name)
                    matched = matched or self._name_matches(info.process_name, intent.expected_process_name)
                if info.exe_path:
                    seen.append(info.exe_path)
                    matched = matched or self._name_matches(
                        info.exe_path.replace("\\", "/").rsplit("/", 1)[-1],
                        intent.expected_process_name,
                    )
                evidence = [
                    (
                        f"Foreground process {seen} "
                        f"{'matches' if matched else 'does not match'} expected "
                        f"{intent.expected_process_name!r}."
                    )
                ]
                checks.append((True, matched, evidence))

        if intent.expected_pid is not None:
            if info is None or info.pid is None:
                checks.append(
                    (False, False, [f"Observation carries no pid; cannot verify pid {intent.expected_pid}."])
                )
            else:
                matched = info.pid == intent.expected_pid
                checks.append(
                    (
                        True,
                        matched,
                        [
                            (
                                f"Foreground pid {info.pid} {'==' if matched else '!='} expected "
                                f"{intent.expected_pid}."
                            )
                        ],
                    )
                )

        if not checks:
            return _uncertain(
                self.name,
                "process_state intent states no criteria; nothing to verify.",
                [],
                0.0,
                changed=False,
            )
        undecided = [check for check in checks if not check[0]]
        if undecided:
            evidence = [item for check in undecided for item in check[2]]
            return _uncertain(
                self.name,
                "Process state could not be determined from the observation.",
                evidence,
                0.2,
                changed=False,
            )
        evidence = [item for check in checks for item in check[2]]
        if any(not check[1] for check in checks):
            return _definitive(
                self.name, "failed", "Foreground process state does not match the intent.", evidence,
                _IDENTITY_MATCH_CONFIDENCE, changed=False,
            )
        return _definitive(
            self.name, "verified", "Foreground process state matches the intent.", evidence,
            _IDENTITY_MATCH_CONFIDENCE, changed=False,
        )

    @staticmethod
    def _name_matches(candidate: str, expected: str) -> bool:
        def normalize(value: str) -> str:
            return value.strip().casefold().removesuffix(".exe")

        return normalize(candidate) == normalize(expected)


class DeterministicPredicateStrategy:
    """Runs a caller-supplied deterministic predicate over the before/after observations.

    The predicate returns ``True`` (verified), ``False`` (failed), or ``None`` (cannot
    determine -> uncertain). Exceptions raised by the predicate are caught and degrade to
    ``uncertain`` — a crashing predicate can never masquerade as success.
    """

    @property
    def name(self) -> str:
        return "deterministic_predicate"

    def can_verify(self, intent: VerificationIntent) -> bool:
        return intent.kind == VerificationKind.PREDICATE

    def verify(self, intent: VerificationIntent, before: Observation, after: Observation) -> VerificationResult:
        label = intent.predicate_name or "anonymous_predicate"
        if intent.predicate is None:
            return _uncertain(
                self.name,
                "No predicate callable supplied; the predicate cannot be evaluated.",
                [f"predicate_name={label!r}"],
                0.0,
                changed=False,
            )
        try:
            result = intent.predicate(before, after)
        except Exception as exc:  # noqa: BLE001 - never let predicate exceptions escape as success
            return _uncertain(
                self.name,
                f"Predicate {label!r} raised an exception; treated as cannot-determine.",
                [f"{type(exc).__name__}: {exc}"],
                0.0,
                changed=False,
            )
        evidence = [f"Predicate {label!r} evaluated to {result!r}."]
        if result is True:
            return _definitive(self.name, "verified", f"Predicate {label!r} holds.", evidence, 1.0, changed=False)
        if result is False:
            return _definitive(
                self.name, "failed", f"Predicate {label!r} does not hold.", evidence, 1.0, changed=False
            )
        return _uncertain(
            self.name,
            f"Predicate {label!r} returned {result!r}; cannot determine the outcome.",
            evidence,
            0.3,
            changed=False,
        )


class TextPredicateStrategy:
    """Verifies that expected text appears in the ``after`` observation's OCR regions.

    T8 anomaly-B2 demotion: OCR-derived text regions proved UNRELIABLE on the
    measurement machine (Windows Server 2022 returns garbage regions even on plain
    text), so this strategy is a LAST RESORT, disabled by default and gated behind the
    ``CORTEX_OCR_TEXT_VERIFICATION=1`` config flag. The default expected-text ladder is
    :class:`UiControlTextStrategy` (window title + ``ui_elements`` control text —
    deterministic window/UIA signals). When enabled, the strategy keeps its exact
    semantics: only OCR data decides, and without OCR data it degrades to ``uncertain``
    — never to success.
    """

    @property
    def name(self) -> str:
        return "text_predicate"

    def can_verify(self, intent: VerificationIntent) -> bool:
        return intent.kind == VerificationKind.EXPECTED_TEXT and ocr_text_verification_enabled()

    def verify(self, intent: VerificationIntent, before: Observation, after: Observation) -> VerificationResult:
        expected = intent.expected_text
        if not expected:
            return _uncertain(self.name, "No expected text stated; nothing to verify.", [], 0.0, changed=False)
        regions = after.ocr_text
        if regions is None:
            return _uncertain(
                self.name,
                "OCR data is unavailable for this observation; text presence cannot be determined.",
                [f"expected_text={expected!r}", "ocr_text=None"],
                0.0,
                changed=False,
            )
        if not regions:
            return _definitive(
                self.name,
                "failed",
                "OCR produced no text regions; expected text is absent.",
                [f"expected_text={expected!r}", "ocr_text=[]"],
                0.7,
                changed=False,
            )
        needle = expected.casefold()
        matches = [region for region in regions if needle in region.text.casefold()]
        sample = [region.text for region in regions[:10]]
        if matches:
            region = matches[0]
            confidence = region.confidence if region.confidence is not None else 0.85
            return _definitive(
                self.name,
                "verified",
                f"Expected text {expected!r} found in OCR output.",
                [
                    (
                        f"Matched OCR region {region.text!r} at (x={region.x}, y={region.y}, "
                        f"w={region.width}, h={region.height})."
                    )
                ],
                confidence,
                changed=False,
            )
        return _definitive(
            self.name,
            "failed",
            f"Expected text {expected!r} not found in OCR output.",
            [f"OCR regions seen: {sample}"],
            0.85,
            changed=False,
        )


#: T8 anomaly-B2 config flag: OCR text-predicate participation (last-resort tier).
#: Default OFF — the OCR path returned garbage on the measurement machine and
#: false-failed every type action; window-title + ui-control text evidence is the
#: default expected-text ladder instead.
OCR_TEXT_VERIFICATION_ENV = "CORTEX_OCR_TEXT_VERIFICATION"


def ocr_text_verification_enabled() -> bool:
    """Whether the OCR text-predicate tier may run (env-flag gated; default False)."""
    import os

    raw = os.getenv(OCR_TEXT_VERIFICATION_ENV)
    if raw is None or not raw.strip():
        return False
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


class UiControlTextStrategy:
    """Verifies expected text through deterministic window/UI-control evidence (B2 fix).

    The default expected-text ladder, replacing OCR as the primary signal:

    1. **Window title**: the ``after`` observation's active window title containing the
       expected text is definitive evidence (e.g. a document retitled after a save).
    2. **UI control text**: ``after.ui_elements`` (the backend's focused-control +
       children read — Win32 ``WM_GETTEXT`` or UIA, whichever the backend supplies)
       carrying the expected text in a control's ``name`` or ``value`` (an edit field's
       content) is definitive evidence.

    When neither channel shows the text the strategy degrades to ``uncertain`` — NEVER
    to ``failed``: typed content is often invisible to Win32/UIA (spreadsheet cells,
    canvases), and on the B2 machine the OCR fallback is garbage, so an absent match
    must not become a false failure. Callers (the controller) then route an
    expected-text ``uncertain`` to the reliable pixel-diff tier.
    """

    @property
    def name(self) -> str:
        return "ui_control_text"

    def can_verify(self, intent: VerificationIntent) -> bool:
        return intent.kind == VerificationKind.EXPECTED_TEXT

    def verify(self, intent: VerificationIntent, before: Observation, after: Observation) -> VerificationResult:
        expected = intent.expected_text
        if not expected:
            return _uncertain(self.name, "No expected text stated; nothing to verify.", [], 0.0, changed=False)
        needle = expected.casefold()

        info = after.active_window_info
        title = (info.title if info is not None else None) or after.active_window
        if title and needle in title.casefold():
            return _definitive(
                self.name,
                "verified",
                f"Expected text {expected!r} found in the active window title.",
                [f"Active window title {title!r} contains {expected!r}."],
                _IDENTITY_MATCH_CONFIDENCE,
                changed=False,
            )

        elements = after.ui_elements or []
        for index, element in enumerate(elements[:50]):
            if not isinstance(element, dict):
                continue
            for field_name in ("value", "name"):
                text = element.get(field_name)
                if isinstance(text, str) and needle in text.casefold():
                    control = element.get("control_type") or element.get("type") or "control"
                    return _definitive(
                        self.name,
                        "verified",
                        f"Expected text {expected!r} found in UI control text.",
                        [
                            (
                                f"Control #{index} ({control}) {field_name} contains "
                                f"{expected!r}."
                            )
                        ],
                        0.85,
                        changed=False,
                    )

        return _uncertain(
            self.name,
            (
                "Typed text is not visible to window-title or UI-control evidence; "
                "text presence cannot be determined (never treated as absence)."
            ),
            [f"expected_text={expected!r}", f"title={title!r}", f"ui_elements={len(elements)}"],
            0.2,
            changed=False,
        )


# =====================================================================================
# ORVEX-2026-105 — the named-identity binding for the focus/window signals.
# =====================================================================================
#
# THE PRINCIPLE, in one sentence and binding on :class:`FocusChangeStrategy` arms
# (a)/(b): a focus/window signal may support ``verified`` ONLY when the identity
# observed AFTER the action corresponds to a specific thing the stated effect NAMED —
# never merely because the identity changed, and never merely because it changed
# within one process.
#
# Before this rule the tier returned ``verified`` on any identity CHANGE and never
# opened ``intent.expected_effect``. Both identity readers read the FOREGROUND window
# (the UIA and the Win32 readers in ``backend.py``) and ``observe()`` reads the
# foreground window, so ANY click that activated a window moved both signals: clicking
# a static Tk ``Label`` while claiming *"The Edit Colors dialog opens with a new color
# swatch"* came back ``verified`` and the dialog never opened. This is the exact
# counterpart of the digest-arm doctrine, which already refuses to let a pixel
# magnitude claim a described effect — arm (c) abstains, and now arms (a)/(b) must
# NAME the thing as well.

#: ORVEX-2026-105 R-3a — the CLOSED stop-list of structural/generic words. A word on
#: this list can never be a corroborating token, because naming "dialog" or "field"
#: says which KIND of thing to look for and nothing about which one; an effect whose
#: every content word is structural therefore yields ``E = {}`` and can never verify.
#:
#: GROWTH RULE (binding): a word is added here ONLY together with a failing reproduction
#: test that shows that word producing a false ``verified`` on a real observation pair,
#: added in the SAME change. No silent edits, no "while we are here" additions, no
#: additions driven by intuition alone.
#:
#: Considered and DELIBERATELY ABSENT: ``toolbar`` and ``widget``. Neither ships with a
#: failing reproduction, so the growth rule forbids admitting them. ``widget`` is
#: additionally a NAMED residual of this fix — an effect naming a generic noun that a
#: real window happens to be called ("the widget appears" vs a new "Widget Tray") still
#: corroborates on it — which is accepted and made AUDITABLE by the R-9 evidence line
#: naming the carrying token on every ``verified``, rather than papered over by a
#: stop-word that no test earned.
IDENTITY_STOP_TOKENS: frozenset[str] = frozenset(
    {
        # articles, copulas, prepositions, connectives, deictics, aspect adverbs
        "a", "an", "and", "the", "is", "are", "was", "were", "be", "been", "being",
        "of", "to", "in", "on", "at", "by", "for", "with", "from", "into", "onto",
        "it", "its", "this", "that", "these", "those", "now", "then", "when", "while",
        "after", "before", "up", "down", "over", "under", "out", "off",
        # appearance verbs and their participles
        "new", "open", "opens", "opened", "close", "closes", "closed",
        "show", "shows", "shown",
        "appear", "appears", "appeared", "disappear", "disappears",
        # window/UI container structure — the residual-risk class the owner named
        "dialog", "window", "popup", "modal", "form", "panel", "box", "pane",
        "frame", "sheet", "banner", "toast",
        "control", "element", "field", "input", "button", "menu", "item", "entry",
        "row", "column", "cell",
        "tab", "page", "screen", "view", "area", "region", "section", "bar",
        # focus / activation / visibility
        "focus", "focused", "focusing", "activate", "activated", "bring", "front",
        "visible",
        # the interaction itself
        "click", "clicks", "clicked", "press", "pressed",
        "select", "selected", "choose", "chosen",
        # the application / the actor
        "app", "application", "program", "user", "please",
        # modality
        "should", "must", "will", "would", "can", "may", "does", "do", "did",
        "not", "no", "yes", "ok", "okay", "done",
        # outcome vocabulary — an effect must name a THING, not a verdict
        "complete", "completed", "success", "successful", "result", "results",
        "state", "changes", "changed", "change",
        "updates", "updated", "update",
        "sets", "set", "becomes", "become", "remains", "remain",
        "inserts", "insert", "types", "type", "enters", "enter",
    }
)

#: ORVEX-2026-105 R-2.4 — a candidate word shorter than this is dropped before it can
#: corroborate.
IDENTITY_MIN_TOKEN_CHARS = 3

#: ORVEX-2026-105 R-1 — identity is read from at most this many ``ui_elements``
#: entries: the same bound :class:`UiControlTextStrategy` scans, so the two tiers
#: never disagree about which elements exist.
IDENTITY_ELEMENT_SCAN_LIMIT = 50

#: ORVEX-2026-105 R-4/R-9 — the three field classes an identity token can belong to.
#: The order is most-specific-first on purpose: :func:`_novel_tokens` records the
#: FIRST class that made a token novel, and "focused element" says more than the
#: population it belongs to.
_IDENTITY_CLASS_FOCUSED = "focused element"
_IDENTITY_CLASS_WINDOW = "active window"
_IDENTITY_CLASS_POPULATION = "ui_elements population"

#: ORVEX-2026-105 R-2.5 — the minimum remainder kept when a trailing plural ``s`` is
#: stripped (``colors`` -> ``color``), and when a trailing ``es``/``ed`` is stripped
#: (``deleted`` -> ``delet``). ``delete`` ends in ``te``: neither rule fires, which is
#: what keeps the Confirm-Delete? re-pin honest (see
#: ``tests/test_d1_normalizer_keeps_delet_and_delete_distinct.py``).
_STEM_PLURAL_MIN_REMAINDER = 3
_STEM_ED_MIN_REMAINDER = 4

#: ORVEX-2026-105 R-9 — the shortest shared prefix at which an observed token counts
#: as a diagnosable near-miss of an effect token rather than an unrelated word.
_NEAR_MISS_MIN_PREFIX = 3


def stem(token: str) -> str:
    """Minimal, deterministic suffix stripping (ORVEX-2026-105 R-2.5) — deliberately
    NOT a stemmer.

    Two rules, applied in this order and in no other: a trailing ``"s"`` is dropped when
    the remainder keeps :data:`_STEM_PLURAL_MIN_REMAINDER` characters, then a trailing
    ``"es"``/``"ed"`` is dropped when the remainder keeps
    :data:`_STEM_ED_MIN_REMAINDER`. A token that does not end in the suffix is returned
    unchanged — which is exactly what keeps ``delete`` (``te``) and ``deleted`` (``ed``)
    DISTINCT.

    No general-purpose stemmer, no POS tagger, no new dependency: a library stemmer
    would make the corroboration rule unauditable, and the whole point of the binding
    is that a reader can check the rule by reading it. The consequence of the fixed
    order is worth stating too — ``boxes`` takes the plural rule and becomes ``boxe``,
    never ``box``."""
    if token.endswith("s") and len(token) - 1 >= _STEM_PLURAL_MIN_REMAINDER:
        return token[:-1]
    for suffix in ("es", "ed"):
        if token.endswith(suffix) and len(token) - len(suffix) >= _STEM_ED_MIN_REMAINDER:
            return token[: -len(suffix)]
    return token


def _normalized_words(*values: Any) -> frozenset[str]:
    """``casefold`` -> non-word characters to spaces -> split (R-2.1/R-2.2/R-2.3).

    A "word character" is ``str.isalnum()`` PLUS the Unicode combining marks
    (general category ``M*`` — Arabic tashkeel, Devanagari matras, …): a mark
    modifies the letter it attaches to and must never split its word (F3,
    VAL-WP3 — before this, a vocalized Arabic word was shattered into fragments,
    one of which could become a spurious token). Non-``str``/empty values (an
    element's ``None`` ``name``, a missing ``value``) contribute nothing."""
    words: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        for chunk in value.casefold().split():
            words.update(
                "".join(
                    char
                    if char.isalnum() or unicodedata.category(char)[0] == "M"
                    else " "
                    for char in chunk
                ).split()
            )
    return frozenset(words)


def _content_tokens(*values: Any) -> frozenset[str]:
    """The R-2 content tokens of ``values`` — ONE rule for BOTH sides of the comparison.

    ``casefold`` -> combining-mark-preserving separator split (R-2.1-R-2.3), then drop
    words shorter than :data:`IDENTITY_MIN_TOKEN_CHARS`, purely numeric words, and every
    :data:`IDENTITY_STOP_TOKENS` word on BOTH its raw and its STEMMED form (R-2.4 + F2,
    VAL-WP3: the stem runs FIRST, so a plural of a structural word — ``dialogs``,
    ``windows``, ``panels``, ``fields`` — stems onto its stop-listed singular and can
    never corroborate, exactly like the singular), then keep the stem of what survives
    (R-2.5).

    The effect prose and the observed identity are built with THIS function and no
    other. A comparison whose two sides were normalised by different rules would be
    exactly the silent mismatch this binding exists to close, so there is deliberately
    no second, looser normaliser in the module — and applying the drops on the identity
    side as well keeps the reported token sets readable (a window titled
    ``"orvex-103-d1"`` contributes ``orvex``, not ``103``/``d1``), while being inert for
    the predicate, since a stopped or numeric token can never appear in ``E``."""
    tokens: set[str] = set()
    for word in _normalized_words(*values):
        if len(word) < IDENTITY_MIN_TOKEN_CHARS or word.isnumeric():
            continue
        stemmed = stem(word)
        if word in IDENTITY_STOP_TOKENS or stemmed in IDENTITY_STOP_TOKENS:
            continue
        if len(stemmed) >= IDENTITY_MIN_TOKEN_CHARS:
            tokens.add(stemmed)
    return frozenset(tokens)


def _effect_tokens(expectation: str | None) -> frozenset[str]:
    """The stated effect's content tokens ``E`` (R-2) — what the identity must name.

    An effect built entirely from generic/structural words yields ``E = {}``, which can
    never corroborate anything (R-6.2) — the honest verdict for an effect that names no
    thing at all, such as ``"the dialog commits"``."""
    return _content_tokens(expectation)


def _identity_token_classes(observation: Observation) -> dict[str, frozenset[str]]:
    """The observed identity tokens of ``observation``, PER FIELD CLASS (R-1/R-4).

    Three classes, because the novelty contrast has to be evaluated per class and
    UNITED (R-8): a single global before-contrast would demote the legitimate Paint
    case, where the BEFORE ``ui_elements`` population already lists the hex field as an
    UNFOCUSED child and a global contrast would call ``hex`` a constant.

    The R-1 field selection is exact. Window side: ``active_window_info.title``,
    ``active_window_info.window_class`` and the ``active_window`` fallback. Element side:
    ``name`` / ``automation_id`` / ``control_type`` / ``value`` of the FOCUS-MARKED
    element, and the same four fields over the first :data:`IDENTITY_ELEMENT_SCAN_LIMIT`
    ``ui_elements``. ``ocr_text`` and ``image_base64`` are STRUCTURALLY EXCLUDED — the
    OCR tier is disabled by default because it returns garbage on the reference machine,
    and a corroboration rule leaning on it would inherit that unreliability.
    ``process_name`` / ``exe_path`` / ``pid`` / ``hwnd`` are EVIDENCE-ONLY (R-3): a
    process name identifies the APPLICATION, not an element indicating the effect
    occurred, so it can never be a corroborating token.

    ONE pass over ``ui_elements`` produces both element-side classes — the focused
    element is by construction a member of the population, so deriving it in the same
    loop is what keeps this to a single scan (a second pass would be pure duplicated
    copying of data already in hand)."""
    info = observation.active_window_info
    window = _content_tokens(
        info.title if info is not None else None,
        info.window_class if info is not None else None,
        observation.active_window,
    )
    population: set[str] = set()
    focused: set[str] | None = None
    for element in (observation.ui_elements or [])[:IDENTITY_ELEMENT_SCAN_LIMIT]:
        if not isinstance(element, dict):
            continue
        tokens = _content_tokens(
            element.get("name"),
            element.get("automation_id"),
            element.get("control_type"),
            element.get("value"),
        )
        population |= tokens
        # The FIRST focus-marked element wins, exactly as ``_focus_marked_element``
        # selects it; a marked element with blank fields still claims the class (and so
        # contributes an empty set), rather than letting a later sibling take its place.
        if focused is None and (
            element.get("focused") or element.get("is_focused") or element.get("has_focus")
        ):
            focused = tokens
    return {
        _IDENTITY_CLASS_FOCUSED: frozenset(focused if focused is not None else ()),
        _IDENTITY_CLASS_WINDOW: window,
        _IDENTITY_CLASS_POPULATION: frozenset(population),
    }


def _novel_tokens(
    before: dict[str, frozenset[str]], after: dict[str, frozenset[str]]
) -> dict[str, str]:
    """``Novel`` as ``{token: the class that made it novel}`` (R-4/R-8).

    ``Novel = union over c of (O_after[c] \\ O_before[c])``. The AFTER side supplies the
    PRESENCE and the BEFORE side supplies the ABSENCE, so a token that was already there
    is a constant, not evidence of a transition. The dict maps each novel token to the
    first class that carried it (the most-specific-first order of
    :func:`_identity_token_classes`), because R-9 requires a ``verified`` to name WHICH
    class made it novel. A token novel in ANY ONE class corroborates (R-8) — that
    per-class union is load-bearing, not a detail."""
    novel: dict[str, str] = {}
    for class_name, tokens in after.items():
        for token in tokens - before.get(class_name, frozenset()):
            novel.setdefault(token, class_name)
    return novel


def _shared_prefix_length(left: str, right: str) -> int:
    """Character-wise common-prefix length (diagnostics only, never a verdict input)."""
    count = 0
    for first, second in zip(left, right):
        if first != second:
            break
        count += 1
    return count


def _closest_non_match(
    effect_tokens: frozenset[str],
    after: dict[str, frozenset[str]],
    novel: dict[str, str],
) -> str | None:
    """The single closest NON-matching candidate, or ``None`` (R-9).

    Two ranked near-misses, because an abstention that cannot say WHY it abstained is
    the failure mode R-9 exists to remove:

    1. an effect token that IS in the AFTER identity but was already in the BEFORE one —
       novelty rejected it, and saying so separates "the effect named the wrong thing"
       from "the effect named a thing that was already true";
    2. otherwise the novel observed token sharing the longest common prefix with some
       unmatched effect token — a stem/synonym miss (``swatch`` vs ``swatches``,
       ``color`` vs ``colour``), which is the other way this rule fails closed."""
    observed: set[str] = set()
    for tokens in after.values():
        observed |= tokens
    stale = sorted((effect_tokens & observed) - set(novel))
    if stale:
        carried_by = next(
            (name for name, tokens in after.items() if stale[0] in tokens), _IDENTITY_CLASS_WINDOW
        )
        return (
            f"closest non-matching candidate: effect token {stale[0]!r} IS present in the "
            f"AFTER {carried_by} identity but was ALREADY present in the BEFORE one — a "
            f"constant, not evidence of a transition"
        )
    best: tuple[int, str, str, str] | None = None
    for candidate in sorted(effect_tokens):
        for token, class_name in novel.items():
            shared = _shared_prefix_length(candidate, token)
            if shared >= _NEAR_MISS_MIN_PREFIX and (best is None or shared > best[0]):
                best = (shared, candidate, token, class_name)
    if best is None:
        return None
    return (
        f"closest non-matching candidate: effect token {best[1]!r} vs observed identity "
        f"token {best[2]!r} (new in the AFTER {best[3]} identity) — no shared effect token"
    )


class FocusChangeStrategy:
    """Deterministic focus/window/digest change verification (REM-B, H2c).

    A click whose stated ``expected_effect`` describes a FOCUS-type transition
    ("Hex input focused", "Edit colors dialog opens") is often INVISIBLE to the
    pixel-diff tier: focusing a text field or opening a dialog moves either no
    measurable pixels (mean 0.013, strongly-changed 0) or opens after the capture.
    The logged Paint session false-failed three such clicks, killing the task at
    the first drawing step.

    Cheap deterministic signals, in order, using OBSERVATION FIELDS ONLY (never
    pixels, never a model):

    (a) UIA focused-element change: the focused control (``ui_elements`` entries
        carrying ``focused`` truthy, falling back to the first element) changed
        between before/after on ``name`` / ``automation_id`` / ``control_type``
        (REM-C F2: the fallback is only trusted when at least ONE side carries a
        real focus marker — with no focus claim anywhere, a UIA re-enumeration
        order change of unfocused controls is NOT a focus change and the signal
        abstains);
    (b) active-window identity change: the active window title OR process changed
        between the two observations (dialog-open signature);
    (c) observation digest change: the screenshots are not pixel-identical
        (REM-C F1: evidence only — F1 fix 1.1 (E-03): even a CORROBORATED digest
        change (at least ``STRONG_CHANGE_MIN_PIXELS`` strongly-changed pixels) is a
        real REDRAW, not the described effect, so this arm abstains
        (``uncertain``, ``changed=True``) in both directions and never asserts the
        expectation; an uncorroborated digest change — a caret blink, a clock tick,
        another app's toast — stays ``changed=False`` so the screenshot-diff tier
        decides, preserving that tier's deliberate flicker exclusion).

    Arms (a)/(b) showing a change — corroborated or not by the named-identity
    binding below — decide the verdict. Arm (c) NEVER verifies (it abstains in both
    directions; see above). NO signal showing change leaves the verdict to the
    next tiers — for a flagged intent the screenshot-diff strategy degrades a
    sub-threshold diff to ``uncertain`` (never ``failed`` from absent
    evidence; the doctrine extends that degrade to ANY stated effect directly in
    ScreenshotDiffStrategy, WITHOUT widening this tier's scope), and it never
    upgrades a pixel-proven non-change.

    **ORVEX-2026-105 — arms (a)/(b) are no longer DEFINITIVE on their own.**
    A changed identity is evidence that SOMETHING moved, and both identity readers
    read the FOREGROUND window, so every click that merely activates a window moved
    both signals: a static Tk ``Label`` click claimed *"The Edit Colors dialog opens
    with a new color swatch"* and came back ``verified`` while nothing opened. The
    binding is now the named-identity one (see the ORVEX-2026-105 block above this
    class): a focus/window signal supports ``verified`` only when a CORROBORATION
    token holds, ``E ∩ Novel ≠ ∅``, where ``E`` is the effect's content tokens and
    ``Novel`` is the per-class before/after novelty of the observed identity.
    Concretely:

    - an identity change whose AFTER identity names nothing the effect named is
      ``uncertain`` — the change is still reported (``changed=True``, and the signal
      line stays in the evidence), it simply no longer CLAIMS the effect, and the
      effect string is kept OUT of the note exactly as arm (c) keeps it out;
    - a corroborated one is ``verified`` and its evidence names the matched token, the
      field class that made it novel, and that it was novel (R-9), so a wrong
      corroboration is auditable from the response itself rather than hidden inside a
      success;
    - "same process" is a PRECONDITION and no more: it is recorded as a fact and can
      never, alone, raise ``verified`` (R-5);
    - both degeneracies resolve to ``uncertain``: no usable identity at all (R-6.1,
      fail-closed) and an effect naming nothing extractable (R-6.2, e.g. ``"the panel
      expands"``);
    - the bias is closed on ``verified`` and open on ``uncertain`` (R-7): a false
      ``uncertain`` costs one re-drive, a false ``verified`` costs a silent wrong
      success that propagates into the plan aggregate as ``completed``.

    Both arms are always derived (two bounded in-memory reads of already-captured
    observations — no capture, no I/O, no input, and only ever reached when
    :meth:`can_verify` is true, i.e. a CLICK/DOUBLE_CLICK carrying a stated effect).
    That is what lets a corroboration be accepted from EITHER arm's evidence rather
    than being missed because the other arm happened to fire first.

    Scope gate: only intents flagged ``{FOCUS_CHANGE_INTENT_FLAG}`` in
    ``intent.metadata`` (set by the controller's ``_build_intent`` for CLICK /
    DOUBLE_CLICK actions carrying a stated expected effect — deliberately still
    Scope note: these transition signals must not "verify" a stated effect on
    other action kinds from window motion alone). Every other visual-change
    intent — drag, scroll, type, keypress, unstated expectations — keeps its
    exact pre-REM-B tier semantics.
    """

    @property
    def name(self) -> str:
        return "focus_change"

    def can_verify(self, intent: VerificationIntent) -> bool:
        return (
            intent.kind == VerificationKind.VISUAL_CHANGE
            and bool(intent.metadata.get(FOCUS_CHANGE_INTENT_FLAG))
            and intent.expected_change is True
            and bool(intent.expected_effect)
        )

    def verify(self, intent: VerificationIntent, before: Observation, after: Observation) -> VerificationResult:
        expected = intent.expected_effect or ""
        # (a) UIA focused-element change, and (b) active-window identity change. Both
        # are derived before the verdict: the ORVEX-2026-105 predicate is defined over
        # the UNION of the identity field classes, not per arm, so gating on (a) alone
        # would miss a corroboration the window side carried. Both reads are bounded
        # in-memory walks of observations already in hand — no capture, no I/O, no
        # input.
        focus_signal = self._focused_element_change(before, after)
        window_signal = self._active_window_change(before, after)
        if focus_signal is not None or window_signal is not None:
            return self._verify_identity_change(before, after, expected, focus_signal, window_signal)
        # (c) Observation digest change — REM-C (V-2 F1): the base64 diff is EVIDENCE,
        # not proof: a 1-pixel caret blink also changes the digest, and the pixel tier
        # deliberately distrusts exactly that flicker class (STRONG_CHANGE_MIN_PIXELS
        # exists so "ambiguous flicker (caret, clock tick) [is] never proof of a
        # transition"). The digest change may VERIFY only when corroborated by real
        # changed-pixel magnitude at the floor the pixel tier itself trusts; otherwise
        # it defers to the pixel tier (uncertain, never a free verified).
        if (
            before.image_base64
            and after.image_base64
            and FocusChangeStrategy._payload_text(before) != FocusChangeStrategy._payload_text(after)
        ):
            magnitude = self._digest_change_magnitude(before, after)
            threshold = max(intent.diff_threshold, 0.0)
            # F3 fix 5.5: ``_digest_change_magnitude`` returns a 0..1 normalised mean
            # while ``threshold`` is the pixel tier's 0..255-scale value, so the note
            # used to print ``mean 0.000383 >= 1`` — a comparison that cannot hold.
            # The notes convert the threshold onto the magnitude's own scale and name
            # ONLY the disjunct that actually fired. SCOPE: note strings only —
            # ``_digest_change_magnitude`` still returns the 0..1 form and the branch
            # condition is unchanged.
            mean_255 = magnitude[0] * 255.0 if magnitude is not None else 0.0
            mean_arm_fired = magnitude is not None and magnitude[0] >= threshold
            strong_arm_fired = magnitude is not None and magnitude[1] >= STRONG_CHANGE_MIN_PIXELS
            if mean_arm_fired or strong_arm_fired:
                # F1 fix 1.1 (E-03): corroboration upgrades a bare digest change to a
                # REAL change, not to the DESCRIBED effect. This signal compares
                # payload text and pixel magnitude and nothing else, so it abstains
                # (``uncertain``, changed=True) and the note names what was OBSERVED —
                # it never asserts the expectation. Signals (a)/(b) keep verifying:
                # a changed focused ELEMENT or a changed window IDENTITY is evidence
                # about the substrate the effect lives on, not about pixels.
                if mean_arm_fired and strong_arm_fired:
                    fired = (
                        f"mean {mean_255:.6g}/255 >= threshold {threshold:.6g}/255 "
                        f"and strongly-changed pixels {magnitude[1]} >= "
                        f"{STRONG_CHANGE_MIN_PIXELS}"
                    )
                elif mean_arm_fired:
                    fired = f"mean {mean_255:.6g}/255 >= threshold {threshold:.6g}/255"
                else:
                    fired = (
                        f"strongly-changed pixels {magnitude[1]} >= "
                        f"{STRONG_CHANGE_MIN_PIXELS}"
                    )
                return _uncertain(
                    self.name,
                    (
                        "The observation digest changed and the pixel change is "
                        f"corroborated ({fired}), which is a real redraw but NOT "
                        "proof of the stated expectation — pixels cannot be compared "
                        "against a described effect. Uncertain is never success."
                    ),
                    [
                        "observation digest changed between before and after",
                        (
                            f"corroborated pixel magnitude: mean {magnitude[0]:.6f} "
                            f"(= {mean_255:.6g}/255), strongly-changed pixels "
                            f"{magnitude[1]} (floor {STRONG_CHANGE_MIN_PIXELS})"
                        ),
                        f"expected_effect={expected!r}",
                    ],
                    0.4,
                    changed=True,
                )
            # Uncorroborated digest change: ambiguous flicker (caret blink, clock
            # tick, foreign toast) — carry the evidence, defer the verdict. The
            # strategy still never emits ``failed``; the pixel tier decides.
            #
            # F3 fix 5.5: this note names only the comparisons that are actually
            # true, with the mean reported on the threshold's own 0..255 scale.
            if magnitude is not None:
                arms = []
                if magnitude[1] < STRONG_CHANGE_MIN_PIXELS:
                    arms.append(f"strongly-changed {magnitude[1]} < {STRONG_CHANGE_MIN_PIXELS}")
                if magnitude[0] < threshold:
                    arms.append(f"mean {mean_255:.6g}/255 < threshold {threshold:.6g}/255")
                uncorroborated_note = (
                    "Observation digest changed but the pixel change is BELOW the "
                    f"corroboration floor ({' and '.join(arms)}); ambiguous flicker is "
                    "never proof of a transition — deferring to the pixel-diff tier."
                )
            else:
                uncorroborated_note = (
                    "Observation digest changed but the pixel magnitude could not be "
                    "computed for corroboration; deferring to the pixel-diff tier."
                )
            return _uncertain(
                self.name,
                uncorroborated_note,
                [
                    "observation digest changed between before and after (sub-threshold)",
                    f"expected_effect={expected!r}",
                ],
                0.3,
                changed=False,
            )
        # No deterministic signal: never failed, never a free success — the next
        # tiers (screenshot_diff, judge) keep their exact existing semantics.
        return _uncertain(
            self.name,
            "No deterministic focus/window/digest change signal; deferring to the pixel-diff tier.",
            [f"expected_effect={expected!r}"],
            0.2,
            changed=False,
        )

    # --- the ORVEX-2026-105 named-identity binding for arms (a)/(b) --------------------------

    def _verify_identity_change(
        self,
        before: Observation,
        after: Observation,
        expected: str,
        focus_signal: str | None,
        window_signal: str | None,
    ) -> VerificationResult:
        """Arms (a)/(b) under the named-identity binding: a change, or a CORROBORATED
        change.

        The predicate is ``E ∩ Novel ≠ ∅`` where ``E`` is the stated effect's content
        tokens and ``Novel`` is the union, over the identity field classes, of the tokens
        present in the AFTER observation and absent from the matching BEFORE one. Both
        sides of that contrast are read here, once each, in memory.

        The note and the evidence never assert the expectation on the uncorroborated
        path — the module's standing rule is that a note names what was OBSERVED, and
        that rule is now applied to arms (a)/(b) exactly as arm (c) applies it. On the
        corroborated path the effect is quoted, because at that point the identity
        really does name the thing the effect described."""
        fired: list[str] = []
        if focus_signal is not None:
            fired.append(f"focused UI element changed ({focus_signal})")
        if window_signal is not None:
            fired.append(f"active window identity changed ({window_signal})")

        effect_tokens = _effect_tokens(expected)
        after_classes = _identity_token_classes(after)
        before_classes = _identity_token_classes(before)
        novel = _novel_tokens(before_classes, after_classes)

        # The observed signal lines come first and are kept verbatim in BOTH verdicts:
        # what changed is a fact the host is entitled to, whether or not it corroborates.
        evidence: list[str] = [
            line for line in (focus_signal, window_signal) if line is not None
        ]
        evidence.append(f"expected_effect={expected!r}")
        evidence.append(self._same_process_evidence(before, after))

        matched = sorted(effect_tokens & set(novel))
        if matched:
            token = matched[0]
            class_name = novel[token]
            # R-9: every verified names itself — token, class, novelty. A bad match is
            # then inspectable from the response instead of hidden inside a success.
            # With more than one match the line says so rather than letting the reader
            # assume the named token is the only thing the verdict rested on.
            #
            # F5 (VAL-WP3): the corroboration line rides FIRST in the evidence. Bounded
            # consumers cap the per-item evidence window (queue entries keep the first
            # three lines), and this is the one line the R-9 auditability promise
            # cannot afford to lose — every other line (the signal facts, the effect
            # echo, the same-process precondition) is retained after it, in the same
            # relative order as before.
            also = (
                f" (also matched: {', '.join(repr(item) for item in matched[1:])})"
                if len(matched) > 1
                else ""
            )
            evidence.insert(
                0,
                f"corroborated by effect token {token!r} present in the AFTER "
                f"{class_name} identity and ABSENT from the BEFORE {class_name} identity "
                f"(novel){also}; searched effect tokens {sorted(effect_tokens)}",
            )
            return _definitive(
                self.name,
                "verified",
                (
                    f"Stated effect corroborated by the observed identity: {expected}. "
                    f"Deterministic signal: {'; '.join(fired)}. "
                    f"Corroborated by novel token {token!r} in the AFTER {class_name} "
                    f"identity."
                ),
                evidence,
                _DETERMINISTIC_CHANGE_CONFIDENCE,
                changed=True,
            )

        # Uncorroborated: the transition really happened, but the identity it produced
        # names nothing the effect named, so this tier abstains rather than assert the
        # effect. R-6.1 is a strict subset (no identity at all -> no novel tokens at all),
        # so the one abstention below covers both.
        evidence.append(
            f"corroboration searched effect tokens {sorted(effect_tokens)} against the "
            f"novel AFTER identity tokens {sorted(novel)} (per class: "
            + ", ".join(
                f"{name}: {sorted(after_classes[name] - before_classes.get(name, frozenset()))}"
                for name in after_classes
            )
            + ")"
        )
        if effect_tokens:
            nearest = _closest_non_match(effect_tokens, after_classes, novel)
            evidence.append(
                nearest
                or "no near-miss candidate: no effect token is present in the AFTER identity"
            )
        else:
            # R-6.2: the effect names nothing extractable after stop-listing/stemming, so
            # there is no token to search for and no candidate to name.
            evidence.append(
                "the stated effect yields no content token after stop-listing and "
                "stemming, so no identity can corroborate it"
            )
        return _uncertain(
            self.name,
            (
                "A focus/window identity change was observed, but the identity present "
                "AFTER the action names nothing the stated effect named; an identity "
                "change — including one within a single process — is not evidence that "
                "the described transition occurred. Uncertain is never success."
            ),
            evidence,
            0.2,
            changed=True,
        )

    @staticmethod
    def _same_process_evidence(before: Observation, after: Observation) -> str:
        """The same-process PRECONDITION, recorded as a fact and never as a verdict (R-5).

        It was once a sufficient condition on its own, which is why it is spelled out
        here rather than left implicit: a same-process transition is the NORMAL case for
        a click, so treating it as corroboration made every intra-app click self-
        confirming. The line always states which way it went, so the host can see that
        the precondition was met and still got no corroboration from it."""
        def process_of(observation: Observation) -> str | None:
            info = observation.active_window_info
            if info is None:
                return None
            if info.process_name:
                return str(info.process_name).removesuffix(".exe")
            if info.exe_path:
                return str(info.exe_path).replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".exe")
            return str(info.pid) if info.pid is not None else None

        before_process = process_of(before)
        after_process = process_of(after)
        if before_process is None or after_process is None:
            return (
                "same-process precondition: undeterminable (one side carries no process "
                "identity); recorded as a fact, never sufficient for verified"
            )
        return (
            f"same-process precondition: "
            f"{'met' if before_process == after_process else 'NOT met'} "
            f"(before {before_process!r}, after {after_process!r}); recorded as a fact, "
            f"never sufficient for verified on its own"
        )

    # --- signal extractors (observation fields only; never raise) ----------------------

    @staticmethod
    def _frame_bytes(observation: Observation) -> bytes | None:
        """The observation's RGB pixels as comparable bytes, without encoding work.

        The R-5 capture-time frame stash is the same pixel source the screenshot-diff
        fast path trusts (``ScreenshotDiffStrategy._frames``). The PNG-payload decode
        fallback preserves the historical image-mode behavior byte-for-byte (it decodes
        exactly the bytes the tier decoded pre-R-6). R-6 note: a text-mode session's
        backend may carry a deterministic raw-pixel key payload (never a PNG) — the
        decode fallback would misread such a payload, so key payloads derive from the
        stashed frame (or abstain when absent), never from a decode. The derivation is
        memoized (``_payload_key_bytes``): the pixels are immutable post-capture.
        """
        cached = getattr(observation, "_payload_key_bytes", None)
        if isinstance(cached, (bytes, bytearray)):
            return bytes(cached)
        frame = _frame_for(observation)
        if frame is not None:
            try:
                derived = frame.tobytes()
            except Exception:  # noqa: BLE001 - a broken stash falls through
                return None
            try:  # memoize: the captured pixels never change
                observation._payload_key_bytes = derived
            except Exception:  # noqa: BLE001 - read-only observation: skip the cache
                pass
            return derived
        payload = observation.image_base64
        if isinstance(payload, str) and payload.startswith("RAW:"):
            return None  # fast-key payload without a frame: no derivable pixels
        try:
            return ScreenshotDiffStrategy._decode(payload).tobytes()
        except Exception:  # noqa: BLE001 - undecodable payload: no derivation
            return None

    @staticmethod
    def _payload_text(observation: Observation) -> str:
        """The payload string for digest-inequality comparison (any stable encoding).

        Identical pixels ALWAYS yield an identical string on both representations (PNG
        encode is deterministic; the R-6 raw key is itself a pure pixel hash), and
        distinct pixels yield distinct strings in practice — the signal's premise
        holds for both. No derivation work is needed on either form: the payload
        IS the comparable text (the pre-R-6 code compared the PNG base64 strings
        verbatim; the raw-key payload compares verbatim the same way).
        """
        payload = observation.image_base64
        if not isinstance(payload, str):
            return ""
        if payload.startswith("RAW:") and not payload:
            return ""  # defensive: a degenerate empty key never counts as a signal
        return payload

    @staticmethod
    def _digest_change_magnitude(before: Observation, after: Observation) -> tuple[float, int] | None:
        """(mean difference, strongly-changed pixel count) between the two captures.

        Mirrors :class:`ScreenshotDiffStrategy`'s diff math exactly (same
        ``STRONG_PIXEL_DELTA`` semantics — R-5: both tiers share one implementation),
        so the corroboration floor applied by signal (c) is the same floor the pixel
        tier trusts. Returns ``None`` on ANY decode/shape failure — fail-closed: an
        uncomputable magnitude never corroborates (the signal then abstains to
        ``uncertain``).
        """
        try:
            if before.width != after.width or before.height != after.height:
                return None
            before_image, after_image = ScreenshotDiffStrategy._frames(before, after)
            if before_image.size != after_image.size:
                return None
            mean_difference, strongly_changed = _diff_magnitude(before_image, after_image)
        except Exception:  # noqa: BLE001 - no magnitude -> no corroboration
            return None
        return mean_difference / 255.0, strongly_changed

    @staticmethod
    def _focus_marked_element(observation: Observation) -> dict[str, Any] | None:
        """The element EXPLICITLY marked focused (``focused``/``is_focused``/``has_focus``)."""
        for element in (observation.ui_elements or [])[:50]:
            if isinstance(element, dict) and (
                element.get("focused") or element.get("is_focused") or element.get("has_focus")
            ):
                return element
        return None

    @staticmethod
    def _element_label(element: dict[str, Any]) -> str:
        parts = [
            str(element.get(field_key) or "") for field_key in ("control_type", "name", "automation_id")
        ]
        return "/".join(part for part in parts if part) or "unnamed control"

    @classmethod
    def _focused_element_change(cls, before: Observation, after: Observation) -> str | None:
        """Evidence line naming the focused-control delta, or None when unavailable.

        REM-C (V-2 F2): grounded in REAL focus claims only. The pre-REM-C positional
        fallback (``elements[0]`` when NOTHING was focused) let a UIA re-enumeration
        ORDER change of unfocused controls masquerade as a focus change; with no
        focus marker on either side the signal now ABSTAINS (never verifies). A
        marker APPEARING where none was reported — or DISAPPEARING — is itself a
        deterministic focused-element transition and is named as such.
        """
        before_marked = cls._focus_marked_element(before)
        after_marked = cls._focus_marked_element(after)
        if before_marked is None and after_marked is None:
            # No focus claim anywhere: positional coincidence is not focus evidence.
            return None
        if before_marked is not None and after_marked is not None:
            before_key = (
                str(before_marked.get("name") or ""),
                str(before_marked.get("automation_id") or ""),
                str(before_marked.get("control_type") or ""),
            )
            after_key = (
                str(after_marked.get("name") or ""),
                str(after_marked.get("automation_id") or ""),
                str(after_marked.get("control_type") or ""),
            )
            if before_key == after_key:
                return None
            field = (
                "name"
                if before_key[0] != after_key[0]
                else ("automation_id" if before_key[1] != after_key[1] else "control_type")
            )
            return (
                f"focused element {field}: "
                f"{cls._element_label(before_marked)!r} -> {cls._element_label(after_marked)!r}"
            )
        if before_marked is None:  # focus identity APPEARED (the control received focus)
            return f"focused element identity appeared: (unfocused) -> {cls._element_label(after_marked)!r}"
        # Focus identity DISAPPEARED (the control lost focus).
        return f"focused element identity disappeared: {cls._element_label(before_marked)!r} -> (unfocused)"

    @staticmethod
    def _active_window_change(before: Observation, after: Observation) -> str | None:
        """Evidence line naming the active-window title/process delta, or None.

        A side that carries NO window identity at all never matches as a "change"
        (fail-closed against None -> title), but identity APPEARING where it was
        absent is a real deterministic transition (a dialog opening onto a desktop
        with no foreground window reported).
        """
        before_info = before.active_window_info
        after_info = after.active_window_info
        before_title = (before_info.title if before_info is not None else None) or before.active_window or ""
        after_title = (after_info.title if after_info is not None else None) or after.active_window or ""
        before_process = (
            (before_info.process_name if before_info is not None else None)
            or (before_info.exe_path if before_info is not None else None)
            or ""
        )
        after_process = (
            (after_info.process_name if after_info is not None else None)
            or (after_info.exe_path if after_info is not None else None)
            or ""
        )
        if before_title and after_title and before_title != after_title:
            return f"active window title: {before_title!r} -> {after_title!r}"
        if before_process and after_process and before_process != after_process:
            return f"active window process: {before_process!r} -> {after_process!r}"
        if not before_title and not before_process and (after_title or after_process):
            return (
                f"active window identity appeared: none -> title {after_title!r}"
                + (f" process {after_process!r}" if after_process else "")
            )
        return None


@runtime_checkable
class ModelJudge(Protocol):
    """Callback interface for model-based visual verification (injected by the controller).

    Implemented by the provider layer (Wave 3) WITHOUT verification.py importing
    provider.py — the dependency points from the controller into this module only.
    """

    def judge(
        self, intent: VerificationIntent, before_img: Image.Image, after_img: Image.Image
    ) -> VerificationResult:
        """Judge the transition; return a result whose outcome is authoritative."""
        ...


class ModelVisualStrategy:
    """Model-based visual verification via an injected :class:`ModelJudge`.

    Degrades to ``uncertain`` when no judge is configured (the P0 default), when the judge
    raises, or when the judge returns an unusable value. A judge-reported ``uncertain`` is
    passed through untouched — never upgraded to ``verified``.
    """

    def __init__(self, judge: ModelJudge | None = None) -> None:
        self._judge = judge

    @property
    def name(self) -> str:
        return "model_visual"

    @property
    def judge(self) -> ModelJudge | None:
        return self._judge

    def can_verify(self, intent: VerificationIntent) -> bool:
        return intent.kind == VerificationKind.MODEL_JUDGE

    def verify(self, intent: VerificationIntent, before: Observation, after: Observation) -> VerificationResult:
        if self._judge is None:
            return _uncertain(
                self.name,
                "No model judge is configured; model-based visual verification is unavailable.",
                [],
                0.0,
                changed=False,
            )
        try:
            before_img = ScreenshotDiffStrategy._decode(before.image_base64)
            after_img = ScreenshotDiffStrategy._decode(after.image_base64)
            result = self._judge.judge(intent, before_img, after_img)
        except Exception as exc:  # noqa: BLE001 - degrade, never escape as success
            return _uncertain(
                self.name,
                "Model judge raised an exception; treated as cannot-determine.",
                [f"{type(exc).__name__}: {exc}"],
                0.0,
                changed=False,
            )
        if not isinstance(result, VerificationResult) or result.outcome not in {"verified", "failed", "uncertain"}:
            return _uncertain(
                self.name,
                "Model judge returned an unusable result; treated as cannot-determine.",
                [f"returned={result!r}"],
                0.0,
                changed=False,
            )
        if not result.verification_method or result.verification_method == "none":
            result.verification_method = self.name
        if result.observation_id is None:
            result.observation_id = after.observation_id
        return result


def default_strategy_chain(judge: ModelJudge | None = None) -> list[VerificationStrategy]:
    """Built-in strategy order: cheap deterministic checks first, model judgment last.

    T8 B2 ordering: :class:`UiControlTextStrategy` (window title + UI control text)
    runs as the default expected-text ladder; :class:`TextPredicateStrategy` (OCR)
    stays in the chain as the LAST-RESORT tier and self-disables unless the
    ``CORTEX_OCR_TEXT_VERIFICATION`` config flag is set.
    """
    return [
        DeterministicPredicateStrategy(),
        WindowStateStrategy(),
        ProcessStateStrategy(),
        UiControlTextStrategy(),
        TextPredicateStrategy(),
        FocusChangeStrategy(),  # REM-B H2c: deterministic focus tier before pixel diff
        ScreenshotDiffStrategy(),
        ModelVisualStrategy(judge=judge),
    ]


DEFAULT_STRATEGY_CHAIN: list[VerificationStrategy] = default_strategy_chain()


def deterministic_tiers(
    intent: VerificationIntent,
) -> list[tuple[VerificationStrategy, VerificationIntent]]:
    """Deterministic (cheap-first) verification tiers derivable from ``intent`` (PERF-004 C3).

    For a :class:`VerificationKind.MODEL_JUDGE` intent, each STATED criterion (window
    title, process name/pid, expected text, predicate) yields a deterministic strategy
    plus a narrowly-scoped sub-intent carrying exactly that criterion. Running these
    tiers BEFORE the model judge implements the research-adopted cheap-first ladder:
    the expensive judge tier runs only when every deterministic tier and the pixel-diff
    tier are inconclusive. Non-judge intents never need this helper (the engine chain
    already orders deterministic strategies first).
    """
    tiers: list[tuple[VerificationStrategy, VerificationIntent]] = []
    metadata = dict(intent.metadata)
    if intent.expected_window_title:
        tiers.append(
            (
                WindowStateStrategy(),
                VerificationIntent(
                    kind=VerificationKind.WINDOW_STATE,
                    expected_window_title=intent.expected_window_title,
                    window_title_match=intent.window_title_match,
                    require_bounds_change=intent.require_bounds_change,
                    expected_effect=intent.expected_effect,
                    metadata=metadata,
                ),
            )
        )
    if intent.expected_process_name or intent.expected_pid is not None:
        tiers.append(
            (
                ProcessStateStrategy(),
                VerificationIntent(
                    kind=VerificationKind.PROCESS_STATE,
                    expected_process_name=intent.expected_process_name,
                    expected_pid=intent.expected_pid,
                    expected_effect=intent.expected_effect,
                    metadata=metadata,
                ),
            )
        )
    if intent.expected_text:
        # B2: the deterministic text tier is the UI-control strategy (title + control
        # text); the OCR text-predicate joins ONLY when the config flag enables it.
        tiers.append(
            (
                UiControlTextStrategy(),
                VerificationIntent(
                    kind=VerificationKind.EXPECTED_TEXT,
                    expected_text=intent.expected_text,
                    expected_effect=intent.expected_effect,
                    metadata=metadata,
                ),
            )
        )
        if ocr_text_verification_enabled():
            tiers.append(
                (
                    TextPredicateStrategy(),
                    VerificationIntent(
                        kind=VerificationKind.EXPECTED_TEXT,
                        expected_text=intent.expected_text,
                        expected_effect=intent.expected_effect,
                        metadata=metadata,
                    ),
                )
            )
    if intent.predicate is not None:
        tiers.append(
            (
                DeterministicPredicateStrategy(),
                VerificationIntent(
                    kind=VerificationKind.PREDICATE,
                    predicate=intent.predicate,
                    predicate_name=intent.predicate_name,
                    expected_effect=intent.expected_effect,
                    metadata=metadata,
                ),
            )
        )
    return tiers


class VerificationEngine:
    """Facade over the strategy chain: ``compare`` (legacy) and ``verify`` (semantic).

    ``compare(before, after, expected_change)`` preserves the legacy signature and routes
    through the strategy framework. ``verify`` runs the applicable strategies in order and
    returns the first definitive (verified/failed) outcome; when every strategy is
    uncertain it returns a combined ``uncertain`` result with merged evidence. It NEVER
    maps ``uncertain`` to ``verified``.
    """

    def __init__(
        self,
        judge: ModelJudge | None = None,
        diff_threshold: float = DEFAULT_DIFF_THRESHOLD,
        strategies: Sequence[VerificationStrategy] | None = None,
    ) -> None:
        self._diff_threshold = diff_threshold
        self._strategies: list[VerificationStrategy] = (
            list(strategies) if strategies is not None else default_strategy_chain(judge)
        )

    @property
    def strategies(self) -> list[VerificationStrategy]:
        return list(self._strategies)

    def compare(
        self, before: Observation, after: Observation, expected_change: str | bool | None = None
    ) -> VerificationResult:
        """Legacy entry point: pixel-diff verification with optional expected-change text.

        A truthy string/bool means a change was expected; ``None``/empty means no
        expectation was stated (the strategy then degrades to uncertain when pixels are
        identical or ambiguous instead of claiming success).
        """
        expected: bool | None
        if expected_change is None or (isinstance(expected_change, str) and not expected_change):
            expected = None
        else:
            expected = bool(expected_change)
        intent = VerificationIntent(
            kind=VerificationKind.VISUAL_CHANGE,
            expected_change=expected,
            expected_effect=expected_change if isinstance(expected_change, str) else None,
            diff_threshold=self._diff_threshold,
        )
        return self.verify(intent, before, after)

    def verify(
        self,
        intent: VerificationIntent,
        before: Observation,
        after: Observation,
        strategies: Sequence[VerificationStrategy] | None = None,
    ) -> VerificationResult:
        """Run the strategy chain for ``intent``; first definitive outcome wins.

        When no strategy reaches a definitive outcome the result is a combined
        ``uncertain`` carrying every strategy's evidence — uncertain is never success.
        """
        chain = list(strategies) if strategies is not None else self._strategies
        applicable = [strategy for strategy in chain if strategy.can_verify(intent)]
        if not applicable:
            return self._combine(
                intent, [], after, note="No applicable verification strategy for this intent.", chain=chain
            )

        uncertains: list[VerificationResult] = []
        for strategy in applicable:
            try:
                result = strategy.verify(intent, before, after)
            except Exception as exc:  # noqa: BLE001 - strategy failure is never success
                result = _uncertain(
                    getattr(strategy, "name", strategy.__class__.__name__),
                    "Strategy raised an exception; treated as cannot-determine.",
                    [f"{type(exc).__name__}: {exc}"],
                    0.0,
                    changed=False,
                )
            result = self._enrich(result, strategy, after)
            if result.outcome in {"verified", "failed"}:
                return result
            uncertains.append(result)
        return self._combine(intent, uncertains, after)

    def _enrich(self, result: VerificationResult, strategy: VerificationStrategy, after: Observation) -> VerificationResult:
        strategy_name = getattr(strategy, "name", strategy.__class__.__name__)
        if not result.verification_method or result.verification_method == "none":
            result.verification_method = strategy_name
        if result.observation_id is None:
            result.observation_id = after.observation_id
        return result

    def _combine(
        self,
        intent: VerificationIntent,
        uncertains: list[VerificationResult],
        after: Observation,
        note: str | None = None,
        chain: Sequence[VerificationStrategy] | None = None,
    ) -> VerificationResult:
        methods: list[str] = []
        evidence: list[str] = []
        notes: list[str] = []
        confidence = 0.0
        changed = False
        for result in uncertains:
            if result.verification_method and result.verification_method not in methods:
                methods.append(result.verification_method)
            for item in result.evidence:
                if item not in evidence:
                    evidence.append(item)
            if result.note and result.note not in notes:
                notes.append(result.note)
            confidence = max(confidence, result.confidence)
            changed = changed or result.changed
        if not methods:  # no applicable strategies ran
            for strategy in chain if chain is not None else self._strategies:
                if strategy.can_verify(intent):
                    methods.append(getattr(strategy, "name", strategy.__class__.__name__))
        combined_note = note or (
            "Verification could not determine the outcome (all strategies uncertain): " + " | ".join(notes)
        )
        # v0.7.1 Defect A: the combined-uncertain result keeps the pixel tier's
        # capture provenance (first strategy that carries one) — the identical-frames
        # case is exactly the ALL-UNCERTAIN path, and dropping the provenance here
        # would blind the agent-side capture-source guard.
        capture_provenance = next(
            (result.capture_provenance for result in uncertains if result.capture_provenance is not None),
            None,
        )
        return VerificationResult(
            outcome="uncertain",
            changed=changed,
            note=combined_note,
            confidence=confidence,
            evidence=evidence,
            verification_method="+".join(methods) if methods else "none",
            observation_id=after.observation_id,
            capture_provenance=capture_provenance,
        )

    @staticmethod
    def observation_digest(observation: Observation) -> str:
        return hashlib.sha256(observation.image_base64.encode("ascii")).hexdigest()
