"""E-01 settle-wiring proof (WP2): a DECLARED settle actually reaches the backend sleep.

ORVEX-2026-102 §5.2 (SPEC 5.2) made the settle a thing the MODEL STATES: the plan
declares ``settle_ms`` per point, the (WP7) normalizer stamps it onto the point's
LAST item as the INTERNAL ``GroundedAction.plan_point_settle_ms``, and the backend's
``execute()`` tail SLEEPS exactly that value (via ``_plan_point_settle_seconds``).
The dev tree's tests asserted only the plan POLICY dict — the wiring gap E-01: the
declared value never reached a sleep anyone measured. This module proves the CONSUMER
side end-to-end through the real backend settle path:

- ``plan_point_settle_ms=200`` with the marker ``True`` -> exactly one ``time.sleep(0.2)``
  issued AFTER the dispatch;
- ``plan_point_settle_ms=0`` -> the same marked path, ZERO sleeps (0 is a meaningful
  "this point settles not at all", not an absent declaration);
- ``plan_point_settle_ms=None`` -> the module policy fallback
  (``_MOVE_SETTLE_SECONDS``; 0.0 on the shipped path, so no sleep);
- the marker absent (``None``) -> the exact legacy policy (nothing sleeps on the
  shipped default; the unmarked move branch only, never the execute tail).

WP7-SEAM(plan-settle-stamping): the PRODUCER that stamps ``plan_point_settle`` /
``plan_point_settle_ms`` from a waypoint's ``settle_ms`` is the WP7 plan normalizer
(``plan.py`` does not exist yet). This module pins the consumer contract that
producer must satisfy.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from recording_engine import RecordingEngine

import computer_use_mcp.backend as backend_module
from computer_use_mcp.backend import FakeComputerBackend, LocalComputerBackend
from computer_use_mcp.models import GroundedAction

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")


class _SleepRecorder:
    """Records every ``time.sleep`` argument with its wall-clock moment (test clock)."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.moments: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.moments.append(time.perf_counter())


@pytest.fixture
def recorded_sleeps(monkeypatch: pytest.MonkeyPatch) -> _SleepRecorder:
    recorder = _SleepRecorder()
    monkeypatch.setattr(backend_module.time, "sleep", recorder)
    return recorder


def _click(point: tuple[int, int], **kw: Any) -> GroundedAction:
    kw.setdefault("confidence", 1.0)
    return GroundedAction(action="click", point={"x": point[0], "y": point[1]}, **kw)


