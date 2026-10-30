"""DELTA 1 tests (VAL-WP2 findings F2, F4, F5, F6, F7).

- F2: the fake backend MODELS held click/right_click (hold recorded, sliced,
  stop-honouring, release ALWAYS recorded) and RECORDS duration-bearing moves —
  same no-silent-ignore principle as held keys.
- F4: the validator's duration_ms gate, symmetric to the hold gate
  (``duration_ms_not_allowed`` / ``duration_ms_invalid``, additive-only codes).
- F5: timed-motion pacing is DERIVED from duration_ms (duration / segments), so a
  5000 ms move costs ~5000 ms of declared pacing sleeps (patched clock).
- F6: a timed move to the CURRENT position issues ONE engine.move to the endpoint
  and marks dispatched (parity with the instant path).
- F7: ``CORTEX_MOVE_BEND_PX`` is bounded (fallback-not-clamp), and the sampling
  interval is fallback-not-clamp too; out-of-band values fall back to defaults.

All tests use RecordingEngine / FakeComputerBackend / monkeypatched clocks — no real
input (the WP2 HARD RULE).
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from recording_engine import RecordingEngine

import computer_use_mcp.backend as backend_module
from computer_use_mcp.backend import (
    FakeComputerBackend,
    LocalComputerBackend,
)
from computer_use_mcp.models import (
    MAX_DURATION_MS,
    ActionSpec,
    ActionType,
    GroundedAction,
    Point,
)
from computer_use_mcp.state import StopToken, TaskStopped
from computer_use_mcp.validator import GroundingValidator

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")


def _move(point: tuple[int, int], **kwargs: Any) -> GroundedAction:
    kwargs.setdefault("confidence", 1.0)
    return GroundedAction(action="move", point={"x": point[0], "y": point[1]}, **kwargs)


# --- F2: the fake models held clicks and records timed moves --------------------------------


class _StopDuringHoldWait(StopToken):
    def wait(self, timeout: float | None = None) -> bool:
        self.stop()
        return True


def test_fake_models_held_click_records_hold_and_release() -> None:
    backend = FakeComputerBackend()
    sleeps: list[float] = []
    original_sleep = backend_module.time.sleep
    backend_module.time.sleep = sleeps.append
    try:
        message = backend.execute(
            GroundedAction(
                action="click",
                point={"x": 10, "y": 10},
                hold_ms=200,
                confidence=1.0,
                source_observation_id=backend.observe().observation_id,
            )
        )
    finally:
        backend_module.time.sleep = original_sleep
    assert message == "Simulated click."
    assert sleeps == [0.2]  # the hold is modelled with the shared sliced wait
    assert backend.held_buttons == []  # released (the append/remove discipline)
    assert len(backend.executed) == 1  # the completed hold IS recorded


def test_fake_models_held_right_click_with_the_right_button() -> None:
    backend = FakeComputerBackend()
    backend.observe()
    message = backend.execute(
        GroundedAction(
            action="right_click",
            point={"x": 10, "y": 10},
            hold_ms=50,
            confidence=1.0,
        )
    )
    assert message == "Simulated right_click."
    assert backend.held_buttons == []  # released
    assert len(backend.executed) == 1


def test_fake_held_click_aborted_mid_hold_is_not_recorded() -> None:
    backend = FakeComputerBackend()
    backend.observe()
    action = GroundedAction(
        action="click", point={"x": 10, "y": 10}, hold_ms=200, confidence=1.0
    )
    with pytest.raises(TaskStopped):
        backend.execute(action, stop=_StopDuringHoldWait())
    assert backend.executed == [], "an aborted hold must not be recorded as completed"
    assert backend.held_buttons == [], "an aborted hold must still release the button"


def test_fake_click_without_hold_is_byte_identical_legacy() -> None:
    backend = FakeComputerBackend()
    backend.observe()
    sleeps: list[float] = []
    original_sleep = backend_module.time.sleep
    backend_module.time.sleep = sleeps.append
    try:
        message = backend.execute(
            GroundedAction(action="click", point={"x": 10, "y": 10}, confidence=1.0)
        )
    finally:
        backend_module.time.sleep = original_sleep
    assert message == "Simulated click."
    assert sleeps == []  # no hold wait on the legacy path
    assert len(backend.executed) == 1


def test_fake_records_a_timed_move() -> None:
    backend = FakeComputerBackend()
    backend.observe()
    backend.set_cursor((5, 5))
    message = backend.execute(_move((10, 10), duration_ms=150))
    assert message == "Simulated move."
    assert backend.timed_moves == [((5, 5), (10, 10), 150)]
    assert backend._cursor_physical == (10, 10)
    assert len(backend.executed) == 1


def test_fake_records_no_timed_move_for_a_jump() -> None:
    backend = FakeComputerBackend()
    backend.observe()
    backend.execute(_move((12, 14)))
    backend.execute(_move((16, 18), duration_ms=0))  # an EXPLICIT zero-duration jump
    assert backend.timed_moves == []


# --- F4: the validator's duration_ms gate ----------------------------------------------------


def _bypass(action: ActionType, **kwargs: Any) -> GroundedAction:
    kwargs.setdefault("confidence", 1.0)
    return GroundedAction.model_construct(action=action, **kwargs)


def test_validator_accepts_duration_ms_on_a_move_bypass() -> None:
    backend = FakeComputerBackend()
    observation = backend.observe()
    action = _bypass(ActionType.MOVE, point=Point(x=5, y=5), duration_ms=250)
    outcome = GroundingValidator().validate(action, observation)
    assert outcome.valid is True
    assert outcome.codes == []


@pytest.mark.parametrize(
    "action",
    [
        ActionType.CLICK,
        ActionType.DOUBLE_CLICK,
        ActionType.TYPE,
        ActionType.KEYPRESS,
        ActionType.SCROLL,
        ActionType.WAIT,
        ActionType.FOCUS_WINDOW,
    ],
    ids=lambda a: a.value,
)
def test_validator_rejects_duration_ms_on_non_move_bypass(action: ActionType) -> None:
    """A ``model_construct`` bypass carrying duration_ms on a non-move action is
    rejected at the validator layer too (the backend defense gate stays as the last
    line)."""
    backend = FakeComputerBackend()
    observation = backend.observe()
    action_obj = _bypass(action, duration_ms=200)
    outcome = GroundingValidator().validate(action_obj, observation)
    assert outcome.valid is False
    assert "duration_ms_not_allowed" in outcome.codes


@pytest.mark.parametrize("bad", [-1, MAX_DURATION_MS + 1, "200", True, 2.5])
def test_validator_rejects_bad_duration_ms_values(bad: Any) -> None:
    backend = FakeComputerBackend()
    observation = backend.observe()
    action_obj = _bypass(ActionType.MOVE, point=Point(x=5, y=5), duration_ms=bad)
    outcome = GroundingValidator().validate(action_obj, observation)
    assert outcome.valid is False
    assert "duration_ms_invalid" in outcome.codes
    assert "duration_ms_not_allowed" not in outcome.codes


def test_validator_duration_ms_codes_are_additive_only() -> None:
    backend = FakeComputerBackend()
    observation = backend.observe()
    codes = {
        code
        for code in [
            *GroundingValidator()
            .validate(_bypass(ActionType.CLICK, duration_ms=1), observation)
            .codes,
            *GroundingValidator()
            .validate(_bypass(ActionType.MOVE, point=Point(x=1, y=1), duration_ms=10_000), observation)
            .codes,
        ]
        if code.startswith("duration_ms")
    }
    assert codes <= {"duration_ms_not_allowed", "duration_ms_invalid"}, codes


def test_action_spec_duration_ms_passes_through_and_validates_in_grounded() -> None:
    spec = ActionSpec(action="move", x=1, y=2, duration_ms=200)
    assert spec.to_grounded().duration_ms == 200
    with pytest.raises(Exception, match="duration_ms is only valid on move actions"):
        ActionSpec(action="click", x=1, y=2, duration_ms=200).to_grounded()


# --- F5: pacing derived from duration_ms (patched clock) -------------------------------------


@WINDOWS_ONLY
def test_timed_move_pacing_honors_the_declared_duration(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F5: a legal-maximum 5000 ms timed move declares 128 samples / 127 segments, so
    the pacing is 5000/127 ms per sample and the total declared pacing is the full
    5000 ms (within one sample's rounding). The old fixed 12 ms slice would have
    cost ~1.5 s — the declared duration was not honoured."""
    sleeps: list[float] = []
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    monkeypatch.setattr(backend_module, "get_cursor_position", lambda: (10, 10))
    monkeypatch.setattr(real_backend, "probe_cursor_position", lambda: None)
    monkeypatch.setattr(backend_module.time, "sleep", sleeps.append)
    message = real_backend.execute(_move((30, 45), duration_ms=5_000))
    assert message == "Executed move."
    assert len(engine.calls) == 128  # sample count unchanged (determinism cap)
    per_sample = 5_000 / 127 / 1000.0
    assert len(sleeps) == 128
    for recorded in sleeps:
        assert recorded == pytest.approx(per_sample, rel=1e-9)
    total = sum(sleeps)
    per_sample_ms = 5_000 / 127
    # "within one sample's rounding": the loop paces AFTER each sample (the shape the
    # 120 ms pin freezes), so the total is duration + exactly one pacing slice.
    assert abs(total * 1000 - 5_000) <= per_sample_ms + 1e-6


