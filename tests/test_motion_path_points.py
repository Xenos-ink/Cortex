"""MOTION PATH tests: the deterministic timed-motion engine (WP2, ORVEX-2026-101 Batch 2).

Covers P2.2 (``_motion_path_points`` — a PURE deterministic path builder installed
next to ``_drag_segment_points`` WITHOUT touching a line of the existing drag
behavior) and P2.3 (the ``move`` branch consuming the builder ONLY when a duration
exists — the no-duration path stays byte-identical). The formulas:

- linear: ``x(u') = x0 + (x1-x0)·u'``; simple bend: ``y(u') += 4h·u'(1-u')`` — peak
  ``h`` at the exact midpoint, sign convention D19: ``+h`` displaces toward ``+y``;
- quadratic Bezier: ``P(u') = (1-u')²P0 + 2(1-u')u'C + u'²P1`` with ``C = mid + 2h·n̂``;
- easing re-schedules TIME only (D20): ``linear`` = identity, ``smoothstep`` =
  ``3u²-2u³``; the geometric curve is unchanged, only the sampling rhythm moves;
- sampling: segments = clamp(ceil(duration_ms / interval), 2, 127) -> 3..128 points,
  first sample = start EXACTLY (0.0 px), last sample = end EXACTLY (0.0 px), zero
  randomness anywhere.

PORT NOTE (WP2, stable tree): the dev tree's plan-side nodes of this area
(``server._normalize_plan`` duration resolution, WaypointPlan construction) are
DEFERRED to WP7 with the plan surface; the in-plan transition DEFAULT policy value
(``PLAN_MOVE_DURATION_MS``) ships here and is pinned by
``test_shipped_env_defaults_match_the_rfc``.
"""

from __future__ import annotations

import math
import time
from typing import Any

import pytest
from recording_engine import RecordingEngine

import computer_use_mcp.backend as backend_module
from computer_use_mcp.backend import (
    FakeComputerBackend,
    InputBlockedError,
    LocalComputerBackend,
    PyAutoGuiInputEngine,
    SendInputEngine,
    _motion_path_points,
)
from computer_use_mcp.models import GroundedAction
from computer_use_mcp.state import StopToken, TaskStopped

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")


def move(point: tuple[int, int], **kwargs: Any) -> GroundedAction:
    kwargs.setdefault("confidence", 1.0)
    return GroundedAction(action="move", point={"x": point[0], "y": point[1]}, **kwargs)


