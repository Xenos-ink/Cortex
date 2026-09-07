"""Launcher-side persistent single-client bridge for host-driven runs (benchmarks harness).

Holds ONE ``CortexClient`` (one fresh server subprocess = one run, per
``benchmarks/tasks_hard/DRIVER-PROTOCOL.md``) and executes tool-call commands that the
driver (an LLM agent) drops into a workdir queue, so the driver can observe results
between actions while the server and session stay alive for the whole task run.

Command file:  <workdir>/cmds/NNNN.json  = {"id": NNNN, "tool": "...", "args": {...}}
Special tool "__exit__" ends the bridge (stop_session must be sent separately).
Result file:   <workdir>/res/NNNN.json   = {"id", "tool", "envelope"(slim), "inner",
                                            "images": [paths], "error"}
Screenshots are decoded to PNG and stripped from the JSON (path recorded).

Optional ``--actions-log FILE``: one JSONL line per EXECUTED ``computer_execute`` call
({"utc", "id", "tool", "action", "ok", "expected_effect"}) — consumed by
``benchmarks/interrupt.py --at-action N`` so the launcher can fire a mid-task
interruption BETWEEN driver actions.

CLI:
    python benchmarks/driver_bridge.py <repo_root> <workdir> [--actions-log FILE] [--stderr-log FILE]
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.mcp_client import CortexClient


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def summarize(envelope: dict[str, Any], res_dir: Path, base: str) -> tuple[dict[str, Any], Any, list[str]]:
    """Split an MCP envelope into (slim JSON-safe copy, parsed inner JSON, image paths)."""
    inner = None
    images: list[str] = []
    slim: dict[str, Any] = {"content": []}
    for item in envelope.get("content", []):
        if item.get("type") == "text":
            slim["content"].append({"type": "text", "text": item.get("text", "")})
            try:
                inner = json.loads(item.get("text", ""))
            except (json.JSONDecodeError, ValueError):
                pass
        elif item.get("type") == "image":
            img_path = res_dir / f"{base}-img{len(images)}.png"
            try:
                img_path.write_bytes(base64.b64decode(item.get("data", "")))
                images.append(str(img_path))
            except Exception as exc:  # noqa: BLE001
                images.append(f"DECODE-ERROR: {exc}")
            slim["content"].append({"type": "image", "mime": item.get("mimeType"),
                                    "bytes": len(item.get("data", "")), "saved": str(img_path)})
        else:
            slim["content"].append({"type": item.get("type")})
    for key, value in envelope.items():
        if key != "content":
            slim[key] = value
    return slim, inner, images


def _action_name(inner: Any) -> str | None:
    """Name of the executed action from a computer_execute result (None if rejected)."""
    if isinstance(inner, dict):
        action = inner.get("action")
        if isinstance(action, dict):
            return str(action.get("action") or "")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="persistent one-client driver bridge")
    parser.add_argument("repo_root")
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--actions-log", type=Path, default=None)
    parser.add_argument("--stderr-log", type=Path, default=None)
    args = parser.parse_args()

    cmds = args.workdir / "cmds"
    res = args.workdir / "res"
    res.mkdir(parents=True, exist_ok=True)
    cmds.mkdir(parents=True, exist_ok=True)
    client = CortexClient(args.repo_root, stderr_log=args.stderr_log)
    print(f"bridge up; server_info={client.server_info}; tools={client.tool_names()}", flush=True)
    try:
        while True:
            pending = sorted(cmds.glob("*.json"))
            if not pending:
                time.sleep(0.25)
                continue
            cmd_path = pending[0]
            try:
                cmd = json.loads(cmd_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(0.25)  # writer still flushing
                continue
            cid = cmd.get("id", cmd_path.stem)
            tool = cmd.get("tool")
            if tool == "__exit__":
                (res / f"{cid}.json").write_text(
                    json.dumps({"id": cid, "tool": tool, "bye": True}), encoding="utf-8")
                cmd_path.unlink(missing_ok=True)
                break
            entry: dict[str, Any] = {"id": cid, "tool": tool, "utc": _utc_now()}
            try:
                envelope = client.call(tool, cmd.get("args") or {})
                slim, inner, images = summarize(envelope, res, str(cid))
                entry.update({"envelope": slim, "inner": inner, "images": images})
                if envelope.get("isError"):
                    entry["tool_error"] = True
                if tool == "computer_execute" and args.actions_log is not None:
                    args.actions_log.parent.mkdir(parents=True, exist_ok=True)
                    with args.actions_log.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps({
                            "utc": entry["utc"], "id": cid, "tool": tool,
                            "action": _action_name(inner), "ok": inner.get("ok") if isinstance(inner, dict) else None,
                        }) + "\n")
            except Exception as exc:  # noqa: BLE001
                entry["error"] = f"{type(exc).__name__}: {exc}"
            (res / f"{cid}.json").write_text(json.dumps(entry, indent=1, default=str), encoding="utf-8")
            cmd_path.unlink(missing_ok=True)
            print(f"executed {cid}:{tool}", flush=True)
    finally:
        client.close()
        print("bridge down", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
