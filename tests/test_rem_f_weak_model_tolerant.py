"""REM-F weak-model tolerant-boundary tests (ORVEX-CORTEX-055, live-test hardening).

Pins the three LIVE-RUN argument-shape defects from the Kimi Code / GLM-5V session:
the pipeline executed fine, but FastMCP's strict argument schema rejected the
DRIVING MODEL's tool calls four times and the model gave up on clicking entirely.
This suite drives the tools through the REAL FastMCP argument-validation boundary
(``call_fn_with_arg_validation``: pre_parse_json + arg_model.model_validate), not
just the raw Python signatures — the surface a remote model actually hits.

DEFECT 1 — non-integer click coordinates.

  Vision models aim at pixel 1343.7 and serialize 1343.7 / "1343" / 1343.0. The
  strict ``int`` schema rejected these as "must be integer | null". The fix adds a
  tolerant integer coercion at the tool boundary ONLY (Annotated BeforeValidator;
  the JSON schema stays "integer" — the coercion is pre-validation): integral
  floats and numeric strings pass through; NON-INTEGRAL floats and numeric
  strings ROUND to the nearest int (a vision model aiming at pixel 1343.7 must
  not hard-fail); non-numeric garbage ("left", booleans, lists, NaN) still fails
  with a clean pydantic-style typed error. Downstream models (GroundedAction,
  ActionSpec, bounds checks) stay strict and untouched.

DEFECT 2 — follow_ups array rejected wholesale.

  The model serialized the queue as something other than a list[dict] and the
  entire array was rejected. The fix accepts list[dict] as today, PLUS: a
  JSON-encoded STRING containing the list (parsed), a single dict (wrapped into
  [dict]), and per-entry JSON strings (each parsed). Unknown keys inside entries
  keep the existing tolerance (dict[str, Any]); garbage still fails with the
  existing ``invalid_action`` teaching style.

DEFECT 3 — start_session allowed_processes array rejected with "must be null".

  The model's array serialization failed the list[str] schema. The fix accepts
  list[str] as today, PLUS a comma/space-separated STRING
  ("mspaint.exe, notepad.exe" -> ["mspaint.exe", "notepad.exe"]) and a single
  bare string ("mspaint.exe" -> ["mspaint.exe"]). Same for ``allowed_windows``.
  ``limits`` stays STRICT (fail-closed numeric contract — untouched, pinned
  here as a no-regression guard).

Boundary-only tolerance: once coerced, values flow into the same strict internal
models unchanged. Written RED first (pre-fix failures confirmed), then greened.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    make_session,
)

from computer_use_mcp import server
from computer_use_mcp.models import MAX_FOLLOW_UPS
from computer_use_mcp.state import SessionRegistry


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh bounded registry/bundles + per-test audit dir (same as controller suite)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    return server

# --- real-boundary harness --------------------------------------------------------------------
# The live failures happened in FastMCP's ARGUMENT VALIDATION, before the tool body
# ran. Calling ``server.computer_execute(...)`` directly would bypass exactly the
# surface that rejected the driving model. These helpers round-trip through the same
# func_metadata pipeline the MCP stdio transport uses (pre_parse_json +
# arg_model.model_validate -> fn(**kwargs)).


def _meta(tool_name: str) -> Any:
    """Synchronous accessor for the registered tool metadata (list_tools registers)."""
    tool = server.mcp._tool_manager.get_tool(tool_name)
    assert tool is not None, f"tool {tool_name} not registered"
    return tool.fn_metadata


async def _call_tool(tool_name: str, fn: Any, arguments: dict[str, Any]) -> Any:
    """Validate ``arguments`` through the REAL FastMCP boundary, then call the body.

    Mirrors ``mcp.server.fastmcp.tools.base.Tool.run``: pre_parse_json ->
    arg_model.model_validate -> model_dump_one_level -> fn(**kwargs). A schema
    rejection raises (exactly like the live run); the tool body's own structured
    error payloads are returned as values.
    """
    meta = _meta(tool_name)
    pre = meta.pre_parse_json(dict(arguments))
    model = meta.arg_model.model_validate(pre)
    kwargs = model.model_dump_one_level()
    result = fn(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


async def boundary_execute(session_id: str, **arguments: Any) -> Any:
    """computer_execute with the arguments re-validated through the FastMCP boundary."""
    arguments.setdefault("action", "click")
    return await _call_tool("computer_execute", server.computer_execute, {"session_id": session_id, **arguments})


async def boundary_start(**arguments: Any) -> Any:
    """start_session with the arguments re-validated through the FastMCP boundary."""
    return await _call_tool("start_session", server.start_session, dict(arguments))


async def _tolerant_session(
    monkeypatch: pytest.MonkeyPatch, **start_kwargs: Any
) -> tuple[str, Any]:
    """A live (non-dry) session with approval off for direct execution assertions.

    The fake backend screenshot is 1920x1080 so the live-run coordinate 1343.x lands
    INSIDE bounds — the exact pixel the driving model aimed at.
    """
    session_id, _bundle, backend, _ = make_session(
        monkeypatch,
        backend=ScriptedBackend(width=1920, height=1080),
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
        **start_kwargs,
    )
    return session_id, backend


# --- DEFECT 1: tolerant integer coordinates ----------------------------------------------------


async def test_click_integral_float_coordinate_executes(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """x=1343.0 (integral float) executes at 1343 — the live-run rejection shape #1."""
    session_id, backend = await _tolerant_session(monkeypatch)
    response = await boundary_execute(
        session_id, action="click", x=1343.0, y=300.0, approved=True
    )
    payload = response if isinstance(response, dict) else json.loads(response[0].text)
    assert payload.get("ok") is True, payload
    assert [(a.action.value, (a.point.x, a.point.y)) for a in backend.executed] == [
        ("click", (1343, 300))
    ]


