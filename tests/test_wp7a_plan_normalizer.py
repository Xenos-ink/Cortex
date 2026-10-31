"""WP7a plan NORMALIZER tests: durations, settle stamping, the move-duration fail-safe.

Ported from the dev tree's test_w5_motion_policy.py PART 2/3 (the pure
normalizer nodes WP2 deferred with the plan surface) plus the WP7a settle-stamping
producer — the real E-01 producer behind the WP2 seam the WP2 consumer in
``backend.py`` has been waiting for.

SPEC 5.1 (outside a plan, INSTANT) and the flat-surface pins live in
tests/test_w5_motion_policy.py / tests/test_move_settle_policy.py and are NOT
restated here. The wire-level zero-effect proofs (an incomplete plan dispatches
nothing) need the server surface and are DEFERRED to WP7b.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from pydantic import ValidationError

import computer_use_mcp.backend as backend_module
import computer_use_mcp.planning.plan as plan_module
from computer_use_mcp.core.models import ActionSpec, GroundedAction
from computer_use_mcp.planning.plan import (
    WaypointPlan,
    normalize_plan,
    plan_settle_markers,
    stamped_plan_actions,
)

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")


def _plan(points: list[dict[str, Any]], **plan_kwargs: Any) -> WaypointPlan:
    return WaypointPlan(points=points, **plan_kwargs)


def test_a_whitespace_only_arrival_condition_is_never_copied_into_expected_effect() -> None:
    """VAL-WP7a-02 #14 (executed in WP7b): a blank ``arrival_condition`` is an ABSENCE
    of a statement — direct construction is untouched (the wire gate rejects it), but
    the normalizer must not inject blank text as an action's ``expected_effect``."""
    for blank in ("", "   "):
        specs, _ctx = normalize_plan(
            _plan(
                [
                    {
                        "id": "a",
                        "x": 1,
                        "y": 2,
                        "arrival_condition": blank,
                        "actions": [{"action": "click"}],
                    }
                ]
            )
        )
        click = next(spec for spec in specs if spec.action.value == "click")
        assert click.expected_effect is None


# --- VAL-WP7a-01 #6: the defense runs at the TOP of normalize_plan -----------------------------


def _smuggled_plan() -> Any:
    """A plan built with ``model_construct`` — pydantic validation skipped entirely,
    so only the model-defense recheck stands between the garbage and ActionSpec's
    lax coercion."""
    from computer_use_mcp.planning.plan import Waypoint

    return WaypointPlan.model_construct(
        points=[
            Waypoint.model_construct(
                x=1,
                y=2,
                actions=[],
                duration_ms="250",  # type: ignore[arg-type]
            )
        ],
        transition_duration_ms=None,
    )


def test_normalize_plan_rejects_a_smuggled_point_level_non_int() -> None:
    """A model_construct'd plan whose POINT duration_ms is a string is rejected at
    normalization — before ActionSpec's lax coercion can silently accept it."""
    with pytest.raises(ValueError, match="duration_ms"):
        normalize_plan(_smuggled_plan())


def test_normalize_plan_rejects_a_smuggled_action_level_non_int() -> None:
    from computer_use_mcp.core.models import ActionSpec
    from computer_use_mcp.planning.plan import Waypoint

    plan = WaypointPlan.model_construct(
        points=[
            Waypoint.model_construct(
                x=1,
                y=2,
                actions=[
                    ActionSpec.model_construct(
                        action="click", x=None, y=None, hold_ms="2.5",
                        x2=None, y2=None, text=None, keys=[], delta=0,
                        target=None, expected_effect=None, via=None, duration_ms=None,
                    )
                ],
                duration_ms=None,
            )
        ],
        transition_duration_ms=None,
    )
    with pytest.raises(ValueError, match="hold_ms"):
        normalize_plan(plan)


# --- PART 2 — the in-plan transition the MODEL states ----------------------------------------


def test_a_stated_in_plan_duration_is_carried_onto_the_transition_move() -> None:
    """A model that STATES a duration gets it on the transition move verbatim; the
    internal fail-safe is not consulted."""
    specs, _ctx = normalize_plan(
        _plan([{"id": "a", "x": 10, "y": 20, "actions": [], "duration_ms": 240}])
    )
    moves = [s for s in specs if s.action.value == "move"]
    assert [m.duration_ms for m in moves] == [240]


def test_an_unstated_in_plan_duration_falls_back_to_the_zero_fail_safe() -> None:
    """The fail-safe: with the internal default at 0, an entirely unstated plan is a
    SINGLE instant ``move`` (no per-sample pacing), never a hidden animation. This is
    the direct-construction path, which SPEC 5.2 deliberately leaves alone."""
    specs, _ctx = normalize_plan(_plan([{"id": "a", "x": 10, "y": 20, "actions": []}]))
    moves = [s for s in specs if s.action.value == "move"]
    assert [m.duration_ms for m in moves] == [backend_module.PLAN_MOVE_DURATION_MS]


