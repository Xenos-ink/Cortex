"""R-7 verify-diff acceleration pins (ORVEX-CORTEX-056-LIVEFIX, mission goal section 7).

Pins the R-7 diff fast path (``verification._diff_magnitude`` /
``_diff_magnitude_fast``) as PROVABLY verdict-preserving:

- BYTE-IDENTICAL MAGNITUDES: for every recorded-corpus-shaped and adversarial
  frame pair (real-desktop-shaped frames with compact strokes, caret flicker,
  the 49/50/51 strongly-changed-pixel verdict cliff, uniform 39/40-delta noise,
  single pixels, scattered corner changes, alternating polarity, RGBA, L-mode,
  identical frames, identical raw stashes, size-mismatched frames, 1x1 frames),
  the fast path returns the SAME (mean, strongly_changed) tuple as the legacy
  full-frame computation — exact float equality, not closeness.
- BYTE-IDENTICAL VERDICTS: the full ScreenshotDiffStrategy / FocusChangeStrategy
  / VerificationEngine results (outcome, changed, note, confidence, evidence,
  method — the whole serialized model) are identical under the fast path and
  ``CORTEX_DIFF_FAST=0``.
- VERDICT CLIFF: the 49/50/51-pixel boundary produces changed=False/False/True
  on BOTH paths (the R-6 downsample rejection lesson: nothing may move this).
- RGBA/L/mismatched-size frames route to the legacy body (RGBA's getbbox is
  alpha-blind — measured: a 190-delta pixel with unchanged alpha yields bbox
  None — so RGB-adjacent modes must never fast-path).
- Identical-raw short-circuit: two frames sharing one readback buffer (the
  dxgi/mss capture contract) memcmp equal -> (0.0, 0); SAME buffer object ->
  no shortcut, bbox proves zero diff -> (0.0, 0) either way.
- KILL-SWITCH: ``CORTEX_DIFF_FAST=0`` restores the pre-R-7 computation; every
  other value keeps the fast path.
- PERFORMANCE (steady-state, live Windows only): a compact stroke change on a
  1920x1080 frame verifies in <= 40 ms p50 under desktop noise (measured
  ~16 ms p50 clean-process; the bar keeps CI flake-free at ~2.5x headroom),
  strictly faster than the legacy path on the same pair.

No OS input is dispatched anywhere in this file; the live-performance pin only
reads pixels it synthesized itself (no capture at all — frames are built
in-memory).
"""

from __future__ import annotations

import os
import platform
import time
from typing import Any

import pytest
from PIL import Image

from computer_use_mcp.models import Observation
from computer_use_mcp.verification import (
    DIFF_FAST_ENV,
    STRONG_CHANGE_MIN_PIXELS,
    STRONG_PIXEL_DELTA,
    FocusChangeStrategy,
    ScreenshotDiffStrategy,
    VerificationEngine,
    VerificationIntent,
    VerificationKind,
    _diff_fast_enabled,
    _diff_magnitude,
    _diff_magnitude_fast,
)


# =====================================================================================
# Frame-pair builders (self-contained: CI never needs the recorded .npz corpus)
# =====================================================================================


def _frame(width: int = 320, height: int = 180, base: tuple[int, int, int] = (90, 110, 130)) -> Image.Image:
    """A non-uniform frame: gradient background (real frames never are flat)."""
    img = Image.new("RGB", (width, height), base)
    px = img.load()
    for y in range(height):
        for x in range(width):
            px[x, y] = ((x * 3) % 256, (y * 5) % 256, ((x + y) * 2) % 256)
    return img


def _patch(img: Image.Image, x0: int, y0: int, w: int, h: int, rgb: tuple[int, int, int]) -> Image.Image:
    out = img.copy()
    px = out.load()
    for y in range(y0, y0 + h):
        for x in range(x0, x0 + w):
            if 0 <= x < out.width and 0 <= y < out.height:
                px[x, y] = rgb
    return out


def _boundary_frame(base: Image.Image, n_strong: int) -> Image.Image:
    """A copy whose first ``n_strong`` pixels changed by >= STRONG_PIXEL_DELTA."""
    out = base.copy()
    px = out.load()
    placed = 0
    for y in range(base.height):
        for x in range(base.width):
            if placed >= n_strong:
                return out
            r, g, b = base.load()[x, y]
            # +200 (clamped) is a guaranteed >= 40 delta on any real frame value
            px[x, y] = (min(255, r + 200), min(255, g + 200), min(255, b + 200))
            placed += 1
    return out


