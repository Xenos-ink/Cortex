"""WAYPOINT PLAN model tests (WP7a port of the dev test_waypoint_plan.py).

The plan schema now lives in ``computer_use_mcp.plan`` (the data layer is
server-import-free); the pins are the dev tree's, adapted only in import path and
in the one node that used to reach into ``server`` for the wire boundary (it now
uses ``plan.coerce_tolerant_plan`` directly — same validator, same message).

The host declares LOCATIONS AND ACTIONS ONLY (the governing user clarification):
``Waypoint`` = one ordered stop (x, y + actions at the point), ``WaypointPlan`` =
the ordered point container. Everything is fail-closed at construction (V1-V7 of
design/MOTION_ACTION_RFC.md §1.4): a rejected plan never reaches the queue, so a
bad plan can never dispatch a single input event. ``duration_ms`` (P2.1) is the
single optional transition-duration override and is valid on ``move`` ONLY.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from pydantic import ValidationError

from computer_use_mcp.core.models import ActionSpec, GroundedAction
from computer_use_mcp.planning.plan import (
    MAX_PLAN_ACTIONS_PER_POINT,
    MAX_PLAN_POINTS,
    Waypoint,
    WaypointPlan,
    coerce_tolerant_plan,
)

USER_PLAN = {
    "points": [
        {"id": "open-menu", "x": 420, "y": 180, "actions": [{"action": "click"}]},
        {"id": "choose-item", "x": 460, "y": 260, "actions": [{"action": "click"}]},
        {"id": "save", "x": 850, "y": 720, "actions": [{"action": "click"}]},
    ]
}


def waypoint(**kwargs: Any) -> Waypoint:
    kwargs.setdefault("x", 10)
    kwargs.setdefault("y", 20)
    return Waypoint(**kwargs)


# --- acceptance: the literal user plan and its legal shapes -------------------------------------


def test_user_reference_three_point_plan_accepted_verbatim() -> None:
    """The user's exact example: (420,180) open the menu -> (460,260) choose ->
    (850,720) press save."""
    plan = WaypointPlan.model_validate(USER_PLAN)
    assert [p.id for p in plan.points] == ["open-menu", "choose-item", "save"]
    assert [(p.x, p.y) for p in plan.points] == [(420, 180), (460, 260), (850, 720)]
    assert all(len(p.actions) == 1 for p in plan.points)
    assert plan.points[0].actions[0].action == "click"


def test_waypoint_without_actions_is_a_legal_pass_through_point() -> None:
    point = waypoint()
    assert point.actions == []  # a crossing waypoint: move, verify the cursor, move on


def test_auto_generated_ids_are_unique_per_point() -> None:
    plan = WaypointPlan.model_validate({"points": [{"x": 1, "y": 2}, {"x": 3, "y": 4}]})
    assert plan.points[0].id != plan.points[1].id
    assert len({p.id for p in plan.points}) == 2


def test_plan_policy_defaults_are_stop_on_failure_and_continue_on_uncertain() -> None:
    """F2-0 (user decision): the pair is now ASYMMETRIC and that asymmetry is the
    point. ``on_failure`` keeps the D7/D8 ``"stop"`` default — a resolved failure is
    real evidence that the sequence is broken. ``on_uncertain`` flipped to
    ``"continue"``: since fix 1.1 a stated ``expected_effect`` is no longer
    satisfiable by "some pixels changed", so every pixel-only point abstains and a
    ``"stop"`` default halted a well-formed plan at its first point. Neither default
    changes what a point is REPORTED as — the honesty contract is where this
    asymmetry is load-bearing.

    RESTATED by ORVEX-2026-102 §5.2 (SPEC 5.2, conflict C5): BOTH defaults are KEPT —
    ``"continue"`` is a measured necessity and flipping it was an explicitly rejected
    alternative — but they are no longer *relyable*.  A plan submitted on the WIRE must
    now STATE both fields and is rejected, naming the missing one, if it does not.  The
    model-level assertion is therefore UNCHANGED; the added half is the honest
    restatement of where the default still applies and where it no longer does.
    """
    plan = WaypointPlan.model_validate(USER_PLAN)
    assert plan.on_failure == "stop" and plan.on_uncertain == "continue"
    point = Waypoint.model_validate({"x": 1, "y": 2})
    assert point.on_failure == "stop" and point.on_uncertain == "continue"
    # SPEC 5.2: the wire no longer inherits the default — the plan boundary rejects a
    # submission that leans on it.  The exhaustive 8-field matrix lives in
    # tests/test_wp7a_plan_boundary.py; this is the C5-specific case.
    with pytest.raises(ValueError, match="on_uncertain"):
        coerce_tolerant_plan(
            {
                "transition_duration_ms": 0,
                "point_timeout_ms": 5_000,
                "on_failure": "stop",
                "points": [
                    {
                        "id": "a",
                        "x": 1,
                        "y": 2,
                        "actions": [{"action": "click"}],
                        "arrival_condition": "clicked",
                    }
                ],
            }
        )


# --- V1: non-integer coordinates/durations (incl. NaN/Infinity) ---------------------------------


@pytest.mark.parametrize("field", ["x", "y", "duration_ms", "timeout_ms"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), 1.5, "abc", [1]])
def test_v1_non_integer_fields_rejected(field: str, bad: Any) -> None:
    """JSON NaN/Infinity fail the int type; fractions/strings/garbage too."""
    payload: dict[str, Any] = {"x": 1, "y": 2}
    payload[field] = bad
    with pytest.raises(ValidationError):
        Waypoint(**payload)


def test_v1_point_level_numeric_fields_rejected_on_plan_too() -> None:
    with pytest.raises(ValidationError):
        WaypointPlan.model_validate(
            {"points": [{"x": 1, "y": 2, "timeout_ms": float("nan")}]}
        )


# --- V2: coordinate bounds are the Point bounds (-8192..16384) ----------------------------------


@pytest.mark.parametrize("bad_x", [-8_193, 16_385])
def test_v2_out_of_bounds_coordinates_rejected(bad_x: int) -> None:
    with pytest.raises(ValidationError):
        Waypoint(x=bad_x, y=0)


@pytest.mark.parametrize("edge_x", [-8_192, 16_384])
def test_v2_boundaries_accepted(edge_x: int) -> None:
    assert Waypoint(x=edge_x, y=edge_x).x == edge_x


# --- V3: missing/empty structure -----------------------------------------------------------------


def test_v3_empty_plan_rejected() -> None:
    with pytest.raises(ValidationError):
        WaypointPlan(points=[])


def test_v3_plan_without_points_rejected() -> None:
    with pytest.raises(ValidationError):
        WaypointPlan()


def test_v3_point_missing_coordinate_rejected() -> None:
    with pytest.raises(ValidationError):
        Waypoint(x=1)  # no y
    with pytest.raises(ValidationError):
        Waypoint(y=1)  # no x


# --- V4/V6: actions inside points inherit the point's location ----------------------------------


def test_v6_action_with_explicit_x_y_inside_point_rejected() -> None:
    """The waypoint IS the location — a contradicting action coordinate is malformed."""
    with pytest.raises(ValidationError, match="x/y"):
        waypoint(actions=[{"action": "click", "x": 5, "y": 6}])


def test_v6_drag_inside_point_requires_explicit_end() -> None:
    with pytest.raises(ValidationError, match="x2/y2"):
        waypoint(actions=[{"action": "drag"}])
    ok = waypoint(actions=[{"action": "drag", "x2": 40, "y2": 60}])
    assert ok.actions[0].x2 == 40


def test_v6_x2_y2_on_non_drag_action_rejected() -> None:
    with pytest.raises(ValidationError, match="x2/y2"):
        waypoint(actions=[{"action": "click", "x2": 40, "y2": 60}])


def test_v4_unsupported_action_name_rejected() -> None:
    with pytest.raises(ValidationError):
        waypoint(actions=[{"action": "teleport"}])


def test_v4_grounded_invalid_action_shape_fails_at_conversion() -> None:
    """Field-membership failures (via on a non-type action) fail at the GroundedAction
    conversion — the plan can never reach the queue (R-18 pattern)."""
    spec = waypoint(actions=[{"action": "click", "via": "clipboard"}]).actions[0]
    with pytest.raises(ValidationError):
        spec.to_grounded()


def test_v6_duplicate_point_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="unique"):
        WaypointPlan.model_validate(
            {"points": [{"id": "a", "x": 1, "y": 2}, {"id": "a", "x": 3, "y": 4}]}
        )


def test_v6_malformed_point_id_rejected() -> None:
    with pytest.raises(ValidationError):
        waypoint(id="bad id!")


# --- V5: duration bounds ------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["duration_ms", "timeout_ms"])
def test_v5_negative_and_over_cap_durations_rejected(field: str) -> None:
    cap = 5_000 if field == "duration_ms" else 60_000
    with pytest.raises(ValidationError):
        waypoint(**{field: -1})
    with pytest.raises(ValidationError):
        waypoint(**{field: cap + 1})
    assert getattr(waypoint(**{field: 0}), field) == 0
    assert getattr(waypoint(**{field: cap}), field) == cap


def test_v5_hold_ms_rules_apply_unchanged_inside_points() -> None:
    """Batch 1's hold_ms doctrine rides inside plans untouched: valid on
    click/right_click, rejected on double_click — at conversion."""
    ok = waypoint(actions=[{"action": "click", "hold_ms": 250}])
    assert ok.actions[0].to_grounded().hold_ms == 250
    with pytest.raises(ValidationError):
        waypoint(actions=[{"action": "double_click", "hold_ms": 250}]).actions[0].to_grounded()


def test_v5_duration_ms_inside_point_actions_rejected_at_conversion() -> None:
    """duration_ms is a TRANSITION property (of the move TO the point) — a duration on
    an action inside the point fails the move-only validator at conversion."""
    spec = waypoint(actions=[{"action": "click", "duration_ms": 200}]).actions[0]
    with pytest.raises(ValidationError, match="duration_ms is only valid on move"):
        spec.to_grounded()


# --- V7: plan length caps -----------------------------------------------------------------------


def test_v7_over_plan_points_cap_rejected() -> None:
    points = [{"x": i, "y": i} for i in range(MAX_PLAN_POINTS + 1)]
    with pytest.raises(ValidationError):
        WaypointPlan(points=points)


def test_v7_at_plan_points_cap_accepted() -> None:
    points = [{"x": i, "y": i} for i in range(MAX_PLAN_POINTS)]
    assert len(WaypointPlan(points=points).points) == MAX_PLAN_POINTS


def test_v7_over_actions_per_point_cap_rejected() -> None:
    actions = [{"action": "click"}] * (MAX_PLAN_ACTIONS_PER_POINT + 1)
    with pytest.raises(ValidationError):
        waypoint(actions=actions)


def test_v7_extra_fields_fail_closed() -> None:
    """A typo'd plan field ('duraton_ms') is REJECTED, never silently ignored."""
    with pytest.raises(ValidationError):
        waypoint(duraton_ms=200)
    with pytest.raises(ValidationError):
        WaypointPlan.model_validate({**USER_PLAN, "transition_duration": 200})


