"""C8-prose / C10 / C11 / A4 — repairs 5.2, 5.3, 5.4, 5.5, 5.9-φ: the taught contract
equals the enforced one. Ported to the stable 0.7.6 tree (WP9).

Ported from the dev copy's ``test_teaching_text_matches_enforcement.py`` and ADAPTED:

* imports follow the layered restructure (``core`` / ``desktop`` / ``pipeline`` /
  ``planning`` / ``server`` packages);
* the README assertions are DROPPED: the stable README still describes v0.7.5 and its
  update is WP10's scope — the doc drift this class of test guards in the README
  (README.md:423 still says "the server never launches by default"; the scroll sign
  convention is absent from the stable tool teaching text) is RECORDED in
  wp9-notes.md as findings, not silently ignored;
* the ``ACTION_VOCABULARY`` / execute-docstring pins are re-targeted at the wording
  the stable tree actually ships (the scroll entry now carries the ``-20..20`` band;
  the ``final_wait_ms`` clause slicing is replaced by a whole-document negative pin);
* the SKILL-side pins now read ``skills/cortex-skill/`` (core + references), the
  shipped location.

The historical drifts this file exists to prevent (all shipped once in the dev line):

* 5.2 — prose said "the server never launches by default" while the enforced default
  is ``launch="server"`` and a NO_INSTANCE DOES spawn with no allowlist configured.
* 5.3 — ``scroll``'s sign convention and its cursor dependence were written down only
  in an internal engine docstring, nowhere the model reads.
* 5.4 — the wait action taught ``delta 0..20 seconds`` against
  ``WAIT_MAX_SECONDS = 10.0``, and the reply carried no timing field at all.
* 5.5 — the corroboration note printed ``mean 0.000383 >= 1``: a 0..1 mean compared
  against a 0..255 threshold, a comparison that cannot hold for any real screen.
* 5.9-φ — the plan surface offered ``final_wait_ms`` as an ALTERNATIVE to the
  unconditional per-wait ``delta``; following the taught text built plans the
  validator refuses.
"""

from __future__ import annotations

import base64
import io
import inspect
import re
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageChops

from computer_use_mcp.desktop.fake_backend import FakeComputerBackend
from computer_use_mcp.desktop.interference import _default_launch_policy
from computer_use_mcp.desktop.motion import WAIT_MAX_SECONDS
from computer_use_mcp.core.models import ActionType, GroundedAction, WindowInfo
from computer_use_mcp.pipeline.verification import (
    DEFAULT_DIFF_THRESHOLD,
    FOCUS_CHANGE_INTENT_FLAG,
    STRONG_CHANGE_MIN_PIXELS,
    FocusChangeStrategy,
    VerificationIntent,
    VerificationKind,
)
from computer_use_mcp.planning.plan import (
    PLAN_EXPLICIT_PLAN_KEYS,
    coerce_tolerant_plan,
    plan_explicitness_message,
)
from computer_use_mcp.server.schemas import ACTION_VOCABULARY

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "cortex-skill"
REFERENCE_NAMES = ("plan-protocol.md", "reply-contract.md", "glossary.md", "examples.md")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _skill_text() -> str:
    return re.sub(
        r"\s+",
        " ",
        "\n".join(
            [_read(SKILL_DIR / "SKILL.md")]
            + [_read(SKILL_DIR / "references" / n) for n in REFERENCE_NAMES]
        ),
    )


# --- 5.2 · the taught launch default equals the enforced one ----------------------------------------


def test_the_taught_launch_default_is_the_enforced_default() -> None:
    """The skill must state ``server``, the value ``_default_launch_policy`` returns."""
    enforced = _default_launch_policy()
    assert enforced == "server"

    skill = _skill_text()
    # The FALSE statement is gone from the shipped manual.
    assert "the server never launches by default" not in skill
    assert "never launches by default" not in skill
    # And the enforced default, its allowlist bound, and the restore are all taught.
    assert 'launch="server"' in skill
    assert "no process allowlist is configured" in skill
    assert "any resolvable target may spawn" in skill
    assert "CORTEX_ATTACH_OR_LAUNCH=driver" in skill


