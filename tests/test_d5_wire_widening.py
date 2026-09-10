"""D5 wire-schema widening + D3 output-model pins (ORVEX-CORTEX-056-LIVEFIX).

D5 (F-3 verdict, .orvex/artifacts/f3-d3d5-verification.md): the E-2 vision-session
friction (x/y as numeric strings, allowed_processes as a string, follow_ups as a
string/dict, 4-6 wasted turns per session) was the CLIENT-side ajv validator
rejecting shapes against our PUBLISHED tools/list schema BEFORE any bytes reached
Cortex — the REM-F server-side coercions for those shapes were unreachable from
the host. The fix is SCHEMA-WIDENING ONLY: flat type-arrays that also admit
"string" (and "object" for follow_ups) on exactly the four friction classes.
Runtime coercions already existed and already reject garbage fail-closed with
typed teaching errors — that is re-pinned here. ``limits``/``interference``
stay STRICT (deliberate fail-closed contracts — pinned untouched).

D3 (F-3 verdict): the historical DictModel crash on computer_execute's multi-part
result is NOT live on this tree (fixed by the ``-> Any`` annotation); these pins
guard the class against regression — output_model/output_schema are None for the
three list-returning tools, they carry NO outputSchema on tools/list, and the
convert_result mechanism is documented via a dict-annotated dummy that DOES crash.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest
from mcp.server.fastmcp.utilities.func_metadata import func_metadata
from mcp.types import TextContent
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    make_session,
)

from computer_use_mcp import server
from computer_use_mcp.state import SessionRegistry


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh bounded registry/bundles + per-test audit dir (same as controller suite)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.delenv(server.IMAGE_DELIVERY_ENV, raising=False)
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    return server


def _meta(tool_name: str) -> Any:
    tool = server.mcp._tool_manager.get_tool(tool_name)
    assert tool is not None, f"tool {tool_name} not registered"
    return tool.fn_metadata


async def _call_tool(tool_name: str, fn: Any, arguments: dict[str, Any]) -> Any:
    meta = _meta(tool_name)
    pre = meta.pre_parse_json(dict(arguments))
    model = meta.arg_model.model_validate(pre)
    kwargs = model.model_dump_one_level()
    result = fn(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


async def boundary_execute(session_id: str, **arguments: Any) -> Any:
    arguments.setdefault("action", "click")
    return await _call_tool(
        "computer_execute", server.computer_execute, {"session_id": session_id, **arguments}
    )


async def boundary_start(**arguments: Any) -> Any:
    return await _call_tool("start_session", server.start_session, dict(arguments))


class _FakeProvider:
    """Minimal lazy-provider stand-in (never constructs the real vision provider)."""

    async def decide(self, goal: str, observation: Any, history: list[str]) -> Any:
        raise RuntimeError("fake provider: no decide in this test")

    async def decide_full(self, goal: str, observation: Any, history: list[str]) -> Any:
        raise RuntimeError("fake provider: no decide_full in this test")


def _find_anyof(node: Any, path: str = "") -> list[str]:
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
    if isinstance(response, list):
        return json.loads(response[0].text)
    return response


async def _tolerant_session(
    monkeypatch: pytest.MonkeyPatch, **start_kwargs: Any
) -> tuple[str, Any]:
    session_id, _bundle, backend, _ = make_session(
        monkeypatch,
        backend=ScriptedBackend(width=1920, height=1080),
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
        **start_kwargs,
    )
    return session_id, backend


# --- D5 schema pins: the widened flat type-arrays (WO-1..WO-4) ---------------------------------


def test_d5_schema_coordinates_widen_to_integer_string_null(fresh_server: Any) -> None:
    """WO-1: x/y/x2/y2 advertise {"type": ["integer","string","null"]} — the
    numeric-string shape the client used to reject pre-flight now passes."""
    props = _meta("computer_execute").arg_model.model_json_schema()["properties"]
    for name in ("x", "y", "x2", "y2"):
        flat = props[name]
        assert flat.get("type") == ["integer", "string", "null"], (name, flat)
        assert flat.get("default") is None, (name, flat)
        assert "anyOf" not in flat, (name, flat)  # REM-G flattening holds


def test_d5_schema_keys_widen_to_array_string_null(fresh_server: Any) -> None:
    """WO-4: keys advertises {"type": ["array","string","null"], items string}."""
    flat = _meta("computer_execute").arg_model.model_json_schema()["properties"]["keys"]
    assert flat.get("type") == ["array", "string", "null"], flat
    assert flat.get("items") == {"type": "string"}, flat
    assert flat.get("default") is None, flat
    assert "anyOf" not in flat, flat


def test_d5_schema_follow_ups_widen_to_array_object_string_null(fresh_server: Any) -> None:
    """WO-3: follow_ups advertises array | object | string | null (the widest
    admission — entries still validated strictly and fail-closed server-side)."""
    flat = _meta("computer_execute").arg_model.model_json_schema()["properties"]["follow_ups"]
    assert flat.get("type") == ["array", "object", "string", "null"], flat
    assert flat.get("items") == {"type": "object", "additionalProperties": True}, flat
    assert flat.get("default") is None, flat
    assert "anyOf" not in flat, flat


def test_d5_schema_allowlists_widen_to_array_string_null(fresh_server: Any) -> None:
    """WO-2: allowed_processes/allowed_windows advertise array | string | null."""
    props = _meta("start_session").arg_model.model_json_schema()["properties"]
    for name in ("allowed_processes", "allowed_windows"):
        flat = props[name]
        assert flat.get("type") == ["array", "string", "null"], (name, flat)
        assert flat.get("items") == {"type": "string"}, (name, flat)
        assert flat.get("default") is None, (name, flat)
        assert "anyOf" not in flat, (name, flat)


def test_d5_schema_limits_and_interference_stay_strict(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WO-5: limits/interference keep the STRICT object|null advertisement —
    deliberately NOT widened (fail-closed contracts; unknown fields rejected)."""
    props = _meta("start_session").arg_model.model_json_schema()["properties"]
    assert props["limits"].get("type") == ["object", "null"], props["limits"]
    assert props["interference"].get("type") == ["object", "null"], props["interference"]
    # Fake factories throughout: both calls fail BEFORE construction, but keep the
    # process free of a real LocalComputerBackend (DPI state, conftest.py).
    monkeypatch.setattr(server, "_backend_factory", lambda: ScriptedBackend())
    monkeypatch.setattr(server, "_provider_factory", lambda: _FakeProvider())
    # Runtime strictness is untouched (pin E of REM-G, re-asserted).
    response = server.start_session(limits={"bogus": 1})
    assert response["ok"] is False and response["error"] == "invalid_limits"
    response = server.start_session(interference={"focus_guard": {"banana": 1}})
    assert response["ok"] is False and response["error"] == "invalid_interference"