def test_duration_resolution_order_is_point_then_plan_then_fail_safe() -> None:
    """The ``??`` order is unchanged: a point's own value wins, the plan's is next,
    and only an entirely unstated plan reaches the internal fail-safe."""
    specs, _ctx = normalize_plan(
        _plan(
            [
                {"id": "a", "x": 1, "y": 2, "actions": [], "duration_ms": 250},
                {"id": "b", "x": 3, "y": 4, "actions": []},
            ],
            transition_duration_ms=120,
        )
    )
    assert [s.duration_ms for s in specs if s.action.value == "move"] == [250, 120]


def test_the_normalizer_is_the_only_consumer_of_the_in_plan_default() -> None:
    """The in-plan default is read ONCE, in the normalizer (WP7-SEAM(plan-normalizer)
    replaced by the real consumer). A flat ``move`` does not go through it, so
    changing the constant cannot slow a single flat action."""
    source = inspect.getsource(plan_module.normalize_plan)
    assert "PLAN_MOVE_DURATION_MS" in source
    flat_source = inspect.getsource(backend_module.LocalComputerBackend._execute_dispatched)
    assert "PLAN_MOVE_DURATION_MS" not in flat_source


def test_an_operator_set_fail_safe_value_is_what_an_unstated_plan_gets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CORTEX_PLAN_MOVE_DURATION_MS is consumed HERE (band 0..500, resolved at import
    in backend.py): the normalizer reads the module constant, so an operator's
    in-band value becomes the fail-safe duration for an unstated plan."""
    monkeypatch.setattr(plan_module, "PLAN_MOVE_DURATION_MS", 300)
    specs, _ctx = normalize_plan(_plan([{"id": "a", "x": 5, "y": 6, "actions": []}]))
    moves = [s for s in specs if s.action.value == "move"]
    assert [m.duration_ms for m in moves] == [300]


# --- PART 3 — the declared settle inside a plan -----------------------------------------------


def test_a_declared_settle_ms_is_the_one_that_is_used() -> None:
    """The settle is no longer inherited: the point says what it wants, the normalizer
    carries it as the internal per-point value, and the stamping producer puts it on
    the point's LAST item only."""
    specs, ctx = normalize_plan(
        _plan([{"id": "a", "x": 1, "y": 2, "actions": [{"action": "click"}], "settle_ms": 42}])
    )
    assert ctx["policies"][-1]["settle_ms"] == 42  # rides the policy entry
    # the marker is applied by the stamping producer, not by ActionSpec: a bare spec
    # carries nothing (P3.2 item 9 — no host surface)
    assert specs[-1].to_grounded().plan_point_settle_ms is None


def test_a_zero_declared_settle_means_no_settle() -> None:
    """``settle_ms: 0`` is a MEANINGFUL statement, not an absent one — the whole point
    of making the settle explicit is that "no settle" is now sayable."""
    _specs, ctx = normalize_plan(
        _plan([{"id": "a", "x": 1, "y": 2, "actions": [], "settle_ms": 0}])
    )
    assert ctx["policies"][-1]["settle_ms"] == 0


def test_a_point_settle_overrides_the_plan_settle() -> None:
    _specs, ctx = normalize_plan(
        _plan(
            [
                {"id": "a", "x": 1, "y": 2, "actions": [], "settle_ms": 77},
                {"id": "b", "x": 3, "y": 4, "actions": []},
            ],
            settle_ms=10,
        )
    )
    assert [p["settle_ms"] for p in ctx["policies"] if p is not None] == [77, 10]


def test_an_unstated_settle_rides_as_none_and_falls_back_to_the_module_policy() -> None:
    """``None`` = "not stated": the WP2 dispatch path falls back to the module policy
    (SPEC 5.1 default 0 ms) — pinned so the None path can never silently become 0."""
    _specs, ctx = normalize_plan(_plan([{"id": "a", "x": 1, "y": 2, "actions": []}]))
    assert ctx["policies"][-1]["settle_ms"] is None


