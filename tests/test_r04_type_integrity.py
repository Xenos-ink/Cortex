"""R-04 typed-text integrity, fix3: FAST-PATH-FIRST end-of-action verification.

Performance contract (E6 formal A/B: every type cost ~2.9-3.4 s on the RC): the fix2
lag-guard horizon (2.5 s) fired on nearly every action because read1 at +50 ms is
routinely MID-DRAIN (profiled live: 58 of 81 chars) and then disagreed with read2.
fix3 keeps every correctness guarantee and makes the happy path pay two quick reads:

- read1 after TYPE_VERIFY_SETTLE_SECONDS, read2 after TYPE_VERIFY_CONFIRM_GAP_SECONDS;
- read2 may EQUAL read1 (stable) or EXTEND it (mid-drain growth — real evidence, not a
  disagreement); matching later read -> verified with NO horizon wait, NO repair;
- blind reads and agreed EMPTY buffers -> unverified, never repaired;
- agreed non-empty buffer sharing no expected suffix -> mismatch, never repaired;
- ESCALATION ONLY for suspicious pairs (shrink/divergence) or agreed genuine partials:
  confirm across TYPE_VERIFY_STABILITY_SECONDS (the landing-lag horizon) before the one
  suffix-diff repair; confirmed still-wrong -> TextIntegrityError; unconfirmable ->
  unverified. unverified NEVER repairs (anti-duplication gate, unchanged);
- wire unchanged: ``Executed type.`` prefix + additive ``integrity=`` suffix;
  ``CORTEX_TYPE_INTEGRITY=0`` legacy path byte-identical.
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
    _landed_prefix_len,
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

    def __init__(self, values: list[str | None], *, control_type: str | None = "Edit") -> None:
        self._values = list(values)
        self._control_type = control_type
        self._last: str | None = None
        self.used = 0

    @property
    def reads_consumed(self) -> int:
        return self.used

    def warm(self) -> bool:
        return True

    def read(self) -> dict[str, object]:
        self.used += 1
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
    stability_seconds: float = 0.0,
    confirm_gap_seconds: float = 0.0,
) -> tuple[RecordingEngine, _ScriptedReader]:
    """Session backend with a recording engine + scripted reader, wait-free by default.

    ``stability_seconds=0``/``confirm_gap_seconds=0`` skip the WAITS (the read pairs
    still run); horizon/gap pins override them and assert on recorded sleeps.
    """
    monkeypatch.setattr(backend_module, "TYPE_VERIFY_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(backend_module, "TYPE_VERIFY_STABILITY_SECONDS", stability_seconds)
    monkeypatch.setattr(backend_module, "TYPE_VERIFY_CONFIRM_GAP_SECONDS", confirm_gap_seconds)
    monkeypatch.setattr(backend_module, "TYPE_CHUNK_CHARS", chunk_chars)
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    reader = _ScriptedReader(reader_values, control_type=control_type)
    monkeypatch.setattr(real_backend, "_semantic_reader", reader)
    return engine, reader


def _type_action(text: str) -> GroundedAction:
    return GroundedAction(action="type", text=text, confidence=1.0)


def _dispatches(engine: RecordingEngine) -> list[str]:
    return [call[1] for call in engine.calls if call[0] == "type_text"]


# --- helpers --------------------------------------------------------------------------------------


def test_split_type_chunks_boundaries() -> None:
    assert _split_type_chunks("", 8) == [""]
    assert _split_type_chunks("abc", 8) == ["abc"]
    assert _split_type_chunks("a" * 8, 8) == ["a" * 8]  # exact multiple: one chunk
    assert _split_type_chunks("a" * 9, 8) == ["a" * 8, "a"]
    assert _split_type_chunks("abcdef", 0) == ["abcdef"]  # degenerate size: whole text


def test_split_type_chunks_keeps_astral_characters_whole() -> None:
    """Chunking is code-point based: an astral character is ONE element, so its
    UTF-16 surrogate pair can never be separated by a chunk boundary."""
    smiley = "\U0001f600"
    text = "a" * 7 + smiley + "b" + smiley
    chunks = _split_type_chunks(text, 8)
    assert "".join(chunks) == text
    for chunk in chunks:
        units = backend_module._text_to_key_units(chunk)
        scans = [scan for _vk, scan, _flags in units[::2]]
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


def test_landed_prefix_len_suffix_diff() -> None:
    assert _landed_prefix_len("abcdef", "abcdef") == 6  # fully landed
    assert _landed_prefix_len("pref-abc", "abcdef") == 3  # pre-existing content tolerated
    assert _landed_prefix_len("abc", "abcdef") == 3  # dropped-suffix signature
    assert _landed_prefix_len("", "abcdef") == 0  # empty: everything missing
    assert _landed_prefix_len("zzz", "abcdef") == 0  # foreign/masked: unrecognizable
    assert _landed_prefix_len("aXc", "abcdef") == 0  # mid-corruption: not appendable


def test_format_integrity_status_statuses() -> None:
    assert _format_integrity_status(6, 6, mismatch=0, unverified=0, healed=0) == "integrity=verified(6/6)"
    assert _format_integrity_status(4, 6, mismatch=0, unverified=2, healed=0) == "integrity=partial(4/6)"
    assert _format_integrity_status(0, 6, mismatch=0, unverified=6, healed=0) == "integrity=unverified(0/6)"
    assert _format_integrity_status(2, 6, mismatch=4, unverified=0, healed=0) == "integrity=mismatch(2/6)"
    assert _format_integrity_status(6, 6, mismatch=0, unverified=0, healed=3) == "integrity=verified(6/6) healed=3"


# --- FAST PATH: two quick reads, no horizon, no repair ----------------------------------------------


def test_verified_on_two_agreeing_reads_within_read_budget(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "a" * 130  # chunk 64 -> 64 + 64 + 2
    engine, reader = _typing_backend(real_backend, monkeypatch, reader_values=[text, text])
    message = real_backend.execute(_type_action(text))
    assert message == "Executed type. integrity=verified(130/130)"
    assert _dispatches(engine) == [text[:64], text[64:128], text[128:]]  # chunks only, NO retype
    assert reader.reads_consumed == 2  # the happy path pays exactly TWO reads


def test_mid_drain_extension_verifies_without_waiting(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE E6 regression pin: read1 at +50 ms is MID-DRAIN (58 of 81 chars), read2 sees
    the completed buffer. read2 EXTENDS read1 (monotone growth = real evidence) and
    matches -> verified on two quick reads, with the horizon never consulted."""
    sleeps: list[float] = []
    monkeypatch.setattr(backend_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    partial = "The quick brown fox jumps over the lazy dog 0123456789"
    full = partial + " pack my box with five dozen liquor jugs."
    engine, reader = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=[partial, full],
        stability_seconds=2.5,
        confirm_gap_seconds=0.075,
    )
    message = real_backend.execute(_type_action(full))
    assert message == f"Executed type. integrity=verified({len(full)}/{len(full)})"
    assert 2.5 not in sleeps  # the lag horizon is NEVER consulted on the fast path
    assert reader.reads_consumed == 2
    assert _dispatches(engine) == [full[:64], full[64:]]  # chunks only, NO retype