def test_the_skill_states_the_restore_knob_and_the_never_launch_policy() -> None:
    skill = _skill_text()
    assert 'launch="driver"' in skill
    assert "restores never-launch" in skill


def test_the_env_restore_knob_is_the_exact_enforced_spelling() -> None:
    """Replaces the dev's never-failing 'driverr' loop (VAL F12): the knob must be
    spelled exactly, and the parser must accept ONLY that exact spelling."""
    skill = _skill_text()
    assert "CORTEX_ATTACH_OR_LAUNCH=driver" in skill
    source = _read(
        REPO_ROOT / "src" / "computer_use_mcp" / "desktop" / "interference.py"
    )
    assert 'casefold() == "driver"' in source, (
        "the parser stopped accepting only the exact spelling; re-check the skill text"
    )


# --- 5.3 · the scroll sign and its cursor dependence are taught ----------------------------------------


def test_the_vocabulary_states_the_enforced_delta_band() -> None:
    assert "scroll (delta -20..20)" in ACTION_VOCABULARY


def test_the_skill_states_both_scroll_facts() -> None:
    """The sign convention and the cursor dependence: the skill is where the model
    reads them (the stable tool teaching text omits them — recorded as a WP10
    finding; this pin keeps the SHIPPED manual honest either way)."""
    skill = _skill_text()
    assert "POSITIVE = UP" in skill
    assert "CURRENT CURSOR POSITION" in skill
    assert "`move` the pointer to the region first" in skill


# --- 5.4 · the taught wait bound equals the enforced one, and the effective duration is reported ----------


def test_the_vocabulary_teaches_the_enforced_wait_bound() -> None:
    assert WAIT_MAX_SECONDS == 10.0
    assert "this flat action is capped at 10 s and reports" in ACTION_VOCABULARY
    assert "waited_seconds" in ACTION_VOCABULARY
    assert "wait (delta 0..20 seconds)" not in ACTION_VOCABULARY
    # The two surfaces are different products and the text must say so.
    assert "computer_wait" in ACTION_VOCABULARY


def _run_wait(delta: float, monkeypatch: pytest.MonkeyPatch) -> tuple[str, list[float]]:
    """Run the REAL wait dispatch and report what it actually slept."""
    import computer_use_mcp.desktop.backend as backend_module

    world = FakeComputerBackend()
    action = GroundedAction(action=ActionType.WAIT, delta=delta, confidence=1.0)
    slept: list[float] = []
    monkeypatch.setattr(
        backend_module, "_sleep_for_wait_action", lambda total, stop: slept.append(total)
    )
    monkeypatch.setattr(backend_module.time, "sleep", lambda seconds: slept.append(seconds))
    return world.execute(action), slept


def test_an_over_cap_wait_sleeps_the_cap_and_reports_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """``delta=15`` slept ~10 s pre-fix with NOTHING in the reply saying so."""
    message, slept = _run_wait(15.0, monkeypatch)

    assert slept == [WAIT_MAX_SECONDS]
    assert "waited_seconds=10" in message


def test_an_in_range_wait_reports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The field is emitted ONLY when the clamp actually bites."""
    message, slept = _run_wait(3.0, monkeypatch)

    assert slept == [3.0]
    assert message == "Simulated wait."
    assert "waited_seconds" not in message


def test_the_exact_cap_reports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``delta == WAIT_MAX_SECONDS`` is inside the taught bound: nothing was clamped."""
    message, slept = _run_wait(WAIT_MAX_SECONDS, monkeypatch)

    assert slept == [WAIT_MAX_SECONDS]
    assert "waited_seconds" not in message


def test_a_negative_wait_is_clamped_to_zero_and_reports_it(monkeypatch: pytest.MonkeyPatch) -> None:
    message, slept = _run_wait(-5.0, monkeypatch)

    assert slept == [0.0]
    assert "waited_seconds=0" in message


