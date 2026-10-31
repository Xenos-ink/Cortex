"""WP7a plan WIRE BOUNDARY tests: strict ints, explicitness, coercion, S-05, S-10.

Four claims, one file:

**Strict ints at the wire boundary.**  Pydantic v2 coerces laxly, so the raw
``plan`` payload passes through ``plan.validate_raw_plan_boundary`` BEFORE model
validation: every plan numeric must be a REAL JSON/Python integer (bool, float,
string, NaN, Infinity all rejected, naming the field), unknown action keys are
rejected outright, and a second model-level defense pass rechecks anything a
``model_construct`` bypass could smuggle through.

**The SPEC 5.2 explicitness matrix** (ORVEX-2026-102 §5.2, ported from the dev
test_w5_motion_policy.py PART 4): one rejection per required detail, each naming
the field, all omissions reported in ONE aggregated teach-in message, and the
rule living at the WIRE only (direct construction keeps the model defaults).

**S-05: the teaching text passes its own validator.**  Every fenced JSON plan
example in ``computer_use_mcp/plan.py``'s docstrings is extracted by the test and
run through the same boundary a submitted plan takes — the docstring can never
drift out of validity.

**S-10: the advertised schema is a typed object.**  ``PLAN_PARAM_PLAIN_SCHEMA``
carries no ``anyOf``, no ``$ref``/``$defs``, and no ``"string"`` arm (the
object|string union is deleted); the JSON-string runtime tolerance stays BEHIND
the boundary; and every bound in the hand-written schema is pinned against the
pydantic models so the two can drift only loudly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import computer_use_mcp.planning.plan as plan_module
from computer_use_mcp.planning.plan import (
    MAX_PLAN_ACTIONS_PER_POINT,
    MAX_PLAN_POINTS,
    PLAN_EXPLICIT_PLAN_KEYS,
    PLAN_EXPLICIT_POINT_KEYS,
    PLAN_PARAM_PLAIN_SCHEMA,
    Waypoint,
    WaypointPlan,
    coerce_tolerant_plan,
    plan_explicitness_message,
    require_strict_plan_int,
    validate_plan_explicitness,
    validate_plan_model_defense,
    validate_raw_plan_boundary,
)

# --- shared plan fixtures ---------------------------------------------------------------------

COMPLETE_PLAN: dict[str, Any] = {
    "transition_duration_ms": 0,
    "point_timeout_ms": 5_000,
    "settle_ms": 0,
    "on_failure": "stop",
    "on_uncertain": "continue",
    "points": [
        {
            "id": "only",
            "x": 10,
            "y": 20,
            "arrival_condition": "the menu opened",
            "actions": [{"action": "click"}],
        }
    ],
}


def _without(missing: str) -> dict[str, Any]:
    """A plan with EXACTLY ONE required detail removed (the AC-15 rejection matrix)."""
    plan = json.loads(json.dumps(COMPLETE_PLAN))
    point = plan["points"][0]
    if missing == "points":
        del plan["points"]
    elif missing == "actions":
        del point["actions"]
    elif missing == "wait_delta":
        point["actions"] = [{"action": "wait"}]
    elif missing in plan:
        del plan[missing]
    else:
        del point[missing]
    return plan


# --- strict ints at the raw wire boundary -----------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "named"),
    [
        ({"points": [{"x": 1.5, "y": 2}]}, "points[0].x"),
        ({"points": [{"x": "1", "y": 2}]}, "points[0].x"),
        ({"points": [{"x": True, "y": 2}]}, "points[0].x"),
        ({"points": [{"x": 1, "y": 2, "settle_ms": 0.0}]}, "points[0].settle_ms"),
        ({"points": [{"x": 1, "y": 2, "duration_ms": float("nan")}]}, "points[0].duration_ms"),
        ({"points": [{"x": 1, "y": 2, "timeout_ms": float("inf")}]}, "points[0].timeout_ms"),
        ({"points": [], "transition_duration_ms": 1.0}, "transition_duration_ms"),
        ({"points": [], "point_timeout_ms": "5"}, "point_timeout_ms"),
        ({"points": [], "final_wait_ms": False}, "final_wait_ms"),
        ({"points": [], "settle_ms": True}, "settle_ms"),
        (
            {
                "points": [
                    {
                        "x": 1,
                        "y": 2,
                        "actions": [{"action": "click", "hold_ms": 2.5}],
                    }
                ]
            },
            "points[0].actions[0].hold_ms",
        ),
        (
            {
                "points": [
                    {
                        "x": 1,
                        "y": 2,
                        "actions": [{"action": "click", "delta": "0"}],
                    }
                ]
            },
            "points[0].actions[0].delta",
        ),
    ],
)
def test_the_raw_wire_boundary_rejects_every_non_integer(payload: dict, named: str) -> None:
    """A float/str/bool/NaN/Infinity plan numeric is a malformed plan naming the
    field — never a coerced one (the raw gate runs BEFORE pydantic's lax coercion)."""
    with pytest.raises(ValueError, match=re.escape(named)):
        validate_raw_plan_boundary(payload)


def test_the_raw_wire_boundary_allows_real_ints_and_nulls() -> None:
    validate_raw_plan_boundary(
        {
            "transition_duration_ms": 0,
            "point_timeout_ms": None,
            "final_wait_ms": 5000,
            "settle_ms": 0,
            "points": [
                {
                    "x": -8_192,
                    "y": 16_384,
                    "duration_ms": None,
                    "timeout_ms": 0,
                    "settle_ms": 200,
                    "actions": [
                        {
                            "action": "click",
                            "x": None,
                            "y": None,
                            "delta": 0,
                            "hold_ms": 5_000,
                            "duration_ms": None,
                        }
                    ],
                }
            ],
        }
    )


def test_the_raw_wire_boundary_names_unknown_action_keys() -> None:
    """The plan boundary CLOSES the action schema: a typo'd/extra key in a point
    action is rejected with the allowed vocabulary, never silently ignored."""
    with pytest.raises(ValueError, match="unknown key"):
        validate_raw_plan_boundary(
            {"points": [{"x": 1, "y": 2, "actions": [{"action": "click", "buton": 1}]}]}
        )


def test_strict_int_gate_rejects_bool_even_though_bool_subclasses_int() -> None:
    with pytest.raises(ValueError, match="integer"):
        require_strict_plan_int(True, "probe")
    require_strict_plan_int(None, "probe")  # explicit None stays legal
    require_strict_plan_int(7, "probe")


def test_model_defense_catches_a_model_construct_bypass() -> None:
    """A ``model_construct`` payload skips pydantic validation entirely; the second,
    model-level pass rejects smuggled non-int values before normalization can coerce."""
    smuggled = WaypointPlan.model_construct(
        points=[
            Waypoint.model_construct(
                x=1,
                y=2,
                actions=[],
                duration_ms="250",  # type: ignore[arg-type]
            )
        ],
        transition_duration_ms=1.5,  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="transition_duration_ms"):
        validate_plan_model_defense(smuggled)


def test_wire_boundary_gates_the_full_coercion_path() -> None:
    with pytest.raises(ValueError, match="settle_ms"):
        coerce_tolerant_plan(
            {
                "transition_duration_ms": 0,
                "point_timeout_ms": 5_000,
                "settle_ms": 0,
                "on_failure": "stop",
                "on_uncertain": "continue",
                "points": [
                    {
                        "id": "a",
                        "x": 1,
                        "y": 2,
                        "actions": [],
                        "arrival_condition": "moved",
                        "settle_ms": 25.0,
                    }
                ],
            }
        )
    with pytest.raises(ValidationError):
        # pydantic owns the structural/range errors AFTER the raw gate passes:
        WaypointPlan.model_validate({**COMPLETE_PLAN, "settle_ms": 201})


# --- tolerant coercion (kept, runtime-only) ---------------------------------------------------


def test_a_json_encoded_string_plan_is_still_accepted_at_runtime() -> None:
    """The tolerant coercion (the follow_ups pattern) is KEPT: a JSON-encoded string
    parses and validates exactly like the object. S-10 keeps this OUT of the
    advertised schema — the shape below never reappears on the wire."""
    plan = coerce_tolerant_plan(json.dumps(COMPLETE_PLAN))
    assert plan.points[0].id == "only"
    assert plan == WaypointPlan.model_validate(COMPLETE_PLAN)


def test_a_malformed_json_string_and_a_non_object_are_typed_rejections() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        coerce_tolerant_plan("{not json")
    with pytest.raises(ValueError, match="must be an object"):
        coerce_tolerant_plan(json.dumps([COMPLETE_PLAN]))


def test_an_oversized_plan_string_is_typed_rejected_at_the_cap() -> None:
    """VAL-WP7a-01 #7: the string arm is parsed behind a stated boundary cap
    (PLAN_STRING_MAX_CHARS) — an oversized string is a typed rejection, never an
    unbounded parse."""
    from computer_use_mcp.planning.plan import PLAN_STRING_MAX_CHARS

    oversized = json.dumps(COMPLETE_PLAN) + " " * (PLAN_STRING_MAX_CHARS + 1)
    with pytest.raises(ValueError, match="boundary"):
        coerce_tolerant_plan(oversized)


def test_a_pathologically_nested_plan_string_is_a_typed_rejection() -> None:
    """A deeply nested JSON string raises RecursionError inside json.loads; the
    boundary converts it into the typed rejection instead of letting the crash
    escape the validator."""
    deep = "[" * 20_000 + "]" * 20_000
    with pytest.raises(ValueError, match="deeply"):
        coerce_tolerant_plan(deep)


# --- SPEC 5.2: the required-in-a-plan rejection matrix ----------------------------------------

#: One row per required detail (AC-15's "8 rejection cases, one per required field"),
#: plus the bare-wait case.  The SECOND element is a substring the rejection message
#: must contain, so the model is told WHICH field is missing.
#:
#: ``points`` is the one row handled by the MODEL's own structural validation (an empty
#: ``points`` list is ``min_length=1``), not by the completeness rule — which is correct:
#: the points are required by the schema, and the completeness rule runs after the model
#: validates.  The third element records which layer rejects it.
REJECTION_MATRIX: tuple[tuple[str, str, str], ...] = (
    ("points", "points", "model"),
    ("actions", "points[0].actions", "explicitness"),
    ("transition_duration_ms", "transition_duration_ms", "explicitness"),
    ("wait_delta", "delta", "explicitness"),
    ("settle_ms", "settle_ms", "explicitness"),
    ("arrival_condition", "arrival_condition", "explicitness"),
    ("point_timeout_ms", "point_timeout_ms", "explicitness"),
    ("on_failure", "on_failure", "explicitness"),
    ("on_uncertain", "on_uncertain", "explicitness"),
)


@pytest.mark.parametrize(("missing", "named", "layer"), REJECTION_MATRIX)
def test_an_incomplete_plan_is_rejected_naming_the_missing_field(
    missing: str, named: str, layer: str
) -> None:
    """SPEC 5.2: one rejection per required detail, each naming the field."""
    with pytest.raises(ValueError) as excinfo:
        coerce_tolerant_plan(_without(missing))
    message = str(excinfo.value)
    assert named in message, message
    if layer == "explicitness":
        # the teach-in text is present and says what to do, not just what is wrong
        assert "EXPLICIT-TIMING mode" in message
        assert "incomplete plan" in message
    else:
        # the model's own structural gate already rejected it, naming the field
        assert "validation error" in message or "Field required" in message


def test_a_complete_plan_is_accepted() -> None:
    plan = coerce_tolerant_plan(COMPLETE_PLAN)
    assert plan.points[0].id == "only"
    assert plan.settle_ms == 0
    assert plan.on_uncertain == "continue"
    assert plan.on_failure == "stop"


def test_the_rejection_reports_every_omission_in_one_reply() -> None:
    """The model learns all of them at once rather than one per round-trip."""
    with pytest.raises(ValueError) as excinfo:
        coerce_tolerant_plan({"points": [{"id": "a", "x": 1, "y": 2}]})
    message = str(excinfo.value)
    for named in ("actions", "duration_ms", "settle_ms", "arrival_condition",
                  "point_timeout_ms", "on_failure", "on_uncertain"):
        assert named in message, named


def test_a_crossing_point_may_declare_no_actions_and_no_arrival_condition() -> None:
    """The crossing-point affordance survives SPEC 5.2: a point may declare
    ``"actions": []`` — it just has to SAY so, and then there is nothing to verify
    there, so no ``arrival_condition`` is required."""
    plan = coerce_tolerant_plan(
        {
            "transition_duration_ms": 0,
            "point_timeout_ms": 5_000,
            "settle_ms": 0,
            "on_failure": "stop",
            "on_uncertain": "continue",
            "points": [{"id": "hop", "x": 1, "y": 2, "actions": []}],
        }
    )
    assert plan.points[0].actions == []


def test_an_action_level_expected_effect_covers_the_verification_declaration() -> None:
    """``arrival_condition`` is the point-level form; an action's own
    ``expected_effect`` is the action-level form, and either satisfies the rule."""
    plan = coerce_tolerant_plan(
        {
            "transition_duration_ms": 0,
            "point_timeout_ms": 5_000,
            "settle_ms": 0,
            "on_failure": "stop",
            "on_uncertain": "continue",
            "points": [
                {
                    "id": "a",
                    "x": 1,
                    "y": 2,
                    "actions": [{"action": "click", "expected_effect": "the menu opened"}],
                }
            ],
        }
    )
    assert plan.points[0].actions[0].expected_effect == "the menu opened"


def test_an_empty_arrival_condition_is_not_a_statement() -> None:
    """VAL-WP7a-02 #14 (executed in WP7b): ``""`` / whitespace-only is an ABSENCE,
    never a statement — the wire gate demands the verification statement exactly as
    if the key were missing, naming the field."""
    for blank in ("", "   ", "\n\t "):
        payload = {
            "transition_duration_ms": 0,
            "point_timeout_ms": 5_000,
            "settle_ms": 0,
            "on_failure": "stop",
            "on_uncertain": "continue",
            "points": [
                {
                    "id": "a",
                    "x": 1,
                    "y": 2,
                    "arrival_condition": blank,
                    "actions": [{"action": "click"}],
                }
            ],
        }
        with pytest.raises(ValueError) as excinfo:
            coerce_tolerant_plan(payload)
        assert "arrival_condition" in str(excinfo.value), blank


def test_an_explicit_delta_on_every_in_plan_wait_is_required() -> None:
    """Rule 4: a ``wait`` action inside a point states its 0..10 s delta; the trailing
    ``final_wait_ms`` is a separate declaration and never a substitute."""
    payload = json.loads(json.dumps(COMPLETE_PLAN))
    payload["points"][0]["actions"] = [{"action": "wait"}]
    with pytest.raises(ValueError, match="delta"):
        coerce_tolerant_plan(payload)
    payload["points"][0]["actions"] = [{"action": "wait", "delta": 2}]
    plan = coerce_tolerant_plan(payload)
    assert plan.points[0].actions[0].delta == 2


# --- VAL-WP7a-01 #1: null is an ABSENCE, never a statement ------------------------------------


#: Every explicitness key that may be stated at PLAN level, with the substring the
#: rejection must name. Setting the key to null (with the points not stating it)
#: must be a rejection — a null used to count as "stated" and silently re-opened
#: the hidden defaults SPEC 5.2 closed.
PLAN_LEVEL_NULL_MATRIX: tuple[str, ...] = (
    "transition_duration_ms",
    "point_timeout_ms",
    "settle_ms",
    "on_failure",
    "on_uncertain",
)


@pytest.mark.parametrize("key", PLAN_LEVEL_NULL_MATRIX)
def test_a_null_plan_level_key_is_missing_not_stated(key: str) -> None:
    """An explicit ``null`` plan-level explicitness key is rejected, naming the key —
    never a silent fallback to the env duration fail-safe, the module settle policy
    or the unbounded default policies."""
    payload = json.loads(json.dumps(COMPLETE_PLAN))
    payload[key] = None
    with pytest.raises(ValueError) as excinfo:
        coerce_tolerant_plan(payload)
    assert key in str(excinfo.value), key


#: Every explicitness key that may be stated at POINT level. Each row: null on the
#: point (with the plan level absent) is a rejection naming the key.
POINT_LEVEL_NULL_MATRIX: tuple[str, ...] = (
    "duration_ms",
    "settle_ms",
    "timeout_ms",
    "on_failure",
    "on_uncertain",
    "arrival_condition",
    "actions",
)


@pytest.mark.parametrize("key", POINT_LEVEL_NULL_MATRIX)
def test_a_null_point_level_key_is_missing_not_stated(key: str) -> None:
    """An explicit ``null`` point-level explicitness key is an absence: the plan is
    rejected naming the key (or its plan-level alternate)."""
    payload = json.loads(json.dumps(COMPLETE_PLAN))
    point = payload["points"][0]
    point[key] = None
    if key in ("duration_ms", "settle_ms", "timeout_ms", "on_failure", "on_uncertain"):
        # remove the plan-level cover so the null point is genuinely uncovered
        plan_key = {
            "duration_ms": "transition_duration_ms",
            "settle_ms": "settle_ms",
            "timeout_ms": "point_timeout_ms",
        }.get(key, key)
        if plan_key in payload:
            payload[plan_key] = None
    with pytest.raises(ValueError) as excinfo:
        coerce_tolerant_plan(payload)
    message = str(excinfo.value)
    assert key in message or {
        "duration_ms": "transition_duration_ms",
        "settle_ms": "settle_ms",
        "timeout_ms": "point_timeout_ms",
    }.get(key, key) in message, message


def test_a_null_point_value_never_reaches_the_normalizer_fallbacks() -> None:
    """The #1 end-to-end statement: a null transition/settle/timeout can NOT ride
    into normalize_plan's fail-safe chain — the wire rejects the plan before any
    normalizer fallback (env duration, module settle, default policies) is reached."""
    payload = json.loads(json.dumps(COMPLETE_PLAN))
    payload["transition_duration_ms"] = None
    payload["settle_ms"] = None
    payload["point_timeout_ms"] = None
    with pytest.raises(ValueError, match="incomplete plan"):
        coerce_tolerant_plan(payload)


# --- VAL-WP7a-01 #2: plan-or-point means EVERY point ------------------------------------------


#: The five plan-or-point keys: stated on ONE of two points (plan level absent) is
#: NOT a statement — the siblings would silently inherit the model default.
#: (key, point_key, plan_key, message-substring-named)
PARTIAL_STATEMENT_KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("on_failure", "on_failure", "on_failure", "on_failure"),
    ("on_uncertain", "on_uncertain", "on_uncertain", "on_uncertain"),
    ("timeout", "timeout_ms", "point_timeout_ms", "point_timeout_ms"),
    ("settle", "settle_ms", "settle_ms", "settle_ms"),
    ("duration", "duration_ms", "transition_duration_ms", "transition_duration_ms"),
)

