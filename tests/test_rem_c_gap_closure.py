"""REM-C gap-closure tests (Wave D / Phase 8, ORVEX-CORTEX-055; V-2 report §6 F1-F4).

Pins the four confirmed Red-Team V-2 findings on the REM-A+REM-B merged tree:

- F1 (C11, MEDIUM): FocusChangeStrategy signal (c) — a bare observation-digest
  change (a 1-pixel caret blink / clock tick / foreign toast, identical focus and
  window identity) must NOT verify a stated click effect. The digest change is
  evidence only: it may verify ONLY when corroborated by real changed-pixel
  magnitude >= STRONG_CHANGE_MIN_PIXELS (the floor the pixel tier already uses to
  exclude caret/clock flicker); uncorroborated it defers (uncertain) so the
  screenshot-diff tier decides. The logged L624 Paint scenario (click focused the
  hex field -> a REAL focused-element change, signal (a)) stays verified.
- F2 (C11.b, MEDIUM-low): signal (a) fallback — when NO element carries a focus
  marker on either side, a UIA re-enumeration ORDER change of unfocused controls
  must NOT report "focused element changed". Absent any focus claim the signal
  abstains (uncertain), never verifies.
- F3 (B7.b, MEDIUM): backend._launch_process spawned the target needle via
  ``subprocess.Popen([needle], shell=True)`` — with no allowlist session a
  quote-breakout needle (``x" & victim.bat``) reaches cmd.exe and executes a
  smuggled second command. After the fix: no shell, and the needle must validate
  against ^[A-Za-z0-9._ -]+$ BEFORE spawning; anything else is a typed rejection
  with NOTHING spawned. Valid names (mspaint.exe) still launch.
- F4 (LOW, docstring only): interference.py must not claim "only the EXACT string
  ``driver``" — parsing is case-insensitive and whitespace-trimmed.

Written RED first (pre-fix failures confirmed), then greened by the REM-C fixes.
"""

from __future__ import annotations

import base64
import io
import re
import shutil
import subprocess
from typing import Any
from unittest.mock import patch

import pytest
from PIL import Image

from computer_use_mcp.models import Observation, WindowInfo
from computer_use_mcp.verification import (
    FOCUS_CHANGE_INTENT_FLAG,
    STRONG_CHANGE_MIN_PIXELS,
    FocusChangeStrategy,
    ScreenshotDiffStrategy,
    VerificationEngine,
    VerificationIntent,
    VerificationKind,
)


# --- shared fixtures -------------------------------------------------------------------------


def _png(image: Image.Image) -> str:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def _observation(
    image: Image.Image,
    *,
    window: WindowInfo | None = None,
    ui_elements: list[Any] | None = None,
) -> Observation:
    if window is None:
        window = WindowInfo(hwnd=1, pid=7, process_name="mspaint.exe", title="Untitled - Paint")
    return Observation(
        image_base64=_png(image),
        width=image.width,
        height=image.height,
        active_window=window.title,
        active_window_info=window,
        ui_elements=ui_elements,
    )


def _white(width: int = 64, height: int = 48) -> Image.Image:
    return Image.new("RGB", (width, height), "white")


def _focused_intent(effect: str = "Hex input focused") -> VerificationIntent:
    """A CLICK intent carrying a stated expected effect — the FocusChangeStrategy scope."""
    return VerificationIntent(
        kind=VerificationKind.VISUAL_CHANGE.value,
        expected_change=True,
        expected_effect=effect,
        metadata={FOCUS_CHANGE_INTENT_FLAG: True},
    )


def _unflagged_intent(effect: str) -> VerificationIntent:
    """The same transition judged by the plain pixel tier (no focus flag)."""
    return VerificationIntent(
        kind=VerificationKind.VISUAL_CHANGE.value,
        expected_change=True,
        expected_effect=effect,
    )


