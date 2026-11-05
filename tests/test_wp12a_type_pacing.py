"""WP12a offline pins: default typing pace + honest corruption flagging.

User ruling (2026-11-02, via the Commander): "Pause by default + flag corruption".
Live runs 4-7 proved Windows 11 packaged Notepad (RichEditD2DPT, Arabic layout)
corrupts/drops fast unicode bursts even without the product, and per-character
pacing fixes it. This module pins, hermetically:

- the DEFAULT pace: the REAL backend's engine paces at
  ``backend.DEFAULT_TYPE_INTERVAL_SECONDS`` (0.02 s/char) by default; the standalone
  ``SendInputEngine``/``FakeComputerBackend`` library defaults stay 0.0 (the offline
  suite stays fast); ``CORTEX_TYPE_INTERVAL`` still overrides and ``0`` is unpaced;
- the per-call PACED-BUDGET cap (``PACING_BUDGET_SECONDS`` within the 45 s
  per-action deadline, sleeps counted per event pair): very long texts pace
  below the default (2000 BMP chars at 18 ms, 2000 astral at 9 ms — disclosed);
- the REAL engine path uses the paced per-character chunking by default (stubbed
  SendInput: one character per call);
- the verification ladder (WP12b baseline-gated semantics) flags a SUBSTITUTED
  suffix — baseline plus a correct leading part that then diverges — as
  ``mismatch`` with ZERO retype, keeps the sound repair for the
  genuinely-missing case (baseline + a proper typed prefix), and every
  ``unverified`` reply names WHICH ladder branch gave up (``branch=`` inside
  the message — docs/REPLY-CONTRACT.md sanctions key=value message suffixes).
"""

from __future__ import annotations

import ctypes
from typing import Any

import pytest

import computer_use_mcp.desktop.backend as backend_module
import computer_use_mcp.desktop.input_engine as input_engine_module
from computer_use_mcp.desktop.backend import LocalComputerBackend

from computer_use_mcp.desktop.input_engine import _text_to_key_units

from test_r04_type_integrity import _dispatches, _type_action, _typing_backend


# --- default pace -----------------------------------------------------------------------------------


def test_default_pace_values_pinned_from_code(
    real_backend: LocalComputerBackend,
) -> None:
    """WP12a ruling: the REAL backend paces by default (0.02 s/char); the standalone
    engine library default stays 0.0 (offline tests stay fast) and the fake backend
    has no pacing at all."""
    assert backend_module.DEFAULT_TYPE_INTERVAL_SECONDS == 0.02
    assert input_engine_module.SENDINPUT_TYPE_INTERVAL == 0.0
    # the backend-BUILT engine (the real product path) carries the paced default;
    # a standalone SendInputEngine() does not.
    assert real_backend._engine.type_interval == pytest.approx(0.02)
    assert input_engine_module.SendInputEngine().type_interval == 0.0


def test_env_override_band(monkeypatch: pytest.MonkeyPatch, real_backend: LocalComputerBackend) -> None:
    """CORTEX_TYPE_INTERVAL still overrides the default, and ``0`` is accepted
    (= the unpaced burst) — the band stays nonnegative floats."""
    monkeypatch.setenv("CORTEX_TYPE_INTERVAL", "0")
    unpaced = real_backend._build_input_engine(None, None, None, None)
    assert unpaced.type_interval == 0.0
    monkeypatch.setenv("CORTEX_TYPE_INTERVAL", "0.05")
    tuned = real_backend._build_input_engine(None, None, None, None)
    assert tuned.type_interval == pytest.approx(0.05)


def test_real_engine_path_chunks_per_character_by_default(
    monkeypatch: pytest.MonkeyPatch, real_backend: LocalComputerBackend
) -> None:
    """The paced engine dispatches ONE CHARACTER per SendInput call (the paced
    chunk_size-2 path, input_engine.py:668) with the exact cbSize SendInput needs."""
    calls: list[tuple[int, int, bytes]] = []

    class _FakeUser32:
        def SendInput(self, count: int, array: Any, cb_size: int) -> int:  # type: ignore[name-defined]
            calls.append((count, cb_size, bytes(array)))
            return count

        def GetCursorPos(self, lp_point: Any) -> int:
            point = lp_point._obj
            point.x, point.y = 960, 540  # center: NOT a failsafe corner
            return 1

        def GetSystemMetrics(self, index: int) -> int:
            return (1920, 1080)[index]

    monkeypatch.setattr(input_engine_module, "_user32", _FakeUser32())
    # WP12b VAL NIT: take the failsafe OFF the engine under test — its cursor probe
    # reads the REAL mouse outside the stubbed seam (corner-flake class).
    monkeypatch.setattr(real_backend._engine, "failsafe", False)
    real_backend._engine.type_text("ab")  # the backend-BUILT (paced) engine
    assert [entry[0] for entry in calls] == [2, 2], "one character (down+up) per call"
    assert all(entry[1] == ctypes.sizeof(input_engine_module._INPUT) for entry in calls)


