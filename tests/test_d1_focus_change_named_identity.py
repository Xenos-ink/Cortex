"""ORVEX-2026-105 named-identity binding tests.

The defect, runtime-proven in the ORVEX-2026-105 investigation.
``FocusChangeStrategy`` returned ``_definitive("verified")`` whenever a focused
element or active window IDENTITY changed, and never once opened
``intent.expected_effect``. Both readers read the FOREGROUND window (``backend.py``:
the UIA reader and the Win32 reader) and ``observe()`` reads the foreground window,
so ANY click that activated a window moved both signals. Clicking a static Tk
``Label`` while claiming *"The Edit Colors dialog opens with a new color swatch"*
came back ``verified`` with the note "focused UI element changed (… -> 'Pane/TkChild/
tk1')" — and no dialog ever opened.

The rule this file pins, in one sentence: a focus/window signal may support
``verified`` ONLY when the identity observed AFTER the action corresponds to a
specific thing the stated effect NAMED. Not because it changed; not because it
changed within one process.

What this file owns, one test per clause:

- a corroborated focus transition still verifies (the capability is preserved);
- an uncorroborated one is ``uncertain`` (the defect is closed);
- same-process is a PRECONDITION, never sufficient;
- process identity / title-only evidence never corroborates on the process alone;
- the binding never reads ``ocr_text`` and never reads pixels;
- the novelty contrast is PER FIELD CLASS, not global (R-8, load-bearing);
- a ``verified`` names its token, its class and its novelty; an ``uncertain`` names
  the closest non-matching candidate (R-9);
- the stop-list is the declared CLOSED one and its growth rule is real;
- the binding costs no extra capture or input (pure, single-pass, in-memory).

(Naming note: this file pins the ORVEX-2026-105 named-identity binding. It has no
connection to the reply-contract image-delivery decision that other files abbreviate
to a similar tag.)
"""

from __future__ import annotations

import asyncio
import base64
import io
from typing import Any

import pytest
from PIL import Image

from computer_use_mcp.models import Observation, TextRegion, WindowInfo
from computer_use_mcp.verification import (
    FOCUS_CHANGE_INTENT_FLAG,
    IDENTITY_ELEMENT_SCAN_LIMIT,
    IDENTITY_STOP_TOKENS,
    FocusChangeStrategy,
    VerificationEngine,
    VerificationIntent,
    VerificationKind,
    _content_tokens,
    _effect_tokens,
    _identity_token_classes,
    _novel_tokens,
    stem,
)

# --- fixtures --------------------------------------------------------------------------------


def _png(image: Image.Image) -> str:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def _white(width: int = 64, height: int = 48) -> Image.Image:
    return Image.new("RGB", (width, height), "white")