def test_verified_against_pre_existing_content_without_baseline_read(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Suffix-diff semantics: pre-existing control content needs NO baseline read."""
    engine, _ = _typing_backend(real_backend, monkeypatch, reader_values=["pref-abc", "pref-abc"])
    message = real_backend.execute(_type_action("abc"))
    assert message == "Executed type. integrity=verified(3/3)"
    assert _dispatches(engine) == ["abc"]


def test_crlf_landing_matches_typed_newline(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = _typing_backend(real_backend, monkeypatch, reader_values=["a\r\nb", "a\r\nb"])
    message = real_backend.execute(_type_action("a\nb"))
    assert message == "Executed type. integrity=verified(3/3)"
    assert _dispatches(engine) == ["a\nb"]


def test_blind_reads_are_unverified_and_never_repaired(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, reader = _typing_backend(
        real_backend, monkeypatch, reader_values=[None], control_type=None
    )
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6)"
    assert _dispatches(engine) == ["abcdef"]
    assert reader.reads_consumed == 1  # read1 blind: no second read needed


def test_agreed_empty_buffer_is_unverified_and_never_repaired(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agreed EMPTY trusted buffer cannot be repaired by appending with confidence
    (the whole text would be re-sent on unproven grounds): unverified, never repair."""
    engine, _ = _typing_backend(real_backend, monkeypatch, reader_values=["", ""])
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6)"
    assert _dispatches(engine) == ["abcdef"]


