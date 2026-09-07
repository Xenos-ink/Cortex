"""Stdlib-only MCP stdio client for host-driven benchmark runs (benchmarks harness).

Spawns a FRESH ``computer_use_mcp`` MCP server as a subprocess (per run — never the
session's long-lived server), performs the MCP ``initialize`` handshake and
``tools/list``, then exposes a tiny request/response API. Exists so baseline and
post-fix measurement runs can target an ARBITRARY repo root (e.g. a pristine git
worktree for baseline, the edited tree for post-fix) while other agents work in the
main tree. Protocol framing is newline-delimited JSON, matching the MCP stdio
transport (no Content-Length headers).

Python API:
    from benchmarks.mcp_client import CortexClient
    with CortexClient(r"C:\\path\\to\\repo") as client:          # spawns + handshake
        session = client.call("start_session", {"dry_run": False, ...})
        client.call("computer_execute", {"session_id": ..., "action": "type", ...})
        client.call("stop_session", {"session_id": ...})
    # close() terminates the subprocess; the context manager guarantees cleanup

CLI:
    python benchmarks/mcp_client.py --repo-root <path> list-tools [--out file]
    python benchmarks/mcp_client.py --repo-root <path> call <tool> '<json-args>' [--out file]

Exit codes: 0 success; 2 usage error; 3 server failed to start/initialize; 4 tool call
failed (JSON-RPC error or isError result). All failures print a clear message on stderr
and never leave orphan server processes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Self

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "cortex-bench-client", "version": "1.0.0"}


class McpClientError(RuntimeError):
    """Server failed to start or the transport broke."""


class McpCallError(RuntimeError):
    """A tools/call failed (JSON-RPC error response or isError result)."""


def _python_exe(repo_root: Path) -> Path:
    for relative in (".venv/Scripts/python.exe", ".venv/bin/python"):
        candidate = repo_root / relative
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def _server_module(repo_root: Path) -> list[str]:
    package = repo_root / "src" / "computer_use_mcp"
    if (package / "__main__.py").is_file():
        return ["-m", "computer_use_mcp"]
    return ["-m", "computer_use_mcp.server"]


def _envelope_text(envelope: dict[str, Any]) -> str:
    parts = []
    for item in envelope.get("content", []):
        if item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    return "\n".join(parts)


class CortexClient:
    """One MCP server subprocess per client; speak newline-delimited JSON-RPC on stdio.

    ``repo_root`` selects BOTH the interpreter (``<repo_root>/.venv`` when present) and
    the code (``PYTHONPATH=<repo_root>/src`` wins over any installed copy), so a run
    against a pristine worktree measures exactly that worktree.
    """

    def __init__(
        self,
        repo_root: str | Path,
        stderr_log: str | Path | None = None,
        startup_timeout_s: float = 60.0,
        request_timeout_s: float = 120.0,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        if not (self.repo_root / "src" / "computer_use_mcp").is_dir():
            raise McpClientError(
                f"repo root {self.repo_root} has no src/computer_use_mcp package"
            )
        self.request_timeout_s = request_timeout_s
        self._next_id = 0
        self._closed = False
        self._pending_replies: list[dict[str, Any]] = []
        self.server_info: dict[str, Any] = {}

        exe = _python_exe(self.repo_root)
        if not exe.is_file():
            raise McpClientError(f"python interpreter not found: {exe}")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.repo_root / "src") + os.pathsep + env.get("PYTHONPATH", "")

        if stderr_log is not None:
            self.stderr_log = Path(stderr_log)
        else:
            self.stderr_log = Path(tempfile.gettempdir()) / f"cortex-mcp-server-{os.getpid()}.log"
        self.stderr_log.parent.mkdir(parents=True, exist_ok=True)
        self._stderr_handle = self.stderr_log.open("w", encoding="utf-8")
        self._stderr_handle.write(
            f"[client] spawning {exe} {' '.join(_server_module(self.repo_root))} (cwd={self.repo_root})\n"
        )
        self._stderr_handle.flush()

        self._proc = subprocess.Popen(
            [str(exe), *_server_module(self.repo_root)],
            cwd=str(self.repo_root),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_handle,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._queue: Queue[tuple[str, str]] = Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        try:
            self.tools = self._initialize()
        except Exception:
            self.close()
            raise

    # --- transport -----------------------------------------------------------------------------------

    def _read_loop(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if line:
                self._queue.put(("stdout", line))
        self._queue.put(("exit", str(self._proc.wait())))

    def _send(self, payload: dict[str, Any]) -> None:
        if self._closed or self._proc.poll() is not None:
            raise McpClientError(
                f"server process is not running (exit={self._proc.poll()}); "
                f"see stderr log {self.stderr_log}"
            )
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

    def _recv(self, request_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + self.request_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpClientError(
                    f"timed out waiting for response to request {request_id} "
                    f"after {self.request_timeout_s}s; see stderr log {self.stderr_log}"
                )
            try:
                channel, line = self._queue.get(timeout=min(remaining, 5.0))
            except Empty:
                continue
            if channel == "exit":
                raise McpClientError(
                    f"server exited unexpectedly (code={line}) while waiting for "
                    f"request {request_id}; see stderr log {self.stderr_log}"
                )
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._stderr_handle.write(f"[client] non-JSON stdout line: {line[:200]}\n")
                self._stderr_handle.flush()
                continue
            if message.get("id") != request_id:
                self._handle_unmatched(message)
                continue
            if "error" in message:
                error = message["error"]
                raise McpCallError(
                    f"JSON-RPC error for request {request_id}: "
                    f"{error.get('code')}: {error.get('message')}"
                )
            return message.get("result", {})

    def _handle_unmatched(self, message: dict[str, Any]) -> None:
        """Queue replies to server->client requests (e.g. ping); ignore notifications."""
        if message.get("method") and "id" in message:
            self._pending_replies.append(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": f"client does not support {message['method']}"},
                }
                if not message["method"].endswith("ping")
                else {"jsonrpc": "2.0", "id": message["id"], "result": {}}
            )

    def _flush_replies(self) -> None:
        while self._pending_replies:
            self._send(self._pending_replies.pop(0))

    # --- handshake + calls ---------------------------------------------------------------------------

    def _initialize(self) -> list[dict[str, Any]]:
        self._next_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"roots": {}, "sampling": {}},
                    "clientInfo": CLIENT_INFO,
                },
            }
        )
        result = self._recv(self._next_id)
        self.server_info = result.get("serverInfo", {})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools_result = self._request("tools/list", {})
        return tools_result.get("tools", [])

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        result = self._recv(self._next_id)
        self._flush_replies()
        return result

    def call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke an MCP tool; returns the full tools/call result envelope."""
        envelope = self._request("tools/call", {"name": tool, "arguments": args or {}})
        if envelope.get("isError"):
            raise McpCallError(f"tool {tool!r} reported isError: {_envelope_text(envelope)[:500]}")
        return envelope

    def tool_names(self) -> list[str]:
        return [tool.get("name", "") for tool in self.tools]

    def has_capability(self, needle: str) -> bool:
        """True when any tool schema mentions ``needle`` (e.g. 'follow_ups')."""
        return needle in json.dumps(self.tools)

    # --- lifecycle -----------------------------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.poll() is None:
                try:
                    if self._proc.stdin is not None:
                        self._proc.stdin.close()
                except (OSError, ValueError):
                    pass
                try:
                    self._proc.terminate()
                except OSError:
                    pass
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=5)
        finally:
            for handle in (self._stderr_handle,):
                try:
                    handle.flush()
                except (OSError, ValueError):
                    pass
                try:
                    handle.close()
                except (OSError, ValueError):
                    pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _parse_json_args(raw: str) -> dict[str, Any]:
    parsed = json.loads(raw) if raw.strip() else {}
    if not isinstance(parsed, dict):
        raise TypeError("tool arguments must be a JSON object")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="stdlib MCP stdio client for cortex servers")
    parser.add_argument("--repo-root", required=True, help="repo whose .venv + src/ server to spawn")
    parser.add_argument("--stderr-log", default=None, help="where to capture server stderr")
    sub = parser.add_subparsers(dest="command", required=True)
    list_parser = sub.add_parser("list-tools", help="initialize + tools/list, print tool names")
    list_parser.add_argument("--out", default=None, help="write full tools/list payload here")
    call_parser = sub.add_parser("call", help="tools/call one tool")
    call_parser.add_argument("tool", help="tool name")
    call_parser.add_argument("args", nargs="?", default="{}", help="JSON object of tool arguments")
    call_parser.add_argument("--out", default=None, help="write the full result envelope JSON here")
    call_parser.add_argument(
        "--allow-tool-error", action="store_true",
        help="exit 0 even when the tool reports isError (envelope is still printed)",
    )
    args = parser.parse_args(argv)

    try:
        with CortexClient(args.repo_root, stderr_log=args.stderr_log) as client:
            if args.command == "list-tools":
                payload = {"server_info": client.server_info, "tools": client.tools}
                print(json.dumps({"tools": client.tool_names()}, indent=2))
                if args.out:
                    Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
                return 0
            try:
                call_args = _parse_json_args(args.args)
            except (json.JSONDecodeError, ValueError) as exc:
                print(f"INVALID ARGS: {exc}", file=sys.stderr)
                return 2
            envelope = client.call(args.tool, call_args)
            rendered = json.dumps(envelope, indent=2)
            print(rendered)
            if args.out:
                Path(args.out).write_text(rendered + "\n", encoding="utf-8")
            if envelope.get("isError") and not args.allow_tool_error:
                print(f"TOOL ERROR: {_envelope_text(envelope)[:500]}", file=sys.stderr)
                return 4
            return 0
    except McpClientError as exc:
        print(f"CLIENT ERROR: {exc}", file=sys.stderr)
        return 3
    except McpCallError as exc:
        print(f"TOOL CALL FAILED: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