async def test_click_numeric_string_coordinate_executes(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """x="1343" (numeric string) executes at 1343 — the live-run rejection shape #1b."""
    session_id, backend = await _tolerant_session(monkeypatch)
    response = await boundary_execute(
        session_id, action="click", x="1343", y="300", approved=True
    )
    payload = response if isinstance(response, dict) else json.loads(response[0].text)
    assert payload.get("ok") is True, payload
    assert [(a.action.value, (a.point.x, a.point.y)) for a in backend.executed] == [
        ("click", (1343, 300))
    ]


async def test_click_fractional_coordinate_rounds_to_nearest_int(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """x=1343.7 rounds to 1344.

    Policy (stated here as the mission docstring requires): a NON-INTEGRAL float or
    numeric string ROUNDS to the nearest integer (banker's rounding via round()) — a
    vision model aiming at pixel 1343.7 must not hard-fail at the schema boundary.
    """
    session_id, backend = await _tolerant_session(monkeypatch)
    response = await boundary_execute(
        session_id, action="click", x=1343.7, y=300.2, approved=True
    )
    payload = response if isinstance(response, dict) else json.loads(response[0].text)
    assert payload.get("ok") is True, payload
    assert [(a.action.value, (a.point.x, a.point.y)) for a in backend.executed] == [
        ("click", (1344, 300))
    ]


async def test_click_fractional_numeric_string_rounds(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """x="1343.7" (fractional numeric string) rounds to 1344 too."""
    session_id, backend = await _tolerant_session(monkeypatch)
    response = await boundary_execute(
        session_id, action="click", x="1343.7", y=300, approved=True
    )
    payload = response if isinstance(response, dict) else json.loads(response[0].text)
    assert payload.get("ok") is True, payload
    assert [(a.action.value, (a.point.x, a.point.y)) for a in backend.executed] == [
        ("click", (1344, 300))
    ]


async def test_click_garbage_coordinate_fails_clean_typed_error(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """x="left" (non-numeric garbage) still fails with a clean pydantic-style error.

    No crash, no silent coercion: the boundary raises a typed ValueError the MCP
    layer converts into the standard isError structure, exactly as before.
    """
    await _tolerant_session(monkeypatch)
    session_id = "garbage-coordinate-session"  # validation fails BEFORE the body runs
    with pytest.raises(Exception) as excinfo:
        await boundary_execute(session_id, action="click", x="left", y=10)
    message = str(excinfo.value)
    assert "integer" in message.lower() or "left" in message  # pydantic-style typed error


async def test_drag_endpoints_coerce_tolerantly(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """x/y AND x2/y2 share the same tolerant integer coercion (drag endpoints)."""
    session_id, backend = await _tolerant_session(monkeypatch)
    response = await boundary_execute(
        session_id,
        action="drag",
        x="10",
        y=20.0,
        x2=110.7,
        y2="70",
        approved=True,
    )
    payload = response if isinstance(response, dict) else json.loads(response[0].text)
    assert payload.get("ok") is True, payload
    executed = [(a.action.value, (a.point.x, a.point.y), (a.to_point.x, a.to_point.y)) for a in backend.executed]
    assert executed == [("drag", (10, 20), (111, 70))]


async def test_boundary_schema_declares_widened_coordinate_types(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The advertised tool schema stays self-describing AND D5-widened: x/y/x2/y2
    allow integer | string | null (flat type-array, no anyOf).

    The coercion is PRE-validation tolerance, not a semantics change. REM-G
    flattened the Optional union to the plain type-array form (client-side
    validators choked on anyOf); D5 (ORVEX-CORTEX-056-LIVEFIX, F-3 work order
    WO-1) then WIDENED the coordinate advertisement to also admit "string" —
    the live client-side ajv validator rejected numeric-string coordinates
    BEFORE the REM-F coercions could ever run, burning 4 turns per vision
    session. The runtime contract is unchanged: ints, integral floats, and
    numeric strings coerce; non-numeric garbage still fails typed (pinned above).
    """
    meta = _meta("computer_execute")
    schema = meta.arg_model.model_json_schema()["properties"]
    for name in ("x", "y", "x2", "y2"):
        assert schema[name].get("type") == ["integer", "string", "null"], (name, schema[name])
        assert "anyOf" not in schema[name], (name, schema[name])


# --- DEFECT 2: tolerant follow_ups shapes -------------------------------------------------------


async def test_follow_ups_json_string_is_parsed_and_executed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """follow_ups as a JSON-encoded STRING is parsed and executed — live defect #2."""
    session_id, backend = await _tolerant_session(monkeypatch)
    response = await boundary_execute(
        session_id,
        action="click",
        x=10,
        y=10,
        approved=True,
        follow_ups='[{"action": "wait", "delta": 1}]',
    )
    payload = response if isinstance(response, dict) else json.loads(response[0].text)
    assert payload.get("ok") is True, payload
    assert payload["follow_ups_stopped_reason"] is None
    assert len(payload["follow_up_results"]) == 2  # the click + the queued wait
    assert [a.action.value for a in backend.executed] == ["click", "wait"]


async def test_follow_ups_single_dict_is_wrapped(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """follow_ups as a single dict is wrapped into [dict] and executed."""
    session_id, backend = await _tolerant_session(monkeypatch)
    response = await boundary_execute(
        session_id,
        action="click",
        x=10,
        y=10,
        approved=True,
        follow_ups={"action": "wait", "delta": 1},
    )
    payload = response if isinstance(response, dict) else json.loads(response[0].text)
    assert payload.get("ok") is True, payload
    assert [a.action.value for a in backend.executed] == ["click", "wait"]


async def test_follow_ups_entry_json_strings_are_parsed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A follow_ups LIST whose ENTRIES are JSON strings parses each entry too."""
    session_id, backend = await _tolerant_session(monkeypatch)
    response = await boundary_execute(
        session_id,
        action="click",
        x=10,
        y=10,
        approved=True,
        follow_ups=['{"action": "wait", "delta": 1}'],
    )
    payload = response if isinstance(response, dict) else json.loads(response[0].text)
    assert payload.get("ok") is True, payload
    assert [a.action.value for a in backend.executed] == ["click", "wait"]


async def test_follow_ups_unknown_keys_still_tolerated(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown keys inside entries keep the existing dict tolerance (unchanged)."""
    session_id, backend = await _tolerant_session(monkeypatch)
    response = await boundary_execute(
        session_id,
        action="click",
        x=10,
        y=10,
        approved=True,
        follow_ups=[{"action": "wait", "delta": 1, "why": "settle", "id": "x"}],
    )
    payload = response if isinstance(response, dict) else json.loads(response[0].text)
    assert payload.get("ok") is True, payload
    assert [a.action.value for a in backend.executed] == ["click", "wait"]


async def test_follow_ups_garbage_string_fails_with_existing_style(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-JSON follow_ups string fails with the existing teaching error style.

    NOTE: this pin targets the tool BODY's structured ``invalid_action`` response;
    a garbage string that cannot even parse as JSON is rejected by the boundary
    coercion with a pydantic-style typed error (also clean, also no crash). Both
    are fail-closed; this test pins whichever layer catches it by accepting either.
    """
    session_id, _backend = await _tolerant_session(monkeypatch)
    try:
        response = await boundary_execute(
            session_id, action="click", x=10, y=10, approved=True, follow_ups="not json ["
        )
        # If the boundary passed it through (it must NOT for non-JSON), the body's
        # structured invalid_action teaching shape is required.
        assert response["ok"] is False
        assert response["error"] == "invalid_action"
    except Exception as exc:  # noqa: BLE001 - boundary rejection: clean typed error, no crash
        assert "json" in str(exc).lower() or "follow_ups" in str(exc).lower()


async def test_follow_ups_list_shape_still_works_no_regression(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain list[dict] follow_ups (the documented shape) still works untouched."""
    session_id, backend = await _tolerant_session(monkeypatch)
    response = await boundary_execute(
        session_id,
        action="click",
        x=10,
        y=10,
        approved=True,
        follow_ups=[
            {"action": "wait", "delta": 1},
            {"action": "click", "x": 20, "y": 20},
        ],
    )
    payload = response if isinstance(response, dict) else json.loads(response[0].text)
    assert payload.get("ok") is True, payload
    assert [a.action.value for a in backend.executed] == ["click", "wait", "click"]


async def test_follow_ups_cap_still_enforced(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MAX_FOLLOW_UPS cap keeps binding after the tolerant shapes (no bypass)."""
    session_id, _backend = await _tolerant_session(monkeypatch)
    too_many_json = json.dumps([{"action": "wait", "delta": 1}] * (MAX_FOLLOW_UPS + 1))
    try:
        response = await boundary_execute(
            session_id, action="click", x=10, y=10, approved=True, follow_ups=too_many_json
        )
        assert response["ok"] is False
        assert response["error"] == "invalid_action"
        assert str(MAX_FOLLOW_UPS) in response["message"]
    except Exception as exc:  # noqa: BLE001 - boundary cap: equally fail-closed
        assert str(MAX_FOLLOW_UPS) in str(exc) or "follow_ups" in str(exc).lower()


# --- DEFECT 3: tolerant allowed_processes / allowed_windows --------------------------------------


async def test_start_session_allowed_processes_comma_string(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """allowed_processes="mspaint.exe, notepad.exe" -> list of 2 (live defect #3)."""
    response = await boundary_start(allowed_processes="mspaint.exe, notepad.exe")
    assert response.get("ok") is not False, response
    assert response["allowed_processes"] == ["mspaint.exe", "notepad.exe"]


async def test_start_session_allowed_processes_bare_string(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single bare string allowed_processes="mspaint.exe" -> ["mspaint.exe"]."""
    response = await boundary_start(allowed_processes="mspaint.exe")
    assert response.get("ok") is not False, response
    assert response["allowed_processes"] == ["mspaint.exe"]


async def test_start_session_allowed_processes_space_separated(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Space-separated (no comma) strings split too — same weak-model serialization."""
    response = await boundary_start(allowed_processes="mspaint.exe notepad.exe")
    assert response.get("ok") is not False, response
    assert response["allowed_processes"] == ["mspaint.exe", "notepad.exe"]


async def test_start_session_allowed_processes_array_no_regression(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """allowed_processes=["mspaint.exe"] (the documented array) still works."""
    response = await boundary_start(allowed_processes=["mspaint.exe"])
    assert response.get("ok") is not False, response
    assert response["allowed_processes"] == ["mspaint.exe"]


async def test_start_session_allowed_windows_string_shapes(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """allowed_windows gets the SAME tolerant shapes (comma string, bare, array)."""
    comma = await boundary_start(allowed_windows="Paint, Notepad")
    assert comma.get("ok") is not False, comma
    assert comma["allowed_windows"] == ["Paint", "Notepad"]

    bare = await boundary_start(allowed_windows="Calculator")
    assert bare.get("ok") is not False, bare
    assert bare["allowed_windows"] == ["Calculator"]

    array = await boundary_start(allowed_windows=["Calculator"])
    assert array.get("ok") is not False, array
    assert array["allowed_windows"] == ["Calculator"]


async def test_follow_ups_entry_coordinates_coerce_tolerantly(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued entry with float/string coordinates rides the SAME coercion.

    The boundary converts the queue to list[dict]; each entry is then validated by
    the strict ActionSpec — so the tool body must also run the tolerant coordinate
    coercion on entry values before handing them to the strict internal model.
    """
    session_id, backend = await _tolerant_session(monkeypatch)
    response = await boundary_execute(
        session_id,
        action="click",
        x=10,
        y=10,
        approved=True,
        follow_ups=[{"action": "click", "x": 1000.7, "y": "500"}],
    )
    payload = response if isinstance(response, dict) else json.loads(response[0].text)
    assert payload.get("ok") is True, payload
    summary = [(a.action.value, (a.point.x, a.point.y)) for a in backend.executed]
    assert summary == [("click", (10, 10)), ("click", (1001, 500))]


# --- limits stays strict (fail-closed numeric contract — DO NOT touch) ---------------------------


async def test_start_session_limits_unknown_field_still_rejected(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """limits={"bogus": 1} is still rejected fail-closed (strict contract unchanged)."""
    response = await boundary_start(limits={"bogus": 1})
    assert response["ok"] is False
    assert response["error"] == "invalid_limits"
    assert "bogus" in response["message"]


def test_start_session_limits_string_value_strict_contract_direct(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The INTERNAL limits contract stays strict: a string value reaching the body
    is a typed ``invalid_limits`` failure (the FastMCP layer may lax-coerce numeric
    strings inside ``dict[str, float]`` — pre-existing pydantic behavior, untouched;
    this pin guards the fail-closed body contract itself).
    """
    response = server.start_session(limits={"max_actions": "three"})
    assert response["ok"] is False
    assert response["error"] == "invalid_limits"
