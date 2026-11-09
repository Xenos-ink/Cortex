"""WP-T (ORVEX-2026-109 Addendum 1 "T"), DELTA 2: every example the skill ships
is validated with the SAME code the server runs.

The field session wasted turns because the driver did not know when to use
which tool; the fix (SKILL.md's which-tool table, references/tool-cards.md,
references/recipes.md, and ``Example:`` lines in the tool descriptions) must
never drift into fiction. This suite parses EVERY example — every ```json
block in the five skill files (each must carry its ``<!-- tool: NAME -->``
tag; an untagged fence is a failure) and every ``Example: {...}`` line in the
live MCP tool descriptions — and validates each one against the code's REAL
surface:

- the tool name must be a registered tool (live ``mcp.list_tools()``);
- every top-level key must exist in the tool's advertised inputSchema
  properties (the server rejects unknown arguments, server/instance.py);
- the whole argument object walks the advertised schema (types, enums,
  min/max, minLength/maxLength, minItems/maxItems, additionalProperties);
- required parameters present (placeholders ``"S"``/``"W"``/``"R"``/``"I"``
  stand for the real session/wait/record/image ids);
- ``computer_execute`` examples run the server's own validation paths:
  the flat action through ``GroundedAction`` exactly as execute.py builds it;
  every ``follow_ups`` item through ``ActionSpec.model_validate(
  _coerce_follow_up_entry(item)).to_grounded()`` (item keys additionally
  checked against the ActionSpec field set — pydantic would silently ignore
  an unknown key); ``plan`` through ``plan_layer.coerce_tolerant_plan`` ->
  ``normalize_plan`` -> ``to_grounded`` (WaypointPlan is extra="forbid");
  a ``record`` object through ``record._validate_record_arguments``;
  wait deltas capped by ``WAIT_MAX_SECONDS``; a plan sent with any flat
  parameter (or a non-``done`` action) fails exactly per execute.py:568-581;
  an unknown ``plan_action`` fails;
- ``computer_record`` examples through the same ``_validate_record_arguments``;
- ``computer_zoom`` examples REQUIRE a region, shape-checked with
  ``observe._zoom_region_shape_error``;
- ``computer_wait`` durations checked against the real ceiling
  ``min(Limits().max_wait_seconds, WAIT_HARD_MAX_SECONDS)``;
- closed string values with no schema enum are checked against the code's
  own value sets: ``image_delivery`` (server.state), ``visual_view``
  (observe._resolve_view_param), ``visual_view_density`` (visual_views.
  resolve_density / GRID_DENSITIES), ``plan_action`` (plan_surface's own
  set), ``video_delivery`` (record._valid_video_delivery);
- ``start_session`` map contents run through the server's own parsers
  (``server.state._parse_limits``, ``interference.parse_interference``) and
  the ``max_steps``/``max_actions`` bands come from ``STEPS_BAND`` /
  ``ACTIONS_BAND``; no start_session example may trigger the caps-differ
  warning (lifecycle.py:942,970).

Fence detection is INDEPENDENT of the json-block regex: every opening fence
matching ``^```(\\S*)`` is counted; only ``json`` and ``text`` info strings
are allowed — a bare fence (or ``JSON``/``jsonc``/anything else) FAILS — and
the number of validated blocks must equal the number of ```json fences found
by that scan.

Nothing here is hard-coded that the code defines. The negative self-tests run
whole dishonest examples through ``_validate_example`` (or the fence
scanner) itself, and each must FAIL.

The description-growth test pins the WP-T budget: the shipped example lines
may grow the 12 tool descriptions by less than 2,500 characters over the
pre-WP-T baseline (descriptions ride every model turn).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from pathlib import Path
from typing import Any

import pytest

from computer_use_mcp.core.limits import (
    ACTIONS_BAND,
    STEPS_BAND,
    Limits,
)
from computer_use_mcp.core.models import MAX_FOLLOW_UPS, ActionSpec, ActionType, GroundedAction
from computer_use_mcp.desktop.interference import parse_interference
from computer_use_mcp.desktop.motion import WAIT_MAX_SECONDS
from computer_use_mcp.planning import plan as plan_layer
from computer_use_mcp.server import lifecycle as server_lifecycle
from computer_use_mcp.server import observe as server_observe
from computer_use_mcp.server import plan_surface as server_plan_surface
from computer_use_mcp.server import record as server_record
from computer_use_mcp.server import state as server_state
from computer_use_mcp.server.app import mcp
from computer_use_mcp.server.schemas import _coerce_follow_up_entry
from computer_use_mcp.server.wait_machine import WAIT_HARD_MAX_SECONDS
from computer_use_mcp.vision.visual_views import (
    GRID_DENSITIES,
    InvalidVisualViewError,
    resolve_density,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "cortex-skill"
EXAMPLE_FILES = (
    SKILL_DIR / "SKILL.md",
    SKILL_DIR / "references" / "tool-cards.md",
    SKILL_DIR / "references" / "recipes.md",
    SKILL_DIR / "references" / "plan-protocol.md",
    SKILL_DIR / "references" / "examples.md",
)

#: The example-tag convention (documented at the top of tool-cards.md and
#: recipes.md): every ```json block is preceded by ``<!-- tool: NAME -->``.
_JSON_BLOCK_RE = re.compile(r"```json\n(.*?)```", re.S)
_TOOL_TAG_RE = re.compile(r"<!--\s*tool:\s*([a-z][a-z_]+)\s*-->")
_EXAMPLE_LINE_RE = re.compile(r"Example[^:]*:\s*(\{.*$)")
#: N3(f): the INDEPENDENT fence scanner — every opening fence, any info string.
_FENCE_LINE_RE = re.compile(r"^```(\S*)[ \t]*$")

ACTION_VALUES = frozenset(action.value for action in ActionType)

#: The total description length of all 12 registered tools BEFORE WP-T added
#: any Example lines — measured from the live ``mcp.list_tools()`` at the
#: WP-T baseline commit (6ff300f, the WP-E tree) with a one-off listing of
#: ``tool.description`` lengths; the Commander deleted that helper, so the
#: number is pinned here as the growth baseline.
PRE_WP_T_TOTAL_DESC_CHARS = 47_490
#: The WP-T budget from the mission brief: descriptions are sent on every
#: model turn, so the shipped examples may grow them by less than this.
DESCRIPTION_GROWTH_BUDGET_CHARS = 2_500


def _field_bound(model: Any, name: str) -> tuple[int, int]:
    ge = le = None
    for constraint in model.model_fields[name].metadata:
        ge = getattr(constraint, "ge", ge)
        le = getattr(constraint, "le", le)
    assert ge is not None and le is not None, (model.__name__, name)
    return int(ge), int(le)


def _plan_action_values() -> frozenset[str]:
    """The plan_action value set, read from plan_surface's own check."""
    source = inspect.getsource(server_plan_surface)
    match = re.search(r"plan_action not in \{([^}]+)\}", source)
    assert match is not None, "plan_surface's plan_action value set moved"
    return frozenset(
        token.strip().strip("'\"") for token in match.group(1).split(",")
    )