def test_masked_transformed_target_is_unverified_without_retype(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-empty agreed buffer sharing no expected suffix (masked field /
    autocorrect / still-pending drain of pre-existing content): the honest fast
    answer is unverified — appending would double-apply, so NO repair and no claim."""
    engine, _ = _typing_backend(real_backend, monkeypatch, reader_values=["zzz", "zzz", "zzz", "zzz"])
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6)"
    assert _dispatches(engine) == ["abcdef"]


def test_growth_outliving_the_fast_budget_is_unverified(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Budget exhausted while the buffer is still growing (never completed, never
    stable): honest unverified — no horizon wait, no repair, no false claim."""
    engine, reader = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=["a" * 30, "a" * 45, "a" * 60, "a" * 75],
        stability_seconds=2.5,
    )
    message = real_backend.execute(_type_action("a" * 81))
    assert message == "Executed type. integrity=unverified(0/81)"
    assert reader.reads_consumed == 4  # the whole fast budget, then honest surrender
    assert _dispatches(engine) == ["a" * 64, "a" * 17]  # chunks only, NO retype


# --- ESCALATION: suspicious pairs and agreed partials -----------------------------------------------


def test_divergent_pair_escalates_through_the_horizon(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read2 that neither equals nor extends read1 is suspicious: the horizon pair
    decides. Here it settles on a foreign buffer -> mismatch, no repair."""
    sleeps: list[float] = []
    monkeypatch.setattr(backend_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    engine, _ = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=["abcdefX", "zzzz", "zzzz", "zzzz"],
        stability_seconds=1.5,
        confirm_gap_seconds=0.05,
    )
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=mismatch(0/6)"
    assert 1.5 in sleeps  # the escalation consulted the landing-lag horizon
    assert _dispatches(engine) == ["abcdef"]


def test_divergent_pair_horizon_blind_is_unverified(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = _typing_backend(
        real_backend, monkeypatch, reader_values=["abcdefX", "zzzz", None]
    )
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6)"
    assert _dispatches(engine) == ["abcdef"]


def test_agreed_partial_confirmed_stable_heals_exact_suffix(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agreed genuine partial (stable across the horizon) -> repair EXACTLY the
    missing suffix once; the quick recheck pair confirms -> verified + healed."""
    engine, _ = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=["abc", "abc", "abc", "abc", "abcdef", "abcdef"],
        stability_seconds=1.0,
    )
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=verified(6/6) healed=3"
    assert _dispatches(engine) == ["abcdef", "def"]


def test_agreed_partial_that_drains_during_horizon_never_repairs(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANTI-DUPLICATION pin: an agreed partial that turns out to be MID-DRAIN (the
    horizon read sees the completed buffer) verifies WITHOUT any retype — the drain
    landing can never be double-applied."""
    engine, _ = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=["abc", "abc", "abcdef", "abcdef"],
        stability_seconds=1.0,
    )
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=verified(6/6)"
    assert _dispatches(engine) == ["abcdef"]  # NO retype: duplication impossible


def test_shrinking_pair_settles_to_stable_partial_then_heals(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shrinking pair is suspicious -> horizon pair settles the truth (a stable
    partial) -> the one verified suffix retype -> confirmed -> verified + healed."""
    engine, _ = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=["abcdefZZ", "abc", "abc", "abc", "abcdef", "abcdef"],
        stability_seconds=1.0,
    )
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=verified(6/6) healed=3"
    assert _dispatches(engine) == ["abcdef", "def"]


def test_shrinking_pair_horizon_blind_is_unverified(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = _typing_backend(
        real_backend, monkeypatch, reader_values=["abcdef", "abc", None]
    )
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6)"
    assert _dispatches(engine) == ["abcdef"]


def test_confirmed_still_wrong_after_repair_raises_and_never_repeats(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=["abc", "abc", "abc", "abc", "abc", "abc"],
        stability_seconds=1.0,
    )
    with pytest.raises(TextIntegrityError, match="verified retype"):
        real_backend.execute(_type_action("abcdef"))
    assert _dispatches(engine) == ["abcdef", "def"]  # exactly one retype; no third dispatch


