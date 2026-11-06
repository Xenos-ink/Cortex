"""The shared FastMCP instance and the server logger (one construction; renamed from `mcp` - a submodule may not collide with the external mcp SDK)."""

import json
import logging
import os
import time
from typing import Any

import pydantic_core
from mcp.server.fastmcp import FastMCP
import mcp.types as mcp_types

import computer_use_mcp

# D2 (Commander, DELTA 1): the FIXED name replaces the moved line 162's __name__, so
# every path (import AND -m, which today logs as "__main__") shares one logger name.

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("computer_use_mcp.server")


class _CortexFastMCP(FastMCP):
    """The Cortex server instance class.

    WP13 (field e4 + e6), Cortex-server-layer fixes — the external mcp package is
    never patched:

    - ``call_tool`` rejects UNKNOWN arguments fail-closed (FastMCP's pydantic
      argument model silently IGNORES extra keys, which is how a direct
      ``hold_ms=...`` was dropped without any error). The rejected reply carries
      the typed reason in ``reasons`` (no ``error`` key — REPLY-CONTRACT.md)
      and teaches where ``hold_ms`` / ``duration_ms`` belong (inside
      ``follow_ups`` items or plan ``points``).
    - ``initialize`` reports CORTEX's own version (``computer_use_mcp.__version__``)
      instead of the mcp SDK version.
    - every reply whose payload carries a ``message`` gains an additive
      `` elapsed_ms=<n>`` suffix (WP13 B) once the call ran at least three
      seconds, so the next field report shows real host-side timings.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        arguments = arguments or {}
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            return await super().call_tool(name, arguments)
        declared = set(tool.parameters.get("properties", {}))
        if tool.context_kwarg:
            declared.add(tool.context_kwarg)
        unknown = sorted(str(key) for key in arguments if str(key) not in declared)
        if unknown:
            pacing_note = ""
            if any(arg in ("hold_ms", "duration_ms") for arg in unknown):
                # L1 (LIVE-WP13-02): every taught example must be VALID — the
                # old text taught hold_ms on a WAIT item, which the per-item
                # validation rejects (hold_ms is only valid on
                # click/right_click/keypress/hotkey; duration_ms on move; a
                # PAUSE is a wait item with delta in SECONDS).
                pacing_note = (
                    " hold_ms/duration_ms are per-item values carried INSIDE a "
                    "follow_ups item or a plan point's action: a HOLD belongs on a "
                    'keypress/hotkey item (e.g. follow_ups=[{"action": "keypress", '
                    '"keys": ["shift"], "hold_ms": 500}]) or a click/right_click item, '
                    'a TIMED MOVE carries duration_ms (e.g. follow_ups=[{"action": '
                    '"move", "x": 100, "y": 100, "duration_ms": 300}]), and a PAUSE is '
                    'a wait item with delta in WHOLE SECONDS (e.g. follow_ups=[{"action": '
                    '"wait", "delta": 1}]).'
                )
            # WP13 DELTA-3 M1 (VAL-WP13-01): the reply contract forbids an
            # ``error`` key on rejected replies (REPLY-CONTRACT.md:33-36) — the
            # typed reason rides ``reasons`` only. DELTA-3 m2: the rejection is
            # converted through the tool's OWN ``fn_metadata.convert_result`` so
            # its content shape equals a NORMAL reply of that tool (structured
            # content only where that tool has it; text serialized the FastMCP
            # way — pydantic_core, Unicode raw, never json.dumps \uXXXX).
            rejection = {
                "ok": False,
                "result": "rejected",
                "reasons": ["unknown_argument"] + [f"unknown_argument: {arg}" for arg in unknown],
                "message": (
                    f"unknown argument(s) for tool '{name}': {', '.join(unknown)}. "
                    f"Valid arguments: {', '.join(sorted(declared))}.{pacing_note}"
                ),
            }
            return tool.fn_metadata.convert_result(rejection)
        started = time.monotonic()
        result = await super().call_tool(name, arguments)
        elapsed_ms = round((time.monotonic() - started) * 1000)
        # WP13 B: timings ride INSIDE the message (the contract has no timing field);
        # only slow calls are annotated so fast-path messages stay byte-stable.
        if elapsed_ms < 3000:
            return result

        def _annotate_body(body: Any) -> bool:
            """Annotate a reply dict's ``message`` in place; True if annotated."""
            if isinstance(body, dict) and isinstance(body.get("message"), str):
                body["message"] = f"{body['message']} elapsed_ms={elapsed_ms}"
                return True
            return False

        if isinstance(result, dict):
            _annotate_body(result)
            return result
        if isinstance(result, tuple) and len(result) == 2:
            # WP13 DELTA-3 m1 (VAL-WP13-01): dict-typed tools (computer_wait,
            # wait_status, cancel_wait, start_session, stop_session) come back
            # as FastMCP's (content, structured) tuple — annotate the
            # STRUCTURED dict and re-serialize its TextContent from it the
            # FastMCP way, so the two can never diverge.
            content, structured = result
            if not _annotate_body(structured):
                return result
            serialized = pydantic_core.to_json(structured, fallback=str, indent=2).decode()
            new_content = []
            for block in content or []:
                if getattr(block, "type", None) == "text" and getattr(block, "text", None):
                    try:
                        json.loads(block.text)
                    except ValueError:
                        new_content.append(block)  # non-JSON text: nothing to annotate
                        continue
                    block = mcp_types.TextContent(type="text", text=serialized)
                new_content.append(block)
            return (new_content, structured)
        entries = [
            block
            for block in (result or [])
            if getattr(block, "type", None) == "text" and getattr(block, "text", None)
        ]
        for entry in entries:
            try:
                body = json.loads(entry.text)
            except ValueError:
                continue  # a non-JSON text block: nothing to annotate
            if _annotate_body(body):
                # WP13 DELTA 1 #4a: re-serialize EXACTLY the way FastMCP
                # serializes a dict tool result (tools/base.py ->
                # func_metadata.convert_result ->
                # pydantic_core.to_json(result, fallback=str, indent=2)) —
                # json.dumps' ensure_ascii=True would turn every Arabic /
                # non-ASCII message character into \uXXXX escapes and
                # change the client-visible text.
                entry.text = pydantic_core.to_json(body, fallback=str, indent=2).decode()
        return result


mcp = _CortexFastMCP("Cortex")
# WP13 (field e6): report CORTEX's own version in serverInfo — the lowlevel server
# otherwise falls back to the mcp SDK's version (1.30.0), which mislabels the server.
mcp._mcp_server.version = computer_use_mcp.__version__
