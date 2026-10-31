"""MOVE SETTLE POLICY tests: the cursor-reposition settle (WP2, ORVEX-2026-101
Batch 3, P3.2 + RFC D15; ORVEX-2026-102 Phase 5, SPEC 5.1/5.2).

**The shipped default is now 0 ms (SPEC 5.1).** Outside a plan the move is INSTANT
and the settle is 0 ms, so the hidden 50 ms on every mouse action is gone (a
BEHAVIOR CHANGE vs v0.7.5, recorded for the VERSIONS Compatibility notes). The
env read follows the established block pattern: unset -> **0.0**, a valid value
inside the **0..200** band applies literally, and garbage/negative/blank/NaN fall
back to the default. The band's low edge moved 50 -> 0, so ``0`` and ``10`` are now
LEGAL values (they used to silently resolve to 50).

Inside plans the settle applies ONCE after each plan point's LAST action
(``plan_point_settle=True`` on that item; transition moves of points WITH actions
carry ``False``), and (SPEC 5.2) a plan STATES the settle it wants as ``settle_ms``
— carried on the INTERNAL ``GroundedAction.plan_point_settle_ms`` and consumed by
the backend dispatch tail (E-01's wiring; proven end-to-end in
tests/test_plan_settle_wiring.py). The PLAN-side producer (the normalizer that
stamps the marker from a waypoint's ``settle_ms``) is the WP7 seam.

PORT NOTE (WP2, stable tree): the dev tree's plan-drive nodes here (the
``plan=`` submissions asserting the queue's marker attachment) are DEFERRED to
WP7 with the plan surface. The ``follow_ups``-without-plan node is ported: the
queue attachment is plan-scoped only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any

import pytest
from recording_engine import RecordingEngine
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    execute_payload,
    make_session,
)

import computer_use_mcp.desktop.backend as backend_module
import computer_use_mcp.server.lifecycle as srv_server_lifecycle
import computer_use_mcp.server.wait_machine as srv_server_wait_machine
import computer_use_mcp.server.wait as srv_server_wait
import computer_use_mcp.core.state as srv_core_state
import computer_use_mcp.server.execute as srv_server_execute
from computer_use_mcp.desktop.backend import LocalComputerBackend
from computer_use_mcp.core.models import ActionSpec, GroundedAction, Point

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")


def move(point: tuple[int, int], **kwargs: Any) -> GroundedAction:
    kwargs.setdefault("confidence", 1.0)
    return GroundedAction(action="move", point={"x": point[0], "y": point[1]}, **kwargs)


def click(point: tuple[int, int], **kwargs: Any) -> GroundedAction:
    kwargs.setdefault("confidence", 1.0)
    return GroundedAction(action="click", point={"x": point[0], "y": point[1]}, **kwargs)


class _SleepRecorder:
    """Records every ``time.sleep`` call with its wall-clock moment (test clock)."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.moments: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.moments.append(time.perf_counter())


@pytest.fixture
def recorded_sleeps(monkeypatch: pytest.MonkeyPatch) -> _SleepRecorder:
    """Patch the backend's ``time.sleep`` (the established test_r04/test_input_engines
    pattern) and return the recorder. The shipped settle value stays whatever the
    module resolved — tests that need a distinctive value monkeypatch it explicitly."""
    recorder = _SleepRecorder()
    monkeypatch.setattr(backend_module.time, "sleep", recorder)
    return recorder


#: SPEC 5.1 made the SHIPPED settle 0.0 ms, which would make every "the settle lands
#: here" assertion vacuous (an empty sleep list satisfies every count, and there is no
#: settle to compare a recorded moment against). The PLACEMENT and COUNT rules are what
#: SPEC 5.1 does NOT change, so the tests that prove them pin this distinctive explicit
#: settle instead of reading the now-zero module default.
EXPLICIT_SETTLE_SECONDS = 0.042  # deliberately not a "round" number, so it cannot alias a pacing sleep