#: The per-point statement value each matrix key uses.
_PER_POINT_VALUES: dict[str, Any] = {
    "on_failure": "stop",
    "on_uncertain": "continue",
    "timeout": 5_000,
    "settle": 0,
    "duration": 0,
}


@pytest.mark.parametrize(("key", "point_key", "plan_key", "named"), PARTIAL_STATEMENT_KEYS)
def test_one_stated_point_never_speaks_for_its_sibling(
    key: str, point_key: str, plan_key: str, named: str
) -> None:
    """A two-point plan where only point 0 states the key (the plan does not state it
    either) is rejected naming the key — the teach-in text says 'on every point'."""
    payload = json.loads(json.dumps(COMPLETE_PLAN))
    second = json.loads(json.dumps(payload["points"][0]))
    second["id"] = "second"
    payload["points"] = [payload["points"][0], second]
    payload.pop(plan_key, None)  # remove ONLY the plan-level cover under test
    payload["points"][0][point_key] = _PER_POINT_VALUES[key]
    with pytest.raises(ValueError) as excinfo:
        coerce_tolerant_plan(payload)
    assert named in str(excinfo.value), str(excinfo.value)


@pytest.mark.parametrize(("key", "point_key", "plan_key", "named"), PARTIAL_STATEMENT_KEYS)
def test_a_key_stated_on_every_point_satisfies_the_rule(
    key: str, point_key: str, plan_key: str, named: str
) -> None:
    """The positive half: the SAME two-point plan with the key stated on EVERY point
    is accepted (the per-point values win over the model defaults)."""
    payload = json.loads(json.dumps(COMPLETE_PLAN))
    second = json.loads(json.dumps(payload["points"][0]))
    second["id"] = "second"
    payload["points"] = [payload["points"][0], second]
    payload.pop(plan_key, None)  # remove ONLY the plan-level cover under test
    for point in payload["points"]:
        point[point_key] = _PER_POINT_VALUES[key]
    plan = coerce_tolerant_plan(payload)
    assert len(plan.points) == 2


