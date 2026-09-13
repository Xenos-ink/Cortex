"""R-04 typed-text integrity: chunked typing + read-back verification + verified retype.

The reference machine randomly drops 1-25-character bursts mid-type (B13), and in its
worst observed state (2026-09-13 evidence/v06-006/eng-input probes) swallows ALL
injected keystrokes after the first of a delivered burst — across SendInput,
keybd_event, VK_PACKET and VK-code paths, unaffected by pacing up to 2 s gaps. A
successful dispatch therefore proves NOTHING about landed text. These tests pin the
honest contract:

- chunked dispatch with a stop-token check between chunks (existing discipline kept);
- per-chunk read-back via the semantic-reader value path (stubbed here — hermetic);
- retype ONLY for a measured dropped suffix (the read-back proved text missing),
  bounded to ONE attempt — never blind (B3/B8 double-submit doctrine);
- degrade honestly: unreadable → ``unverified`` (no retype, no false claim);
  transformed/masked targets → ``mismatch`` (appending would double-apply);
- repair failure → typed :class:`TextIntegrityError` with the evidence;
- the action message keeps its ``Executed type.`` prefix (additive suffix only).
"""

from __future__ import annotations

import pytest
from recording_engine import RecordingEngine

import computer_use_mcp.backend as backend_module
from computer_use_mcp.backend import (
    FakeComputerBackend,
    LocalComputerBackend,
    TextIntegrityError,
    _format_integrity_status,
    _normalize_landed_text,
    _split_type_chunks,
    _typed_index_for_norm_len,
)
from computer_use_mcp.models import GroundedAction
from computer_use_mcp.state import StopToken, TaskStopped


class _ScriptedReader:
    """Semantic-reader stand-in: pops scripted focused-control values (hermetic).

    The LAST value repeats once the script is exhausted (a control that keeps holding
    its text across reads). ``None`` entries model an unreadable control UNLESS the
    focused summary carries an always-valued ``control_type`` (Edit) — mirroring the
    real readers, where a None value on an Edit means the control is EMPTY.
    """

    available = True

    def __init__(self, values: list[str | None], *, control_type: str = "Edit") -> None:
        self._values = list(values)
        self._control_type = control_type
        self._last: str | None = None

    def warm(self) -> bool:
        return True

    def read(self) -> dict[str, object]:
        if self._values:
            value = self._values.pop(0)
        else:
            value = self._last
        self._last = value
        return {
            "focused": {"value": value, "control_type": self._control_type},
            "elements": [],
            "source": "scripted",
        }