# --- paced-budget cap (WP12b M1/M2: sleeps counted per EVENT PAIR) -------------------------------------


def test_paced_budget_is_capped_for_the_longest_allowed_text(
    real_backend: LocalComputerBackend,
) -> None:
    """WP12c (VAL C4) arithmetic: the paced engine sleeps once per CHARACTER (a
    surrogate pair travels in ONE SendInput call with ONE sleep), so the cap
    divides the pacing budget by ``len(text)`` — identically for BMP and astral.

    - 2000 BMP characters -> 2000 sleeps -> effective = 36 / 2000 = 0.018 (18 ms).
    - 2000 ASTRAL characters -> 2000 sleeps (the surrogate pair is ONE group) ->
      effective = 36 / 2000 = 0.018 — the same proven-safe pace for astral text
      (the WP12b per-pair counting gave 9 ms, below any evidence; fixed).
    Both restore the configured interval afterwards."""
    assert backend_module.PACING_BUDGET_SECONDS == 36.0
    bmp = "x" * 2000
    effective = real_backend._cap_type_interval(bmp)
    assert effective == pytest.approx(36.0 / 2000)  # 18 ms — disclosed in the docs
    real_backend._restore_type_interval()

    astral = "\U0001f600" * 2000
    effective_astral = real_backend._cap_type_interval(astral)
    assert effective_astral == pytest.approx(36.0 / 2000)  # 18 ms — same as BMP now
    real_backend._restore_type_interval()
    assert real_backend._engine.type_interval == pytest.approx(0.02)  # restored


def test_short_text_keeps_the_full_default_interval(real_backend: LocalComputerBackend) -> None:
    """Short text is nowhere near the bound: the pace stays exactly the default."""
    real_backend._cap_type_interval("e2e-typed-7391 quick brown fox")
    try:
        assert real_backend._engine.type_interval == pytest.approx(0.02)
    finally:
        real_backend._restore_type_interval()


# --- honest corruption flagging ------------------------------------------------------------------------


def test_substituted_suffix_is_mismatch_with_zero_retype(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP12b B1(c) THE BLOCKER SHAPE (VAL-WP12 reproduction): empty baseline, typed
    'test' landed as 'tett' — the buffer began correctly (matches 'tet') and then
    diverged: POSITIVE evidence our text landed and was corrupted -> mismatch with
    ZERO retype (appending 'est' would produce 'tettest' — the false heal).
    Dispatches: the initial chunk only."""
    engine, _ = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=["", "tett", "tett", "tett", "tett"],
        stability_seconds=1.0,
    )
    message = real_backend.execute(_type_action("test"))
    assert message == "Executed type. integrity=mismatch(0/4)"
    assert _dispatches(engine) == ["test"]


def test_substituted_tail_brown_xxx_shape_is_mismatch(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live 'brown xxx' for 'brown fox' shape: the buffer shares the correct
    leading part ('brown ') and then diverges -> mismatch, never repaired."""
    engine, _ = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=["", "brown xxx", "brown xxx", "brown xxx", "brown xxx"],
        stability_seconds=1.0,
    )
    message = real_backend.execute(_type_action("brown fox"))
    assert message == "Executed type. integrity=mismatch(0/9)"
    assert _dispatches(engine) == ["brown fox"]


