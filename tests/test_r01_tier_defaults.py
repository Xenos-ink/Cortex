"""R-01: deterministic-first verification tier defaults for ``type`` and ``keypress``.

ROADMAP R-01: the pixel-diff tier produced false-negatives on ``type`` actions
(verdict ``failed`` although the effect had landed), forcing chain stops and
single-action fallbacks. The contract: the deterministic / observe-confirm tier
(UiControlText / FocusChange / window / process signals) is the DEFAULT decision
tier for ``type`` and ``keypress``; pixel-diff is demoted to an ambiguous-band
escalation used ONLY when the deterministic tiers are inconclusive; the observed
false-``failed`` verdicts are dead (uncertain is NEVER success — preserved).

Pinned here, hermetically:

- the built-in strategy chain is ordered deterministic-first with the pixel tier
  positioned as a late escalation;
- a TYPE action's deterministic text needle is the TYPED TEXT (the string that
  lands in the field value), not the effect prose — the prose made the
  deterministic tier abstain on every stated-effect type action and escalated
  them all to pixels;
- a stated-effect TYPE/KEYPRESS verdict on absent pixel evidence is UNCERTAIN,
  never ``failed`` (the D11 doctrine at the intent level), and uncertain is
  never success;
- the keyboard launch-prefix promotion (``open …``/``focus …``) keeps its
  deterministic ``window_state`` default, deciding BEFORE pixels;
- the deterministic focus-change flag stays CLICK-scoped (the R2 D11
  fault-injection pin: a window/element change must not verify a non-click
  effect — generalizing it was a false-success vector and stays forbidden).

Everything runs against fakes/observations only (no real GUI, no provider).
"""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace
from typing import Any

from PIL import Image, ImageDraw

from computer_use_mcp.agent import ComputerUseAgent
from computer_use_mcp.models import ActionType, GroundedAction, Observation
from computer_use_mcp.verification import (
    FOCUS_CHANGE_INTENT_FLAG,
    ScreenshotDiffStrategy,
    VerificationEngine,
    VerificationIntent,
    VerificationKind,
    default_strategy_chain,
    deterministic_tiers,
)

# --- helpers ---------------------------------------------------------------------------------


def _png(color: str = "white", width: int = 64, height: int = 48) -> str:
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _stroke_png(width: int = 320, height: int = 240) -> str:
    """A frame with a real drawn stroke: hundreds of strongly-changed pixels."""
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.line([(10, 10), (width - 10, 12)], fill=(0, 0, 0), width=2)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _observation(png: str = "white", **kwargs: Any) -> Observation:
    return Observation(
        image_base64=_png() if png == "white" else png,
        width=64,
        height=48,
        **kwargs,
    )


def _build_intent(action: GroundedAction, hint: str | None, effect: str | None):
    """The intent exactly as both controller paths build it (W1 test pattern).

    ``_build_intent`` references no instance state, so a dummy instance is faithful.
    """
    return ComputerUseAgent._build_intent(SimpleNamespace(), action, hint, effect)


def _typed(text: str, effect: str | None = None) -> GroundedAction:
    return GroundedAction(
        action=ActionType.TYPE,
        text=text,
        reason="R-01 pin",
        confidence=1.0,
        expected_effect=effect,
    )


def _pressed(*keys: str, effect: str | None = None) -> GroundedAction:
    return GroundedAction(
        action=ActionType.KEYPRESS,
        keys=list(keys),
        reason="R-01 pin",
        confidence=1.0,
        expected_effect=effect,
    )


# --- 1. the chain itself: deterministic strategies first, pixels as escalation ---------------


def test_default_chain_is_deterministic_first_with_pixel_escalation() -> None:
    """R-01 structure pin: every deterministic tier precedes the pixel-diff tier."""
    names = [strategy.name for strategy in default_strategy_chain()]
    assert names == [
        "deterministic_predicate",
        "window_state",
        "process_state",
        "ui_control_text",
        "text_predicate",
        "focus_change",
        "screenshot_diff",
        "model_visual",
    ]
    pixel_index = names.index("screenshot_diff")
    for deterministic in (
        "deterministic_predicate",
        "window_state",
        "process_state",
        "ui_control_text",
        "focus_change",
    ):
        assert names.index(deterministic) < pixel_index, names


def test_pixel_tier_only_sees_intents_the_deterministic_tiers_abandoned() -> None:
    """Escalation semantics at the engine level: the first DEFINITIVE verdict wins,
    so a deterministic ``verified``/``failed`` ends the chain BEFORE pixels run; the
    pixel tier decides only when every earlier tier returned uncertain."""
    engine = VerificationEngine()
    before = _observation("white")
    after = _observation(
        "white",
        ui_elements=[{"name": "Editor", "control_type": "Edit", "value": "typed line"}],
    )
    intent = VerificationIntent(
        kind=VerificationKind.EXPECTED_TEXT.value, expected_text="typed line"
    )
    result = engine.verify(intent, before, after)
    assert result.outcome == "verified", result
    # the pixel tier never participated: the deterministic tier decided alone.
    assert result.verification_method == "ui_control_text", result