def test_unconfirmed_repair_reports_unverified_without_raise(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repair landing unconfirmable (unstable recheck): honest unverified, no raise,
    no second retype."""
    engine, _ = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=["abc", "abc", "abc", "abc", "abcdef", "X"],
        stability_seconds=1.0,
    )
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6)"
    assert _dispatches(engine) == ["abcdef", "def"]


# --- honest degradation ----------------------------------------------------------------------------


def test_valueless_non_edit_control_stays_unverified(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = _typing_backend(
        real_backend, monkeypatch, reader_values=[None], control_type="Custom"
    )
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6)"
    assert _dispatches(engine) == ["abcdef"]


def test_empty_edit_reads_as_empty_and_unverified_when_it_stays_empty(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A None value on an ALWAYS-VALUED control (Edit) means EMPTY, not unreadable;
    an empty buffer that stays empty across both quick reads is honest unverified."""
    engine, _ = _typing_backend(real_backend, monkeypatch, reader_values=[None, None])
    message = real_backend.execute(_type_action("Z"))
    assert message == "Executed type. integrity=unverified(0/1)"
    assert _dispatches(engine) == ["Z"]


# --- stop-token and focus-hook interplay -------------------------------------------------------------


def test_pre_stopped_type_dispatches_nothing(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = _typing_backend(real_backend, monkeypatch, reader_values=[])
    stop = StopToken()
    stop.stop()
    with pytest.raises(TaskStopped):
        real_backend.execute(_type_action("abcdef"), stop)
    assert engine.calls == []


def test_stop_token_between_chunks_aborts_remaining_chunks(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = _typing_backend(
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
    assert _dispatches(engine) == ["ab", "cd"]  # chunk 3 never dispatched after the stop fired


def test_focus_hook_rides_every_chunk(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = _typing_backend(
        real_backend, monkeypatch, reader_values=[None], chunk_chars=2, control_type=None
    )
    hooks: list[int] = []

    def hook() -> None:
        hooks.append(1)

    message = real_backend.execute(_type_action("abcd"), None, focus_hook=hook)
    assert message == "Executed type. integrity=unverified(0/4)"
    assert len(hooks) == 2  # one per chunk (T8 focus-continuity cadence preserved)
    assert _dispatches(engine) == ["ab", "cd"]


# --- kill switch and legacy message -------------------------------------------------------------------


def test_integrity_disabled_restores_legacy_path(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = _typing_backend(real_backend, monkeypatch, reader_values=[])
    monkeypatch.setattr(backend_module, "TYPE_INTEGRITY_ENABLED", False)
    message = real_backend.execute(_type_action("a\nb"))
    assert message == "Executed type."  # byte-identical pre-R-04 message
    assert _dispatches(engine) == ["a\nb"]  # ONE dispatch


def test_empty_text_stays_legacy_noop(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = _typing_backend(real_backend, monkeypatch, reader_values=[])
    assert real_backend.execute(_type_action("")) == "Executed type."
    assert _dispatches(engine) == [""]  # one no-op dispatch


# --- FakeComputerBackend parity ------------------------------------------------------------------------


def test_fake_backend_type_integrity_contract() -> None:
    backend = FakeComputerBackend()
    # unreadable by default: honest unverified, action still recorded.
    # v0.7.1 (Defect C): focused_control_value=None IS the simulated no-readable-target
    # (Blender) shape, so the additive typed diagnostic is now part of the expected
    # payload (prefix + integrity suffix unchanged).
    message = backend.execute(_type_action("abc"))
    assert message == (
        "Simulated type. integrity=unverified(0/3) TYPE_UNCONFIRMED no-readable-target "
        '(verify visually with a stated expected_effect (or retry with via="clipboard"))'
    )
    assert [item.action.value for item in backend.executed] == ["type"]
    # matching value: verified
    backend.focused_control_value = "abc"
    assert backend.execute(_type_action("abc")) == "Simulated type. integrity=verified(3/3)"
    # pre-existing content tolerated by suffix-diff semantics
    backend.focused_control_value = "log: abc"
    assert backend.execute(_type_action("abc")) == "Simulated type. integrity=verified(3/3)"
    # dropped suffix: simulated verified retype heals with the EXACT missing suffix
    backend.focused_control_value = "a"
    assert backend.execute(_type_action("abc")) == "Simulated type. integrity=verified(3/3) healed=2"
    assert backend.focused_control_value == "abc"  # control now holds the full text
    # agreed empty buffer: unverified, never repaired
    backend.focused_control_value = ""
    assert backend.execute(_type_action("abc")) == "Simulated type. integrity=unverified(0/3)"
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