def _typing_backend(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    reader_values: list[str | None],
    *,
    chunk_chars: int = 64,
    control_type: str | None = "Edit",
) -> RecordingEngine:
    """Session backend with a recording engine + scripted reader, settle-free."""
    monkeypatch.setattr(backend_module, "TYPE_VERIFY_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(backend_module, "TYPE_CHUNK_CHARS", chunk_chars)
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(
        real_backend, "_semantic_reader", _ScriptedReader(reader_values, control_type=control_type)
    )
    return engine


def _type_action(text: str) -> GroundedAction:
    return GroundedAction(action="type", text=text, confidence=1.0)


# --- helpers --------------------------------------------------------------------------------------


def test_split_type_chunks_boundaries() -> None:
    assert _split_type_chunks("", 8) == [""]
    assert _split_type_chunks("abc", 8) == ["abc"]
    assert _split_type_chunks("a" * 8, 8) == ["a" * 8]  # exact multiple: one chunk
    assert _split_type_chunks("a" * 9, 8) == ["a" * 8, "a"]
    assert _split_type_chunks("abcdef", 0) == ["abcdef"]  # degenerate size: whole text


def test_split_type_chunks_keeps_astral_characters_whole() -> None:
    """Chunking is code-point based: an astral character is ONE element, so its
    UTF-16 surrogate pair can never be separated by a chunk boundary (the event-level
    surrogate encoding is the engine's concern and stays within the chunk)."""
    smiley = "\U0001f600"
    text = "a" * 7 + smiley + "b" + smiley  # astral chars fall across the size-8 boundary
    chunks = _split_type_chunks(text, 8)
    assert "".join(chunks) == text
    for chunk in chunks:
        units = backend_module._text_to_key_units(chunk)
        scans = [scan for _vk, scan, _flags in units[::2]]  # every down event's unit
        # no lone high surrogate without its low twin inside the same chunk
        pending_high = 0
        for scan in scans:
            if 0xD800 <= scan <= 0xDBFF:
                pending_high += 1
            elif 0xDC00 <= scan <= 0xDFFF:
                assert pending_high, f"low surrogate without high in chunk {chunk!r}"
                pending_high -= 1
        assert pending_high == 0, f"dangling high surrogate in chunk {chunk!r}"


def test_typed_index_for_norm_len_maps_crlf() -> None:
    text = "a\r\nbc"
    assert _typed_index_for_norm_len(text, 0) == 0
    assert _typed_index_for_norm_len(text, 1) == 1  # 'a'
    assert _typed_index_for_norm_len(text, 2) == 3  # CRLF counts as ONE unit
    assert _typed_index_for_norm_len(text, 4) == 5  # clamps to the end


def test_normalize_landed_text_folds_crlf() -> None:
    assert _normalize_landed_text("a\r\nb\rc") == "a\nb\nc"


def test_format_integrity_status_statuses() -> None:
    assert _format_integrity_status(6, 6, mismatch=0, unverified=0, healed=0) == "integrity=verified(6/6)"
    assert _format_integrity_status(4, 6, mismatch=0, unverified=2, healed=0) == "integrity=partial(4/6)"
    assert _format_integrity_status(0, 6, mismatch=0, unverified=6, healed=0) == "integrity=unverified(0/6)"
    assert _format_integrity_status(2, 6, mismatch=4, unverified=0, healed=0) == "integrity=mismatch(2/6)"
    assert _format_integrity_status(6, 6, mismatch=0, unverified=0, healed=3) == "integrity=verified(6/6) healed=3"


# --- verified path --------------------------------------------------------------------------------


def test_type_chunks_dispatch_per_chunk_and_verify(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "a" * 130  # chunk 64 -> 64 + 64 + 2
    engine = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=["", text[:64], text[:128], text],
    )
    message = real_backend.execute(_type_action(text))
    assert message == "Executed type. integrity=verified(130/130)"
    assert [call[1] for call in engine.calls if call[0] == "type_text"] == [text[:64], text[64:128], text[128:]]


def test_type_verifies_against_baseline_content(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-existing control content is read as the baseline, not treated as corruption."""
    engine = _typing_backend(real_backend, monkeypatch, reader_values=["pref-", "pref-abc"])
    message = real_backend.execute(_type_action("abc"))
    assert message == "Executed type. integrity=verified(3/3)"
    assert engine.calls[-1][0] == "type_text"  # no retype: the last dispatch is the chunk


def test_type_crlf_landing_matches_typed_newline(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typed \\n lands as edit-control \\r\\n; normalization must treat that as verified."""
    engine = _typing_backend(real_backend, monkeypatch, reader_values=["", "a\r\nb"])
    message = real_backend.execute(_type_action("a\nb"))
    assert message == "Executed type. integrity=verified(3/3)"
    assert [call[1] for call in engine.calls if call[0] == "type_text"] == ["a\nb"]


# --- dropped suffix: the ONE verified retype --------------------------------------------------------


def test_dropped_suffix_triggers_single_verified_retype(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Landed 'abc' of typed 'abcdef' -> retype EXACTLY 'def' once, then verified."""
    engine = _typing_backend(real_backend, monkeypatch, reader_values=["", "abc", "abcdef"])
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=verified(6/6) healed=3"
    dispatches = [call[1] for call in engine.calls if call[0] == "type_text"]
    assert dispatches == ["abcdef", "def"]  # the chunk, then exactly the missing suffix


def test_retype_failure_raises_and_never_repeats(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second failure after the ONE verified retype fails typed — never blind loops."""
    engine = _typing_backend(real_backend, monkeypatch, reader_values=["", "abc", "abc"])
    with pytest.raises(TextIntegrityError, match="verified retype"):
        real_backend.execute(_type_action("abcdef"))
    dispatches = [call[1] for call in engine.calls if call[0] == "type_text"]
    assert dispatches == ["abcdef", "def"]  # exactly one retype; no third dispatch


def test_retype_landing_unconfirmed_reports_unverified_without_raise(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the CONFIRM read degrades after the retype, the landing is UNKNOWN: the chunk
    reports unverified honestly (never claimed verified), no raise, no second retype."""
    engine = _typing_backend(
        real_backend, monkeypatch, reader_values=["", "abc", None], control_type=None
    )
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6)"
    dispatches = [call[1] for call in engine.calls if call[0] == "type_text"]
    assert dispatches == ["abcdef", "def"]  # chunk + the one verified retype; nothing more


# --- honest degradation ----------------------------------------------------------------------------


def test_unreadable_control_reports_unverified_and_skips_repair(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _typing_backend(
        real_backend, monkeypatch, reader_values=[None], control_type=None
    )
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6)"
    assert [call[1] for call in engine.calls if call[0] == "type_text"] == ["abcdef"]


def test_empty_edit_control_reads_as_empty_not_unreadable(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A None value on an ALWAYS-VALUED control (Edit) means EMPTY, not unreadable:
    the baseline read succeeds and the action is verifiable (the live-smoke case)."""
    engine = _typing_backend(real_backend, monkeypatch, reader_values=[None, "Z"])
    message = real_backend.execute(_type_action("Z"))
    assert message == "Executed type. integrity=verified(1/1)"
    assert [call[1] for call in engine.calls if call[0] == "type_text"] == ["Z"]


def test_valueless_non_edit_control_stays_unverified(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value-LESS non-edit control (canvas, custom widget) is never verified against."""
    engine = _typing_backend(
        real_backend, monkeypatch, reader_values=[None], control_type="Custom"
    )
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6)"
    assert [call[1] for call in engine.calls if call[0] == "type_text"] == ["abcdef"]


def test_transformed_target_reports_mismatch_without_retype(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-prefix divergence (masked field, autocorrect) must NOT be 'repaired'."""
    engine = _typing_backend(real_backend, monkeypatch, reader_values=["", "zzz"])
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=mismatch(0/6)"
    assert [call[1] for call in engine.calls if call[0] == "type_text"] == ["abcdef"]


# --- stop-token and focus-hook interplay -------------------------------------------------------------


def test_pre_stopped_type_dispatches_nothing(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _typing_backend(real_backend, monkeypatch, reader_values=[""])
    stop = StopToken()
    stop.stop()
    with pytest.raises(TaskStopped):
        real_backend.execute(_type_action("abcdef"), stop)
    assert engine.calls == []


def test_stop_token_between_chunks_aborts_remaining_chunks(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _typing_backend(
        real_backend, monkeypatch, reader_values=[None], chunk_chars=2, control_type=None
    )
    stop = StopToken()
    seen: list[str] = []

    def hook() -> None:
        seen.append("x")
        if len(seen) >= 2:
            stop.stop()  # fires while chunk 2's pre-dispatch hook chain runs

    with pytest.raises(TaskStopped):
        real_backend.execute(_type_action("abcdefgh"), stop, focus_hook=hook)
    dispatches = [call[1] for call in engine.calls if call[0] == "type_text"]
    assert dispatches == ["ab", "cd"]  # chunk 3 never dispatched after the stop fired


def test_focus_hook_rides_every_chunk(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _typing_backend(real_backend, monkeypatch, reader_values=["", "ab", "abcd"], chunk_chars=2)
    hooks: list[int] = []

    def hook() -> None:
        hooks.append(1)

    message = real_backend.execute(_type_action("abcd"), None, focus_hook=hook)
    assert message == "Executed type. integrity=verified(4/4)"
    assert len(hooks) == 2  # one per chunk (T8 focus-continuity cadence preserved)
    assert [call[1] for call in engine.calls if call[0] == "type_text"] == ["ab", "cd"]


# --- kill switch and legacy message -------------------------------------------------------------------


def test_integrity_disabled_restores_legacy_path(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _typing_backend(real_backend, monkeypatch, reader_values=[])
    monkeypatch.setattr(backend_module, "TYPE_INTEGRITY_ENABLED", False)
    message = real_backend.execute(_type_action("a\nb"))
    assert message == "Executed type."  # byte-identical pre-R-04 message
    assert [call[1] for call in engine.calls if call[0] == "type_text"] == ["a\nb"]  # ONE dispatch


def test_empty_text_stays_legacy_noop(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _typing_backend(real_backend, monkeypatch, reader_values=[])
    assert real_backend.execute(_type_action("")) == "Executed type."
    assert [call[1] for call in engine.calls if call[0] == "type_text"] == [""]  # one no-op dispatch


# --- FakeComputerBackend parity ------------------------------------------------------------------------


def test_fake_backend_type_integrity_contract() -> None:
    backend = FakeComputerBackend()
    # unreadable by default: honest unverified, action still recorded
    message = backend.execute(_type_action("abc"))
    assert message == "Simulated type. integrity=unverified(0/3)"
    assert [item.action.value for item in backend.executed] == ["type"]
    # matching value: verified
    backend.focused_control_value = "abc"
    assert backend.execute(_type_action("abc")) == "Simulated type. integrity=verified(3/3)"
    # dropped suffix: simulated verified retype heals, reported with healed count
    backend.focused_control_value = "a"
    assert backend.execute(_type_action("abc")) == "Simulated type. integrity=verified(3/3) healed=2"
    assert backend.focused_control_value == "abc"  # control now holds the full text
    # unrelated content (masked/transformed): mismatch, never claimed verified
    backend.focused_control_value = "zzz"
    assert backend.execute(_type_action("abc")) == "Simulated type. integrity=mismatch(0/3)"


def test_fake_backend_type_integrity_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend_module, "TYPE_INTEGRITY_ENABLED", False)
    backend = FakeComputerBackend()
    assert backend.execute(_type_action("abc")) == "Simulated type."
    assert [item.action.value for item in backend.executed] == ["type"]


# --- engine dispatch boundary (stubs, no real input) ----------------------------------------------------


def test_chunks_reach_the_sendinput_engine_as_separate_batches(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the SendInput engine, each backend chunk is its own engine dispatch."""
    from test_io_parity import _ParityFakeUser32

    fake_user32 = _ParityFakeUser32()
    monkeypatch.setattr(backend_module, "_user32", fake_user32)
    monkeypatch.setattr(backend_module, "TYPE_VERIFY_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(backend_module, "TYPE_CHUNK_CHARS", 3)
    monkeypatch.setattr(real_backend, "_engine", backend_module.SendInputEngine())
    monkeypatch.setattr(
        real_backend, "_semantic_reader", _ScriptedReader([None], control_type=None)
    )
    monkeypatch.setattr(real_backend, "_active_context", None)  # passthrough transform
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6)"
    keyboard_batches = [
        [event for event in batch if event["kind"] == 1] for batch in fake_user32.batches
    ]
    assert [len(batch) for batch in keyboard_batches if batch] == [6, 6]  # 3 chars x (down+up)
