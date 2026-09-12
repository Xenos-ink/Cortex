"""Regression repro (v0.5.6): focus-type clicks false-fail on absent pixel evidence.

Real-desktop occurrence (v0.5.5):

- click (1109,223) ``expected_effect="Hex input focused"`` -> outcome "failed", ok=false,
  note "Expected change was not observed: Hex input focused.", evidence "Mean pixel
  difference 0.013007 (threshold 1). Strongly-changed pixels (delta >= 40): 0 (change
  threshold 50).", verification_method "screenshot_diff" — although the click landed and
  the Edit Colors dialog was open (a FOCUS-type transition, invisible to the pixel tier).
- click ``expected_effect="Edit colors dialog opens"`` -> outcome "failed" with
  "Mean pixel difference 0.000000" — pixel-IDENTICAL frames, still a definitive failure,
  while an expectation-less hotkey with the SAME identical-frames evidence correctly
  degraded to "uncertain". The stated expectation is the only variable.

Defect: the strategy chain ends in a DEFINITIVE "failed" built from ABSENT visual
evidence for a focus-type expectation, contradicting this module's own outcome doctrine
(verification.py: "uncertain is NEVER success — strategies degrade to ``uncertain`` when
they lack the data to decide"). Desired behavior pinned here:

1. the direct MCP ``computer_execute`` path (``run_single`` -> ``_run_single_pipeline``
   -> ``ComputerUseAgent._build_intent(action, None, expected_effect)``) DOES flag a
   CLICK with a stated effect with ``FOCUS_CHANGE_INTENT_FLAG``;
2. a flagged focus-type click whose before/after diff is sub-threshold — including
   pixel-identical frames — must degrade to ``uncertain`` (NEVER a definitive
   ``failed``), with zero extra screen captures;
3. a genuinely visible transition must still be ``verified`` by the pixel tier
   (the strong-pixel path is preserved; no doctrine regression).

Non-e2e: synthetic PIL frames only; no backend, no screen, no MCP server.
"""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace

from PIL import Image

from computer_use_mcp.agent import ComputerUseAgent
from computer_use_mcp.models import ActionType, GroundedAction, Observation, WindowInfo
from computer_use_mcp.verification import (
    DEFAULT_DIFF_THRESHOLD,
    FOCUS_CHANGE_INTENT_FLAG,
    VerificationEngine,
    VerificationIntent,
    VerificationKind,
)

WIDTH, HEIGHT = 320, 180

# The live quirk that silenced FocusChangeStrategy signals (a) and (b): the Win32
# enumeration reports the SAME focused element (InputSiteWindowClass) and the SAME
# active window identity ("Untitled - Paint") before AND after the click, so neither
# deterministic signal fires and the verdict falls to the pixel tier.
_UI_ELEMENTS = [
    {"name": None, "control_type": "InputSiteWindowClass", "automation_id": None, "value": None, "focused": True, "source": "win32"},
    {"name": None, "control_type": "MSPaintView", "automation_id": None, "value": None, "focused": False, "source": "win32"},
]
_WINDOW = WindowInfo(
    hwnd=1706924,
    pid=20548,
    process_name="mspaint.exe",
    title="Untitled - Paint",
    bounds=(0, 0, 1920, 1080),
)