@pytest.mark.parametrize("bad", [-1, 201, 5_000])
def test_settle_ms_band_is_0_to_200(bad: Any) -> None:
    """Fail-closed at the model: a settle outside 0..MAX_PLAN_SETTLE_MS is a malformed
    plan, never a coerced one. (PORT ADAPTATION, recorded in wp7a-notes.md: the dev
    node passed non-int forms ("40", 40.0, True) here too, but its assertion held only
    through an incidental structural error — ``Waypoint(points=...)`` is not a legal
    Waypoint kwarg at all. The model-level band is pins [-1, 201, 5000]; the non-int
    forms are rejected at the WIRE strict-int gate, pinned by
    tests/test_wp7a_plan_boundary.py::test_the_raw_wire_boundary_rejects_every_non_integer
    and ::test_wire_boundary_gates_the_full_coercion_path.)"""
    with pytest.raises(ValidationError):
        WaypointPlan(points=[{"id": "a", "x": 1, "y": 2, "actions": [], "settle_ms": bad}])


def test_settle_ms_non_int_forms_are_wire_only_rejections() -> None:
    """Pydantic lax mode coerces "40"/40.0/True at direct construction (a payload the
    WIRE never sees — the raw boundary rejects them first); pinned so the division of
    labor above stays honest."""
    for tolerable in ("40", 40.0, True):
        assert WaypointPlan(
            points=[{"id": "a", "x": 1, "y": 2, "actions": [], "settle_ms": tolerable}]
        ).points[0].settle_ms == int(tolerable)


# --- the settle-stamping producer (the WP2 seam's real producer) -------------------------------


def _normalized(plan: WaypointPlan) -> tuple[list[ActionSpec], dict[str, Any]]:
    return normalize_plan(plan)


def test_the_producer_marks_transitions_false_and_point_ends_true() -> None:
    """``[move, click]`` -> markers ``[(False, None), (True, declared)]``: every
    transition move suppressed, the point's LAST item marked for the single per-point
    settle (D15), carrying the DECLARED settle in ms (E-01)."""
    specs, ctx = _normalized(
        _plan([{"id": "a", "x": 1, "y": 2, "actions": [{"action": "click"}], "settle_ms": 200}])
    )
    assert plan_settle_markers(specs, ctx) == [(False, None), (True, 200)]


def test_three_point_plan_settles_exactly_once_per_point() -> None:
    specs, ctx = _normalized(
        _plan(
            [
                {"id": "a", "x": 1, "y": 2, "actions": [{"action": "click"}], "settle_ms": 5},
                {"id": "b", "x": 3, "y": 4, "actions": [{"action": "click"}], "settle_ms": 6},
                {"id": "c", "x": 5, "y": 6, "actions": [{"action": "click"}], "settle_ms": 7},
            ]
        )
    )
    markers = plan_settle_markers(specs, ctx)
    assert [flag for flag, _ms in markers] == [False, True, False, True, False, True]
    assert [ms for _flag, ms in markers if ms is not None] == [5, 6, 7]


def test_actionless_point_marks_its_move_as_the_point_end() -> None:
    """A point with no actions settles after its move (the move IS the last element):
    the single element carries True, not False."""
    specs, ctx = _normalized(
        _plan(
            [
                {"id": "hop", "x": 100, "y": 120, "actions": []},
                {"id": "act", "x": 200, "y": 220, "actions": []},
            ]
        )
    )
    assert [flag for flag, _ms in plan_settle_markers(specs, ctx)] == [True, True]


def test_the_final_wait_element_carries_no_settle_marker() -> None:
    """The trailing ``final_wait_ms`` element is NOT part of any point: it carries no
    settle (the wait itself is the pacing)."""
    specs, ctx = _normalized(
        _plan(
            [{"id": "a", "x": 1, "y": 2, "actions": [{"action": "click"}], "settle_ms": 9}],
            final_wait_ms=1_000,
        )
    )
    assert plan_settle_markers(specs, ctx) == [(False, None), (True, 9), (False, None)]


def test_a_misaligned_context_fails_closed() -> None:
    """A torn context (policies not aligned with specs) is an error, never a silent
    mis-stamp: the producer refuses before the backend can settle in the wrong place."""
    specs, ctx = _normalized(_plan([{"id": "a", "x": 1, "y": 2, "actions": []}]))
    torn = dict(ctx)
    torn["policies"] = ctx["policies"][:-1]
    with pytest.raises(ValueError, match="misaligned"):
        plan_settle_markers(specs, torn)


def test_stamped_plan_actions_carry_the_internal_markers_on_groundedactions_only() -> None:
    """P3.2 item 9 (no surface): the markers exist on the INTERNAL GroundedAction only
    — ActionSpec deliberately does not mirror them, ``to_grounded`` leaves them None,
    and only the producer stamps them."""
    assert "plan_point_settle" in GroundedAction.model_fields
    assert "plan_point_settle" not in ActionSpec.model_fields
    specs, ctx = _normalized(
        _plan([{"id": "a", "x": 30, "y": 45, "actions": [{"action": "click"}], "settle_ms": 25}])
    )
    grounded = stamped_plan_actions(specs, ctx)
    assert [(a.plan_point_settle, a.plan_point_settle_ms) for a in grounded] == [
        (False, None),
        (True, 25),
    ]
    assert grounded[0].reason.startswith("MCP plan action")