def test_the_marker_is_named_once_and_uses_the_established_annotation_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The effective duration rides the SAME ``key=value`` suffix channel the restore
    and the launch statuses already use, so one parser reads them all."""
    from computer_use_mcp.desktop.backend import (
        CLIPBOARD_RESTORE_DESTROYED,
        CLIPBOARD_RESTORE_FAILED,
    )
    from computer_use_mcp.desktop.motion import WAIT_CLAMPED_MARKER

    assert WAIT_CLAMPED_MARKER == "waited_seconds"
    assert "=" not in WAIT_CLAMPED_MARKER  # the "=" is added at composition time
    assert "waited_seconds=" in _run_wait(15.0, monkeypatch)[0]
    # The two clipboard events deliberately SHARE the ``clipboard_restore`` name and
    # differ by VALUE (same annotation shape, three distinct facts); the wait marker is
    # its own name.  A driver can therefore parse one ``key=value`` suffix chain.
    assert CLIPBOARD_RESTORE_DESTROYED.startswith("clipboard_restore=")
    assert CLIPBOARD_RESTORE_FAILED.startswith("clipboard_restore=")
    assert CLIPBOARD_RESTORE_DESTROYED != CLIPBOARD_RESTORE_FAILED
    assert not WAIT_CLAMPED_MARKER.startswith("clipboard_restore")


def test_the_skill_agrees_on_the_wait_bound() -> None:
    skill = _skill_text()
    assert "capped at 10 seconds" in skill
    assert "delta 0..20" not in skill


# --- 5.5 · the corroboration note is self-consistent --------------------------------------------------


WIDTH, HEIGHT = 1280, 720
EFFECT = "the knight moves to f3"


def _window() -> WindowInfo:
    return WindowInfo(
        hwnd=1, pid=2, process_name="app.exe", exe_path="C:\\app.exe",
        title="Board", bounds=(0, 0, WIDTH, HEIGHT),
    )


def _png(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _observation(image: Image.Image) -> Any:
    from computer_use_mcp.core.models import Observation

    return Observation(
        image_base64=_png(image),
        width=image.width,
        height=image.height,
        active_window="Board",
        active_window_info=_window(),
        ui_elements=[{"focused": True, "name": "board", "control_type": "Pane"}],
    )


def _intent(threshold: float = DEFAULT_DIFF_THRESHOLD) -> VerificationIntent:
    return VerificationIntent(
        kind=VerificationKind.VISUAL_CHANGE,
        expected_change=True,
        expected_effect=EFFECT,
        diff_threshold=threshold,
        metadata={FOCUS_CHANGE_INTENT_FLAG: True},
    )


def _focus_note(before: Image.Image, after: Image.Image, threshold: float = 1.0) -> Any:
    return FocusChangeStrategy().verify(
        _intent(threshold), _observation(before), _observation(after)
    )


def _numbers(text: str) -> list[float]:
    return [float(m) for m in re.findall(r"(-?\d+\.?\d*(?:[eE][-+]?\d+)?)", text)]


def test_the_printed_comparison_is_true_in_its_own_units() -> None:
    """The claim's literal case: ``mean 0.000383 >= 1`` can never hold."""
    base = Image.new("RGB", (WIDTH, HEIGHT), (20, 20, 20))
    tooltip = base.copy()
    tooltip.paste((220, 220, 220), (100, 100, 130, 115))  # 30x15 = 450 px, delta 200

    result = _focus_note(base, tooltip)

    assert "mean 0.000383 >= 1" not in result.note
    # Every `a >= b` / `a < b` the note prints is a TRUE comparison.
    for left, op, right in re.findall(r"([\d.]+)\s*(>=|<)\s*([\d.eE+-]+)", result.note):
        if op == ">=":
            assert float(left) >= float(right), result.note
        else:
            assert float(left) < float(right), result.note


def test_only_the_arm_that_fired_is_printed() -> None:
    base = Image.new("RGB", (WIDTH, HEIGHT), (20, 20, 20))
    tooltip = base.copy()
    tooltip.paste((220, 220, 220), (100, 100, 130, 115))

    note = _focus_note(base, tooltip).note

    # This case entered through the STRONG-PIXEL arm: the mean arm is not true, so the
    # note must not print it as if it were.
    assert "strongly-changed pixels" in note
    assert ">=" not in note or "mean" not in note.split("strongly-changed pixels")[0]