def test_the_wire_rule_does_not_touch_direct_construction() -> None:
    """The rule lives at the WIRE boundary only.  A directly constructed
    ``WaypointPlan`` still inherits its model defaults, so the stepwise handshake's
    continuations, the plan queue and every model-level unit are untouched."""
    plan = WaypointPlan(points=[{"id": "a", "x": 1, "y": 2}])
    assert plan.on_uncertain == "continue"  # C5: the default is KEPT
    assert plan.on_failure == "stop"
    assert plan.settle_ms is None
    assert plan.transition_duration_ms is None
    assert Waypoint.model_validate({"x": 1, "y": 2}).on_uncertain == "continue"


def test_the_explicitness_key_lists_cannot_drift_from_the_validator() -> None:
    """The two closed key lists are the validator's own vocabulary: every plan-level
    key appears in the teach-in text and every point-level alternate too."""
    message = plan_explicitness_message(["probe"])
    for key in PLAN_EXPLICIT_PLAN_KEYS:
        assert key in message, key
    for key in PLAN_EXPLICIT_POINT_KEYS:
        assert key in message, key


# --- S-05: the docstring examples pass their own validator ------------------------------------


_FENCE = re.compile(r"```json\s*(.*?)```", re.DOTALL)


def _docstring_plan_examples() -> list[str]:
    """Every fenced JSON block in plan.py's docstrings (S-05 extraction)."""
    source = Path(plan_module.__file__).read_text(encoding="utf-8")
    examples = [match.group(1) for match in _FENCE.finditer(source)]
    assert examples, "the S-05 extraction found no fenced example — fix the test"
    return examples