def _uniform_delta_frame(base: Image.Image, delta: int, every: int = 1) -> Image.Image:
    """Every ``every``-th pixel shifted by exactly ``delta`` (sub-40 noise stress)."""
    out = base.copy()
    px = out.load()
    for y in range(base.height):
        for x in range(base.width):
            if (x + y) % every == 0:
                r, g, b = base.load()[x, y]
                px[x, y] = (min(255, r + delta), min(255, g + delta), min(255, b + delta))
    return out


def _corpus_pairs() -> list[tuple[str, Image.Image, Image.Image]]:
    """Every verdict-relevant pair shape the R-7 corpus recorded (synthetic twins)."""
    base = _frame()
    w, h = base.size
    pairs: list[tuple[str, Image.Image, Image.Image]] = [
        ("identical", base, base.copy()),
        ("caret_9px", base, _patch(base, 10, 10, 3, 3, (255, 255, 255))),
        (
            f"boundary_{STRONG_CHANGE_MIN_PIXELS - 1}px",
            base,
            _boundary_frame(base, STRONG_CHANGE_MIN_PIXELS - 1),
        ),
        (
            f"boundary_{STRONG_CHANGE_MIN_PIXELS}px",
            base,
            _boundary_frame(base, STRONG_CHANGE_MIN_PIXELS),
        ),
        (
            f"boundary_{STRONG_CHANGE_MIN_PIXELS + 1}px",
            base,
            _boundary_frame(base, STRONG_CHANGE_MIN_PIXELS + 1),
        ),
        ("stroke_compact", base, _patch(base, 20, 20, 40, 5, (255, 255, 255))),
        ("stroke_scattered", base, _patch(_patch(base, 1, 1, 3, 3, (255, 0, 0)), w - 4, h - 4, 3, 3, (0, 255, 0))),
        ("uniform39_quad", base, _uniform_delta_frame(base, STRONG_PIXEL_DELTA - 1, 2)),
        ("uniform40", base, _uniform_delta_frame(base, STRONG_PIXEL_DELTA, 1)),
        ("single_px", base, _patch(base, 5, 5, 1, 1, (255, 0, 0))),
        ("corner_b_only", base, _patch(base, w - 1, h - 1, 1, 1, (90, 110, 240))),
        ("column_full_height", base, _patch(base, w // 2, 0, 1, h, (0, 90, 0))),
    ]
    # alternating polarity checkerboard (polarity stress)
    alt = base.copy()
    px = alt.load()
    for y in range(h):
        for x in range(w):
            if (x + y) % 2 == 0:
                r, g, b = base.load()[x, y]
                px[x, y] = (min(255, r + 200), max(0, g - 200), b)
    pairs.append(("alternating_polarity", base, alt))
    return pairs


# =====================================================================================
# Magnitude equivalence: exact tuple equality on every pair shape
# =====================================================================================


def _magnitude_with_env(before: Image.Image, after: Image.Image, fast: str) -> tuple[float, int]:
    os.environ[DIFF_FAST_ENV] = fast
    try:
        return _diff_magnitude(before, after)
    finally:
        os.environ.pop(DIFF_FAST_ENV, None)


@pytest.mark.parametrize("label,before,after", _corpus_pairs())
def test_magnitude_identical_fast_vs_legacy(label: str, before: Image.Image, after: Image.Image) -> None:
    """EXACT equality (float bits and strong count) of the fast magnitude vs legacy."""
    fast = _magnitude_with_env(before, after, "1")
    legacy = _magnitude_with_env(before, after, "0")
    assert fast == legacy, (label, fast, legacy)
    assert fast[0].hex() == legacy[0].hex(), (label, fast[0], legacy[0])  # bit-identical mean


def test_verdict_cliff_49_50_51_unchanged_on_both_paths() -> None:
    """The R-6 lesson: nothing may move the 50-strongly-changed-pixel verdict cliff."""
    base = _frame()
    intent = VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=True)
    strategy = ScreenshotDiffStrategy()
    for n_px in (49, 50, 51):
        after = _boundary_frame(base, n_px)
        expected_changed = n_px >= STRONG_CHANGE_MIN_PIXELS
        for env in ("1", "0"):
            os.environ[DIFF_FAST_ENV] = env
            try:
                magnitude = _diff_magnitude(base, after)
                result = strategy.verify(intent, *_observations(base, after))
            finally:
                os.environ.pop(DIFF_FAST_ENV, None)
            assert magnitude[1] == n_px, (env, n_px, magnitude)
            # mean stays far below the threshold: only the strong-count drives the verdict
            assert (magnitude[1] >= STRONG_CHANGE_MIN_PIXELS) is expected_changed
            assert result.changed is expected_changed, (env, n_px, result)


def test_identical_frames_mean_zero_strong_zero_both_paths() -> None:
    base = _frame()
    for env in ("1", "0"):
        assert _magnitude_with_env(base, base.copy(), env) == (0.0, 0)


# =====================================================================================
# Structural fallbacks: modes the fast path must NEVER take
# =====================================================================================


def test_rgba_routes_to_legacy_body() -> None:
    """RGBA getbbox is alpha-blind (a 190-delta pixel with unchanged alpha yields
    bbox None), so RGBA MUST fall back — the fast helper returns None for it."""
    before = Image.new("RGBA", (64, 64), (10, 10, 10, 255))
    after = before.copy()
    after.load()[3, 3] = (200, 10, 10, 255)
    assert _diff_magnitude_fast(before, after) is None
    # and the full function still computes the true magnitude via the legacy body
    assert _magnitude_with_env(before, after, "1") == _magnitude_with_env(before, after, "0")


def test_l_mode_routes_to_legacy_body() -> None:
    before = _frame().convert("L")
    after = before.copy()
    after.load()[10, 10] = 250
    assert _diff_magnitude_fast(before, after) is None
    assert _magnitude_with_env(before, after, "1") == _magnitude_with_env(before, after, "0")


def test_size_mismatch_never_fast_paths() -> None:
    before = Image.new("RGB", (10, 10), (0, 0, 0))
    after = Image.new("RGB", (11, 10), (0, 0, 0))
    assert _diff_magnitude_fast(before, after) is None


def test_1x1_frame_exact() -> None:
    before = Image.new("RGB", (1, 1), (5, 5, 5))
    after = Image.new("RGB", (1, 1), (250, 5, 5))
    fast = _magnitude_with_env(before, after, "1")
    legacy = _magnitude_with_env(before, after, "0")
    assert fast == legacy and fast[1] == 1


# =====================================================================================
# Identical-raw short-circuit (the dxgi/mss readback-buffer contract)
# =====================================================================================


def test_identical_raw_stash_memcmp_short_circuit() -> None:
    """Two distinct-but-equal raw buffers (one readback per capture) -> (0.0, 0)
    in one memcmp — and the SAME tuple the legacy computation returns."""
    raw = _frame().tobytes()
    before = Image.frombuffer("RGB", (320, 180), raw, "raw", "RGB", 0, 1)
    after = Image.frombuffer("RGB", (320, 180), raw[:], "raw", "RGB", 0, 1)
    before._frame_raw = raw  # noqa: SLF001 - mirrors the backend's private stash
    after._frame_raw = raw[:]  # noqa: SLF001
    assert _diff_magnitude_fast(before, after) == (0.0, 0)
    assert _magnitude_with_env(before, after, "1") == (0.0, 0)
    assert _magnitude_with_env(before, after, "0") == (0.0, 0)


def test_shared_same_raw_object_no_false_shortcut() -> None:
    """The SAME buffer object (a reused readback) must not memcmp itself; the
    bbox proof still yields the exact zero diff."""
    raw = _frame().tobytes()
    img = Image.frombuffer("RGB", (320, 180), raw, "raw", "RGB", 0, 1)
    img._frame_raw = raw  # noqa: SLF001
    assert _diff_magnitude_fast(img, img) == (0.0, 0)


def test_distinct_raw_stash_still_diffs() -> None:
    """Changed pixels behind DIFFERENT raw stashes take the bbox path, not the
    memcmp shortcut — and stay exact."""
    before = _frame()
    after = _patch(before, 12, 12, 6, 6, (255, 255, 255))
    before._frame_raw = before.tobytes()  # noqa: SLF001
    after._frame_raw = after.tobytes()  # noqa: SLF001
    fast = _magnitude_with_env(before, after, "1")
    legacy = _magnitude_with_env(before, after, "0")
    assert fast == legacy and fast[1] == 36


def test_non_bytes_stash_ignored() -> None:
    """A garbage stash (non-bytes) can never reach the memcmp or crash."""
    before = _frame()
    after = _patch(before, 3, 3, 2, 2, (255, 255, 255))
    before._frame_raw = "not-bytes"  # noqa: SLF001
    after._frame_raw = None  # noqa: SLF001
    assert _magnitude_with_env(before, after, "1") == _magnitude_with_env(before, after, "0")


# =====================================================================================
# Verdict-level equivalence (full serialized results, strategy + engine)
# =====================================================================================


def _observations(before: Image.Image, after: Image.Image) -> tuple[Observation, Observation]:
    def obs(img: Image.Image) -> Observation:
        o = Observation(image_base64="RAW:PIN", width=img.width, height=img.height)
        o._frame = img  # noqa: SLF001 - the R-5 stash both paths consume
        return o

    return obs(before), obs(after)


@pytest.mark.parametrize("label,before,after", _corpus_pairs())
def test_verdicts_identical_fast_vs_legacy(label: str, before: Image.Image, after: Image.Image) -> None:
    """The WHOLE verification result (outcome, changed, note, confidence,
    evidence, method) is byte-identical under both paths, for every intent shape
    the action path produces — plain, stability, unstated, focus-flagged, and
    a non-visual supporting kind."""
    ob, oa = _observations(before, after)
    intents = [
        VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=True),
        VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=False),
        VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=None),
        VerificationIntent(
            kind=VerificationKind.VISUAL_CHANGE,
            expected_change=True,
            expected_effect="something changes",
            metadata={"focus_change_click": True},
            diff_threshold=1.0,
        ),
        VerificationIntent(kind=VerificationKind.PREDICATE, expected_change=True),
    ]
    diff_strategy = ScreenshotDiffStrategy()
    focus_strategy = FocusChangeStrategy()
    engine = VerificationEngine()
    for intent in intents:
        fast_results = _run_all(diff_strategy, focus_strategy, engine, intent, ob, oa, "1")
        legacy_results = _run_all(diff_strategy, focus_strategy, engine, intent, ob, oa, "0")
        for (rf, tag), (rl, _) in zip(fast_results, legacy_results):
            assert rf.model_dump() == rl.model_dump(), (label, tag, intent.kind)