@WINDOWS_ONLY
def test_the_producer_output_is_the_sleep_the_dispatch_path_issues(
    real_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E-01 end to end: feed the producer's output to the REAL backend through the
    WP2 consumer — a point that declares 25 ms settles 25 ms, a declared 0 settles
    not at all, and an unmarked action never enters the settle tail. The in-plan
    duration fail-safe is pinned to 0 (VAL-WP7a-01 #13) so the operator environment
    cannot inject timed-motion pacing sleeps into this node's sleep ledger."""
    from recording_engine import RecordingEngine

    monkeypatch.setattr(plan_module, "PLAN_MOVE_DURATION_MS", 0)
    sleeps: list[float] = []

    def _record(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(backend_module.time, "sleep", _record)
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    specs, ctx = _normalized(
        _plan([{"id": "a", "x": 10, "y": 10, "actions": [{"action": "click"}], "settle_ms": 25}])
    )
    for grounded in stamped_plan_actions(specs, ctx):
        real_backend.execute(grounded)
    assert sleeps == [0.025]
    specs0, ctx0 = _normalized(
        _plan([{"id": "b", "x": 11, "y": 11, "actions": [{"action": "click"}], "settle_ms": 0}])
    )
    for grounded in stamped_plan_actions(specs0, ctx0):
        real_backend.execute(grounded)
    assert sleeps == [0.025]  # nothing added by the zero declaration


# --- the final-wait element -------------------------------------------------------------------


def test_final_wait_ms_appends_one_ceil_to_seconds_wait_item() -> None:
    """``final_wait_ms`` appends ONE trailing ``wait`` item, ceil'd to whole seconds
    (at least 1 s: the tail waits AT LEAST the requested time), and the context
    records both the requested ms and the item index."""
    specs, ctx = _normalized(
        _plan([{"id": "a", "x": 1, "y": 2, "actions": []}], final_wait_ms=1_500)
    )
    assert specs[-1].action.value == "wait"
    assert specs[-1].delta == 2
    assert ctx["final_wait_ms"] == 1_500
    assert ctx["final_wait_item"] == len(specs) - 1


def test_final_wait_ms_zero_or_absent_appends_nothing() -> None:
    for plan_kwargs in ({}, {"final_wait_ms": 0}):
        specs, ctx = _normalized(_plan([{"id": "a", "x": 1, "y": 2, "actions": []}], **plan_kwargs))
        assert all(spec.action.value != "wait" for spec in specs)
        assert ctx["final_wait_item"] is None


def test_final_wait_above_the_model_cap_is_rejected_before_normalization() -> None:
    """``final_wait_ms`` is ``le=MAX_DURATION_MS`` (5000): a request above it is
    rejected BY THE MODEL, so the tail's largest possible delta is 5 — the flat 10 s
    wait cap is only ever observable on a flat action."""
    with pytest.raises(Exception):
        WaypointPlan(
            points=[{"id": "a", "x": 1, "y": 2, "actions": []}], final_wait_ms=5_001
        )
    specs, _ctx = _normalized(
        _plan([{"id": "a", "x": 1, "y": 2, "actions": []}], final_wait_ms=5_000)
    )
    assert specs[-1].delta == 5


# --- context shape ----------------------------------------------------------------------------


def test_the_plan_context_is_complete_and_queue_aligned() -> None:
    specs, ctx = _normalized(
        _plan(
            [
                {"id": "a", "x": 1, "y": 2, "actions": [{"action": "click"}]},
                {"id": "b", "x": 3, "y": 4, "actions": []},
            ],
            transition_duration_ms=100,
            point_timeout_ms=4_000,
            final_wait_ms=1_000,
        )
    )
    assert ctx["point_count"] == 2
    assert ctx["normalized_item_count"] == len(specs) == 4  # move+click, move, wait
    assert [p["id"] for p in ctx["points"]] == ["a", "b"]
    assert ctx["points"][0]["first_item"] == 0 and ctx["points"][0]["last_item"] == 1
    assert ctx["points"][1]["first_item"] == 2 and ctx["points"][1]["last_item"] == 2
    assert ctx["points"][0]["timeout_ms"] == 4_000
    assert ctx["points"][0]["action_types"] == ["move", "click"]
    assert len(ctx["policies"]) == len(specs)
    assert ctx["plan_id"]
