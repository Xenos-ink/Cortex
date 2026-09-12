"""Semantic verification framework: did the intended state transition actually occur?

Layering (master-mission section 5): this module imports only ``models`` (and PIL for the
pixel-diff strategy). It must NEVER import ``provider`` — model-based visual verification
receives a :class:`ModelJudge` callback instead, breaking the provider->verification cycle.

Outcome doctrine (Goal.md section 7, P0-A): every verification produces
``outcome`` in ``{"verified", "failed", "uncertain"}``. ``uncertain`` is NEVER success —
strategies degrade to ``uncertain`` when they lack the data to decide, and the engine's
combined result for all-uncertain chains is itself ``uncertain``. No code path in this
module maps ``uncertain`` to ``verified``.

Confidence doctrine (Goal.md section 6): verification confidence is reported separately
from model/grounding confidence; each strategy returns its own evidence-grounded value.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from PIL import Image, ImageChops

from .models import Observation, VerificationResult

__all__ = [
    "DEFAULT_STRATEGY_CHAIN",
    "DIFF_FAST_ENV",
    "FOCUS_CHANGE_INTENT_FLAG",
    "OCR_TEXT_VERIFICATION_ENV",
    "DeterministicPredicateStrategy",
    "FocusChangeStrategy",
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


def _uncertain(strategy: str, note: str, evidence: list[str], confidence: float, changed: bool) -> VerificationResult:
    return VerificationResult(
        outcome="uncertain",
        changed=changed,
        note=note,
        confidence=confidence,
        evidence=evidence,
        verification_method=strategy,
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
) -> VerificationResult:
    return VerificationResult(
        outcome=outcome,
        changed=changed,
        note=note,
        confidence=confidence,
        evidence=evidence,
        verification_method=strategy,
    )


class ScreenshotDiffStrategy:
    """Pixel-difference verification, upgraded with explicit uncertainty.

    Full positive+negative semantics for ``kind="visual_change"``:

    - dimensions changed -> ``failed`` (legacy-compatible note/confidence 0.5);
    - a screen counts as *changed* when the screen-wide mean difference reaches the
      threshold OR at least ``STRONG_CHANGE_MIN_PIXELS`` pixels changed by
      ``STRONG_PIXEL_DELTA`` or more (mean alone cannot see compact changes such as
      drawn strokes or small controls);
    - ``expected_change=True``: change detected -> ``verified``; none ->
      ``failed`` — except an intent flagged ``FOCUS_CHANGE_INTENT_FLAG`` (a
      focus-type click expectation pixels cannot observe), whose not-observed
      sub-threshold diff degrades to ``uncertain`` (W-1; never success, and a
      real above-threshold change still ``verified``);
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
        try:
            before_image, after_image = self._frames(before, after)
            if before_image.size != after_image.size:
                return _uncertain(
                    self.name,
                    "Decoded image sizes disagree with observation metadata; cannot diff reliably.",
                    [f"Decoded sizes {before_image.size} vs {after_image.size}."],
                    0.2,
                    changed=True,
                )
            mean_difference, strongly_changed = _diff_magnitude(before_image, after_image)
        except Exception as exc:  # noqa: BLE001 - degrade, never escape as success
            return _uncertain(
                self.name,
                "Screenshot diff could not be computed; both observations are treated as unverifiable.",
                [f"{type(exc).__name__}: {exc}"],
                0.0,
                changed=False,
            )

        threshold = max(intent.diff_threshold, 0.0)
        changed = (
            mean_difference >= threshold
            or strongly_changed >= STRONG_CHANGE_MIN_PIXELS
        )

        if intent.kind == VerificationKind.VISUAL_CHANGE:
            return self._verify_visual_change(intent, mean_difference, changed, strongly_changed)
        return self._verify_supporting(intent, mean_difference, changed, strongly_changed)

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
                return _definitive(
                    self.name,
                    "verified",
                    "Visual state changed.",
                    evidence,
                    change_confidence,
                    changed=True,
                )
            # a flagged focus-type expectation ("Hex input
            # focused", "Edit colors dialog opens") describes a transition pixels
            # CANNOT observe. A sub-threshold diff here is the strategy's no-data
            # case, not proof of absence — the exact evidence class this tier
            # degrades to ``uncertain`` when no expectation is stated (below).
            # Emitting a definitive ``failed`` from absent evidence false-failed the
            # live Paint session (mean 0.000/0.013, strong 0 -> failed) and killed
            # the follow_ups queue. Doctrine: degrade to ``uncertain`` (never
            # success); unflagged visual-change intents keep their exact legacy
            # failure semantics below. Zero extra captures: reuses the diff
            # computed above (2-capture floor / CORTEX_DIFF_FAST untouched).
            if intent.metadata.get(FOCUS_CHANGE_INTENT_FLAG):
                note = (
                    "No pixel evidence of the stated focus-type expectation"
                    + (f": {intent.expected_effect}" if intent.expected_effect else "")
                    + "; focus/dialog transitions are often invisible to the pixel diff."
                )
                return _uncertain(self.name, note + " Uncertain is never success.", evidence, 0.4, changed=False)
            note = "Expected change was not observed"
            if intent.expected_effect:
                note += f": {intent.expected_effect}"
            return _definitive(
                self.name, "failed", note + ".", evidence, _EXPECTATION_FAILED_CONFIDENCE, changed=False
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
        (REM-C F1: evidence only by default — it may VERIFY solely when
        corroborated by real changed-pixel magnitude, at least
        ``STRONG_CHANGE_MIN_PIXELS`` strongly-changed pixels as the pixel tier
        defines them; an uncorroborated digest change — a caret blink, a clock
        tick, another app's toast — degrades to ``uncertain`` so the
        screenshot-diff tier decides, preserving that tier's deliberate
        flicker exclusion instead of bypassing it).

    Any one of (a)/(b) showing change, or a CORROBORATED (c), is DEFINITIVE ``verified`` with a note naming
    the signal that fired. NO signal showing change leaves the verdict to the
    next tiers — for a flagged intent the screenshot-diff strategy degrades a
    sub-threshold diff to ``uncertain`` (never ``failed`` from absent
    evidence); unflagged intents keep its exact legacy failure semantics, and it
    never upgrades a pixel-proven non-change.

    Scope gate: only intents flagged ``{FOCUS_CHANGE_INTENT_FLAG}`` in
    ``intent.metadata`` (set by the controller's ``_build_intent`` for CLICK /
    DOUBLE_CLICK actions carrying a stated expected effect). Every other
    visual-change intent — drag, scroll, type, unstated expectations — keeps its
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
        # (a) UIA focused-element change.
        focus_signal = self._focused_element_change(before, after)
        if focus_signal is not None:
            return _definitive(
                self.name,
                "verified",
                (
                    f"Expected focus-type change observed: {expected}. "
                    f"Deterministic signal: focused UI element changed ({focus_signal})."
                ),
                [focus_signal, f"expected_effect={expected!r}"],
                _DETERMINISTIC_CHANGE_CONFIDENCE,
                changed=True,
            )
        # (b) Active-window identity change (title or process).
        window_signal = self._active_window_change(before, after)
        if window_signal is not None:
            return _definitive(
                self.name,
                "verified",
                (
                    f"Expected focus-type change observed: {expected}. "
                    f"Deterministic signal: active window identity changed ({window_signal})."
                ),
                [window_signal, f"expected_effect={expected!r}"],
                _DETERMINISTIC_CHANGE_CONFIDENCE,
                changed=True,
            )
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
            if magnitude is not None and (
                magnitude[0] >= threshold or magnitude[1] >= STRONG_CHANGE_MIN_PIXELS
            ):
                return _definitive(
                    self.name,
                    "verified",
                    (
                        f"Expected focus-type change observed: {expected}. "
                        "Deterministic signal: observation digest changed and the pixel "
                        f"change is corroborated (mean {magnitude[0]:.6f} >= {threshold:.6g} "
                        f"or strongly-changed pixels {magnitude[1]} >= {STRONG_CHANGE_MIN_PIXELS})."
                    ),
                    [
                        "observation digest changed between before and after",
                        (
                            f"corroborated pixel magnitude: mean {magnitude[0]:.6f}, "
                            f"strongly-changed pixels {magnitude[1]} "
                            f"(floor {STRONG_CHANGE_MIN_PIXELS})"
                        ),
                        f"expected_effect={expected!r}",
                    ],
                    _DETERMINISTIC_CHANGE_CONFIDENCE,
                    changed=True,
                )
            # Uncorroborated digest change: ambiguous flicker (caret blink, clock
            # tick, foreign toast) — carry the evidence, defer the verdict. The
            # strategy still never emits ``failed``; the pixel tier decides.
            uncorroborated_note = (
                "Observation digest changed but the pixel change is BELOW the "
                "corroboration floor (strongly-changed "
                f"{magnitude[1] if magnitude is not None else 'uncomputable'} < "
                f"{STRONG_CHANGE_MIN_PIXELS}, mean "
                f"{magnitude[0]:.6f} < {threshold:.6g}); ambiguous flicker is never "
                "proof of a transition — deferring to the pixel-diff tier."
                if magnitude is not None
                else "Observation digest changed but the pixel magnitude could not be "
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
        return VerificationResult(
            outcome="uncertain",
            changed=changed,
            note=combined_note,
            confidence=confidence,
            evidence=evidence,
            verification_method="+".join(methods) if methods else "none",
            observation_id=after.observation_id,
        )

    @staticmethod
    def observation_digest(observation: Observation) -> str:
        return hashlib.sha256(observation.image_base64.encode("ascii")).hexdigest()