def _elements(names: list[str], *, focused: bool = False) -> list[dict[str, Any]]:
    return [
        {"control_type": "Pane", "name": name, "automation_id": "", "focused": focused}
        for name in names
    ]


# --- F1: digest-change signal (c) needs pixel-magnitude corroboration --------------------------


def test_f1_caret_blink_digest_change_does_not_verify_click_effect() -> None:
    """F1 core pin: identical focus + identical window identity + a ONE-pixel change
    (caret blink / clock tick) between the captures. The flagged click intent must
    NOT come back ``verified`` from the deterministic tier; the bare digest change is
    sub-threshold flicker the pixel tier deliberately distrusts, so this tier must
    defer (uncertain). W-1 (057) contract update: the flagged-intent chain now
    degrades to ``uncertain`` (pixels cannot observe a focus transition) instead of
    the old definitive false failure — the core pin (never verified on caret-blink
    evidence) is unchanged. RC-D11 (058) update: a stated effect on ANY intent
    (flagged or not) now degrades the sub-threshold diff to ``uncertain`` — absent
    pixels are not proof of absence; uncertain is never success, so the core pin
    (never verified on caret-blink evidence) is unchanged."""
    before = _observation(_white(), ui_elements=_elements(["canvas"], focused=True))
    after_image = _white()
    after_image.putpixel((32, 24), (0, 0, 0))  # a single dark pixel — a caret
    after = _observation(
        after_image, ui_elements=_elements(["canvas"], focused=True)  # SAME focus
    )
    assert before.image_base64 != after.image_base64  # digest DID change (precondition)
    # The pixel tier itself refuses the caret blink as proof of the stated effect
    # for an UNFLAGGED intent (RC-D11: uncertain, never verified, never "failed"
    # from absent evidence)...
    assert (
        ScreenshotDiffStrategy().verify(_unflagged_intent("Hex input focused"), before, after).outcome
        == "uncertain"
    )
    strategy = FocusChangeStrategy()
    result = strategy.verify(_focused_intent(), before, after)
    assert result.outcome != "verified", (
        f"a 1-pixel caret blink verified the click effect: {result.outcome} ({result.note})"
    )
    # Engine-level: the first-definitive-wins chain must not be hijacked either —
    # and under the W-1 contract a flagged focus expectation degrades, not fails.
    engine = VerificationEngine(judge=None)
    chain = engine.verify(_focused_intent(), before, after)
    assert chain.outcome == "uncertain", (
        f"engine misread a caret-blink-only transition: {chain.outcome} "
        f"({chain.verification_method})"
    )


def test_f1_subthreshold_toast_digest_change_does_not_verify_click_effect() -> None:
    """F1 second shape: a toast from ANOTHER app changes 42 pixels — below the
    strong-change floor (STRONG_CHANGE_MIN_PIXELS = 50) but enough to alter the
    digest. Same rule as the caret pin: the digest change alone may not verify a
    click effect."""
    before = _observation(_white(), ui_elements=_elements(["desktop"], focused=True))
    after_image = _white()
    for x in range(10, 17):  # 7x6 = 42 strongly-changed pixels < STRONG_CHANGE_MIN_PIXELS
        for y in range(10, 16):
            after_image.putpixel((x, y), (0, 0, 0))  # a small dark toast patch
    after = _observation(after_image, ui_elements=_elements(["desktop"], focused=True))
    result = FocusChangeStrategy().verify(
        _focused_intent("Edit colors dialog opens"), before, after
    )
    assert result.outcome != "verified", (
        f"a 42-pixel foreign toast verified the click effect: {result.outcome} ({result.note})"
    )


