"""v0.7.1 Defect C: the ``via`` type-transport selector + clipboard text entry.

Field evidence (v0.7.0, live Blender): chords reached the app (hotkey ctrl+v,
keypress enter) while per-character ``type`` inserted ZERO characters (OpenGL/console
apps expose no accessibility tree and skip VK_PACKET input). Approved design:

- additive ``via`` on TYPE only: ``"sendinput"`` (default, byte-identical) |
  ``"clipboard"``; anything else / any non-type action = fail-closed construction error;
- clipboard transport: save -> set CF_UNICODETEXT -> ctrl+v chord through the EXISTING
  chord path -> best-effort restore in ``finally`` (restore failure annotated, never
  raised, never dispatches keystrokes; aborts still restore);
- GATE PARITY: the SAME safety/redaction/approval decision for both transports
  (pinned here by SafetyDecision-equality rows over identical texts);
- typed diagnostic ``TYPE_UNCONFIRMED no-readable-target`` + hint when the read-back
  is blind because there is NO readable focused target.

Every backend mechanic here runs on the RecordingEngine / scripted readers (hermetic;
no real clipboard, no real input), plus one Windows-gated real-clipboard roundtrip
that preserves the user's clipboard content.
"""

from __future__ import annotations

import ctypes
from typing import Any

import pytest
from desktop_tripwire import LIVE_DESKTOP

from recording_engine import RecordingEngine

import computer_use_mcp.desktop.backend as backend_module
import computer_use_mcp.desktop.clipboard as clipboard_module
from computer_use_mcp.desktop.backend import (
    TYPE_UNCONFIRMED_HINT_CLIPBOARD,
    TYPE_UNCONFIRMED_HINT_SENDINPUT,
    TYPE_UNCONFIRMED_MARKER,
    LocalComputerBackend,
    _clipboard_write_text,
)
from computer_use_mcp.desktop.clipboard import _clipboard_read_verdict
from computer_use_mcp.desktop.errors import InputBlockedError
from computer_use_mcp.desktop.fake_backend import FakeComputerBackend
from computer_use_mcp.core.models import ActionSpec, ActionType, GroundedAction, SessionState
from computer_use_mcp.security.safety import SafetyContext, SafetyPolicy
from computer_use_mcp.server.schemas import _grounded_shape_hint
from computer_use_mcp.core.state import StopToken, TaskStopped
from computer_use_mcp.pipeline.validator import GroundingValidator

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")

# DELTA 1 (VAL-WP4b BLOCKER): the five real-Win32 tests below TOUCH THE USER'S LIVE
# SYSTEM CLIPBOARD (save/set/restore of real content, including a test that writes a
# pre-existing astral payload without a save). They are the v0.7.1 F-01 live proofs and
# are NOT part of the offline suite any more: they run only under the project's live
# opt-in (CUMCP_RUN_E2E=1 — the same gate as tests/e2e, i.e. the WP12 live gate).
# Everything they pin that is testable WITHOUT the OS is pinned offline by the stubbed
# equivalents below (the F-01 code-unit sizing on the recording kernel32 fake, the
# stubbed-OS astral roundtrip, and the refused-write-never-opens ordering).
# WP-H: LIVE_CLIPBOARD is now the SHARED LIVE_DESKTOP marker
# (tests/desktop_tripwire.py) — one gate and one reason for every real-desktop test
# (real input, real system clipboard, real app launches).
LIVE_CLIPBOARD = LIVE_DESKTOP


# --- helpers ----------------------------------------------------------------------------------------


class _ScriptedReader:
    """Scripted semantic-reader stand-in (mirrors test_r04's shape, minimal).

    The LAST value repeats once the script is exhausted. ``available=False`` models the
    OpenGL/console shape: the reader cannot even identify a focused target.
    """

    def __init__(
        self,
        values: list[str | None],
        *,
        available: bool = True,
        control_type: str | None = "Edit",
    ) -> None:
        self.available = available
        self._values = list(values)
        self._last: str | None = None
        self._control_type = control_type

    def warm(self) -> bool:
        return True

    def read(self) -> dict[str, object]:
        if self._values:
            self._last = self._values.pop(0)
        return {
            "focused": {"value": self._last, "control_type": self._control_type},
            "elements": [],
            "source": "scripted",
        }


def _type_action(text: str, via: str | None = None) -> GroundedAction:
    return GroundedAction(action="type", text=text, confidence=1.0, via=via)


