"""ORVEX-2026-107 Phase 1: ``hold_ms`` on ``keypress``/``hotkey`` (WP2 port).

``hold_ms`` already exists as an additive field (ORVEX-2026-101 Batch 1) legal ONLY on
``click``/``right_click``. This mission widens the LEGAL ACTION SET to include
``keypress`` and ``hotkey`` so a key can be physically held for an explicit duration
(``keydown -> sliced interruptible wait -> keyup``). NOTHING here changes the field's
type, range (``0..MAX_HOLD_MS``), or name.

The four enforcement sites:

1. ``models.GroundedAction.HOLD_ACTIONS``
2. ``models.GroundedAction._hold_ms_hold_family_only``
3. ``backend._validate_action_defense``
4. ``validator.GroundingValidator.validate`` -> ``hold_ms_not_allowed``

and the dispatch lives at ``LocalComputerBackend._execute_dispatched`` (real) /
``FakeComputerBackend.execute`` (fake).

PORT NOTE (WP2, stable tree): sections A-D and the queue-tier E1 echo are ported
verbatim in behavior. Section E2 (the PLAN-aggregate echo of a held key) is
DEFERRED to WP7 — it needs the waypoint-plan surface (``plan=`` on
``computer_execute``), which does not exist in the stable tree yet.

The tests never sleep for real: the hold wait is either patched (``time.sleep``) or
driven by a ``StopToken`` that arms deterministically, so the suite stays fast and
scheduling noise cannot pass for the missing capability.
"""

from __future__ import annotations

import threading
import time as wall_time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from recording_engine import RecordingEngine
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    execute_payload,
    make_session,
)

import computer_use_mcp.backend as backend_module
from computer_use_mcp import server
from computer_use_mcp.backend import (
    _KEYEVENTF_KEYUP,
    HOLD_SLICE_SECONDS,
    FakeComputerBackend,
    InputEngine,
    PyAutoGuiInputEngine,
    SendInputEngine,
    UnsupportedActionError,
    _chord_key_units,
    _validate_action_defense,
)
from computer_use_mcp.models import MAX_HOLD_MS, ActionSpec, ActionType, GroundedAction, Point
from computer_use_mcp.state import StopToken, TaskStopped
from computer_use_mcp.validator import GroundingValidator


WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")

#: The four actions a non-None ``hold_ms`` is legal on AFTER ORVEX-2026-107.
HOLD_SET: frozenset[ActionType] = frozenset(
    {ActionType.CLICK, ActionType.RIGHT_CLICK, ActionType.KEYPRESS, ActionType.HOTKEY}
)
#: The two it was legal on BEFORE — the widening must not drop these.
PRE_EXISTING_HOLD_SET: frozenset[ActionType] = frozenset(
    {ActionType.CLICK, ActionType.RIGHT_CLICK}
)
#: The full complement — every action that must KEEP failing closed (AC-4).
NON_HOLD_ACTIONS: tuple[ActionType, ...] = tuple(a for a in ActionType if a not in HOLD_SET)

#: Valid minimal payloads per action so a construction test exercises the HOLD gate and
#: not some unrelated "missing field" gate.
_ACTION_KWARGS: dict[ActionType, dict[str, Any]] = {
    ActionType.CLICK: {"point": {"x": 10, "y": 20}},
    ActionType.DOUBLE_CLICK: {"point": {"x": 10, "y": 20}},
    ActionType.RIGHT_CLICK: {"point": {"x": 10, "y": 20}},
    ActionType.DRAG: {"point": {"x": 1, "y": 1}, "to_point": {"x": 2, "y": 2}},
    ActionType.TYPE: {"text": "hi"},
    ActionType.KEYPRESS: {"keys": ["a"]},
    ActionType.SCROLL: {"delta": 1},
    ActionType.WAIT: {"delta": 1},
    ActionType.DONE: {},
    ActionType.MOVE: {"point": {"x": 1, "y": 1}},
    ActionType.HOTKEY: {"keys": ["ctrl", "s"]},
    ActionType.FOCUS_WINDOW: {"target": "Notepad"},
    ActionType.ENSURE_APP: {"target": "notepad"},
}


def build(action: ActionType, **kwargs: Any) -> GroundedAction:
    """Construct a ``GroundedAction`` of ``action`` with valid minimal payloads."""
    return GroundedAction(action=action, confidence=1.0, **{**_ACTION_KWARGS[action], **kwargs})


def keypress(keys: list[str], **kwargs: Any) -> GroundedAction:
    return GroundedAction(action="keypress", keys=keys, confidence=1.0, **kwargs)


def hotkey(keys: list[str], **kwargs: Any) -> GroundedAction:
    return GroundedAction(action="hotkey", keys=keys, confidence=1.0, **kwargs)


def bypass(action: ActionType, **kwargs: Any) -> GroundedAction:
    """``model_construct``-built action that SKIPS pydantic validation.

    This is how the validator gate and the shared backend defense gate are exercised
    in isolation: construction rules cannot mask the gate under test, and the gate
    cannot mask a construction rule.
    """
    return GroundedAction.model_construct(
        action=action,
        keys=list(_ACTION_KWARGS[action].get("keys", [])),
        confidence=1.0,
        **kwargs,
    )


# --- primitive-name discovery (name-tolerant, existence-strict) --------------------------------

_KEY_DOWN_NAMES = ("key_down", "keyDown", "keydown", "press_key", "key_press")
_KEY_UP_NAMES = ("key_up", "keyUp", "keyup", "release_key", "key_release")


def _resolve_primitive(cls: type, candidates: tuple[str, ...]) -> str | None:
    return next((n for n in candidates if callable(getattr(cls, n, None))), None)


def declares_key_primitives(cls: type) -> tuple[str | None, str | None]:
    """The (key-down, key-up) primitive names ``cls`` ITSELF declares, or ``(None, None)``.

    OWN attributes only (``vars(cls)``) — deliberately NOT the MRO. The ABC default is a
    non-abstract no-op every engine inherits (D3), so an MRO-walking probe reports even
    ``_MinimalStubEngine`` as declaring, which is the opposite of what this name claims.
    Pair with :func:`resolves_key_primitives`, the MRO-walking half that D1/D2/D4 need.
    """
    own = vars(cls)
    return (
        next((n for n in _KEY_DOWN_NAMES if callable(own.get(n))), None),
        next((n for n in _KEY_UP_NAMES if callable(own.get(n))), None),
    )