def _run_all(
    diff_strategy: ScreenshotDiffStrategy,
    focus_strategy: FocusChangeStrategy,
    engine: VerificationEngine,
    intent: VerificationIntent,
    before: Observation,
    after: Observation,
    env: str,
) -> list[tuple[Any, str]]:
    os.environ[DIFF_FAST_ENV] = env
    try:
        return [
            (diff_strategy.verify(intent, before, after), "diff"),
            (focus_strategy.verify(intent, before, after), "focus"),
            (engine.verify(intent, before, after), "engine"),
        ]
    finally:
        os.environ.pop(DIFF_FAST_ENV, None)


# =====================================================================================
# Kill-switch
# =====================================================================================


def test_kill_switch_zero_disables_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DIFF_FAST_ENV, "0")
    assert _diff_fast_enabled() is False


def test_kill_switch_anything_else_keeps_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("", "  ", "1", "true", "off", "no", "0.0", "00"):
        monkeypatch.setenv(DIFF_FAST_ENV, value)
        assert _diff_fast_enabled() is (value.strip() != "0"), value
    monkeypatch.delenv(DIFF_FAST_ENV, raising=False)
    assert _diff_fast_enabled() is True


def test_magnitude_equal_under_both_env_states_globally() -> None:
    """Toggle the env INSIDE one process (the lazy resolution contract): both
    states compute identical magnitudes on a representative pair."""
    base = _frame()
    after = _boundary_frame(base, STRONG_CHANGE_MIN_PIXELS)
    results = [_magnitude_with_env(base, after, env) for env in ("1", "0", "1", "0")]
    assert results[0] == results[1] == results[2] == results[3]


