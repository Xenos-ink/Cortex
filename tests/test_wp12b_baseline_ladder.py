"""WP12b offline pins: the BASELINE-GATED repair ladder + the per-action deadline.

VAL-WP12 blockers/majors pinned here, all hermetic (stubbed reader + engine):

- B1: the suffix-append repair fires ONLY when the horizon-confirmed trusted
  buffer is exactly the pre-dispatch BASELINE plus a proper prefix of the typed
  text. Every other shape names its branch and never retypes:
  * 'tett'/'brown xxx' (positive divergence) -> mismatch (pinned in
    tests/test_wp12a_type_pacing.py);
  * masked '●●●●' -> unverified branch=transformed_buffer (r04);
  * 'at'+nothing -> unverified branch=not_landed, no repair;
  * caret mid-text with a matching baseline -> verified; without -> no_baseline /
    landed_not_at_tail;
  * an unreadable baseline -> branch=no_baseline, never repair.
- M1/M2: a repair that does not fit the remaining wall-clock deadline is never
  typed (branch=repair_over_budget), and the capped interval is restored on EVERY
  exit path through ``execute()`` (FocusDriftError, TaskStopped, success).
"""

from __future__ import annotations

import pytest
from recording_engine import RecordingEngine

import computer_use_mcp.desktop.backend as backend_module
from computer_use_mcp.desktop.backend import LocalComputerBackend
from computer_use_mcp.desktop.errors import FocusDriftError
from computer_use_mcp.core.state import StopToken, TaskStopped

from test_r04_type_integrity import _dispatches, _type_action, _typing_backend


def test_nothing_landed_on_preexisting_content_is_not_landed(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1(d) 'at'+nothing: the buffer still EQUALS the non-empty baseline ('at') —
    nothing of ours landed. k == 0 is excluded from the sound-repair rule: honest
    unverified branch=not_landed, NO repair dispatch."""
    engine, _ = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=["at", "at", "at", "at", "at"],
        stability_seconds=1.0,
    )
    message = real_backend.execute(_type_action("test"))
    assert message == "Executed type. integrity=unverified(0/4) branch=not_landed"
    assert _dispatches(engine) == ["test"]


def test_baseline_none_means_no_baseline_and_never_repair(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable baseline can never attribute the buffer's new characters:
    branch=no_baseline and no repair, whatever the tail looks like."""
    engine, _ = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=[None, "zzz", "zzz", "zzz", "zzz"],
        control_type=None,
        stability_seconds=1.0,
    )
    message = real_backend.execute(_type_action("test"))
    assert message == "Executed type. integrity=unverified(0/4) branch=no_baseline"
    assert _dispatches(engine) == ["test"]


def test_landed_at_the_caret_with_matching_baseline_verifies(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1(d): the typed text landed at the caret INSIDE the baseline ('hello' +
    ' there' + ' world') — provable with the baseline -> verified, not a failure."""
    engine, _ = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=["hello world", "hello world", "hello there world", "hello there world"],
        stability_seconds=1.0,
    )
    message = real_backend.execute(_type_action(" there"))
    assert message == "Executed type. integrity=verified(6/6)"
    assert _dispatches(engine) == [" there"]


def test_landed_not_at_tail_without_matching_baseline_is_unverified(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1(d): the typed text appears in the buffer but NOT at the tail (caret
    mid-text / autocomplete); the baseline was BLIND so the insertion cannot be
    proven -> honest unverified branch=landed_not_at_tail (never mismatch, never
    repair)."""
    engine, _ = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=[None, "hello there world", "hello there world", "hello there world"],
        control_type=None,
        stability_seconds=1.0,
    )
    message = real_backend.execute(_type_action(" there"))
    assert message == "Executed type. integrity=unverified(0/6) branch=landed_not_at_tail"
    assert _dispatches(engine) == [" there"]