def _changed(changed_pixels: int) -> Image.Image:
    image = _white()
    for index in range(changed_pixels):  # a compact, real redraw
        image.putpixel((index % 64, index // 64), (0, 0, 0))
    return image


def _observation(
    image: Image.Image | None = None,
    *,
    title: str | None = "Untitled - Paint",
    window_class: str | None = None,
    process: str | None = "mspaint.exe",
    ui_elements: list[Any] | None = None,
    ocr_text: list[TextRegion] | None = None,
) -> Observation:
    info = (
        WindowInfo(hwnd=1, pid=7, process_name=process, title=title, window_class=window_class)
        if title is not None or process is not None
        else None
    )
    return Observation(
        image_base64=_png(image if image is not None else _white()),
        width=(image if image is not None else _white()).width,
        height=(image if image is not None else _white()).height,
        active_window=title,
        active_window_info=info,
        ui_elements=ui_elements,
        ocr_text=ocr_text,
    )


def _focus_intent(effect: str) -> VerificationIntent:
    """A CLICK/DOUBLE_CLICK intent carrying a stated effect — the strategy's whole scope."""
    return VerificationIntent(
        kind=VerificationKind.VISUAL_CHANGE,
        expected_change=True,
        expected_effect=effect,
        metadata={FOCUS_CHANGE_INTENT_FLAG: True},
    )


def _verify(before: Observation, after: Observation, effect: str) -> Any:
    return FocusChangeStrategy().verify(_focus_intent(effect), before, after)


# --- the defect: an uncorroborated identity change is NOT a verified effect ------------------


def test_a_focus_change_that_names_nothing_in_the_effect_does_not_verify() -> None:
    """THE defect. Clicking a static label moved focus and activated the window —
    both arms fired — while the effect named a dialog that never opened. The change is
    real and is still REPORTED; what it no longer does is claim the effect."""
    before = _observation(
        ui_elements=[
            {"control_type": "Pane", "name": "Chrome_WidgetWin_1/ZCode", "automation_id": "zcode", "focused": True}
        ],
        title="Chrome_WidgetWin_1/ZCode",
        process="python.exe",
    )
    after = _observation(
        ui_elements=[
            {"control_type": "Pane", "name": "TkChild", "automation_id": "tk1", "focused": True}
        ],
        title="orvex-103-named-identity",
        process="python.exe",
    )
    result = _verify(before, after, "The Edit Colors dialog opens with a new color swatch")

    assert result.outcome == "uncertain", result
    assert result.verified is False
    assert result.changed is True  # the transition is observed, only not claimed
    # the note names what was OBSERVED and never the expectation (the arm-(c) doctrine)
    assert "The Edit Colors dialog opens with a new color swatch" not in result.note
    # ...and the evidence is still carried in full
    assert any("focused element" in line for line in result.evidence)
    assert any("active window title" in line for line in result.evidence)
    assert any("corroboration searched effect tokens" in line for line in result.evidence)


def test_a_window_change_into_an_unrelated_window_does_not_verify() -> None:
    """A same-process window swap is still not the stated effect. The effect names a
    delete; what appeared is a confirm prompt."""
    before = _observation(title="Main", process="app.exe")
    after = _observation(title="Confirm Delete?", process="app.exe")
    result = _verify(before, after, "the record is deleted")

    assert result.outcome == "uncertain", result
    assert "the record is deleted" not in result.note


# --- the capability is preserved: a corroborated transition still verifies -------------------


def test_a_corroborated_focus_transition_still_verifies() -> None:
    """The Paint hex-field case, preserved: the focused element's identity changed AND
    the new identity names the thing the effect named."""
    before = _observation(
        ui_elements=[{"control_type": "Edit", "name": "", "automation_id": "", "focused": True}]
    )
    after = _observation(
        ui_elements=[
            {"control_type": "Edit", "name": "Hex color input", "automation_id": "hexInput", "focused": True}
        ],
    )
    result = _verify(before, after, "Hex input focused")

    assert result.outcome == "verified", result
    assert result.verification_method == "focus_change"
    assert "focused UI element changed" in result.note


def test_a_corroborated_window_transition_still_verifies() -> None:
    """The dialog-open signature, preserved: the new window title carries the word the
    effect named, and it was not there before."""
    before = _observation(title="Untitled - Paint", process="mspaint.exe")
    after = _observation(title="Edit Colors", process="mspaint.exe")
    result = _verify(before, after, "Edit colors dialog opens")

    assert result.outcome == "verified", result
    assert "window" in result.note.casefold()


def test_a_corroboration_can_come_from_the_ui_elements_population() -> None:
    """The third field class earns its place: the corroborating token is novel in the
    POPULATION, not in the focused element and not in the window. The binding is a gate
    on correspondence, not a whitelist of two arms."""
    before = _observation(
        ui_elements=[{"control_type": "Pane", "name": "canvas", "automation_id": "", "focused": True}]
    )
    after = _observation(
        ui_elements=[
            {"control_type": "Button", "name": "Shapes", "automation_id": "shapes", "focused": True},
            {"control_type": "Text", "name": "Swatch", "automation_id": "swatch1", "focused": False},
        ]
    )
    result = _verify(before, after, "a new color swatch appears")

    assert result.outcome == "verified", result
    carrying = [line for line in result.evidence if "corroborated by effect token" in line]
    assert carrying and "ui_elements population" in carrying[0], result.evidence


# --- R-5: same-process is a precondition, never a corroboration ----------------------------


def test_same_process_alone_never_raises_verified() -> None:
    """The demoted C1 condition. Same process, focus moved, window unchanged, and the
    effect names a dialog: the precondition is MET and is recorded as a fact, yet the
    verdict is ``uncertain`` because nothing corroborated."""
    before = _observation(
        title="Untitled - Paint",
        process="mspaint.exe",
        ui_elements=[{"control_type": "Button", "name": "Brushes", "automation_id": "b1", "focused": True}],
    )
    after = _observation(
        title="Untitled - Paint",
        process="mspaint.exe",
        ui_elements=[{"control_type": "Button", "name": "Canvas", "automation_id": "b2", "focused": True}],
    )
    result = _verify(before, after, "the Shapes dialog opens")

    assert result.outcome == "uncertain", result
    precondition = [line for line in result.evidence if "same-process precondition" in line]
    assert precondition and "met" in precondition[0], result.evidence
    assert "never sufficient" in precondition[0]


def test_a_cross_process_transition_is_reported_as_not_meeting_the_precondition() -> None:
    """The other direction of the precondition: a genuine foreground steal is recorded
    as NOT meeting it, and is still not sufficient in that direction either."""
    before = _observation(title="Untitled - Paint", process="mspaint.exe")
    after = _observation(title="orvex-103-named-identity", process="python.exe")
    result = _verify(before, after, "the record is deleted")

    assert result.outcome == "uncertain", result
    precondition = [line for line in result.evidence if "same-process precondition" in line]
    assert precondition and "NOT met" in precondition[0], result.evidence


# --- R-3: process identity is evidence-only, never a corroborating token --------------------


def test_process_name_never_corroborates_on_its_own() -> None:
    """A process name identifies the APPLICATION, not an element indicating the effect
    occurred. The effect names the process that took over, the foreground process really
    did change, and the window title and every control name say nothing about it — yet
    the verdict is ``uncertain``, because R-3 makes the process EVIDENCE-ONLY."""
    before = _observation(title="Untitled - Notepad", process="notepad.exe")
    after = _observation(title="Untitled - Notepad", process="calculator.exe")
    result = _verify(before, after, "Calculator takes over the foreground")

    assert result.outcome == "uncertain", result
    assert "calculator" in _effect_tokens("Calculator takes over the foreground")
    assert any("active window process" in line for line in result.evidence)


def test_process_name_is_structurally_absent_from_the_identity_token_classes() -> None:
    """R-3, asserted at the helper rather than through a verdict: no process/exe/pid/hwnd
    token can ever reach the corroboration set, so the class of bug is closed, not just
    the instance that was observed."""
    before = _observation(title="Main", process="app.exe")
    after = _observation(title="Main", process="other.exe")
    assert after.active_window_info is not None
    # ...on BOTH sides, and the process really did change: the exclusion is structural.
    assert after.active_window_info.process_name != before.active_window_info.process_name
    every = set().union(*_identity_token_classes(after).values())
    every |= set().union(*_identity_token_classes(before).values())

    assert "other" not in every  # the new process name
    assert "app" not in every  # ...and the old one ("app" is a stop-word besides)
    assert "exe" not in every and "7" not in every  # the exe suffix and the pid


def test_a_title_only_corroboration_still_needs_novelty() -> None:
    """Title-only evidence is the strongest window-side signal, but it is still gated:
    the named word must be ABSENT before, not merely present after."""
    before = _observation(title="Report - Calculator", process="calc.exe")
    after = _observation(title="Report - Calculator", process="calc.exe")  # identical title
    result = _verify(
        before,
        after,
        "Calculator opens",
    )
    # no identity changed at all, so no signal fires and the tier abstains outright
    assert result.outcome == "uncertain", result

    # ...and with a genuine change, a word that was ALREADY in the title is a constant
    moved_before = _observation(title="Calculator", process="calc.exe")
    moved_after = _observation(title="Calculator - untitled", process="calc.exe")
    stale = _verify(moved_before, moved_after, "Calculator opens")
    assert stale.outcome == "uncertain", stale
    assert any("ALREADY present in the BEFORE" in line for line in stale.evidence)


# --- R-1: the binding never reads OCR and never reads pixels --------------------------------


def test_the_binding_never_reads_ocr_text() -> None:
    """``ocr_text`` is STRUCTURALLY EXCLUDED. Here the OCR regions carry the effect's
    words verbatim, and the deterministic identity says nothing about them: if the
    binding leaned on OCR this would verify."""
    effect = "The Edit Colors dialog opens with a new color swatch"
    ocr = [
        TextRegion(text="The Edit Colors dialog opens with a new color swatch", x=0, y=0, width=10, height=10),
        TextRegion(text="Edit Colors", x=0, y=20, width=10, height=10),
    ]
    before = _observation(
        ui_elements=[{"control_type": "Pane", "name": "TkChild", "automation_id": "tk1", "focused": True}],
        title="orvex-103-named-identity",
        ocr_text=ocr,
    )
    after = _observation(
        ui_elements=[{"control_type": "Pane", "name": "TkLabel", "automation_id": "lbl", "focused": True}],
        title="orvex-103-named-identity-b",
        ocr_text=ocr,
    )
    result = _verify(before, after, effect)

    assert result.outcome == "uncertain", result
    assert _effect_tokens(effect) == frozenset({"color", "edit", "swatch"})


def test_the_binding_never_launder_a_stated_effect_through_pixels() -> None:
    """A real redraw changes the digest and the effect names nothing the new identity
    carries. The identity arms never see pixels at all — no focus/window signal fires
    here — so the verdict comes from the digest arm alone, which on a sub-corroboration
    redraw defers (uncertain) exactly as the caret-blink doctrine requires.

    PORT ADAPTATION (WP3 seam, recorded in the gate notes): the upstream tree's copy of
    this node uses a full-corroboration redraw and relies on the digest arm ABSTAINING
    on a corroborated magnitude — a behavior owned by the verification-accuracy work
    package (the digest-arm verdict change). Until that lands, the stable digest arm
    still verifies a corroborated redraw, so this node pins the same property (no pixel
    magnitude may launder a described effect through the IDENTITY binding; no
    focus/window signal fires on a pixels-only transition) at a redraw strength below
    the corroboration floor. The upstream strengthening is deferred to the digest-arm
    owner together with its own re-pin."""
    before = _observation(_white())
    # 12 strongly-changed pixels: below STRONG_CHANGE_MIN_PIXELS (50) AND below the
    # mean-diff threshold — a real digest change the corroboration floor rejects.
    after = _observation(_changed(12))
    assert before.image_base64 != after.image_base64  # the precondition really holds
    result = _verify(before, after, "the Shapes dialog opens")

    assert result.outcome == "uncertain", result
    assert "the Shapes dialog opens" not in result.note


# --- R-8: the novelty contrast is PER FIELD CLASS, then united ------------------------------


def test_novelty_is_per_class_not_global() -> None:
    """The R-8 case, exactly: the BEFORE ``ui_elements`` POPULATION already contains the
    hex field as an unfocused child, so a single GLOBAL before-contrast would call
    ``hex`` a constant and demote this legitimate verification. The token is novel in
    the FOCUSED class, and the evidence must say so."""
    before = _observation(
        ui_elements=[
            {"control_type": "Pane", "name": "canvas", "automation_id": "", "focused": True},
            {"control_type": "Edit", "name": "Hex color input", "automation_id": "hexInput", "focused": False},
        ]
    )
    after = _observation(
        ui_elements=[
            {"control_type": "Edit", "name": "Hex color input", "automation_id": "hexInput", "focused": True},
            {"control_type": "Pane", "name": "canvas", "automation_id": "", "focused": False},
        ]
    )
    result = _verify(before, after, "Hex input focused")

    assert result.outcome == "verified", result
    corroborating = [line for line in result.evidence if "corroborated by effect token" in line]
    assert corroborating, result.evidence
    assert "focused element" in corroborating[0]
    assert "ABSENT from the BEFORE" in corroborating[0]


def test_the_per_class_union_does_not_weaken_the_focused_element_pin() -> None:
    """R-8's other half: "present but unfocused before, focused after" verifies
    (above), and "ALREADY focused before" does not — the token is not novel in any
    class, so there was no transition to corroborate."""
    element = {"control_type": "Edit", "name": "Hex color input", "automation_id": "hexInput", "focused": True}
    steady = _verify(
        _observation(ui_elements=[element]),
        _observation(ui_elements=[dict(element)]),
        "Hex input focused",
    )
    assert steady.outcome == "uncertain", steady

    moved = _verify(
        _observation(ui_elements=[element]),
        _observation(ui_elements=[{"control_type": "Edit", "name": "Canvas", "automation_id": "c", "focused": True}]),
        "Hex input focused",
    )
    assert moved.outcome == "uncertain", moved


# --- R-9: every verdict names itself ---------------------------------------------------------


def test_a_verified_names_its_token_its_class_and_its_novelty() -> None:
    """A bad match must be inspectable from the response itself, not hidden inside a
    success. This is the property that converts the residual risk from invisible to
    auditable — and it is what lets a real false corroboration be spotted in the field
    and promoted into a stop-list entry with its reproduction test."""
    before = _observation(title="Untitled - Paint")
    after = _observation(title="Edit Colors")
    result = _verify(before, after, "Edit colors dialog opens")

    assert result.outcome == "verified", result
    carrying = [line for line in result.evidence if "corroborated by effect token" in line]
    assert len(carrying) == 1, result.evidence
    line = carrying[0]
    assert "'color'" in line or "'edit'" in line  # the matched token
    assert "active window" in line  # the field class that carried it
    assert "novel" in line and "ABSENT from the BEFORE" in line


def test_an_uncertain_names_the_closest_non_matching_candidate() -> None:
    """The near-miss has to be diagnosable. A stale token (present after AND before) is
    the highest-value near-miss, because it separates "named the wrong thing" from
    "named a thing that was already true"."""
    before = _observation(title="Swatch")
    after = _observation(title="Swatch - untitled")
    result = _verify(before, after, "Swatch opens")

    assert result.outcome == "uncertain", result
    assert any("closest non-matching candidate" in line for line in result.evidence)
    assert any("ALREADY present in the BEFORE" in line for line in result.evidence)


def test_an_uncertain_names_a_lexical_near_miss_when_no_token_is_stale() -> None:
    """The other near-miss shape: nothing in the effect is present at all, but the novel
    identity token is a near miss of an effect token (``colour`` vs ``color``), which is
    one of the ways this rule legitimately fails closed. Diagnosing it needs the focus
    change to have happened — the binding only speaks once a signal fired."""
    before = _observation(
        ui_elements=[{"control_type": "Pane", "name": "host", "automation_id": "h", "focused": True}]
    )
    after = _observation(
        ui_elements=[
            {"control_type": "Button", "name": "shapes", "automation_id": "s", "focused": True},
            {"control_type": "Text", "name": "Colour", "automation_id": "c", "focused": False},
        ]
    )
    result = _verify(before, after, "The color swatch appears")

    assert result.outcome == "uncertain", result
    nearest = [line for line in result.evidence if "closest non-matching candidate" in line]
    assert nearest, result.evidence
    assert "'color'" in nearest[0] and "colour" in nearest[0]


# --- R-6: both degeneracies are uncertain, never verified ------------------------------------


@pytest.mark.parametrize(
    "effect",
    [
        "the dialog opens",  # "dialog" and "opens" are both stop-listed
        "a new popup appears",  # "new", "popup", "appears" are all stop-listed
        "the panel is shown",  # "panel" and "shown" are stop-listed
    ],
)
def test_an_effect_with_no_extractable_token_never_verifies(effect: str) -> None:
    """R-6.2: ``E = {}`` can never satisfy the predicate, so a vague effect abstains
    even when a real identity change happened. Fail-closed, never a free success."""
    before = _observation(
        ui_elements=[{"control_type": "Pane", "name": "host", "automation_id": "h", "focused": True}]
    )
    after = _observation(
        ui_elements=[{"control_type": "Pane", "name": "editor", "automation_id": "e", "focused": True}]
    )
    result = _verify(before, after, effect)

    assert _effect_tokens(effect) == frozenset()
    assert result.outcome == "uncertain", result
    assert result.changed is True  # the identity change is still observed
    assert any("no content token" in line for line in result.evidence)


def test_no_usable_identity_is_fail_closed() -> None:
    """R-6.1: the new identity is None/empty, so the identity token set is empty and the
    corroboration set is too. The existing abstention (no UIA data, no window identity)
    is unchanged."""
    before = _observation(title=None, process=None, ui_elements=[])
    after = _observation(title=None, process=None, ui_elements=[])
    result = _verify(before, after, "Hex input focused")

    assert result.outcome == "uncertain", result
    assert all(
        not tokens for tokens in _identity_token_classes(after).values()
    ), _identity_token_classes(after)


def test_window_class_counts_as_window_side_identity() -> None:
    """R-1's I-4: the Win32 class name is a window-side identity FIELD, and it
    corroborates. Two honest limits are pinned here rather than papered over: the class
    name is not split on CamelCase (an effect has to name the whole token), and a
    class-name change on its own is not a SIGNAL — arm (b) watches the title and the
    process — so the corroboration rides a title change too."""
    before = _observation(title="host", window_class="HostWindow")
    after = _observation(title="host2", window_class="SwatchWindow")
    result = _verify(before, after, "the SwatchWindow class appears")

    assert result.outcome == "verified", result
    assert any(
        "corroborated by effect token 'swatchwindow'" in line and "active window" in line
        for line in result.evidence
    ), result.evidence
    assert "swatchwindow" in _identity_token_classes(after)["active window"]


# --- R-3a: the stop-list is declared, closed, and its growth rule is real -------------------


def test_token_extraction_clauses_are_pinned_separately() -> None:
    """Each R-2 clause, pinned separately so a change to one is a visible diff rather
    than a silent broadening. The stop-list sample is the list's own anti-deletion
    guard: the growth rule only ever ADDS words, so a member going missing is a bug."""
    # R-2.1 casefold / R-2.2 separators / R-2.3 split
    assert _effect_tokens("EDIT-Colors_DIALOG") == _effect_tokens("edit colors dialog")
    # R-2.4 sub-3-character drop
    assert "a1" not in _effect_tokens("goes to a1")
    # R-2.4 purely numeric drop
    assert _effect_tokens("version 1.25") == frozenset({"version"})
    # R-2.4 stop-list drop (a representative sample of each structural group)
    for structural in (
        "dialog", "window", "input", "field", "panel", "button", "menu", "popup",
        "form", "control", "focused", "appears", "opened", "the", "with", "new",
    ):
        assert structural in IDENTITY_STOP_TOKENS, structural
        assert _effect_tokens(f"the {structural} opens") & {structural} == frozenset()
    # ...and a word NOT on the list is content-bearing and may corroborate
    assert "swatch" not in IDENTITY_STOP_TOKENS
    assert "swatch" in _effect_tokens("a new color swatch")
    # R-2.5 minimal stemming, both rules, and their length floors
    assert _effect_tokens("colors") == _effect_tokens("color")
    assert _effect_tokens("deleted") == frozenset({"delet"})
    # R-3a growth rule: the closed list is the module's declared one, and the two words
    # considered-and-rejected are still absent (adding either needs its failing repro).
    assert "toolbar" not in IDENTITY_STOP_TOKENS
    assert "widget" not in IDENTITY_STOP_TOKENS


def test_plurals_of_structural_words_never_corroborate() -> None:
    """F2 (VAL-WP3): the stop-list applies to the STEMMED form as well as the raw form.
    Before this pin a plural of a structural word (``dialogs``, ``windows``, ``panels``,
    ``buttons``, ``fields``) stemmed onto its stop-listed singular and stayed in ``E``,
    so an effect such as "new windows open" could corroborate on a "Windows Security"
    title. The growth rule is honoured: the failing reproduction IS the plural."""
    for plural, singular in (
        ("dialogs", "dialog"),
        ("windows", "window"),
        ("panels", "panel"),
        ("buttons", "button"),
        ("fields", "field"),
    ):
        assert stem(plural) == singular
        assert _content_tokens(plural) == frozenset(), plural

    # the reported repro, end to end: the effect has NO extractable token (its every
    # word is stop-listed raw or after stemming), so nothing can corroborate
    assert _effect_tokens("new windows open") == frozenset()
    before = _observation(title="Main", process="app.exe")
    after = _observation(title="Windows Security", process="app.exe")
    result = _verify(before, after, "new windows open")
    assert result.outcome == "uncertain", result
    assert any("no content token" in line for line in result.evidence)


def test_a_plural_content_noun_still_corroborates_its_singular() -> None:
    """F2's other half: the stemmed-stop rule must not swallow content words. A plural
    content noun stems to its singular, and the singular observed in the AFTER identity
    still corroborates."""
    assert _content_tokens("records") == frozenset({"record"})
    before = _observation(title="Main", process="app.exe")
    after = _observation(title="Records", process="app.exe")
    result = _verify(before, after, "the records table")
    assert result.outcome == "verified", result
    carrying = [line for line in result.evidence if "corroborated by effect token" in line]
    assert carrying and "'record'" in carrying[0], result.evidence


# --- R-2.1: the word split keeps Unicode combining marks (non-ASCII identity) ---------------

#: The same Arabic word ("notepad"), plain and with full tashkeel (combining marks).
ARABIC_PLAIN = "مفكرة"
ARABIC_VOCALIZED = "مُفَكِّرَة"


def test_combining_marks_stay_inside_their_word() -> None:
    """F3 (VAL-WP3), unit level: Unicode combining marks (category ``M*``) are word
    characters. Before the fix a vocalized Arabic word was shattered at every tashkeel
    mark into sub-3-character fragments (all dropped, or one long enough to become a
    spurious token); now the vocalized word is ONE token. The vocalized and plain
    forms stay DISTINCT tokens — the normalizer compares literally, marks included —
    so there is no fragment match in either direction."""
    vocalized = _content_tokens(ARABIC_VOCALIZED)
    plain = _content_tokens(ARABIC_PLAIN)
    assert len(vocalized) == 1, vocalized  # one word, not fragments
    assert vocalized != plain  # marks are part of the literal token
    assert vocalized & plain == frozenset()  # and never a spurious fragment match


def test_an_arabic_title_matches_an_arabic_effect() -> None:
    """F3 (VAL-WP3), behavioral: a legitimate Arabic title match verifies exactly like
    a Latin one — window signal fires, the named word is novel, ``verified``."""
    before = _observation(title="Main", process="app.exe")
    after = _observation(title=f"{ARABIC_PLAIN} - محرر", process="app.exe")
    result = _verify(before, after, ARABIC_PLAIN)
    assert result.outcome == "verified", result
    carrying = [line for line in result.evidence if "corroborated by effect token" in line]
    assert carrying and ARABIC_PLAIN in carrying[0], result.evidence


def test_a_vocalized_title_matches_only_the_vocalized_effect() -> None:
    """F3 (VAL-WP3), behavioral: the tashkeel-bearing title matches the tashkeel-bearing
    effect (verified), while the PLAIN spelling of the same word does NOT (fail closed —
    the marks are part of the literal token, and no fragment ever matches). The
    normalizer supports literal-with-marks matching; cross-form matching is a documented
    non-goal."""
    before = _observation(title="Main", process="app.exe")
    vocalized_title = _observation(title=f"{ARABIC_VOCALIZED} - محرر", process="app.exe")

    matched = _verify(before, vocalized_title, ARABIC_VOCALIZED)
    assert matched.outcome == "verified", matched

    mismatched = _verify(before, vocalized_title, ARABIC_PLAIN)
    assert mismatched.outcome == "uncertain", mismatched
    assert "corroborated by effect token" not in "\n".join(mismatched.evidence)


def test_an_unrelated_arabic_title_does_not_verify() -> None:
    """F3 (VAL-WP3), behavioral: an unrelated Arabic title never verifies an Arabic
    effect — and the English-only stop-list cannot accidentally drop Arabic content
    words (it never contains any), so the effect's word stays searchable and simply
    does not match."""
    before = _observation(title="Main", process="app.exe")
    after = _observation(title="الألوان", process="app.exe")
    result = _verify(before, after, ARABIC_PLAIN)
    assert result.outcome == "uncertain", result
    assert _effect_tokens(ARABIC_PLAIN) != frozenset()  # searchable, just not matched
    assert "corroborated by effect token" not in "\n".join(result.evidence)


# --- documented residual (F6, VAL-WP3): the population carries the dialog's own noun -------


def test_confirm_dialog_whose_surface_repeats_the_noun_is_a_documented_residual() -> None:
    """F6 (VAL-WP3), DOCUMENTED RESIDUAL — by design, and auditable. When the confirm
    dialog's own visible surface (here its title; the same holds for a body control the
    backend lists in ``ui_elements``) repeats the effect's noun, the noun IS novel in
    the AFTER identity and the binding verifies. The design cannot tell "the record was
    deleted" from "a dialog about the record appeared" when the dialog names the record
    — that judgment needs reading comprehension, not token correspondence. What keeps
    this honest is the R-9 evidence line: the verified reply names the matched token and
    its field class, so the residual is visible on every reply that rides on it.
    Recorded for the WP10 VERSIONS compatibility notes."""
    before = _observation(title="Main", process="app.exe")
    after = _observation(title="Delete the record?", process="app.exe")
    result = _verify(before, after, "the record is deleted")
    assert result.outcome == "verified", result
    carrying = [line for line in result.evidence if "corroborated by effect token" in line]
    assert carrying and "'record'" in carrying[0] and "active window" in carrying[0], (
        result.evidence
    )


# --- F1 (VAL-WP3): the WP4a-owned gap, pinned honestly as a strict xfail ---------------------

@pytest.mark.xfail(
    reason="WP4a: pixel tier must abstain on uncorroborated identity change",
    strict=True,
)
def test_a_strong_pixel_change_does_not_verify_an_unrelated_identity_effect() -> None:
    """WP4a-OWNED (F1, VAL-WP3) — the ONE allowed xfail; strict, so it FAILS LOUDLY the
    moment WP4a fixes the pixel tier and must be updated then.

    The identity tier's demotion is TIER-LEVEL only. On a window-activating click that
    ALSO moves pixels above the corroboration floor, this tier abstains (uncertain,
    changed=True) and the chain falls through to :class:`ScreenshotDiffStrategy`, which
    still returns ``verified`` ("Visual state changed.") for ANY stated effect on an
    above-floor pixel change. The desired end state — pinned here — is that the ENGINE
    chain comes back ``uncertain``: a pixel magnitude must no more claim a described
    effect on the chain path than it does inside the identity tier. Until WP4a lands its
    pixel-tier abstention (upstream F1 fix 1.1) this assertion is expected to fail."""
    before = _observation(_white(), title="Main", process="app.exe")
    after = _observation(_changed(150), title="Confirm Delete?", process="app.exe")

    # the tier-level demotion itself already holds unconditionally ...
    tier = _verify(before, after, "the record is deleted")
    assert tier.outcome == "uncertain", tier
    assert tier.changed is True

    # ... but the ENGINE chain still verifies through the pixel tier (the F1 gap)
    engine = VerificationEngine(judge=None)
    chain = engine.verify(_focus_intent("the record is deleted"), before, after)
    assert chain.outcome == "uncertain", chain


def test_the_identity_scan_is_bounded_at_fifty_elements() -> None:
    """R-1's cap, and the same bound :class:`UiControlTextStrategy` uses. A corroborating
    element past the cap must not be reachable — the bound is part of the rule, not an
    implementation detail. The window title changes in both cases so arm (b) fires and
    the VERDICT is decided by the token scan rather than by the absence of a signal."""
    assert IDENTITY_ELEMENT_SCAN_LIMIT == 50
    hidden = {"control_type": "Text", "name": "Swatch", "automation_id": "s", "focused": False}
    filler = {"control_type": "Pane", "name": "filler", "automation_id": "", "focused": False}
    beyond = _observation(
        title="host2", ui_elements=[dict(filler) for _ in range(IDENTITY_ELEMENT_SCAN_LIMIT)] + [dict(hidden)]
    )
    inside = _observation(
        title="host2",
        ui_elements=[dict(filler) for _ in range(IDENTITY_ELEMENT_SCAN_LIMIT - 1)] + [dict(hidden)],
    )
    before = _observation(title="host", ui_elements=[dict(filler)])

    assert "swatch" not in _identity_token_classes(beyond)["ui_elements population"]
    assert "swatch" in _identity_token_classes(inside)["ui_elements population"]
    assert _verify(before, beyond, "a new color swatch appears").outcome == "uncertain"
    assert _verify(before, inside, "a new color swatch appears").outcome == "verified"


# --- cost discipline: the binding reads no pixels and adds no capture -----------------------


def test_the_binding_costs_no_extra_capture_or_input() -> None:
    """The owner's hard cost constraint, COUNTED (F7, VAL-WP3) and asserted structurally.

    Structural: the binding is pure — it reads the two observations it is handed, calls
    no backend, and dispatches nothing; the identity classes are derived from the SAME
    observation objects (the focused element is taken from the population pass, not a
    second scan), so the tier adds no duplicated copy of ``ui_elements``.

    Counted: the same identity-change click through the FULL agent pipeline captures
    and dispatches the SAME number of times with the binding path (stated effect —
    ``can_verify`` true, the tier consulted) as without it (effect-less — the tier never
    consulted). Equal counts pin contract item (e): no extra capture, no extra input."""
    before = _observation(
        ui_elements=[{"control_type": "Edit", "name": "", "automation_id": "", "focused": True}]
    )
    after = _observation(
        ui_elements=[
            {"control_type": "Edit", "name": "Hex color input", "automation_id": "hexInput", "focused": True}
        ]
    )
    before_elements = before.ui_elements
    after_elements = after.ui_elements
    FocusChangeStrategy().verify(_focus_intent("Hex input focused"), before, after)
    assert before.ui_elements is before_elements  # untouched, not rebuilt
    assert after.ui_elements is after_elements

    # The focused element is a member of the population set, derived in one pass.
    classes = _identity_token_classes(after)
    assert classes["focused element"] <= classes["ui_elements population"]
    assert _novel_tokens(_identity_token_classes(before), classes)["hex"] == "focused element"

    # --- the counted half ---------------------------------------------------------------
    from types import SimpleNamespace

    from computer_use_mcp.agent import ComputerUseAgent
    from computer_use_mcp.limits import Limits
    from computer_use_mcp.models import GroundedAction
    from computer_use_mcp.observation import ObservationEngine
    from computer_use_mcp.safety import SafetyPolicy
    from computer_use_mcp.state import StopToken, TaskState
    from test_controller_integration import ScriptedBackend

    window = WindowInfo(hwnd=1, pid=7, process_name="mspaint.exe", title="Untitled - Paint")

    class CountingBackend(ScriptedBackend):
        """Counts captures (observe calls) alongside the engine-level executed list."""

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.captures = 0

        def observe(self) -> Any:
            self.captures += 1
            return super().observe()

    def _counted_run(*, expected_effect: str | None) -> tuple[int, int]:
        backend = CountingBackend(flip=False, active_window=window)
        agent = ComputerUseAgent(
            backend,
            provider=None,
            safety=SafetyPolicy(),
            task=TaskState(),
            stop=StopToken(),
            limits=Limits(min_screenshot_interval_ms=0, max_actions=5, max_task_seconds=60.0).validate(),
        )
        agent.observation = ObservationEngine(backend)
        state = SimpleNamespace(
            dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
            max_steps=5, step_count=0, require_approval=False, max_retries_per_action=1,
        )
        action = GroundedAction(action="click", point={"x": 10, "y": 10}, confidence=1.0)
        asyncio.run(agent.run_single(state, action, approved=True, expected_effect=expected_effect))
        return backend.captures, len(backend.executed)

    with_binding = _counted_run(expected_effect="Hex input focused")
    without_binding = _counted_run(expected_effect=None)
    assert with_binding[1] == without_binding[1] == 1  # exactly one input either way
    assert with_binding[0] == without_binding[0]  # exactly the same capture count