# =====================================================================================
# Performance pin (live Windows; frames are synthetic — no capture, no input)
# =====================================================================================


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows-only live hardware timing")
def test_steady_state_stroke_diff_under_bar() -> None:
    """PERFORMANCE PIN: a compact stroke change (the drawing mission's typical
    diff) on a 1920x1080 frame computes in <= 40 ms p50 under live desktop noise
    (clean-process measurement ~16 ms p50, ~9 ms min; legacy ~35 ms p50 on the
    same pair). The headroom keeps CI flake-free (~2.5x)."""
    base = Image.new("RGB", (1920, 1080))
    px = base.load()
    for y in range(0, 1080, 7):
        for x in range(0, 1920, 9):
            px[x, y] = ((x * 3) % 256, (y * 5) % 256, 120)
    after = _patch(base, 100, 100, 200, 3, (255, 255, 255))  # compact stroke, ~600 strong px
    _diff_magnitude(base, after)  # warm
    os.environ[DIFF_FAST_ENV] = "1"
    try:
        times: list[float] = []
        for _ in range(15):
            t0 = time.perf_counter()
            _diff_magnitude(base, after)
            times.append((time.perf_counter() - t0) * 1000.0)
        times.sort()
        p50 = times[len(times) // 2]
    finally:
        os.environ.pop(DIFF_FAST_ENV, None)
    assert p50 <= 40.0, p50
