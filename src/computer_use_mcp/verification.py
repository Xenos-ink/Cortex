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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from PIL import Image, ImageChops, ImageStat

from .models import Observation, VerificationResult

__all__ = [
    "DEFAULT_STRATEGY_CHAIN",
    "DeterministicPredicateStrategy",
    "ModelJudge",
    "ModelVisualStrategy",
    "ProcessStateStrategy",
    "ScreenshotDiffStrategy",
    "TextPredicateStrategy",
    "VerificationEngine",
    "VerificationIntent",
    "VerificationKind",
    "VerificationStrategy",
    "WindowStateStrategy",
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
      ``failed``;
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
            before_image = self._decode(before.image_base64)
            after_image = self._decode(after.image_base64)
            if before_image.size != after_image.size:
                return _uncertain(
                    self.name,
                    "Decoded image sizes disagree with observation metadata; cannot diff reliably.",
                    [f"Decoded sizes {before_image.size} vs {after_image.size}."],
                    0.2,
                    changed=True,
                )
            diff = ImageChops.difference(before_image, after_image)
            mean_difference = sum(ImageStat.Stat(diff).mean) / 3.0
            bands = diff.split()
            if len(bands) == 1:
                max_band = bands[0]
            else:
                max_band = ImageChops.lighter(ImageChops.lighter(bands[0], bands[1]), bands[2])
            strongly_changed = sum(max_band.histogram()[STRONG_PIXEL_DELTA:])
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

    Works only when OCR data is present (P1 perception fills ``Observation.ocr_text``);
    without OCR data it degrades to ``uncertain`` — never to success.
    """

    @property
    def name(self) -> str:
        return "text_predicate"

    def can_verify(self, intent: VerificationIntent) -> bool:
        return intent.kind == VerificationKind.EXPECTED_TEXT

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
    """Built-in strategy order: cheap deterministic checks first, model judgment last."""
    return [
        DeterministicPredicateStrategy(),
        WindowStateStrategy(),
        ProcessStateStrategy(),
        TextPredicateStrategy(),
        ScreenshotDiffStrategy(),
        ModelVisualStrategy(judge=judge),
    ]


DEFAULT_STRATEGY_CHAIN: list[VerificationStrategy] = default_strategy_chain()


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
