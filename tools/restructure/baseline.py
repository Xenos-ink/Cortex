#!/usr/bin/env python
"""WP-R0 restructure baseline tool (stdlib only).

Captures the BEFORE picture of the tree so WP-R3 can re-run the same capture on
the restructured tree and diff the two outputs:

  python tools/restructure/baseline.py capture --out DIR [--only STAGES]
  python tools/restructure/baseline.py inventory --out FILE

capture stages (default all):
  nodeids       full collected test node-id list (pytest --collect-only -q)
  counts        pass/skip/fail counts from a full gate run
                (-W error::RuntimeWarning, tripwire armed, CUMCP_RUN_E2E unset)
  tools         tools/list payload byte-for-byte over the live stdio handshake
                (same message sequence as cli.probe_server) + sha256
  probe         cortex-mcp probe result (gate venv python, repo as cwd)
  audit         audit_shapes.json — per event_type: top-level keys and metadata
                keys; sources: an AST scan of all src emit/_audit call sites
                (the authoritative static source) UNION a dynamic fake-backend
                run that instruments AuditLogger.emit. No desktop contact.
  perf          perf.json — TWO full runs, each with (i) server import time
                N=30 fresh child interpreters, (ii) benchmarks/r5_speed_profile
                N=30 iterations (screen READING only; it injects no input),
                (iii) the offline fake-backend perf harness (captures/action +
                Metrics p50/p95); plus the computed run-to-run spread.
  wheel         wheel_files.txt — sorted file list of a wheel built into
                DIR/wheel + console-script entry points; build residue in the
                tree (build/, src/*.egg-info) is removed and recorded.

inventory: for every top-level def/class/assignment in every module under
src/computer_use_mcp/: module, qualname, kind, and a normalized AST hash.

NORMALIZATION (exact rules):
  1. Only the node's own AST subtree is hashed.
  2. Every Import / ImportFrom node anywhere in the subtree is REMOVED.
  3. Docstrings are KEPT (they are ordinary Expr(Constant str) nodes).
  4. Line/column info is DROPPED (ast.dump with include_attributes=False).
  5. Module-qualified references are normalized: any Name/Attribute chain
     whose dotted path names the package itself ("computer_use_mcp"), one of
     its modules/subpackages ("computer_use_mcp.limits"), or an import alias
     bound to such a module ("import computer_use_mcp.server as srv" binds
     "srv"; "from computer_use_mcp import server" binds "server") is replaced
     by the remainder of the chain after the module prefix is stripped —
     "computer_use_mcp.limits.Limits" and "srv._f" become "Limits" / "_f".
     Chains that do not resolve to a src module are left untouched.
  6. The normalized subtree is serialized with ast.dump and hashed with
     sha256 (hex digest).
Consequence: moving a symbol between modules keeps its hash; import-line-only
edits keep it; editing the body or the docstring changes it.

Perf provenance: every perf item records machine, python version, timestamp
and the exact child command.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
PKG = SRC / "computer_use_mcp"
DEFAULT_PY = Path(r"C:/Users/HP/AppData/Local/Temp/orvex-108/gate-venv/Scripts/python.exe")
PERF_HARNESS = REPO / ".orvex" / "mission" / "artifacts" / "gates" / "perf_harness.py"
R5 = REPO / "benchmarks" / "r5_speed_profile.py"
EXPECTED_TOOL_COUNT = 10


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("CUMCP_RUN_E2E", None)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def meta(command: list[str]) -> dict:
    return {
        "machine": platform.platform(),
        "python": sys.version.replace("\n", " "),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "command": command,
    }


def run(cmd: list[str], timeout: float, cwd: Path = REPO) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), env=child_env(), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def percentile(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def stats_of(xs: list[float]) -> dict:
    return {"n": len(xs), "median": statistics.median(xs), "p95": percentile(xs, 0.95), "min": min(xs)}


# ---------------------------------------------------------------------------
# nodeids / counts
# ---------------------------------------------------------------------------

def stage_nodeids(py: Path, out: Path) -> dict:
    cmd = [str(py), "-m", "pytest", "tests", "--collect-only", "-q",
           "-p", "no:cacheprovider"]
    proc = run(cmd, timeout=900)
    ids = sorted({
        s.strip() for s in proc.stdout.splitlines()
        if ("::" in s or s.strip().endswith(".py"))
        and not s.strip().startswith("=")
        and "collected" not in s
    })
    (out / "nodeids.txt").write_text("\n".join(ids) + "\n", encoding="utf-8")
    result = {"count": len(ids), **meta(cmd), "returncode": proc.returncode}
    if proc.returncode != 0:
        result["stderr_tail"] = proc.stderr[-2000:]
    return result


def parse_pytest_counts(text: str) -> dict:
    tail = text.strip().splitlines()[-1] if text.strip() else ""
    counts: dict = {}
    for token, key in (("passed", "passed"), ("failed", "failed"), ("skipped", "skipped"),
                       ("error", "errors"), ("warning", "warnings"), ("xpassed", "xpassed"),
                       ("xfailed", "xfailed"), ("deselected", "deselected")):
        mm = re.findall(r"(\d+) " + token, tail)
        if mm:
            counts[key] = sum(int(x) for x in mm)
    return counts


def stage_counts(py: Path, out: Path) -> dict:
    cmd = [str(py), "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider",
           "-rfEx", "-W", "error::RuntimeWarning"]
    proc = run(cmd, timeout=14400)
    counts = parse_pytest_counts(proc.stdout)
    payload = {"counts": counts, "summary_line": proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "",
               **meta(cmd), "returncode": proc.returncode}
    if proc.returncode != 0:
        payload["short_summary_tail"] = "\n".join(
            ln for ln in proc.stdout.splitlines() if ln.startswith("FAILED") or ln.startswith("ERROR"))[-4000:]
        payload["stderr_tail"] = proc.stderr[-2000:]
    (out / "counts.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


# ---------------------------------------------------------------------------
# tool schemas (live stdio handshake, mirroring cli.probe_server)
# ---------------------------------------------------------------------------

def stage_tools(py: Path, out: Path) -> dict:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "cortex-mcp-probe", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    stdin_payload = "\n".join(json.dumps(m) for m in messages) + "\n"
    cmd = [str(py), "-m", "computer_use_mcp.server"]
    proc = subprocess.Popen(
        cmd, cwd=str(REPO), env=child_env(), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace",
    )
    collected: list[str] = []
    done = threading.Event()

    def reader() -> None:
        try:
            collected.extend(proc.stdout)
        except (OSError, ValueError):
            pass
        finally:
            done.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    raw_line = None
    try:
        try:
            proc.stdin.write(stdin_payload)
            proc.stdin.flush()
        except (OSError, ValueError):
            pass
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            for line in collected:
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == 2:
                    raw_line = line
                    break
            if raw_line is not None or done.is_set():
                break
            time.sleep(0.02)
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        t.join(timeout=2.0)
    if raw_line is None:
        raise RuntimeError("no tools/list response from server")
    raw_line = raw_line.rstrip("\n")
    payload = json.loads(raw_line)
    tools = payload["result"]["tools"]
    if len(tools) != EXPECTED_TOOL_COUNT:
        raise RuntimeError(f"expected {EXPECTED_TOOL_COUNT} tools, got {len(tools)}: "
                           f"{sorted(t_.get('name') for t_ in tools)}")
    blob = {"sha256": hashlib.sha256(raw_line.encode("utf-8")).hexdigest(),
            "tool_count": len(tools),
            "tool_names": sorted(t_["name"] for t_ in tools),
            "raw_response_line": raw_line,
            "result": payload["result"],
            **meta(cmd)}
    (out / "tool_schemas.json").write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")
    return {"tool_count": len(tools), "sha256": blob["sha256"]}


# ---------------------------------------------------------------------------
# probe (existing cortex-mcp probe semantics)
# ---------------------------------------------------------------------------

def stage_probe(py: Path, out: Path) -> dict:
    cmd = [str(py), "-m", "computer_use_mcp.cli", "probe",
           "--python", str(py), "--cwd", str(REPO)]
    proc = run(cmd, timeout=180)
    (out / "probe.txt").write_text(proc.stdout, encoding="utf-8")
    ok = proc.returncode == 0 and "PROBE PASS" in proc.stdout
    result = {"ok": ok, **meta(cmd), "returncode": proc.returncode}
    if not ok:
        result["stderr_tail"] = proc.stderr[-2000:]
    return result


# ---------------------------------------------------------------------------
# audit shapes
# ---------------------------------------------------------------------------

VOCAB_FALLBACK = {
    "observation", "model_decision", "grounding", "validation", "safety",
    "approval", "execution", "verification", "recovery", "failure", "stop",
    "emergency_stop", "limit_exceeded", "session_start", "session_stop",
    "subtask_created", "subtask_started", "subtask_completed", "subtask_failed",
    "subtask_paused", "replan", "checkpoint", "resume", "approval_epoch",
    "health_check", "queue", "plan_start", "plan_point", "plan_complete",
    "plan_stopped",
}


def audit_vocabulary() -> set[str]:
    try:
        from typing import get_args
        from computer_use_mcp.audit import AuditEventType
        vocab = set(get_args(AuditEventType))
        if vocab:
            return vocab
    except Exception:
        pass
    return set(VOCAB_FALLBACK)


def _static_top_level_keys() -> list[str]:
    try:
        from computer_use_mcp.audit import AuditEvent
        return sorted(AuditEvent.model_fields.keys())
    except Exception:
        return sorted(["timestamp", "session_id", "task_id", "observation_id",
                       "action_id", "event_type", "active_app", "risk", "result",
                       "duration_ms", "metadata"])


def _call_is_emit(call: ast.Call) -> bool:
    f = call.func
    return (isinstance(f, ast.Attribute) and f.attr in ("emit", "_audit")) or \
           (isinstance(f, ast.Name) and f.id in ("emit", "_audit"))


def _first_arg_string(call: ast.Call) -> str | None:
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    return None


def _variable_first_arg_name(call: ast.Call) -> str | None:
    if call.args and isinstance(call.args[0], ast.Name):
        return call.args[0].id
    return None


def ast_scan_audit(vocab: set[str]) -> tuple[dict, dict]:
    """Scan src for emit/_audit call sites.

    Returns ({event_type: {"top_level_keys", "metadata_keys", "modules"}},
             {event_type: [modules]}) for variable-arg emit sites whose loop
    iterates a literal tuple of vocabulary strings.
    """
    shapes: dict[str, dict] = {}
    for path in sorted(PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        mod = _dotted(path.relative_to(SRC).with_suffix(""))
        for call in [c for c in ast.walk(tree) if isinstance(c, ast.Call) and _call_is_emit(c)]:
            et = _first_arg_string(call)
            if et is None:
                continue
            # agent._audit(event_type, *, observation=, action=, result=,
            # duration_ms=, metadata=, **extra): the wrapper maps observation/
            # action to *_id and merges **extra into metadata — so extra kwargs
            # are METADATA keys, not top-level event fields.
            wrapper = getattr(call.func, "attr", getattr(call.func, "id", "")) == "_audit"
            entry = shapes.setdefault(et, {"top_level_keys": set(), "metadata_keys": set(), "modules": set()})
            entry["modules"].add(mod)
            for kw in call.keywords:
                if kw.arg == "metadata" and isinstance(kw.value, ast.Dict):
                    for k in kw.value.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            entry["metadata_keys"].add(k.value)
                elif kw.arg is None:
                    continue
                elif wrapper:
                    if kw.arg not in ("observation", "action", "result", "duration_ms", "metadata"):
                        entry["metadata_keys"].add(kw.arg)
                else:
                    entry["top_level_keys"].add(kw.arg)
        # variable first arg emitted from a literal vocabulary-tuple loop
        for fornode in [n for n in ast.walk(tree)
                        if isinstance(n, ast.For)
                        and isinstance(n.iter, ast.Tuple)
                        and all(isinstance(e, ast.Constant) and isinstance(e.value, str) and e.value in vocab
                                for e in n.iter.elts)
                        and isinstance(n.target, ast.Name)]:
            var = fornode.target.id
            for call in [c for c in ast.walk(fornode) if isinstance(c, ast.Call) and _call_is_emit(c)]:
                if _variable_first_arg_name(call) == var:
                    et = None
                    for e in fornode.iter.elts:
                        et = e.value  # shape identical across the tuple; keep last
                    entry = shapes.setdefault(et, {"top_level_keys": set(), "metadata_keys": set(), "modules": set()})
                    entry["modules"].add(mod + " (via loop variable)")
                    for kw in call.keywords:
                        if kw.arg == "metadata" and isinstance(kw.value, ast.Name):
                            pass  # dynamic metadata dict; shapes come from dynamic capture
                        elif kw.arg == "metadata" and isinstance(kw.value, ast.Dict):
                            for k in kw.value.keys:
                                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                                    entry["metadata_keys"].add(k.value)
                        elif kw.arg is not None:
                            entry["top_level_keys"].add(kw.arg)
    return shapes, {}


def dynamic_audit_shapes() -> tuple[dict[str, dict], list[str]]:
    """Instrument AuditLogger.emit and drive offline fake-backend scenarios.

    Returns ({event_type: {"top_level_keys", "metadata_keys"}}, notes).
    """
    notes: list[str] = []
    import asyncio
    import base64
    import io
    from types import SimpleNamespace

    from PIL import Image

    from computer_use_mcp.agent import ComputerUseAgent
    from computer_use_mcp.audit import AuditLogger, Metrics
    from computer_use_mcp.backend import FakeComputerBackend
    from computer_use_mcp.limits import Limits
    from computer_use_mcp.models import GroundedAction, WindowInfo
    from computer_use_mcp.observation import ObservationEngine
    from computer_use_mcp.safety import SafetyPolicy
    from computer_use_mcp.state import StopToken, TaskState

    recorded: dict[str, dict] = {}
    orig_emit = AuditLogger.emit

    def recording_emit(self, event_type, session_id, **fields):  # type: ignore[no-untyped-def]
        entry = recorded.setdefault(str(event_type), {"top_level_keys": set(), "metadata_keys": set()})
        entry["top_level_keys"].update(["session_id"])
        md = fields.pop("metadata", None)
        entry["top_level_keys"].update(fields.keys())
        if isinstance(md, dict):
            entry["metadata_keys"].update(md.keys())
        return orig_emit(self, event_type, session_id, **fields)

    AuditLogger.emit = recording_emit  # type: ignore[assignment]
    try:
        win = WindowInfo(hwnd=12, pid=500, process_name="notepad.exe",
                         window_class="Notepad", title="Untitled - Notepad")
        flows = [
            GroundedAction(action="hotkey", keys=["win", "r"], confidence=1.0),
            GroundedAction(action="type", text="notepad", confidence=1.0),
            GroundedAction(action="keypress", keys=["enter"], confidence=1.0),
        ]

        class AltBackend(FakeComputerBackend):
            def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
                super().__init__(**kwargs)

            def observe(self, monitor_index=None):  # type: ignore[no-untyped-def]
                obs = super().observe(monitor_index)
                obs.active_window = win.title
                obs.active_window_info = win.model_copy()
                img = Image.new("RGB", (self.width, self.height),
                                "white" if len(self.executed) % 2 == 0 else "black")
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                obs.image_base64 = base64.b64encode(buf.getvalue()).decode("ascii")
                return obs

        backend = AltBackend()
        backend.set_windows([win])
        agent = ComputerUseAgent(
            backend, provider=None, safety=SafetyPolicy(), task=TaskState(), stop=StopToken(),
            limits=Limits(max_actions=64, max_task_seconds=3600.0).validate(),
            metrics=Metrics(),
        )
        agent.observation = ObservationEngine(backend)
        state = SimpleNamespace(
            dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
            max_steps=64, step_count=0, require_approval=False, max_retries_per_action=1,
        )
        for action in flows:
            try:
                asyncio.run(agent.run_single(state, action))
            except Exception as exc:  # scenarios must not abort the capture
                notes.append(f"scenario {action.action}: {type(exc).__name__}: {exc}")
        # failure path: confidence below min_confidence -> validation/failure events
        state.min_confidence = 0.99
        try:
            asyncio.run(agent.run_single(
                state, GroundedAction(action="click", x=10, y=10, confidence=0.5)))
        except Exception as exc:
            notes.append(f"scenario low-confidence click: {type(exc).__name__}: {exc}")
        state.min_confidence = 0.0
    finally:
        AuditLogger.emit = orig_emit  # type: ignore[assignment]
    return recorded, notes


def stage_audit(py: Path, out: Path) -> dict:
    if Path(sys.executable).resolve() != py.resolve():
        # the dynamic half needs the product importable in THIS interpreter
        raise RuntimeError(f"audit stage must run under the gate python ({py}); "
                           f"re-run capture with --python {py}")
    vocab = audit_vocabulary()
    static_scan, _ = ast_scan_audit(vocab)
    dynamic, notes = dynamic_audit_shapes()
    all_types = sorted(vocab)
    shapes = {}
    for et in all_types:
        top = set(_static_top_level_keys())
        md: set[str] = set()
        sources = []
        if et in static_scan:
            top |= static_scan[et]["top_level_keys"]
            md |= static_scan[et]["metadata_keys"]
            sources.append("ast:" + ",".join(sorted(static_scan[et]["modules"])))
        if et in dynamic:
            top |= dynamic[et]["top_level_keys"]
            md |= dynamic[et]["metadata_keys"]
            sources.append("dynamic:fake-backend-flow")
        shapes[et] = {
            "top_level_keys": sorted(top),
            "metadata_keys": sorted(md),
            "sources": sources,
            "unseen_statically_and_dynamically": not sources,
        }
    blob = {
        "method": ("AST scan of every emit/_audit call site under src/computer_use_mcp "
                   "(authoritative static source; every emission passes through "
                   "AuditLogger.emit) UNION a dynamic fake-backend battery that "
                   "instruments AuditLogger.emit at runtime. No desktop contact."),
        "static_top_level_keys": _static_top_level_keys(),
        "notes": notes,
        "shapes": shapes,
        **meta([sys.executable] + sys.argv[1:]),
    }
    (out / "audit_shapes.json").write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")
    return {"event_types": len(shapes), "dynamic_types": len(dynamic), "notes": notes}


# ---------------------------------------------------------------------------
# perf
# ---------------------------------------------------------------------------

IMPORT_CHILD = '''"""Spawned by tools/restructure/baseline.py — fresh-interpreter import time."""
import time

t0 = time.perf_counter()
import computer_use_mcp.server  # noqa: E402,F401

print(time.perf_counter() - t0)
'''


def stage_perf(py: Path, out: Path, runs: int) -> dict:
    child_path = out / "_import_time_child.py"
    if not child_path.exists():
        child_path.write_text(IMPORT_CHILD, encoding="utf-8")

    def one_run(idx: int) -> dict:
        run_block: dict = {"run": idx}

        # (i) server import time, N=30 fresh child interpreters
        cmd_i = [str(py), str(child_path)]
        xs = []
        for _ in range(30):
            proc = run(cmd_i, timeout=120)
            if proc.returncode != 0:
                raise RuntimeError(f"import-time child failed: {proc.stderr[-500:]}")
            xs.append(float(proc.stdout.strip().splitlines()[-1]) * 1000.0)
        run_block["server_import_time_ms"] = {**stats_of(xs), **meta(cmd_i)}

        # (ii) r5 profile, N=30 iterations — screen READING only (no input injection).
        # Runs the signature-correct mirror tools/restructure/r5_profile_mirror.py:
        # identical stages/order to benchmarks/r5_speed_profile.py, whose stage E
        # unpacks a stale 2-tuple of server._bound_outbound_image (now a 3-tuple);
        # benchmarks/ is frozen for WP-R0, so the mirror adapts only that unpack.
        r5_json = out / f"_r5_run{idx}.json"
        cmd_ii = [str(py), str(REPO / "tools" / "restructure" / "r5_profile_mirror.py"),
                  "--iterations", "30", "--json", str(r5_json)]
        proc = run(cmd_ii, timeout=3600)
        if proc.returncode != 0:
            raise RuntimeError(f"r5 failed: {proc.stderr[-800:]}")
        run_block["r5_profile"] = {**json.loads(r5_json.read_text(encoding="utf-8")), **meta(cmd_ii)}

        # (iii) offline fake-backend harness: captures/action + Metrics p50/p95
        cmd_iii = [str(py), str(PERF_HARNESS), "--repeats", "30"]
        proc = run(cmd_iii, timeout=1200)
        if proc.returncode != 0:
            raise RuntimeError(f"perf harness failed: {proc.stderr[-800:]}")
        harness = json.loads(proc.stdout[proc.stdout.index("{"):proc.stdout.rindex("}") + 1])
        run_block["captures_per_action_and_metrics"] = {**harness, **meta(cmd_iii)}
        return run_block

    all_runs = [one_run(i + 1) for i in range(runs)]
    spread: dict = {"_note": "abs diff and relative % of run2 vs run1 (relative to run1); "
                             "WP-R3's +2% ceiling is judged against this noise"}
    if len(all_runs) >= 2:
        a, b = all_runs[0], all_runs[1]
        imp_a = a["server_import_time_ms"]
        imp_b = b["server_import_time_ms"]
        spread["server_import_time_ms"] = {
            k: {"abs": imp_b[k] - imp_a[k],
                "pct": (imp_b[k] - imp_a[k]) / imp_a[k] * 100.0 if imp_a[k] else None}
            for k in ("median", "p95", "min")}
        r5_spread = {}
        for stage, st in a["r5_profile"].items():
            if isinstance(st, dict) and "p50" in st and st.get("n") and stage in b["r5_profile"]:
                stb = b["r5_profile"][stage]
                r5_spread[stage] = {
                    "median_abs": stb["p50"] - st["p50"],
                    "median_pct": (stb["p50"] - st["p50"]) / st["p50"] * 100.0 if st["p50"] else None,
                    "p95_abs": stb["p95"] - st["p95"],
                    "p95_pct": (stb["p95"] - st["p95"]) / st["p95"] * 100.0 if st["p95"] else None,
                }
        spread["r5_profile"] = r5_spread
        h_a = a["captures_per_action_and_metrics"]
        h_b = b["captures_per_action_and_metrics"]
        spread["captures_per_action"] = {"abs": h_b["captures_per_action"] - h_a["captures_per_action"]}
        m_spread = {}
        lat_a = h_a["metrics_snapshot"].get("latencies", {})
        lat_b = h_b["metrics_snapshot"].get("latencies", {})
        for key, val in lat_a.items():
            if isinstance(val, dict) and val.get("p50_ms"):
                val_b = lat_b.get(key, {})
                m_spread[key] = {
                    "p50_abs": val_b.get("p50_ms", 0) - val["p50_ms"],
                    "p50_pct": (val_b.get("p50_ms", 0) - val["p50_ms"]) / val["p50_ms"] * 100.0,
                    "p95_abs": (val_b.get("p95_ms") or 0) - (val.get("p95_ms") or 0),
                    "p95_pct": ((val_b.get("p95_ms") or 0) - (val.get("p95_ms") or 0)) / val["p95_ms"] * 100.0
                    if val.get("p95_ms") else None,
                }
        spread["metrics_latencies"] = m_spread
        cnt_a = h_a["metrics_snapshot"].get("counters", {})
        cnt_b = h_b["metrics_snapshot"].get("counters", {})
        spread["metrics_counters_diff"] = {k: cnt_b.get(k) - v for k, v in cnt_a.items()}
    blob = {"runs": runs, "run_data": all_runs, "spread": spread,
            "r5_note": ("benchmarks/r5_speed_profile.py has no fake/offline path; it is "
                        "screen-READING-only (mss grab + PNG encode + digest + verify diff + "
                        "outbound bound; zero input injection, zero launches). Its stage E "
                        "crashes against the current 3-tuple _bound_outbound_image signature "
                        "(ValueError: too many values to unpack), and benchmarks/ is frozen "
                        "for WP-R0, so capture runs tools/restructure/r5_profile_mirror.py — "
                        "identical stages and order, only that unpack adapted."),
            "harness_note": (".orvex/mission/artifacts/gates/perf_harness.py — the existing "
                             "offline fake-backend harness (captures/action + Metrics p50/p95).")}
    (out / "perf.json").write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")
    for tmp in out.glob("_r5_run*.json"):
        tmp.unlink()
    return {"runs": runs, "stages": list(all_runs[0].keys())}


# ---------------------------------------------------------------------------
# wheel
# ---------------------------------------------------------------------------

def stage_wheel(out: Path) -> dict:
    wheel_dir = out / "wheel"
    wheel_dir.mkdir(parents=True, exist_ok=True)
    for old in wheel_dir.glob("*.whl"):
        old.unlink()
    cmd = ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)]
    proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=1200)
    if proc.returncode != 0:
        raise RuntimeError(f"uv build failed: {proc.stderr[-1500:]}")
    wheels = sorted(wheel_dir.glob("*.whl"))
    if not wheels:
        raise RuntimeError("uv build produced no wheel")
    whl = wheels[0]
    names = sorted(zipfile.ZipFile(whl).namelist())
    entry_points: list[str] = []
    with zipfile.ZipFile(whl) as zf:
        for name in names:
            if name.endswith("entry_points.txt"):
                entry_points = [ln for ln in zf.read(name).decode("utf-8").splitlines() if ln.strip()]
                break
    residue_found: list[str] = []
    residue_removed: list[str] = []
    for cand in [REPO / "build"] + sorted(SRC.glob("*.egg-info")) + sorted(SRC.glob("computer_use_mcp.egg-info")):
        if cand.exists():
            residue_found.append(str(cand.relative_to(REPO)))
    for cand in [REPO / "build"] + list(SRC.glob("*.egg-info")):
        if cand.exists():
            shutil.rmtree(cand, ignore_errors=True)
            if not cand.exists():
                residue_removed.append(str(cand.relative_to(REPO)))
    lines = names + ["", "# console-script entry points (from wheel metadata entry_points.txt)"] + entry_points
    (out / "wheel_files.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"wheel": whl.name, "files": len(names), "entry_points": entry_points,
            "build_residue_found_in_tree": residue_found,
            "build_residue_removed": residue_removed, **meta(cmd)}


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------

def _module_chain(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class _Normalizer(ast.NodeTransformer):
    def __init__(self, module_ids: set[str], aliases: dict[str, str]) -> None:
        self.module_ids = module_ids
        self.aliases = aliases

    def strip_module(self, dotted: str) -> str | None:
        best = None
        for mid in self.module_ids:
            if dotted == mid or dotted.startswith(mid + "."):
                if best is None or len(mid) > len(best):
                    best = mid
        if best is None:
            return None
        return "" if dotted == best else dotted[len(best) + 1:]

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        dotted = _module_chain(node)
        if dotted is not None:
            first, _sep, tail = dotted.partition(".")
            if first in self.aliases:
                dotted = self.aliases[first] + (("." + tail) if tail else "")
            rest = self.strip_module(dotted)
            if rest is not None:
                parts = rest.split(".") if rest else dotted.rsplit(".", 1)[-1:]
                new_node: ast.AST = ast.Name(id=parts[0], ctx=ast.Load())
                for part in parts[1:]:
                    new_node = ast.Attribute(value=new_node, attr=part, ctx=ast.Load())
                ast.copy_location(new_node, node)
                return new_node
        self.generic_visit(node)
        return node

    def visit_Import(self, node: ast.Import) -> None:
        return None

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        return None


def _alias_map(tree: ast.Module, module_ids: set[str], module_dotted: str = "") -> dict[str, str]:
    """Alias/binding -> resolved module dotted name, for src-module imports only.

    ``module_dotted`` is the dotted name of the module being parsed; relative
    imports (level >= 1) resolve against its package chain.
    """
    parts = module_dotted.split(".") if module_dotted else []

    def resolve(level: int, module: str | None) -> str | None:
        if level == 0:
            return module
        base_parts = parts[: max(len(parts) - (level - 1), 0)]
        if module:
            base_parts = base_parts + [module]
        return ".".join(base_parts) if base_parts else None

    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in module_ids and a.asname:
                    aliases[a.asname] = a.name
        elif isinstance(node, ast.ImportFrom):
            base = resolve(node.level, node.module)
            if base and base in module_ids:
                for a in node.names:
                    target = f"{base}.{a.name}"
                    if target in module_ids:
                        aliases[a.asname or a.name] = target
    return aliases


def _dotted(rel: Path) -> str:
    return rel.as_posix().replace("/", ".")


def collect_module_ids() -> set[str]:
    ids = {"computer_use_mcp"}
    for path in PKG.rglob("*.py"):
        if path.name == "__init__.py":
            ids.add(_dotted(path.parent.relative_to(SRC)))
        else:
            ids.add(_dotted(path.relative_to(SRC).with_suffix("")))
    return ids


def normalized_hash(node: ast.AST, module_ids: set[str], aliases: dict[str, str]) -> str:
    clone = copy.deepcopy(node)  # deep copy independent of parents
    clone = _Normalizer(module_ids, aliases).visit(clone)
    ast.fix_missing_locations(clone)
    dump = ast.dump(clone, include_attributes=False)
    return hashlib.sha256(dump.encode("utf-8")).hexdigest()


def top_level_symbols(tree: ast.Module) -> list[tuple[str, str, ast.AST]]:
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.name, "def", node))
        elif isinstance(node, ast.ClassDef):
            out.append((node.name, "class", node))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.append((t.id, "assign", node))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.append((node.target.id, "assign", node))
    return out


def cmd_inventory(args: argparse.Namespace) -> int:
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    module_ids = collect_module_ids()
    entries = []
    for path in sorted(PKG.rglob("*.py")):
        if path.name == "__init__.py":
            mod = _dotted(path.parent.relative_to(SRC))
        else:
            mod = _dotted(path.relative_to(SRC).with_suffix(""))
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        aliases = _alias_map(tree, module_ids, mod)
        for name, kind, node in top_level_symbols(tree):
            entries.append({
                "module": mod,
                "name": name,
                "kind": kind,
                "hash": normalized_hash(node, module_ids, aliases),
            })
    blob = {
        "normalization": ("sha256 of ast.dump(include_attributes=False) of the symbol's own "
                          "subtree; Import/ImportFrom nodes removed; docstrings kept; every "
                          "dotted reference resolving to computer_use_mcp or any of its "
                          "modules/subpackages (or an alias bound to one) stripped of the "
                          "module prefix; line/col dropped"),
        "module_count": len({e["module"] for e in entries}),
        "symbol_count": len(entries),
        "symbols": entries,
        **meta([sys.executable] + sys.argv[1:]),
    }
    out_path.write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")
    print(f"inventory: {len(entries)} symbols in {blob['module_count']} modules -> {out_path}")
    return 0


# ---------------------------------------------------------------------------
# capture driver
# ---------------------------------------------------------------------------

STAGES = ("nodeids", "counts", "tools", "probe", "audit", "perf", "wheel")


def cmd_capture(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    py = Path(args.python).resolve() if args.python else DEFAULT_PY
    if not py.exists():
        raise SystemExit(f"python not found: {py}")
    wanted = STAGES if not args.only else tuple(s.strip() for s in args.only.split(","))
    manifest: dict = {"repo": str(REPO), "git_head": _git_head(), "stages": {}}
    for stage in wanted:
        if stage not in STAGES:
            raise SystemExit(f"unknown stage: {stage}")
        print(f"[capture] {stage} ...", flush=True)
        t0 = time.perf_counter()
        if stage == "nodeids":
            res = stage_nodeids(py, out)
        elif stage == "counts":
            res = stage_counts(py, out)
        elif stage == "tools":
            res = stage_tools(py, out)
        elif stage == "probe":
            res = stage_probe(py, out)
        elif stage == "audit":
            res = stage_audit(py, out)
        elif stage == "perf":
            res = stage_perf(py, out, runs=args.perf_runs)
        elif stage == "wheel":
            res = stage_wheel(out)
        else:  # pragma: no cover
            raise AssertionError(stage)
        res["seconds"] = round(time.perf_counter() - t0, 1)
        manifest["stages"][stage] = res
        print(f"[capture] {stage} done: {json.dumps({k: v for k, v in res.items() if k != 'stderr_tail'}, default=str)[:300]}", flush=True)
    (out / "capture_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"[capture] manifest -> {out / 'capture_manifest.json'}")
    return 0


def _git_head() -> str:
    try:
        proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO),
                              capture_output=True, text=True, timeout=30)
        return proc.stdout.strip()
    except Exception:
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baseline")
    sub = parser.add_subparsers(dest="command", required=True)
    p_cap = sub.add_parser("capture", help="capture the baseline picture into DIR")
    p_cap.add_argument("--out", required=True)
    p_cap.add_argument("--only", default=None, help="comma list of: " + ",".join(STAGES))
    p_cap.add_argument("--python", default=None, help="gate venv python (default: orvex-108 gate)")
    p_cap.add_argument("--perf-runs", type=int, default=2)
    p_cap.set_defaults(func=cmd_capture)
    p_inv = sub.add_parser("inventory", help="write the symbol inventory to FILE")
    p_inv.add_argument("--out", required=True)
    p_inv.set_defaults(func=cmd_inventory)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