@WINDOWS_ONLY
def test_timed_move_120ms_pacing_is_byte_identical(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The short example is unchanged by F5: 120 ms / 10 segments = 12 ms x 11."""
    sleeps: list[float] = []
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    monkeypatch.setattr(backend_module, "get_cursor_position", lambda: (10, 10))
    monkeypatch.setattr(real_backend, "probe_cursor_position", lambda: None)
    monkeypatch.setattr(backend_module.time, "sleep", sleeps.append)
    real_backend.execute(_move((30, 45), duration_ms=120))
    assert sleeps == [0.012] * 11


# --- F6: a timed move to the current position is ONE move -----------------------------------


@WINDOWS_ONLY
def test_timed_move_to_current_position_issues_one_move(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6: when the cursor is already at the target the sampled path is empty — the
    timed branch must behave like the instant path: ONE engine.move to the endpoint,
    dispatched, no lie of an empty loop."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    monkeypatch.setattr(backend_module, "get_cursor_position", lambda: (30, 45))
    monkeypatch.setattr(real_backend, "probe_cursor_position", lambda: None)
    monkeypatch.setattr(backend_module.time, "sleep", lambda _s: None)
    action = _move((30, 45), duration_ms=120)
    message = real_backend.execute(action)
    assert message == "Executed move."
    assert engine.calls == [("move", 30, 45)]
    assert action._dispatch_started is True


# --- F7: the knobs are bounded, fallback-not-clamp (subprocess probes) ----------------------


def _probe_env(env_name: str, value: str, expression: str) -> str:
    from fresh_backend_import import fresh_backend

    module = fresh_backend({env_name: value})
    return str(eval(expression, {"b": module}))  # noqa: S307 - test-authored expression


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0", "0"), ("250", "250"), ("-250", "-250"), ("5000", "5000"), ("-5000", "-5000")],
)
def test_bend_knob_accepts_the_whole_declared_band(raw: str, expected: str) -> None:
    """F7: CORTEX_MOVE_BEND_PX is bounded to -5000..5000 (fallback-not-clamp)."""
    assert _probe_env("CORTEX_MOVE_BEND_PX", raw, "b.MOVE_BEND_PX") == expected


@pytest.mark.parametrize("raw", ["5001", "-5001", "abc", ""])
def test_bend_knob_falls_back_out_of_band(raw: str) -> None:
    assert _probe_env("CORTEX_MOVE_BEND_PX", raw, "b.MOVE_BEND_PX") == "0"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("8", "8"), ("12", "12"), ("16", "16"), ("7", "12"), ("17", "12"), ("abc", "12"), ("1e400", "12")],
)
def test_sample_interval_knob_is_fallback_not_clamp(raw: str, expected: str) -> None:
    """F7: out-of-band CORTEX_PLAN_SAMPLE_INTERVAL_MS falls back to 12 (it used to be
    silently CLAMPED to the band edge — the K-12 typo class)."""
    assert _probe_env("CORTEX_PLAN_SAMPLE_INTERVAL_MS", raw, "b.PLAN_SAMPLE_INTERVAL_MS") == expected