def _clipboard_backend(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reader: _ScriptedReader | None,
    prior_clipboard: str | None = "prior-content",
    write_failures: set[str] | None = None,
) -> tuple[RecordingEngine, list[str], list[str]]:
    """Backend with a recording engine, scripted reader, and HERMETIC clipboard helpers.

    Returns ``(engine, writes, reads_seen)``: ``writes`` records every
    ``_clipboard_write_text`` payload in order (set first, restore last); ``reads_seen``
    records every save-half read. No real clipboard is touched.
    """
    monkeypatch.setattr(backend_module, "TYPE_VERIFY_SETTLE_SECONDS", 0.0)
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    if reader is not None:
        monkeypatch.setattr(real_backend, "_semantic_reader", reader)
    writes: list[str] = []
    reads_seen: list[str] = []

    reason = "restorable" if prior_clipboard is not None else "non_text"

    def _verdict() -> tuple[str | None, str]:
        reads_seen.append("read")
        return (prior_clipboard, reason)

    failures = write_failures or set()

    def _write(text: str) -> bool:
        writes.append(text)
        return text not in failures

    # D13 / repair 3.3 + WP10a: the transport reads the clipboard ONLY through
    # ``_clipboard_read_verdict`` (the three-way ``restorable``/``non_text``/``unknown``
    # answer); the two-valued ``_clipboard_read_text`` projection was deleted as dead
    # code.  The verdict is patched on the backend module, where production looks it up.
    # The verdict shape is what production gets: ``prior_clipboard=None`` is reported as
    # a NON-TEXT payload, which is exactly the case 3.3 discloses.
    monkeypatch.setattr(backend_module, "_clipboard_read_verdict", _verdict)
    monkeypatch.setattr(backend_module, "_clipboard_write_text", _write)
    return engine, writes, reads_seen


# --- surface: GroundedAction.via + ActionSpec pass-through ------------------------------------------


def test_via_defaults_to_none_and_both_values_are_accepted() -> None:
    assert GroundedAction(action="type", text="x", confidence=1.0).via is None
    assert _type_action("x", "sendinput").via == "sendinput"
    assert _type_action("x", "clipboard").via == "clipboard"


def test_via_unknown_value_fails_construction() -> None:
    with pytest.raises(ValueError, match="via must be"):
        GroundedAction(action="type", text="x", confidence=1.0, via="paste")
    with pytest.raises(ValueError, match="via must be"):
        GroundedAction(action="type", text="x", confidence=1.0, via="CLIPBOARD")  # case-sensitive


def test_via_on_non_type_action_fails_construction() -> None:
    with pytest.raises(ValueError, match="via is only valid on type actions"):
        GroundedAction(action="hotkey", keys=["ctrl", "v"], confidence=1.0, via="clipboard")
    with pytest.raises(ValueError, match="via is only valid on type actions"):
        GroundedAction(action="click", point={"x": 1, "y": 1}, confidence=1.0, via="sendinput")


def test_actionspec_passes_via_through_to_grounded() -> None:
    spec = ActionSpec.model_validate({"action": "type", "text": "x", "via": "clipboard"})
    assert spec.to_grounded().via == "clipboard"
    plain = ActionSpec.model_validate({"action": "type", "text": "x"})
    assert plain.to_grounded().via is None  # default unchanged


def test_actionspec_via_misuse_fails_at_the_queue_boundary() -> None:
    # R-18 boundary: an ActionSpec-valid but GroundedAction-invalid via must raise HERE
    # (server converts it to the typed invalid_action teaching rejection; never silently
    # dropped, never an uncaught error inside the queue).
    bad_action = ActionSpec.model_validate({"action": "click", "x": 1, "y": 1, "via": "clipboard"})
    with pytest.raises(ValueError, match="via is only valid on type actions"):
        bad_action.to_grounded()
    bad_value = ActionSpec.model_validate({"action": "type", "text": "x", "via": "paste"})
    with pytest.raises(ValueError, match="via must be"):
        bad_value.to_grounded()


def test_teaching_hint_covers_both_via_failure_classes() -> None:
    hint_action = _grounded_shape_hint(
        'via is only valid on type actions; received via="clipboard" on action="hotkey"'
    )
    hint_value = _grounded_shape_hint('via must be "sendinput" or "clipboard"; received \'paste\'')
    assert hint_action is not None and "type" in hint_action
    assert hint_value is not None and "clipboard" in hint_value


# --- pipeline validator: defense-in-depth via codes ---------------------------------------------------


def test_validator_accepts_clipboard_via_on_type() -> None:
    backend = FakeComputerBackend()
    observation = backend.observe()
    outcome = GroundingValidator().validate(_type_action("hello", "clipboard"), observation)
    assert outcome.valid, outcome.reasons
    assert "via_not_allowed" not in outcome.codes and "via_invalid" not in outcome.codes


def test_validator_rejects_via_on_non_type_via_model_construct() -> None:
    backend = FakeComputerBackend()
    observation = backend.observe()
    # model_construct bypasses pydantic validators: the pipeline gate must still refuse.
    action = GroundedAction.model_construct(
        action=ActionType.HOTKEY, keys=["ctrl", "v"], via="clipboard"
    )
    outcome = GroundingValidator().validate(action, observation)
    assert not outcome.valid
    assert "via_not_allowed" in outcome.codes