def resolves_key_primitives(cls: type) -> tuple[str | None, str | None]:
    """The primitive names reachable on ``cls`` at all (including inherited defaults)."""
    return _resolve_primitive(cls, _KEY_DOWN_NAMES), _resolve_primitive(cls, _KEY_UP_NAMES)


class _KeyRecordingEngine(RecordingEngine):
    """``RecordingEngine`` plus key-down/key-up recording (no real input).

    Extends the SHARED stub rather than inventing one, so every non-keyboard call the
    backend makes is recorded in exactly the same way as the proven click-hold tests.
    ``keys_down`` is the live physical state (append on down, remove on up) so a test
    can observe — mid-hold — whether the key is actually still pressed. The primitive
    accepts EITHER a single key name or a batch, so an implementation that splits per
    key and one that splits per chord are both observable; neither can pass by doing
    nothing.
    """

    def __init__(self) -> None:
        super().__init__()
        self.key_calls: list[tuple[str, str]] = []
        self.keys_down: list[str] = []

    @staticmethod
    def _each(key: str | list[str] | tuple[str, ...]) -> list[str]:
        return [key] if isinstance(key, str) else list(key)

    def key_down(self, key: str | list[str]) -> None:
        for name in self._each(key):
            self.keys_down.append(name)
            self.key_calls.append(("key_down", name))

    def key_up(self, key: str | list[str]) -> None:
        for name in self._each(key):
            if name in self.keys_down:
                self.keys_down.remove(name)
            self.key_calls.append(("key_up", name))

    # Spelling aliases: the behavioural assertions target the ORDER of key events, not
    # one identifier. Every alias funnels into the same recorder, so an implementation
    # cannot pass by naming the primitive something the recorder ignores.
    keyDown = key_down
    keydown = key_down
    press_key = key_down
    key_press = key_down
    keyUp = key_up
    keyup = key_up
    release_key = key_up
    key_release = key_up


class _FailingReleaseEngine(_KeyRecordingEngine):
    """``_KeyRecordingEngine`` whose Nth key-UP raises (``fail_at`` is 1-based).

    Recording the ATTEMPT before raising is the whole point. A release the sweep SKIPPED
    and a release the engine attempted-and-failed are different failures, and only the
    second one is what this stub models. The key also leaves ``keys_down`` exactly as an
    ordinary release would, so a test can assert the sweep reached every key without
    re-deriving the bookkeeping.
    """

    def __init__(self, fail_at: int = 1) -> None:
        super().__init__()
        self.fail_at = fail_at
        self.releases = 0

    def key_up(self, key: str | list[str]) -> None:
        super().key_up(key)
        self.releases += 1
        if self.releases == self.fail_at:
            raise RuntimeError(f"synthetic key_up failure #{self.releases}")


class _FailingPressEngine(_KeyRecordingEngine):
    """``_KeyRecordingEngine`` whose Nth key-DOWN raises (``fail_at`` is 1-based).

    Nothing at all is recorded on the failure, and the opposite bookkeeping rule to
    :class:`_FailingReleaseEngine` is the honest one here: a key-down that raised never
    put the key down, so it must not enter ``keys_down`` and must never be released.
    """

    def __init__(self, fail_at: int) -> None:
        super().__init__()
        self.fail_at = fail_at
        self.presses = 0

    def key_down(self, key: str | list[str]) -> None:
        self.presses += 1
        if self.presses == self.fail_at:
            raise RuntimeError(f"synthetic key_down failure #{self.presses}")
        super().key_down(key)


class _MinimalStubEngine(InputEngine):
    """An ``InputEngine`` implementing ONLY the pre-existing abstract primitives.

    Design decision D3: widening the ABC must be ADDITIVE with a safe base default, so
    a third-party or test engine that never heard of a key-hold keeps constructing and
    running. This stub is that third party. It deliberately declares NO key primitive,
    so if the ABC addition were made ``@abstractmethod`` this class would fail to
    instantiate at all — which is exactly the rot D3 forbids.
    """

    click_interval: float = 0.0
    type_interval: float = 0.0
    drag_interpolate: bool = False
    drag_step_pause: float = 0.0

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def move(self, x: int, y: int) -> None:
        self.calls.append(("move", x, y))

    def click(self, x: int, y: int, clicks: int = 1, button: str = "left") -> None:
        self.calls.append(("click", x, y, clicks, button))

    def mouse_down(self, button: str = "left") -> None:
        self.calls.append(("mouse_down", button))

    def mouse_up(self, button: str = "left") -> None:
        self.calls.append(("mouse_up", button))

    def type_text(self, text: str, before_chunk: object = None) -> None:
        self.calls.append(("type_text", text))

    def chord(self, keys: list[str]) -> None:
        self.calls.append(("chord", *keys))

    def scroll(self, delta: int) -> None:
        self.calls.append(("scroll", delta))


class _StubPyautogui:
    """pyautogui stand-in for the fallback engine, which takes the module BY INJECTION.

    Same shape as the established ``_FakePyautogui`` precedent:
    ``PyAutoGuiInputEngine.__init__`` only ever assigns ``PAUSE``/``FAILSAFE`` and
    calls named methods on the object it was handed, so a plain object exposing those
    names is the entire contract D4 needs to exercise — and hand-rolled here rather
    than imported so this file keeps no cross-module test dependency.
    """

    FailSafeException = type("FailSafeException", (Exception,), {})

    def __init__(self) -> None:
        self.pressed: list[str] = []
        self.released: list[str] = []
        self.PAUSE = 99.0  # sentinel: the engine must overwrite it
        self.FAILSAFE = False

    def keyDown(self, key: str) -> None:
        self.pressed.append(key)

    def keyUp(self, key: str) -> None:
        self.released.append(key)