def test_over_budget_repair_is_never_typed(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1/M2: a repair that does not fit the remaining wall-clock deadline is
    NEVER typed — branch=repair_over_budget, dispatches = initial chunks only."""
    engine, _ = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=["", "abc", "abc", "abc", "abc", "abcdef", "abcdef"],
        stability_seconds=0.0,
    )
    # Shrink the deadline to nothing: by the time the repair is considered, the
    # remaining wall clock cannot possibly fund it. DELTA-3 m7: the ladder's
    # per-read budget checks use the semantic READ wall — zero it out here so
    # the reads stay affordable and the ladder reaches the REPAIR decision,
    # whose own (settle + 2 s) reserve still cannot fit the dead deadline.
    monkeypatch.setattr(backend_module, "type_action_deadline_seconds", lambda: 1e-9)
    monkeypatch.setattr(backend_module, "semantic_read_timeout_seconds", lambda: 0.0)
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6) branch=repair_over_budget"
    assert _dispatches(engine) == ["abcdef"]


# --- the deadline/cap survives every exit path -------------------------------------------------


class _DriftThenRecordEngine(RecordingEngine):
    """Records the engine's ``type_interval`` at each type_text call, then raises
    FocusDriftError on the FIRST call (the mid-type focus-drift shape)."""

    def __init__(self) -> None:
        super().__init__()
        self.intervals_at_call: list[float] = []

    def type_text(self, text: str, before_chunk: object = None) -> None:
        self.intervals_at_call.append(self.type_interval)
        raise FocusDriftError("mid-type focus drift")


def test_capped_interval_is_restored_after_focus_drift(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP12c M1/M2 restore pin (FocusDriftError), over a LONG text where the cap
    actually lowers the interval below the configured one (>1800 characters): the
    engine keeps the CAPPED interval for the whole dispatch and the CONFIGURED
    interval is restored afterwards."""
    drift_engine = _DriftThenRecordEngine()
    drift_engine.type_interval = 0.02  # the configured (backend default) pace
    monkeypatch.setattr(real_backend, "_engine", drift_engine)
    monkeypatch.setattr(real_backend, "_semantic_reader", _silent_reader())
    long_text = "x" * 2000  # 2000 sleeps -> capped = 36/2000 = 0.018 != configured
    with pytest.raises(FocusDriftError):
        real_backend.execute(_type_action(long_text))
    assert drift_engine.intervals_at_call == [pytest.approx(0.018)]  # capped pace in use
    assert real_backend._engine.type_interval == pytest.approx(0.02)  # configured restored


def test_capped_interval_is_restored_after_task_stop(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP12e G1 (VAL-WP12-06 NIT), done properly per WP13 item G: the stop fires
    MID-DISPATCH — a focus hook arms it after the first character group, so the
    SECOND group's ``before_chunk`` chain (``stop.ensure_live``) raises
    TaskStopped INSIDE the dispatch try, over a LONG text where the cap actually
    lowers the interval: the CAPPED pace (0.018) was in use during the dispatch
    and the CONFIGURED pace (0.02) is restored."""
    intervals_at_call: list[float] = []
    stop = StopToken()

    class _HookCallingEngine(RecordingEngine):
        def type_text(self, text: str, before_chunk: object = None) -> None:
            intervals_at_call.append(self.type_interval)
            if before_chunk is not None:
                before_chunk()  # the backend's chain: stop.ensure_live raises here

    hooking_engine = _HookCallingEngine()
    hooking_engine.type_interval = 0.02  # the configured (backend default) pace
    monkeypatch.setattr(real_backend, "_engine", hooking_engine)
    monkeypatch.setattr(real_backend, "_semantic_reader", _silent_reader())

    def _arming_hook() -> None:
        stop.stop()  # the driver-side hook arms the stop after the first group

    with pytest.raises(TaskStopped):
        real_backend.execute(_type_action("x" * 2000), stop, focus_hook=_arming_hook)
    assert len(intervals_at_call) >= 1  # at least one group dispatched
    assert all(iv == pytest.approx(0.018) for iv in intervals_at_call)  # capped pace
    assert hooking_engine.type_interval == pytest.approx(0.02)  # restored


def test_cap_is_not_applied_when_the_stop_fires_before_it(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP12c VAL C1: the cap is applied AFTER the stop-check and the baseline read
    — a stop arriving during the focus settle (before the cap) must leave the
    engine's configured interval untouched (no capped value to leak)."""
    engine = RecordingEngine()
    engine.type_interval = 0.02  # configured
    monkeypatch.setattr(real_backend, "_engine", engine)

    def _raise_stop() -> None:
        raise TaskStopped("stop during the focus settle")

    monkeypatch.setattr(real_backend, "_apply_focus_settle", _raise_stop)
    with pytest.raises(TaskStopped):
        real_backend.execute(_type_action("x" * 2000))
    assert engine.type_interval == pytest.approx(0.02)  # never capped, never leaked


def test_repair_does_not_leak_the_capped_interval(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP12c VAL C1/leak MAJOR: a repaired LONG text (capped 0.018 during the
    dispatch AND the repair) must leave the CONFIGURED 0.02 on the engine — the
    WP12b repair path leaked the capped value for the rest of the session."""
    engine = RecordingEngine()
    engine.type_interval = 0.02  # configured
    monkeypatch.setattr(real_backend, "_engine", engine)
    text = "x" * 2000
    partial = "x" * 1900
    reader_values = ["", partial, partial, partial, partial, text, text]
    reads: list[str] = list(reader_values)

    def _scripted_read():
        return reads.pop(0) if len(reads) > 1 else reads[-1]

    monkeypatch.setattr(real_backend, "_semantic_reader", _silent_reader())
    monkeypatch.setattr(real_backend, "_read_landed_normalized", _scripted_read)
    message = real_backend.execute(_type_action(text))
    assert "healed=100" in message, message
    assert engine.type_interval == pytest.approx(0.02)  # configured restored after repair


def _silent_reader():
    """A minimal always-blind semantic reader (the baseline/verification reads
    return None; no input is ever dispatched)."""
    from types import SimpleNamespace

    reader = SimpleNamespace(available=True, warm=lambda: True, read=lambda: None)
    return reader


# --- WP12d N1: a RAW read at the WM_GETTEXT truncation cap is BLIND -------------------


def test_read_at_the_raw_truncation_cap_is_blind_and_never_repaired(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP12d N1 BLOCKER (VAL-WP12-03): a RAW read at the reader's 8192-char
    WM_GETTEXT cap is TRUNCATED — the document holds 8150 + 100 chars but every
    read returns only the first 8192, which looks like 'baseline + a proper
    prefix of the typed text' and used to trigger a repair that typed the last
    58 characters a SECOND time. With N1, every capped read is blind: the ladder
    answers unverified (branch=no_baseline) and NEVER repairs — and
    TextIntegrityError can never fire either (the duplicate would only be
    discoverable after it was typed). The REAL ``_read_landed_normalized`` runs
    (only its RAW source ``read_focused_control_value`` is scripted)."""
    E = "E" * 100
    baseline = "a" * 8150
    truncated = (baseline + E[:42])[:8192]  # what the capped reader returns
    raws = [baseline, truncated, truncated, truncated, truncated, truncated]

    def _scripted_raw():
        return raws.pop(0) if len(raws) > 1 else raws[-1]

    engine, _ = _typing_backend(real_backend, monkeypatch, reader_values=[], control_type=None)
    monkeypatch.setattr(real_backend, "read_focused_control_value", _scripted_raw)
    message = real_backend.execute(_type_action(E))
    # WP12e: every capped read is blind AND flagged — the FAST path answers first
    # with branch=read_truncated (no_baseline is reachable only through escalation
    # when a trusted buffer is readable).
    assert "integrity=unverified(0/100)" in message and "branch=read_truncated" in message, message
    # NO repair dispatch: the two dispatches are the 64-char CHUNKS of E only.
    assert _dispatches(engine) == [E[:64], E[64:]]


def test_read_with_crlf_at_the_raw_cap_is_blind_despite_shorter_normalized(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1 completion: line-ending folding SHORTENS the text, so the cap check must
    use the RAW length — a raw read at the cap with CRLF endings (normalized
    length well under 8192) is still blind, not a 'known' baseline."""
    raw = ("ab\r\n" * 2200)[:8192]  # raw len == 8192; normalized would be shorter
    assert len(raw) == 8192
    engine, _ = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=[],
        control_type=None,
        stability_seconds=0.0,
    )
    monkeypatch.setattr(real_backend, "read_focused_control_value", lambda: raw)
    message = real_backend.execute(_type_action("test"))
    assert "integrity=unverified(0/4)" in message and "branch=read_truncated" in message, message
    assert _dispatches(engine) == ["test"]


def test_uia_values_are_not_cap_blind_and_verify_normally(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP12e (VAL MINOR): the 8192-char blind rule applies ONLY to the truncating
    Win32 WM_GETTEXT reader. A UIA reader's 9000-char value is NOT truncated: the
    ladder verifies it normally on a proper landing (typed text appended to the
    baseline — the insertion form holds) — no blind_read, no read_truncated."""
    baseline = "x" * 7100
    E = "u" * 900  # 7100 + 900 = 9000 chars — at the cap, but UIA does not truncate
    engine, reader = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=[baseline, baseline + E, baseline + E],
        control_type=None,
    )
    reader.value_source = "uia"  # the reader REPORTS its path (UIA, not truncated)
    message = real_backend.execute(_type_action(E))
    assert f"integrity=verified({len(E)}/{len(E)})" in message, message
    assert "read_truncated" not in message, message
    # the stub records the backend's 64-char chunk dispatches (pacing happens
    # INSIDE the real engine — orthogonal to this verdict pin)
    assert _dispatches(engine) == [E[i : i + 64] for i in range(0, len(E), 64)]


def test_win32_value_at_the_cap_is_read_truncated_and_never_repaired(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP12e (VAL MINOR): a Win32 WM_GETTEXT value at the cap is blind AND flagged —
    the ladder answers branch=read_truncated (never repaired, never verified)."""
    E = "END"
    capped = "x" * 8997 + E  # 9000 chars at the cap: the typed tail is cut off
    engine, reader = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=["", capped, capped, capped, capped],
        control_type=None,
    )
    reader.value_source = "win32"
    message = real_backend.execute(_type_action(E))
    assert message == "Executed type. integrity=unverified(0/3) branch=read_truncated", (
        message
    )
    assert _dispatches(engine) == [E]  # chunks only — never repaired
