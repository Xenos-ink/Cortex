"""SPEC 5.2 plan-explicitness helper (ORVEX-2026-102 Phase 5).

A submitted ``plan`` must now STATE every timing, verification and uncertainty
decision, because ORVEX-2026-102 §5.1 removed the hidden defaults outside a plan
(0 ms move, 0 ms settle) and §5.2 made a plan the EXPLICIT-TIMING mode: there is
no hidden default left for a sequence of steps to inherit.

The restatement obligation of MASTER MISSION §5.4.1 / risk R4 is therefore
mechanical for the test tree: every fixture that submits a plan through the WIRE
(which routes through ``computer_use_mcp.plan.coerce_tolerant_plan``) now declares
the eight required details.  This module states that requirement ONCE so each test
file's restatement is one readable call rather than eight repeated keys.

It lives in its own top-level module, not in ``conftest.py``, for the same reason
``recording_engine`` does: this tree is imported root-less and ``conftest`` is
ambiguous between ``tests/`` and ``tests/e2e/``.

The function is deliberately INVISIBLE-BY-DEFAULT for the values it fills in: it
only ADDS keys the payload does not already state, so a test that means to pin a
specific value (``on_uncertain: "stop"``, ``settle_ms: 42``) keeps it, and a test
that means to pin the crossing-point shape keeps its explicit ``"actions": []``.
"""

from __future__ import annotations

from typing import Any

#: The plan-level defaults every restated fixture gets.  Each is a NEUTRAL, honest
#: statement, not a value chosen to make a test pass: transitions jump instantly
#: (0), nothing settles (0), a point gets a 5 s cooperative timeout, a resolved
#: failure halts, and an uncertainty is passed over but still REPORTED as uncertain.
DEFAULT_PLAN_DECLARATIONS: dict[str, Any] = {
    "transition_duration_ms": 0,
    "point_timeout_ms": 5_000,
    "settle_ms": 0,
    "on_failure": "stop",
    "on_uncertain": "continue",
}

#: The per-point subset of the above: only keys ``Waypoint`` itself accepts, because
#: both plan models are ``extra="forbid"`` and a plan-level name on a point is a
#: structural error, not a declaration.
POINT_DECLARATIONS: dict[str, Any] = {
    "duration_ms": 0,
    "settle_ms": 0,
    "on_failure": "stop",
    "on_uncertain": "continue",
}

#: What a point states when it carries an input action and the test has no opinion
#: about what the click should produce.  A restated fixture that genuinely cares
#: about verification passes its own string; this default only satisfies the
#: "declare what you verify" rule so the restated test keeps testing what it was
#: written to test.
DEFAULT_ARRIVAL_CONDITION = "the point's action took effect"


def complete_plan(plan: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Return ``plan`` with the SPEC 5.2 required-in-a-plan details stated.

    SPEC 5.2 (ORVEX-2026-102 §5.2): a submitted plan must state — the ordered
    points, the actions at each point, the movement timing, the wait times, the
    settle wanted, what is verified after each step, the per-step timeout, and both
    ``on_failure`` and ``on_uncertain``.  Anything the payload already states is
    left alone, so this never overrides a test's intent.  ``overrides`` replace the
    plan-level defaults (e.g. ``complete_plan(p, on_uncertain="stop")``).

    Precedence, mirroring the product's own ``point ?? plan`` inheritance: a value
    the PLAN states is inherited by every point that does not state its own, so this
    helper only injects a per-point value when NEITHER the point nor the plan declared
    it.  Injecting one anyway would silently outrank the plan's own policy — the exact
    bug this ordering exists to prevent.
    """
    plan_declarations = {**DEFAULT_PLAN_DECLARATIONS, **overrides}
    # what the plan (or an explicit override) ends up declaring
    stated_at_plan = {k: v for k, v in plan_declarations.items() if k not in plan}
    point_declarations = {
        key: stated_at_plan[key]
        for key in POINT_DECLARATIONS
        if key in stated_at_plan
    }
    completed: dict[str, Any] = dict(stated_at_plan)
    completed.update(plan)
    points: list[Any] = []
    for point in plan.get("points", []):
        if not isinstance(point, dict):
            points.append(point)
            continue
        filled = {key: value for key, value in point_declarations.items() if key not in point}
        # rule 2: the actions at each point are declared, never absent
        filled.setdefault("actions", [])
        # rule 6: a point that carries an input action declares what it verifies.  An
        # ACTION-level ``expected_effect`` already covers the whole point (the product's
        # own rule), so nothing is added when any action declares one — otherwise this
        # helper would blanket every action with the point's condition and change the
        # verdicts the restated tests exist to observe.
        declares = any(
            isinstance(spec, dict) and isinstance(spec.get("expected_effect"), str)
            and spec.get("expected_effect").strip() != ""
            for spec in point.get("actions") or []
        )
        if point.get("actions") and not declares and not point.get("arrival_condition"):
            filled.setdefault("arrival_condition", DEFAULT_ARRIVAL_CONDITION)
        points.append({**filled, **point})
    if "points" in completed:
        completed["points"] = points
    return completed
