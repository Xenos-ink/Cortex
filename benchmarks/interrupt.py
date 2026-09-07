"""Interruption-injection harness for the T8 realism tasks (launcher-owned tooling).

The HARNESS injects a disturbance mid-task — never the driver. The launcher invokes
this module once the driver has executed the configured number of actions (``at_action``
in the task's ``interrupt`` block); this script performs the injection against the real
desktop, writes a window SNAPSHOT (consumed by the scorer's ``window_state_unchanged``
predicate) and an EVENT LOG (consumed by ``focus_steal_recovered`` and by run review).

Interruption types
    foreign_window : launch a second Notepad with a throwaway dummy document and put it
                     in the foreground (focus steal).
    modal          : open a throwaway Notepad document, make its edit buffer dirty and
                     close the window so the native "Do you want to save changes?"
                     dialog (#32770) appears.

Trigger modes (first one reached wins; without either the injection fires immediately):
    --at-action N --actions-log FILE : poll the launcher-side actions JSONL (written by
                                       benchmarks/driver_bridge.py, one line per executed
                                       computer_execute) until at least N executed actions
                                       have been recorded, then fire BETWEEN driver actions.
    --after-seconds S                : sleep S seconds, then fire.

CLI (see DRIVER-PROTOCOL.md):
    python benchmarks/interrupt.py inject --task-yaml benchmarks/tasks_hard/r2-interruption-recovery.yaml \
        --evidence-dir evidence/perf-004/p3/runs/r2-run1 --at-action 6 --actions-log run1/actions.jsonl
    python benchmarks/interrupt.py inject --task-yaml <yaml> --evidence-dir <dir> --type modal
    python benchmarks/interrupt.py cleanup --log <evidence-dir>/interrupt-log.json

Outputs (in --evidence-dir):
    interrupt-log.json             {"fired", "type", "timestamp_utc", "at_action_observed",
                                    "focus_ok", "spec", "disturbance", "snapshot", "pids"}
    interrupt-window-snapshot.json {"title", "title_token", "class", "pid", "rect",
                                    "text_sha256", "snapshot_utc"}
Run review passes both to the scorer via the run record keys ``interrupt_log`` and
``interrupt_snapshot``.

This module is launcher-side harness tooling: it drives REAL input (Popen + focus +
WM_SETTEXT) and must never run while a driver action is in flight. It only ever touches
windows/documents it created itself.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.appwin import (
    child_controls,
    close_window,
    focus_window,
    kill_process_tree,
    read_edit_text,
    set_dpi_awareness,
    wait_for_window,
    window_class,
    window_rect,
    window_text,
)

WM_SETTEXT = 0x000C
INTERRUPT_TEMP_DIR = Path(tempfile.gettempdir()) / "cortex-interrupt"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_interrupt_block(task_yaml: Path) -> dict[str, Any]:
    data = json.loads(task_yaml.read_text(encoding="utf-8"))
    block = data.get("interrupt")
    if not isinstance(block, dict) or "type" not in block or "spec" not in block:
        raise ValueError(f"{task_yaml.name}: missing or invalid 'interrupt' block")
    return block


def _write_dummy_doc(spec: dict[str, Any]) -> Path:
    """Create the throwaway document the disturbance window will display."""
    INTERRUPT_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    doc = INTERRUPT_TEMP_DIR / str(spec.get("dummy_doc", "cortex_foreign_doc.txt"))
    doc.write_text(str(spec.get("content", "Scripted disturbance document.")), encoding="utf-8")
    return doc


def _snapshot_window(hwnd: int, title_token: str) -> dict[str, Any]:
    set_dpi_awareness()
    text = read_edit_text(hwnd)
    return {
        "title": window_text(hwnd),
        "title_token": title_token,
        "class": window_class(hwnd),
        "pid": _pid_of(hwnd),
        "rect": list(window_rect(hwnd)),
        "text_sha256": _sha256_text(text) if text else None,
        "snapshot_utc": _utc_now(),
    }


def _wait_rect_stable(hwnd: int, timeout_s: float = 4.0) -> None:
    """Wait until the window rect stops changing (opening/DWM animations settle) so the
    snapshot is taken in the window's resting state (scorer compares against it)."""
    deadline = time.monotonic() + timeout_s
    last = window_rect(hwnd)
    while time.monotonic() < deadline:
        time.sleep(0.5)
        current = window_rect(hwnd)
        if current == last:
            return
        last = current


def _pid_of(hwnd: int) -> int:
    pid = ctypes.wintypes.DWORD(0)
    ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return int(pid.value)


def _inject_foreign_window(spec: dict[str, Any]) -> dict[str, Any]:
    doc = _write_dummy_doc(spec)
    proc = subprocess.Popen([str(spec.get("app", "notepad.exe")), str(doc)])
    hwnd = wait_for_window(
        30.0, pid=proc.pid, title_needle=str(spec.get("title_token", doc.name))
    )
    focus_ok = focus_window(hwnd)
    time.sleep(0.4)  # let the foreground settle before the snapshot
    _wait_rect_stable(hwnd)
    snapshot = _snapshot_window(hwnd, str(spec.get("title_token", doc.name)))
    return {
        "pids": [proc.pid],
        "focus_ok": bool(focus_ok),
        "disturbance": {"hwnd": hwnd, "exe": str(spec.get("app", "notepad.exe")), "doc": str(doc)},
        "snapshot": snapshot,
    }


