"""REM-G plain advertised schemas (ORVEX-CORTEX-055, live-test hardening 2).

Pins the WIRE-SCHEMA half of the two-defect live-run forensics. REM-F already
fixed RUNTIME argument tolerance, but TWO further Kimi Code / GLM-5V live runs
still rejected EVERY parameter whose ADVERTISED schema used pydantic's Optional
anyOf union form — x=679 (a plain integer!), keys=["enter"], a valid follow_ups
array, allowed_processes=["mspaint.exe"] all bounced with "/x must be integer;
/x must be null; /x must match a schema in anyOf", while plain-string params
passed. The CLIENT-side validator chokes on the anyOf UNION FORM itself.

The fix (this suite's subject) flattens every Optional parameter's advertised
JSON Schema to the maximally-compatible type-array form via a reusable
``_PlainJsonSchema`` marker attached as the LAST Annotated metadata item
(``Annotated[TolerantX | None, marker]``), COMPOSING the REM-F runtime
validators — runtime validation is byte-identical, only the wire shape changes.

  pin A — every inputSchema property of all 10 tools contains NO "anyOf" key,
          enumerated through the REAL FastMCP tool metadata, with the flattened
          type-array shapes asserted and "default": null preserved;
  pin B — REM-F no-regression: x still validates 679 / 679.0 / "679" and
          679.7 -> 680 through the REAL argument boundary;
  pin C — REM-G keys runtime tolerance: keys="enter" -> ["enter"] executes,
          keys='["ctrl","a"]' is parsed, keys=["enter"] is unchanged;
  pin D — REM-F no-regression: allowed_processes accepts ["mspaint.exe"],
          "mspaint.exe", and "a, b" through the REAL argument boundary;
  pin E — limits={"bogus": 1} is still rejected fail-closed (strict runtime
          contract untouched — only the ADVERTISED shape was flattened).
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
from computer_use_mcp.state import SessionRegistry

#: The 10 MCP tools exposed on the stdio boundary (fixed contract).
ALL_TOOLS = (
    "start_session",
    "stop_session",
    "computer_observe",
    "computer_screenshot",
    "computer_execute",
    "run_goal",
    "create_subtask",
    "list_subtasks",
    "run_subtask",
    "get_session_progress",
)


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh bounded registry/bundles + per-test audit dir (same as controller suite)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    return server


# --- real-boundary harness (mirrors the FastMCP argument-validation surface) --------------------
# The live rejections happened in FastMCP's ARGUMENT VALIDATION, before the tool
# body ran. Calling ``server.computer_execute(...)`` directly would bypass exactly
# the surface that rejected the driving model — these helpers round-trip through
# the same pre_parse_json + arg_model.model_validate -> fn(**kwargs) pipeline the
# stdio transport uses.


def _meta(tool_name: str) -> Any:
    """Synchronous accessor for the registered tool metadata (list_tools registers)."""
    tool = server.mcp._tool_manager.get_tool(tool_name)
    assert tool is not None, f"tool {tool_name} not registered"
    return tool.fn_metadata


async def _call_tool(tool_name: str, fn: Any, arguments: dict[str, Any]) -> Any:
    """Validate ``arguments`` through the REAL FastMCP boundary, then call the body."""
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
    return await _call_tool(
        "computer_execute", server.computer_execute, {"session_id": session_id, **arguments}
    )


async def boundary_start(**arguments: Any) -> Any:
    """start_session with the arguments re-validated through the FastMCP boundary."""
    return await _call_tool("start_session", server.start_session, dict(arguments))


async def _tolerant_session(
    monkeypatch: pytest.MonkeyPatch, **start_kwargs: Any
) -> tuple[str, Any]:
    """A live (non-dry) session with approval off for direct execution assertions."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch,
        backend=ScriptedBackend(width=1920, height=1080),
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
        **start_kwargs,
    )
    return session_id, backend