class _TimedRecordingEngine(RecordingEngine):
    """RecordingEngine that also timestamps every dispatched move (test clock)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.stamps: list[float] = []
        self.fail_on_move_index: int | None = None

    def move(self, x: int, y: int) -> None:
        if self.fail_on_move_index is not None and len(self.calls) == self.fail_on_move_index:
            raise InputBlockedError("synthetic blocked injection mid-path")
        self.calls.append(("move", x, y))
        self.stamps.append(time.perf_counter())


class _StopAfterRecordingMoves(RecordingEngine):
    """Arm a stop token at a deterministic sampling boundary (no host clock)."""

    def __init__(self, stop: StopToken, stop_after: int) -> None:
        super().__init__()
        self.stop = stop
        self.stop_after = stop_after

    def move(self, x: int, y: int) -> None:
        super().move(x, y)
        if len(self.calls) >= self.stop_after:
            self.stop.stop()


# --- P2.2 pure builder: geometry ------------------------------------------------------------


def test_endpoints_exact_linear() -> None:
    """First sample = start exactly, last sample = end exactly (0.0 px both)."""
    points = _motion_path_points((10, 20), (300, -40), 240)
    assert points[0] == (10, 20)
    assert points[-1] == (300, -40)


def test_endpoints_exact_bend_and_bezier() -> None:
    for shape in ("linear", "quadratic_bezier"):
        points = _motion_path_points(
            (0, 0), (200, 0), 240, shape=shape, bend_px=40
        )
        assert points[0] == (0, 0), shape
        assert points[-1] == (200, 0), shape


def test_deterministic_same_inputs_same_list_100_iterations() -> None:
    """Absolute determinism: the same inputs produce a byte-identical list (no
    randomness anywhere in the motion design)."""
    first = _motion_path_points(
        (5, 7), (640, 400), 500, shape="quadratic_bezier", bend_px=-30, easing="smoothstep"
    )
    for _ in range(100):
        assert _motion_path_points(
            (5, 7), (640, 400), 500, shape="quadratic_bezier", bend_px=-30, easing="smoothstep"
        ) == first


def test_linear_midpoint_on_the_straight_line() -> None:
    """Linear geometry: the midpoint sample lands on the straight line (±1 px rounding)."""
    points = _motion_path_points((0, 0), (100, 50), 240)  # 240/12 = 20 segments (even)
    assert points[len(points) // 2] == (50, 25)


def test_simple_bend_peak_h_at_midpoint_with_d19_sign() -> None:
    """y += 4h·u(1-u): deviation from the straight line is exactly |h| at the exact
    midpoint, zero at both ends; +h toward +y (D19)."""
    for bend in (40, -40):
        points = _motion_path_points((0, 0), (200, 0), 240, bend_px=bend)
        deviations = [y for _x, y in points]  # the line is y=0
        assert deviations[0] == 0 and deviations[-1] == 0
        assert max(deviations, key=abs) == bend  # the peak IS h with its sign
        assert deviations[len(deviations) // 2] == bend  # peak sits at the midpoint


def test_bezier_passes_endpoints_peak_h_inside_hull() -> None:
    """Bezier: both endpoints exact; peak = mid + h·n̂ (horizontal move -> +y peak)."""
    points = _motion_path_points((0, 0), (200, 0), 240, shape="quadratic_bezier", bend_px=40)
    assert points[0] == (0, 0) and points[-1] == (200, 0)
    assert points[len(points) // 2] == (100, 40)  # C = mid + 2h·n̂ -> B(0.5) = mid + h·n̂
    ys = [y for _x, y in points]
    assert min(ys) >= 0 and max(ys) <= 40  # inside the control hull


def test_bezier_vertical_move_deterministic_horizontal_normal() -> None:
    """A perfectly vertical move has a horizontal unit normal (deterministic choice):
    the peak magnitude is still exactly |h|, displaced along x."""
    points = _motion_path_points((0, 0), (0, 200), 240, shape="quadratic_bezier", bend_px=40)
    assert points[len(points) // 2] == (-40, 100)


def test_sampling_floor_two_segments_small_duration() -> None:
    """Tiny duration -> the two-slice floor (3 points: start, mid, end)."""
    points = _motion_path_points((0, 0), (90, 30), 1)
    assert len(points) == 3
    assert points[0] == (0, 0) and points[-1] == (90, 30)


def test_sampling_cap_128_points_long_duration() -> None:
    """Very long duration -> the 128-point cap (127 segments)."""
    points = _motion_path_points((0, 0), (1000, 600), 1_000_000)
    assert len(points) == 128
    assert points[-1] == (1000, 600)


def test_start_equals_end_returns_empty_list() -> None:
    """Degenerate zero-length motion: no waypoints (the _drag_waypoints precedent)."""
    assert _motion_path_points((50, 60), (50, 60), 200) == []


def test_all_outputs_finite_integers() -> None:
    """Every returned coordinate is a finite int — no NaN/inf/fraction ever escapes."""
    points = _motion_path_points(
        (-100, -50), (900, 700), 333, shape="quadratic_bezier", bend_px=-17, easing="smoothstep"
    )
    for x, y in points:
        assert isinstance(x, int) and isinstance(y, int)
        assert math.isfinite(x) and math.isfinite(y)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"duration_ms": 0},
        {"duration_ms": -5},
        {"duration_ms": 1.5},
        {"duration_ms": float("nan")},
        {"duration_ms": float("inf")},
        {"duration_ms": "200"},
        {"duration_ms": True},
        {"target_interval_ms": 0},
        {"target_interval_ms": -12},
        {"shape": "spiral"},
        {"easing": "bounce"},
        {"bend_px": 1.5},
    ],
)
def test_rejects_nonpositive_or_nonsense_arguments(kwargs: dict) -> None:
    """Fail-closed argument validation: garbage never silently degrades the path."""
    payload: dict[str, Any] = {"duration_ms": 200}
    payload.update(kwargs)
    with pytest.raises(ValueError):
        _motion_path_points((0, 0), (100, 100), **payload)


@pytest.mark.parametrize("bad_start", [(1.5, 0), ("0", 0), (True, 0), (float("nan"), 0)])
def test_rejects_non_integer_coordinates(bad_start: tuple) -> None:
    with pytest.raises(ValueError):
        _motion_path_points(bad_start, (100, 100), 200)  # type: ignore[arg-type]


# --- P2.2 easing: timing-only (D20) ----------------------------------------------------------


def test_smoothstep_reshuffles_timing_not_geometry() -> None:
    """Both easings sample the SAME geometric curve; smoothstep is symmetric so the
    midpoint TIME sample stays at 50%, while the QUARTER-time sample moves to 15.625%
    (3u²-2u³ at u=0.25) — timing, not shape."""
    linear = _motion_path_points((0, 0), (400, 0), 240, easing="linear")
    smooth = _motion_path_points((0, 0), (400, 0), 240, easing="smoothstep")
    assert len(linear) == len(smooth)  # the sampling count is easing-independent
    assert linear[0] == smooth[0] and linear[-1] == smooth[-1]
    mid = len(linear) // 2
    assert linear[mid] == (200, 0)  # identity easing: halfway in time = halfway in space
    assert smooth[mid] == (200, 0)  # smoothstep is symmetric: 0.5 -> 0.5
    quarter = len(linear) // 4
    assert linear[quarter] == (100, 0)
    assert smooth[quarter] == (62, 0)  # 400 * 0.15625 = 62.5 -> 62 (banker's rounding)
    # every smoothstep sample still lies ON the same geometric line (y == 0)
    assert all(y == 0 for _x, y in smooth)


def test_smoothstep_monotone_progression() -> None:
    """smoothstep is monotonically increasing on [0, 1]: the projected progress never
    decreases and hits exactly 0 and 1 at the ends."""
    points = _motion_path_points((0, 0), (600, 0), 480, easing="smoothstep")
    progress = [x / 600 for x, _y in points]
    assert progress[0] == 0.0 and progress[-1] == 1.0
    assert progress == sorted(progress)  # monotonically non-decreasing


# --- P2.3 backend: the move branch consumes the builder only with a duration -----------------


@WINDOWS_ONLY
def test_timed_move_dispatches_path_and_ends_exact(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """duration_ms=120: exact samples/endpoints and exact pacing requests.

    Recording sleep makes this a sequencing test rather than a Windows scheduler
    measurement: the product requested duration is represented by eleven 12ms pacing
    sleeps and nothing else (SPEC 5.1: the shipped settle default is 0.0 ms, so the
    guarded trailing settle is short-circuited; the settle policy itself is pinned by
    tests/test_move_settle_policy.py with an explicit value).
    """
    sleeps: list[float] = []
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)  # passthrough transform
    monkeypatch.setattr(backend_module, "get_cursor_position", lambda: (10, 10))
    monkeypatch.setattr(real_backend, "probe_cursor_position", lambda: None)
    monkeypatch.setattr(backend_module.time, "sleep", sleeps.append)
    message = real_backend.execute(move((30, 45), duration_ms=120))
    assert message == "Executed move."
    points = [(call[1], call[2]) for call in engine.calls]
    assert points[0] == (10, 10)  # the actual cursor position starts the path
    assert points[-1] == (30, 45)  # 0.0 px arrival error
    assert len(points) == 11  # 120/12 = 10 segments -> 11 samples
    assert backend_module._MOVE_SETTLE_SECONDS == 0.0  # SPEC 5.1
    assert sleeps == [0.012] * 11


@WINDOWS_ONLY
@pytest.mark.parametrize("duration", [None, "omitted"])
def test_regression_move_without_duration_byte_identical_legacy(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch, duration: Any
) -> None:
    """THE batch's most important invariant: no duration -> the exact legacy single
    absolute move (and NO settle sleep — SPEC 5.1), byte-identical dispatch."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    monkeypatch.setattr(backend_module, "MOVE_DURATION_MS", 0)  # the shipped default
    kwargs = {} if duration == "omitted" else {"duration_ms": None}
    message = real_backend.execute(move((30, 45), **kwargs))
    assert message == "Executed move."
    assert engine.calls == [("move", 30, 45)]


