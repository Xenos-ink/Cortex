"""Human-reference builder for the harsh benchmark tasks (benchmarks/tasks_hard/).

Replays a fixed competent-human action script per task through the REAL backend
(``computer_use_mcp.backend.LocalComputerBackend``) at calibrated human cadence and
times itself -> ``evidence/perf-004/p3/human-references.json``.

Cadence calibration (provisional, per mission; reconcile with
``evidence/perf-004/p1/cadence.json`` when it lands):
    Fitts click ~0.6 s, typing ~220 ms/char, dialog wait ~1.0 s,
    keypress ~0.5 s, drag stroke ~1.2 s (plus the backend's intrinsic input pacing).

References are CALIBRATED REFERENCES, not real human runs; each task YAML says so in
``human_reference.method_log`` and they are superseded when a real human performs the
task. Where a UI replay is too brittle to be deterministic (h6: Explorer multi-select
move+rename), an ARITHMETIC reference (explicit action list x cadence) is produced and
labeled as such.

Usage:
    python benchmarks/make_references.py                 # all six tasks
    python benchmarks/make_references.py --tasks h1,h3   # subset
    python benchmarks/make_references.py --list          # show the h6 action table

WARNING: drives the REAL desktop (real mouse/keyboard). Do not touch the machine while
it runs. Every replay closes the applications it launched.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import io
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks import score_task
from benchmarks.appwin import (
    focus_window,
    launch_notepad,
    wait_for_window,
    window_text,
)
from benchmarks.tasks_hard.setup_fixtures import seed

BENCH_ROOT = Path.home() / "Desktop" / "cortex-bench"
EVIDENCE = REPO_ROOT / "evidence" / "perf-004" / "p3" / "references"
TASKS_DIR = REPO_ROOT / "benchmarks" / "tasks_hard"
EXCEL = r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE"
MSPAINT = r"C:\Windows\System32\mspaint.exe"

CLICK_S, CHAR_S, DIALOG_S, KEYPRESS_S, DRAG_S = 0.6, 0.22, 1.0, 0.5, 1.2
COGNITIVE = {"h1": 0, "h2": 20, "h3": 5, "h4": 45, "h5": 0, "h6": 15}  # documented per task

DISCLAIMER = ("Calibrated human references (scripted replay / arithmetic at Fitts cadence) — "
              "NOT real human runs; superseded by real human reference runs when performed.")


# --- replay engine --------------------------------------------------------------------------------


class Replay:
    """Executes GroundedActions through the real backend with human cadence added."""

    def __init__(self, backend: Any, evidence_dir: Path) -> None:
        self.backend = backend
        self.evidence_dir = evidence_dir
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.log: list[dict[str, Any]] = []

    def act(self, action: str, **kwargs: Any) -> None:
        from computer_use_mcp.models import GroundedAction

        grounded = GroundedAction(action=action, confidence=1.0,
                                  reason="human reference replay", **kwargs)
        self.backend.execute(grounded)
        if action == "type":
            pause = CHAR_S * len(str(kwargs.get("text") or ""))
        elif action in ("click", "double_click"):
            pause = CLICK_S
        elif action == "keypress":
            pause = KEYPRESS_S
        elif action == "drag":
            pause = DRAG_S
        else:  # wait already elapsed inside the backend; anything else needs no cadence
            pause = 0.0
        if pause > 0:
            time.sleep(pause)
        self.log.append({"action": action, **kwargs})

    def type(self, text: str) -> None:
        assert len(text) <= 2000, "backend type actions cap at 2000 chars"
        self.act("type", text=text)

    def key(self, keys: list[str]) -> None:
        self.act("keypress", keys=keys)

    def click(self, x: int, y: int) -> None:
        self.act("click", point={"x": x, "y": y})

    def drag(self, x0: int, y0: int, x1: int, y1: int) -> None:
        self.act("drag", point={"x": x0, "y": y0}, to_point={"x": x1, "y": y1})

    def wait(self, seconds: float) -> None:
        self.act("wait", delta=int(seconds))

    def observe_png(self) -> Any:
        from PIL import Image

        observation = self.backend.observe()
        return Image.open(io.BytesIO(base64.b64decode(observation.image_base64))).convert("RGB")


def kill_pid(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True, timeout=15, check=False)


def precheck_no_window(class_name: str, task: str) -> None:
    from benchmarks.appwin import find_window

    if find_window(class_name=class_name) is not None:
        raise RuntimeError(f"{task}: an existing {class_name} window is open; close it before replaying")


def read_employees() -> list[tuple[str, str, str]]:
    rows = []
    for line in score_task._fixture("t1_source.txt").read_text(encoding="utf-8").splitlines()[1:]:
        if line.strip():
            name, position, salary = [p.strip() for p in line.split(",")]
            rows.append((name, position, salary))
    return rows


def save_as_via_f12(replay: Replay, out_path: Path, evidence_dir: Path) -> None:
    """F12 -> type full path -> Enter, verify the exact output file, up to 3 attempts.

    Each attempt is followed by one corrective Enter (a dead dialog, or the
    replace-confirmation). The retry exists because a fast synthetic typist can drop or
    duplicate one keystroke in the dialog (observed once live: a stray '1' produced
    t4_out_summary.xlsx1.xlsx); wrongly-named artifacts are deleted so the bench area
    never keeps one, and incidents are logged to evidence."""
    incidents: list[str] = []
    for attempt in (1, 2, 3):
        replay.key(["f12"])
        replay.wait(DIALOG_S)
        replay.type(str(out_path))
        replay.key(["enter"])
        replay.wait(1.5)
        if not out_path.exists():
            replay.key(["enter"])  # dead dialog, or 'replace file?' confirmation
            replay.wait(1.5)
        if out_path.exists():
            break
        for stray in BENCH_ROOT.glob(f"{out_path.stem}*"):
            if stray.is_file() and stray != out_path:
                stray.unlink(missing_ok=True)
                incidents.append(f"attempt {attempt}: deleted stray {stray.name}")
    else:
        replay.observe_png().save(evidence_dir / "save-failure.png")
        raise RuntimeError(f"save failed: {out_path} still missing (see save-failure.png)")
    if incidents:
        (evidence_dir / "save-incidents.json").write_text(json.dumps(incidents, indent=2), encoding="utf-8")


def launch_excel(replay: Replay) -> tuple[subprocess.Popen, int]:
    """Deterministic Excel cold start: /e suppresses the flaky Start screen; ctrl+n opens
    the blank workbook. Documented deviation: models a profile with the Start screen
    disabled (drivers must handle the Start screen themselves if it appears)."""
    proc = subprocess.Popen([EXCEL, "/e"])
    hwnd = wait_for_window(60, class_name="XLMAIN")
    focus_window(hwnd)
    replay.key(["ctrl", "n"])
    time.sleep(0.8)
    return proc, hwnd


# --- h1: Excel employee sheet ---------------------------------------------------------------------


def replay_h1(backend: Any, evidence_dir: Path) -> dict[str, Any]:
    precheck_no_window("XLMAIN", "h1")
    out_path = BENCH_ROOT / "t1_out_employees.xlsx"
    out_path.unlink(missing_ok=True)
    replay = Replay(backend, evidence_dir)
    proc = None
    start = time.perf_counter()
    try:
        proc, _hwnd = launch_excel(replay)
        replay.type("Name\tPosition\tSalary\n")
        for name, position, salary in read_employees():
            replay.type(f"{name}\t{position}\t{salary}\n")
        replay.key(["right"])                    # cursor A12 -> B12
        replay.key(["right"])                    # B12 -> C12
        replay.type("=SUM(C2:C11)\n")
        replay.key(["ctrl", "home"])
        replay.key(["ctrl", "b"])                # bold A1
        replay.key(["right"])                    # -> B1
        replay.key(["ctrl", "b"])                # bold B1
        replay.key(["right"])                    # -> C1
        replay.key(["ctrl", "b"])                # bold C1
        replay.key(["ctrl", "home"])
        save_as_via_f12(replay, out_path, evidence_dir)
        elapsed = time.perf_counter() - start
    finally:
        if proc is not None:
            kill_pid(proc.pid)
    check = score_task.score_task(TASKS_DIR / "h1-excel-employee-sheet.yaml", {})
    return {"replay_seconds": round(elapsed, 2), "self_check": check, "output": str(out_path)}


# --- h2: cross-app research ------------------------------------------------------------------------


def replay_h2(backend: Any, evidence_dir: Path) -> dict[str, Any]:
    precheck_no_window("Notepad", "h2")
    from benchmarks.appwin import close_window, find_window

    if find_window(class_name="Chrome_WidgetWin_1", title_needle="Asset Register") is not None:
        raise RuntimeError("h2: an 'Asset Register' Edge window is already open; close it before replaying")
    out_path = BENCH_ROOT / "t2_out_facts.txt"
    out_path.unlink(missing_ok=True)
    replay = Replay(backend, evidence_dir)
    page_url = "file:///" + str(BENCH_ROOT / "t2_facts.html").replace("\\", "/")
    edge = next((p for p in
                 (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")
                 if Path(p).exists()), None)
    if edge is None:
        raise RuntimeError("h2: Microsoft Edge not found")
    proc = None
    edge_hwnd = None
    start = time.perf_counter()
    try:
        # Edge joins an existing browser session on this box (the new window belongs to
        # a different pid), so the wait is by class+title, never by pid.
        proc = subprocess.Popen([
            edge, "--new-window", "--no-first-run", "--no-default-browser-check",
            "--disable-features=msEdgeWelcome", "--window-size=1200,800", page_url,
        ])
        edge_hwnd = wait_for_window(45, class_name="Chrome_WidgetWin_1", title_needle="Asset Register")
        focus_window(edge_hwnd)
        replay.wait(1.0)                          # page render settle
        proc = subprocess.Popen(["notepad.exe"])  # a NEW untitled Notepad document
        hwnd = wait_for_window(30, pid=proc.pid, title_needle="Notepad")
        focus_window(hwnd)
        replay.type("asset: NX-7391-ALPHA\nfirmware: 4.2.117-RC9\nbackup_ip: 10.42.7.19\n")
        replay.key(["ctrl", "s"])                 # Save As on an untitled document
        replay.wait(DIALOG_S)
        replay.type(str(out_path))
        replay.key(["enter"])
        replay.wait(1.0)
        if not out_path.exists():
            replay.key(["enter"])
            replay.wait(1.0)
        elapsed = time.perf_counter() - start
        title = window_text(hwnd)
    finally:
        if proc is not None:
            kill_pid(proc.pid)
        if edge_hwnd is not None:
            close_window(int(edge_hwnd), wait_s=2.0)  # ONLY our window; the browser process is shared
    content = out_path.read_text(encoding="utf-8", errors="replace") if out_path.is_file() else ""
    return {
        "replay_seconds": round(elapsed, 2),
        "self_check": {"file_contains_facts": all(
            score_task._norm_alnum(n) in score_task._norm_alnum(content)
            for n in ("asset: NX-7391-ALPHA", "firmware: 4.2.117-RC9", "backup_ip: 10.42.7.19")),
            "notepad_title": title},
        "output": str(out_path),
        "deviations": "Edge cold start with the direct file URL models 'open Edge + navigate'; on "
                      "this box Edge joins an existing browser session, so the replay opens a new "
                      "window in it and teardown closes ONLY that window (the browser process is "
                      "shared and never killed). The 1.0 s dialog wait covers the Notepad Save As.",
    }


# --- h3: precision drawing -------------------------------------------------------------------------


def replay_h3(backend: Any, evidence_dir: Path) -> dict[str, Any]:
    precheck_no_window("MSPaintApp", "h3")
    replay = Replay(backend, evidence_dir)
    replay.observe_png().save(evidence_dir / "canvas_before.png")  # blank-canvas evidence
    proc = None
    start = time.perf_counter()
    check: dict[str, Any] = {}
    try:
        proc = subprocess.Popen([MSPAINT])
        hwnd = wait_for_window(30, class_name="MSPaintApp")
        time.sleep(1.0)
        ctypes.windll.user32.ShowWindow(int(hwnd), 3)  # SW_MAXIMIZE (models the maximize click)
        time.sleep(CLICK_S)
        focus_window(hwnd)
        canvas = None
        for _ in range(5):
            image = replay.observe_png()
            canvas = score_task.detect_canvas(image.load(), image.width, image.height)
            if canvas:
                break
            time.sleep(0.5)
        if canvas is None:
            raise RuntimeError("h3: could not locate the canvas on the maximized window")
        cx, cy = canvas[0], canvas[1]
        replay.click(515, 77)                     # rectangle tool (verified ribbon coordinate)
        replay.drag(cx + 100, cy + 100, cx + 300, cy + 220)
        replay.click(313, 85)                     # fill-with-color (bucket) tool
        replay.click(cx + 200, cy + 160)          # fill the interior
        elapsed = time.perf_counter() - start
        check = score_task.score_task(TASKS_DIR / "h3-precision-drawing.yaml", {})  # window still open
        replay.observe_png().save(evidence_dir / "canvas_after.png")
    finally:
        if proc is not None:
            kill_pid(proc.pid)
    return {
        "replay_seconds": round(elapsed, 2),
        "self_check": check,
        "method_log": "ribbon coordinates (rectangle tool, bucket tool) verified live on this box "
                      "(evidence/perf-004/p3/runs/paint-recon/); canvas origin measured from the "
                      "live screenshot with the scorer's own detector before drawing.",
    }


# --- h4: composite thinking ------------------------------------------------------------------------


def replay_h4(backend: Any, evidence_dir: Path) -> dict[str, Any]:
    precheck_no_window("XLMAIN", "h4")
    out_path = BENCH_ROOT / "t4_out_summary.xlsx"
    out_path.unlink(missing_ok=True)
    replay = Replay(backend, evidence_dir)
    totals = score_task._fixture_totals("t4_source.txt")
    order = ["north", "south", "east"]
    proc = None
    start = time.perf_counter()
    source = launch_notepad(str(BENCH_ROOT / "t4_source.txt"))
    try:
        replay.wait(3.0)                          # reading dwell (computation allowance added separately)
        proc, _hwnd = launch_excel(replay)
        replay.type("Region\tTotal\n")
        for region in order:
            replay.type(f"{region}\t{round(totals[region], 2)}\n")
        save_as_via_f12(replay, out_path, evidence_dir)
        elapsed = time.perf_counter() - start
    finally:
        if proc is not None:
            kill_pid(proc.pid)
        source.close()
    check = score_task.score_task(TASKS_DIR / "h4-composite-thinking.yaml", {})
    return {"replay_seconds": round(elapsed, 2), "self_check": check, "output": str(out_path),
            "computed_totals": {k: round(v, 2) for k, v in totals.items()}}


# --- h5: bulk data entry ---------------------------------------------------------------------------


def replay_h5(backend: Any, evidence_dir: Path) -> dict[str, Any]:
    precheck_no_window("XLMAIN", "h5")
    out_path = BENCH_ROOT / "t5_out_roster.xlsx"
    out_path.unlink(missing_ok=True)
    replay = Replay(backend, evidence_dir)
    lines = [line for line in score_task._fixture("t5_roster.csv").read_text(encoding="utf-8").splitlines()
             if line.strip()]
    proc = None
    start = time.perf_counter()
    try:
        proc, _hwnd = launch_excel(replay)
        for line in lines:
            cells = [c.strip() for c in line.split(",")]
            replay.type("\t".join(cells) + "\n")
        save_as_via_f12(replay, out_path, evidence_dir)
        elapsed = time.perf_counter() - start
    finally:
        if proc is not None:
            kill_pid(proc.pid)
    check = score_task.score_task(TASKS_DIR / "h5-bulk-data-entry.yaml", {})
    return {"replay_seconds": round(elapsed, 2), "self_check": check, "output": str(out_path),
            "lines_typed": len(lines)}


# --- h6: file triage (arithmetic reference: auditable action list x cadence) -----------------------


def h6_seconds() -> tuple[float, list[dict[str, Any]]]:
    table: list[dict[str, Any]] = [
        {"op": "open t6_inbox in Explorer (click address bar, type path, Enter, load wait)",
         "count": 1, "seconds": round(3 * CLICK_S + 47 * CHAR_S + 1.0, 2)},
    ]
    for name, chars in {"Reports": 7, "Images": 6, "Data": 4}.items():
        table.append({"op": f"create folder '{name}' (right-click > New > Folder, type name, Enter)",
                      "count": 1, "seconds": round(3 * CLICK_S + KEYPRESS_S + chars * CHAR_S, 2)})
    per_file = (CLICK_S          # select the file
                + KEYPRESS_S     # ctrl+x
                + 2 * CLICK_S    # double-click destination folder
                + KEYPRESS_S     # ctrl+v
                + KEYPRESS_S     # F2
                + 9 * CHAR_S     # type 'archived_' prefix (F2 preselects the stem)
                + KEYPRESS_S     # Enter
                + 0.8)           # navigate back up (Alt+Up)
    table.append({"op": "per file: select, cut, open folder, paste, F2, type prefix, Enter, go back",
                  "count": 12, "seconds": round(12 * per_file, 2)})
    table.append({"op": "final visual verification scan of the three folders",
                  "count": 1, "seconds": 6.0})
    total = round(sum(item["seconds"] for item in table), 2)
    return total, table


# --- driver ----------------------------------------------------------------------------------------

REPLAYS = {"h1": replay_h1, "h2": replay_h2, "h3": replay_h3, "h4": replay_h4, "h5": replay_h5}


def run_one(task: str, backend: Any) -> dict[str, Any]:
    seed(["t" + task[1:]])
    evidence_dir = EVIDENCE / f"ref-{task}"
    result: dict[str, Any] = {"task": task, "method": "replayed"}
    if task in REPLAYS:
        outcome = REPLAYS[task](backend, evidence_dir)
        result.update(outcome)
        seconds = outcome["replay_seconds"]
    else:
        total, table = h6_seconds()
        result.update({"method": "arithmetic", "seconds_table": table, "replay_seconds": total})
        seconds = total
    cognitive = COGNITIVE[task]
    result["cognitive_allowance_s"] = cognitive
    result["seconds"] = round(seconds + cognitive, 2)
    result["calibration"] = {
        "fitts_click_s": CLICK_S, "char_s": CHAR_S, "dialog_wait_s": DIALOG_S,
        "keypress_s": KEYPRESS_S, "drag_s": DRAG_S,
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "result.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build calibrated human references (real UI replays)")
    parser.add_argument("--tasks", default="h1,h2,h3,h4,h5,h6", help="comma list like h1,h3")
    parser.add_argument("--list", action="store_true", help="print the h6 arithmetic action table and exit")
    args = parser.parse_args(argv)

    if args.list:
        total, table = h6_seconds()
        print(json.dumps({"total_seconds": total, "table": table}, indent=2))
        return 0

    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    from computer_use_mcp.backend import LocalComputerBackend

    backend = LocalComputerBackend()
    backend.observe()  # warm capture + fix the coordinate context (physical pixels)
    results = {}
    for task in tasks:
        print(f"[{task}] replaying...", flush=True)
        started = datetime.now(UTC).isoformat()
        try:
            results[task] = run_one(task, backend)
            results[task]["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 - a failed replay must not lose the others
            results[task] = {"task": task, "method": "replayed", "status": "failed",
                             "error": f"{type(exc).__name__}: {exc}"}
        results[task]["replayed_at_utc"] = started
        print(f"[{task}] -> {json.dumps({k: results[task].get(k) for k in ('status', 'seconds', 'method')})}",
              flush=True)
    payload = {
        "disclaimer": DISCLAIMER,
        "generated_utc": datetime.now(UTC).isoformat(),
        "calibration": {
            "fitts_click_s": CLICK_S, "char_s": CHAR_S, "dialog_wait_s": DIALOG_S,
            "keypress_s": KEYPRESS_S, "drag_s": DRAG_S,
            "cognitive_allowances_s": COGNITIVE,
            "source": "provisional mission cadence; reconcile with evidence/perf-004/p1/cadence.json when it lands",
        },
        "reconciliation_with_a3_cadence": {
            "status": "these references use the mission-issued provisional cadence, which equals "
                      "A3's FAST BOUNDS; A3's conservative human table is ~1.5-2x higher",
            "a3_conservative_s": {"click_acquire": 1.2, "type_per_char": 0.28,
                                  "hotkey_chord": 1.9, "drag_400px": 1.8, "app_launch_wait": 1.0},
            "a3_fast_bound_s": {"click_acquire": 0.6, "type_per_char": 0.12,
                                "hotkey_chord": 0.6, "drag_400px": 1.2, "app_launch_wait": 1.0},
            "used_here_s": {"click": CLICK_S, "char": CHAR_S, "keypress": KEYPRESS_S,
                            "drag": DRAG_S, "dialog": DIALOG_S},
            "note": "ratio-sensitive consumers should re-run make_references.py with conservative "
                    "constants or scale per-action; app latencies (cold starts, page loads) are REAL "
                    "measured times in the replays and dominate h1/h4",
        },
        "machine": {"platform": sys.platform, "screen": "1920x1080 @ 125%"},
        "tasks": results,
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE.parent / "human-references.json"
    # MERGE semantics: per-task replay results accumulate across invocations so a single
    # task can be re-measured without discarding the others (each carries its own
    # replayed_at_utc timestamp).
    previous_tasks: dict[str, Any] = {}
    if out.exists():
        try:
            previous_tasks = json.loads(out.read_text(encoding="utf-8")).get("tasks", {})
        except (OSError, json.JSONDecodeError):
            previous_tasks = {}
    previous_tasks.update(results)
    payload["tasks"] = previous_tasks
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"written {out}")
    return 0 if all(r.get("status") == "ok" for r in payload["tasks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