# --- deterministic stop tokens ----------------------------------------------------------------


class _StopDuringHoldWait(StopToken):
    """Arms the stop the moment the hold wait asks it to sleep (mid-hold cancellation)."""

    def wait(self, timeout: float | None = None) -> bool:
        self.stop()
        return True


class _SyntheticWaitFailure(StopToken):
    """``wait`` raises a synthetic mid-hold exception."""

    def wait(self, timeout: float | None = None) -> bool:
        raise RuntimeError("synthetic mid-hold failure")


class _SlicingProbeStop(StopToken):
    """Records the slice it is asked to wait for, then arms immediately.

    Proves a hold wait consults the token IN SLICES (``stop.wait`` is what an
    interruptible wait is made of) rather than performing one uninterruptible sleep.
    """

    def __init__(self) -> None:
        super().__init__()
        self.slices: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.slices.append(timeout)
        self.stop()
        return True


class _SliceRecordingStop(StopToken):
    """Real ``StopToken`` armed from a background thread; records every wait slice.

    Used for the ONE claim that genuinely needs wall time: a stop arriving mid-hold
    must be honoured within roughly one ``HOLD_SLICE_SECONDS``.
    """

    def __init__(self, arm_after_s: float = 0.005, on_wait: Any = None) -> None:
        super().__init__()
        self.arm_after_s = arm_after_s
        self.on_wait = on_wait
        self.slices: list[float | None] = []

    def start(self) -> threading.Timer:
        timer = threading.Timer(self.arm_after_s, self.stop)
        timer.daemon = True
        timer.start()
        return timer

    def wait(self, timeout: float | None = None) -> bool:
        self.slices.append(timeout)
        if self.on_wait is not None:
            self.on_wait()
        return super().wait(timeout)


class _SleepRecorder:
    """Replacement for ``time.sleep`` that records the request AND a state snapshot."""

    def __init__(self, snapshot: Any = None) -> None:
        self.sleeps: list[float] = []
        self.snapshots: list[Any] = []
        self._snapshot = snapshot

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.snapshots.append(None if self._snapshot is None else self._snapshot())


class _RaisingSleep:
    @staticmethod
    def sleep(_seconds: float) -> None:
        raise RuntimeError("synthetic no-token failure")


# --- fixtures ---------------------------------------------------------------------------------


@pytest.fixture
def dispatching_backend(real_backend, monkeypatch: pytest.MonkeyPatch):
    """``real_backend`` with BOTH pacing contracts neutralized.

    The B8 focus settle and the B3 key-dispatch gap both call ``time.sleep`` and would
    otherwise pollute the recorded sleep list of the ``stop=None`` hold tests (the
    ``real_backend`` fixture is session-scoped, so a prior module may have moved it).
    The test keys (``right``/``ctrl``/``shift``/``c``) are deliberately non-terminal, so
    the B3 gap is structurally inert for them regardless.
    """
    monkeypatch.setattr(real_backend, "_last_focus_transition", 0.0)
    monkeypatch.setattr(real_backend, "_last_key_dispatch", 0.0)
    monkeypatch.setattr(real_backend, "_active_context", None)
    return real_backend


def install_engine(backend: Any, engine: InputEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend, "_engine", engine)


# =================================================================================================
# A — ACCEPTANCE.
# =================================================================================================


def test_a1_keypress_accepts_hold_ms() -> None:
    """AC-1: ``{"action":"keypress","keys":["right"],"hold_ms":2000}`` is not malformed."""
    action = GroundedAction(action="keypress", keys=["right"], hold_ms=2_000, confidence=1.0)
    assert action.action is ActionType.KEYPRESS
    assert action.keys == ["right"]
    assert action.hold_ms == 2_000


def test_a2_hotkey_accepts_hold_ms() -> None:
    """AC-2: the same widening for the compound ``hotkey`` sibling."""
    action = GroundedAction(action="hotkey", keys=["ctrl", "c"], hold_ms=2_000, confidence=1.0)
    assert action.action is ActionType.HOTKEY
    assert action.keys == ["ctrl", "c"]
    assert action.hold_ms == 2_000


def test_a3_hold_actions_widens_to_the_key_family() -> None:
    """The single source of truth for "where may hold_ms live" names the key actions."""
    assert ActionType.KEYPRESS in GroundedAction.HOLD_ACTIONS
    assert ActionType.HOTKEY in GroundedAction.HOLD_ACTIONS


def test_a4_validator_accepts_hold_ms_on_key_family() -> None:
    """Enforcement site 4 no longer rejects a held key.

    ``model_construct`` is used so the failure is attributable to the VALIDATOR gate,
    not to pydantic construction (which A1/A2 already cover).
    """
    backend = FakeComputerBackend()
    observation = backend.observe()
    for action_type, keys in ((ActionType.KEYPRESS, ["right"]), (ActionType.HOTKEY, ["ctrl", "c"])):
        held = bypass(action_type, hold_ms=2_000)
        held.keys = list(keys)
        outcome = GroundingValidator().validate(held, observation)
        assert outcome.valid is True, (action_type, outcome.codes, outcome.reasons)
        assert outcome.codes == []


def test_a5_action_defense_accepts_hold_ms_on_key_family() -> None:
    """Enforcement site 3 no longer raises for a held key."""
    for action_type in (ActionType.KEYPRESS, ActionType.HOTKEY):
        _validate_action_defense(bypass(action_type, hold_ms=2_000))


def test_a6_action_spec_carries_hold_ms_to_a_key_action() -> None:
    """Integration (AC-6/queue tier): the host surface mirrors the widened legality."""
    spec = ActionSpec(action="keypress", keys=["right"], hold_ms=2_000)
    grounded = spec.to_grounded()
    assert grounded.action is ActionType.KEYPRESS
    assert grounded.hold_ms == 2_000