# --- duration_ms on GroundedAction/ActionSpec: move-only ----------------------------------------


def test_duration_ms_accepted_on_move_only() -> None:
    ok = GroundedAction(action="move", point={"x": 1, "y": 2}, duration_ms=200)
    assert ok.duration_ms == 200
    with pytest.raises(ValidationError, match="duration_ms is only valid on move"):
        GroundedAction(action="click", point={"x": 1, "y": 2}, duration_ms=200)
    with pytest.raises(ValidationError, match="duration_ms is only valid on move"):
        GroundedAction(action="drag", point={"x": 1, "y": 2}, to_point={"x": 3, "y": 4}, duration_ms=200)
    with pytest.raises(ValidationError, match="duration_ms is only valid on move"):
        GroundedAction(action="type", text="hi", duration_ms=200)


def test_duration_ms_bounds_fail_closed_on_move() -> None:
    with pytest.raises(ValidationError):
        GroundedAction(action="move", point={"x": 1, "y": 2}, duration_ms=-1)
    with pytest.raises(ValidationError):
        GroundedAction(action="move", point={"x": 1, "y": 2}, duration_ms=5_001)


def test_action_spec_mirrors_duration_ms_and_to_grounded_passes_it_through() -> None:
    spec = ActionSpec(action="move", x=12, y=34, duration_ms=200)
    grounded = spec.to_grounded()
    assert grounded.duration_ms == 200
    assert grounded.point is not None and (grounded.point.x, grounded.point.y) == (12, 34)
    assert ActionSpec(action="move", x=12, y=34).to_grounded().duration_ms is None
    with pytest.raises(ValidationError):
        ActionSpec(action="move", x=1, y=2, duration_ms=5_001).to_grounded()