@WINDOWS_ONLY
def test_e01_declared_settle_200_ms_is_slept_after_the_dispatch(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """THE E-01 proof: ``plan_point_settle_ms=200`` sleeps EXACTLY 200 ms — once,
    after the dispatch (the click lands first, the settle second)."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    dispatch_moment = time.perf_counter()
    message = real_backend.execute(_click((10, 10), plan_point_settle=True, plan_point_settle_ms=200))
    assert message == "Executed click."
    assert engine.calls == [("click", 10, 10, 1, "left")]  # the input really dispatched
    assert recorded_sleeps.sleeps == [0.2], recorded_sleeps.sleeps
    assert recorded_sleeps.moments[0] > dispatch_moment  # the settle is AFTER the dispatch


@WINDOWS_ONLY
def test_e01_declared_settle_zero_sleeps_nothing(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """``plan_point_settle_ms=0`` is a DECLARED "no settle": the same marked path,
    zero sleeps — 0 is meaningful, not an absent declaration."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    message = real_backend.execute(_click((10, 10), plan_point_settle=True, plan_point_settle_ms=0))
    assert message == "Executed click."
    assert engine.calls == [("click", 10, 10, 1, "left")]
    assert recorded_sleeps.sleeps == []


@WINDOWS_ONLY
def test_e01_undeclared_settle_falls_back_to_the_module_policy(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """``plan_point_settle_ms=None`` (no declared settle) -> the module policy
    ``_MOVE_SETTLE_SECONDS``. On the SHIPPED path that policy is 0.0 (SPEC 5.1), so a
    marked point with no declared settle settles nothing; the fallback RESOLUTION is
    proved with an explicit operator-settle pinned (the same resolution the v0.7.5
    default used to express)."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    # shipped policy: None -> 0.0 -> no sleep
    real_backend.execute(_click((10, 10), plan_point_settle=True, plan_point_settle_ms=None))
    assert recorded_sleeps.sleeps == []
    # the resolution itself: policy 0.042 -> the marked item settles 0.042
    monkeypatch.setattr(backend_module, "_MOVE_SETTLE_SECONDS", 0.042)
    real_backend.execute(_click((11, 11), plan_point_settle=True, plan_point_settle_ms=None))
    assert recorded_sleeps.sleeps == [0.042]


@WINDOWS_ONLY
def test_e01_unmarked_action_never_enters_the_execute_tail(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """The legacy invariant is untouched: an UNMARKED action (``plan_point_settle``
    None — every non-plan action) never sleeps in the ``execute()`` tail, on the
    shipped policy AND under an explicit operator settle (the tail is marker-gated;
    only the unmarked MOVE branch consults the policy, and a click is not a move)."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    monkeypatch.setattr(backend_module, "_MOVE_SETTLE_SECONDS", 0.042)
    real_backend.execute(_click((10, 10)))
    assert engine.calls == [("click", 10, 10, 1, "left")]
    assert recorded_sleeps.sleeps == []


@WINDOWS_ONLY
def test_e01_declared_settle_covers_the_timed_move_tail(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """A DECLARED settle on a point whose last element is a TIMED MOVE: the motion
    pacing sleeps (11 x 12 ms) and the declared 30 ms settle rides the SAME tail —
    the declared value wins over the module policy."""
    settle = 0.030
    monkeypatch.setattr(real_backend, "_engine", RecordingEngine())
    monkeypatch.setattr(real_backend, "_active_context", None)
    monkeypatch.setattr(backend_module, "get_cursor_position", lambda: (10, 10))
    monkeypatch.setattr(real_backend, "probe_cursor_position", lambda: None)
    action = GroundedAction(
        action="move",
        point={"x": 30, "y": 45},
        duration_ms=120,
        confidence=1.0,
        plan_point_settle=True,
        plan_point_settle_ms=30,
    )
    real_backend.execute(action)
    assert recorded_sleeps.sleeps == [0.012] * 11 + [settle]
    assert recorded_sleeps.sleeps[-1] == settle  # the settle is the LAST sleep


@WINDOWS_ONLY
def test_e01_fake_backend_never_settles(real_backend: LocalComputerBackend) -> None:
    """The harness fake simulates no timing (the O3 precedent): a declared settle on
    the fake records the action and performs zero sleeps of its own."""
    fake = FakeComputerBackend()
    observation = fake.observe()
    sleeps = _SleepRecorder()
    original_sleep = backend_module.time.sleep
    backend_module.time.sleep = sleeps
    try:
        action = _click(
            (10, 10),
            plan_point_settle=True,
            plan_point_settle_ms=200,
            source_observation_id=observation.observation_id,
        )
        message = fake.execute(action)
    finally:
        backend_module.time.sleep = original_sleep
    assert message == "Simulated click."
    assert len(fake.executed) == 1
    assert sleeps.sleeps == []


def test_e01_the_declared_value_beats_the_module_policy_and_the_marker_gates_the_tail() -> None:
    """The resolution rule, pinned on the helper itself (no backend needed):
    declared wins; None falls back to the module policy; the tail is marker-gated."""
    declared = GroundedAction.model_construct(
        action="click", plan_point_settle=True, plan_point_settle_ms=200
    )
    assert backend_module._plan_point_settle_seconds(declared) == 0.2
    undeclared = GroundedAction.model_construct(
        action="click", plan_point_settle=True, plan_point_settle_ms=None
    )
    assert backend_module._plan_point_settle_seconds(undeclared) == (
        backend_module._MOVE_SETTLE_SECONDS
    )
    assert GroundedAction(action="click", point={"x": 1, "y": 2}).plan_point_settle is None