def explicit_settle(monkeypatch: pytest.MonkeyPatch) -> float:
    """Pin a distinctive non-zero settle so the placement/count rules stay provable."""
    monkeypatch.setattr(backend_module, "_MOVE_SETTLE_SECONDS", EXPLICIT_SETTLE_SECONDS)
    return EXPLICIT_SETTLE_SECONDS


# --- the env key (P3.2 item 7): the established block pattern --------------------------------


def _probe_settle(env_value: str | None) -> dict[str, float]:
    """Read the shipped settle values from a FRESH in-process import with a controlled
    env so ambient CORTEX_* variables on the dev desktop cannot mask the behavior
    under test. WP-H DELTA 1 (m2): the former ``sys.executable -c`` child probe is
    replaced by the in-process fresh import (no subprocess at all)."""
    from fresh_backend_import import fresh_backend

    module = fresh_backend({} if env_value is None else {"CORTEX_MOVE_SETTLE_MS": env_value})
    return {"ms": module.MOVE_SETTLE_MS, "seconds": module._MOVE_SETTLE_SECONDS}


def test_shipped_settle_default_is_the_legacy_50ms() -> None:
    """No env -> **0 ms / 0.0 s** — the shipped outside-plan policy is INSTANT.

    RESTATED by ORVEX-2026-102 §5.1 (SPEC 5.1): the expectation moved from
    ``{"ms": 50.0, "seconds": 0.05}`` to ``{"ms": 0.0, "seconds": 0.0}``. This is the
    single most direct pin of the value SPEC 5.1 changes; the function NAME is kept
    verbatim so the restatement audit can find this exact function.
    """
    shipped = _probe_settle(None)
    assert shipped == {"ms": 0.0, "seconds": 0.0}


@pytest.mark.parametrize(
    "raw,expected",
    [("120", 120.0), ("50", 50.0), ("200", 200.0), (" 75 ", 75.0), ("10", 10.0), ("0", 0.0)],
)
def test_settle_env_values_in_decision_range_apply(raw: str, expected: float) -> None:
    """The accepted band is now **0..200 ms**; values in it apply literally.

    RESTATED by SPEC 5.1: the band's low edge moved 50 -> 0, so ``"10"`` and ``"0"``
    are legal values that apply instead of silently falling back to 50 (K-12: with
    the old 50..200 band a zero settle was inexpressible by ANY env route, because
    ``_env_bounded_float`` is range-validation-with-fallback, not clamping).
    """
    probed = _probe_settle(raw)
    assert probed["ms"] == expected
    assert probed["seconds"] == pytest.approx(expected / 1000.0)


@pytest.mark.parametrize("raw", ["201", "-1", "abc", "", "   ", "NaN"])
def test_settle_env_out_of_range_or_invalid_falls_back(raw: str) -> None:
    """Out-of-band/invalid still falls back — now to the 0.0 default, not 50."""
    assert _probe_settle(raw)["ms"] == 0.0


@pytest.mark.parametrize("raw", ["abc", "-5", "", "   ", "NaN"])
def test_settle_env_garbage_negative_blank_fall_back_to_default(raw: str) -> None:
    """Garbage/negative/blank/NaN fall back to the default (block pattern)."""
    assert _probe_settle(raw)["ms"] == 0.0


# --- legacy regression: outside plans the move branch stays byte-identical -------------------


