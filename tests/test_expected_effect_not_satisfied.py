"""AN ``expected_effect`` MAY NOT BE SATISFIED BY "SOME PIXELS CHANGED" (WP4a / E-03).

``agent._build_intent`` reduces the driver's prose ``expected_effect`` to one boolean
(``expected_change=True``) and the string is never compared to anything again.  The
tiers that can return ``verified`` each judge a PROXY — "pixels moved", "a digest
changed", "a UI element reports focus" — so an action whose stated effect provably did
NOT happen (a tooltip instead of the drag, a highlight instead of the move) came back
``ok=true`` with a note that asserted the false effect out loud.

Decision D-1(a) (upstream F1 fix 1.1, ported as WP4a / ORVEX-2026-106 E-03): the
stated effect is authoritative WHEN PRESENT; a host that states no effect keeps the
pixel-proxy behaviour exactly.  These cases pin both halves of that contract plus the
ceiling: a real transition — judged by a tier that holds real evidence about the
effect, not by pixel movement — still verifies.
"""

from __future__ import annotations

import asyncio
import base64
import io
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image, ImageDraw

from computer_use_mcp.agent import ComputerUseAgent
from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.limits import Limits
from computer_use_mcp.models import GroundedAction, Observation, Point
from computer_use_mcp.safety import SafetyPolicy
from computer_use_mcp.state import StopToken, TaskState
from computer_use_mcp.verification import (
    FOCUS_CHANGE_INTENT_FLAG,
    STRONG_CHANGE_MIN_PIXELS,
    FocusChangeStrategy,
    VerificationEngine,
    VerificationIntent,
    VerificationKind,
)

WIDTH = 1280
HEIGHT = 720
#: The effect the driver stated — it NEVER happens in this world.  A 41x41 marker on a
#: 1280x720 frame leaves the mean difference far below the 1.0 threshold but puts
#: ~1700 strongly-changed pixels above the 50 floor, so the pixel tier's ``changed``
#: is True purely through the "some pixels moved" branch.
STATED_EFFECT = "the marker moves from a1 to h8"
HIGHLIGHT = 40


class _RecordingAuditor:
    def emit(self, event_type: str, session_id: str, **kwargs: Any) -> None:
        return None


class StickyWorld(FakeComputerBackend):
    """A world whose INTENT never happens but whose SCREEN always changes.

    Clicking anywhere paints a small selection highlight (the "tooltip").  The
    ``moved`` set — the thing the stated effect talks about — is never mutated, so
    the effect provably does not occur while the pixel proxy is satisfied.
    """

    def __init__(self) -> None:
        super().__init__(width=WIDTH, height=HEIGHT)
        self.moved: set[tuple[int, int]] = set()
        self.marker = (120, 90)

    def _frame(self) -> Image.Image:
        image = Image.new("RGB", (WIDTH, HEIGHT), (28, 28, 32))
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            [self.marker[0], self.marker[1], self.marker[0] + HIGHLIGHT, self.marker[1] + HIGHLIGHT],
            fill=(250, 210, 60),
        )
        return image

    def observe(self, monitor_index: int | None = None) -> Any:  # type: ignore[no-untyped-def]
        observation = super().observe(monitor_index=monitor_index)
        buffer = io.BytesIO()
        self._frame().save(buffer, format="PNG")
        observation.image_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
        return observation

    def execute(self, action: Any, stop: Any = None, **kw: Any) -> str:  # type: ignore[no-untyped-def]
        message = super().execute(action, stop, **kw)
        if action.action.value in {"click", "right_click"} and action.point is not None:
            # the ONLY feedback this world gives — `self.moved` is never touched
            self.marker = (int(action.point.x), int(action.point.y))
        return message


class TransitioningWorld(StickyWorld):
    """The control world: the same click, but the marker REALLY moves."""

    def execute(self, action: Any, stop: Any = None, **kw: Any) -> str:  # type: ignore[no-untyped-def]
        message = super().execute(action, stop, **kw)
        if action.action.value == "click" and action.point is not None:
            self.marker = (int(action.point.x), int(action.point.y))
            self.moved.add(self.marker)
        return message


def _run(backend: FakeComputerBackend, action: GroundedAction, **kwargs: Any) -> Any:
    agent = ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        limits=Limits(
            min_screenshot_interval_ms=1, max_actions=100, max_task_seconds=600.0
        ).validate(),
        auditor=_RecordingAuditor(),
    )
    state = SimpleNamespace(
        dry_run=False,
        stopped=False,
        allowed_windows=[],
        min_confidence=0.0,
        max_steps=100,
        step_count=0,
        require_approval=False,
        max_retries_per_action=1,
    )
    return asyncio.run(agent.run_single(state, action, approved=True, **kwargs))


def _click(x: int = 600, y: int = 300) -> GroundedAction:
    return GroundedAction(action="click", point=Point(x=x, y=y), confidence=1.0)


# --- the defect: a generic visual change must not verify a stated effect ------------------------


def test_click_with_a_stated_effect_is_not_verified_by_the_highlight_alone() -> None:
    """The world never moves the marker; only a 41x41 highlight appears.  The verdict
    must not be ``verified`` and the note must not claim the stated effect."""
    world = StickyWorld()
    outcome = _run(world, _click(), expected_effect=STATED_EFFECT)
    verification = outcome.result.verification

    assert world.moved == set(), "the world never performs the stated effect"
    assert verification.outcome != "verified", verification
    assert verification.verified is False, verification
    assert STATED_EFFECT not in verification.note, verification.note
    assert "moves from a1 to h8" not in verification.note, verification.note