_PLAN_ACTION_VALUES = _plan_action_values()


@pytest.fixture(scope="module")
def tools() -> dict[str, Any]:
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


# --- example extraction ------------------------------------------------------------------------


def _balanced_json(text: str, start: int) -> str:
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unbalanced JSON braces in example: {text[start:start + 80]!r}")


def _scan_fences(text: str, source: str) -> int:
    """Count the opening ```json fences; FAIL on any opening fence whose info
    string is not ``json`` or ``text`` (a bare fence hides its example from
    validation). Returns the json-fence count, independently of the block
    regex."""
    inside = False
    json_openings = 0
    for line_number, line in enumerate(text.splitlines(), 1):
        match = _FENCE_LINE_RE.match(line)
        if match is None:
            continue
        if inside:
            inside = False  # the closing fence
            continue
        info = match.group(1)
        if info not in ("json", "text"):
            raise AssertionError(
                f"{source}:{line_number}: opening fence info {info!r} is not "
                "allowed — only ```json and ```text; a bare or foreign fence "
                "(JSON, jsonc, ...) hides its example from validation"
            )
        if info == "json":
            json_openings += 1
        inside = True
    assert not inside, f"{source}: an opening fence is never closed"
    return json_openings


def _tagged_json_blocks(text: str, source: str) -> list[tuple[str, str]]:
    """(tool_name, json_text) for every ```json block; an untagged fence FAILS."""
    blocks: list[tuple[str, str]] = []
    last_fence_end = 0
    for match in _JSON_BLOCK_RE.finditer(text):
        window = text[last_fence_end : match.start()]
        tags = list(_TOOL_TAG_RE.finditer(window))
        assert tags, (
            f"{source}: a ```json block at offset {match.start()} carries no "
            "preceding <!-- tool: NAME --> tag; every example block must be tagged"
        )
        blocks.append((tags[-1].group(1), match.group(1)))
        last_fence_end = match.end()
    return blocks