@pytest.mark.parametrize(
    "example", _docstring_plan_examples(), ids=lambda example: f"example-{example[:24]!r}"
)
def test_every_docstring_plan_example_passes_the_boundary(example: str) -> None:
    """S-05: the teaching text can never drift — every fenced plan example in
    plan.py survives the EXACT boundary a submitted plan takes (raw strict-int gate,
    strict model validation, SPEC 5.2 explicitness)."""
    payload = json.loads(example)
    plan = coerce_tolerant_plan(payload)
    assert plan.points, "the example must declare at least one point"


def test_the_docstring_example_covers_the_explicitness_vocabulary() -> None:
    """The example is a COMPLETE declaration (stated at plan level) and exercises a
    crossing point, so it demonstrates every required key, not just a lucky subset."""
    example = json.loads(_docstring_plan_examples()[0])
    for key in PLAN_EXPLICIT_PLAN_KEYS:
        assert key in example, key
    actions_shapes = {tuple(sorted(point.keys())) for point in example["points"]}
    assert any("actions" in keys and "settle_ms" in keys for keys in actions_shapes)
    assert any(point["actions"] == [] for point in example["points"])


# --- S-10: the advertised wire schema ---------------------------------------------------------


def _walk(node: Any) -> list[Any]:
    """Depth-first walk over a JSON-schema-shaped structure."""
    out = [node]
    if isinstance(node, dict):
        for value in node.values():
            out.extend(_walk(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_walk(value))
    return out


def test_the_advertised_plan_schema_has_no_union_or_ref_forms() -> None:
    """S-10: no ``anyOf``/``oneOf``/``allOf``/``not`` and no ``$ref``/``$defs``
    anywhere in the advertised schema (the REM-G plain-schema rule, extended per
    VAL-WP7a-01 #8), and NO ``"string"`` arm — the object|string union the finding
    names is gone."""
    for node in _walk(PLAN_PARAM_PLAIN_SCHEMA):
        if isinstance(node, dict):
            for banned in ("anyOf", "oneOf", "allOf", "not", "$ref", "$defs"):
                assert banned not in node, (banned, node)
    assert PLAN_PARAM_PLAIN_SCHEMA["type"] == ["object", "null"]
    assert "string" not in PLAN_PARAM_PLAIN_SCHEMA["type"]


def test_the_advertised_plan_schema_is_required_and_fail_closed() -> None:
    """``points`` is the only required key; everything else is additive-optional with
    the omission semantics the tool contract relies on; unknown keys are closed."""
    assert PLAN_PARAM_PLAIN_SCHEMA["required"] == ["points"]
    assert PLAN_PARAM_PLAIN_SCHEMA["additionalProperties"] is False
    points_schema = PLAN_PARAM_PLAIN_SCHEMA["properties"]["points"]
    assert points_schema["minItems"] == 1
    waypoint_schema = points_schema["items"]
    assert waypoint_schema["required"] == ["x", "y"]
    action_schema = waypoint_schema["properties"]["actions"]["items"]
    assert action_schema["required"] == ["action"]


def test_the_advertised_plan_schema_bounds_match_the_models() -> None:
    """Every bound in the schema is read from the SAME place the models read theirs —
    the test derives its expectations from the models' OWN field metadata
    (VAL-WP7a-01 #8), so a drift fails here, never silently on the wire."""
    from computer_use_mcp.core.models import ActionSpec

    def _model_bounds(field: Any) -> tuple[Any, Any]:
        ge = le = None
        for constraint in field.metadata:
            ge = getattr(constraint, "ge", ge)
            le = getattr(constraint, "le", le)
        return ge, le

    def _schema_bounds(schema: dict[str, Any]) -> tuple[Any, Any]:
        return schema.get("minimum"), schema.get("maximum")

    def _max_len(field: Any) -> int | None:
        for constraint in field.metadata:
            value = getattr(constraint, "max_length", None)
            if value is not None:
                return int(value)
        return None

    plan_props = PLAN_PARAM_PLAIN_SCHEMA["properties"]
    waypoint_schema = plan_props["points"]["items"]
    waypoint_props = waypoint_schema["properties"]
    action_schema = waypoint_props["actions"]["items"]
    action_props = action_schema["properties"]

    for name in ("x", "y"):
        assert _schema_bounds(waypoint_props[name]) == _model_bounds(Waypoint.model_fields[name])
        for spec_name in (name, f"{name}2"):
            assert _schema_bounds(action_props[spec_name]) == _model_bounds(
                ActionSpec.model_fields[spec_name]
            )
    for name in ("timeout_ms", "duration_ms", "settle_ms"):
        assert _schema_bounds(waypoint_props[name]) == _model_bounds(
            Waypoint.model_fields[name]
        ), name
    assert waypoint_props["arrival_condition"]["maxLength"] == _max_len(
        Waypoint.model_fields["arrival_condition"]
    )
    for name in ("point_timeout_ms", "transition_duration_ms", "final_wait_ms", "settle_ms"):
        assert _schema_bounds(plan_props[name]) == _model_bounds(WaypointPlan.model_fields[name])
    for name in ("delta", "hold_ms", "duration_ms"):
        assert _schema_bounds(action_props[name]) == _model_bounds(ActionSpec.model_fields[name])
    for name in ("text", "keys", "target", "expected_effect", "via"):
        field_max = _max_len(ActionSpec.model_fields[name])
        schema_max = (
            action_props[name].get("maxLength")
            if "maxLength" in action_props[name]
            else action_props[name].get("maxItems")
        )
        assert schema_max == field_max, name
    assert plan_props["points"]["maxItems"] == MAX_PLAN_POINTS
    assert waypoint_props["actions"]["maxItems"] == MAX_PLAN_ACTIONS_PER_POINT
    assert waypoint_schema["properties"]["id"]["maxLength"] == _max_len(
        Waypoint.model_fields["id"]
    )


def test_the_advertised_plan_schema_key_sets_match_the_models() -> None:
    """The schema's property names are exactly the model field names — a field added
    to a model without its schema counterpart (or vice versa) fails here."""
    from computer_use_mcp.core.models import ActionSpec

    plan_fields = set(WaypointPlan.model_fields) - {"points"}
    plan_props = PLAN_PARAM_PLAIN_SCHEMA["properties"]
    assert set(plan_props) - {"points"} == plan_fields
    waypoint_fields = set(Waypoint.model_fields)
    waypoint_props = PLAN_PARAM_PLAIN_SCHEMA["properties"]["points"]["items"]["properties"]
    assert set(waypoint_props) == waypoint_fields
    action_props = waypoint_props["actions"]["items"]["properties"]
    assert set(action_props) == set(ActionSpec.model_fields)


def test_the_advertised_action_enum_is_the_exact_action_vocabulary() -> None:
    from computer_use_mcp.core.models import ActionType

    enum = (
        PLAN_PARAM_PLAIN_SCHEMA["properties"]["points"]["items"]["properties"]["actions"][
            "items"
        ]["properties"]["action"]["enum"]
    )
    assert enum == [member.value for member in ActionType]


# --- VAL-WP7a-01 #9: plan.py imports nothing from server.py -----------------------------------


def test_plan_py_imports_nothing_from_the_server_module() -> None:
    """Pinned by AST over the plan.py SOURCE (not by import-time observation): no
    ``import server`` / ``from .server`` / ``from computer_use_mcp.server`` anywhere
    in the module — the data layer can never grow a dependency on the surface it
    serves."""
    import ast

    source = Path(plan_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert imported, "the AST walk found no imports — fix the test"
    for module in imported:
        assert "server" not in module.split("."), module


def test_plan_py_imports_stay_in_the_declared_data_layer_set() -> None:
    """The full import vocabulary of the data layer, pinned: pydantic + stdlib + the
    three sibling modules (models/backend/redaction). A new dependency is a decision,
    never an accident. The backend import is JUSTIFIED (wp7a-notes.md): it is the
    single home of the CORTEX_PLAN_MOVE_DURATION_MS knob resolution (band
    0..500, fallback-not-clamp, WP2); moving the constant out of backend.py would
    split the motion-knob table and touch WP2 pins for zero gain — any plan execution
    path already imports backend through the server."""
    import ast

    source = Path(plan_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    local_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level >= 1:
            local_modules.add(node.module or "")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("computer_use_mcp"):
                    local_modules.add(alias.name)
    assert local_modules == {"desktop.backend", "core.models", "security.redaction"}