# --- round-trip serialization -------------------------------------------------------------------


def test_plan_round_trip_serialization_preserves_everything() -> None:
    plan = WaypointPlan.model_validate(
        {
            **USER_PLAN,
            "on_failure": "continue",
            "transition_duration_ms": 300,
            "final_wait_ms": 1_500,
            "points": [
                {**p, "arrival_condition": "menu is open", "duration_ms": 250}
                for p in USER_PLAN["points"]
            ],
        }
    )
    dumped = plan.model_dump()
    restored = WaypointPlan.model_validate(dumped)
    assert restored == plan
    from_json = WaypointPlan.model_validate_json(plan.model_dump_json())
    assert from_json == plan
    assert restored.points[0].duration_ms == 250
    assert restored.transition_duration_ms == 300
    assert restored.final_wait_ms == 1_500
    assert restored.points[0].arrival_condition == "menu is open"


def test_nan_never_survives_any_plan_field() -> None:
    """Belt-and-suspenders: no NaN sneaks through ANY numeric plan field."""
    for payload in (
        {"points": [{"x": math.nan, "y": 2}]},
        {"points": [{"x": 1, "y": 2, "duration_ms": math.nan}]},
        {"points": [{"x": 1, "y": 2}], "transition_duration_ms": math.nan},
        {"points": [{"x": 1, "y": 2}], "final_wait_ms": math.nan},
    ):
        with pytest.raises(ValidationError):
            WaypointPlan.model_validate(payload)
