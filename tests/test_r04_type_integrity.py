"""R-04 typed-text integrity, fix2 redesign: END-OF-ACTION verification only.

Live A/B evidence (evidence/v06-006/live-desktop, 2026-09-13) caught the wave-1 design
acting on MID-DISPATCH reads: on this machine's input stack a read taken right after a
dispatch can be stale-short while the burst drains late, so the repair retype
double-applied (buffers like `#00 alpha 0. end00 alpha 0. end`). The fix2 contract:

- NO read-backs interleaved mid-dispatch — verify once, after the final chunk settles;
- read truthfulness: two consecutive reads must AGREE before any value is trusted;
  the second read of any non-verified verdict is taken after the landing-lag horizon
  (``TYPE_VERIFY_STABILITY_SECONDS``); disagreement/shrink/missing -> ``unverified``;
- ``unverified`` NEVER triggers a retype (this kills the duplication class — pinned by
  the lag-drain test below);
- a genuine partial repairs with EXACTLY the missing suffix diffed from the REAL
  buffer (suffix-diff semantics, no baseline read — pre-existing content tolerated);
  still wrong after the one verified retype -> typed TextIntegrityError;
- wire contract unchanged: ``Executed type.`` prefix + additive ``integrity=``
  suffix; ``CORTEX_TYPE_INTEGRITY=0`` restores the legacy path byte-identically.
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
    stability_seconds: float = 0.0,
) -> RecordingEngine:
    """Session backend with a recording engine + scripted reader, settle-free.

    ``stability_seconds=0`` skips the horizon WAIT (the double-read itself still runs);
    tests that pin the horizon behavior override it.
    """
    monkeypatch.setattr(backend_module, "TYPE_VERIFY_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(backend_module, "TYPE_VERIFY_STABILITY_SECONDS", stability_seconds)
    monkeypatch.setattr(backend_module, "TYPE_CHUNK_CHARS", chunk_chars)
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(
        real_backend, "_semantic_reader", _ScriptedReader(reader_values, control_type=control_type)
    )
    return engine


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
    UTF-16 surrogate pair can never be separated by a chunk boundary (the event-level
    surrogate encoding is the engine's concern and stays within the chunk)."""
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


# --- verified path (end-of-action, double-read trust) ----------------------------------------------