def _png(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _observation(image: Image.Image) -> Observation:
    return Observation(
        image_base64=_png(image),
        width=WIDTH,
        height=HEIGHT,
        active_window="Untitled - Paint",
        active_window_info=_WINDOW,
        ui_elements=list(_UI_ELEMENTS),
    )


def _base_frame() -> Image.Image:
    return Image.new("RGB", (WIDTH, HEIGHT), (40, 40, 40))


def _caret_blink_frame() -> Image.Image:
    """After-frame with a sub-threshold, sub-strong-pixel flicker (live: mean 0.013, strong 0)."""
    after = _base_frame().copy()
    pixels = after.load()
    for x in range(10, 20):  # a 10x10 block, delta 3 in one channel only
        for y in range(10, 20):
            r, g, b = pixels[x, y]
            pixels[x, y] = (r + 3, g, b)
    return after


def _stroke_frame() -> Image.Image:
    """After-frame with a real drawn stroke (hundreds of strongly-changed pixels)."""
    after = _base_frame().copy()
    pixels = after.load()
    for x in range(40, 240):  # a 200x2 stroke, delta 255 -> strongly-changed >= 50
        for y in range(80, 82):
            pixels[x, y] = (255, 255, 255)
    return after


def _direct_path_click_intent(expected_effect: str) -> VerificationIntent:
    """The intent exactly as the direct ``computer_execute`` path builds it.

    server.py:1600-1630 grounds the client action and calls
    ``agent.run_single(..., expected_effect=...)``; agent.py's
    ``_run_single_pipeline`` then calls ``_build_intent(action, None, expected_effect)``.
    ``_build_intent`` references no instance state, so a dummy instance is faithful.
    """
    action = GroundedAction(
        action=ActionType.CLICK,
        point={"x": 1109, "y": 223},
        reason="Explicit MCP action",
        confidence=1.0,
        expected_effect=expected_effect,
    )
    return ComputerUseAgent._build_intent(SimpleNamespace(), action, None, expected_effect)


def test_direct_path_click_with_effect_sets_focus_change_flag():
    """Q1 pin: the direct MCP path flags focus-type clicks (focus tier IS in the chain)."""
    intent = _direct_path_click_intent("Hex input focused")
    assert intent.kind == VerificationKind.VISUAL_CHANGE.value
    assert intent.expected_change is True
    assert intent.expected_effect == "Hex input focused"
    assert intent.metadata.get(FOCUS_CHANGE_INTENT_FLAG) is True


def test_focus_click_subthreshold_diff_degrades_to_uncertain_not_failed():
    """The live false failure: stated focus expectation + sub-threshold diff (mean 0.013,
    strong 0) must NOT yield a definitive ``failed`` — pixels cannot observe a focus
    transition, so the honest outcome is ``uncertain`` (never success, never false fail)."""
    engine = VerificationEngine()
    intent = _direct_path_click_intent("Hex input focused")
    result = engine.verify(intent, _observation(_base_frame()), _observation(_caret_blink_frame()))
    assert result.outcome != "failed", (
        f"focus-type click false-failed from absent pixel evidence: outcome={result.outcome!r} "
        f"note={result.note!r} method={result.verification_method!r}"
    )
    assert result.outcome == "uncertain"


def test_focus_click_pixel_identical_degrades_to_uncertain_not_failed():
    """The second live class: pixel-IDENTICAL frames (live mean 0.000000) with a stated
    focus expectation failed definitively while the expectation-less path degraded to
    ``uncertain`` on the same evidence. The stated expectation must not convert
    absent evidence into a definitive failure."""
    engine = VerificationEngine()
    intent = _direct_path_click_intent("Edit colors dialog opens")
    result = engine.verify(intent, _observation(_base_frame()), _observation(_base_frame()))
    assert result.outcome != "failed", (
        f"focus-type click false-failed on pixel-identical frames: outcome={result.outcome!r} "
        f"note={result.note!r} method={result.verification_method!r}"
    )
    assert result.outcome == "uncertain"


def test_genuine_visible_change_still_verifies():
    """Scope guard: a real visual transition (drawn stroke) still verifies via the pixel
    tier's strong-pixel path — the fix must not downgrade genuine evidence."""
    engine = VerificationEngine()
    intent = _direct_path_click_intent("Line segment drawn on canvas")
    result = engine.verify(intent, _observation(_base_frame()), _observation(_stroke_frame()))
    assert result.outcome == "verified", (
        f"genuine change lost its verdict: outcome={result.outcome!r} note={result.note!r}"
    )
    assert result.changed is True


def test_default_thresholds_unchanged():
    """R-7 invariant pin: the repro runs against the shipped thresholds."""
    assert DEFAULT_DIFF_THRESHOLD == 1.0
    intent = _direct_path_click_intent("Hex input focused")
    assert intent.diff_threshold == DEFAULT_DIFF_THRESHOLD


def test_unflagged_visual_change_intent_keeps_legacy_failed():
    """verdict-honesty  contract update of the focus-click scope guard.

    The W-1-era pin held that an UNFLAGGED visual-change intent with a stated
    effect keeps the definitive legacy ``failed``. D11 extends the absent-evidence
    doctrine to ANY stated effect (flagged or not): a hotkey/keypress expectation
    with sub-threshold pixel evidence now degrades to ``uncertain`` (0.4) — the
    live Paint keypress-Enter commit false-failed with a 0.000000 diff. The LEGACY
    semantics survive verbatim for a BARE change expectation (no described
    effect): there the pixel change is the whole claim, so absent change is a
    definitive ``failed`` — real-defect detection is not weakened.
    """
    engine = VerificationEngine()
    stated = VerificationIntent(
        kind=VerificationKind.VISUAL_CHANGE.value,
        expected_change=True,
        expected_effect="Hex input focused",
        metadata={"action_id": "unflagged-intent", "verification_hint": ""},
    )
    assert stated.metadata.get(FOCUS_CHANGE_INTENT_FLAG) is None
    result = engine.verify(
        stated, _observation(_base_frame()), _observation(_caret_blink_frame())
    )
    assert result.outcome == "uncertain", (
        f"stated-effect unflagged intent did not degrade: outcome={result.outcome!r} "
        f"note={result.note!r}"
    )
    assert result.confidence == 0.4  # uncertain is never success; ok stays False upstream
    assert result.verification_method == "screenshot_diff"
    # The bare change expectation (no effect stated) keeps the exact legacy failure.
    bare = VerificationIntent(
        kind=VerificationKind.VISUAL_CHANGE.value,
        expected_change=True,
        metadata={"action_id": "unflagged-intent", "verification_hint": ""},
    )
    legacy = engine.verify(bare, _observation(_base_frame()), _observation(_caret_blink_frame()))
    assert legacy.outcome == "failed", (
        f"bare change expectation lost the legacy failure: outcome={legacy.outcome!r}"
    )
    assert legacy.note == "Expected change was not observed."
    assert legacy.confidence == 0.85  # _EXPECTATION_FAILED_CONFIDENCE, unchanged
    assert legacy.verification_method == "screenshot_diff"