def _find_anyof(node: Any, path: str = "") -> list[str]:
    """Recursively collect every "anyOf" occurrence (with its path) in a schema."""
    hits: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "anyOf":
                hits.append(f"{path}/{key}")
            hits.extend(_find_anyof(value, f"{path}/{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            hits.extend(_find_anyof(value, f"{path}[{index}]"))
    return hits


def _payload(response: Any) -> dict[str, Any]:
    """Unwrap an executed MCP content-block response to its dict payload."""
    if isinstance(response, list):
        return json.loads(response[0].text)
    return response


# --- pin A: NO anyOf in ANY inputSchema property of the 10 tools -------------------------------


def test_pin_a_no_anyof_in_any_tool_input_schema(fresh_server: Any) -> None:
    """No property on ANY of the 10 tools' advertised inputSchemas uses "anyOf".

    The pre-REM-G wire schema advertised every Optional parameter as
    {"anyOf": [{...}, {"type": "null"}], "default": null} — the exact form the
    live client-side validator rejected. This pin walks the FULL inputSchema of
    each tool (nested, including $defs) through the REAL FastMCP tool metadata.
    """
    assert len(ALL_TOOLS) == 10  # the fixed tool surface
    total_hits: list[str] = []
    for name in ALL_TOOLS:
        schema = _meta(name).arg_model.model_json_schema()
        hits = _find_anyof(schema, name)
        total_hits.extend(hits)
    assert total_hits == [], f"anyOf leaked back into advertised schemas: {total_hits}"


def test_pin_a_flattened_shapes_and_defaults(fresh_server: Any) -> None:
    """The flattened type-array forms are the mission-advertised shapes, defaults kept.

    Every previously-rejected Optional parameter advertises the plain union form
    ({"type": ["integer", "null"]}, {"type": ["array", "null"], ...}) AND still
    carries "default": null — the omission semantics remote callers rely on.
    """
    props = _meta("computer_execute").arg_model.model_json_schema()["properties"]
    expected: dict[str, dict[str, Any]] = {
        "x": {"type": ["integer", "null"], "default": None},
        "y": {"type": ["integer", "null"], "default": None},
        "x2": {"type": ["integer", "null"], "default": None},
        "y2": {"type": ["integer", "null"], "default": None},
        "text": {"type": ["string", "null"], "default": None},
        "expected_effect": {"type": ["string", "null"], "default": None},
        "target": {"type": ["string", "null"], "default": None},
        "include_screenshot_after": {"type": ["boolean", "null"], "default": None},
        "keys": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "default": None,
        },
        "follow_ups": {
            "type": ["array", "null"],
            "items": {"type": "object", "additionalProperties": True},
            "default": None,
        },
    }
    for name, wanted in expected.items():
        assert name in props, f"computer_execute lost parameter {name}"
        flat = props[name]
        for key, value in wanted.items():
            assert flat.get(key) == value, (name, key, flat)

    start_props = _meta("start_session").arg_model.model_json_schema()["properties"]
    for name in ("allowed_processes", "allowed_windows"):
        flat = start_props[name]
        assert flat.get("type") == ["array", "null"], (name, flat)
        assert flat.get("items") == {"type": "string"}, (name, flat)
        assert flat.get("default") is None, (name, flat)
        assert "anyOf" not in flat, (name, flat)
    limits_flat = start_props["limits"]
    assert limits_flat.get("type") == ["object", "null"], limits_flat
    assert limits_flat.get("default") is None, limits_flat
    assert "anyOf" not in limits_flat, limits_flat

    depends_flat = _meta("create_subtask").arg_model.model_json_schema()["properties"][
        "depends_on"
    ]
    assert depends_flat.get("type") == ["array", "null"], depends_flat
    assert depends_flat.get("items") == {"type": "string"}, depends_flat
    assert depends_flat.get("default") is None, depends_flat


async def test_pin_a_flatten_is_not_widening_runtime_schema(fresh_server: Any) -> None:
    """The flattened schema is the ADVERTISED form only — x still rejects garbage.

    The wire schema loses anyOf but runtime validation keeps the REM-F tolerant
    contract: a NON-numeric string is still a typed boundary rejection (this is
    a flattening of the Optional UNION FORM, not a widening of int acceptance).
    """
    with pytest.raises(Exception) as excinfo:
        await boundary_execute(
            "schema-only-check-session", action="click", x="left", y=10
        )
    message = str(excinfo.value)
    assert "integer" in message.lower() or "left" in message


# --- pin B: REM-F no-regression (tolerant integer coordinates through the boundary) --------------


async def test_pin_b_tolerant_int_runtime_unchanged(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """x still validates 679 / 679.0 / "679" / 679.7 -> 680 at runtime (REM-F)."""
    session_id, backend = await _tolerant_session(monkeypatch)
    for raw, expected in ((679, 679), (679.0, 679), ("679", 679), (679.7, 680)):
        backend.executed.clear()
        response = await boundary_execute(
            session_id, action="click", x=raw, y=300, approved=True
        )
        payload = _payload(response)
        assert payload.get("ok") is True, (raw, payload)
        assert [(a.action.value, (a.point.x, a.point.y)) for a in backend.executed] == [
            ("click", (expected, 300))
        ], raw


# --- pin C: REM-G keys runtime tolerance (bare string / JSON-encoded array string) ---------------


async def test_pin_c_keys_bare_string_wraps_to_list(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """keys="enter" -> ["enter"]: a single bare key-name string executes a keypress.

    This is the exact live-run rejection shape #2 (the model sent a bare key
    name and the strict list[str] schema bounced it).
    """
    session_id, backend = await _tolerant_session(monkeypatch)
    response = await boundary_execute(
        session_id, action="keypress", keys="enter", approved=True
    )
    payload = _payload(response)
    assert payload.get("ok") is True, payload
    assert [a.action.value for a in backend.executed] == ["keypress"]


async def test_pin_c_keys_json_string_array_parsed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """keys='["ctrl","a"]' (JSON-encoded array string) parses and executes a hotkey."""
    session_id, backend = await _tolerant_session(monkeypatch)
    response = await boundary_execute(
        session_id, action="hotkey", keys='["ctrl","a"]', approved=True
    )
    payload = _payload(response)
    assert payload.get("ok") is True, payload
    assert [a.action.value for a in backend.executed] == ["hotkey"]


async def test_pin_c_keys_list_unchanged_no_regression(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """keys=["enter"] (the documented array) is unchanged and still executes."""
    session_id, backend = await _tolerant_session(monkeypatch)
    response = await boundary_execute(
        session_id, action="keypress", keys=["enter"], approved=True
    )
    payload = _payload(response)
    assert payload.get("ok") is True, payload
    assert [a.action.value for a in backend.executed] == ["keypress"]


async def test_pin_c_keys_garbage_fails_typed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-JSON bracket keys string still fails with a clean typed error (no crash)."""
    await _tolerant_session(monkeypatch)
    with pytest.raises(Exception) as excinfo:
        await boundary_execute(
            "garbage-keys-session", action="keypress", keys='["ctrl", unquoted]'
        )
    message = str(excinfo.value)
    assert "json" in message.lower() or "keys" in message.lower()


# --- pin D: REM-F no-regression (allowed_processes tolerant shapes through the boundary) --------


async def test_pin_d_allowed_processes_all_shapes_still_work(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """allowed_processes=["mspaint.exe"] AND "mspaint.exe" AND "a, b" all still work."""
    array = await boundary_start(allowed_processes=["mspaint.exe"])
    assert array.get("ok") is not False, array
    assert array["allowed_processes"] == ["mspaint.exe"]

    bare = await boundary_start(allowed_processes="mspaint.exe")
    assert bare.get("ok") is not False, bare
    assert bare["allowed_processes"] == ["mspaint.exe"]

    comma = await boundary_start(allowed_processes="a, b")
    assert comma.get("ok") is not False, comma
    assert comma["allowed_processes"] == ["a", "b"]


# --- pin E: limits stays strict (fail-closed numeric contract — DO NOT touch) -------------------


async def test_pin_e_limits_unknown_field_still_rejected(fresh_server: Any) -> None:
    """limits={"bogus": 1} is still rejected fail-closed (strict runtime contract).

    REM-G flattened only the ADVERTISED schema shape of ``limits`` — the runtime
    fail-closed parsing (unknown fields rejected) is byte-identical.
    """
    response = await boundary_start(limits={"bogus": 1})
    assert response["ok"] is False
    assert response["error"] == "invalid_limits"
    assert "bogus" in response["message"]