def test_validator_rejects_unknown_via_value_via_model_construct() -> None:
    backend = FakeComputerBackend()
    observation = backend.observe()
    action = GroundedAction.model_construct(action=ActionType.TYPE, text="hello", via="paste")
    outcome = GroundingValidator().validate(action, observation)
    assert not outcome.valid
    assert "via_invalid" in outcome.codes


# --- GATE PARITY: one SafetyDecision for both transports (the approved-design pin) -------------------


def test_gate_parity_identical_safety_decision_across_transports() -> None:
    """The SAME gate decision must apply to both transports for the same text.

    Rows cover the field-probe string (``exec(open(...))`` — low/plain_text_entry yet
    approval-required by the interactive default policy), plain text, and a
    gate-failing secret-shaped text. Zero new gate exceptions: the decision object is
    EQUAL for ``sendinput`` and ``clipboard``; transports differ only after approval.
    """
    policy = SafetyPolicy()
    state = SessionState(session_id="parity", dry_run=False, require_approval=True)
    context = SafetyContext()
    texts = [
        "print('hello world')",
        "exec(open('payload.py').read())",  # the exact field-probe string
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",  # secret-shaped: gate denial both ways
    ]
    for text in texts:
        decision_sendinput = policy.evaluate(_type_action(text, "sendinput"), state, context)
        decision_clipboard = policy.evaluate(_type_action(text, "clipboard"), state, context)
        assert decision_sendinput == decision_clipboard, text
    # The field-probe string stays approval-required under the interactive default policy
    # on BOTH transports (with an identified target it classifies low/plain_text_entry,
    # per the approved design's classifier probe; the default context demotes an unknown
    # target honestly — identical on both transports either way).
    identified = SafetyContext(active_process_name="blender.exe", window_title="Blender")
    probe = "exec(open('payload.py').read())"
    for via in ("sendinput", "clipboard"):
        decision = policy.evaluate(_type_action(probe, via), state, identified)
        assert decision.allowed is True
        assert decision.requires_approval is True
        assert decision.risk is not None and decision.risk.value == "low"
        assert decision.category == "plain_text_entry"
    # Unknown-target context: still identical across transports (honest MEDIUM both ways).
    assert policy.evaluate(_type_action(probe, "sendinput"), state) == policy.evaluate(
        _type_action(probe, "clipboard"), state
    )


def test_gate_parity_classification_identical_across_transports() -> None:
    policy = SafetyPolicy()
    for text in ("rm -rf /", "notepad please", "password=(hunter2)"):
        assert policy.classify(_type_action(text, "sendinput")) == policy.classify(
            _type_action(text, "clipboard")
        )


# --- real backend clipboard mechanics (hermetic helpers, recording engine) ---------------------------


