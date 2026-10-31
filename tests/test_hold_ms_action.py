"""HOLD_MS action tests: an explicit press duration on click/right_click (ORVEX-2026-101 Batch 1).

``hold_ms`` is an additive field treated exactly like the ``via`` precedent
(v0.7.1, "optional field defaulting to None + fail-closed validator"):

- models: valid ONLY on ``click``/``right_click``/``keypress``/``hotkey`` (never
  ``double_click`` — a two-click duration is a different, deferred semantic — and never
  on drag/move/type/scroll/wait/done/focus_window/ensure_app); ORVEX-2026-107 widened
  the set from the click pair to the click pair PLUS the key family;
  ``0 <= hold_ms <= 5000`` enforced by pydantic ``ge``/``le``; ``None`` (absent)
  keeps the legacy path byte-identical;
- validator: defense in depth second gate — a ``model_construct``-style bypass
  carrying ``hold_ms`` on a non-hold action or out of range/type is
  rejected with declared codes (``hold_ms_not_allowed`` / ``hold_ms_invalid``);
- backend (click hold path): when ``hold_ms`` is not None the click branch dispatches
  ``move`` -> ``mouse_down`` -> sliced interruptible wait -> ``mouse_up`` with
  the button ALWAYS released on any stop/exception (the proven drag
  try/finally pattern, D02 released 15/15); ``hold_ms=None`` keeps the legacy
  single ``engine.click`` call verbatim;
- no InputEngine contract change on the CLICK path (P1.4): the click hold path consumes
  the existing ``mouse_down``/``mouse_up`` primitives both engines already implement.
  (The key hold path's engine contract is ORVEX-2026-107 and is covered by
  ``tests/test_orvex_107_key_hold.py``.)
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from recording_engine import RecordingEngine

import computer_use_mcp.desktop.backend as backend_module
from computer_use_mcp.desktop import backend as backend_pkg
from computer_use_mcp.desktop.fake_backend import FakeComputerBackend
from computer_use_mcp.core.models import ActionSpec, ActionType, GroundedAction, Point
from computer_use_mcp.core.state import StopToken, TaskStopped
from computer_use_mcp.pipeline.validator import GroundingValidator

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")

#: The rule clause of ``GroundedAction._hold_ms_click_family_only``'s error, verbatim.
#: It ENUMERATES the legal action set, so matching the whole clause (not a prefix) is
#: what makes a future widening or narrowing of that set a test failure instead of a
#: silently-stale regex. ORVEX-2026-107 grew it from click/right_click to
#: click/right_click/keypress/hotkey.
HOLD_MS_RULE_MESSAGE = "hold_ms is only valid on click/right_click/keypress/hotkey actions"


def click(point: tuple[int, int], **kwargs: Any) -> GroundedAction:
    kwargs.setdefault("confidence", 1.0)
    return GroundedAction(action="click", point={"x": point[0], "y": point[1]}, **kwargs)


def right_click(point: tuple[int, int], **kwargs: Any) -> GroundedAction:
    kwargs.setdefault("confidence", 1.0)
    return GroundedAction(action="right_click", point={"x": point[0], "y": point[1]}, **kwargs)


class _StopDuringHoldWait(StopToken):
    """Stop token that fires the moment the hold wait asks it to sleep.

    Simulates a cancellation arriving mid-hold deterministically: the very first
    sliced wait observes the armed stop and the wait path must raise
    ``TaskStopped`` — the finally must still have released the button.
    """

    def wait(self, timeout: float | None = None) -> bool:
        self.stop()
        return True


class _SyntheticWaitFailure(StopToken):
    """Stop token whose ``wait`` raises a synthetic mid-hold exception."""

    def wait(self, timeout: float | None = None) -> bool:
        raise RuntimeError("synthetic mid-hold failure")


class _StopOnNthCheck(StopToken):
    """Stop token that fires (raises) on its own ``n``-th ``ensure_live`` call."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self._remaining = n

    def ensure_live(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self.stop()
        super().ensure_live()


# --- P1.1 models: additive field, click/right_click-only, fail-closed bounds ---------------------


def test_hold_ms_defaults_to_none_and_additive() -> None:
    """Absent hold_ms = None = the legacy path; existing fields are untouched."""
    assert click((10, 20)).hold_ms is None
    assert click((10, 20), hold_ms=None).hold_ms is None


def test_hold_ms_accepted_on_click_and_right_click() -> None:
    assert click((10, 20), hold_ms=250).hold_ms == 250
    assert right_click((10, 20), hold_ms=250).hold_ms == 250


@pytest.mark.parametrize(
    ("action", "extra"),
    [
        ("double_click", {}),
        ("drag", {"point": {"x": 1, "y": 1}, "to_point": {"x": 2, "y": 2}}),
        ("move", {"point": {"x": 1, "y": 1}}),
        ("type", {"text": "hi"}),
        ("scroll", {"delta": 1}),
        ("wait", {"delta": 1}),
        ("done", {}),
        ("focus_window", {"target": "Notepad"}),
        ("ensure_app", {"target": "notepad"}),
    ],
)
def test_hold_ms_rejected_on_every_non_hold_action(action: str, extra: dict) -> None:
    """Declared rejection message names the rule: the four hold actions only.

    ``keypress``/``hotkey`` were in this set before ORVEX-2026-107 widened the legal
    action set; they now live in ``test_hold_ms_now_accepted_on_the_key_family`` below, so
    the pair is pinned from BOTH sides — absent from the rejection set, present in the
    positive set.
    """
    with pytest.raises(ValidationError, match=HOLD_MS_RULE_MESSAGE):
        GroundedAction(action=action, hold_ms=250, **extra)


@pytest.mark.parametrize(
    ("action", "extra"),
    [
        ("keypress", {"keys": ["a"]}),
        ("hotkey", {"keys": ["ctrl", "s"]}),
    ],
)
def test_hold_ms_now_accepted_on_the_key_family(action: str, extra: dict) -> None:
    """ORVEX-2026-107: ``keypress``/``hotkey`` joined the click family as hold actions.

    Same two payloads the rejection set used to carry, asserted in the direction that is
    now true: construction SUCCEEDS, the value survives, and the action is unchanged.
    """
    grounded = GroundedAction(action=action, hold_ms=250, **extra)
    assert grounded.action == ActionType(action)
    assert grounded.hold_ms == 250


def test_hold_ms_bounds_fail_closed_with_named_cap() -> None:
    """ge=0 / le=5000: negative and over-cap rejected; the boundaries accepted."""
    with pytest.raises(ValidationError):
        click((10, 20), hold_ms=-1)
    with pytest.raises(ValidationError):
        click((10, 20), hold_ms=5_001)
    assert click((10, 20), hold_ms=0).hold_ms == 0
    assert click((10, 20), hold_ms=5_000).hold_ms == 5_000


def test_hold_ms_rejects_wrong_type() -> None:
    """Non-integer payloads fail construction (pydantic). NOTE: pydantic lax mode
    coerces integral strings/floats ('250' -> 250) exactly like every other int
    field in this codebase (e.g. ``delta``); the validator's strict type check is
    the second gate for bypass payloads (see validator tests below)."""
    with pytest.raises(ValidationError):
        click((10, 20), hold_ms="abc")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        click((10, 20), hold_ms=250.5)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        click((10, 20), hold_ms=[250])  # type: ignore[arg-type]


def test_hold_ms_round_trip_serialization_preserves_value() -> None:
    action = click((10, 20), hold_ms=250)
    dumped = action.model_dump()
    assert dumped["hold_ms"] == 250
    restored = GroundedAction.model_validate(dumped)
    assert restored.hold_ms == 250
    assert restored == action
    from_json = GroundedAction.model_validate_json(action.model_dump_json())
    assert from_json.hold_ms == 250


def test_action_spec_mirrors_hold_ms_and_to_grounded_passes_it_through() -> None:
    """Queued follow_up specs carry hold_ms exactly like the via pass-through."""
    spec = ActionSpec(action="click", x=12, y=34, hold_ms=250)
    assert spec.hold_ms == 250
    grounded = spec.to_grounded()
    assert grounded.hold_ms == 250
    assert grounded.point == Point(x=12, y=34)
    assert ActionSpec(action="click", x=12, y=34).to_grounded().hold_ms is None


def test_action_spec_hold_ms_bounds_fail_closed() -> None:
    with pytest.raises(ValidationError):
        ActionSpec(action="click", x=1, y=2, hold_ms=-1)
    with pytest.raises(ValidationError):
        ActionSpec(action="click", x=1, y=2, hold_ms=5_001)


def test_action_spec_hold_ms_on_double_click_rejected_at_to_grounded() -> None:
    """Validation happens in GroundedAction (via precedent): the spec converts,
    the conversion fails closed before anything can reach the queue."""
    spec = ActionSpec(action="double_click", x=1, y=2, hold_ms=250)
    with pytest.raises(ValidationError, match=HOLD_MS_RULE_MESSAGE):
        spec.to_grounded()


# --- P1.2 validator: defense in depth against construction bypasses ------------------------------


def test_validator_accepts_hold_ms_on_click_family() -> None:
    backend = FakeComputerBackend()
    observation = backend.observe()
    action = click((10, 10), hold_ms=250, source_observation_id=observation.observation_id)
    outcome = GroundingValidator().validate(action, observation)
    assert outcome.valid is True
    assert outcome.codes == []


def test_validator_hold_ms_none_unchanged_rejection_surface() -> None:
    """hold_ms=None rides the exact legacy validate() path (no new codes)."""
    backend = FakeComputerBackend()
    observation = backend.observe()
    outcome = GroundingValidator().validate(click((10, 10)), observation)
    assert outcome.valid is True
    assert outcome.codes == []


def test_validator_rejects_hold_ms_on_non_click_family_bypass() -> None:
    """A model_construct bypass carrying hold_ms on double_click is rejected."""
    bypassed = GroundedAction.model_construct(
        action=ActionType.DOUBLE_CLICK,
        point=Point(x=10, y=10),
        hold_ms=250,
        confidence=1.0,
    )
    backend = FakeComputerBackend()
    observation = backend.observe()
    outcome = GroundingValidator().validate(bypassed, observation)
    assert outcome.valid is False
    assert "hold_ms_not_allowed" in outcome.codes


@pytest.mark.parametrize("bypass_value", [9_999, -5, "250", True, 1.5])
def test_validator_rejects_hold_ms_out_of_range_or_wrong_type_bypass(bypass_value: Any) -> None:
    """Out-of-range OR non-int (incl. bool/str/float bypass payloads) -> one code."""
    bypassed = GroundedAction.model_construct(
        action=ActionType.CLICK,
        point=Point(x=10, y=10),
        hold_ms=bypass_value,
        confidence=1.0,
    )
    backend = FakeComputerBackend()
    observation = backend.observe()
    outcome = GroundingValidator().validate(bypassed, observation)
    assert outcome.valid is False
    assert "hold_ms_invalid" in outcome.codes


# --- P1.3 backend: hold press = move -> down -> sliced wait -> up inside try/finally --------------


@WINDOWS_ONLY
def test_real_click_hold_sequence_and_gap_on_test_clock(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hold sequencing and requested duration are deterministic; host sleep jitter is not logic.

    The contract under test is ``move -> down -> exact 250ms wait request -> up``.
    A recording sleep replaces ``time.sleep`` so the assertion measures the product's
    sequencing/requested duration, not Windows scheduler fairness (FA1 §3 jitter).
    """
    sleeps: list[float] = []
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)  # passthrough transform
    monkeypatch.setattr(backend_module.time, "sleep", sleeps.append)
    message = real_backend.execute(click((30, 45), hold_ms=250))
    assert message == "Executed click."
    assert engine.calls == [("move", 30, 45), ("mouse_down", "left"), ("mouse_up", "left")]
    assert sleeps == [0.25]
    assert engine.down is False


@WINDOWS_ONLY
def test_real_right_click_hold_uses_right_button(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    real_backend.execute(right_click((30, 45), hold_ms=100))
    assert engine.calls == [("move", 30, 45), ("mouse_down", "right"), ("mouse_up", "right")]


@WINDOWS_ONLY
def test_real_click_hold_zero_is_explicit_down_up_without_wait(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """hold_ms=0 (explicit zero hold) takes the hold path with no wait between."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)

    sleeps: list[float] = []
    monkeypatch.setattr(backend_module.time, "sleep", sleeps.append)
    real_backend.execute(click((30, 45), hold_ms=0))
    assert engine.calls == [("move", 30, 45), ("mouse_down", "left"), ("mouse_up", "left")]
    assert sleeps == [0.0]  # the existing zero-duration call; scheduler jitter is irrelevant
    assert engine.down is False


@WINDOWS_ONLY
@pytest.mark.parametrize("hold", [None, "omitted"])
def test_real_click_without_hold_ms_is_byte_identical_legacy(
    real_backend, monkeypatch: pytest.MonkeyPatch, hold: Any
) -> None:
    """REGRESSION (the batch's most important invariant): no hold_ms -> the exact
    legacy single engine.click call, nothing else."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    kwargs = {} if hold == "omitted" else {"hold_ms": None}
    message = real_backend.execute(click((30, 45), **kwargs))
    assert message == "Executed click."
    assert engine.calls == [("click", 30, 45, 1, "left")]
    assert engine.down is False


@WINDOWS_ONLY
def test_real_double_click_without_hold_ms_is_byte_identical_legacy(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    real_backend.execute(GroundedAction(action="double_click", point={"x": 30, "y": 45}))
    assert engine.calls == [("click", 30, 45, 2, "left")]


@WINDOWS_ONLY
def test_real_click_hold_prestopped_token_performs_zero_inputs(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    stop = StopToken()
    stop.stop()
    with pytest.raises(TaskStopped):
        real_backend.execute(click((30, 45), hold_ms=250), stop=stop)
    assert engine.calls == []
    assert engine.down is False


@WINDOWS_ONLY
def test_real_click_hold_stop_before_press_never_presses(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop firing between move and mouse_down -> TaskStopped, zero press, released."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    # 1: execute header, 2: after transform (pre-branch), 3: before mouse_down.
    stop = _StopOnNthCheck(3)
    with pytest.raises(TaskStopped):
        real_backend.execute(click((30, 45), hold_ms=250), stop=stop)
    assert engine.calls == [("move", 30, 45)]
    assert engine.down is False


@WINDOWS_ONLY
def test_real_click_hold_cancelled_mid_wait_releases_button(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation mid-hold: TaskStopped raised AND the button is released (finally)."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    stop = _StopDuringHoldWait()
    with pytest.raises(TaskStopped):
        real_backend.execute(click((30, 45), hold_ms=250), stop=stop)
    assert engine.calls == [("move", 30, 45), ("mouse_down", "left"), ("mouse_up", "left")]
    assert engine.down is False


@WINDOWS_ONLY
def test_real_click_hold_synthetic_exception_mid_wait_releases_button(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any mid-hold exception: the original error propagates, button still released."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    stop = _SyntheticWaitFailure()
    with pytest.raises(RuntimeError, match="synthetic mid-hold failure"):
        real_backend.execute(click((30, 45), hold_ms=250), stop=stop)
    assert engine.calls == [("move", 30, 45), ("mouse_down", "left"), ("mouse_up", "left")]
    assert engine.down is False


@WINDOWS_ONLY
def test_real_click_hold_without_stop_token_still_releases_on_error(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stop=None hold: a synthetic wait failure still releases (finally, no token)."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    real_backend_without_wait_hold = real_backend

    class _BrokenSleep:
        @staticmethod
        def sleep(_seconds: float) -> None:
            raise RuntimeError("synthetic no-token failure")

    monkeypatch.setattr(backend_pkg.time, "sleep", _BrokenSleep.sleep)
    try:
        with pytest.raises(RuntimeError, match="synthetic no-token failure"):
            real_backend_without_wait_hold.execute(click((30, 45), hold_ms=250))
    finally:
        monkeypatch.undo()
    assert engine.calls == [("move", 30, 45), ("mouse_down", "left"), ("mouse_up", "left")]
    assert engine.down is False


@WINDOWS_ONLY
def test_real_double_click_with_hold_rejected_before_any_movement(
    real_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """hold on double_click fails closed at the backend bypass gate: zero engine calls."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    bypassed = GroundedAction.model_construct(
        action=ActionType.DOUBLE_CLICK, point=Point(x=30, y=45), hold_ms=250
    )
    with pytest.raises(ValueError, match="hold_ms"):
        real_backend.execute(bypassed)
    assert engine.calls == []
    assert engine.down is False