def test_f1_real_focus_change_still_verifies_with_identical_pixels() -> None:
    """F1 preservation pin (the logged L624 Paint scenario): the click focused the
    hex field — a REAL focused-element change (signal (a)) with pixel-identical
    captures. The deterministic tier must keep verifying it (corroboration is only
    required for the digest-only signal, never for the deterministic UIA/window
    signals)."""
    before = _observation(
        _white(),
        ui_elements=[{"control_type": "Edit", "name": "", "automation_id": "", "focused": True}],
    )
    after = _observation(
        _white(),  # SAME pixels — the pixel tier alone would false-fail (logged L624)
        ui_elements=[
            {
                "control_type": "Edit",
                "name": "Hex color input",
                "automation_id": "hexInput",
                "focused": True,
            }
        ],
    )
    result = FocusChangeStrategy().verify(_focused_intent("Hex input focused"), before, after)
    assert result.outcome == "verified"
    assert result.verification_method == "focus_change"
    engine = VerificationEngine(judge=None)
    assert engine.verify(_focused_intent("Hex input focused"), before, after).outcome == "verified"


def test_f1_digest_change_with_strong_pixel_magnitude_verifies() -> None:
    """F1 positive pin: the digest change IS corroborated by real changed-pixel
    magnitude (>= STRONG_CHANGE_MIN_PIXELS strongly-changed pixels) — a real
    focus-invisible transition (e.g. the dialog opened after the capture while the
    focus/window fields are unchanged) may verify through the digest signal again."""
    before = _observation(_white(), ui_elements=_elements(["canvas"], focused=True))
    after_image = _white()
    changed = STRONG_CHANGE_MIN_PIXELS * 3
    for index in range(changed):  # a compact strongly-changed block (a real redraw)
        after_image.putpixel((index % 64, index // 64), (0, 0, 0))
    after = _observation(after_image, ui_elements=_elements(["canvas"], focused=True))
    assert before.image_base64 != after.image_base64
    result = FocusChangeStrategy().verify(
        _focused_intent("Edit colors dialog opens"), before, after
    )
    assert result.outcome == "verified", (
        f"a strongly-corroborated digest change must still verify: {result.outcome} ({result.note})"
    )


def test_f1_subthreshold_digest_change_degrades_to_uncertain_not_failed() -> None:
    """F1 doctrine pin: an uncorroborated digest change DEGRADES to uncertain (the
    evidence is carried, the pixel tier decides) — the strategy itself never emits
    ``failed`` and never hands out a free ``verified``."""
    before = _observation(_white(), ui_elements=_elements(["canvas"], focused=True))
    after_image = _white()
    after_image.putpixel((5, 5), (128, 128, 128))
    after = _observation(after_image, ui_elements=_elements(["canvas"], focused=True))
    result = FocusChangeStrategy().verify(_focused_intent(), before, after)
    assert result.outcome == "uncertain"
    assert result.verification_method == "focus_change"
    assert result.changed is False  # never claims a proven change


def test_f1_engine_chain_uncorroborated_digest_defers_to_pixel_tier() -> None:
    """F1 chain pin: the flagged-intent chain on a sub-threshold digest-only change
    lands on the screenshot-diff verdict, not a focus_change verdict (V-2 C11's
    decisive engine-level test, now inverted). W-1 (057) contract update: that
    pixel-tier verdict for a FLAGGED focus expectation is ``uncertain`` (the
    no-data case), not the old definitive false failure."""
    before = _observation(_white(), ui_elements=_elements(["field"], focused=True))
    after_image = _white()
    after_image.putpixel((32, 24), (0, 0, 0))
    after = _observation(after_image, ui_elements=_elements(["field"], focused=True))
    engine = VerificationEngine(judge=None)
    result = engine.verify(_focused_intent("Edit colors dialog opens"), before, after)
    assert result.verification_method != "focus_change"
    assert result.outcome == "uncertain"  # the honest pixel-tier no-data verdict
    assert result.outcome != "verified"  # the digest change alone proves nothing


# --- F2: signal (a) needs a real focus marker on at least one side -----------------------------


def test_f2_unfocused_reenumeration_order_change_does_not_verify() -> None:
    """F2 core pin: NO element carries a focus marker on either side; UIA
    re-enumeration merely reordered the unfocused controls (elements[0] changed).
    This must NOT verify the click effect — without a real focus claim the signal
    abstains (uncertain)."""
    before = _observation(
        _white(), ui_elements=_elements(["toolbar", "old-first"])  # no focus flags
    )
    after = _observation(
        _white(), ui_elements=_elements(["NEW-FIRST", "toolbar"])  # reordered, still unfocused
    )
    strategy = FocusChangeStrategy()
    result = strategy.verify(_focused_intent(), before, after)
    assert result.outcome != "verified", (
        f"a re-enumeration order change verified as a focus change: {result.outcome} ({result.note})"
    )
    engine = VerificationEngine(judge=None)
    chain = engine.verify(_focused_intent(), before, after)
    # W-1 (057) contract update: identical pixels + flagged focus expectation ->
    # the pixel tier's no-data case degrades to uncertain (never the old false
    # failure; never a free verified).
    assert chain.outcome == "uncertain"  # pixel tier cannot observe a focus change


def test_f2_focus_appearing_where_absent_still_verifies() -> None:
    """F2 preservation pin: focus IDENTITY appearing where it was absent (before had
    no focus marker, after DOES — the control actually received focus) remains a
    real deterministic signal (the click-into-a-field transition itself)."""
    before = _observation(
        _white(), ui_elements=_elements(["canvas"])  # nothing focused before the click
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
        ],  # focused AFTER the click
    )
    result = FocusChangeStrategy().verify(_focused_intent(), before, after)
    assert result.outcome == "verified"
    assert result.verification_method == "focus_change"


def test_f2_focus_disappearing_still_verifies() -> None:
    """F2 preservation pin (mirror): a focus marker present BEFORE and absent AFTER
    is also a real focused-element transition (focus left the control)."""
    before = _observation(
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
    after = _observation(_white(), ui_elements=_elements(["canvas"]))  # focus gone
    result = FocusChangeStrategy().verify(_focused_intent(), before, after)
    assert result.outcome == "verified"
    assert result.verification_method == "focus_change"


def test_f2_no_uia_data_at_all_still_abstains() -> None:
    """F2 unchanged contract: no UIA elements on either side -> the signal abstains
    (uncertain), never a failure, never a free success."""
    before = _observation(_white(), ui_elements=[])
    after = _observation(_white(), ui_elements=[])
    result = FocusChangeStrategy().verify(_focused_intent(), before, after)
    assert result.outcome == "uncertain"


# --- F3: _launch_process — no shell, typed needle validation, nothing spawned on reject ---------


def test_f3_quote_breakout_needle_is_typed_rejected_and_nothing_spawns() -> None:
    """F3 core pin (V-2 B7.b canary shape): the quote-breakout needle that reached
    ``subprocess.Popen([needle], shell=True)`` and executed the smuggled second
    command is now a typed rejection — validation fires BEFORE any spawn (Popen
    never called)."""
    from computer_use_mcp.backend import LaunchTargetError, LocalComputerBackend

    backend = LocalComputerBackend.__new__(LocalComputerBackend)  # _launch_process touches no instance state
    with patch.object(subprocess, "Popen") as popen:
        with pytest.raises(LaunchTargetError) as excinfo:
            backend._launch_process('x" & victim2.bat')
    assert not popen.called  # NOTHING was spawned
    assert "x\" & victim2.bat" in str(excinfo.value)


def test_f3_needle_charset_rejects_all_smuggled_forms() -> None:
    """F3 pin: every metachar-bearing needle (every cmd.exe operator and quote form
    V-2 tried, plus path separators/colons) is rejected by validation alone."""
    from computer_use_mcp.backend import LaunchTargetError, validate_launch_needle

    for needle in (
        'x" & victim2.bat',
        "x' & victim2.bat",
        "notepad.exe&calc",
        "notepad & calc",
        "notepad|calc",
        "notepad>calc",
        "notepad<calc",
        "notepad%calc%",
        "notepad^calc",
        "notepad(calc)",
        "notepad;calc",
        "notepad,calc",
        "notepad=calc",
        "notepad!calc",
        "notepad@calc",
        "notepad#calc",
        "notepad$calc",
        "notepad*calc",
        "notepad+calc",
        "notepad?calc",
        "notepad[calc]",
        "notepad{calc}",
        "notepad`calc",
        "notepad~calc",
        "notepad/calc",
        "notepad\\calc",
        "notepad:calc",
        "notepad\ttab",
        "notepad\nnewline",
        "../../evil.exe",
        "C:\\Windows\\System32\\notepad.exe",
    ):
        with pytest.raises(LaunchTargetError):
            validate_launch_needle(needle)


def test_f3_valid_needles_pass_validation() -> None:
    """F3 pin: legitimate process-name shapes (alnum, dot, underscore, space,
    hyphen) still validate — REM-B's mspaint launch keeps working."""
    from computer_use_mcp.backend import validate_launch_needle

    for needle in (
        "mspaint.exe",
        "mspaint",
        "MSPAINT.EXE",
        "notepad.exe",
        "totally-unknown-app.exe",
        "some app.exe",
        "my_tool.v2.exe",
        "definitely-not-a-real-app-xyz",
    ):
        assert validate_launch_needle(needle) == needle


def test_f3_fake_backend_rejects_bad_needle_and_spawns_nothing() -> None:
    """F3 pin: the FAKE backend (used by the e2e REM-B launch tests) applies the
    same typed validation before recording a launch — a hostile target never
    reaches the ``launched=`` payload and is never recorded as spawned."""
    from computer_use_mcp.backend import FakeComputerBackend

    backend = FakeComputerBackend()  # no app_windows: NO_INSTANCE path
    payload = backend.ensure_app('x" & victim2.bat', allow_launch=True)
    assert "launch_rejected=LaunchTargetError" in payload
    assert "launched=" not in payload
    assert backend.launched_processes == []
    assert backend.ensure_app_calls == ['x" & victim2.bat']


def test_f3_real_ensure_app_folds_typed_rejection_into_payload() -> None:
    """F3 pin: the REAL backend's ensure_app folds the typed rejection into the
    NO_INSTANCE payload (probe-outcome contract preserved — the controller still
    sees a NO_INSTANCE probe, now with an explicit launch_rejected marker)."""
    from computer_use_mcp.backend import LocalComputerBackend

    backend = LocalComputerBackend.__new__(LocalComputerBackend)
    backend.enumerate_app_windows = lambda needle: []  # nothing to attach to
    payload = backend.ensure_app('x" & victim2.bat', allow_launch=True)
    assert "launch_rejected=LaunchTargetError" in payload
    assert "launched=" not in payload


def test_f3_fake_backend_still_launches_valid_needle() -> None:
    """F3 preservation pin: the REM-B end-to-end launch scenario (allowlisted
    ``mspaint.exe`` target, no existing window) still launches and still reports
    ``launched=``."""
    from computer_use_mcp.backend import FakeComputerBackend

    backend = FakeComputerBackend()  # no app_windows: nothing to attach to
    payload = backend.ensure_app("mspaint.exe", allow_launch=True)
    assert "launched=mspaint.exe" in payload
    assert backend.launched_processes == ["mspaint.exe"]


def test_f3_valid_needle_resolves_via_which_and_spawns_without_shell() -> None:
    """F3 pin (mspaint must still launch): a valid needle resolves via
    ``shutil.which`` and is spawned as a plain one-element argv — ``shell`` is
    falsy (the cmd.exe quote-breakout surface is gone)."""
    from computer_use_mcp.backend import LocalComputerBackend

    backend = LocalComputerBackend.__new__(LocalComputerBackend)
    resolved = r"C:\Windows\System32\mspaint.exe"
    with patch.object(shutil, "which", return_value=resolved) as which:
        with patch.object(subprocess, "Popen") as popen:
            result = backend._launch_process("mspaint.exe")
    assert result == "mspaint.exe"
    which.assert_called_once_with("mspaint.exe")
    args, kwargs = popen.call_args
    assert args[0] == [resolved]
    assert not kwargs.get("shell")


def test_f3_unresolvable_bare_name_falls_back_to_raw_name() -> None:
    """F3 pin (PATH resolution preserved): when ``shutil.which`` cannot resolve the
    bare name, the raw name still goes to Popen (Windows CreateProcess searches
    PATH) — the pre-F3 launchability of bare names is kept, minus the shell."""
    from computer_use_mcp.backend import LocalComputerBackend

    backend = LocalComputerBackend.__new__(LocalComputerBackend)
    with patch.object(shutil, "which", return_value=None):
        with patch.object(subprocess, "Popen") as popen:
            result = backend._launch_process("totally-unknown-app-xyz")
    assert result == "totally-unknown-app-xyz"  # the spawn was attempted (soft contract)
    args, _ = popen.call_args
    assert args[0] == ["totally-unknown-app-xyz"]


def test_f3_unknown_valid_name_still_fails_soft_not_raises() -> None:
    """F3 pin: a VALID-charset needle that does not exist keeps the REM-B
    soft-failure contract — ``None`` (payload degrade), never an exception; the
    typed rejection fires only for INVALID needles."""
    from computer_use_mcp.backend import LocalComputerBackend

    backend = LocalComputerBackend.__new__(LocalComputerBackend)
    with patch.object(shutil, "which", return_value=None):
        with patch.object(subprocess, "Popen", side_effect=FileNotFoundError("not found")):
            assert backend._launch_process("definitely-not-a-real-app-xyz") is None


# --- F4: docstring truthfulness (env knob parsing) --------------------------------------------


def test_f4_env_knob_docstrings_are_truthful(monkeypatch: pytest.MonkeyPatch) -> None:
    """F4 pin (docstring-only fix): the module-level comment and the helper
    docstring at the V-2-cited sites must NOT claim "only the EXACT value/string
    ``driver``" — parsing is case-insensitive and whitespace-trimmed, and the docs
    must say so. The BEHAVIOR was already case/whitespace-tolerant (direction of
    the discrepancy is safe); only the claim was wrong."""
    from computer_use_mcp import interference

    source = interference.__doc__ or ""
    source += "\n" + interference.ATTACH_OR_LAUNCH_ENV
    # every module-level docstring/comment site...
    with open(interference.__file__, encoding="utf-8") as handle:
        source = handle.read()
    # ...must stop claiming EXACT-string parsing...
    stale = re.findall(r"[Oo]nly the exact (?:value|string) ``driver``", source)
    assert not stale, f"stale EXACT-string claims remain: {stale}"
    # ...and both cited sites must state the real parsing contract.
    corrected = re.findall(
        r"only the value ``driver`` \(case-insensitive, whitespace-trimmed\)",
        source,
        flags=re.IGNORECASE,
    )
    assert len(corrected) >= 2, "both cited sites (interference.py ~:126/:136) must be corrected"
    helper_doc = interference._default_launch_policy.__doc__ or ""
    assert "(case-insensitive, whitespace-trimmed)" in helper_doc
    # Behavior unchanged (already tolerant — re-pinned to stay honest about it):
    for raw, expected in (
        ("driver", "driver"),
        ("DRIVER", "driver"),
        ("Driver", "driver"),
        (" driver ", "driver"),
        ("driver\t", "driver"),
        ("", "server"),
        ("  ", "server"),
        ("driverX", "server"),
        ("Server", "server"),
        ("SERVER", "server"),
        ("bogus", "server"),
    ):
        monkeypatch.setenv("CORTEX_ATTACH_OR_LAUNCH", raw)
        assert interference._default_launch_policy() == expected, raw