@WINDOWS_ONLY
def test_regression_legacy_move_one_settle_of_the_shipped_value(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """A plain move (no env, no plan marker) keeps the legacy SHAPE — one absolute
    move event — and now issues NO settle sleep at all (SPEC 5.1; the primary
    before/after claim: the flat move's recorded ``time.sleep`` arguments go
    ``[0.05] -> []`` and the return message stays ``"Executed move."``)."""
    monkeypatch.delenv("CORTEX_MOVE_SETTLE_MS", raising=False)
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    action = move((30, 45))
    assert action.plan_point_settle is None  # the unmarked (legacy) default
    message = real_backend.execute(action)
    assert message == "Executed move."
    assert engine.calls == [("move", 30, 45)]
    assert backend_module._MOVE_SETTLE_SECONDS == 0.0
    assert recorded_sleeps.sleeps == []


@WINDOWS_ONLY
def test_regression_legacy_timed_move_settles_once_after_the_path(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """The timed path keeps its RFC §3.5 shape: pacing sleeps per sample and the
    settle ONCE at the very end (never per path point). Placement only — an explicit
    distinctive settle is pinned because the shipped settle is 0."""
    settle = explicit_settle(monkeypatch)
    monkeypatch.setattr(real_backend, "_engine", RecordingEngine())
    monkeypatch.setattr(real_backend, "_active_context", None)
    monkeypatch.setattr(backend_module, "get_cursor_position", lambda: (10, 10))
    monkeypatch.setattr(real_backend, "probe_cursor_position", lambda: None)
    real_backend.execute(move((30, 45), duration_ms=120))
    settles = [s for s in recorded_sleeps.sleeps if s == settle]
    pacing = [s for s in recorded_sleeps.sleeps if s != settle]
    assert len(settles) == 1  # settle ONCE (both paths), the E2 discipline
    assert recorded_sleeps.sleeps[-1] == settle  # at the end
    assert len(pacing) == 11  # 120ms/12ms -> 11 samples (10 segments + start), one pacing each


@WINDOWS_ONLY
def test_outside_plan_move_with_zero_settle_jumps_without_sleeping(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """settle=0 outside plans: the jump dispatches, zero settle sleep. SPEC 5.1 makes
    this the SHIPPED behaviour rather than an operator experiment."""
    assert backend_module._MOVE_SETTLE_SECONDS == 0.0  # SPEC 5.1: the default IS zero
    monkeypatch.setattr(backend_module, "_MOVE_SETTLE_SECONDS", 0.0)
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    real_backend.execute(move((30, 45)))
    assert engine.calls == [("move", 30, 45)]
    assert recorded_sleeps.sleeps == []


# --- the D15 in-plan semantics: the backend consumes the queue's marker ----------------------


@WINDOWS_ONLY
def test_plan_transition_move_skips_the_settle(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """A plan transition move whose point HAS actions (marker False) settles NOT after
    the move — the point's settle lands after its LAST action (D15). Proved with a
    NON-ZERO settle pinned, so the assertion still says "this branch never settles"."""
    explicit_settle(monkeypatch)
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    real_backend.execute(move((30, 45), plan_point_settle=False))
    assert engine.calls == [("move", 30, 45)]
    assert recorded_sleeps.sleeps == []


@WINDOWS_ONLY
def test_plan_timed_transition_skips_the_settle_but_keeps_pacing(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """The REAL in-plan transition is a timed move: the suppressed settle disappears
    while the motion pacing stays untouched."""
    monkeypatch.setattr(real_backend, "_engine", RecordingEngine())
    monkeypatch.setattr(real_backend, "_active_context", None)
    monkeypatch.setattr(backend_module, "get_cursor_position", lambda: (10, 10))
    monkeypatch.setattr(real_backend, "probe_cursor_position", lambda: None)
    real_backend.execute(move((30, 45), duration_ms=120, plan_point_settle=False))
    assert recorded_sleeps.sleeps == [0.012] * 11  # pacing only — zero settle sleeps


@WINDOWS_ONLY
def test_plan_point_end_move_settles_once_at_the_tail(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """A point whose ONLY element is the move keeps its settle (the move IS the
    point's last action): exactly one settle, after the dispatch."""
    settle = explicit_settle(monkeypatch)
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    real_backend.execute(move((30, 45), plan_point_settle=True))
    assert engine.calls == [("move", 30, 45)]
    assert recorded_sleeps.sleeps == [settle]


@WINDOWS_ONLY
def test_plan_point_end_click_settles_once_after_its_dispatch(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """THE placement proof: the settle happens AFTER the point's last action's engine
    dispatch (click first, settle second) — exactly once."""
    settle = explicit_settle(monkeypatch)
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    click_moment = time.perf_counter()
    real_backend.execute(click((30, 45), plan_point_settle=True))
    assert engine.calls == [("click", 30, 45, 1, "left")]
    assert recorded_sleeps.sleeps == [settle]
    assert recorded_sleeps.moments[0] > click_moment  # settle AFTER the click dispatch


@WINDOWS_ONLY
def test_plan_point_end_keypress_settles_once(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """The point-end settle is action-agnostic: a keypress-ending point settles once
    after its chord (the tail covers every branch, including early returns)."""
    settle = explicit_settle(monkeypatch)
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    real_backend.execute(
        GroundedAction(
            action="keypress", keys=["enter"], confidence=1.0, plan_point_settle=True
        )
    )
    assert engine.calls == [("chord", "enter")]
    assert recorded_sleeps.sleeps == [settle]


@WINDOWS_ONLY
def test_three_point_plan_settles_three_times_one_per_point_after_its_last_action(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """The literal D15 acceptance shape, [move,click]x3 with the queue's markers: THREE
    settles for the plan (one per point, never 2xN, never 0), each AFTER its point's
    click dispatch and BEFORE the next point's move. Count/placement re-proved with an
    explicit distinctive settle (the shipped zero default's own shape is asserted in
    ``test_three_point_plan_settles_zero_times_on_the_shipped_zero_default``)."""
    settle = explicit_settle(monkeypatch)
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    points = [(420, 180), (460, 260), (850, 720)]
    for x, y in points:
        real_backend.execute(move((x, y), plan_point_settle=False))  # transition: no settle
        real_backend.execute(click((x, y), plan_point_settle=True))  # point end: settles
    assert len(engine.calls) == 6
    settles = [s for s in recorded_sleeps.sleeps if s == settle]
    assert len(settles) == 3  # one per point — NOT 2/point, NOT zero
    # placement: each settle follows its own point's click (3 moves, 3 clicks, 3 settles)
    assert recorded_sleeps.sleeps == [settle] * 3


@WINDOWS_ONLY
def test_three_point_plan_settles_zero_times_on_the_shipped_zero_default(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """The SAME 3-point [move,click] flow on the shipped policy settles ZERO times
    while every input still dispatches — the before/after of the D15 shape."""
    assert backend_module._MOVE_SETTLE_SECONDS == 0.0  # SPEC 5.1 default
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    for x, y in [(420, 180), (460, 260), (850, 720)]:
        real_backend.execute(move((x, y), plan_point_settle=False))
        real_backend.execute(click((x, y), plan_point_settle=True))
    assert len(engine.calls) == 6  # every input still dispatched
    assert recorded_sleeps.sleeps == []  # and nothing slept


@WINDOWS_ONLY
def test_legacy_plan_shape_baseline_settled_after_each_transition(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """HONEST BASELINE (documented, not a Batch-3 behavior): unmarked elements — the
    pre-Batch-3 shapes — settled after EVERY transition move (clicks never settled),
    so the same 3-point plan paid 3 settles placed BETWEEN move and click. Batch 3
    relocates the per-point settle (D15); it never multiplies it."""
    settle = explicit_settle(monkeypatch)
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    for x, y in [(420, 180), (460, 260), (850, 720)]:
        real_backend.execute(move((x, y)))  # unmarked: legacy settle after the move
        real_backend.execute(click((x, y)))
    assert len(engine.calls) == 6
    assert recorded_sleeps.sleeps == [settle] * 3


@WINDOWS_ONLY
def test_plan_settle_zero_disables_every_plan_settle(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """settle=0 with a plan: the whole plan runs with ZERO settle sleeps (the A/B
    lever), while every input still dispatches."""
    assert backend_module._MOVE_SETTLE_SECONDS == 0.0  # SPEC 5.1: the default IS zero
    monkeypatch.setattr(backend_module, "_MOVE_SETTLE_SECONDS", 0.0)
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    for x, y in [(420, 180), (460, 260), (850, 720)]:
        real_backend.execute(move((x, y), plan_point_settle=False))
        real_backend.execute(click((x, y), plan_point_settle=True))
    assert len(engine.calls) == 6
    assert recorded_sleeps.sleeps == []


@WINDOWS_ONLY
def test_bypass_move_without_marker_keeps_the_legacy_settle(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """A model_construct bypass (no marker field set -> None) fails safe to the module
    settle policy — and on the shipped policy that path settles nothing. The bypass
    still fails safe to exactly the same branch it always did; the settling-branch
    half is proved by the companion test below."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    bypassed = GroundedAction.model_construct(
        action="move", point=Point(x=30, y=45), confidence=1.0
    )
    assert bypassed.plan_point_settle is None
    assert backend_module._MOVE_SETTLE_SECONDS == 0.0  # SPEC 5.1: unmarked -> no settle
    real_backend.execute(bypassed)
    assert engine.calls == [("move", 30, 45)]
    assert recorded_sleeps.sleeps == []


@WINDOWS_ONLY
def test_bypass_move_without_marker_lands_on_the_settling_branch_when_a_settle_is_set(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """The SAME bypass still lands on the SETTLING branch when a settle exists — i.e.
    the branch selection is unchanged and only the default value moved."""
    settle = explicit_settle(monkeypatch)
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    bypassed = GroundedAction.model_construct(
        action="move", point=Point(x=30, y=45), confidence=1.0
    )
    assert bypassed.plan_point_settle is None
    real_backend.execute(bypassed)
    assert engine.calls == [("move", 30, 45)]
    assert recorded_sleeps.sleeps == [settle]


@WINDOWS_ONLY
def test_fake_backend_treats_the_marker_as_harness_only(real_backend: LocalComputerBackend) -> None:
    """The harness fake simulates no timing (the O3 precedent): a marked move records
    exactly one executed action and performs zero settle sleeps of its own."""
    from computer_use_mcp.desktop.fake_backend import FakeComputerBackend

    fake = FakeComputerBackend()
    observation = fake.observe()
    action = move(
        (10, 10), plan_point_settle=True, source_observation_id=observation.observation_id
    )
    sleeps = _SleepRecorder()
    original_sleep = backend_module.time.sleep
    backend_module.time.sleep = sleeps
    try:
        message = fake.execute(action)
    finally:
        backend_module.time.sleep = original_sleep
    assert message == "Simulated move."
    assert len(fake.executed) == 1
    assert sleeps.sleeps == []


# --- the queue attachment: plan_policies mark point ends (follow_ups stay untouched) ---------


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Module-local copy of the shared ``fresh_server`` fixture (same body)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(srv_server_lifecycle, "_registry", srv_core_state.SessionRegistry(max_sessions=8))
    monkeypatch.setattr(srv_server_lifecycle, "_bundles", {})
    monkeypatch.setattr(srv_server_wait_machine, "_bundles", srv_server_lifecycle._bundles)
    monkeypatch.setattr(srv_server_wait, "_bundles", srv_server_lifecycle._bundles)
    yield


def _plan_settle_flags(backend: Any) -> list[bool | None]:
    """The settle marker each executed action actually carried to the backend."""
    return [getattr(action, "plan_point_settle", "MISSING") for action in backend.executed]


async def test_follow_ups_queue_without_plan_keeps_legacy_unset_markers(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """follow_ups WITHOUT a plan never touches the marker (None = the legacy settle
    policy, byte-identical): the attachment is plan-scoped only."""
    backend = ScriptedBackend(flip=True, width=1024, height=800)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "click",
            x=40,
            y=50,
            approved=True,
            follow_ups=[{"action": "click", "x": 60, "y": 70}],
            include_screenshot_after=False,
        )
    )
    assert payload["ok"] is True, payload
    assert _plan_settle_flags(backend) == [None, None]


def test_marker_is_internal_to_groundedaction_not_host_surface() -> None:
    """P3.2 item 9 (no surface): the marker exists on the INTERNAL GroundedAction only
    — ActionSpec deliberately does not mirror it, so no host payload can carry it, and
    ``to_grounded`` leaves it None (the legacy default)."""
    assert "plan_point_settle" in GroundedAction.model_fields
    assert "plan_point_settle" not in ActionSpec.model_fields
    spec = ActionSpec(action="move", x=1, y=2)
    assert spec.to_grounded().plan_point_settle is None