def _description_examples(description: str) -> list[str]:
    examples: list[str] = []
    for line in description.splitlines():
        match = _EXAMPLE_LINE_RE.search(line)
        if match is None:
            continue
        examples.append(_balanced_json(line, match.start(1)))
    return examples


# --- the advertised-schema walker ---------------------------------------------------------------


def _type_admits(schema_type: Any, value: Any) -> bool:
    types = list(schema_type) if isinstance(schema_type, list) else [schema_type]
    if value is None:
        return "null" in types
    if isinstance(value, bool):
        return "boolean" in types
    if isinstance(value, int):
        return "integer" in types or "number" in types
    if isinstance(value, float):
        return "number" in types or "integer" in types
    if isinstance(value, str):
        return "string" in types
    if isinstance(value, list):
        return "array" in types
    if isinstance(value, dict):
        return "object" in types
    return False


def _validate_value(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    if not isinstance(schema, dict):
        return
    declared = schema.get("type")
    if declared is not None and not _type_admits(declared, value):
        errors.append(f"{path}: {value!r} does not match declared type {declared}")
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not in the enum {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} is below the minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} is above the maximum {schema['maximum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength {schema['maxLength']}")
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        additional = schema.get("additionalProperties", True)
        for key, sub in value.items():
            if key in properties:
                _validate_value(sub, properties[key], f"{path}.{key}", errors)
            elif additional is False:
                errors.append(
                    f"{path}: unknown parameter {key!r} (allowed: {sorted(properties)})"
                )
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems {schema['maxItems']}")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _validate_value(item, items, f"{path}[{index}]", errors)


# --- the server-path validators for computer_execute's nested surfaces --------------------------


def _validate_follow_ups_item(item: Any, path: str, errors: list[str]) -> None:
    """Exactly the server's queue-item path (execute.py): ActionSpec.model_validate
    on the coerced entry, then spec.to_grounded(). An unknown item key is checked
    against the ActionSpec FIELD SET (pydantic would silently ignore it)."""
    if not isinstance(item, dict):
        errors.append(f"{path}: a follow_ups item must be an object")
        return
    unknown = set(item) - set(ActionSpec.model_fields)
    if unknown:
        errors.append(f"{path}: unknown follow_ups item key(s) {sorted(unknown)}")
    try:
        spec = ActionSpec.model_validate(_coerce_follow_up_entry(item))
    except Exception as exc:  # noqa: BLE001 - the server rejects via _teaching_invalid_action
        errors.append(f"{path}: fails ActionSpec validation: {exc}")
        return
    try:
        spec.to_grounded()
    except Exception as exc:  # noqa: BLE001 - R-18: grounded-shape failure
        errors.append(f"{path}: fails GroundedAction validation: {exc}")
    if spec.action.value == "wait" and spec.delta > WAIT_MAX_SECONDS:
        errors.append(
            f"{path}: a queued wait delta {spec.delta} exceeds the "
            f"{WAIT_MAX_SECONDS} s cap (the server clamps and reports waited_seconds)"
        )


def _validate_plan(plan_value: Any, errors: list[str]) -> None:
    """Exactly the server's plan path: coerce_tolerant_plan (WaypointPlan is
    extra="forbid" + model defense + explicitness) -> normalize_plan ->
    to_grounded per spec."""
    try:
        plan_model = plan_layer.coerce_tolerant_plan(plan_value)
    except Exception as exc:  # noqa: BLE001 - the typed plan rejections
        errors.append(f"plan fails coerce_tolerant_plan validation: {exc}")
        return
    try:
        specs, _policies = plan_layer.normalize_plan(plan_model)
    except Exception as exc:  # noqa: BLE001 - the model-defense rejections
        errors.append(f"plan fails normalize_plan validation: {exc}")
        return
    for index, spec in enumerate(specs):
        try:
            spec.to_grounded()
        except Exception as exc:  # noqa: BLE001 - R-18 grounded-shape failure
            errors.append(f"plan.items[{index}] fails GroundedAction validation: {exc}")


def _validate_record_arguments(
    record_value: Any, path: str, errors: list[str]
) -> None:
    """Exactly record.py's own argument validation (shared by computer_record
    and the execute ``record`` object)."""
    if not isinstance(record_value, dict):
        errors.append(f"{path}: a record object is required")
        return
    code, message, _resolved = server_record._validate_record_arguments(
        record_value.get("duration_s"),
        record_value.get("fps"),
        record_value.get("max_keyframes"),
        record_value.get("include_video"),
        record_value.get("save"),
        record_value.get("video_delivery"),
        record_value.get("region"),
    )
    if code is not None:
        errors.append(f"{path}: the server's record validation rejects it ({code}: {message})")


# --- the per-example validator (schema + the server paths) --------------------------------------


def _validate_example(tool: Any, args: Any, tool_name: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(args, dict):
        return [f"{tool_name}: an example must be an argument object"]
    schema = tool.inputSchema
    properties = schema.get("properties") or {}
    for key in args:
        if key not in properties:
            errors.append(
                f"{tool_name}: unknown parameter {key!r} (the server rejects "
                f"unknown arguments; allowed: {sorted(properties)})"
            )
    missing = set(schema.get("required") or []) - set(args)
    if missing:
        errors.append(f"{tool_name}: missing required parameters {sorted(missing)}")
    _validate_value(args, schema, tool_name, errors)

    if tool_name == "start_session":
        _validate_start_session(args, errors)
    elif tool_name == "computer_execute":
        _validate_execute_example(args, errors)
    elif tool_name == "computer_record":
        _validate_record_arguments(args, tool_name, errors)
        if args.get("video_delivery") is not None and not server_record._valid_video_delivery(
            args["video_delivery"]
        ):
            errors.append(
                f"video_delivery {args['video_delivery']!r} fails record."
                "_valid_video_delivery (must be \"file\" or \"inline\")"
            )
    elif tool_name == "computer_record_result":
        if args.get("video_delivery") is not None and not server_record._valid_video_delivery(
            args["video_delivery"]
        ):
            errors.append(
                f"video_delivery {args['video_delivery']!r} fails record."
                "_valid_video_delivery (must be \"file\" or \"inline\")"
            )
    elif tool_name == "computer_zoom":
        if "region" not in args or args.get("region") is None:
            errors.append("computer_zoom examples must carry a region")
        else:
            shape_error = server_observe._zoom_region_shape_error(args["region"])
            if shape_error is not None:
                errors.append(f"computer_zoom.region: {shape_error}")
    elif tool_name == "computer_wait":
        duration = args.get("duration_seconds")
        ceiling = min(Limits().max_wait_seconds, WAIT_HARD_MAX_SECONDS)
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            errors.append(f"computer_wait.duration_seconds must be a number, got {duration!r}")
        elif not 0 < duration <= ceiling:
            errors.append(f"computer_wait.duration_seconds {duration} outside 0..{ceiling}")

    # closed string values with no schema enum, checked against the code's own sets
    _validate_closed_values(tool_name, args, errors)
    return errors


def _validate_closed_values(tool_name: str, args: dict[str, Any], errors: list[str]) -> None:
    if tool_name == "start_session" and args.get("image_delivery") is not None:
        try:
            server_state._normalize_image_delivery_param(args["image_delivery"])
        except Exception as exc:  # noqa: BLE001 - the server's invalid_image_delivery path
            errors.append(f"image_delivery fails the server's check: {exc}")
    if tool_name in {"computer_observe", "computer_screenshot", "computer_zoom", "computer_execute"}:
        view = args.get("visual_view")
        if view is not None:
            try:
                server_observe._resolve_view_param(view)
            except InvalidVisualViewError as exc:
                errors.append(f"visual_view fails the server's check: {exc}")
            except Exception as exc:  # noqa: BLE001 - any typed view rejection
                errors.append(f"visual_view fails the server's check: {exc}")
        density = args.get("visual_view_density")
        if density is not None and args.get("visual_view") == "grid":
            if density not in GRID_DENSITIES:
                errors.append(
                    f"visual_view_density {density!r} is not in GRID_DENSITIES "
                    f"(accepted: {sorted(GRID_DENSITIES)})"
                )


def _validate_start_session(args: dict[str, Any], errors: list[str]) -> None:
    """The server's own parsers for the map-valued parameters, plus the
    max_steps / max_actions bands from the code constants."""
    limits = args.get("limits")
    if limits is not None:
        try:
            server_state._parse_limits(limits)
        except Exception as exc:  # noqa: BLE001 - the server's limits rejection
            errors.append(f"limits fail the server's _parse_limits: {exc}")
        max_actions = limits.get("max_actions")
        if max_actions is not None and not ACTIONS_BAND[0] <= max_actions <= ACTIONS_BAND[1]:
            errors.append(f"limits.max_actions {max_actions} outside {ACTIONS_BAND}")
    interference = args.get("interference")
    if interference is not None:
        try:
            parse_interference(interference)
        except Exception as exc:  # noqa: BLE001 - the server's invalid_interference path
            errors.append(f"interference fails the server's parser: {exc}")
    max_steps = args.get("max_steps")
    if max_steps is not None and not STEPS_BAND[0] <= max_steps <= STEPS_BAND[1]:
        errors.append(f"max_steps {max_steps} outside {STEPS_BAND}")


def _validate_execute_example(args: dict[str, Any], errors: list[str]) -> None:
    plan = args.get("plan")
    plan_action = args.get("plan_action")
    if plan_action is not None and plan_action not in _PLAN_ACTION_VALUES:
        errors.append(
            f"plan_action {plan_action!r} is not one of {sorted(_PLAN_ACTION_VALUES)}"
        )
    follow_ups = args.get("follow_ups")
    if follow_ups is not None:
        if not isinstance(follow_ups, list):
            errors.append("follow_ups must be a list")
        else:
            if len(follow_ups) > MAX_FOLLOW_UPS:
                errors.append(f"follow_ups longer than {MAX_FOLLOW_UPS}")
            for index, item in enumerate(follow_ups):
                _validate_follow_ups_item(item, f"follow_ups[{index}]", errors)
    if isinstance(plan, dict):
        # execute.py:568-581: a plan call leaves EVERY flat parameter at its
        # default (action="done", no x/y/text/keys/...).
        offending: list[str] = []
        if args.get("action") != "done":
            offending.append("action")
        for name in ("x", "y", "text", "expected_effect", "x2", "y2", "target", "via", "follow_ups"):
            if args.get(name) is not None:
                offending.append(name)
        if args.get("keys"):
            offending.append("keys")
        if args.get("delta", 0) != 0:
            offending.append("delta")
        if offending:
            errors.append(
                "a plan must be sent ALONE (execute.py:568-581): these flat "
                f"parameters are set: {sorted(offending)}"
            )
        _validate_plan(plan, errors)
    elif plan_action in (None, "replace") and "action" in args:
        # the FLAT action, built exactly as execute.py builds it
        try:
            GroundedAction(
                action=args["action"],  # type: ignore[arg-type]
                point=(
                    None
                    if args.get("x") is None or args.get("y") is None
                    else {"x": args["x"], "y": args["y"]}
                ),
                to_point=(
                    None
                    if args.get("x2") is None or args.get("y2") is None
                    else {"x": args["x2"], "y": args["y2"]}
                ),
                text=args.get("text"),
                keys=args.get("keys") or [],
                delta=args.get("delta", 0),
                reason="example validation",
                confidence=1.0,
                expected_effect=args.get("expected_effect"),
                target=args.get("target"),
                via=args.get("via"),
            )
        except Exception as exc:  # noqa: BLE001 - _teaching_invalid_action path
            errors.append(f"flat action fails the server's GroundedAction validation: {exc}")
        if args.get("action") == "wait" and args.get("delta", 0) > WAIT_MAX_SECONDS:
            errors.append(
                f"a flat wait delta {args['delta']} exceeds the "
                f"{WAIT_MAX_SECONDS} s cap (use the computer_wait tool)"
            )
    record = args.get("record")
    if record is not None:
        _validate_record_arguments(record, "record", errors)


# --- the tests ----------------------------------------------------------------------------------


def test_every_marked_json_block_is_schema_honest(tools: dict[str, Any]) -> None:
    checked = 0
    json_fences = 0
    for path in EXAMPLE_FILES:
        text = path.read_text(encoding="utf-8")
        json_fences += _scan_fences(text, path.name)
        for tool_name, raw in _tagged_json_blocks(text, path.name):
            assert tool_name in tools, (
                f"{path.name}: example tagged with unregistered tool {tool_name!r}"
            )
            args = json.loads(raw)
            errors = _validate_example(tools[tool_name], args, tool_name)
            assert not errors, f"{path.name}: example for {tool_name} is not honest: {errors}"
            checked += 1
    assert checked == json_fences, (
        f"checked {checked} json blocks but the independent fence scan found "
        f"{json_fences} opening ```json fences in the five skill files — a "
        "fence was skipped"
    )
    assert checked >= 12, f"only {checked} tagged example blocks found — the files shrank?"


def test_every_description_example_line_is_schema_honest(tools: dict[str, Any]) -> None:
    per_tool: dict[str, int] = {}
    for tool_name, tool in tools.items():
        examples = _description_examples(tool.description or "")
        for raw in examples:
            args = json.loads(raw)
            errors = _validate_example(tool, args, tool_name)
            assert not errors, (
                f"{tool_name}: the description's example {raw!r} is not honest: {errors}"
            )
        per_tool[tool_name] = len(examples)
    missing = sorted(name for name, count in per_tool.items() if count == 0)
    assert not missing, f"tools without an Example line: {missing}"
    assert per_tool["computer_execute"] <= 3, (
        "computer_execute's description carries more than 3 example lines — "
        "descriptions ride every turn; keep them small"
    )


def test_no_start_session_example_triggers_the_caps_differ_warning(
    tools: dict[str, Any],
) -> None:
    """lifecycle.py:942,970: the caps-differ warning fires when max_steps and
    max_actions differ; a copied example must never start a session with a
    surprise effective cap. Defaults are read from the code."""
    default_steps = inspect.signature(server_lifecycle.start_session).parameters[
        "max_steps"
    ].default
    default_actions = Limits().max_actions
    examples: list[tuple[str, dict[str, Any]]] = []
    for path in EXAMPLE_FILES:
        for tool_name, raw in _tagged_json_blocks(path.read_text(encoding="utf-8"), path.name):
            if tool_name == "start_session":
                examples.append((path.name, json.loads(raw)))
    for raw in _description_examples(tools["start_session"].description or ""):
        examples.append(("start_session description", json.loads(raw)))
    assert examples, "no start_session examples found — the files shrank?"
    for source, args in examples:
        max_steps = args.get("max_steps", default_steps)
        max_actions = (args.get("limits") or {}).get("max_actions", default_actions)
        assert max_steps == max_actions, (
            f"{source}: the start_session example {args!r} would start with "
            f"max_steps={max_steps} vs max_actions={max_actions} — the "
            "caps-differ warning fires and the session is capped at "
            f"{min(max_steps, max_actions)}"
        )


def test_description_growth_stays_under_the_wp_t_budget(tools: dict[str, Any]) -> None:
    total = sum(len(tool.description or "") for tool in tools.values())
    assert len(tools) == 12
    # the measured total, so the notes can quote it from this run
    print(f"TOTAL_DESC_CHARS={total}")
    assert total >= PRE_WP_T_TOTAL_DESC_CHARS, (
        f"the descriptions shrank below the pre-WP-T baseline "
        f"({total} < {PRE_WP_T_TOTAL_DESC_CHARS}); re-pin the baseline"
    )
    growth = total - PRE_WP_T_TOTAL_DESC_CHARS
    assert growth < DESCRIPTION_GROWTH_BUDGET_CHARS, (
        f"the WP-T example lines grew the descriptions by {growth} chars — "
        f"over the {DESCRIPTION_GROWTH_BUDGET_CHARS}-char budget"
    )


def test_every_tool_has_a_card_with_all_six_headings(tools: dict[str, Any]) -> None:
    text = (SKILL_DIR / "references" / "tool-cards.md").read_text(encoding="utf-8")
    for name in tools:
        match = re.search(rf"^## {re.escape(name)}\s*$", text, re.M)
        assert match is not None, f"tool-cards.md has no '## {name}' card"
        rest = text[match.end() :]
        nxt = re.search(r"^## ", rest, re.M)
        section = rest[: nxt.start()] if nxt else rest
        for heading in (
            "**Use when**",
            "**Don't use when**",
            "**Minimal call**",
            "**Common variants**",
            "**Read the reply**",
            "**Common mistake**",
        ):
            assert heading in section, f"the '## {name}' card lacks the {heading} heading"


def test_the_which_tool_table_names_only_real_tools(tools: dict[str, Any]) -> None:
    core = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    start = core.index("## The twelve tools")
    nxt = re.search(r"^## ", core[start + 3 :], re.M)
    section = core[start : start + 3 + nxt.start()] if nxt else core[start:]
    tokens = set(re.findall(r"`([a-z][a-z_]+)`", section))
    toolish = {
        token
        for token in tokens
        if token.startswith("computer_") or token in {"start_session", "stop_session"}
    }
    assert toolish <= set(tools), (
        f"the which-tool table names tools the server does not serve: "
        f"{sorted(toolish - set(tools))}"
    )
    assert toolish == set(tools), (
        f"the which-tool table never names: {sorted(set(tools) - toolish)}"
    )


# --- the negative self-tests: dishonest examples must FAIL ---------------------------------------


def _execute_tool(tools: dict[str, Any]) -> Any:
    return tools["computer_execute"]


def test_an_unknown_top_level_parameter_fails_validation(tools: dict[str, Any]) -> None:
    errors = _validate_example(
        _execute_tool(tools),
        {"session_id": "S", "action": "click", "x": 1, "y": 2, "bogus_param": 1},
        "computer_execute",
    )
    assert any("unknown parameter" in error and "bogus_param" in error for error in errors), (
        "the validator accepted an unknown top-level parameter"
    )


def test_an_unknown_follow_ups_item_key_fails_validation(tools: dict[str, Any]) -> None:
    errors = _validate_example(
        _execute_tool(tools),
        {
            "session_id": "S",
            "action": "done",
            "follow_ups": [{"action": "keypress", "keys": ["enter"], "bogus_item_key": 1}],
        },
        "computer_execute",
    )
    assert any("bogus_item_key" in error for error in errors), (
        "the validator accepted an unknown follow_ups item key"
    )


def test_an_out_of_band_record_duration_fails_validation(tools: dict[str, Any]) -> None:
    errors = _validate_example(
        _execute_tool(tools),
        {"session_id": "S", "action": "click", "x": 1, "y": 2, "record": {"duration_s": 60}},
        "computer_execute",
    )
    assert any("record validation rejects it" in error for error in errors), (
        "the validator accepted an out-of-band record duration"
    )


def test_a_removed_action_name_fails_validation(tools: dict[str, Any]) -> None:
    errors = _validate_example(
        _execute_tool(tools),
        {"session_id": "S", "action": "key", "keys": ["enter"]},
        "computer_execute",
    )
    assert any("fails the server's GroundedAction validation" in error for error in errors), (
        "the validator accepted the removed 'key' action"
    )


def test_a_zoom_without_a_region_fails_validation(tools: dict[str, Any]) -> None:
    errors = _validate_example(tools["computer_zoom"], {"session_id": "S"}, "computer_zoom")
    assert any("must carry a region" in error for error in errors), (
        "the validator accepted a zoom without a region"
    )


def test_a_wrong_type_fails_validation(tools: dict[str, Any]) -> None:
    errors = _validate_example(
        tools["computer_record_result"],
        {"session_id": "S", "record_id": "R", "block": "yes"},
        "computer_record_result",
    )
    assert any("does not match declared type" in error for error in errors), (
        "the validator accepted a wrong-typed argument"
    )


def test_a_plan_with_an_extra_key_fails_validation(tools: dict[str, Any]) -> None:
    errors = _validate_example(
        _execute_tool(tools),
        {
            "session_id": "S",
            "action": "done",
            "plan": {
                "points": [
                    {
                        "id": "p",
                        "x": 100,
                        "y": 100,
                        "actions": [],
                        "arrival_condition": "there",
                        "duration_ms": 100,
                        "settle_ms": 50,
                        "timeout_ms": 5000,
                        "on_failure": "stop",
                        "on_uncertain": "stop",
                    }
                ],
                "transition_duration_ms": 100,
                "point_timeout_ms": 5000,
                "settle_ms": 50,
                "on_failure": "stop",
                "on_uncertain": "stop",
                "bogus_plan_key": 1,
            },
        },
        "computer_execute",
    )
    assert any("coerce_tolerant_plan" in error for error in errors), (
        "the validator accepted a plan with an extra key"
    )


def test_a_record_object_with_an_unknown_key_fails_validation(
    tools: dict[str, Any],
) -> None:
    errors = _validate_example(
        _execute_tool(tools),
        {"session_id": "S", "action": "done", "record": {"duration_s": 2, "bogus": 1}},
        "computer_execute",
    )
    assert any("unknown parameter" in error and "bogus" in error for error in errors), (
        "the validator accepted an unknown key inside the record object"
    )


def test_an_untagged_fence_fails_the_scanner() -> None:
    with pytest.raises(AssertionError, match="carries no"):
        _tagged_json_blocks("```json\n{}\n```\n", "untagged")


def test_a_bare_or_foreign_fence_fails_the_scanner() -> None:
    with pytest.raises(AssertionError, match="opening fence info"):
        _scan_fences("```\n{}\n```\n", "bare")
    with pytest.raises(AssertionError, match="opening fence info"):
        _scan_fences("```JSON\n{}\n```\n", "uppercase")


def test_a_limits_typo_key_fails_validation(tools: dict[str, Any]) -> None:
    errors = _validate_example(
        tools["start_session"],
        {"max_steps": 100, "limits": {"max_actionz": 100}},
        "start_session",
    )
    assert any("_parse_limits" in error for error in errors), (
        "the validator accepted a typo'd limits key"
    )


def test_a_bad_image_delivery_value_fails_validation(tools: dict[str, Any]) -> None:
    errors = _validate_example(
        tools["start_session"],
        {"max_steps": 100, "image_delivery": "txt"},
        "start_session",
    )
    assert any("image_delivery fails the server's check" in error for error in errors), (
        "the validator accepted an unknown image_delivery value"
    )


def test_a_plan_with_a_flat_action_fails_validation(tools: dict[str, Any]) -> None:
    errors = _validate_example(
        _execute_tool(tools),
        {
            "session_id": "S",
            "action": "click",
            "plan": {
                "points": [
                    {
                        "id": "p",
                        "x": 100,
                        "y": 100,
                        "actions": [],
                        "arrival_condition": "there",
                        "duration_ms": 100,
                        "settle_ms": 50,
                        "timeout_ms": 5000,
                        "on_failure": "stop",
                        "on_uncertain": "stop",
                    }
                ],
                "transition_duration_ms": 100,
                "point_timeout_ms": 5000,
                "settle_ms": 50,
                "on_failure": "stop",
                "on_uncertain": "stop",
            },
        },
        "computer_execute",
    )
    assert any("sent ALONE" in error and "action" in error for error in errors), (
        "the validator accepted a plan sent with a flat action"
    )


def test_an_unknown_plan_action_fails_validation(tools: dict[str, Any]) -> None:
    errors = _validate_example(
        _execute_tool(tools),
        {"session_id": "S", "action": "done", "plan_action": "contine"},
        "computer_execute",
    )
    assert any("plan_action" in error and "contine" in error for error in errors), (
        "the validator accepted an unknown plan_action"
    )
