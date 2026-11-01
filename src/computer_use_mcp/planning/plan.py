"""Waypoint-plan data layer (ORVEX-2026-108 WP7a).

This module owns everything about a submitted waypoint plan that can be decided
WITHOUT touching a session: the wire models (:class:`Waypoint` /
:class:`WaypointPlan`), the strict-integer wire boundary, the SPEC 5.2
explicitness validator, the tolerant boundary coercion, the normalizer that turns
a plan into action-queue items, the per-point settle stamping (the E-01 producer
the WP2 consumer in ``backend.py`` has been waiting for), the step slices, the
stepwise lease state, and the pure terminal aggregate. It imports nothing from
``server.py`` — WP7b wires this layer into ``computer_execute``.

ONE normalization path, NO second executor: a plan normalizes to plain
action-queue items (``[move, actions...]`` per point) and runs through the
EXISTING queue, so every item inherits the full independent pipeline
(ground -> validate -> safety -> approval -> execute -> verify) — "wait and
re-observe" per point is inherited, never bypassed (RFC §4.1, decision D1).

A plan is the EXPLICIT-TIMING mode (ORVEX-2026-102 §5.2, SPEC 5.2): outside a
plan a single action is instant (0 ms move, 0 ms settle — SPEC 5.1), so a
sequence of steps must state every timing, verification and uncertainty decision
itself. A submitted plan that leans on a model default is rejected, naming every
missing field, BEFORE any capture or dispatch.

S-10 (the advertised wire schema): the ``plan`` parameter is advertised as a
TYPED OBJECT — :data:`PLAN_PARAM_PLAIN_SCHEMA` is a plain inline JSON Schema
(``"type": ["object", "null"]`` with fully spelled-out nested ``properties``),
carrying NO ``anyOf``, NO ``$ref``/``$defs``, and NO ``"string"`` arm — the
REM-G plain-schema rule holds verbatim. A JSON-encoded string is still accepted
AT RUNTIME (the follow_ups tolerance pattern): tolerance lives behind the
validation boundary, never in the advertised shape, so the object|string union
S-10 forbids never reappears on the wire.

The example below is the teaching text AND a live fixture: it passes this
module's own validators, and a permanent test extracts every fenced example from
this file and runs it through the same boundary (S-05).

```json
{
  "transition_duration_ms": 250,
  "point_timeout_ms": 5000,
  "settle_ms": 0,
  "on_failure": "stop",
  "on_uncertain": "continue",
  "final_wait_ms": 1000,
  "points": [
    {
      "id": "open-menu",
      "x": 420,
      "y": 180,
      "actions": [{"action": "click", "expected_effect": "the menu is open"}]
    },
    {
      "id": "choose-save",
      "x": 850,
      "y": 720,
      "duration_ms": 120,
      "settle_ms": 40,
      "arrival_condition": "the file is saved",
      "actions": [{"action": "click"}]
    },
    {
      "id": "crossing",
      "x": 500,
      "y": 400,
      "actions": []
    }
  ]
}
```
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..desktop.backend import PLAN_MOVE_DURATION_MS
from ..core.models import (
    MAX_DURATION_MS,
    MAX_HOLD_MS,
    MAX_PLAN_SETTLE_MS,
    ActionSpec,
    ActionType,
    GroundedAction,
    Point,
    _new_id,
)
from ..security.redaction import redact_text

# --- limits (master-mission WP9 list; read from ONE place) -----------------------------------
# The numeric bands live on the pydantic fields below and on the WP2 constants imported
# above (MAX_HOLD_MS / MAX_DURATION_MS / MAX_PLAN_SETTLE_MS from models.py). The plan
# scale caps are declared here — plan.py is the single home of every plan limit.

#: Hard cap on the points of one ``WaypointPlan`` (RFC §7-D10). Enforced by the
#: model's own ``Field(max_length=...)``; the advertised schema reads the same cap.
MAX_PLAN_POINTS = 10

#: Hard cap on the actions attached to ONE waypoint (RFC §7-D10).
MAX_PLAN_ACTIONS_PER_POINT = 5

#: Hard cap for a waypoint's whole-point cooperative timeout in milliseconds
#: (RFC §1.2; ``None`` = the session ``Limits`` remain the default governor).
MAX_PLAN_TIMEOUT_MS = 60_000


def _numeric_bounds(field: Any) -> tuple[int | float | None, int | float | None]:
    """A pydantic field's ``(ge, le)`` from its annotated constraints (one source).

    The advertised schema (#8) and its tests read the SAME numbers from the models'
    own field metadata — a bound drifts only loudly.
    """
    ge: int | float | None = None
    le: int | float | None = None
    for constraint in field.metadata:
        ge = getattr(constraint, "ge", ge)
        le = getattr(constraint, "le", le)
    return ge, le


def _max_length(field: Any) -> int | None:
    """A pydantic field's ``max_length`` (string or array) from its constraints."""
    for constraint in field.metadata:
        value = getattr(constraint, "max_length", None)
        if value is not None:
            return int(value)
    return field.max_length


#: The Point coordinate bounds, read from the model (never re-literalized here).
_COORD_MIN, _COORD_MAX = (int(bound) for bound in _numeric_bounds(Point.model_fields["x"]))


# --- plan wire models -------------------------------------------------------------------------


class Waypoint(BaseModel):
    """One ordered stop of a host plan (ORVEX-2026-101 Batch 2, RFC §1.2).

    The governing user clarification: the model declares LOCATIONS AND ACTIONS ONLY —
    the motion BETWEEN points is an internal Cortex detail (shape/duration/easing are
    internal defaults with at most an optional ``duration_ms`` override; there are no
    path/bend parameters on this surface — the rejected alternative, RFC §0.1).
    Coordinates are SCREENSHOT-space (the same space ``computer_observe`` reports);
    the single ``_map_to_physical`` transform (F1) applies once at execution.

    ``actions`` are the ordered actions AT the point. They carry NO ``x``/``y`` — the
    point is the single location and the coordinates are injected during
    normalization (V6 fail-closed: a contradicting coordinate is malformed).
    ``x2``/``y2`` belong to ``drag`` only (the explicit drag end). An empty
    ``actions`` list is a legal crossing waypoint (move, verify the cursor, move on).

    ``on_failure``/``on_uncertain`` default to ``"stop"``/``"continue"`` — the
    deliberate/asymmetric pair a plan needs.  A point that resolves ``failed`` carries
    real evidence (a window_state expectation that never appeared), so the default
    halts the sequence.  A point that resolves ``uncertain`` carries an ABSENCE of
    evidence: since fix 1.1 a *stated* ``expected_effect`` is no longer satisfiable by
    "some pixels changed", so every pixel-only point abstains and a ``stop`` default
    would halt a well-formed plan at its first point.  ``uncertain`` is still never
    success — it is still REPORTED as ``uncertain`` on the point, the per-item queue
    entry and the handshake ``result``; the policy only decides whether the sequence
    keeps walking.  Set ``on_uncertain="stop"`` on the plan or on a single point to
    get the halt-every-uncertainty behaviour back.

    ORVEX-2026-102 §5.2 (SPEC 5.2) — **A PLAN IS THE EXPLICIT-TIMING MODE.**  A bare
    action is the *instant* mode: outside a plan the move duration and the settle are
    both ``0 ms`` (SPEC 5.1), so one action is dispatched as fast as the machine
    allows and nothing is hidden.  A plan is the mode in which the model must say
    everything out loud, because a sequence of timed steps is exactly where a hidden
    default is dangerous.  Therefore, at the plan wire boundary
    (:func:`coerce_tolerant_plan`), a SUBMITTED plan must state: the ordered
    ``points``; the ``actions`` at each point (an explicit ``[]`` declares a crossing
    point); the movement timing (``duration_ms`` per point or
    ``transition_duration_ms`` on the plan, for EVERY point); an explicit ``delta`` on
    every ``wait`` action; the ``settle_ms`` it wants (``0`` = instant); an
    ``arrival_condition`` on every point that carries an input action; a
    ``point_timeout_ms``; and BOTH ``on_failure`` and ``on_uncertain``.  The two
    ``on_*`` DEFAULT values above survive for internal/direct construction only — a
    submitted plan is rejected if it leans on them (conflict C5: ``"continue"`` is a
    measured necessity, so it stays a default but is no longer *relyable*).  Nothing
    about direct construction moves; only the WIRE demands the fields.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        default_factory=_new_id,
        min_length=1,
        max_length=64,
        # anchored: pydantic's pattern uses search semantics, so the anchors make the
        # WHOLE id the addressable token (the action_id-style vocabulary)
        pattern=r"^[A-Za-z0-9_.\-]+$",
    )
    x: int = Field(ge=-8_192, le=16_384)  # the Point bounds (models.py Point)
    y: int = Field(ge=-8_192, le=16_384)
    actions: list[ActionSpec] = Field(
        default_factory=list, max_length=MAX_PLAN_ACTIONS_PER_POINT
    )
    arrival_condition: str | None = Field(default=None, max_length=500)
    timeout_ms: int | None = Field(default=None, ge=0, le=MAX_PLAN_TIMEOUT_MS)
    on_failure: Literal["stop", "continue"] = "stop"
    on_uncertain: Literal["stop", "continue"] = "continue"
    duration_ms: int | None = Field(default=None, ge=0, le=MAX_DURATION_MS)
    # ORVEX-2026-102 §5.2 (SPEC 5.2): the EXPLICIT settle this point wants after its
    # last action, in milliseconds.  ``0`` is a legal, meaningful value ("this point
    # settles not at all"), which is the point: SPEC 5.1 removed the hidden 50 ms
    # default, so a plan now STATES the settle instead of inheriting one.  The
    # normalizer copies it onto the point's last item as the INTERNAL
    # ``GroundedAction.plan_point_settle_ms``; ``ActionSpec`` never mirrors it, so the
    # flat-action wire surface is unchanged.
    settle_ms: int | None = Field(default=None, ge=0, le=MAX_PLAN_SETTLE_MS)

    @model_validator(mode="after")
    def _actions_inherit_point_coordinates(self) -> Waypoint:
        """V6: the point IS the location — fail closed on any coordinate contradiction.

        Actions must not carry ``x``/``y`` (injected from the waypoint during
        normalization); ``x2``/``y2`` are drag-only (an explicit end is required for a
        drag inside a point; the waypoint coordinates are the drag start).
        """
        for index, spec in enumerate(self.actions):
            if spec.x is not None or spec.y is not None:
                raise ValueError(
                    f"actions[{index}] ({spec.action.value}) must not carry x/y: the "
                    "waypoint coordinates are the single location (they are injected "
                    "into every action of the point)"
                )
            if spec.action is ActionType.DRAG:
                if spec.x2 is None or spec.y2 is None:
                    raise ValueError(
                        f"actions[{index}] is a drag and requires explicit x2/y2 (the "
                        "drag end); the waypoint coordinates are the drag start"
                    )
            elif spec.x2 is not None or spec.y2 is not None:
                raise ValueError(
                    f"actions[{index}] ({spec.action.value}) must not carry x2/y2 "
                    "(drag-only fields)"
                )
        return self


class WaypointPlan(BaseModel):
    """An ordered waypoint plan (ORVEX-2026-101 Batch 2, RFC §1.3).

    Normalizes internally to plain action-queue items (``[move, actions...]`` per
    point) and executes through the EXISTING queue, so every item inherits the full
    independent pipeline — "wait and re-observe" per point is inherited, never
    bypassed (RFC §4.1, decision D1: the plan is a thin layer OVER the queue, never a
    second executor). Plan-level fields are the INHERITED defaults for points that do
    not set their own. Unknown fields are rejected fail-closed (``extra="forbid"``) —
    a typo'd plan key is an error, never a silently ignored instruction.

    ``on_failure``/``on_uncertain`` are the INHERITED defaults for points that do not
    set their own: ``"stop"`` for a failure (real evidence that the sequence is
    broken) and ``"continue"`` for an uncertainty (an absence of evidence, which since
    fix 1.1 is what every pixel-only point carrying a stated ``expected_effect``
    resolves to).  Both are overridable per plan and per point; neither ever changes
    what a point is REPORTED as — a passed-over uncertain point is still reported
    ``uncertain``, never ``completed`` and never ``verified``.

    ORVEX-2026-102 §5.2 (SPEC 5.2): this model is the EXPLICIT-TIMING mode — see
    :class:`Waypoint`'s docstring.  ``transition_duration_ms``/``point_timeout_ms``/
    ``on_failure``/``on_uncertain`` are all REQUIRED TO BE STATED on a submitted plan
    (the wire boundary rejects a plan that omits them, naming the field), while the
    model-level defaults here are unchanged so internal/direct construction and the
    stepwise handshake's ``plan_action`` continuations are untouched.  With the model
    default for the transition at ``0`` and SPEC 5.1's ``0 ms`` settle, an unstated
    plan therefore moves instantly and settles nowhere: the fail-safe, never a hidden
    animation.
    """

    model_config = ConfigDict(extra="forbid")

    points: list[Waypoint] = Field(min_length=1, max_length=MAX_PLAN_POINTS)
    on_failure: Literal["stop", "continue"] = "stop"
    on_uncertain: Literal["stop", "continue"] = "continue"
    point_timeout_ms: int | None = Field(default=None, ge=0, le=MAX_PLAN_TIMEOUT_MS)
    transition_duration_ms: int | None = Field(default=None, ge=0, le=MAX_DURATION_MS)
    final_wait_ms: int | None = Field(default=None, ge=0, le=MAX_DURATION_MS)
    # ORVEX-2026-102 §5.2 (SPEC 5.2): the plan-level DEFAULT settle for points that do
    # not state their own — the same inherit-override shape as
    # ``transition_duration_ms``.  ``0`` is legal and means "no settle", which is what
    # makes stating it meaningful now that SPEC 5.1 removed the hidden 50 ms default.
    settle_ms: int | None = Field(default=None, ge=0, le=MAX_PLAN_SETTLE_MS)

    @model_validator(mode="after")
    def _point_ids_unique(self) -> WaypointPlan:
        """V6: waypoint ids are the addresses of the aggregate results — duplicates
        would make per-point reporting ambiguous, so they fail construction."""
        seen: set[str] = set()
        for point in self.points:
            if point.id in seen:
                raise ValueError(
                    f"waypoint ids must be unique within a plan; duplicate id: {point.id!r}"
                )
            seen.add(point.id)
        return self


# --- the strict-integer wire boundary ---------------------------------------------------------
# This closed vocabulary belongs ONLY to the raw ``plan`` wire boundary.  ``ActionSpec``
# itself intentionally keeps its legacy pydantic ``extra`` behavior for ``follow_ups``;
# changing that shared model would change the established host path outside this
# remediation. (The plan boundary additionally rejects unknown action keys outright.)

_PLAN_ACTION_ALLOWED_KEYS = frozenset(ActionSpec.model_fields)
#: ORVEX-2026-102 §5.2: ``settle_ms`` joins the point's strict-int wire fields (the
#: same fail-closed gate as ``duration_ms``/``timeout_ms`` — a float/str/bool settle is
#: a malformed plan, never a coerced one).
_PLAN_POINT_NUMERIC_FIELDS = frozenset({"x", "y", "duration_ms", "timeout_ms", "settle_ms"})
_PLAN_ACTION_NUMERIC_FIELDS = frozenset(
    {"x", "y", "x2", "y2", "delta", "hold_ms", "duration_ms"}
)
_PLAN_NUMERIC_FIELDS = frozenset(
    {"point_timeout_ms", "transition_duration_ms", "final_wait_ms", "settle_ms"}
)


def require_strict_plan_int(value: Any, path: str) -> None:
    """Accept only a real Python ``int`` (or explicit ``None``) on the plan wire.

    Pydantic v2 runs lax integer coercion before ordinary field/model validators,
    so checking a converted model cannot prove that the host sent an integer.  This
    raw, plan-only gate deliberately rejects ``bool`` even though ``bool`` subclasses
    ``int``.  It is intentionally independent of the legacy ``follow_ups`` boundary.
    """
    if value is None:
        return
    if isinstance(value, bool) or type(value) is not int:
        raise ValueError(
            f"plan field {path} must be a JSON/Python integer (not bool, float, "
            f"string, NaN, or Infinity); got {type(value).__name__}"
        )


def validate_raw_plan_boundary(value: dict[str, Any]) -> None:
    """Close the nested plan schema before pydantic can coerce or discard input."""
    for field_name in _PLAN_NUMERIC_FIELDS:
        if field_name in value:
            require_strict_plan_int(value[field_name], field_name)

    points = value.get("points")
    if not isinstance(points, (list, tuple)):
        return  # pydantic owns the structural error for a non-list points field
    for point_index, point in enumerate(points):
        if not isinstance(point, dict):
            continue  # pydantic owns the structural point error
        point_path = f"points[{point_index}]"
        for field_name in _PLAN_POINT_NUMERIC_FIELDS:
            if field_name in point:
                require_strict_plan_int(point[field_name], f"{point_path}.{field_name}")
        actions = point.get("actions")
        if not isinstance(actions, (list, tuple)):
            continue
        for action_index, action in enumerate(actions):
            if not isinstance(action, dict):
                continue  # pydantic owns the structural action error
            action_path = f"{point_path}.actions[{action_index}]"
            unknown = sorted(set(action) - _PLAN_ACTION_ALLOWED_KEYS)
            if unknown:
                raise ValueError(
                    f"plan field {action_path} contains unknown key(s): "
                    f"{', '.join(unknown)}; allowed keys: "
                    f"{', '.join(sorted(_PLAN_ACTION_ALLOWED_KEYS))}"
                )
            for field_name in _PLAN_ACTION_NUMERIC_FIELDS:
                if field_name in action:
                    require_strict_plan_int(action[field_name], f"{action_path}.{field_name}")


def validate_plan_model_defense(plan: WaypointPlan) -> WaypointPlan:
    """Recheck numeric values that survived possible ``model_construct`` bypasses.

    Normal wire input has already passed :func:`validate_raw_plan_boundary`; this
    second, model-level check exists for internal/direct construction misuse.  It
    catches values that pydantic never converted because construction was bypassed,
    before normalization can build an ``ActionSpec`` and coerce them.
    """
    for field_name in _PLAN_NUMERIC_FIELDS:
        if hasattr(plan, field_name):
            require_strict_plan_int(getattr(plan, field_name), field_name)
    for point_index, point in enumerate(plan.points):
        for field_name in _PLAN_POINT_NUMERIC_FIELDS:
            if not hasattr(point, field_name):
                raise ValueError(
                    f"plan field points[{point_index}].{field_name} is missing"
                )
            require_strict_plan_int(
                getattr(point, field_name), f"points[{point_index}].{field_name}"
            )
        for action_index, action in enumerate(point.actions):
            for field_name in _PLAN_ACTION_NUMERIC_FIELDS:
                if hasattr(action, field_name):
                    require_strict_plan_int(
                        getattr(action, field_name),
                        f"points[{point_index}].actions[{action_index}].{field_name}",
                    )
    return plan


# --- SPEC 5.2: the explicitness validator -----------------------------------------------------


#: ORVEX-2026-102 §5.2 (SPEC 5.2): the plan wire boundary's closed list of what a
#: SUBMITTED plan must state.  A bare action is the INSTANT mode (0 ms duration, 0 ms
#: settle — SPEC 5.1); a plan is the EXPLICIT-TIMING mode, so nothing that affects
#: timing, verification or uncertainty handling may be inherited silently.  A
#: plan-level key covers every point that does not state its own; the per-point
#: alternates are named in the teaching text so the two lists cannot drift.
PLAN_EXPLICIT_PLAN_KEYS: tuple[str, ...] = (
    "transition_duration_ms",
    "point_timeout_ms",
    "settle_ms",
    "on_failure",
    "on_uncertain",
)
PLAN_EXPLICIT_POINT_KEYS: tuple[str, ...] = (
    "actions",
    "duration_ms",
    "settle_ms",
    "arrival_condition",
    "on_failure",
    "on_uncertain",
)


def _stated_text(value: Any) -> bool:
    """A text statement counts only when it is a string with non-whitespace content.

    VAL-WP7a-02 #14: an empty or whitespace-only ``arrival_condition`` is an ABSENCE
    of a statement, never one — it is treated exactly like a missing key (the
    explicitness gate demands the verification statement, and the normalizer never
    copies it into ``expected_effect``)."""
    return isinstance(value, str) and value.strip() != ""


def plan_explicitness_message(missing: list[str]) -> str:
    """Teach-in text for a plan rejected at the wire boundary (SPEC 5.2)."""
    return (
        "incomplete plan: "
        + "; ".join(missing)
        + ". A plan is the EXPLICIT-TIMING mode — outside a plan a single action is "
        "instant (0 ms move, 0 ms settle), but a sequence of steps must state every "
        "timing, verification and uncertainty decision itself, because there is no "
        "hidden default to fall back on. Required, each stated either on the plan "
        f"({', '.join(PLAN_EXPLICIT_PLAN_KEYS)}) or on every point "
        f"({', '.join(PLAN_EXPLICIT_POINT_KEYS)}): the ordered points and the actions "
        "at each one — an explicit empty \"actions\": [] declares a crossing point; "
        "the movement timing — transition_duration_ms on the plan or duration_ms on "
        "every point (0 = an instant jump, 1..5000 = that many ms); the wait between "
        "actions — an explicit delta on every wait action inside a point (0..10 s; "
        "final_wait_ms is a separate optional trailing wait, not a substitute for it); "
        "the settle wanted — settle_ms (0 = no settle, 1..200 = that many ms); what "
        "is verified after the step — arrival_condition, or an action-level "
        "expected_effect; the per-step "
        "cooperative timeout — point_timeout_ms or timeout_ms; and BOTH on_failure "
        "and on_uncertain (\"stop\" or \"continue\") — how to treat a failure and how "
        "to treat an uncertain step. An uncertain step is never reported as "
        "completed either way; the policy only decides whether the sequence keeps "
        "walking."
    )


def validate_plan_explicitness(value: dict[str, Any]) -> None:
    """SPEC 5.2: reject an incomplete plan BEFORE any effect (ORVEX-2026-102 §5.2).

    Runs at the plan wire boundary only, so internal/direct construction
    (``WaypointPlan(...)``, the stepwise handshake's ``plan_action`` continuations and
    every existing unit test of the models) is untouched.  Each rule names the
    missing field in the rejection and the whole thing fails closed with ONE
    aggregated message, so the model learns every omission in a single reply.

    A key counts as STATED only when it is present AND not ``null`` — an explicit
    ``null`` is an ABSENCE, never a statement (fail closed, VAL-WP7a-01 #1: ``null``
    used to pass key-presence checks and silently re-open the exact hidden defaults
    SPEC 5.2 closed — the env duration fail-safe, the module settle policy, the
    unbounded default policies).

    A key that may be stated at plan level OR per point must be stated at plan level
    or on EVERY point (VAL-WP7a-01 #2): one stated point must never let its siblings
    inherit a model default. The teach-in text says "on every point" and the rule now
    enforces exactly that.

    The rules, one per detail the user enumerated:
      1. points + their order      — ``points`` (already required by the model);
      2. the action at each point  — ``actions`` stated on every point (an explicit
         ``[]`` is the crossing-point declaration; an ABSENT or ``null`` key is not);
      3. movement timing           — ``duration_ms`` on EVERY point, or
         ``transition_duration_ms`` on the plan;
      4. inter-action waits        — an explicit ``delta`` on every ``wait`` action
         inside a plan (0..10 s, the flat action's own band);
      5. the settle wanted         — ``settle_ms`` on EVERY point, or on the plan;
      6. what is verified          — ``arrival_condition`` on every point that
         carries an input action (an action-level ``expected_effect`` covers it);
      7. the per-step timeout      — ``point_timeout_ms`` on the plan, or
         ``timeout_ms`` on EVERY point;
      8. uncertain/failure policy  — BOTH ``on_failure`` and ``on_uncertain``,
         stated on the plan or on EVERY point (conflict C5: the ``"continue"`` default
         is kept on the models but may no longer be relied upon by a submission).
    """
    points = value["points"]
    missing: list[str] = []

    def _stated(mapping: dict[str, Any], key: str) -> bool:
        return key in mapping and mapping[key] is not None

    plan_states_transition = _stated(value, "transition_duration_ms")
    plan_states_timeout = _stated(value, "point_timeout_ms")
    plan_states_settle = _stated(value, "settle_ms")

    # rules 3, 5, 7 and 8 are plan-or-point: a plan-level statement covers every
    # point; WITHOUT one, EVERY point must state its own — one stated point never
    # speaks for its siblings.  Reported ONCE at the plan level, so the model is not
    # told the same thing once per point.
    if not plan_states_timeout and not all(_stated(point, "timeout_ms") for point in points):
        missing.append("point_timeout_ms (or timeout_ms on every point)")
    if not plan_states_transition and not all(_stated(point, "duration_ms") for point in points):
        missing.append("transition_duration_ms (or duration_ms on every point)")
    if not plan_states_settle and not all(_stated(point, "settle_ms") for point in points):
        missing.append("settle_ms (0 = no settle, 1..200 = that many ms), on the plan or on every point")
    if not _stated(value, "on_failure") and not all(_stated(point, "on_failure") for point in points):
        missing.append("on_failure (on the plan or on every point)")
    if not _stated(value, "on_uncertain") and not all(
        _stated(point, "on_uncertain") for point in points
    ):
        missing.append("on_uncertain (on the plan or on every point)")

    for index, point in enumerate(points):
        path = f"points[{index}]"
        if not _stated(point, "actions"):
            missing.append(f"{path}.actions (an explicit [] declares a crossing point)")
        if not plan_states_transition and not _stated(point, "duration_ms"):
            missing.append(
                f"{path}.duration_ms (the plan states no transition_duration_ms either)"
            )
        if not plan_states_settle and not _stated(point, "settle_ms"):
            missing.append(f"{path}.settle_ms (0 = no settle, 1..200 = that many ms)")
        actions = point.get("actions") or []
        if actions and not _stated_text(point.get("arrival_condition")) and not any(
            isinstance(spec, dict) and _stated_text(spec.get("expected_effect"))
            for spec in actions
        ):
            missing.append(
                f"{path}.arrival_condition (what to verify after this step — an "
                "action-level expected_effect covers it)"
            )
        for action_index, spec in enumerate(actions):
            if not isinstance(spec, dict):
                continue
            if spec.get("action") == "wait" and "delta" not in spec:
                missing.append(
                    f"{path}.actions[{action_index}].delta (an explicit 0..10 s wait)"
                )

    if missing:
        raise ValueError(plan_explicitness_message(missing))


#: The boundary cap for a JSON-encoded-string plan submission (VAL-WP7a-01 #7).
#: A string plan larger than this is rejected with a typed error before parsing —
#: the object form is the advertised shape and has no such gate; the string arm is
#: weak-model tolerance, and an unbounded parse is a denial-of-service surface.
PLAN_STRING_MAX_CHARS = 65_536


def coerce_tolerant_plan(value: Any) -> WaypointPlan:
    """Boundary coercion for the ``plan`` parameter (the follow_ups tolerance pattern).

    Accepts the plan object as-is or a JSON-encoded STRING containing it; anything
    else (or a non-object JSON payload) raises a typed error — never a guess. The
    strict fail-closed :class:`WaypointPlan` validation (V1-V7) applies afterwards
    regardless of the wire shape.

    S-10: the string arm is RUNTIME tolerance only. The ADVERTISED wire schema
    (:data:`PLAN_PARAM_PLAIN_SCHEMA`) is the typed object with no ``"string"`` arm,
    so the tolerant path never reintroduces the object|string union on the wire.

    ORVEX-2026-102 §5.2: this is also the ONE place the "required in a plan" rule
    lives (:func:`validate_plan_explicitness`).  It runs after the model validates,
    so the model owns the structural/range errors and this owns the completeness
    error — and it runs BEFORE any capture or dispatch, so an incomplete plan has
    zero effect.
    """
    if isinstance(value, str):
        if len(value) > PLAN_STRING_MAX_CHARS:
            raise ValueError(
                f"plan string exceeds the {PLAN_STRING_MAX_CHARS}-character boundary "
                "cap; submit the plan as an object"
            )
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"plan string is not valid JSON: {exc}") from None
        except RecursionError as exc:
            # a pathologically nested payload must surface as the typed rejection,
            # never as a crash escaping the validation boundary
            raise ValueError("plan string nests too deeply to parse") from None
    if not isinstance(value, dict):
        # ValueError on purpose: pydantic converts ONLY ValueError inside BeforeValidator
        # chains into a typed ValidationError (the follow_ups precedent).
        raise ValueError(  # noqa: TRY004 - deliberate, see comment above
            "plan must be an object: {\"points\": [{\"x\", \"y\", \"actions\"}]}"
        )
    validate_raw_plan_boundary(value)
    plan = validate_plan_model_defense(WaypointPlan.model_validate(value))
    validate_plan_explicitness(value)
    return plan


# --- the normalizer (RFC §4.2) ----------------------------------------------------------------


def normalize_plan(plan: WaypointPlan) -> tuple[list[ActionSpec], dict[str, Any]]:
    """Normalize a host plan into action-queue items + plan context (RFC §4.2).

    Runs the model-level defense FIRST (VAL-WP7a-01 #6): values that survived a
    ``model_construct`` bypass are rechecked before any ``ActionSpec`` can laxly
    coerce them.

    Per point: one ``move`` transition first, then the point's actions in order with
    the point's coordinates injected (V6: actions never carry x/y) and the
    ``arrival_condition`` riding as ``expected_effect`` ONLY for actions that lack an
    explicit one (a host declaration is never overwritten). The transition duration
    resolves point ?? plan ?? the internal plan default (motion stays an internal
    detail; an explicit ``0`` passes through as an explicit jump). ``final_wait_ms``
    appends one trailing ``wait`` element when set. The returned ``policies`` list is
    aligned with the queue items and carries the point-end stop policy (RFC T7/T8)
    at the LAST item of each point.
    """
    validate_plan_model_defense(plan)
    plan_id = _new_id()
    specs: list[ActionSpec] = []
    policies: list[dict[str, Any] | None] = []
    points_context: list[dict[str, Any]] = []
    for index, waypoint in enumerate(plan.points):
        first_item = len(specs)
        # point ?? plan inheritance (an explicitly-set point value always wins; the
        # pydantic defaults on the Literal policies are indistinguishable from
        # absent, so the set-ness check is the honest inheritance test)
        on_failure = (
            waypoint.on_failure
            if "on_failure" in waypoint.model_fields_set
            else plan.on_failure
        )
        on_uncertain = (
            waypoint.on_uncertain
            if "on_uncertain" in waypoint.model_fields_set
            else plan.on_uncertain
        )
        duration_ms = (
            waypoint.duration_ms
            if waypoint.duration_ms is not None
            else plan.transition_duration_ms
        )
        if duration_ms is None:
            # ORVEX-2026-102 §5.2 (SPEC 5.2): inside a plan the duration is REQUIRED
            # FROM THE MODEL, so this constant is a FAIL-SAFE (0 = an instant jump),
            # not a behaviour.  It is read here and ONLY here — a flat action never
            # touches it — which is why changing the default cannot slow one. The
            # value is CORTEX_PLAN_MOVE_DURATION_MS (band 0..500, fallback-not-clamp),
            # resolved once at import in backend.py.
            duration_ms = PLAN_MOVE_DURATION_MS
        # ORVEX-2026-102 §5.2: the point's EXPLICIT settle, point ?? plan ?? None.  None
        # means "not stated" and the dispatch path falls back to the module policy,
        # which SPEC 5.1 now sets to 0 ms.  It rides the point's policies entry (not
        # ActionSpec) and is applied to the GroundedAction by the plan queue, so the
        # flat-action wire surface gains nothing.
        settle_ms = (
            waypoint.settle_ms
            if waypoint.settle_ms is not None
            else plan.settle_ms
        )
        specs.append(ActionSpec(action="move", x=waypoint.x, y=waypoint.y, duration_ms=duration_ms))
        policies.append(None)
        for spec in waypoint.actions:
            updates: dict[str, Any] = {"x": waypoint.x, "y": waypoint.y}
            if spec.expected_effect is None and _stated_text(waypoint.arrival_condition):
                updates["expected_effect"] = waypoint.arrival_condition
            specs.append(spec.model_copy(update=updates))
            policies.append(None)
        last_item = len(specs) - 1
        policies[last_item] = {
            "point_index": index,
            "point_id": waypoint.id,
            "first_item_index": first_item,
            "on_failure": on_failure,
            "on_uncertain": on_uncertain,
            "settle_ms": settle_ms,
        }
        points_context.append(
            {
                "id": waypoint.id,
                "index": index,
                "first_item": first_item,
                "last_item": last_item,
                "timeout_ms": (
                    waypoint.timeout_ms
                    if waypoint.timeout_ms is not None
                    else plan.point_timeout_ms
                ),
            }
        )
    final_wait_item: int | None = None
    if plan.final_wait_ms is not None and plan.final_wait_ms > 0:
        # ceil to whole seconds (the wait action's integer delta), at least 1s: the
        # tail waits AT LEAST the requested time.
        #
        # ORVEX-2026-102 §5.0 item 5 / K-13: ``final_wait_ms`` is
        # ``le=MAX_DURATION_MS = 5_000`` (models.py), so a request above 5000 ms is
        # REJECTED BY THE MODEL before it ever reaches this line and the largest
        # delta a plan tail can generate is ``5000 // 1000 = 5``.  The two real
        # ceilings are the model's 5000 ms (binding, because it is the tighter of
        # the two) and the backend's ``WAIT_MAX_SECONDS = 10.0`` — and for a PLAN
        # TAIL the model's bound always wins, so a plan tail can never reach even
        # 10 s.  The 10 s cap is only ever observable on the FLAT ``wait`` action.
        delta = max(1, -(-plan.final_wait_ms // 1_000))
        specs.append(ActionSpec(action="wait", delta=int(delta)))
        policies.append(None)
        final_wait_item = len(specs) - 1
    for point in points_context:
        point["action_types"] = [
            spec.action.value
            for spec in specs[point["first_item"] : point["last_item"] + 1]
        ]
    return specs, {
        "plan_id": plan_id,
        "points": points_context,
        "policies": policies,
        "final_wait_ms": plan.final_wait_ms,
        "final_wait_item": final_wait_item,
        "point_count": len(plan.points),
        "normalized_item_count": len(specs),
    }


def plan_settle_markers(
    specs: list[ActionSpec], context: dict[str, Any]
) -> list[tuple[bool, int | None]]:
    """The per-item ``(plan_point_settle, plan_point_settle_ms)`` pair (E-01 producer).

    The policy list from :func:`normalize_plan` carries a point's policy dict ONLY on
    the point's LAST item — exactly the item the backend settles after (D15: a plan
    point settles ONCE, after its last dispatch and before its post-action
    observation).  This function translates that layout into the internal
    ``GroundedAction`` marker pairs: ``(True, declared_ms)`` on each point's last
    item, ``(False, None)`` on every other in-plan item (transition moves and
    intermediate actions settle NOT there), and ``(False, None)`` on the trailing
    ``final_wait`` element (the wait itself is the pacing; it belongs to no point).
    ``ActionSpec`` never mirrors either field — P3.2 item 9, no host surface.
    """
    if len(context["policies"]) != len(specs):
        raise ValueError(
            "plan policies/context are misaligned with the normalized items "
            f"({len(context['policies'])} policies for {len(specs)} items)"
        )
    return [
        (True, policy["settle_ms"]) if policy is not None else (False, None)
        for policy in context["policies"]
    ]


def stamped_plan_actions(
    specs: list[ActionSpec],
    context: dict[str, Any],
    *,
    reason_prefix: str = "MCP plan action",
) -> list[GroundedAction]:
    """Convert normalized plan items to ``GroundedAction`` s WITH the settle stamped.

    This is the REAL producer behind the WP2 seam: each spec goes through the same
    ``to_grounded`` conversion the queue uses, then carries the INTERNAL
    ``plan_point_settle`` / ``plan_point_settle_ms`` markers from
    :func:`plan_settle_markers`.  Feeding these to the backend exercises the E-01
    wiring end to end: a point that declares ``settle_ms: 200`` really sleeps 200 ms
    after its last dispatch (``tests/test_plan_settle_wiring.py`` pins the consumer).
    """
    markers = plan_settle_markers(specs, context)
    grounded: list[GroundedAction] = []
    for spec, (settle_flag, settle_ms) in zip(specs, markers, strict=True):
        action = spec.to_grounded(reason_prefix=reason_prefix)
        action.plan_point_settle = settle_flag
        action.plan_point_settle_ms = settle_ms
        grounded.append(action)
    return grounded


# --- step slices + step context ---------------------------------------------------------------


def plan_step_slices(
    plan_specs: list[ActionSpec], plan_context: dict[str, Any]
) -> list[list[ActionSpec]]:
    """Split normalized plan items into one handshake step per waypoint.

    A point's transition and all actions at that point are one indivisible handshake
    step.  The optional final wait rides on the final point step so the host never
    has to issue a separate synthetic step.
    """
    steps: list[list[ActionSpec]] = []
    for point in plan_context["points"]:
        steps.append(list(plan_specs[point["first_item"] : point["last_item"] + 1]))
    final_wait_item = plan_context.get("final_wait_item")
    if final_wait_item is not None and steps:
        steps[-1].append(plan_specs[final_wait_item])
    return steps


def plan_step_context(
    lease: PlanLease, step_index: int, step_specs: list[ActionSpec]
) -> tuple[dict[str, Any], list[dict[str, Any] | None]]:
    """Build the one-point context/policy slice consumed by the existing queue.

    The point's policy dict rides the step's LAST item with a STEP-LOCAL
    ``first_item_index`` of 0 (the queue consumes step-local ``follow_up_results``,
    so its point-resolution slice ``[first_item_index : index + 1]`` must index the
    step, never the full plan — the dev copy kept the full-plan index, which only
    worked on the removed single-call path; the stepwise handshake needs this
    rewritten or ``on_failure``/``on_uncertain`` could never halt a step).
    """
    point = lease.context["points"][step_index]
    first_item = point["first_item"]
    last_item = point["last_item"]
    source_policies = lease.context["policies"]
    policies = list(source_policies[first_item : last_item + 1])
    if lease.context.get("final_wait_item") is not None and step_index == len(lease.steps) - 1:
        policies.append(None)
    if policies and policies[-1] is not None:
        policies[-1] = {**policies[-1], "first_item_index": 0}
    step_point = {
        "id": point["id"],
        "index": point["index"],
        "first_item": 0,
        "last_item": len(step_specs) - 1,
        "timeout_ms": point["timeout_ms"],
        "action_types": [spec.action.value for spec in step_specs],
    }
    context = {
        "plan_id": lease.context["plan_id"],
        "points": [step_point],
        "policies": policies,
        "final_wait_ms": lease.context["final_wait_ms"],
        "final_wait_item": (
            len(step_specs) - 1
            if lease.context.get("final_wait_item") is not None
            and step_index == len(lease.steps) - 1
            else None
        ),
        "point_count": lease.context["point_count"],
    }
    return context, policies


# --- redaction at the sink --------------------------------------------------------------------


def redact_queue_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """F2 defense-in-depth for queue entries: redact text at the sink.

    Same behavior as the server-side helper this module replaces for the plan path
    (WP7b delegates the flat-queue sink to this function); the entry's text-bearing
    keys go through :func:`computer_use_mcp.redaction.redact_text`.
    """
    redacted = dict(entry)
    if redacted.get("message"):
        redacted["message"] = redact_text(str(redacted["message"]))[0]
    reasons = redacted.get("reasons")
    if isinstance(reasons, list):
        redacted["reasons"] = [redact_text(str(reason))[0] for reason in reasons]
    verification_note = redacted.get("verification_note")
    if verification_note:
        redacted["verification_note"] = redact_text(str(verification_note))[0]
    return redacted


# --- the stepwise lease -----------------------------------------------------------------------


#: F3 fix 3.5 (D·N2, decision D-8a) — the plan-handshake evidence KEY.
#:
#: The plan handshake publishes its backend/capture evidence under this DICT key so
#: ``evidence`` keeps meaning exactly one thing everywhere else — a LIST of evidence
#: lines. WP7b builds the value from the evidence store's ``captures()`` plus the
#: lease's ``repositions`` (S-03: real on BOTH backends, never fake-only attributes).
PLAN_EVIDENCE_KEY = "evidence_by_backend"


@dataclass
class PlanLease:
    """Session-scoped stepwise plan state (ORVEX-2026-101 decision 1).

    The lease intentionally lives on the session bundle rather than in a queue-local
    coroutine: one ``computer_execute`` call executes one waypoint step, returns to a
    blocked host, and a later call continues it.  ``cursor`` is the next unexecuted
    point; ``prefix`` is the only truthful per-item evidence retained across calls.
    """

    model: WaypointPlan
    specs: list[ActionSpec]
    context: dict[str, Any]
    steps: list[list[ActionSpec]]
    cursor: int = 0
    prefix: list[dict[str, Any]] = field(default_factory=list)
    source_indices: list[int] = field(default_factory=list)
    approved: bool = False
    repositions: list[dict[str, Any]] = field(default_factory=list)
    remainder: list[dict[str, Any]] = field(default_factory=list)
    # F11 (VAL-WP8-01): the replace disclosure rides ONLY the replace call's own
    # reply — the server clears both fields right after that reply is built.
    replaced_plan_id: str | None = None
    replaced_prefix: list[dict[str, Any]] | None = None
    # ORVEX-2026-105 D-3: the Interference Guard's events are produced on the per-call
    # outcome and are therefore GONE by the time a later call builds a reply — a
    # terminal reply executes in a different stack frame that never saw the step's
    # outcome, so deferring is structurally impossible and the lease is the only
    # cross-call carrier.  Appended at the one site where the step outcome and the
    # lease are simultaneously in scope, read back by the terminal aggregate.
    interference_events: list[str] = field(default_factory=list)
    # F3 (VAL-WP8-01): the terminal lifecycle audit event is IDEMPOTENT per lease —
    # the first terminal path to emit (the in-step TaskStopped handler or the
    # session-end funnel racing it) sets this and the loser stays silent, so a
    # killed session mid-step produces exactly ONE terminal event for the plan.
    terminal_audited: bool = False
    # N4 (VAL-WP8-02): True while a step's dispatch is in flight. The session-end
    # funnel DEFERS to an in-flight step (it skips its own terminal emission) so
    # the record order is plan_point BEFORE the terminal and the terminal counts
    # are read from the FINAL prefix. Every step path that runs while this is set
    # emits the terminal itself (TaskStopped handler, the killed-step reply, the
    # BaseException handler); the armed stop token guarantees the in-flight step
    # wakes, so the defer cannot lose the terminal event.
    step_in_flight: bool = False
    # DELTA 4 (VAL-WP8-04): set by the funnel (under the terminal-audit lock)
    # when it found the step in flight — the step's SINGLE try/finally closes
    # the lifecycle on every exit (the step's own plan_point is emitted before
    # the finally runs, so the funnel can never emit terminal-before-point).
    kill_deferred: bool = False


def merge_plan_step_prefix(
    lease: PlanLease, entries: list[dict[str, Any]], *, source_offset: int
) -> None:
    """Merge one queue result into the session prefix, preserving both indices."""
    offset = len(lease.prefix)
    for local_index, entry in enumerate(entries):
        copied = dict(entry)
        copied["index"] = offset + local_index
        copied = redact_queue_entry(copied)
        lease.prefix.append(copied)
        lease.source_indices.append(source_offset + local_index)
        lease.repositions.extend(list(copied.get("repositions", []) or []))
    lease.context["partial_results"] = lease.prefix


def set_plan_step_remainder(
    lease: PlanLease, step_index: int, step_specs: list[ActionSpec],
    entries: list[dict[str, Any]],
) -> None:
    """Record unexecuted actions inside a partially attempted point."""
    consumed = len(entries)
    pending = step_specs[consumed:]
    if not pending:
        lease.remainder = []
        return
    waypoint = lease.model.points[step_index]
    lease.remainder = [
        {
            "step_index": step_index,
            "point": {"id": waypoint.id, "x": waypoint.x, "y": waypoint.y},
            "action_types": [spec.action.value for spec in pending],
            "reason": "previous_step_failed",
        }
    ]


def plan_remaining(lease: PlanLease, reason: str) -> list[dict[str, Any]]:
    """Return bounded, truthful descriptions of all unexecuted work."""
    remaining: list[dict[str, Any]] = [dict(item) for item in lease.remainder]
    remainder_indices = {int(item["step_index"]) for item in remaining}
    for index in range(lease.cursor, len(lease.model.points)):
        if index in remainder_indices:
            continue
        waypoint = lease.model.points[index]
        remaining.append(
            {
                "step_index": index,
                "point": {"id": waypoint.id, "x": waypoint.x, "y": waypoint.y},
                "action_types": [spec.action.value for spec in waypoint.actions],
                "reason": reason,
            }
        )
    return remaining


def plan_not_executed(lease: PlanLease, reason: str) -> list[dict[str, Any]]:
    """Decision 3: the explicit, bounded list of plan work that never executed.

    Same shape and same boundedness as ``remaining`` (decision 6 additive).  The
    only difference is the per-step ``reason``, which carries the failure reason on
    EVERY entry — including the partially attempted current step, whose stored
    remainder still carries the generic historical phrase.
    """
    return [{**entry, "reason": reason} for entry in plan_remaining(lease, reason)]


def plan_next_step(lease: PlanLease) -> dict[str, Any] | None:
    """Describe the next unexecuted waypoint without leaking raw user text."""
    if lease.cursor >= len(lease.steps):
        return None
    point = lease.context["points"][lease.cursor]
    waypoint = lease.model.points[lease.cursor]
    safe_actions: list[dict[str, Any]] = []
    for spec in waypoint.actions:
        action = spec.model_dump(mode="json")
        if action.get("text"):
            action["text"] = redact_text(str(action["text"]))[0]
        if action.get("expected_effect"):
            action["expected_effect"] = redact_text(str(action["expected_effect"]))[0]
        safe_actions.append(action)
    return {
        "step_index": lease.cursor,
        "point": {
            "id": waypoint.id,
            "x": waypoint.x,
            "y": waypoint.y,
        },
        "actions": safe_actions,
        "action_types": [spec.action.value for spec in waypoint.actions],
        "point_status": point.get("status", "pending"),
    }


class PlanLeaseGuard:
    """Atomic install / idempotent release of a session's plan lease.

    The server keeps ONE guard. ``install`` is atomic under the guard's lock and
    fail-closed: a second lease on the same session (or a session id still marked
    in-flight) raises :class:`ValueError` and changes nothing. ``release`` is
    idempotent and is the ONE exit for every terminal/session-end path. Both operate
    on a session-bundle-like object passed in — the only protocol it needs is
    ``bundle.extra`` (a dict) and ``bundle.context.session_id`` (any str()-able id) —
    so this module never imports the server.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()

    def install(self, bundle: Any, lease: PlanLease, *, _race_probe: Any = None) -> None:
        """Install a validated lease atomically at the session boundary.

        ``_race_probe`` (TEST-ONLY, VAL-WP7a-01 #11): a callable invoked between the
        occupancy check and the set, so a test can hold a barrier inside the critical
        section — an unlocked implementation admits every racer, the locked one
        admits exactly one.
        """
        session_id = str(bundle.context.session_id)
        with self._lock:
            existing = bundle.extra.get("plan_lease")
            if existing is not None or session_id in self._in_flight:
                raise ValueError("a plan lease is already active on this session")
            if _race_probe is not None:
                _race_probe()
            bundle.extra["plan_lease"] = lease
            self._in_flight.add(session_id)

    def release(self, session_id: Any, bundle: Any) -> None:
        """Release a plan lease idempotently from every terminal/session exit path."""
        with self._lock:
            bundle.extra.pop("plan_lease", None)
            self._in_flight.discard(str(session_id))

    def is_active(self, session_id: Any) -> bool:
        """Whether a lease is currently in flight for this session id."""
        return str(session_id) in self._in_flight

    def active_lease(self, bundle: Any) -> PlanLease | None:
        """The bundle's installed lease, or ``None``."""
        lease = bundle.extra.get("plan_lease")
        return lease if isinstance(lease, PlanLease) else None


# --- the cursor rule (K-53) -------------------------------------------------------------------

#: The queue entry ``kind`` values that mean REAL input was dispatched. A pre-dispatch
#: refusal (rejected, safety_denied, approval_required, ...) dispatched nothing, so the
#: cursor MUST stay — otherwise a retried step would skip work that never ran.
DISPATCHED_ITEM_KINDS = frozenset({"executed", "dispatched_unverified"})


def plan_step_attempted(entries: list[dict[str, Any]]) -> bool:
    """Whether a handshake step sent at least one input (truthful cursor rule)."""
    return any(entry.get("kind") in DISPATCHED_ITEM_KINDS for entry in entries)


def plan_cursor_after(
    cursor: int, step_index: int, entries: list[dict[str, Any]]
) -> int:
    """The lease cursor after a step: advance iff >= 1 item of the step dispatched.

    K-53: a step that was refused BEFORE any dispatch (rejection, approval, safety)
    leaves the cursor on the same step, so ``continue`` re-executes it; a step that
    dispatched at least one item advances, even when its verification abstained.
    Pure: takes and returns the cursor, touches nothing else.
    """
    if plan_step_attempted(entries):
        return step_index + 1
    return cursor


# --- the terminal aggregate (master-mission §1.2/§1.3, V/4244 fix) ----------------------------


#: The closed terminal ``result`` token set for a plan.
TERMINAL_PLAN_RESULTS = ("verified", "unknown", "failed", "stopped")

#: The queue-entry ``result`` tokens that count as a verified step (the §1.1 ok rule:
#: verified, plus the intentional non-screen markers). Everything else — uncertain,
#: failed, rejected, not_executed, dry_run, or an absent token — never verifies.
VERIFIED_RESULT_TOKENS = frozenset({"verified", "marker"})

#: The queue-entry ``result`` tokens that FAIL a plan (§1.2: proven not achieved,
#: refused, or never dispatched; a dry_run dispatched nothing and is never verified).
FAILED_RESULT_TOKENS = frozenset({"failed", "rejected", "not_executed", "dry_run"})


class PlanTerminalOutcome(NamedTuple):
    """The terminal plan verdict: ``ok`` is TRUE iff ``result == "verified"``."""

    result: str
    ok: bool


def plan_result_from_entries(entries: list[dict[str, Any]]) -> str:
    """Translate internal queue outcomes to the host handshake vocabulary.

    FAIL-CLOSED (VAL-WP7a-01 #3): each entry is classified from its OWN unified
    ``result`` token (the §1.2 token ``agent._queue_entry`` stamps on every item,
    from the same derivation as the flat reply). Only ``"verified"`` and
    ``"marker"`` count as verified — an entry with an absent/unknown ``result``
    (or a missing verification outcome) is UNCERTAIN, never verified; a ``dry_run``
    entry dispatched nothing and is never verified; ``rejected``/``not_executed``
    and ``failed`` all fail the plan. When ``result`` is absent, the entry's
    ``verification_outcome`` is the honest fallback (``failed`` -> failed;
    everything else -> uncertain, including a ``dispatched_unverified`` item,
    which master-mission §1.2 defines as "dispatched, unproven").
    """
    if not entries:
        return "failed"

    def _entry_outcome(entry: dict[str, Any]) -> str:
        token = entry.get("result")
        if token in VERIFIED_RESULT_TOKENS:
            return "verified"
        if token in FAILED_RESULT_TOKENS:
            return "failed"
        if token is None:
            outcome = entry.get("verification_outcome")
            if outcome == "failed":
                return "failed"
            return "uncertain"
        return "uncertain"

    entry_outcomes = [_entry_outcome(entry) for entry in entries]
    if "failed" in entry_outcomes:
        return "failed"
    if "uncertain" in entry_outcomes:
        return "unknown"
    return "verified"


def plan_terminal_outcome(
    entries: list[dict[str, Any]],
    *,
    stopped: bool = False,
    expected_items: int,
) -> PlanTerminalOutcome:
    """The pure terminal aggregate (master-mission §1.2/§1.3).

    ``result`` is one of :data:`TERMINAL_PLAN_RESULTS`: ``"stopped"`` wins when the
    host requested the stop (a stopped plan verified nothing, whatever its prefix
    looked like); otherwise the retained prefix classifies as ``verified`` (every
    item verified or marker), ``unknown`` (at least one step uncertain or
    dispatched-unverified, none failed), or ``failed``.

    ``expected_items`` (VAL-WP7a-01 #4; REQUIRED — VAL-WP7a-02 #15) is the plan's
    declared item count (``context["normalized_item_count"]``). Every caller passes
    the plan's expected item count, so no caller can silently drop the completeness
    check: when the prefix holds FEWER entries than expected, a verified-so-far
    prefix is capped at ``unknown``: work the plan declared never produced evidence,
    so the plan's end state is an ABSENCE of evidence — ``unknown`` per §1.2
    ("dispatched, unproven" covers the unexecuted remainder's silence), never
    ``stopped`` (the host did not request a stop) and never ``failed`` (nothing was
    proven not achieved).

    ``ok = (result == "verified")`` BY CONSTRUCTION — the V/4244 fix: the terminal
    reply of a plan whose steps abstained is ``unknown`` with ``ok:false``, never the
    dev tree's ``ok = result != "failed"`` honesty hole that reported ``unknown``
    as success.
    """
    result = "stopped" if stopped else plan_result_from_entries(entries)
    if result == "verified" and len(entries) < int(expected_items):
        result = "unknown"
    return PlanTerminalOutcome(result=result, ok=result == "verified")


# --- B6–B8 failure classification (WP8) --------------------------------------------------------
#
# Every token below is a literal that ALREADY exists in the product vocabulary — the
# tables are keyed by the three pre-existing signals a step reply already carries (the
# typed response ``error``, the agent's outcome ``kind``, and the queue's
# ``follow_ups_stopped_reason``).  Nothing is invented: a code state that none of those
# names classifies as ``unclassified`` rather than growing a new reason.  The tables
# live HERE (not in the server) so plan.py stays import-free of server.py (the AST pin)
# and the classifier is pure data + one function over the signals the reply carries.
#
# Completeness is enforced by ``tests/test_batch8_classification_completeness.py``:
# it extracts every literal the product can put in ``SingleActionOutcome.kind``, in
# ``follow_ups_stopped_reason`` and in the step reply's ``error``, then fails if any
# of them is unnamed.  Adding a stop reason to the product without classifying it
# fails that test instead of silently shipping ``unclassified``.
_PLAN_ERROR_TO_FAILURE_REASON: dict[str, str] = {
    "limit_exceeded": "limit_exceeded",
    "action_error": "execution_error",
}
# F8 (VAL-WP8-01): the dev copy's ``digest_surprise`` error-table entry was
# dropped (a digest surprise is a REJECTION kind here — rejected + reasons, no
# error key), and the stable ``cancelled``/``invalid_action`` error keys are NOT
# table entries: the exception reply builder passes those failure_reason tokens
# EXPLICITLY and never consults the classifier, so the keys would be unreachable
# (``no_table_key_is_unreachable`` keeps the tables honest).
_PLAN_OUTCOME_KIND_TO_FAILURE_REASON: dict[str, str] = {
    "safety_denied": "safety_denied",
    "approval_required": "approval_required",
    "digest_surprise": "digest_surprise",
    "error": "execution_error",
    "rejected": "rejected",
}
# Stop reasons that NAME the cause win over the per-item verdict; stop reasons that
# only describe the MECHANISM ("the queue halted") must not mask it — a plan whose
# on_failure policy stopped the batch after a failed verification is a
# verification_failed step, not a policy_stop step.
_PLAN_SPECIFIC_STOP_REASON_TO_FAILURE_REASON: dict[str, str] = {
    "safety_denied": "safety_denied",
    "limit_exceeded": "limit_exceeded",
    "modal_dialog": "modal_dialog",
    "focus_drifted": "focus_drifted",
}
# F8 (VAL-WP8-01): the dev copy's ``"stopped": "cancelled"`` entry is gone —
# ``"stopped"`` is never a ``follow_ups_stopped_reason`` the queue can produce
# (a TaskStopped kill is a TERMINAL exception path that carries its failure_reason
# explicitly), so the key was unreachable.
_PLAN_STOP_REASON_TO_FAILURE_REASON: dict[str, str] = {
    "verification_failed": "verification_failed",
    "invalid_follow_up": "invalid_follow_up",
    "policy_stop": "policy_stop",
    "no_post_action_observation": "no_post_action_observation",
    # The queue sets this literal when a FOLLOW-UP item of the step raised
    # (``stopped_reason = outcome.kind`` for a non-``executed`` kind, and that kind
    # IS ``"error"``).  It is the same state the kind table already calls
    # ``execution_error``; the two places the literal appears now agree.
    "error": "execution_error",
    # The queue copies a non-executed ``outcome.kind`` verbatim into the stop reason
    # when the step's FIRST item succeeded, so the KIND table — keyed on that first
    # outcome — is never consulted.  Each maps to exactly the token the KIND table
    # already gives it, so the two places one literal appears agree.
    "rejected": "rejected",
    "approval_required": "approval_required",
    "digest_surprise": "digest_surprise",
    # Stable tree only (the queue's ensure-app guard): a follow-up ensure_app whose
    # TYPED outcome is a failure/ambiguous stops the batch.  The item dispatched and
    # produced no proof of a target, so — when the per-entry verdicts carry nothing —
    # the honest class is the same "dispatched, unproven" one §1.2 uses.
    "ensure_app_failed": "verification_uncertain",
    # The four NAMED interference stops beyond the two with their own tokens: the
    # focus events share the product's one focus-interference token with
    # ``focus_drifted``, and the remaining two are refusals.  The exact cause stays
    # verbatim in ``follow_ups_stopped_reason`` and in the entry's ``reasons``, so
    # ``failure_reason`` carries the CLASS only.
    "focus_taken_by": "focus_drifted",
    "focus_identity_unknown": "focus_drifted",
    "stuck_modifier": "rejected",
    "target_gone": "rejected",
}
# The complete, closed set the reply surface may report.  A strict consumer can
# validate against it; ``unclassified`` is the documented escalation fallback.
# NOT dead code: this closed vocabulary is read by the plan-reply truth tests
# (test_batch6/7/8) and the cortex-skill truth tests, which derive their taught
# vocabulary from it.
PLAN_FAILURE_REASONS = frozenset(
    {
        "verification_failed",
        "verification_uncertain",
        "execution_error",
        "limit_exceeded",
        "cancelled",
        "safety_denied",
        "approval_required",
        "digest_surprise",
        "rejected",
        "invalid_action",
        "invalid_follow_up",
        "policy_stop",
        "modal_dialog",
        "focus_drifted",
        "no_post_action_observation",
        "unclassified",
    }
)


def plan_failure_reason(
    outcome: Any,
    step_entries: list[dict[str, Any]],
    *,
    result: str,
    error: str | None = None,
) -> str | None:
    """Classify a non-verified step into ONE token of the closed vocabulary.

    ``None`` is the honest answer for a verified step (no failure happened).  The
    classification is a pure read of signals the reply already carries, so it can
    never disagree with ``result``, ``error``, or the retained prefix.  Adapted from
    the dev classifier to the unified reply contract: the per-entry verdicts also
    read the entries' own ``result`` token (the same derivation the flat reply and
    every ``follow_up_results[i]`` use), the entries alone classify a TERMINAL
    prefix (outcome=None), and an explicit queue stop reason outranks
    entry-derived uncertainty while entry-derived FAILURES keep theirs.
    """
    if result == "verified":
        return None
    if error is not None:
        classified = _PLAN_ERROR_TO_FAILURE_REASON.get(str(error))
        if classified is not None:
            return classified
    kind = getattr(outcome, "kind", None)
    if kind is not None:
        classified = _PLAN_OUTCOME_KIND_TO_FAILURE_REASON.get(str(kind))
        if classified == "rejected" and any(
            "invalid_action" in list(entry.get("reasons") or []) for entry in step_entries
        ):
            return "invalid_action"
        if classified is not None:
            return classified
    stopped = getattr(outcome, "follow_ups_stopped_reason", None)
    if stopped is not None:
        classified = _PLAN_SPECIFIC_STOP_REASON_TO_FAILURE_REASON.get(str(stopped))
        if classified is not None:
            return classified
    if any(entry.get("kind") == "safety_denied" for entry in step_entries):
        return "safety_denied"
    if any(entry.get("verification_outcome") == "failed" for entry in step_entries):
        return "verification_failed"
    # F7 (VAL-WP8-01): an EXPLICIT queue stop reason names the cause and must win
    # over entry-derived UNCERTAINTY — an uncertain first item followed by an
    # approval hold / rejection / named focus stop classifies as the STOP, not as
    # verification_uncertain.  (Entry-derived FAILURES keep their earlier
    # precedence: a policy stop over failed verifications stays
    # verification_failed.)
    if stopped is not None:
        classified = _PLAN_STOP_REASON_TO_FAILURE_REASON.get(str(stopped))
        if classified is not None:
            return classified
    # F1 (VAL-WP8-01): the terminal replies classify with outcome=None, so the
    # ENTRIES must carry the refusal facts on their own — the unified result
    # token and the approval marker are the same derivation the step surface
    # used, so the terminal reply can never disagree with its own prefix.
    if any(entry.get("requires_approval") for entry in step_entries):
        return "approval_required"
    if any(
        entry.get("result") == "rejected"
        or entry.get("kind") in ("rejected", "digest_surprise")
        for entry in step_entries
    ):
        return "rejected"
    if any(entry.get("result") == "failed" for entry in step_entries):
        # a failed entry with NO failed verification outcome is an operation
        # error (kind "error"); the verification-failed case returned above
        return "execution_error"
    if any(entry.get("result") == "not_executed" for entry in step_entries):
        # N3 (VAL-WP8-02/03): a not_executed step WITHOUT an approval hold (that
        # case returned above) never dispatched and was never refused — the cut
        # token "cancelled" is the honest class.
        return "cancelled"
    if any(entry.get("result") == "dry_run" for entry in step_entries):
        # VAL-WP8-03 (Commander decision): a dry_run step was VALIDATED and
        # DELIBERATELY not dispatched — a cut, not a cancellation. The step
        # level already gives a dry-run step "no_post_action_observation" (no
        # post-action observation can exist for a dispatch that never ran), so
        # the terminal aggregate uses the SAME existing token.
        return "no_post_action_observation"
    if any(
        entry.get("verification_outcome") == "uncertain"
        or entry.get("result") == "uncertain"
        or entry.get("kind") == "dispatched_unverified"
        for entry in step_entries
    ):
        return "verification_uncertain"
    if result in ("uncertain", "unknown"):
        return "verification_uncertain"
    return "unclassified"


# --- S-10: the advertised wire schema ---------------------------------------------------------

#: The ONE hand-written advertised schema for the ``plan`` tool parameter (S-10).
#:
#: A typed OBJECT: ``"type": ["object", "null"]`` with the full nested shape spelled
#: out INLINE — no ``anyOf``, no ``$ref``/``$defs`` (the REM-G plain-schema rule), and
#: deliberately NO ``"string"`` arm (the object|string union S-10 deletes). The
#: JSON-string runtime tolerance lives in :func:`coerce_tolerant_plan`, behind the
#: validation boundary, never in the advertised shape. Bounds mirror the pydantic
#: models and are pinned against them by tests (drift fails the suite, not a host).
PLAN_PARAM_PLAIN_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "properties": {
        "points": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_PLAN_POINTS,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "x": {"type": "integer", "minimum": _COORD_MIN, "maximum": _COORD_MAX},
                    "y": {"type": "integer", "minimum": _COORD_MIN, "maximum": _COORD_MAX},
                    "actions": {
                        "type": "array",
                        "maxItems": MAX_PLAN_ACTIONS_PER_POINT,
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": [member.value for member in ActionType],
                                },
                                "x": {"type": "integer", "minimum": _COORD_MIN, "maximum": _COORD_MAX},
                                "y": {"type": "integer", "minimum": _COORD_MIN, "maximum": _COORD_MAX},
                                "x2": {"type": "integer", "minimum": _COORD_MIN, "maximum": _COORD_MAX},
                                "y2": {"type": "integer", "minimum": _COORD_MIN, "maximum": _COORD_MAX},
                                "text": {
                                    "type": "string",
                                    "maxLength": _max_length(ActionSpec.model_fields["text"]),
                                },
                                "keys": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "maxItems": _max_length(ActionSpec.model_fields["keys"]),
                                },
                                "delta": {
                                    "type": "integer",
                                    "minimum": _numeric_bounds(ActionSpec.model_fields["delta"])[0],
                                    "maximum": _numeric_bounds(ActionSpec.model_fields["delta"])[1],
                                },
                                "target": {
                                    "type": "string",
                                    "maxLength": _max_length(ActionSpec.model_fields["target"]),
                                },
                                "expected_effect": {
                                    "type": "string",
                                    "maxLength": _max_length(ActionSpec.model_fields["expected_effect"]),
                                },
                                "via": {
                                    "type": "string",
                                    "maxLength": _max_length(ActionSpec.model_fields["via"]),
                                },
                                "hold_ms": {
                                    "type": "integer", "minimum": 0, "maximum": MAX_HOLD_MS,
                                },
                                "duration_ms": {
                                    "type": "integer", "minimum": 0, "maximum": MAX_DURATION_MS,
                                },
                            },
                            "required": ["action"],
                            "additionalProperties": False,
                        },
                    },
                    "arrival_condition": {
                        "type": "string",
                        "maxLength": _max_length(Waypoint.model_fields["arrival_condition"]),
                    },
                    "timeout_ms": {
                        "type": "integer", "minimum": 0, "maximum": MAX_PLAN_TIMEOUT_MS,
                    },
                    "on_failure": {"type": "string", "enum": ["stop", "continue"]},
                    "on_uncertain": {"type": "string", "enum": ["stop", "continue"]},
                    "duration_ms": {
                        "type": "integer", "minimum": 0, "maximum": MAX_DURATION_MS,
                    },
                    "settle_ms": {
                        "type": "integer", "minimum": 0, "maximum": MAX_PLAN_SETTLE_MS,
                    },
                },
                "required": ["x", "y"],
                "additionalProperties": False,
            },
        },
        "on_failure": {"type": "string", "enum": ["stop", "continue"]},
        "on_uncertain": {"type": "string", "enum": ["stop", "continue"]},
        "point_timeout_ms": {
            "type": "integer", "minimum": 0, "maximum": MAX_PLAN_TIMEOUT_MS,
        },
        "transition_duration_ms": {
            "type": "integer", "minimum": 0, "maximum": MAX_DURATION_MS,
        },
        "final_wait_ms": {"type": "integer", "minimum": 0, "maximum": MAX_DURATION_MS},
        "settle_ms": {"type": "integer", "minimum": 0, "maximum": MAX_PLAN_SETTLE_MS},
    },
    "required": ["points"],
    "additionalProperties": False,
}