# --- 2. TYPE: the deterministic needle is the typed text, not the effect prose ----------------


def test_type_intent_defaults_to_deterministic_expected_text() -> None:
    """Default kind for a type action is ``expected_text`` (the deterministic
    ui_control_text ladder), with or without a stated effect."""
    plain = _build_intent(_typed("hello world"), None, None)
    assert plain.kind == VerificationKind.EXPECTED_TEXT.value
    assert plain.expected_text == "hello world"
    stated = _build_intent(_typed("C8C3B2", "the hex field shows the value"), None, None)
    assert stated.kind == VerificationKind.EXPECTED_TEXT.value


def test_type_needle_is_the_typed_text_not_the_effect_prose() -> None:
    """R-01 core fix: with a stated effect, the deterministic tier searches the TYPED
    TEXT. The effect prose never appears verbatim in a window title or control value,
    so the old preference made the deterministic tier abstain on EVERY stated-effect
    type action and escalated them all to the pixel band."""
    intent = _build_intent(_typed("C8C3B2", "the hex field shows the value"), None, None)
    assert intent.kind == VerificationKind.EXPECTED_TEXT.value
    assert intent.expected_text == "C8C3B2", intent
    assert intent.expected_effect == "the hex field shows the value"


def test_type_with_stated_effect_verifies_deterministically_without_pixels() -> None:
    """The deterministic tier DECIDES a stated-effect type action when the typed text
    is visible in a control value — the pixel tier is never reached (method is exactly
    ``ui_control_text``, not a combined all-uncertain method)."""
    engine = VerificationEngine()
    before = _observation("white")
    after = _observation(
        "white",  # pixels IDENTICAL: the pixel tier could never verify this
        ui_elements=[{"name": "Hex", "control_type": "Edit", "value": "C8C3B2"}],
    )
    intent = _build_intent(_typed("C8C3B2", "the hex field shows the value"), None, None)
    result = engine.verify(intent, before, after)
    assert result.outcome == "verified", result
    assert result.verification_method == "ui_control_text", result
    assert result.verified is True


def test_type_model_judge_ladder_keeps_typed_text_needle() -> None:
    """A model_judge intent carries the deterministic criteria too (PERF-004 C3); the
    R-01 needle rule holds there: the cheap ui_control_text tier searches the typed
    text, so a deterministic verdict can still skip the judge."""
    intent = _build_intent(_typed("C8C3B2", "the hex field shows the value"), "model_judge", None)
    assert intent.kind == VerificationKind.MODEL_JUDGE.value
    assert intent.expected_text == "C8C3B2"
    tiers = deterministic_tiers(intent)
    text_tiers = [
        sub.expected_text
        for strategy, sub in tiers
        if strategy.name == "ui_control_text"
    ]
    assert text_tiers == ["C8C3B2"], tiers


# --- 3. the escalation band: absent pixel evidence is uncertain, never failed -----------------


def test_type_invisible_text_escalates_to_pixels_and_never_false_fails() -> None:
    """The documented two-step protocol: the deterministic text tier returns
    uncertain for content invisible to title/controls (spreadsheet cells, canvases);
    the controller then escalates to the pixel band (visual_change re-verify). Both
    verdicts on absent evidence are UNCERTAIN — the R-01 false-``failed`` class is
    dead — and uncertain is never success."""
    engine = VerificationEngine()
    before = _observation("white")
    after = _observation("white", ui_elements=[{"name": "Sheet1", "control_type": "Custom"}])
    action = _typed("cell payload", "the cell shows the payload")

    first = engine.verify(_build_intent(action, None, None), before, after)
    assert first.outcome == "uncertain", first
    assert first.verified is False

    escalation_intent = _build_intent(action, "visual_change", "the cell shows the payload")
    assert escalation_intent.kind == VerificationKind.VISUAL_CHANGE.value
    escalation = engine.verify(escalation_intent, before, after)
    assert escalation.outcome == "uncertain", escalation
    assert escalation.verified is False
    assert "screenshot_diff" in str(escalation.verification_method), escalation
    assert escalation.outcome != "failed"


def test_stated_effect_type_and_keypress_absent_evidence_is_uncertain_not_failed() -> None:
    """D11 at the R-01 intent level: stated-effect type AND keypress intents on
    pixel-identical frames degrade to uncertain (never the historical false
    ``failed``); the type escalation band lands at confidence 0.4 and the same
    evidence with a REAL above-floor change still verifies."""
    engine = VerificationEngine()
    before = _observation("white")
    identical = _observation("white")

    for action in (
        _typed("C8C3B2", "the hex field shows the value"),
        _pressed("enter", effect="the dialog commits"),
    ):
        intent = _build_intent(action, None, action.expected_effect)
        assert intent.expected_effect, intent
        if intent.kind == VerificationKind.VISUAL_CHANGE.value:
            assert intent.expected_change is True, intent
        absent = engine.verify(intent, before, identical)
        assert absent.outcome == "uncertain", (action.action, absent)
        assert absent.verified is False
        if intent.kind == VerificationKind.EXPECTED_TEXT.value:
            # the controller escalates an uncertain expected-text verdict to the
            # pixel band; that escalation is ALSO uncertain (never failed) at 0.4.
            escalation = engine.verify(
                _build_intent(action, "visual_change", action.expected_effect),
                before,
                identical,
            )
            assert escalation.outcome == "uncertain", (action.action, escalation)
            assert "screenshot_diff" in str(escalation.verification_method), escalation
            assert abs(escalation.confidence - 0.4) < 1e-9, (action.action, escalation)
        else:
            assert abs(absent.confidence - 0.4) < 1e-9, (action.action, absent)

    # The positive band is intact: a real pixel transition verifies.
    before_big = Observation(image_base64=_png("white", 320, 240), width=320, height=240)
    after_big = Observation(image_base64=_stroke_png(320, 240), width=320, height=240)
    keypress_intent = _build_intent(
        _pressed("enter", effect="the dialog commits"), None, "the dialog commits"
    )
    real = engine.verify(keypress_intent, before_big, after_big)
    assert real.outcome == "verified", real