def test_clipboard_transport_sets_pastes_and_restores(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _ScriptedReader(["target: hello world"])
    engine, writes, reads_seen = _clipboard_backend(real_backend, monkeypatch, reader=reader)
    message = real_backend.execute(_type_action("hello world", "clipboard"))
    # ONE chord through the field-proven path, no per-character dispatch:
    assert engine.calls == [("chord", "ctrl", "v")]
    # restore discipline: prior read BEFORE the set, set text, restore prior, in order:
    assert reads_seen == ["read"]
    assert writes == ["hello world", "prior-content"]
    # readable target confirms the paste:
    assert message == "Executed type. via=clipboard integrity=verified(11/11)"


def test_clipboard_transport_blind_read_carries_the_typed_diagnostic(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    # available=False = the OpenGL/console shape: NO readable focused target at all.
    reader = _ScriptedReader([None], available=False)
    engine, writes, _reads = _clipboard_backend(real_backend, monkeypatch, reader=reader)
    message = real_backend.execute(_type_action("abcdef", "clipboard"))
    assert message == (
        "Executed type. via=clipboard integrity=unverified(0/6) "
        f"{TYPE_UNCONFIRMED_MARKER} ({TYPE_UNCONFIRMED_HINT_CLIPBOARD})"
    )
    assert engine.calls == [("chord", "ctrl", "v")]
    assert writes == ["abcdef", "prior-content"]


def test_clipboard_transport_valueless_but_present_target_stays_unverified_without_marker(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A target EXISTS (valueless control): honest unverified, no diagnostic — the marker
    # is reserved for the genuinely target-less shape.
    reader = _ScriptedReader([None], control_type=None)
    engine, _writes, _reads = _clipboard_backend(real_backend, monkeypatch, reader=reader)
    message = real_backend.execute(_type_action("abcdef", "clipboard"))
    assert message == "Executed type. via=clipboard integrity=unverified(0/6)"
    assert engine.calls == [("chord", "ctrl", "v")]


def test_clipboard_restore_failure_is_annotated_and_never_fails_the_action(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _ScriptedReader(["abcdef"])
    engine, _writes, _reads = _clipboard_backend(
        real_backend, monkeypatch, reader=reader, write_failures={"prior-content"}
    )
    message = real_backend.execute(_type_action("abcdef", "clipboard"))
    assert "clipboard_restore=failed" in message
    assert message.startswith("Executed type. via=clipboard")
    assert ("chord", "ctrl", "v") in engine.calls  # the paste itself still succeeded


def test_clipboard_transport_mid_transport_abort_still_restores(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop AFTER the clipboard is set (pre-chord check) must still restore."""
    reader = _ScriptedReader([None], available=False)
    engine, writes, _reads = _clipboard_backend(real_backend, monkeypatch, reader=reader)
    stop = StopToken()

    def _stop_after_set(text: str) -> bool:
        writes.append(text)
        stop.stop()  # fires between the set and the chord
        return True

    monkeypatch.setattr(backend_module, "_clipboard_write_text", _stop_after_set)
    with pytest.raises(TaskStopped):
        real_backend.execute(_type_action("abcdef", "clipboard"), stop)
    assert engine.calls == []  # ZERO keystrokes dispatched
    assert writes == ["abcdef", "prior-content"]  # restore ran in finally


def test_clipboard_transport_chord_blocked_still_restores(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _ScriptedReader([None], available=False)
    engine, writes, _reads = _clipboard_backend(real_backend, monkeypatch, reader=reader)

    def _blocked_chord(keys: list[str]) -> None:
        raise InputBlockedError("failsafe")

    monkeypatch.setattr(engine, "chord", _blocked_chord)
    with pytest.raises(InputBlockedError):
        real_backend.execute(_type_action("abcdef", "clipboard"))
    assert writes == ["abcdef", "prior-content"]  # the abort still restored


def test_clipboard_set_failure_is_a_typed_failure_with_zero_keystrokes(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _ScriptedReader([None], available=False)
    engine, writes, _reads = _clipboard_backend(real_backend, monkeypatch, reader=reader)

    def _refuse(text: str) -> bool:
        writes.append(text)
        return False  # the clipboard could not be set (held / allocation refused)

    monkeypatch.setattr(backend_module, "_clipboard_write_text", _refuse)
    with pytest.raises(InputBlockedError, match="clipboard could not be set"):
        real_backend.execute(_type_action("abcdef", "clipboard"))
    assert engine.calls == []  # fail closed: no chord, nothing typed anywhere


def test_empty_text_via_clipboard_stays_a_noop(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, writes, reads_seen = _clipboard_backend(real_backend, monkeypatch, reader=None)
    assert real_backend.execute(_type_action("", "clipboard")) == "Executed type."
    assert engine.calls == []  # no chord, no typing
    assert writes == [] and reads_seen == []  # no clipboard churn at all


def test_sendinput_via_and_default_pay_zero_clipboard_cost(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Perf contract: the default path must not touch ANY clipboard helper."""
    reader = _ScriptedReader(["abc"])
    engine, _writes, _reads = _clipboard_backend(real_backend, monkeypatch, reader=reader)

    def _bomb_verdict() -> tuple[str | None, str]:
        raise AssertionError("clipboard read reached on the sendinput path")

    def _bomb_write(text: str) -> bool:
        raise AssertionError("clipboard write reached on the sendinput path")

    # D13 / repair 3.3 + WP10a: the verdict form is the ONLY reader the clipboard
    # transport calls (the two-valued ``_clipboard_read_text`` projection was deleted
    # as dead code), so the "the default path pays ZERO clipboard cost" bombs are
    # planted on the verdict and the write.
    monkeypatch.setattr(backend_module, "_clipboard_read_verdict", _bomb_verdict)
    monkeypatch.setattr(backend_module, "_clipboard_write_text", _bomb_write)
    legacy = real_backend.execute(_type_action("abc"))  # via=None
    explicit = real_backend.execute(_type_action("abc", "sendinput"))  # explicit default
    # WP12c C2: the field already holds 'abc' (the scripted baseline) and the
    # dispatch cannot prove the insertion — honest unverified, ZERO clipboard cost
    # either way (the point of this test).
    assert legacy == explicit == (
        "Executed type. integrity=unverified(0/3) branch=tail_unproven"
    )
    assert [call[0] for call in engine.calls] == ["type_text", "type_text"]


def test_unverified_sendinput_blind_target_carries_typed_diagnostic_and_hint(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _ScriptedReader([None], available=False)
    engine, _writes, _reads = _clipboard_backend(real_backend, monkeypatch, reader=reader)
    message = real_backend.execute(_type_action("abcdef"))  # default transport, blind target
    assert message == (
        "Executed type. integrity=unverified(0/6) branch=blind_read "
        f"{TYPE_UNCONFIRMED_MARKER} ({TYPE_UNCONFIRMED_HINT_SENDINPUT})"
    )
    assert ("type_text", "abcdef") in engine.calls


def test_unverified_sendinput_valueless_but_present_target_keeps_legacy_message(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression pin: a present-but-valueless control does NOT fire the marker.
    # WP12a: the reply names the ladder branch (blind read) inside the message.
    reader = _ScriptedReader([None], control_type=None)
    _engine, _writes, _reads = _clipboard_backend(real_backend, monkeypatch, reader=reader)
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6) branch=blind_read"


# --- FakeComputerBackend mirror -----------------------------------------------------------------------


def test_fake_clipboard_transport_records_paste_and_restores() -> None:
    backend = FakeComputerBackend()
    backend.clipboard_value = "old-clipboard"
    backend.focused_control_value = "console: "  # readable target
    message = backend.execute(_type_action("print(1)", "clipboard"))
    assert message == "Simulated type. via=clipboard integrity=verified(8/8)"
    assert backend.clipboard_pastes == ["print(1)"]  # the chord was dispatched
    assert backend.focused_control_value == "console: print(1)"  # the paste landed
    assert backend.clipboard_value == "old-clipboard"  # restore discipline held
    assert [item.action.value for item in backend.executed] == ["type"]


def test_fake_clipboard_transport_blind_target_carries_diagnostic() -> None:
    backend = FakeComputerBackend()  # focused_control_value=None: the simulated Blender
    backend.clipboard_value = "old"
    message = backend.execute(_type_action("abc", "clipboard"))
    assert message == (
        "Simulated type. via=clipboard integrity=unverified(0/3) "
        f"{TYPE_UNCONFIRMED_MARKER} ({TYPE_UNCONFIRMED_HINT_CLIPBOARD})"
    )
    assert backend.clipboard_pastes == ["abc"]
    assert backend.clipboard_value == "old"


def test_fake_clipboard_transport_mid_transport_abort_restores_without_paste() -> None:
    backend = FakeComputerBackend()
    backend.clipboard_value = "old"
    stop = StopToken()
    stop.stop()  # armed to fire DURING the transport (after the set, before the paste)
    with pytest.raises(TaskStopped):
        backend.execute(_type_action("abc", "clipboard"), stop)
    assert backend.clipboard_pastes == []  # no chord recorded
    assert backend.clipboard_value == "old"  # the set was rolled back by the restore


def test_fake_sendinput_blind_target_still_carries_the_diagnostic() -> None:
    backend = FakeComputerBackend()
    message = backend.execute(_type_action("abc"))  # default transport
    assert message == (
        "Simulated type. integrity=unverified(0/3) branch=blind_read "
        f"{TYPE_UNCONFIRMED_MARKER} ({TYPE_UNCONFIRMED_HINT_SENDINPUT})"
    )


def test_fake_kill_switch_does_not_disable_the_explicit_clipboard_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # CORTEX_TYPE_INTEGRITY=0 removes the read-back LADDER, not an explicitly requested
    # transport: the paste still rides the chord path and reports honestly.
    # C-4: the driven reader is FakeComputerBackend — patch ITS binding.
    from computer_use_mcp.desktop import fake_backend as fake_backend_module

    monkeypatch.setattr(fake_backend_module, "TYPE_INTEGRITY_ENABLED", False)
    backend = FakeComputerBackend()
    backend.focused_control_value = ""
    message = backend.execute(_type_action("abc", "clipboard"))
    assert backend.clipboard_pastes == ["abc"]
    assert message == "Simulated type. via=clipboard integrity=verified(3/3)"


# --- real Win32 clipboard mechanics (Windows-gated roundtrip; preserves user content) -----------------


#: The plain-text clipboard formats whose loss a test's save/restore can undo.
#: Anything else (bitmap, HDROP file list, enhanced metafile, ...) alongside or
#: instead of text would be DESTROYED by a write+restore cycle, so the live tests
#: SKIP unless the clipboard holds only these. (CF_TEXT=1, CF_OEMTEXT=7,
#: CF_UNICODETEXT=13, CF_LOCALE=16.)
_TEXT_CLIPBOARD_FORMATS = frozenset({1, 7, 13, 16})


def _restore_user_clipboard(prior: str, restored: dict[str, Any]) -> None:
    """WP13 G (VAL-WP12-06 NIT): best-effort restore of the user's text. Called
    from the ``finally`` — INCLUDING when the test body already failed — so a
    failed restore is never silently swallowed: it raises AssertionError (chained
    with the body exception) instead of vanishing."""
    if restored["ok"]:
        return
    ok = _clipboard_write_text(prior) is True
    readback = _clipboard_read_verdict()[0]
    if not (ok and readback == prior):
        raise AssertionError(
            f"clipboard restore FAILED after the body had already failed "
            f"(write ok={ok}, readback={readback!r}, expected {prior!r}) — the "
            "user's clipboard content could not be put back"
        )


def _require_text_only_clipboard() -> str:
    """WP12e P1 precondition for every LIVE clipboard test.

    Enumerates the clipboard formats READ-ONLY (open + EnumClipboardFormats +
    CloseClipboard — no EmptyClipboard, no writes) and SKIPS the test unless the
    clipboard holds ONLY plain-text formats AND is readable as text. Returns the
    user's text so the test can restore it in a ``finally`` (with a read-back
    verification)."""
    prior = _clipboard_read_verdict()[0]
    if prior is None:
        pytest.skip("clipboard not readable as text right now (non-text content or locked)")
    if not clipboard_module._clipboard_open():
        pytest.skip("clipboard could not be opened for the read-only format check")
    # WP13 G (VAL-WP12-06 NIT): bind EnumClipboardFormats on a PRIVATE WinDLL —
    # setting argtypes on the SHARED clipboard_module._user32 object is exactly the
    # shared-DLL mutation the WP12 class-A guard forbids.
    private_user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        private_user32.EnumClipboardFormats.argtypes = [ctypes.c_uint]
        private_user32.EnumClipboardFormats.restype = ctypes.c_uint
        formats: set[int] = set()
        fmt = int(private_user32.EnumClipboardFormats(0))
        while fmt:
            formats.add(fmt)
            fmt = int(private_user32.EnumClipboardFormats(fmt))
    finally:
        clipboard_module._user32.CloseClipboard()
    non_text = formats - _TEXT_CLIPBOARD_FORMATS
    if non_text:
        pytest.skip(
            f"clipboard holds non-text formats {sorted(non_text)}; a write+restore "
            "cycle would destroy them — not overwriting the user's clipboard"
        )
    return prior


@WINDOWS_ONLY
@LIVE_CLIPBOARD
def test_real_win32_clipboard_roundtrip_preserves_prior_content() -> None:
    prior = _require_text_only_clipboard()  # P1: text-only or skip; user's text saved
    restored = {"ok": False, "readback": None}
    try:
        assert _clipboard_write_text("cortex-v071-clipboard-roundtrip") is True
        assert _clipboard_read_verdict()[0] == "cortex-v071-clipboard-roundtrip"
        restored["ok"] = _clipboard_write_text(prior) is True
        restored["readback"] = _clipboard_read_verdict()[0]
    finally:
        _restore_user_clipboard(prior, restored)
    assert restored["ok"] is True and restored["readback"] == prior


# --- F-01 (RED-1) regression: allocation sizing by UTF-16 CODE UNITS, never char count ---------------
#
# The defect: ``size = (len(text) + 1) * 2`` counted CHARACTERS, but an astral char
# (emoji, CJK Ext-B) is TWO UTF-16 wchars — the CF_UNICODETEXT block shipped WITHOUT
# its terminating NUL and pasted text silently lost its final character (the restore
# path corrupted the user's clipboard the same way). RED-1 proof: wrote
# 61003dd880de6200, clipboard held 61003dd880de0000 ('b' -> 0000).


class _FakeAllocKernel32:
    """kernel32 stand-in recording every GlobalAlloc size (exact-size zeroed blocks)."""

    def __init__(self) -> None:
        self.alloc_sizes: list[int] = []
        self._blocks: dict[int, ctypes.create_string_buffer] = {}  # type: ignore[valid-type]
        self._next = 1

    @staticmethod
    def _unwrap(handle: object) -> int:
        """The integer handle behind an int or a c_void_p wrapper (None -> 0)."""
        value = getattr(handle, "value", handle)
        return int(value) if value is not None else 0

    def GlobalAlloc(self, flags: int, size: int) -> int:
        size = int(size)
        self.alloc_sizes.append(size)
        handle = self._next
        self._next += 1
        self._blocks[handle] = ctypes.create_string_buffer(size)  # EXACT size, zero-filled
        return handle

    def GlobalLock(self, handle: object) -> int | None:
        block = self._blocks.get(self._unwrap(handle)) if self._unwrap(handle) else None
        return ctypes.addressof(block) if block is not None else None

    def GlobalUnlock(self, handle: object) -> int:
        return 1

    def GlobalFree(self, handle: object) -> None:
        self._blocks.pop(self._unwrap(handle), None)


class _FakeClipboardUser32:
    """user32 stand-in: clipboard content = the LAST successfully Set handle."""

    def __init__(self, kernel32: _FakeAllocKernel32) -> None:
        self.k = kernel32
        self._handle: int | None = None

    def OpenClipboard(self, _owner: int | None) -> int:
        return 1

    def CloseClipboard(self) -> int:
        return 1

    def EmptyClipboard(self) -> int:
        self._handle = None
        return 0

    def IsClipboardFormatAvailable(self, fmt: int) -> int:
        return 1 if self._handle is not None else 0

    def GetClipboardData(self, fmt: int) -> int:
        return self._handle or 0

    def SetClipboardData(self, fmt: int, handle: object) -> int | None:
        value = getattr(handle, "value", handle)
        if value is None or int(value) == 0:
            return None
        self._handle = int(value)
        return self._handle


def test_clipboard_alloc_size_counts_utf16_code_units_not_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-01 sizing pin: GlobalAlloc receives (UTF-16 units + 1) * 2 BYTES for every text.

    The exact-size zero-filled fake makes the defect mechanically visible: with the
    char-counted formula, astral text leaves the terminator OUTSIDE the block (the
    recorded size is simply wrong). Hermetic — no real clipboard.
    """
    kernel32 = _FakeAllocKernel32()
    monkeypatch.setattr(clipboard_module, "_kernel32", kernel32)
    monkeypatch.setattr(clipboard_module, "_user32", _FakeClipboardUser32(kernel32))
    # The fakes are plain callables: ctypes prototype binding is skipped (no-op needed).
    monkeypatch.setattr(clipboard_module, "_CLIPBOARD_ARGTYPES_BOUND", True)
    cases = [
        ("plain", (5 + 1) * 2),  # 5 BMP chars + NUL
        ("", (0 + 1) * 2),  # NUL only
        ("héllo", (5 + 1) * 2),  # non-ASCII BMP still 1 unit per char
        ("a\U0001F680b", (4 + 1) * 2),  # emoji = 2 units: 4 units + NUL (was 8 defectively)
        ("\U000020000\U000020001", (4 + 1) * 2),  # CJK Ext-B pair = 4 units + NUL
        ("e\u0301\U0001F9D1\u200D\U0001F91D\u200D\U0001F9D1", (10 + 1) * 2),  # combining + ZWJ family
    ]
    for text, expected_size in cases:
        kernel32.alloc_sizes.clear()
        assert _clipboard_write_text(text) is True
        assert kernel32.alloc_sizes == [expected_size], f"{text!r}: {kernel32.alloc_sizes}"


@WINDOWS_ONLY
@LIVE_CLIPBOARD
def test_real_win32_clipboard_astral_roundtrip_byte_identical() -> None:
    """F-01 end-to-end: astral text round-trips write->read BYTE-identical (was lossy)."""
    prior = _require_text_only_clipboard()  # P1: text-only or skip; user's text saved
    restored = {"ok": False, "readback": None}
    cases = [
        "a\U0001F680b",  # the RED-1 proof shape: astral MIDDLE (tail 'b' was zeroed)
        "\U0001F680",  # astral tail: the last character was lost entirely
        "script\U00020000ab",  # CJK Ext-B
        "e\u0301\u0327",  # combining marks (BMP)
        "héllo 世界 \U0001F680RED1",  # mixed BMP + astral
    ]
    try:
        for text in cases:
            assert _clipboard_write_text(text) is True, text
            assert _clipboard_read_verdict()[0] == text, text  # the last char SURVIVES
        restored["ok"] = _clipboard_write_text(prior) is True
        restored["readback"] = _clipboard_read_verdict()[0]
    finally:
        _restore_user_clipboard(prior, restored)
    assert restored["ok"] is True and restored["readback"] == prior


@WINDOWS_ONLY
@LIVE_CLIPBOARD
def test_real_win32_clipboard_property_roundtrip_bmp_and_astral() -> None:
    """F-01 property row: N seeded random BMP+astral+combining strings all round-trip."""
    import random

    rng = random.Random(0xF01)  # deterministic
    pool_bmp = list("abcXYZ 0189") + ["é", "世", "界", "e\u0301", "£", "\U0000FFFD"]
    pool_astral = ["\U0001F680", "\U00020000", "\U0001D54F", "\U0001F9D1\u200D\U0001F91D"]
    prior = _require_text_only_clipboard()  # P1: text-only or skip; user's text saved
    restored = {"ok": False, "readback": None}
    try:
        for _ in range(40):
            text = "".join(
                rng.choice(pool_bmp) if rng.random() < 0.6 else rng.choice(pool_astral)
                for _ in range(rng.randint(0, 24))
            )
            assert _clipboard_write_text(text) is True
            assert _clipboard_read_verdict()[0] == text, f"roundtrip corrupted {text!r}"
        restored["ok"] = _clipboard_write_text(prior) is True
        restored["readback"] = _clipboard_read_verdict()[0]
    finally:
        _restore_user_clipboard(prior, restored)
    assert restored["ok"] is True and restored["readback"] == prior


@WINDOWS_ONLY
@LIVE_CLIPBOARD
def test_clipboard_transport_astral_paste_and_restore(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-01 at transport level: an astral suffix survives the paste AND the restore.

    WP12e P1: the test first requires a TEXT-ONLY user clipboard (read-only format
    enumeration — any rich format would be destroyed by the write+restore cycle),
    saves the user's text, and restores it in a finally with a read-back
    verification."""
    text = "console: a\U0001F680b"  # astral char mid-text, BMP tail (the lossy shape)
    prior = "prior \U00020000-content"  # astral content the restore must preserve
    user_text = _require_text_only_clipboard()  # P1: text-only or skip; user's text saved
    restored = {"ok": False, "readback": None}
    monkeypatch.setattr(backend_module, "TYPE_VERIFY_SETTLE_SECONDS", 0.0)
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_semantic_reader", _ScriptedReader([text]))
    captured: list[str | None] = []

    real_chord = engine.chord

    def _chord(keys: list[str]) -> None:
        captured.append(_clipboard_read_verdict()[0])  # what the OS clipboard holds AT PASTE TIME
        real_chord(keys)

    monkeypatch.setattr(engine, "chord", _chord)
    try:
        assert _clipboard_write_text(prior) is True  # pre-existing astral clipboard content
        message = real_backend.execute(_type_action(text, "clipboard"))
        assert message == f"Executed type. via=clipboard integrity=verified({len(text)}/{len(text)})"
        assert engine.calls[-1] == ("chord", "ctrl", "v")
        assert captured == [text], f"clipboard at paste time lost the astral tail: {captured}"
        assert _clipboard_read_verdict()[0] == prior  # the astral restore is byte-identical
        restored["ok"] = _clipboard_write_text(user_text) is True
        restored["readback"] = _clipboard_read_verdict()[0]
    finally:
        _restore_user_clipboard(user_text, restored)
    assert restored["ok"] is True and restored["readback"] == user_text


@WINDOWS_ONLY
@LIVE_CLIPBOARD
def test_real_win32_refused_write_leaves_user_clipboard_intact() -> None:
    """A refused write (lone surrogate cannot be UTF-16 encoded) must not destroy the
    user's current clipboard content: the encode runs BEFORE the clipboard is opened.
    WP12e P1: text-only precondition (skip otherwise — rich formats must not even be
    risk-adjacent)."""
    prior = _require_text_only_clipboard()  # P1: text-only or skip; user's text saved
    assert _clipboard_write_text("x\ud800y") is False  # refused, never a corrupt block
    assert _clipboard_read_verdict()[0] == prior  # content untouched by the refused write


# --- DELTA 1 (VAL-WP4b BLOCKER): offline stubbed equivalents for the gated live proofs ----
#
# The live tests above run only under CUMCP_RUN_E2E=1. What they pin that is testable
# WITHOUT the OS is pinned here against the SAME recording fakes the F-01 sizing test
# uses (an exact-size zeroed GlobalAlloc kernel32 and a last-Set-handle user32), so a
# regression in code-unit sizing, encode-before-open ordering, or the astral roundtrip
# itself fails OFFLINE. No real clipboard is reachable from any of these: both fakes
# are plain classes and `_clipboard_open`'s only OS touch is through the patched
# `_user32`/`_kernel32`.


class _OpenCountingClipboardUser32(_FakeClipboardUser32):
    """The stubbed-OS user32, plus a record of every OpenClipboard attempt."""

    def __init__(self, kernel32: _FakeAllocKernel32) -> None:
        super().__init__(kernel32)
        self.open_calls = 0

    def OpenClipboard(self, _owner: int | None) -> int:
        self.open_calls += 1
        return 1


def _install_stubbed_os(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeAllocKernel32, _OpenCountingClipboardUser32]:
    kernel32 = _FakeAllocKernel32()
    user32 = _OpenCountingClipboardUser32(kernel32)
    monkeypatch.setattr(clipboard_module, "_kernel32", kernel32)
    monkeypatch.setattr(clipboard_module, "_user32", user32)
    # The fakes are plain callables: ctypes prototype binding is skipped (no-op needed).
    monkeypatch.setattr(clipboard_module, "_CLIPBOARD_ARGTYPES_BOUND", True)
    return kernel32, user32


def test_clipboard_write_read_roundtrip_astral_on_the_stubbed_os(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offline twin of the gated live astral roundtrips: astral / BMP / combining text
    round-trips write->read BYTE-identical through the stubbed OS (the exact-size
    zeroed block + the terminating NUL are what the real clipboard reads back). The
    RED-1 lossy shapes (astral MIDDLE and astral TAIL) are the cases that mattered."""
    _kernel32, _user32 = _install_stubbed_os(monkeypatch)
    cases = [
        "a\U0001F680b",  # astral MIDDLE (the RED-1 shape: tail 'b' was zeroed)
        "\U0001F680",  # astral TAIL: the last character was lost entirely
        "script\U00020000ab",  # CJK Ext-B
        "e\u0301\u0327",  # combining marks (BMP)
        "héllo 世界 \U0001F680RED1",  # mixed BMP + astral
        "",  # NUL-only block round-trips as ""
    ]
    for text in cases:
        assert _clipboard_write_text(text) is True, text
        assert _clipboard_read_verdict()[0] == text, text  # the last char SURVIVES


def test_refused_write_never_opens_the_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline twin of the gated live refused-write pin: a lone surrogate cannot be
    UTF-16 encoded, and the encode runs BEFORE the clipboard is opened — so a refused
    write performs ZERO OpenClipboard calls (there is nothing to destroy)."""
    _kernel32, user32 = _install_stubbed_os(monkeypatch)
    assert _clipboard_write_text("x\ud800y") is False  # refused, never a corrupt block
    assert user32.open_calls == 0  # the clipboard was NEVER opened by the refused write