def test_the_full_frame_inversion_still_names_the_mean_arm() -> None:
    """The one input for which the mean arm IS true — it must still be reported, on the
    threshold's own scale."""
    black = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    inverted = ImageChops.invert(black)  # every channel delta 255 -> raw mean 255.0

    note = _focus_note(black, inverted).note

    assert "mean 255/255 >= threshold 1/255" in note


def test_the_abstention_note_is_also_self_consistent() -> None:
    """The companion ``<`` note blamed a mean for a decision the mean could not make."""
    base = Image.new("RGB", (WIDTH, HEIGHT), (20, 20, 20))
    faint = base.copy()
    faint.paste((25, 25, 25), (100, 100, 120, 110))  # 20x10 = 200 px, delta 5

    result = _focus_note(base, faint)

    assert result.outcome == "uncertain"
    for left, op, right in re.findall(r"([\d.]+)\s*(>=|<)\s*([\d.eE+-]+)", result.note):
        if op == ">=":
            assert float(left) >= float(right), result.note
        else:
            assert float(left) < float(right), result.note


def test_the_note_reports_the_mean_on_the_threshold_scale() -> None:
    """``threshold`` is a 0..255 value; the printed mean must be on the same scale."""
    base = Image.new("RGB", (WIDTH, HEIGHT), (20, 20, 20))
    tooltip = base.copy()
    tooltip.paste((220, 220, 220), (100, 100, 130, 115))

    result = _focus_note(base, tooltip)

    # This case entered through the STRONG-PIXEL arm (the mean arm is false), so the
    # note must NOT print a mean comparison at all.  The default threshold lives on the
    # 0..255 scale, and the 0..1 magnitude is converted for the evidence line instead.
    assert "mean" not in result.note
    assert "/255" in " ".join(result.evidence)


def test_the_magnitude_itself_is_unchanged() -> None:
    """SCOPE: the fix is the NOTE STRINGS.  The behaviour half belongs with 1.1."""
    base = Image.new("RGB", (WIDTH, HEIGHT), (20, 20, 20))
    caret = base.copy()
    caret.paste((80, 80, 80), (100, 100, 101, 101))  # a real 1 px, delta-60 caret blink

    magnitude = FocusChangeStrategy._digest_change_magnitude(
        _observation(base), _observation(caret)
    )
    assert magnitude is not None
    assert 0.0 <= magnitude[0] <= 1.0  # STILL the 0..1 normalised form


def test_a_zero_threshold_cannot_launder_a_one_pixel_caret() -> None:
    """The behavioural half the plan flagged: at ``diff_threshold=0.0`` the mean arm
    always fires.  This pin records the CURRENT behaviour, so a future fix to it is a
    deliberate change and not an accident."""
    base = Image.new("RGB", (WIDTH, HEIGHT), (20, 20, 20))
    caret = base.copy()
    caret.paste((80, 80, 80), (100, 100, 101, 101))

    shipped = _focus_note(base, caret, threshold=1.0)
    zeroed = _focus_note(base, caret, threshold=0.0)

    assert shipped.outcome == "uncertain"
    assert zeroed.outcome == "uncertain"  # 1.1 already removed the false `verified`
    assert STRONG_CHANGE_MIN_PIXELS > 0


def test_the_confidence_and_evidence_are_unchanged() -> None:
    base = Image.new("RGB", (WIDTH, HEIGHT), (20, 20, 20))
    tooltip = base.copy()
    tooltip.paste((220, 220, 220), (100, 100, 130, 115))

    result = _focus_note(base, tooltip)

    assert result.outcome == "uncertain"
    assert result.confidence == 0.4  # unchanged by the note fix
    assert len(result.evidence) == 3  # unchanged by the note fix
    assert _numbers(result.evidence[1])  # the magnitude line is still machine-readable