def test_verified_end_of_action_with_double_read_agreement(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "a" * 130  # chunk 64 -> 64 + 64 + 2
    engine = _typing_backend(real_backend, monkeypatch, reader_values=[text, text])
    message = real_backend.execute(_type_action(text))
    assert message == "Executed type. integrity=verified(130/130)"
    assert _dispatches(engine) == [text[:64], text[64:128], text[128:]]  # chunks only, NO retype


def test_verified_against_pre_existing_content_without_baseline_read(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Suffix-diff semantics: pre-existing control content needs NO baseline read."""
    engine = _typing_backend(real_backend, monkeypatch, reader_values=["pref-abc", "pref-abc"])
    message = real_backend.execute(_type_action("abc"))
    assert message == "Executed type. integrity=verified(3/3)"
    assert _dispatches(engine) == ["abc"]


def test_crlf_landing_matches_typed_newline(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _typing_backend(real_backend, monkeypatch, reader_values=["a\r\nb", "a\r\nb"])
    message = real_backend.execute(_type_action("a\nb"))
    assert message == "Executed type. integrity=verified(3/3)"
    assert _dispatches(engine) == ["a\nb"]


def test_reads_must_agree_even_on_the_verified_path(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read1 looks verified but read2 disagrees -> untrusted -> honest unverified."""
    engine = _typing_backend(real_backend, monkeypatch, reader_values=["abcdef", "Xabcdef"])
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6)"
    assert _dispatches(engine) == ["abcdef"]  # never repaired on untrusted reads


# --- lag guard: the anti-duplication gate -----------------------------------------------------------


def test_lag_drain_between_reads_is_unverified_and_never_repairs(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE live-defect pin: read1 stale-short, read2 longer (the burst drained during
    the horizon). The wave-1 design retyped on read1 and DOUBLE-APPLIED when the drain
    landed; fix2 treats the disagreement as unverified and NEVER repairs."""
    engine = _typing_backend(real_backend, monkeypatch, reader_values=["abc", "abcdef"])
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6)"
    assert _dispatches(engine) == ["abcdef"]  # exactly one dispatch: duplication impossible


def test_shrinking_buffer_between_reads_is_unverified(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _typing_backend(real_backend, monkeypatch, reader_values=["abcdef", "abc"])
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6)"
    assert _dispatches(engine) == ["abcdef"]


def test_horizon_wait_actually_guards_the_repair_decision(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lag-guard read is taken after TYPE_VERIFY_STABILITY_SECONDS (the observed
    landing-lag horizon), not back-to-back with the first read."""
    sleeps: list[float] = []
    monkeypatch.setattr(backend_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    engine = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=["abc", "abc", "abcdef", "abcdef"],
        stability_seconds=1.5,
    )
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=verified(6/6) healed=3"
    assert 1.5 in sleeps  # the lag guard waited out the horizon before trusting
    assert _dispatches(engine) == ["abcdef", "def"]


# --- genuine partial: the ONE verified suffix retype -------------------------------------------------


def test_genuine_partial_repairs_exact_missing_suffix(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _typing_backend(real_backend, monkeypatch, reader_values=["abc", "abc", "abcdef", "abcdef"])
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=verified(6/6) healed=3"
    assert _dispatches(engine) == ["abcdef", "def"]  # the chunk, then EXACTLY the missing suffix


def test_empty_stable_buffer_repairs_whole_text(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _typing_backend(real_backend, monkeypatch, reader_values=["", "", "abcdef", "abcdef"])
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=verified(6/6) healed=6"
    assert _dispatches(engine) == ["abcdef", "abcdef"]


def test_confirmed_still_wrong_after_repair_raises_and_never_repeats(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _typing_backend(real_backend, monkeypatch, reader_values=["abc", "abc", "abc", "abc"])
    with pytest.raises(TextIntegrityError, match="verified retype"):
        real_backend.execute(_type_action("abcdef"))
    assert _dispatches(engine) == ["abcdef", "def"]  # exactly one retype; no third dispatch


def test_unconfirmed_repair_reports_unverified_without_raise(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repair landing unconfirmable (unstable recheck): honest unverified, no raise,
    no second retype."""
    engine = _typing_backend(real_backend, monkeypatch, reader_values=["abc", "abc", "abcdef", "X"])
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6)"
    assert _dispatches(engine) == ["abcdef", "def"]


# --- honest degradation ----------------------------------------------------------------------------


def test_unreadable_control_reports_unverified_and_skips_repair(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _typing_backend(
        real_backend, monkeypatch, reader_values=[None], control_type=None
    )
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6)"
    assert _dispatches(engine) == ["abcdef"]


def test_empty_edit_control_reads_as_empty_not_unreadable(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A None value on an ALWAYS-VALUED control (Edit) means EMPTY, not unreadable:
    an empty stable buffer is a genuine partial (whole text missing)."""
    engine = _typing_backend(real_backend, monkeypatch, reader_values=[None, None, "Z", "Z"])
    message = real_backend.execute(_type_action("Z"))
    assert message == "Executed type. integrity=verified(1/1) healed=1"
    assert _dispatches(engine) == ["Z", "Z"]


def test_valueless_non_edit_control_stays_unverified(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _typing_backend(
        real_backend, monkeypatch, reader_values=[None], control_type="Custom"
    )
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6)"
    assert _dispatches(engine) == ["abcdef"]


def test_masked_transformed_target_reports_mismatch_without_retype(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-empty trusted buffer sharing no expected suffix: appending would
    double-apply (masked field / autocorrect) -> mismatch, NO repair."""
    engine = _typing_backend(real_backend, monkeypatch, reader_values=["zzz", "zzz"])
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=mismatch(0/6)"
    assert _dispatches(engine) == ["abcdef"]


# --- stop-token and focus-hook interplay -------------------------------------------------------------


def test_pre_stopped_type_dispatches_nothing(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _typing_backend(real_backend, monkeypatch, reader_values=[])
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
    assert _dispatches(engine) == ["ab", "cd"]  # chunk 3 never dispatched after the stop fired


def test_focus_hook_rides_every_chunk(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _typing_backend(
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
    engine = _typing_backend(real_backend, monkeypatch, reader_values=[])
    monkeypatch.setattr(backend_module, "TYPE_INTEGRITY_ENABLED", False)
    message = real_backend.execute(_type_action("a\nb"))
    assert message == "Executed type."  # byte-identical pre-R-04 message
    assert _dispatches(engine) == ["a\nb"]  # ONE dispatch


def test_empty_text_stays_legacy_noop(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _typing_backend(real_backend, monkeypatch, reader_values=[])
    assert real_backend.execute(_type_action("")) == "Executed type."
    assert _dispatches(engine) == [""]  # one no-op dispatch


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
    # pre-existing content tolerated by suffix-diff semantics
    backend.focused_control_value = "log: abc"
    assert backend.execute(_type_action("abc")) == "Simulated type. integrity=verified(3/3)"
    # dropped suffix: simulated verified retype heals with the EXACT missing suffix
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