def test_a7_key_family_reaches_the_range_and_type_gate() -> None:
    """A held key must be stopped by the RANGE/TYPE rule, never by the action-set rule."""
    backend = FakeComputerBackend()
    observation = backend.observe()
    for action_type in (ActionType.KEYPRESS, ActionType.HOTKEY):
        for bad in ("abc", -1, MAX_HOLD_MS + 1):
            held = bypass(action_type, hold_ms=bad)
            outcome = GroundingValidator().validate(held, observation)
            assert outcome.valid is False, (action_type, bad)
            assert "hold_ms_invalid" in outcome.codes, (action_type, bad, outcome.codes)
            assert "hold_ms_not_allowed" not in outcome.codes, (action_type, bad, outcome.codes)


# =================================================================================================
# B — FAIL-CLOSED PRESERVATION.
# =================================================================================================


@pytest.mark.parametrize("action", NON_HOLD_ACTIONS, ids=[a.value for a in NON_HOLD_ACTIONS])
def test_b1_hold_ms_still_rejected_on_every_non_hold_action(action: ActionType) -> None:
    """AC-4: the FULL complement, not one example. ``double_click`` is in this set."""
    assert action not in HOLD_SET
    with pytest.raises(ValidationError, match="hold_ms"):
        build(action, hold_ms=250)


@pytest.mark.parametrize(
    "action",
    [ActionType.CLICK, ActionType.RIGHT_CLICK, ActionType.DOUBLE_CLICK],
    ids=lambda a: a.value,
)
@pytest.mark.parametrize("hold", [-1, MAX_HOLD_MS + 1])
def test_b2_hold_ms_out_of_band_still_fails_closed_at_construction(action: ActionType, hold: int) -> None:
    """``ge=0``/``le=MAX_HOLD_MS`` is frozen by the goal; the widening must not move it."""
    with pytest.raises(ValidationError):
        build(action, hold_ms=hold)


@pytest.mark.parametrize("action", sorted(PRE_EXISTING_HOLD_SET), ids=lambda a: a.value)
def test_b2b_band_edges_are_still_accepted_on_the_click_family(action: ActionType) -> None:
    """``0`` and ``MAX_HOLD_MS`` remain the exact accepted edges (not ``MAX_HOLD_MS + 1``)."""
    assert build(action, hold_ms=0).hold_ms == 0
    assert build(action, hold_ms=MAX_HOLD_MS).hold_ms == MAX_HOLD_MS


@pytest.mark.parametrize(
    "action", [ActionType.CLICK, ActionType.RIGHT_CLICK], ids=lambda a: a.value
)
@pytest.mark.parametrize("hold", ["abc", 250.5, [250]])
def test_b3_uncoercible_hold_ms_types_still_fail_at_construction(
    action: ActionType, hold: Any
) -> None:
    """Pydantic lax mode coerces ``"500"``/``5.0``/``True`` (see b4/b5 for the strict
    gate); only genuinely unrepresentable payloads fail here. That split is unchanged."""
    with pytest.raises(ValidationError):
        build(action, hold_ms=hold)


@pytest.mark.parametrize(
    "action",
    [ActionType.CLICK, ActionType.RIGHT_CLICK, ActionType.DOUBLE_CLICK],
    ids=lambda a: a.value,
)
@pytest.mark.parametrize("hold", [-1, MAX_HOLD_MS + 1, "500", 5.0, True, "abc", 250.5])
def test_b4_backend_defense_keeps_the_strict_range_and_type_gate(
    action: ActionType, hold: Any
) -> None:
    """The backend defense gate is unchanged by the widening: bool, non-int, out of range."""
    with pytest.raises(ValueError, match="hold_ms"):
        _validate_action_defense(bypass(action, hold_ms=hold))


@pytest.mark.parametrize("action", NON_HOLD_ACTIONS, ids=[a.value for a in NON_HOLD_ACTIONS])
@pytest.mark.parametrize("hold", [250, -1, MAX_HOLD_MS + 1, "500", 5.0, True])
def test_b5_backend_defense_rejects_bad_hold_ms_on_non_hold_actions(
    action: ActionType, hold: Any
) -> None:
    """A ``model_construct`` bypass carrying hold_ms anywhere illegal still dies at the
    backend gate, before a single engine call could be made."""
    with pytest.raises(ValueError, match="hold_ms"):
        _validate_action_defense(bypass(action, hold_ms=hold))


def test_b6_reject_reason_codes_do_not_drift() -> None:
    """D7: the typed vocabulary is ADDITIVE-ONLY. ``hold_ms_not_allowed`` keeps its exact
    spelling and no new ``hold_ms_*`` code may appear (a client matches on these)."""
    backend = FakeComputerBackend()
    observation = backend.observe()
    illegal = GroundingValidator().validate(bypass(ActionType.DOUBLE_CLICK, hold_ms=250), observation)
    assert illegal.valid is False
    assert "hold_ms_not_allowed" in illegal.codes

    for bad in (9_999, -5, "250", True, 1.5):
        outcome = GroundingValidator().validate(bypass(ActionType.CLICK, hold_ms=bad), observation)
        assert outcome.valid is False
        assert "hold_ms_invalid" in outcome.codes

    observed_codes = {code for code in [*illegal.codes] if code.startswith("hold_ms")}
    assert observed_codes <= {"hold_ms_not_allowed", "hold_ms_invalid"}, observed_codes


@pytest.mark.parametrize("action", list(ActionType), ids=[a.value for a in ActionType])
@pytest.mark.parametrize("hold", [None, "omitted"])
def test_b7_hold_ms_absent_is_legal_on_every_action(action: ActionType, hold: Any) -> None:
    """``None``/absent never trips the hold gate anywhere — the legacy path for all 13."""
    kwargs = {} if hold == "omitted" else {"hold_ms": None}
    assert build(action, **kwargs).hold_ms is None


def test_b8_hold_actions_keeps_the_pre_existing_pair() -> None:
    """The widening is additive: ``click``/``right_click`` stay legal."""
    assert PRE_EXISTING_HOLD_SET <= set(GroundedAction.HOLD_ACTIONS)
    assert set(GroundedAction.HOLD_ACTIONS) <= HOLD_SET