def test_no_note_anywhere_compares_a_0_1_mean_to_a_0_255_threshold() -> None:
    """A module-wide sweep: no verification note may print an unsatisfiable comparison."""
    from computer_use_mcp.pipeline import verification as verification_module

    source = inspect.getsource(verification_module)
    # The comparison must never mix a normalised magnitude with a raw threshold.
    assert "magnitude[0]:.6f} >= {threshold" not in source
    assert "magnitude[0]:.6f} < {threshold" not in source


# --- 5.9-φ · the taught wait rule is the ENFORCED wait rule -----------------------------------------
#
# The same class of drift as 5.2/5.3/5.4, in the plan surface: the plan teaching text
# once presented ``final_wait_ms`` as an ALTERNATIVE to the per-wait ``delta``, but the
# explicitness gate requires the delta UNCONDITIONALLY.  The behaviour pins run the
# REAL validator; the prose pins keep the skill (and the teach-in message) from
# drifting back into a disjunction.


def _wait_plan(*, final_wait_ms: int | None, delta: int | None) -> dict[str, Any]:
    """A plan whose single action is an in-plan ``wait``, with or without a ``delta``."""
    action: dict[str, Any] = {"action": "wait"}
    if delta is not None:
        action["delta"] = delta
    plan: dict[str, Any] = {
        "points": [
            {
                "id": "hold",
                "x": 10,
                "y": 10,
                "actions": [action],
                # rule 6 also applies: a point carrying an action says what it verifies,
                # so it is stated here to keep the DELTA the only variable under test.
                "arrival_condition": "the wait elapsed",
            }
        ],
        "transition_duration_ms": 0,
        "point_timeout_ms": 5_000,
        "settle_ms": 0,
        "on_failure": "stop",
        "on_uncertain": "continue",
    }
    if final_wait_ms is not None:
        plan["final_wait_ms"] = final_wait_ms
    return plan


def test_the_enforced_rule_is_an_unconditional_delta_not_a_plan_level_alternative() -> None:
    """BEHAVIOUR first: this is what the wire boundary actually does.

    Stating ``final_wait_ms`` does NOT satisfy the in-plan wait rule; the delta is
    required whether or not the plan carries a trailing wait.
    """
    with pytest.raises(ValueError, match=r"actions\[0\]\.delta"):
        coerce_tolerant_plan(_wait_plan(final_wait_ms=1_200, delta=None))

    with pytest.raises(ValueError, match=r"actions\[0\]\.delta"):
        coerce_tolerant_plan(_wait_plan(final_wait_ms=None, delta=None))

    plan = coerce_tolerant_plan(_wait_plan(final_wait_ms=1_200, delta=3))
    assert plan.final_wait_ms == 1_200


def test_final_wait_ms_is_not_one_of_the_plan_level_keys_that_cover_a_point() -> None:
    """``PLAN_EXPLICIT_PLAN_KEYS`` is the taught "state it here and every point is
    covered" list, so a key that is NOT in it cannot be an alternative statement."""
    assert "final_wait_ms" not in PLAN_EXPLICIT_PLAN_KEYS
    # ... and it is genuinely OPTIONAL, not a requirement: a plan without it is fine.
    assert coerce_tolerant_plan(_wait_plan(final_wait_ms=None, delta=0))


def test_the_plan_teach_in_message_does_not_offer_final_wait_ms_as_an_alternative() -> None:
    """The rejection text is the same contract, reached one turn later — it must match."""
    message = plan_explicitness_message(["points[0].actions[0].delta"])

    assert "final_wait_ms on the plan, or an explicit delta" not in message
    assert "an explicit delta on every wait action inside a point" in message
    assert "not a substitute for it" in message


def test_the_skill_the_vocabulary_and_the_teach_in_agree_on_the_wait_rule() -> None:
    """One rule, three copies: the skill says "not a substitute", and neither the
    tool vocabulary nor the teach-in message may say anything weaker."""
    skill = _skill_text()

    assert "not a substitute" in skill
    for text in (ACTION_VOCABULARY, plan_explicitness_message(["x"]), skill):
        assert "final_wait_ms on the plan, or an explicit" not in text