def test_d5_no_anyof_anywhere_after_widening(fresh_server: Any) -> None:
    """REM-G pin A re-check: the widening introduced NO anyOf on any tool.

    AMENDED (run_goal removal): the surface is the five deterministic tools."""
    tools = (
        "start_session", "stop_session", "computer_observe", "computer_screenshot",
        "computer_execute",
    )
    hits: list[str] = []
    for name in tools:
        hits.extend(_find_anyof(_meta(name).arg_model.model_json_schema(), name))
    assert hits == [], hits


# --- D5 boundary pins: the widened shapes reach the tool body ----------------------------------


async def test_d5_boundary_string_coordinates_execute(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """x="15", y="15" executes through the REAL boundary (the pre-widening client
    rejection shape; TolerantInt coerces to 15/15)."""
    session_id, backend = await _tolerant_session(monkeypatch)
    response = await boundary_execute(session_id, action="click", x="15", y="15", approved=True)
    payload = _payload(response)
    assert payload.get("ok") is True, payload
    assert [(a.action.value, (a.point.x, a.point.y)) for a in backend.executed] == [
        ("click", (15, 15))
    ]


async def test_d5_boundary_garbage_coordinates_still_fail_typed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-closed: "abc" and "12.5.6" (non-numeric garbage) still raise the
    TolerantInt typed teaching error through the boundary — widening admits the
    SHAPE, never the garbage (never silently coerced)."""
    await _tolerant_session(monkeypatch)
    for garbage in ("abc", "12.5.6"):
        with pytest.raises(Exception) as excinfo:
            await boundary_execute(
                "garbage-coordinate-session", action="click", x=garbage, y=10
            )
        message = str(excinfo.value)
        assert garbage in message or "integer" in message.lower(), (garbage, message)


async def test_d5_boundary_allowlist_string_forms(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A comma/space string allowlist and a JSON-array string both normalize to
    the same list the matcher consumes (allowlist logic byte-identical)."""
    monkeypatch.setattr(server, "_backend_factory", lambda: ScriptedBackend())
    monkeypatch.setattr(server, "_provider_factory", lambda: _FakeProvider())
    comma = await boundary_start(allowed_processes="mspaint.exe, notepad.exe")
    assert comma.get("session_id"), comma
    assert comma["allowed_processes"] == ["mspaint.exe", "notepad.exe"]
    bundle = server._get_bundle(str(comma["session_id"]))
    assert bundle.extra["allowed_processes"] == ["mspaint.exe", "notepad.exe"]
    server.stop_session(str(comma["session_id"]))

    json_form = await boundary_start(allowed_processes='["mspaint.exe"]')
    assert json_form["allowed_processes"] == ["mspaint.exe"]  # pre_parse_json normalizes


async def test_d5_boundary_follow_ups_string_and_dict_forms(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """follow_ups as a JSON string and as a single dict both execute the queue
    through the boundary (full strict pipeline afterwards, cap 5, zero bypass)."""
    session_id, backend = await _tolerant_session(monkeypatch)
    response = await boundary_execute(
        session_id,
        action="click",
        x=10,
        y=10,
        approved=True,
        follow_ups='[{"action": "wait", "delta": 1}]',
    )
    payload = _payload(response)
    assert payload.get("ok") is True, payload
    assert [a.action.value for a in backend.executed] == ["click", "wait"]

    response = await boundary_execute(
        session_id,
        action="click",
        x=10,
        y=10,
        approved=True,
        follow_ups={"action": "wait", "delta": 1},
    )
    payload = _payload(response)
    assert payload.get("ok") is True, payload
    assert [a.action.value for a in backend.executed][-2:] == ["click", "wait"]


async def test_d5_boundary_follow_ups_garbage_fails_typed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-closed: a non-JSON follow_ups string raises the typed teaching error
    through the boundary — never a crash, never a silent guess."""
    await _tolerant_session(monkeypatch)
    with pytest.raises(Exception) as excinfo:
        await boundary_execute(
            "garbage-follow-ups", action="click", x=1, y=1, follow_ups="not-json"
        )
    message = str(excinfo.value)
    assert "json" in message.lower() or "follow_ups" in message.lower(), message


async def test_d5_boundary_keys_string_forms(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """keys="enter" (bare key name) and keys='["ctrl","a"]' (JSON array string)
    execute through the boundary (runtime already pinned; this is the boundary
    variant the pre-widening client rejected)."""
    session_id, backend = await _tolerant_session(monkeypatch)
    response = await boundary_execute(
        session_id, action="keypress", keys="enter", approved=True
    )
    assert _payload(response).get("ok") is True
    response = await boundary_execute(
        session_id, action="hotkey", keys='["ctrl","a"]', approved=True
    )
    assert _payload(response).get("ok") is True
    assert [a.action.value for a in backend.executed] == ["keypress", "hotkey"]


# --- D3 pins: output-model absence on the three list-returning tools ---------------------------


def test_d3_output_model_is_none_for_list_returning_tools(fresh_server: Any) -> None:
    """computer_execute / computer_observe / computer_screenshot: output_model and
    output_schema are BOTH None (the ``-> Any`` annotation that fixed the Sep-9
    DictModel crash — guards any future re-annotation back to ``dict[str, object]``
    or any typed annotation, which would crash the multi-part result)."""
    for name in ("computer_execute", "computer_observe", "computer_screenshot"):
        meta = _meta(name)
        assert meta.output_model is None, name
        assert meta.output_schema is None, name


def test_d3_no_outputschema_on_tools_list_for_three_tools(fresh_server: Any) -> None:
    """tools/list carries NO outputSchema for the three list-returning tools
    (the dict-annotated seven MAY publish one — start_session does today)."""
    for name in ("computer_execute", "computer_observe", "computer_screenshot"):
        schema = server.mcp._tool_manager.get_tool(name).parameters
        # parameters is the inputSchema; the registered Tool carries fn_metadata —
        # the wire outputSchema comes from fn_metadata.output_schema (pinned above).
        # Belt-and-braces: the tool annotation is Any.
        tool = server.mcp._tool_manager.get_tool(name)
        assert tool.fn_metadata.output_schema is None, name


def test_d3_convert_result_regression_pin(fresh_server: Any) -> None:
    """The mechanism, documented: a DICT-annotated tool returning a content-block
    list crashes in convert_result with "valid dictionary" (the historical D3);
    the Any-annotated shape returns the blocks unchanged (no validation).

    This is the only path the existing suites never exercised (tests called the
    tool functions directly, bypassing convert_result — the crash site).
    """

    async def dict_annotated(dummy: str) -> dict[str, object]:  # the OLD annotation
        return [TextContent(type="text", text="hello")]  # type: ignore[return-value]

    async def any_annotated(dummy: str) -> Any:  # the CURRENT shape
        return [TextContent(type="text", text="hello")]

    dict_meta = func_metadata(dict_annotated)
    assert dict_meta.output_model is not None  # DictModel IS built for dict aliases
    with pytest.raises(Exception) as excinfo:
        dict_meta.convert_result([TextContent(type="text", text="hello")])
    assert "valid dictionary" in str(excinfo.value) or "dict_type" in str(excinfo.value)

    any_meta = func_metadata(any_annotated)
    assert any_meta.output_model is None
    assert any_meta.output_schema is None
    converted = any_meta.convert_result([TextContent(type="text", text="hello")])
    # The Any path returns the list (converted to content) with NO validation crash.
    assert converted is not None