@WINDOWS_ONLY
@pytest.mark.parametrize("hold", [None, "omitted"])
@pytest.mark.parametrize(
    ("action", "keys"),
    [(ActionType.KEYPRESS, ["right"]), (ActionType.HOTKEY, ["ctrl", "c"])],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_b9_key_actions_without_hold_ms_are_byte_identical_legacy(
    dispatching_backend,
    monkeypatch: pytest.MonkeyPatch,
    hold: Any,
    action: ActionType,
    keys: list[str],
) -> None:
    """AC-6 / D5: exactly ONE ``engine.chord`` call, zero key-down/key-up events, zero waits.

    This is the regression the whole design pivots on (``hold_ms is None`` is the legacy
    path, byte-identical), so it must be green BEFORE and AFTER the change.
    """
    engine = _KeyRecordingEngine()
    install_engine(dispatching_backend, engine, monkeypatch)
    recorder = _SleepRecorder(lambda: list(engine.keys_down))
    monkeypatch.setattr(backend_module.time, "sleep", recorder)

    kwargs = {} if hold == "omitted" else {"hold_ms": None}
    message = dispatching_backend.execute(build(action, keys=keys, **kwargs))
    assert message == f"Executed {action.value}."
    assert engine.calls == [("chord", *keys)]
    assert engine.key_calls == []
    assert recorder.sleeps == []
    assert engine.keys_down == []


@WINDOWS_ONLY
@pytest.mark.parametrize(
    ("action", "keys"),
    [(ActionType.KEYPRESS, ["right"]), (ActionType.HOTKEY, ["ctrl", "c"])],
    ids=lambda v: v if isinstance(v, str) else "",
)
@pytest.mark.parametrize("hold", [None, "omitted"])
def test_b10_fake_backend_key_actions_without_hold_ms_do_not_wait(
    monkeypatch: pytest.MonkeyPatch, action: ActionType, keys: list[str], hold: Any
) -> None:
    """The fake mirrors the real contract: no hold means no hold wait."""
    backend = FakeComputerBackend()
    recorder = _SleepRecorder()
    monkeypatch.setattr(backend_module.time, "sleep", recorder)
    kwargs = {} if hold == "omitted" else {"hold_ms": None}

    message = backend.execute(build(action, keys=keys, **kwargs))
    assert message == f"Simulated {action.value}."
    assert recorder.sleeps == []
    assert [a.action for a in backend.executed] == [action]


# =================================================================================================
# C — DISPATCH SEMANTICS. The behavioral core.
# =================================================================================================


@WINDOWS_ONLY
def test_c1_held_keypress_downs_then_holds_then_ups(
    dispatching_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: key-DOWN -> the key is STILL DOWN across the wait -> key-UP."""
    engine = _KeyRecordingEngine()
    install_engine(dispatching_backend, engine, monkeypatch)
    recorder = _SleepRecorder(lambda: list(engine.keys_down))
    monkeypatch.setattr(backend_module.time, "sleep", recorder)

    message = dispatching_backend.execute(keypress(["right"], hold_ms=250))
    assert message == "Executed keypress."
    assert engine.key_calls == [("key_down", "right"), ("key_up", "right")]
    assert recorder.sleeps == [0.25]
    assert recorder.snapshots == [["right"]], "the key must still be down when the wait starts"
    assert engine.keys_down == []


@WINDOWS_ONLY
def test_c2_held_hotkey_presses_every_key_and_releases_in_reverse(
    dispatching_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3 / D1: simultaneous press, reverse-order release, shift-style hygiene."""
    engine = _KeyRecordingEngine()
    install_engine(dispatching_backend, engine, monkeypatch)
    recorder = _SleepRecorder(lambda: list(engine.keys_down))
    monkeypatch.setattr(backend_module.time, "sleep", recorder)

    message = dispatching_backend.execute(hotkey(["ctrl", "c"], hold_ms=250))
    assert message == "Executed hotkey."
    assert engine.key_calls == [
        ("key_down", "ctrl"),
        ("key_down", "c"),
        ("key_up", "c"),
        ("key_up", "ctrl"),
    ]
    assert recorder.snapshots == [["ctrl", "c"]], "both keys are down for the whole duration"
    assert engine.keys_down == []


@WINDOWS_ONLY
def test_c3_held_three_key_hotkey_releases_exactly_in_reverse(
    dispatching_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Release order is the strict reverse of press order for every member, not just two."""
    engine = _KeyRecordingEngine()
    install_engine(dispatching_backend, engine, monkeypatch)
    recorder = _SleepRecorder(lambda: list(engine.keys_down))
    monkeypatch.setattr(backend_module.time, "sleep", recorder)

    dispatching_backend.execute(hotkey(["ctrl", "shift", "c"], hold_ms=120))
    assert engine.key_calls == [
        ("key_down", "ctrl"),
        ("key_down", "shift"),
        ("key_down", "c"),
        ("key_up", "c"),
        ("key_up", "shift"),
        ("key_up", "ctrl"),
    ]
    assert recorder.snapshots == [["ctrl", "shift", "c"]]


@WINDOWS_ONLY
def test_c4_zero_hold_takes_the_split_path_not_a_chord_fast_path(
    dispatching_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2: ``hold_ms=0`` is an explicit zero-duration split press, never a chord shortcut."""
    engine = _KeyRecordingEngine()
    install_engine(dispatching_backend, engine, monkeypatch)
    recorder = _SleepRecorder(lambda: list(engine.keys_down))
    monkeypatch.setattr(backend_module.time, "sleep", recorder)

    dispatching_backend.execute(keypress(["right"], hold_ms=0))
    assert engine.key_calls == [("key_down", "right"), ("key_up", "right")]
    assert engine.calls == [], "hold_ms=0 must NOT fall back to the atomic chord batch"
    assert recorder.sleeps == [0.0]
    assert recorder.snapshots == [["right"]]
    assert engine.keys_down == []


@WINDOWS_ONLY
def test_c5_stop_mid_hold_is_honoured_within_one_slice_and_releases(
    dispatching_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE STUCK-KEY TEST: a mid-hold stop releases the key on the exception path."""
    engine = _KeyRecordingEngine()
    install_engine(dispatching_backend, engine, monkeypatch)
    observed_down: list[list[str]] = []
    stop = _SliceRecordingStop(on_wait=lambda: observed_down.append(list(engine.keys_down)))
    timer = stop.start()
    try:
        started = wall_time.monotonic()
        with pytest.raises(TaskStopped):
            dispatching_backend.execute(keypress(["right"], hold_ms=2_000), stop=stop)
        elapsed = wall_time.monotonic() - started
    finally:
        timer.cancel()

    assert stop.slices, "the hold wait must consult the stop token in slices"
    assert all(timeout is not None and 0 < timeout <= HOLD_SLICE_SECONDS for timeout in stop.slices)
    assert elapsed < HOLD_SLICE_SECONDS * 4, (
        f"a 2000 ms hold aborted in {elapsed:.3f}s: the wait is not sliced "
        f"(slice={HOLD_SLICE_SECONDS})"
    )
    assert observed_down and observed_down[0] == ["right"], "the key was down when the stop fired"
    assert engine.key_calls == [("key_down", "right"), ("key_up", "right")], "the key was left down"
    assert engine.keys_down == []


@WINDOWS_ONLY
def test_c6_exception_mid_hold_propagates_and_still_releases_the_key(
    dispatching_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any mid-hold failure: the original error surfaces AND the key comes back up."""
    engine = _KeyRecordingEngine()
    install_engine(dispatching_backend, engine, monkeypatch)

    with pytest.raises(RuntimeError, match="synthetic mid-hold failure"):
        dispatching_backend.execute(keypress(["right"], hold_ms=250), stop=_SyntheticWaitFailure())
    assert engine.key_calls == [("key_down", "right"), ("key_up", "right")]
    assert engine.keys_down == []


@WINDOWS_ONLY
def test_c7_exception_mid_hold_releases_every_held_hotkey_key(
    dispatching_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stuck-KEY worst case is a held MODIFIER; the release must cover the whole set."""
    engine = _KeyRecordingEngine()
    install_engine(dispatching_backend, engine, monkeypatch)

    with pytest.raises(RuntimeError, match="synthetic mid-hold failure"):
        dispatching_backend.execute(
            hotkey(["ctrl", "shift", "c"], hold_ms=250), stop=_SyntheticWaitFailure()
        )
    assert engine.key_calls == [
        ("key_down", "ctrl"),
        ("key_down", "shift"),
        ("key_down", "c"),
        ("key_up", "c"),
        ("key_up", "shift"),
        ("key_up", "ctrl"),
    ]
    assert engine.keys_down == []


@WINDOWS_ONLY
def test_c8_prestopped_token_performs_zero_key_inputs(
    dispatching_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop already armed before the call dispatches NOTHING — no orphan key-down."""
    engine = _KeyRecordingEngine()
    install_engine(dispatching_backend, engine, monkeypatch)
    stop = StopToken()
    stop.stop()
    with pytest.raises(TaskStopped):
        dispatching_backend.execute(keypress(["right"], hold_ms=250), stop=stop)
    assert engine.key_calls == []
    assert engine.keys_down == []


@WINDOWS_ONLY
def test_c9_no_token_path_still_releases_the_key_on_error(
    dispatching_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stop=None`` (a bare ``time.sleep`` wait): a failing wait still releases."""
    engine = _KeyRecordingEngine()
    install_engine(dispatching_backend, engine, monkeypatch)
    monkeypatch.setattr(backend_module.time, "sleep", _RaisingSleep.sleep)
    with pytest.raises(RuntimeError, match="synthetic no-token failure"):
        dispatching_backend.execute(keypress(["right"], hold_ms=250))
    assert engine.key_calls == [("key_down", "right"), ("key_up", "right")]
    assert engine.keys_down == []


@WINDOWS_ONLY
def test_c10_mid_hold_stop_is_reported_as_dispatched_not_cancelled(
    dispatching_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key-down already left the process, so a mid-hold stop must be honestly reported."""
    engine = _KeyRecordingEngine()
    install_engine(dispatching_backend, engine, monkeypatch)
    action = keypress(["right"], hold_ms=250)
    with pytest.raises(TaskStopped):
        dispatching_backend.execute(action, stop=_StopDuringHoldWait())
    assert action._dispatch_started is True
    assert engine.key_calls == [("key_down", "right"), ("key_up", "right")], (
        "an interrupted hold must still release the key it pressed"
    )
    assert engine.keys_down == []


@WINDOWS_ONLY
def test_c11_double_click_exclusion_survives_the_widened_set(
    dispatching_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5 / D6: ``double_click`` is still refused at all FOUR sites, before any motion."""
    engine = _KeyRecordingEngine()
    install_engine(dispatching_backend, engine, monkeypatch)
    observation = FakeComputerBackend().observe()

    with pytest.raises(ValidationError, match="hold_ms"):
        build(ActionType.DOUBLE_CLICK, hold_ms=250)

    outcome = GroundingValidator().validate(bypass(ActionType.DOUBLE_CLICK, hold_ms=250), observation)
    assert outcome.valid is False
    assert "hold_ms_not_allowed" in outcome.codes

    with pytest.raises(ValueError, match="hold_ms"):
        _validate_action_defense(bypass(ActionType.DOUBLE_CLICK, hold_ms=250))

    with pytest.raises(ValueError, match="hold_ms"):
        dispatching_backend.execute(bypass(ActionType.DOUBLE_CLICK, point=Point(x=30, y=45), hold_ms=250))
    assert engine.key_calls == []
    assert engine.calls == []


@WINDOWS_ONLY
def test_c12_fake_backend_honours_the_requested_hold_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fake-backend parity: the fake must MODEL the hold, not skip it."""
    backend = FakeComputerBackend()
    recorder = _SleepRecorder()
    monkeypatch.setattr(backend_module.time, "sleep", recorder)
    message = backend.execute(keypress(["right"], hold_ms=250))
    assert message == "Simulated keypress."
    assert recorder.sleeps == [0.25]


@WINDOWS_ONLY
def test_c13_fake_backend_hold_wait_is_sliced_and_interruptible() -> None:
    """The fake reuses the SAME sliced, interruptible wait (``_sleep_for_hold``, D4)."""
    backend = FakeComputerBackend()
    stop = _SlicingProbeStop()
    with pytest.raises(TaskStopped):
        backend.execute(keypress(["right"], hold_ms=250), stop=stop)
    assert stop.slices, "the fake must wait in slices (a bare sleep is not interruptible)"
    assert all(timeout is not None and 0 < timeout <= HOLD_SLICE_SECONDS for timeout in stop.slices)


@WINDOWS_ONLY
def test_c14_fake_backend_hold_releases_on_a_mid_hold_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fake must record the interrupted hold as NOT completed."""
    backend = FakeComputerBackend()
    action = keypress(["right"], hold_ms=250)
    with pytest.raises(TaskStopped):
        backend.execute(action, stop=_StopDuringHoldWait())
    assert backend.executed == [], "an aborted hold must not be recorded as completed"
    assert action._dispatch_started is True
    assert backend.held_keys == [], "an interrupted hold must clear the simulated keyboard"


# --- release-discipline regression tests (L4.1) --------------------------------------------------


@WINDOWS_ONLY
def test_c15_release_failure_never_masks_the_in_flight_error(
    dispatching_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whichever error was already unwinding, the release error is dropped, not raised over it."""
    for stop, expected, message in (
        (_StopDuringHoldWait(), TaskStopped, "Task stop requested"),
        (_SyntheticWaitFailure(), RuntimeError, "synthetic mid-hold failure"),
    ):
        engine = _FailingReleaseEngine(fail_at=1)
        install_engine(dispatching_backend, engine, monkeypatch)
        with pytest.raises(expected) as caught:
            dispatching_backend.execute(hotkey(["ctrl", "shift", "c"], hold_ms=250), stop=stop)
        assert message in str(caught.value), (
            f"the release error masked the in-flight {expected.__name__}: {caught.value!r}"
        )
        assert engine.releases == 3, (
            f"a release failure abandoned the sweep after {engine.releases} of 3 key-ups"
        )
        assert engine.keys_down == []


@WINDOWS_ONLY
def test_c16_every_release_is_attempted_even_when_an_earlier_one_raises(
    dispatching_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release sweep is exhaustive on its OWN terms — the stuck-MODIFIER case."""
    engine = _FailingReleaseEngine(fail_at=1)
    install_engine(dispatching_backend, engine, monkeypatch)
    recorder = _SleepRecorder()
    monkeypatch.setattr(backend_module.time, "sleep", recorder)

    with pytest.raises(RuntimeError, match=r"synthetic key_up failure #1"):
        dispatching_backend.execute(hotkey(["ctrl", "shift", "c"], hold_ms=250))

    assert recorder.sleeps == [0.25], "the hold completed normally on this path"
    assert engine.key_calls == [
        ("key_down", "ctrl"),
        ("key_down", "shift"),
        ("key_down", "c"),
        ("key_up", "c"),  # 1st release: raised
        ("key_up", "shift"),  # attempted anyway
        ("key_up", "ctrl"),  # attempted anyway
    ]
    assert engine.keys_down == []


@WINDOWS_ONLY
def test_c17_partial_key_down_releases_only_the_keys_that_went_down(
    dispatching_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither error of a partial press: no stuck key, and no phantom key-up."""
    engine = _FailingPressEngine(fail_at=2)
    install_engine(dispatching_backend, engine, monkeypatch)

    with pytest.raises(RuntimeError, match=r"synthetic key_down failure #2"):
        dispatching_backend.execute(hotkey(["ctrl", "shift", "c"], hold_ms=250))

    assert engine.key_calls == [
        ("key_down", "ctrl"),
        ("key_up", "ctrl"),
    ]
    assert engine.keys_down == []


# =================================================================================================
# D — ENGINE CONTRACT.
# =================================================================================================


def test_d1_input_engine_abc_exposes_key_down_and_key_up() -> None:
    """The ABC must grow the key-down/key-up contract (additive, non-abstract)."""
    down, up = resolves_key_primitives(InputEngine)
    assert down is not None, (
        f"InputEngine exposes no key-down primitive (looked for {_KEY_DOWN_NAMES}); "
        f"it has only {sorted(n for n in vars(InputEngine) if not n.startswith('_'))}"
    )
    assert up is not None, f"InputEngine exposes no key-up primitive (looked for {_KEY_UP_NAMES})"


def test_d2_key_primitives_are_additive_and_fail_honestly_on_the_base() -> None:
    """D3, restated by DELTA 1 (F3): the ABC addition stays non-abstract (a third-party
    stub keeps CONSTRUCTING), but the base default now FAILS HONESTLY at call time — a
    held keypress on an engine without the pair must never report success."""
    down, up = resolves_key_primitives(InputEngine)
    assert down is not None and up is not None
    for name in (down, up):
        assert not getattr(getattr(InputEngine, name), "__isabstractmethod__", False), (
            f"InputEngine.{name} must NOT be abstract (a stub must construct)"
        )
    # The base default raises the typed error instead of silently succeeding.
    stub = _MinimalStubEngine()
    with pytest.raises(UnsupportedActionError, match="key_down/key_up"):
        getattr(stub, down)("a")
    with pytest.raises(UnsupportedActionError, match="key_down/key_up"):
        getattr(stub, up)("a")


def test_d3_engine_without_the_pair_fails_a_held_keypress_honestly(
    dispatching_backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3 end-to-end, restated by DELTA 1 (F3): an engine that never implemented
    key-down/key-up FAILS a held keypress with the typed error — no "Executed
    keypress." lie, no chord, no input, no hold wait."""
    assert declares_key_primitives(_MinimalStubEngine) == (None, None), (
        "the D3 stub must declare NO key primitive of its own"
    )
    stub = _MinimalStubEngine()
    install_engine(dispatching_backend, stub, monkeypatch)
    recorder = _SleepRecorder()
    monkeypatch.setattr(backend_module.time, "sleep", recorder)

    with pytest.raises(UnsupportedActionError, match="key_down/key_up"):
        dispatching_backend.execute(keypress(["right"], hold_ms=120))
    assert stub.calls == [], "no chord may be dispatched for a HELD keypress"
    assert recorder.sleeps == [], "the hold wait is never reached when the press fails"


@WINDOWS_ONLY
@pytest.mark.parametrize("engine_cls", [PyAutoGuiInputEngine, SendInputEngine], ids=lambda c: c.__name__)
def test_d4_both_concrete_engines_implement_the_key_primitives(
    engine_cls: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both shipped engines override the base no-op — proven by BEHAVIOUR, not a name."""
    down, up = resolves_key_primitives(InputEngine)
    assert down is not None and up is not None

    sent: list[Any] = []

    if engine_cls is SendInputEngine:
        engine = engine_cls()
        # HERMETICITY (WP7b carry-over, VAL-WP7a full-suite flake): the press path
        # routes through ``_send``, whose failsafe pre-check reads the LIVE cursor
        # (``get_cursor_position``) — a full-suite run with the desktop cursor
        # parked in a screen corner raised InputBlockedError before any event
        # reached the stub, failing a purely structural node. The corner check is
        # stubbed FALSE on the instance (the same seam ``_dispatch`` already
        # stubs); the dispatch-event assertions below are UNCHANGED and no real
        # input is ever sent.
        monkeypatch.setattr(engine, "_cursor_in_failsafe_corner", lambda: False)
        # DELTA 1 (F1): releases route through ``_send_release``, whose corner check
        # is skipped — record the COMMON dispatch tail (``_dispatch``) so the
        # behaviour proof covers both the press and the release path.
        monkeypatch.setattr(engine, "_dispatch", lambda events: (sent.extend(events), len(events))[1])
    else:  # pyautogui fallback: the module is CONSTRUCTOR-INJECTED
        stub = _StubPyautogui()
        engine = engine_cls(stub)
        getattr(engine, down)("right")
        getattr(engine, up)("right")
        assert stub.pressed == ["right"], f"{engine_cls.__name__} did not press the key"
        assert stub.released == ["right"], f"{engine_cls.__name__} did not release the key"
        assert (stub.PAUSE, stub.FAILSAFE) == (0.0, True), (
            f"{engine_cls.__name__} ignored the injected module's configuration"
        )
        return

    # ``_INPUT`` is ``{type, union}``; the keyboard fields live at ``union.ki``.
    getattr(engine, down)("right")
    assert sent, f"{engine_cls.__name__}.{down} dispatched no event"
    assert all(event.union.ki.dwFlags == 0 for event in sent), (
        f"{engine_cls.__name__}.{down} must queue flags-0 (key-DOWN) events"
    )
    first_vk = sent[0].union.ki.wVk
    sent.clear()
    getattr(engine, up)("right")
    assert sent, f"{engine_cls.__name__}.{up} dispatched no event"
    assert all(event.union.ki.dwFlags == _KEYEVENTF_KEYUP for event in sent), (
        f"{engine_cls.__name__}.{up} must queue exactly _KEYEVENTF_KEYUP (== 2)"
    )
    assert sent[0].union.ki.wVk == first_vk


def test_d5_chord_units_press_and_release_halves_are_separable() -> None:
    """The event model a held key relies on is ALREADY correct: hold is a SPLIT."""
    units = _chord_key_units(["ctrl", "shift", "c"])
    first_up = next(i for i, (_, _, flags) in enumerate(units) if flags & _KEYEVENTF_KEYUP)
    press, release = units[:first_up], units[first_up:]
    assert all(flags == 0 for _, _, flags in press)
    assert all(flags & _KEYEVENTF_KEYUP for _, _, flags in release)
    assert [vk for vk, _, _ in press] == [vk for vk, _, _ in reversed(release)]
    assert len(release) == len(press), "one release per press: no double-release"


# =================================================================================================
# E — RESULT-PAYLOAD ECHO (L4.5). E1 is the QUEUE-tier echo; the PLAN-tier echo (E2 in
# the dev tree) is DEFERRED to WP7 with the waypoint-plan surface.
# =================================================================================================


#: The two key actions a hold is now legal on, with the keys each needs on the wire.
_KEY_HOLD_SPECS: tuple[tuple[str, list[str]], ...] = (
    ("keypress", ["right"]),
    ("hotkey", ["ctrl", "c"]),
)


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Per-module session isolation (the same fixture the other server-level modules
    declare locally)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", server.SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    yield server


@pytest.mark.parametrize(("action", "keys"), _KEY_HOLD_SPECS)
async def test_e1_follow_up_results_echo_the_hold_of_a_held_key(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, action: str, keys: list[str]
) -> None:
    """L4.5: a ``follow_ups``-carried key hold echoes ``hold_ms`` in its own result entry.

    The primary click carries no ``hold_ms``, which pins the additive half at the same
    time — an entry for a call without a hold must not grow the key, so the echo is
    attributable to the queued key and not to something the queue stamps unconditionally.
    """
    session_id, _bundle, backend, _ = make_session(
        monkeypatch,
        backend=ScriptedBackend(flip=True),
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    payload = execute_payload(
        await server.computer_execute(
            session_id,
            "click",
            x=10,
            y=10,
            follow_ups=[{"action": action, "keys": keys, "hold_ms": 250}],
            include_screenshot_after=False,
        )
    )
    assert payload["ok"] is True, payload
    entries = payload["follow_up_results"]
    assert [entry["index"] for entry in entries] == [0, 1]
    assert entries[0]["action_type"] == "click"
    assert "hold_ms" not in entries[0], "an entry without a hold must not grow the key"
    assert entries[1]["action_type"] == action
    assert entries[1]["hold_ms"] == 250, "the requested hold must round-trip EXACTLY"
    # The dispatched twin, so a passing echo can never be an unheld keypress that merely
    # reported a field: the queue entry and the backend agree on the same 250 ms.
    assert backend.executed[1].action.value == action
    assert backend.executed[1].hold_ms == 250