def test_truncated_tail_is_missing_and_repairs_exactly_once(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live run-7 L2 truncation shape: 'e2e-ty' for the full marker — the
    buffer equals the BASELINE plus a proper prefix of the typed text (nothing
    extra after the correct part): the suffix is genuinely MISSING -> the one
    sound repair appends exactly the missing suffix."""
    text = "e2e-typed-7391 quick brown fox"
    engine, _ = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=["", "e2e-ty", "e2e-ty", "e2e-ty", "e2e-ty", text, text],
        stability_seconds=1.0,
    )
    message = real_backend.execute(_type_action(text))
    assert message == f"Executed type. integrity=verified({len(text)}/{len(text)}) healed=24"
    assert _dispatches(engine) == [text, text[len("e2e-ty"):]]


def test_foreign_buffer_is_unverified_transformed_buffer(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP12b B1(d): a foreign buffer ('zzzz') shares nothing with the typed text —
    honest ``unverified`` with branch=transformed_buffer, never a failure (a masked
    or transformed field must not report one) and never repaired."""
    engine, _ = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=["", "zzzz", "zzzz", "zzzz", "zzzz"],
        stability_seconds=1.0,
    )
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6) branch=transformed_buffer"
    assert _dispatches(engine) == ["abcdef"]


def test_genuinely_missing_suffix_still_repairs_exactly_once(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SOUND repair survives: a stable buffer that ENDS exactly at the matched
    prefix ('abc' for 'abcdef' — suffix genuinely missing) gets the ONE verified
    retype of exactly the missing suffix, then verifies."""
    engine, _ = _typing_backend(
        real_backend,
        monkeypatch,
        reader_values=["", "abc", "abc", "abc", "abc", "abcdef", "abcdef"],
        stability_seconds=1.0,
    )
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=verified(6/6) healed=3"
    assert _dispatches(engine) == ["abcdef", "def"]  # exactly ONE repair dispatch


def test_fake_backend_tett_shape_is_mismatch_not_a_false_heal(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP12c (VAL MINOR) FAKE PARITY, the B1 shape: field 'tett', typed 'test' ->
    the fake must NOT run the old tail-match heal (which produced
    ``verified(4/4) healed=3`` with the field becoming 'tettest'); it reports
    mismatch and leaves the field untouched."""
    from computer_use_mcp.desktop.fake_backend import FakeComputerBackend

    backend = FakeComputerBackend()
    backend.focused_control_value = "tett"
    message = backend.execute(_type_action("test"))
    assert message == "Simulated type. integrity=mismatch(0/4)"
    assert backend.focused_control_value == "tett"  # untouched — no false heal


def test_fake_backend_missing_suffix_heals_exactly_the_suffix(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP12c FAKE PARITY, the tes+test shape: field 'tes', typed 'test' — the
    suffix is genuinely missing -> the simulated retype heals exactly 't'."""
    from computer_use_mcp.desktop.fake_backend import FakeComputerBackend

    backend = FakeComputerBackend()
    backend.focused_control_value = "tes"
    message = backend.execute(_type_action("test"))
    assert message == "Simulated type. integrity=verified(4/4) healed=1"
    assert backend.focused_control_value == "test"


def test_baseline_landing_proven_needs_the_insertion_form(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP12c C2 direct pin: reader ['tes', 'test', 'test'] + type 'test' — the
    buffer ends with the typed text but the KNOWN baseline ('tes') proves NO
    insertion (nothing new landed) -> unverified branch=tail_unproven, dispatches
    ['test']."""
    engine, _ = _typing_backend(
        real_backend, monkeypatch, reader_values=["tes", "test", "test"], control_type=None
    )
    message = real_backend.execute(_type_action("test"))
    assert message == "Executed type. integrity=unverified(0/4) branch=tail_unproven"
    assert _dispatches(engine) == ["test"]


def test_fully_dropped_retype_into_matching_baseline_is_tail_unproven(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP12c C2 (the VAL trace): the field already ends with the typed text
    (baseline == buffer == 'test') and the re-dispatch dropped entirely — the
    single-read accept must NOT report verified; branch=tail_unproven."""
    engine, _ = _typing_backend(
        real_backend, monkeypatch, reader_values=["test", "test", "test"], control_type=None
    )
    message = real_backend.execute(_type_action("test"))
    assert message == "Executed type. integrity=unverified(0/4) branch=tail_unproven"
    assert _dispatches(engine) == ["test"]


def test_unverified_replies_carry_the_branch_reason(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every unverified names its ladder branch inside the message (WP12a): blind
    reads here -> ``branch=blind_read``."""
    engine, _ = _typing_backend(real_backend, monkeypatch, reader_values=[None], control_type=None)
    message = real_backend.execute(_type_action("abcdef"))
    assert message == "Executed type. integrity=unverified(0/6) branch=blind_read"
    assert _dispatches(engine) == ["abcdef"]
