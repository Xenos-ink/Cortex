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
src/computer_use_mcp/: module, qualname, kind, and a normalized AST hash;
PLUS every top-level statement that is not a def/class/import/simple assign
(If/Try/With/For/While/Expr/AugAssign/tuple-target Assign/__all__) with its
module, ordinal among that module's top-level statements, kind, and the same
normalized hash — so a dropped, duplicated or reordered import-time statement
is visible in the diff (WP-R3 method change 2).

NORMALIZATION (exact rules):
  1. Only the node's own AST subtree is hashed.
  2. Every Import / ImportFrom node anywhere in the subtree is REMOVED.
  3. Docstrings are KEPT (they are ordinary Expr(Constant str) nodes).
  4. Line/column info is DROPPED (ast.dump with include_attributes=False).
  5. Module-qualified references are normalized: any Name/Attribute chain
     whose dotted path names the package itself ("computer_use_mcp"), one of
     its modules/subpackages ("computer_use_mcp.core.limits"), or an import alias
     bound to such a module ("import computer_use_mcp.server as srv" binds
     "srv"; "from computer_use_mcp import server" binds "server") is replaced
     by the remainder of the chain after the module prefix is stripped —
     "computer_use_mcp.core.limits.Limits" and "srv._f" become "Limits" / "_f".
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
        from computer_use_mcp.security.audit import AuditEventType
        vocab = set(get_args(AuditEventType))
        if vocab:
            return vocab
    except Exception:
        pass
    return set(VOCAB_FALLBACK)


def _static_top_level_keys() -> list[str]:
    try:
        from computer_use_mcp.security.audit import AuditEvent
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

    from computer_use_mcp.pipeline.agent import ComputerUseAgent
    from computer_use_mcp.security.audit import AuditLogger, Metrics
    from computer_use_mcp.desktop.fake_backend import FakeComputerBackend
    from computer_use_mcp.core.limits import Limits
    from computer_use_mcp.core.models import GroundedAction, WindowInfo
    from computer_use_mcp.vision.observation import ObservationEngine
    from computer_use_mcp.security.safety import SafetyPolicy
    from computer_use_mcp.core.state import StopToken, TaskState

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
import computer_use_mcp.server.app  # noqa: E402,F401  (D6: the registration surface)

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
    # n3: --offline guarantees no network; it requires the build dependencies
    # to already be in the local uv/build cache (they are, from WP-R0 onward)
    cmd = ["uv", "build", "--wheel", "--offline", "--out-dir", str(wheel_dir)]
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


def _is_docstring_stmt(node: ast.stmt) -> bool:
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def _safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - exotic target shapes
        return "?"


def top_level_statements(tree: ast.Module) -> list[tuple[int, str, str, ast.AST]]:
    """(ordinal, kind, name, node) for every top-level statement that is NOT a
    def/class/Import/simple single-Name assign and NOT the module docstring —
    If/Try/With/For/While/Expr/AugAssign/tuple-or-multi-target Assign and the
    ``__all__`` assign (WP-R3 method change 2: a dropped, duplicated or
    reordered import-time statement must be VISIBLE in the inventory diff).
    The ordinal is the statement's index among ALL of the module's top-level
    statements (defs and simple assigns included), so relative order to the
    surrounding definitions is checked too."""
    out: list[tuple[int, str, str, ast.AST]] = []
    for ordinal, node in enumerate(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Import, ast.ImportFrom)):
            continue
        if _is_docstring_stmt(node):
            continue
        kind = type(node).__name__
        name = ""
        if isinstance(node, ast.Assign):
            single = (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name))
            if single and node.targets[0].id != "__all__":
                continue  # a simple assign — covered by the symbol inventory
            name = "__all__" if single else (
                "assign:" + "+".join(sorted(_safe_unparse(t) for t in node.targets))
            )
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                continue
            name = "ann:" + _safe_unparse(node.target)
        out.append((ordinal, kind, name, node))
    return out