def test_no_stated_effect_keeps_todays_pixel_proxy_behaviour() -> None:
    """D-1(a)'s other half: a host that omits ``expected_effect`` is NOT penalised —
    the pixel proxy still verifies a real visual change exactly as it did before."""
    world = StickyWorld()
    outcome = _run(world, _click())
    verification = outcome.result.verification

    assert verification.outcome == "verified", verification
    assert verification.note == "Visual state changed.", verification.note
    assert outcome.result.ok is True


def test_a_world_that_really_transitions_still_verifies_without_a_stated_effect() -> None:
    """The ceiling of the contract: the pixel proxy is untouched for a bare change
    expectation, so a genuinely changed world still verifies."""
    world = TransitioningWorld()
    outcome = _run(world, _click())
    verification = outcome.result.verification

    assert world.moved, "the control world really moved the marker"
    assert verification.outcome == "verified", verification
    assert outcome.result.ok is True


# --- the tier-level contract (the two branches fix 1.1 touches) ---------------------------------


def _observation(image: Image.Image, ui_elements: list[Any] | None = None) -> Observation:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Observation(
        image_base64=base64.b64encode(buffer.getvalue()).decode("ascii"),
        width=image.width,
        height=image.height,
        ui_elements=ui_elements,
    )


def _white(width: int = 64, height: int = 48) -> Image.Image:
    return Image.new("RGB", (width, height), "white")


def _big_change() -> Image.Image:
    image = _white()
    for index in range(STRONG_CHANGE_MIN_PIXELS * 3):  # a real redraw, well above the floor
        image.putpixel((index % 64, index // 64), (0, 0, 0))
    return image


def _intent(effect: str | None, *, focus_flag: bool = False) -> VerificationIntent:
    metadata = {FOCUS_CHANGE_INTENT_FLAG: True} if focus_flag else {}
    return VerificationIntent(
        kind=VerificationKind.VISUAL_CHANGE.value,
        expected_change=True if effect else None,
        expected_effect=effect,
        metadata=metadata,
    )


def test_pixel_tier_abstains_for_a_stated_effect_however_big_the_change() -> None:
    """``ScreenshotDiffStrategy``: a stated effect plus a genuinely large pixel change
    is still only a proxy — the tier abstains (``uncertain``), it never names the
    effect.  The same change with NO stated effect keeps verifying."""
    engine = VerificationEngine()
    before = _observation(_white())
    after = _observation(_big_change())

    stated = engine.verify(_intent("the panel expands"), before, after)
    assert stated.outcome == "uncertain", stated
    assert stated.verified is False, stated
    assert "the panel expands" not in stated.note, stated.note

    bare = engine.verify(_intent(None), before, after)
    assert bare.outcome == "verified", bare


def test_focus_tier_digest_signal_abstains_and_names_the_observation_not_the_effect() -> None:
    """``FocusChangeStrategy`` signal (c) — a digest change corroborated by pixel
    magnitude — is a pixel measurement, not the stated effect."""
    intent = _intent("Edit colors dialog opens", focus_flag=True)
    result = FocusChangeStrategy().verify(
        intent, _observation(_white()), _observation(_big_change())
    )
    assert result.outcome == "uncertain", result
    assert result.verified is False, result
    assert "Edit colors dialog opens" not in result.note, result.note
    assert "Expected focus-type change observed" not in result.note, result.note


def test_focus_tier_deterministic_signals_still_verify_a_stated_effect() -> None:
    """The fix is narrow on purpose: signal (a) — the focused UI ELEMENT's identity
    changed — is evidence about the effect's substrate, not about pixels, and keeps
    verifying (unchanged, and the reason ``on_uncertain`` throughput stays usable)."""
    before = _observation(
        _white(),
        ui_elements=[{"control_type": "Edit", "name": "", "automation_id": "", "focused": True}],
    )
    after = _observation(
        _white(),
        ui_elements=[
            {
                "control_type": "Edit",
                "name": "Hex color input",
                "automation_id": "hexInput",
                "focused": True,
            }
        ],
    )
    result = FocusChangeStrategy().verify(
        _intent("Hex input focused", focus_flag=True), before, after
    )
    assert result.outcome == "verified", result
    assert "focused UI element changed" in result.note, result.note


def test_plain_change_expectation_with_no_described_effect_still_verifies() -> None:
    """A bare ``expected_change=True`` (observe-style: the change IS the claim) is not
    an ``expected_effect`` statement and keeps its historical ``verified``."""
    before = _observation(_white())
    after = _observation(_big_change())
    result = VerificationEngine().verify(_intent(""), before, after)
    assert result.outcome == "verified", result


@pytest.mark.parametrize("effect", ["", "   "])
def test_blank_effect_text_is_not_a_stated_effect(effect: str) -> None:
    """``_build_intent`` normalizes a blank ``expected_effect`` to ``None``
    (``(effect or "").strip() or None``), so a host that sends whitespace is treated
    as having stated nothing — the bare-change path is taken and the pixel proxy is
    NOT penalised.  A degrade keyed on the effect's truthiness can never swallow it."""
    world = StickyWorld()
    outcome = _run(world, _click(), expected_effect=effect)
    verification = outcome.result.verification
    assert verification.outcome == "verified", verification
    assert verification.note == "Visual state changed.", verification.note