@WINDOWS_ONLY
def test_move_explicit_zero_duration_is_an_explicit_jump(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """duration_ms=0 is an EXPLICIT jump (passes through to the legacy single event)."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    real_backend.execute(move((30, 45), duration_ms=0))
    assert engine.calls == [("move", 30, 45)]


@WINDOWS_ONLY
def test_move_env_duration_outside_plans_animates(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CORTEX_MOVE_DURATION_MS (default 0) is the outside-plans internal override:
    a direct move without the field animates when the operator sets it."""
    engine = _TimedRecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    monkeypatch.setattr(backend_module, "get_cursor_position", lambda: (10, 10))
    monkeypatch.setattr(real_backend, "probe_cursor_position", lambda: None)
    monkeypatch.setattr(backend_module, "MOVE_DURATION_MS", 120)
    real_backend.execute(move((30, 45)))
    assert len(engine.calls) == 11
    assert engine.calls[-1] == ("move", 30, 45)


@WINDOWS_ONLY
def test_cancel_mid_motion_responds_within_interval_and_holds_nothing(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation is injected at sample 4, so the next boundary stops deterministically."""
    stop = StopToken()
    engine = _StopAfterRecordingMoves(stop, stop_after=4)
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    monkeypatch.setattr(backend_module, "get_cursor_position", lambda: (10, 10))
    monkeypatch.setattr(real_backend, "probe_cursor_position", lambda: None)
    monkeypatch.setattr(backend_module.time, "sleep", lambda _seconds: None)
    with pytest.raises(TaskStopped):
        real_backend.execute(move((30, 45), duration_ms=120), stop=stop)
    assert len(engine.calls) == 4
    assert engine.calls[-1] != ("move", 30, 45)
    assert engine.down is False  # a move holds no button — nothing to release


@WINDOWS_ONLY
def test_input_blocked_mid_path_fails_clean_without_resend(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine rejection at a mid-path point propagates cleanly; no re-send, no
    state held, the arrival point simply not reached (T3: partial result is honest)."""
    engine = _TimedRecordingEngine()
    engine.fail_on_move_index = 2  # raise when the 3rd move is attempted
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    monkeypatch.setattr(backend_module, "get_cursor_position", lambda: (10, 10))
    monkeypatch.setattr(real_backend, "probe_cursor_position", lambda: None)
    with pytest.raises(InputBlockedError, match="synthetic blocked"):
        real_backend.execute(move((30, 45), duration_ms=120))
    assert len(engine.calls) == 2
    assert engine.calls[-1] != ("move", 30, 45)


@WINDOWS_ONLY
def test_parity_timed_move_same_points_on_both_engines(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timed loop consumes engine.move ONLY (P2.5): both engines walk the SAME
    point sequence with PAUSE=0.0 — no per-call inflation on the fallback engine."""
    from test_io_parity import (
        _ParityFakeUser32,
        _ParityPyautogui,
        _pyautogui_move_points,
        _sendinput_move_points,
    )

    monkeypatch.setattr(real_backend, "_active_context", None)
    monkeypatch.setattr(backend_module, "get_cursor_position", lambda: (10, 10))
    monkeypatch.setattr(real_backend, "probe_cursor_position", lambda: None)

    pa_module = _ParityPyautogui()
    monkeypatch.setattr(
        real_backend,
        "_engine",
        PyAutoGuiInputEngine(
            pa_module,
            pause=0.0,
            click_interval=0.0,
            type_interval=0.0,
            drag_interpolate=True,
            drag_step_pause=0.0,
        ),
    )
    real_backend.execute(move((30, 45), duration_ms=60))

    fake_user32 = _ParityFakeUser32()
    monkeypatch.setattr(backend_module, "_user32", fake_user32)
    monkeypatch.setattr(real_backend, "_engine", SendInputEngine())
    real_backend.execute(move((30, 45), duration_ms=60))

    assert _pyautogui_move_points(pa_module.calls) == _sendinput_move_points(fake_user32)
    assert len(_pyautogui_move_points(pa_module.calls)) == 6  # 60/12 = 5 segments -> 6 points
    assert _pyautogui_move_points(pa_module.calls)[-1] == (30, 45)


# --- env helpers (garbage -> default, the established block pattern) ------------------------


def test_env_helpers_garbage_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORTEX_TEST_DURATION", "abc")
    assert backend_module._env_nonnegative_int("CORTEX_TEST_DURATION", 7) == 7
    monkeypatch.setenv("CORTEX_TEST_DURATION", "-3")
    assert backend_module._env_nonnegative_int("CORTEX_TEST_DURATION", 7) == 7
    monkeypatch.setenv("CORTEX_TEST_DURATION", " 250 ")
    assert backend_module._env_nonnegative_int("CORTEX_TEST_DURATION", 7) == 250
    monkeypatch.setenv("CORTEX_TEST_DURATION", "")
    assert backend_module._env_nonnegative_int("CORTEX_TEST_DURATION", 7) == 7
    monkeypatch.setenv("CORTEX_TEST_SIGNED", "-40")
    assert backend_module._env_int("CORTEX_TEST_SIGNED", 0) == -40
    monkeypatch.setenv("CORTEX_TEST_SIGNED", "junk")
    assert backend_module._env_int("CORTEX_TEST_SIGNED", 0) == 0
    monkeypatch.setenv("CORTEX_TEST_CHOICE", "quadratic_bezier")
    assert backend_module._env_choice(
        "CORTEX_TEST_CHOICE", "linear", ("linear", "quadratic_bezier")
    ) == ("quadratic_bezier")
    monkeypatch.setenv("CORTEX_TEST_CHOICE", "spiral")
    assert backend_module._env_choice(
        "CORTEX_TEST_CHOICE", "linear", ("linear", "quadratic_bezier")
    ) == "linear"


def test_shipped_env_defaults_match_the_rfc() -> None:
    """The shipped motion defaults, read in a CLEAN-env subprocess so ambient
    CORTEX_* variables on the dev desktop cannot mask the shipped values:
    outside-plan duration 0, in-plan transition **0**, linear shape, bend 0, linear
    easing, 12ms sampling.

    SPEC 5.1 + 5.2: inside a plan the transition duration is REQUIRED FROM THE MODEL,
    so the internal constant is a fail-safe (0 = an instant jump) rather than a
    300 ms behaviour; ``move_duration`` was already 0 and stays 0.
    """
    import json
    import os
    import subprocess
    import sys

    source_root = str(backend_module.__file__).rsplit(os.sep + "computer_use_mcp", 1)[0]
    probe = (
        "import json; from computer_use_mcp import backend as b; "
        "print(json.dumps({"
        "'move_duration': b.MOVE_DURATION_MS, 'plan_move_duration': b.PLAN_MOVE_DURATION_MS, "
        "'shape': b.MOVE_SHAPE, 'bend': b.MOVE_BEND_PX, 'easing': b.MOVE_EASING, "
        "'interval': b.PLAN_SAMPLE_INTERVAL_MS}))"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("CORTEX_")}
    env["PYTHONPATH"] = source_root
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,  # the return code is asserted below (report stderr on failure)
    )
    assert completed.returncode == 0, completed.stderr
    shipped = json.loads(completed.stdout)
    assert shipped == {
        "move_duration": 0,
        "plan_move_duration": 0,  # SPEC 5.2: the model must state it (WP7 consumes)
        "shape": "linear",
        "bend": 0,
        "easing": "linear",
        "interval": 12,
    }


def test_fake_backend_records_plan_move_once_like_hold_precedent() -> None:
    """The harness fake records a duration-bearing move as ONE move (the O3
    precedent from Batch 1: the fake simulates no motion physics — harness only)."""
    backend = FakeComputerBackend()
    observation = backend.observe()
    action = move((10, 10), duration_ms=200, source_observation_id=observation.observation_id)
    message = backend.execute(action)
    assert message == "Simulated move."
    assert len(backend.executed) == 1
    assert backend._cursor_physical == (10, 10)