def statement_entries(tree: ast.Module, module_ids: set[str], aliases: dict[str, str]) -> list[dict]:
    """Hash-normalized inventory entries for a module's non-simple statements.

    F4 (VAL-WP-R2a-0-01): every statement gets a stable, ADDRESSABLE name
    ``stmt@<hash8>`` (the first 8 hex of its normalized hash) so a symbol-map
    can route individual statements — e.g. backend's ``if IS_WINDOWS:`` block
    to win32 and the three ``_VK_NAMED_KEYS.update(...)`` calls to
    input_engine — instead of one destination for all of a module's If/Expr
    statements. The name is stable under module moves (the hash is
    location-independent)."""
    entries: list[dict] = []
    for ordinal, kind, _label, node in top_level_statements(tree):
        digest = normalized_hash(node, module_ids, aliases)
        entries.append({
            "ordinal": ordinal, "kind": kind, "name": f"stmt@{digest[:8]}",
            "hash": digest,
        })
    return entries


def cmd_inventory(args: argparse.Namespace) -> int:
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    module_ids = collect_module_ids()
    entries = []
    statements = []
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
        for entry in statement_entries(tree, module_ids, aliases):
            statements.append({"module": mod, **entry})
    blob = {
        "normalization": ("sha256 of ast.dump(include_attributes=False) of the symbol's own "
                          "subtree; Import/ImportFrom nodes removed; docstrings kept; every "
                          "dotted reference resolving to computer_use_mcp or any of its "
                          "modules/subpackages (or an alias bound to one) stripped of the "
                          "module prefix; line/col dropped"),
        "module_count": len({e["module"] for e in entries}),
        "symbol_count": len(entries),
        "symbols": entries,
        "statements_note": ("every top-level statement that is not a def/class/import/"
                            "simple single-Name assign (If/Try/With/For/While/Expr/"
                            "AugAssign/tuple-target Assign/__all__), same normalized hash; "
                            "ordinal = index among ALL top-level statements of its module; "
                            "name = 'stmt@<hash8>' — a stable, addressable key for "
                            "--symbol-map routing (F4)"),
        "statement_count": len(statements),
        "statements": statements,
        **meta([sys.executable] + sys.argv[1:]),
    }
    out_path.write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")
    print(f"inventory: {len(entries)} symbols + {len(statements)} statements "
          f"in {blob['module_count']} modules -> {out_path}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    """Diff two inventory files (BEFORE vs AFTER) by (name, kind) across modules.

    Matching, in order:
      UNCHANGED  same (module, name, kind, hash) on both sides
      MOVED      same (name, kind, hash), module changed (a pure file move)
      CHANGED    same (module, name, kind) but different hash
      MISSING    in BEFORE only (no hash-equal match anywhere)
      ADDED      in AFTER only (no hash-equal match, not a CHANGED partner)
    With --move-table FILE (JSON: old module dotted name -> new module dotted
    name), a BEFORE symbol whose module is in the table is paired ONLY inside
    the module the table names (same leaf name, kind, hash); if it is found
    anywhere else the pair is reported in moved_mismatch and the command exits 1.
    With --symbol-map FILE (WP-R3 method change 2; JSON: "old.module" or
    "old.module:name" -> "new.module", or {"symbol_map": {...}}), every mapped
    BEFORE symbol AND every mapped BEFORE top-level statement must land in its
    mapped module (symbol_map_mismatch otherwise); additionally every top-level
    statement that stays in a module keeps its RELATIVE order
    (statement_order_violations otherwise); both fail the command (exit 1).
    Exit code 1 if any MISSING, CHANGED, moved_mismatch, symbol_map_mismatch,
    statement_missing or statement_order_violations entry exists.
    """
    before_blob = json.loads(Path(args.before).resolve().read_text(encoding="utf-8"))
    after_blob = json.loads(Path(args.after).resolve().read_text(encoding="utf-8"))
    before: list[dict] = before_blob["symbols"]
    after: list[dict] = after_blob["symbols"]
    table: dict[str, str] | None = None
    if getattr(args, "move_table", None):
        table = json.loads(Path(args.move_table).resolve().read_text(encoding="utf-8"))

    def key(e: dict) -> tuple:  # (module, name, kind, hash)
        return (e["module"], e["name"], e["kind"], e["hash"])

    def nk(e: dict) -> tuple:  # (name, kind, hash)
        return (e["name"], e["kind"], e["hash"])

    def mnk(e: dict) -> tuple:  # (module, name, kind)
        return (e["module"], e["name"], e["kind"])

    from collections import Counter

    b_counts, a_counts = Counter(map(key, before)), Counter(map(key, after))
    unchanged_multiset = b_counts & a_counts
    unchanged: set[tuple] = set()
    remaining_b, remaining_a = [], []
    used_b: Counter = Counter()
    used_a: Counter = Counter()
    for e in before:
        if used_b[key(e)] < unchanged_multiset[key(e)]:
            used_b[key(e)] += 1
            unchanged.add(key(e))
        else:
            remaining_b.append(e)
    for e in after:
        if used_a[key(e)] < unchanged_multiset[key(e)]:
            used_a[key(e)] += 1
        else:
            remaining_a.append(e)

    moved_pairs: list[dict] = []
    moved_mismatch: list[dict] = []
    moved_verified = 0
    # pass 2a (with a move table): pair each mapped BEFORE symbol ONLY inside
    # the module the table names (identical hash, same leaf name + kind).
    if table is not None:
        pool_a: dict[tuple, list[dict]] = {}
        for e in remaining_a:
            pool_a.setdefault((e["module"],) + nk(e), []).append(e)
        fallback_b: list[dict] = []
        matched_after: list[dict] = []
        for e in remaining_b:
            expected = table.get(e["module"])
            cands = pool_a.get((expected,) + nk(e)) if expected else None
            if cands:
                cand = cands.pop(0)
                matched_after.append(cand)
                pair = {"name": e["name"], "kind": e["kind"], "hash": e["hash"],
                        "from_module": e["module"], "to_module": cand["module"],
                        "verified_by_table": True}
                moved_pairs.append(pair)
                moved_verified += 1
            else:
                fallback_b.append(e)
        matched_ids = {id(m) for m in matched_after}
        leftover_a = [e for e in remaining_a if id(e) not in matched_ids]
    else:
        fallback_b = list(remaining_b)
        leftover_a = list(remaining_a)

    # pass 2b: global multiset match on (name, kind, hash) for the leftovers
    b_by_nk: Counter = Counter(nk(e) for e in fallback_b)
    a_by_nk: Counter = Counter(nk(e) for e in leftover_a)
    matched_nk = b_by_nk & a_by_nk
    used_moved: Counter = Counter()
    still_b: list[dict] = []
    moved_b: list[dict] = []
    for e in fallback_b:
        if used_moved[nk(e)] < matched_nk[nk(e)]:
            used_moved[nk(e)] += 1
            moved_b.append(e)
        else:
            still_b.append(e)
    still_a: list[dict] = []
    used_after_moved: Counter = Counter()
    for e in leftover_a:
        if used_after_moved[nk(e)] < matched_nk[nk(e)]:
            used_after_moved[nk(e)] += 1
        else:
            still_a.append(e)

    # pair the fallback MOVED old->new per (name, kind, hash)
    a_pool: dict[tuple, list[str]] = {}
    for e in leftover_a:
        a_pool.setdefault(nk(e), []).append(e["module"])
    for e in moved_b:
        to_mod = a_pool[nk(e)].pop(0)
        pair = {"name": e["name"], "kind": e["kind"], "hash": e["hash"],
                "from_module": e["module"], "to_module": to_mod}
        moved_pairs.append(pair)
    if table is not None:
        for pair in moved_pairs:
            expected = table.get(pair["from_module"])
            if expected is not None and pair["to_module"] != expected:
                pair["expected_module"] = expected
                moved_mismatch.append(pair)

    # CHANGED: same (module, name, kind), different hash; the rest of still_b is MISSING
    a_by_mnk: dict[tuple, list[dict]] = {}
    for e in still_a:
        a_by_mnk.setdefault(mnk(e), []).append(e)
    changed: list[dict] = []
    missing: list[dict] = []
    for e in still_b:
        partners = a_by_mnk.get(mnk(e))
        if partners:
            changed.append({"before": e, "after": partners.pop(0)})
        else:
            missing.append(e)
    changed_after_ids = {id(c["after"]) for c in changed}
    added = [e for e in still_a if id(e) not in changed_after_ids]

    # --- --symbol-map (WP-R3 method change 2): symbol-level old->new module map.
    # Accepted shapes: a flat JSON object mapping "old.module" (module-level:
    # every symbol AND top-level statement of that module moves there) or
    # "old.module:name" (symbol-level) to "new.module"; or a plan.json-style
    # wrapper {"symbol_map": {...same flat shape...}}.
    symbol_map: dict[str, str] | None = None
    if getattr(args, "symbol_map", None):
        raw = json.loads(Path(args.symbol_map).resolve().read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("symbol_map"), dict):
            raw = raw["symbol_map"]
        if not isinstance(raw, dict):
            raise SystemExit("--symbol-map must be a JSON object "
                             "({'old.module[:name]': 'new.module'} or {'symbol_map': ...})")
        symbol_map = {str(k): str(v) for k, v in raw.items()}

    def mapped_module(module: str, name: str) -> str | None:
        if symbol_map is None:
            return None
        if f"{module}:{name}" in symbol_map:
            return symbol_map[f"{module}:{name}"]
        return symbol_map.get(module)

    symbol_map_verified = 0
    symbol_map_mismatch: list[dict] = []
    if symbol_map is not None:
        a_sym_pool: Counter = Counter(
            (e["module"], e["name"], e["kind"], e["hash"]) for e in after
        )
        used: Counter = Counter()
        for e in before:
            expected = mapped_module(e["module"], e["name"])
            if expected is None or expected == e["module"]:
                continue  # unmapped or explicitly staying put
            k = (expected, e["name"], e["kind"], e["hash"])
            if used[k] < a_sym_pool.get(k, 0):
                used[k] += 1
                symbol_map_verified += 1
            else:
                symbol_map_mismatch.append({
                    "name": e["name"], "kind": e["kind"], "hash": e["hash"],
                    "from_module": e["module"], "expected_module": expected,
                    "reason": "symbol not found in its mapped module (or exhausted)",
                })

    # --- top-level statement checks (both inventories must carry "statements")
    statement_missing: list[dict] = []
    statement_added: list[dict] = []
    statement_moved = 0
    statement_order_violations: list[dict] = []
    if "statements" in before_blob and "statements" in after_blob:
        before_stmts: list[dict] = before_blob["statements"]
        after_stmts: list[dict] = after_blob["statements"]

        # every BEFORE statement must land in its mapped module (or stay put)
        a_stmt_pool: Counter = Counter(
            (s["module"], s["kind"], s["hash"]) for s in after_stmts
        )
        stmt_used: Counter = Counter()
        for s in before_stmts:
            expected = mapped_module(s["module"], s["name"]) or s["module"]
            k = (expected, s["kind"], s["hash"])
            if stmt_used[k] < a_stmt_pool.get(k, 0):
                stmt_used[k] += 1
                if expected != s["module"]:
                    statement_moved += 1
            else:
                statement_missing.append({
                    "module": s["module"], "kind": s["kind"], "name": s["name"],
                    "hash": s["hash"], "ordinal": s["ordinal"],
                    "expected_module": expected,
                })
        # F3: duplicated statements — any (DESTINATION module, kind, hash) MORE
        # often in AFTER than in BEFORE (a copy-instead-of-move or a duplicated
        # .update(...) call must fail, not pass silently). Destination-aware:
        # statements MOVED per the symbol map land in their new module and are
        # not counted as additions.
        b_dest_pool: Counter = Counter(
            ((mapped_module(s["module"], s["name"]) or s["module"]), s["kind"], s["hash"])
            for s in before_stmts
        )
        for (module, kind, digest), count in sorted(a_stmt_pool.items()):
            extra = count - b_dest_pool.get((module, kind, digest), 0)
            if extra > 0:
                statement_added.append({
                    "module": module, "kind": kind, "hash": digest,
                    "name": next((s["name"] for s in after_stmts
                                  if s["module"] == module and s["kind"] == kind
                                  and s["hash"] == digest), ""),
                    "extra_count": extra,
                })

        # F3: duplicated SYMBOLS — per (DESTINATION module, name, kind, hash) more
    # often in AFTER than in BEFORE (destination-aware: symbols MOVED per the
    # symbol map land in their new module and are not counted as duplicates).
    symbol_duplicated: list[dict] = []
    a_sym_counts: Counter = Counter(
        (e["module"], e["name"], e["kind"], e["hash"]) for e in after
    )
    b_sym_counts: Counter = Counter(
        ((mapped_module(e["module"], e["name"]) or e["module"]),
         e["name"], e["kind"], e["hash"])
        for e in before
    )
    for (module, name, kind, digest), count in sorted(a_sym_counts.items()):
        extra = count - b_sym_counts.get((module, name, kind, digest), 0)
        if extra > 0:
            symbol_duplicated.append({
                "module": module, "name": name, "kind": kind, "hash": digest,
                "extra_count": extra,
            })

    # F5: ORDER check ACROSS the symbol map — group BEFORE statements by their
    # mapped DESTINATION module and compare the relative order of the common
    # (kind, hash) statements with AFTER. Covers NEW modules (the three
    # _VK_NAMED_KEYS.update calls move into a module absent from BEFORE — a
    # swap there is still caught).
    if "statements" in before_blob and "statements" in after_blob:
        before_stmts = before_blob["statements"]
        after_stmts = after_blob["statements"]

        def dest_of(s: dict) -> str:
            return mapped_module(s["module"], s["name"]) or s["module"]

        by_dest_b: dict[str, list[dict]] = {}
        by_dest_a: dict[str, list[dict]] = {}
        for s in before_stmts:
            by_dest_b.setdefault(dest_of(s), []).append(s)
        for s in after_stmts:
            by_dest_a.setdefault(s["module"], []).append(s)
        for module in sorted(by_dest_b):
            b_mod = sorted(by_dest_b[module], key=lambda s: s["ordinal"])
            a_mod = sorted(by_dest_a.get(module, []), key=lambda s: s["ordinal"])
            common = (Counter((s["kind"], s["hash"]) for s in b_mod)
                      & Counter((s["kind"], s["hash"]) for s in a_mod))
            if not common:
                continue

            def ordered(seq: list[dict]) -> list[tuple]:
                remaining = Counter(common)
                out_seq = []
                for s in seq:
                    k = (s["kind"], s["hash"])
                    if remaining.get(k, 0) > 0:
                        remaining[k] -= 1
                        out_seq.append(k)
                return out_seq

            b_seq, a_seq = ordered(b_mod), ordered(a_mod)
            if b_seq != a_seq:
                first_diverge = next(
                    (i for i, (x, y) in enumerate(zip(b_seq, a_seq)) if x != y),
                    min(len(b_seq), len(a_seq)),
                )
                statement_order_violations.append({
                    "module": module,
                    "note": "relative order of the module's unchanged top-level "
                            "statements changed",
                    "before_at_divergence": (b_seq[first_diverge]
                                             if first_diverge < len(b_seq) else None),
                    "after_at_divergence": (a_seq[first_diverge]
                                            if first_diverge < len(a_seq) else None),
                })

    # F14: --symbol-map keys that match NOTHING in BEFORE fail the diff (a
    # typo'd key would otherwise be silently ignored)
    symbol_map_unknown_keys: list[str] = []
    if symbol_map is not None:
        before_modules = {e["module"] for e in before}
        before_stmt_modules = {s["module"] for s in before_blob.get("statements", [])}
        before_names = {(e["module"], e["name"]) for e in before}
        before_stmt_names = {(s["module"], s["name"])
                             for s in before_blob.get("statements", [])}
        for key in sorted(symbol_map):
            module, _sep, name = key.partition(":")
            if name:
                matched = ((module, name) in before_names
                           or (module, name) in before_stmt_names)
            else:
                matched = module in before_modules or module in before_stmt_modules
            if not matched:
                symbol_map_unknown_keys.append(key)

    report = {
        "before": str(Path(args.before)),
        "after": str(Path(args.after)),
        "before_symbol_count": len(before),
        "after_symbol_count": len(after),
        "unchanged_count": len(unchanged),
        "moved": moved_pairs,
        "missing": missing,
        "added": added,
        "changed": changed,
        "move_table": str(Path(args.move_table).resolve()) if table is not None else None,
        "moved_verified_by_table": moved_verified,
        "moved_mismatch": moved_mismatch,
        "symbol_map": str(Path(args.symbol_map).resolve()) if symbol_map is not None else None,
        "symbol_map_verified": symbol_map_verified,
        "symbol_map_mismatch": symbol_map_mismatch,
        "symbol_map_unknown_keys": symbol_map_unknown_keys,
        "statement_missing": statement_missing,
        "statement_added": statement_added,
        "statement_moved_per_map": statement_moved,
        "statement_order_violations": statement_order_violations,
        "symbol_duplicated": symbol_duplicated,
    }
    report["moved_count"] = len(moved_pairs)
    report["moved_mismatch_count"] = len(moved_mismatch)
    report["symbol_map_mismatch_count"] = len(symbol_map_mismatch)
    report["symbol_map_unknown_key_count"] = len(symbol_map_unknown_keys)
    report["statement_missing_count"] = len(statement_missing)
    report["statement_added_count"] = len(statement_added)
    report["statement_order_violation_count"] = len(statement_order_violations)
    report["symbol_duplicated_count"] = len(symbol_duplicated)
    summary = (f"missing={len(missing)} changed={len(changed)} added={len(added)} "
               f"moved={len(moved_pairs)} unchanged={len(unchanged)} "
               f"table_mismatch={len(moved_mismatch)} "
               f"symmap_verified={symbol_map_verified} "
               f"symmap_mismatch={len(symbol_map_mismatch)} "
               f"symmap_unknown={len(symbol_map_unknown_keys)} "
               f"stmt_missing={len(statement_missing)} "
               f"stmt_added={len(statement_added)} "
               f"stmt_order={len(statement_order_violations)} "
               f"symbol_dup={len(symbol_duplicated)}")
    print(f"inventory diff: {summary}")
    if missing:
        for e in missing[:50]:
            print(f"  MISSING {e['module']} {e['kind']} {e['name']}")
    if changed:
        for c in changed[:50]:
            print(f"  CHANGED {c['before']['module']} {c['before']['kind']} {c['before']['name']}")
    if added:
        for e in added[:50]:
            print(f"  ADDED {e['module']} {e['kind']} {e['name']}")
    if moved_mismatch:
        for pair in moved_mismatch[:50]:
            print(f"  MOVE-MISMATCH {pair['from_module']} {pair['kind']} {pair['name']}"
                  f" -> {pair['to_module']} (expected {pair['expected_module']})")
    if symbol_map_mismatch:
        for pair in symbol_map_mismatch[:50]:
            print(f"  SYMBOL-MAP-MISMATCH {pair['from_module']} {pair['kind']} {pair['name']}"
                  f" (expected in {pair['expected_module']})")
    if symbol_map_unknown_keys:
        for key in symbol_map_unknown_keys[:50]:
            print(f"  SYMBOL-MAP-UNKNOWN-KEY {key!r} matches nothing in BEFORE")
    if statement_missing:
        for s in statement_missing[:50]:
            print(f"  STATEMENT-MISSING {s['module']}:{s['ordinal']} {s['kind']} {s['name']}"
                  f" (expected in {s['expected_module']})")
    if statement_added:
        for s in statement_added[:50]:
            print(f"  STATEMENT-ADDED {s['module']} {s['kind']} {s['name']}"
                  f" (x{s['extra_count']} in AFTER only)")
    if statement_order_violations:
        for v in statement_order_violations[:50]:
            print(f"  STATEMENT-ORDER {v['module']} — {v['note']}")
    if symbol_duplicated:
        for s in symbol_duplicated[:50]:
            print(f"  SYMBOL-DUPLICATED {s['module']} {s['kind']} {s['name']}"
                  f" (x{s['extra_count']} in AFTER only)")
    if args.out:
        out_path = Path(args.out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"diff report -> {out_path}")
    return 1 if (missing or changed or moved_mismatch or symbol_map_mismatch
                 or symbol_map_unknown_keys or statement_missing or statement_added
                 or statement_order_violations or symbol_duplicated) else 0



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
    p_diff = sub.add_parser("diff", help="diff two inventory files (BEFORE AFTER)")
    p_diff.add_argument("before")
    p_diff.add_argument("after")
    p_diff.add_argument("--out", default=None, help="write the full diff report JSON here")
    p_diff.add_argument("--move-table", default=None, dest="move_table",
                        help="JSON file mapping old module -> new module; MOVED symbols "
                             "must land in the named module or the diff fails")
    p_diff.add_argument("--symbol-map", default=None, dest="symbol_map",
                        help="JSON file mapping 'old.module' or 'old.module:name' -> "
                             "'new.module' (or {'symbol_map': {...}}); every mapped "
                             "symbol AND top-level statement must land in its mapped "
                             "module, and statements that stay in a module keep their "
                             "relative order, or the diff fails")
    p_diff.set_defaults(func=cmd_diff)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