def _inject_modal(spec: dict[str, Any]) -> dict[str, Any]:
    doc = _write_dummy_doc(spec)
    proc = subprocess.Popen([str(spec.get("app", "notepad.exe")), str(doc)])
    hwnd = wait_for_window(
        30.0, pid=proc.pid, title_needle=str(spec.get("title_token", doc.name))
    )
    # Make the edit buffer dirty (EN_CHANGE fires -> Notepad's modified flag), then ask
    # the window to close: the native "Do you want to save changes?" #32770 modal appears.
    edits = [c for c in child_controls(hwnd) if c["cls"] == "Edit"]
    if not edits:
        raise RuntimeError(f"no Edit control found on disturbance notepad hwnd={hwnd}")
    ctypes.windll.user32.SendMessageW(
        ctypes.c_void_p(edits[0]["hwnd"]), WM_SETTEXT, 0,
        ctypes.c_wchar_p(str(spec.get("modal_text", "dirty buffer"))),
    )
    time.sleep(0.4)
    close_window(hwnd, wait_s=1.5)
    dialog_title = window_text(int(ctypes.windll.user32.GetForegroundWindow() or 0))
    snapshot = _snapshot_window(hwnd, str(spec.get("title_token", doc.name)))
    return {
        "pids": [proc.pid],
        "focus_ok": True,
        "disturbance": {
            "hwnd": hwnd, "exe": str(spec.get("app", "notepad.exe")), "doc": str(doc),
            "modal_expected": True, "foreground_after_close": dialog_title,
        },
        "snapshot": snapshot,
    }


_INJECTORS = {"foreign_window": _inject_foreign_window, "modal": _inject_modal}


def _count_actions(actions_log: Path) -> int:
    if not actions_log.is_file():
        return 0
    count = 0
    for line in actions_log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("tool") == "computer_execute" and entry.get("action"):
            count += 1
    return count


def _wait_for_action_threshold(at_action: int, actions_log: Path, timeout_s: float) -> int:
    deadline = time.monotonic() + timeout_s
    observed = _count_actions(actions_log)
    while observed < at_action:
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"actions log never reached {at_action} executed actions "
                f"(observed {observed}); aborting injection fail-safe"
            )
        time.sleep(0.25)
        observed = _count_actions(actions_log)
    return observed


def inject(
    block: dict[str, Any],
    evidence_dir: Path,
    *,
    type_override: str | None = None,
    at_action: int | None = None,
    actions_log: Path | None = None,
    after_seconds: float | None = None,
    wait_timeout_s: float = 3600.0,
) -> dict[str, Any]:
    kind = type_override or str(block["type"])
    spec = dict(block.get("spec") or {})
    if kind not in _INJECTORS:
        raise ValueError(f"unknown interruption type {kind!r} (known: {sorted(_INJECTORS)})")
    if at_action is not None:
        if actions_log is None:
            raise ValueError("--at-action requires --actions-log")
        observed = _wait_for_action_threshold(at_action, actions_log, wait_timeout_s)
    else:
        observed = None
        if after_seconds is not None:
            time.sleep(max(0.0, after_seconds))
    result = _INJECTORS[kind](spec)
    event = {
        "fired": True,
        "type": kind,
        "timestamp_utc": _utc_now(),
        "at_action_observed": observed,
        "at_action_configured": at_action if at_action is not None else block.get("at_action"),
        "focus_ok": result["focus_ok"],
        "spec": spec,
        "disturbance": result["disturbance"],
        "snapshot": str(evidence_dir / "interrupt-window-snapshot.json"),
        "pids": result["pids"],
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = evidence_dir / "interrupt-window-snapshot.json"
    snapshot_path.write_text(json.dumps(result["snapshot"], indent=1), encoding="utf-8")
    (evidence_dir / "interrupt-log.json").write_text(json.dumps(event, indent=1), encoding="utf-8")
    return event


def cleanup(log_path: Path) -> dict[str, Any]:
    """Kill the disturbance processes recorded in an inject log (launcher-side hygiene)."""
    log = json.loads(log_path.read_text(encoding="utf-8"))
    killed = []
    for pid in log.get("pids", []):
        try:
            kill_process_tree(int(pid))
            killed.append(int(pid))
        except OSError:
            continue
    return {"cleaned_pids": killed, "log": str(log_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="launcher-side interruption injection (T8 realism)")
    sub = parser.add_subparsers(dest="command", required=True)
    p_inject = sub.add_parser("inject", help="perform one mid-task injection")
    p_inject.add_argument("--task-yaml", type=Path, required=True)
    p_inject.add_argument("--evidence-dir", type=Path, required=True)
    p_inject.add_argument("--type", dest="type_override", default=None,
                          choices=sorted(_INJECTORS), help="override the task's interrupt type")
    p_inject.add_argument("--at-action", type=int, default=None,
                          help="fire once the actions log shows >= N executed actions")
    p_inject.add_argument("--actions-log", type=Path, default=None,
                          help="launcher-side actions JSONL written by driver_bridge.py")
    p_inject.add_argument("--after-seconds", type=float, default=None,
                          help="time-based fallback trigger")
    p_inject.add_argument("--wait-timeout-s", type=float, default=3600.0)
    p_cleanup = sub.add_parser("cleanup", help="kill the disturbance processes from a log")
    p_cleanup.add_argument("--log", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "cleanup":
        print(json.dumps(cleanup(args.log), indent=1))
        return 0
    block = _load_interrupt_block(args.task_yaml)
    event = inject(
        block, args.evidence_dir,
        type_override=args.type_override,
        at_action=args.at_action,
        actions_log=args.actions_log,
        after_seconds=args.after_seconds,
        wait_timeout_s=args.wait_timeout_s,
    )
    print(json.dumps(event, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