def test_pixel_tier_stated_effect_absent_evidence_degrades_to_uncertain() -> None:
    """Tier-level pin of the escalation band: ScreenshotDiffStrategy itself never
    emits a definitive ``failed`` from absent evidence when an effect is stated —
    the bare-expectation legacy failure is the ONLY definitive negative left, and
    the controller cannot produce a bare expectation for type/keypress (a stated
    effect always rides the intent)."""
    strategy = ScreenshotDiffStrategy()
    before = _observation("white")
    after = _observation("white")
    stated = VerificationIntent(
        kind=VerificationKind.VISUAL_CHANGE.value,
        expected_change=True,
        expected_effect="the dialog commits",
    )
    result = strategy.verify(stated, before, after)
    assert result.outcome == "uncertain", result
    assert result.verified is False


# --- 4. KEYPRESS: deterministic defaults -------------------------------------------------------


def test_keypress_launch_prefix_effect_promotes_to_window_state_before_pixels() -> None:
    """``open …``/``launch …``/``switch to …``/``focus …`` effects keep the
    deterministic ``window_state`` DEFAULT: the verdict comes from window identity,
    never pixels."""
    action = _pressed("win", "r", effect="open Task Manager")
    intent = _build_intent(action, None, "open Task Manager")
    assert intent.kind == VerificationKind.WINDOW_STATE.value, intent
    assert intent.expected_window_title == "Task Manager"

    engine = VerificationEngine()
    before = _observation("white")
    after = _observation("white", active_window="Task Manager")
    result = engine.verify(intent, before, after)
    assert result.outcome == "verified", result
    assert result.verification_method == "window_state", result


def test_keypress_stated_effect_pixel_band_is_the_escalation_and_stays_honest() -> None:
    """A non-launch keypress with a stated effect: kind stays ``visual_change`` with
    the effect recorded — the pixel band then speaks ONLY on real change (verified)
    or ambiguous absence (uncertain), never on absence-as-failure. The intent carries
    NO focus-change flag (click-scoped by the R2 D11 pin)."""
    action = _pressed("enter", effect="the dialog commits")
    intent = _build_intent(action, None, "the dialog commits")
    assert intent.kind == VerificationKind.VISUAL_CHANGE.value, intent
    assert intent.expected_change is True and intent.expected_effect == "the dialog commits"
    assert intent.metadata.get(FOCUS_CHANGE_INTENT_FLAG) is None, intent

    engine = VerificationEngine()
    absent = engine.verify(intent, _observation("white"), _observation("white"))
    assert absent.outcome == "uncertain", absent
    assert absent.verified is False


def test_keypress_without_expectation_stays_honest_on_the_pixel_band() -> None:
    """No stated effect: identical pixels -> uncertain (never failed, never
    verified); a real transition -> verified. Uncertain is never success."""
    action = _pressed("esc")
    intent = _build_intent(action, None, None)
    assert intent.kind == VerificationKind.VISUAL_CHANGE.value, intent
    assert intent.expected_change is None

    engine = VerificationEngine()
    steady = engine.verify(intent, _observation("white"), _observation("white"))
    assert steady.outcome == "uncertain", steady
    assert steady.verified is False

    before_big = Observation(image_base64=_png("white", 320, 240), width=320, height=240)
    after_big = Observation(image_base64=_stroke_png(320, 240), width=320, height=240)
    moved = engine.verify(intent, before_big, after_big)
    assert moved.outcome == "verified", moved


# --- 5. scope discipline: the focus-change flag never widens to type/keypress -----------------


def test_focus_change_flag_stays_click_scoped() -> None:
    """The R2 D11 fault-injection doctrine: deterministic window/element transition
    signals must never verify a non-click effect (an unrelated foreground steal would
    become a false success). Type and keypress intents carry no flag even with a
    stated effect."""
    for action in (
        _typed("C8C3B2", "the hex field shows the value"),
        _pressed("enter", effect="the dialog commits"),
    ):
        intent = _build_intent(action, None, action.expected_effect)
        assert intent.metadata.get(FOCUS_CHANGE_INTENT_FLAG) is None, (action.action, intent)
