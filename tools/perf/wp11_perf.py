#!/usr/bin/env python
"""WP11 performance A/B: v0.7.5 (BEFORE) vs 0.7.6 (AFTER) against the +5% ceiling.

Protocol (contract WP11): fresh, interleaved, same-interpreter measurement.
BEFORE = a detached read-only git worktree of 2ae76b0 (v0.7.5, FLAT layout);
AFTER = the stable tree (0.7.6, LAYERED layout). Every sample runs in a FRESH
interpreter with that side's own PYTHONPATH=<tree>/src. Statistics and the
verdict rule are REUSED from tools/restructure/equivalence.py by import
(bootstrap_ratio_ci, perf_verdict, tree_env/_base_env/warm_child_env, the
normalize/raw-counts helpers for the STOP diagnosis) — nothing there is
modified. The one documented adaptation: equivalence pins CEILING=1.02 while
the WP11 contract pins 1.05, so verdicts are computed here with
``perf_verdict(..., ceiling=1.05)`` over ``bootstrap_ratio_ci`` CIs.

Phases (each invocation appends; every invocation stays well under 200 s):

  phase --name dry                     one hot + r5 round per side -> dry.json,
                                       NOT appended to the series
  phase --name hot [--rounds K]        K interleaved hot-path rounds (default 4;
                                       contract N = 30 per side)
  phase --name import-cold [--pairs K] K interleaved cold-import pairs (contract
                                       N = 40 per side)
  phase --name import-warm [--pairs K] same, warm (per-side PYTHONPYCACHEPREFIX
                                       under the artifacts dir; priming import
                                       first, .pyc presence asserted)
  phase --name rerun-hot --rounds 30   the contract's 2xN rerun for NOISE
                                       metrics (same generation, appended)
  phase --name rerun-import-cold|rerun-import-warm   2xN rerun for imports
  compute                              statistics + verdicts; writes
                                       .orvex/mission/artifacts/gates/
                                       wp11-perf.json and perf-after.txt
  diagnose                             cProfile comparison per side (co_qualname
                                       keyed) — ONLY run on a hot-path REGRESSION

Verdict rule per metric (ceiling 1.05): PASS if CI upper <= 1.05; NOISE if
median <= 1.05 < CI upper (after the 2xN rerun: NOISE-UNRESOLVED — never
coerced to PASS); REGRESSION if CI lower > 1.05. Any REGRESSION on a hot-path
metric (``hot.*`` or ``r5.*``) => overall STOP. Import time (``import_*``) is a
finding, never a STOP.

Metrics per hot round (fresh interpreter per side, per round):
  hot.captures_per_action        backend.observe() calls per executed action
                                 over the scripted 8-action workload (click,
                                 move, type, keypress, hotkey, scroll, wait,
                                 focus_window) on the fake backend
  hot.execution_ms.p50/.p95      audit Metrics.snapshot() latencies
  hot.observation_ms.p50/.p95
  hot.verification_ms.p50/.p95
  r5.<stage>.p50/.p95            benchmarks/r5_speed_profile.py's stage set
                                 re-shaped onto the fake backend (real-desktop
                                 stages — mss grab, refresh_monitors, UIA read —
                                 are NOT COMPARABLE on the fake and absent)
The fake backend's sleep knobs (CORTEX_KEY_DISPATCH_GAP,
CORTEX_FOCUS_SETTLE_SECONDS) are set to 0 IDENTICALLY on both sides via env
(both trees honour them; v0.7.5 confirmed at backend.py:980/988) and recorded
in every artifact. The wait workload action uses delta=0 so the fake's wait
sleep is zero on both sides.

Call-site differences BETWEEN THE TREES (adapted, documented — no tuning):
  1. flat vs layered module paths (resolved with the imp(flat, layered) pair,
     the same helper equivalence.py's _fake-harness uses).
  2. server entry module: BEFORE ``computer_use_mcp.server`` (module), AFTER
     ``computer_use_mcp.server.app``.
  3. ``_bound_outbound_image``: BEFORE ``computer_use_mcp.server`` returning a
     2-tuple; AFTER ``computer_use_mcp.server.replies`` returning a 3-tuple
     (WP5 H7 added the outbound scale). The r5 child unpacks positionally.
  4. 0.7.6 GroundedAction adds hold_ms/duration_ms/plan_point_settle_ms and
     run_single adds trailing-optional params — all unused here (defaults keep
     the legacy behavior), recorded as differences, not adaptations.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
REPO = TOOL_DIR.parents[1]
RESTRUCTURE_DIR = REPO / "tools" / "restructure"
for _p in (str(TOOL_DIR), str(RESTRUCTURE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import equivalence  # tools/restructure/equivalence.py — reused, never modified

GATE_PY = Path(os.environ.get("CORTEX_GATE_PYTHON", sys.executable))
ART = REPO / ".orvex" / "mission" / "artifacts"
WP11_DIR = ART / "perf" / "wp11"
GATES_DIR = ART / "gates"
SERIES_FILE = WP11_DIR / "series.json"

BEFORE_GIT = "2ae76b0"
AFTER_GIT = "05b6a00"
CEILING = 1.05
HOT_ROUNDS = 30
IMPORT_PAIRS = 40
HOT_CHILD_REPEATS = 6
R5_ITERS = 8
PROFILE_EXECS = 240
#: identical on BOTH sides (recorded); both trees honour these env knobs
#: (BEFORE backend.py:980/988, AFTER desktop/backend.py:295/303).
SLEEP_KNOBS = {"CORTEX_KEY_DISPATCH_GAP": "0", "CORTEX_FOCUS_SETTLE_SECONDS": "0"}
HOT_PREFIXES = ("hot.", "r5.")
IMPORT_METRICS = ("import_cold_ms", "import_warm_ms")
IMPORT_ENTRIES = {"before": "computer_use_mcp.server", "after": "computer_use_mcp.server.app"}


# ---------------------------------------------------------------------------
# pure helpers (unit-tested in tests/test_wp11_perf_tool.py)
# ---------------------------------------------------------------------------

def metric_record(before: list[float], after: list[float]) -> dict:
    """WP11 verdict record: equivalence's statistics at the WP11 ceiling 1.05."""
    mb = statistics_median(before)
    ma = statistics_median(after)
    if mb == 0 and ma == 0:
        return {"n": [len(before), len(after)], "before_median": 0.0,
                "after_median": 0.0, "ratio": 1.0, "ci95": [1.0, 1.0],
                "verdict": "PASS", "degenerate": True}
    ratio = (ma / mb) if mb else float("inf")
    lo, hi = equivalence.bootstrap_ratio_ci(before, after)
    return {"n": [len(before), len(after)], "before_median": mb,
            "after_median": ma, "ratio": ratio, "ci95": [lo, hi],
            "verdict": equivalence.perf_verdict(lo, hi, ratio, ceiling=CEILING)}


def statistics_median(values: list[float]) -> float:
    return __import__("statistics").median(values) if values else float("nan")


def is_hot_path(metric: str) -> bool:
    return metric.startswith(HOT_PREFIXES)


def rerun_done(metric: str, phases: dict) -> bool:
    """The contract's 2xN rerun for THIS metric group, from the rerun-* phase
    counters only (full-size: N again, not N total)."""
    if metric == "import_cold_ms":
        return int(phases.get("rerun-import-cold", 0)) >= IMPORT_PAIRS
    if metric == "import_warm_ms":
        return int(phases.get("rerun-import-warm", 0)) >= IMPORT_PAIRS
    return int(phases.get("rerun-hot", 0)) >= HOT_ROUNDS


def first_pass_records(series: dict) -> dict:
    """First contract pass, derived deterministically from the FIRST N samples
    of each series (import N=40 pairs, hot N=30 rounds)."""
    out = {}
    for metric, d in sorted(series.items()):
        n = IMPORT_PAIRS if metric in IMPORT_METRICS else HOT_ROUNDS
        out[metric] = metric_record(d["before"][:n], d["after"][:n])
    return out


def downgrade_noise(records: dict, phases: dict) -> list[str]:
    """NOISE + rerun-done => NOISE-UNRESOLVED (never PASS). Mutates records."""
    resolved = []
    for metric, rec in records.items():
        if rec.get("verdict") == "NOISE" and rerun_done(metric, phases):
            rec["verdict"] = "NOISE-UNRESOLVED"
            resolved.append(metric)
    return sorted(resolved)


def overall_verdict(records: dict) -> dict:
    """STOP on any hot-path REGRESSION; NOISE-UNRESOLVED / rerun-pending NOISE
    next; PASS only when every hot-path metric PASSed. Import metrics never
    affect the overall verdict (finding only)."""
    regression = sorted(m for m, r in records.items()
                        if is_hot_path(m) and r.get("verdict") == "REGRESSION")
    unresolved = sorted(m for m, r in records.items()
                        if is_hot_path(m) and r.get("verdict") == "NOISE-UNRESOLVED")
    pending = sorted(m for m, r in records.items()
                     if is_hot_path(m) and r.get("verdict") == "NOISE"
                     and not rerun_done(m, _PHASES_FOR_PENDING))
    if regression:
        overall = "STOP"
    elif unresolved:
        overall = "NOISE-UNRESOLVED"
    elif pending:
        overall = "NOISE"
    else:
        overall = "PASS"
    return {"overall": overall, "regression_metrics": regression,
            "noise_unresolved": unresolved, "rerun_required": pending}


#: set by load_blob()/compute; read by overall_verdict for the pending check
_PHASES_FOR_PENDING: dict = {}


def merge_mix(acc: dict, round_mix: dict) -> dict:
    """Fold one round's per-action-type {actions, kinds, verdicts} into the
    per-side accumulator."""
    for name, slot in round_mix.items():
        out = acc.setdefault(name, {"actions": 0, "kinds": {}, "verdicts": {}})
        out["actions"] += int(slot.get("actions", 0))
        for key in ("kinds", "verdicts"):
            for k, v in slot.get(key, {}).items():
                out[key][k] = out[key].get(k, 0) + int(v)
    return acc


def child_env(tree: Path) -> dict:
    """Env for one side's child: the side's src on PYTHONPATH, PYTHONDONTWRITE-
    BYTECODE=1, CUMCP_RUN_E2E popped, the sleep knobs identical both sides."""
    return equivalence.tree_env(tree, dict(SLEEP_KNOBS))


# ---------------------------------------------------------------------------
# child templates (written into a tempfile dir by this tool — never into a tree)
# ---------------------------------------------------------------------------

IMP_HELPER = '''
def imp(flat, layered):
    try:
        return __import__(flat, fromlist=["*"])
    except ImportError:
        return __import__(layered, fromlist=["*"])
'''

HOT_CHILD = '''"""WP11 hot-path child (generated by tools/perf/wp11_perf.py; one fresh
interpreter per sample; fake backend only, offline)."""
import argparse
import asyncio
import base64
import io
import json
from types import SimpleNamespace

from PIL import Image
''' + IMP_HELPER + '''
agent_mod = imp("computer_use_mcp.agent", "computer_use_mcp.pipeline.agent")
audit_mod = imp("computer_use_mcp.audit", "computer_use_mcp.security.audit")
backend_mod = imp("computer_use_mcp.backend", "computer_use_mcp.desktop.fake_backend")
limits_mod = imp("computer_use_mcp.limits", "computer_use_mcp.core.limits")
models_mod = imp("computer_use_mcp.models", "computer_use_mcp.core.models")
obs_mod = imp("computer_use_mcp.observation", "computer_use_mcp.vision.observation")
safety_mod = imp("computer_use_mcp.safety", "computer_use_mcp.security.safety")
state_mod = imp("computer_use_mcp.state", "computer_use_mcp.core.state")

NOTEPAD = models_mod.WindowInfo(hwnd=12, pid=500, process_name="notepad.exe",
                                window_class="Notepad", title="Untitled - Notepad")

#: the scripted single-action workload both versions support; wait uses delta=0
#: (zero sleep on both sides); no action states an expected_effect (the WP4a
#: "effect-less" acceptance shape).
FLOWS = [
    ("click", models_mod.GroundedAction(action="click", point=models_mod.Point(x=640, y=400), confidence=1.0)),
    ("move", models_mod.GroundedAction(action="move", point=models_mod.Point(x=300, y=200), confidence=1.0)),
    ("type", models_mod.GroundedAction(action="type", text="notepad", confidence=1.0)),
    ("keypress", models_mod.GroundedAction(action="keypress", keys=["enter"], confidence=1.0)),
    ("hotkey", models_mod.GroundedAction(action="hotkey", keys=["win", "r"], confidence=1.0)),
    ("scroll", models_mod.GroundedAction(action="scroll", delta=3, confidence=1.0)),
    ("wait", models_mod.GroundedAction(action="wait", delta=0, confidence=1.0)),
    ("focus_window", models_mod.GroundedAction(action="focus_window", target="Untitled - Notepad", confidence=1.0)),
]


class CountingFlowBackend(backend_mod.FakeComputerBackend):
    """Counts captures; the fake screen alternates per executed action so the
    verification diff has a deterministic change to judge."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.capture_count = 0

    def observe(self, monitor_index=None):
        self.capture_count += 1
        observation = super().observe(monitor_index)
        observation.active_window = NOTEPAD.title
        observation.active_window_info = NOTEPAD.model_copy()
        color = "white" if len(self.executed) % 2 == 0 else "black"
        image = Image.new("RGB", (self.width, self.height), color)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        observation.image_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
        return observation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=6)
    args = parser.parse_args()
    repeats = max(1, args.repeats)
    backend = CountingFlowBackend()
    backend.set_windows([NOTEPAD])
    metrics = audit_mod.Metrics()
    agent = agent_mod.ComputerUseAgent(
        backend, provider=None, safety=safety_mod.SafetyPolicy(),
        task=state_mod.TaskState(), stop=state_mod.StopToken(),
        limits=limits_mod.Limits(max_actions=repeats * len(FLOWS) + 10,
                                 max_task_seconds=3600.0).validate(),
        metrics=metrics,
    )
    agent.observation = obs_mod.ObservationEngine(backend)
    state = SimpleNamespace(
        dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
        max_steps=repeats * len(FLOWS) + 10, step_count=0, require_approval=False,
        max_retries_per_action=1,
    )
    mix = {}
    actions_run = 0
    for _ in range(repeats):
        for name, action in FLOWS:
            outcome = asyncio.run(agent.run_single(state, action))
            slot = mix.setdefault(name, {"actions": 0, "kinds": {}, "verdicts": {}})
            slot["actions"] += 1
            kind = getattr(outcome, "kind", "unknown")
            slot["kinds"][kind] = slot["kinds"].get(kind, 0) + 1
            verdict = "none"
            result = getattr(outcome, "result", None)
            verification = getattr(result, "verification", None) if result is not None else None
            if verification is not None:
                verdict = getattr(verification, "outcome", "none")
            slot["verdicts"][verdict] = slot["verdicts"].get(verdict, 0) + 1
            actions_run += 1
    snapshot = metrics.snapshot()
    print(json.dumps({
        "mode": "fake backend, offline, serial (WP11 hot child)",
        "repeats": repeats,
        "actions_run": actions_run,
        "captures_per_action": backend.capture_count / actions_run,
        "captures_total": backend.capture_count,
        "executed_total": len(backend.executed),
        "mix": mix,
        "metrics_snapshot": snapshot,
    }))


if __name__ == "__main__":
    main()
'''

R5_CHILD = '''"""WP11 r5-stages-on-fake child (generated by tools/perf/wp11_perf.py).

benchmarks/r5_speed_profile.py's stage set re-shaped onto the FAKE backend so
both trees run the identical code offline (real desktop untouched). Stages that
need the real desktop (mss_grab, refresh_monitors, UIA semantic read) are
deliberately absent — NOT COMPARABLE on the fake backend. Screens are two
full-size solid-color PNGs (white/black) built exactly like the hot child's
alternating fake screen."""
import argparse
import base64
import io
import json
import time

from PIL import Image
''' + IMP_HELPER + '''
backend_mod = imp("computer_use_mcp.backend", "computer_use_mcp.desktop.fake_backend")
limits_mod = imp("computer_use_mcp.limits", "computer_use_mcp.core.limits")
models_mod = imp("computer_use_mcp.models", "computer_use_mcp.core.models")
obs_mod = imp("computer_use_mcp.observation", "computer_use_mcp.vision.observation")
ver_mod = imp("computer_use_mcp.verification", "computer_use_mcp.pipeline.verification")

# API difference 3: BEFORE server monolith (2-tuple) / AFTER server.replies (3-tuple)
try:
    bound_fn = __import__("computer_use_mcp.server.replies", fromlist=["*"])._bound_outbound_image
except ImportError:
    bound_fn = __import__("computer_use_mcp.server", fromlist=["*"])._bound_outbound_image


def _ms(samples):
    xs = sorted(samples)
    if not xs:
        return {"n": 0, "p50": 0.0, "p95": 0.0}
    return {"n": len(xs), "p50": xs[len(xs) // 2],
            "p95": xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]}


def _solid_png(color, width, height):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=8)
    args = parser.parse_args()
    iters = max(3, args.iterations)
    backend = backend_mod.FakeComputerBackend()
    width, height = backend.width, backend.height
    report = {}

    backend.query_foreground_window()  # warm the attribute path once
    fgw_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        backend.query_foreground_window()
        fgw_ms.append((time.perf_counter() - t0) * 1000.0)
    report["query_foreground_window"] = _ms(fgw_ms)

    white = _solid_png("white", width, height)
    black = _solid_png("black", width, height)
    white_b64 = base64.b64encode(white).decode("ascii")
    black_b64 = base64.b64encode(black).decode("ascii")
    before = models_mod.Observation(image_base64=white_b64, width=width, height=height)
    after = models_mod.Observation(image_base64=black_b64, width=width, height=height)

    # a. observe_full (fake observe == white frame + window identity + monitors)
    obs_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        backend.observe()
        obs_ms.append((time.perf_counter() - t0) * 1000.0)
    report["observe_full"] = _ms(obs_ms)

    # b. frombytes-less PNG encode (full-screen solid color, r5 stage B shape)
    png_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        out = io.BytesIO()
        Image.new("RGB", (width, height), "white").save(out, format="PNG")
        png_ms.append((time.perf_counter() - t0) * 1000.0)
    report["pil_png_encode"] = _ms(png_ms)
    png_bytes = out.getvalue()

    # c. base64 encode
    b64_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        base64.b64encode(png_bytes).decode("ascii")
        b64_ms.append((time.perf_counter() - t0) * 1000.0)
    report["b64_encode"] = _ms(b64_ms)
    report["png_size_kb"] = {"n": 1, "p50": len(png_bytes) / 1024.0, "p95": 0.0}

    # C. digest
    dig_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        obs_mod.observation_digest(before)
        dig_ms.append((time.perf_counter() - t0) * 1000.0)
    report["observation_digest"] = _ms(dig_ms)

    # D. verification
    engine = ver_mod.VerificationEngine()
    intent = ver_mod.VerificationIntent(kind=ver_mod.VerificationKind.VISUAL_CHANGE,
                                        expected_change=True)
    ver_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        engine.verify(intent, before, after)
        ver_ms.append((time.perf_counter() - t0) * 1000.0)
    report["verify_screenshot_diff"] = _ms(ver_ms)

    strategy = ver_mod.ScreenshotDiffStrategy()
    ver_ms2 = []
    for _ in range(iters):
        t0 = time.perf_counter()
        strategy.verify(intent, before, after)
        ver_ms2.append((time.perf_counter() - t0) * 1000.0)
    report["verify_diff_only"] = _ms(ver_ms2)

    dec_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        Image.open(io.BytesIO(base64.b64decode(before.image_base64))).convert("RGB")
        dec_ms.append((time.perf_counter() - t0) * 1000.0)
    report["diff_decode_one_side"] = _ms(dec_ms)

    # E. outbound bound (REM-E ladder); 2-tuple BEFORE / 3-tuple AFTER —
    # positional unpack of the first two slots (mime unchanged)
    bound_ms = []
    enc_counts = []
    orig_save = Image.Image.save

    def counting_save(self, *a, **k):
        enc_counts.append(1)
        return orig_save(self, *a, **k)

    for _ in range(iters):
        enc_counts.clear()
        t0 = time.perf_counter()
        bound_result = bound_fn(before.image_base64)
        bound_ms.append((time.perf_counter() - t0) * 1000.0)
    bounded, mime = bound_result[0], bound_result[1]
    report["bound_outbound_image"] = {**_ms(bound_ms), "jpeg_encodes_per_call": len(enc_counts)}
    report["bound_outbound_mime"] = {"n": 1, "p50": 1.0 if mime == "image/jpeg" else 0.0, "p95": 0.0}

    enc_counts.clear()
    Image.Image.save = counting_save
    try:
        bound_probe = bound_fn(before.image_base64)
    finally:
        Image.Image.save = orig_save
    report["bound_outbound_encodes"] = {"n": 1, "p50": float(len(enc_counts)), "p95": 0.0}
    del bound_probe

    # F. rate-gate wait probe (min_screenshot_interval_ms=250)
    limits = limits_mod.Limits(min_screenshot_interval_ms=250)
    enforcer = limits_mod.LimitEnforcer(limits)
    waits = []
    for _ in range(iters):
        enforcer.record_screenshot()
        t0 = time.perf_counter()
        waited = 0.0
        while not enforcer.can_screenshot():
            time.sleep(0.05)
            waited += 0.05
            if waited > 2.0:
                break
        waits.append((time.perf_counter() - t0) * 1000.0)
    report["rate_gate_wait_after_capture"] = _ms(waits)

    # G. composites
    comp_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        src = backend.observe()
        post = backend.observe()
        engine.verify(intent, src, post)
        comp_ms.append((time.perf_counter() - t0) * 1000.0)
    report["composite_action_two_observes_plus_verify"] = _ms(comp_ms)

    comp2 = []
    for _ in range(iters):
        t0 = time.perf_counter()
        src = backend.observe()
        current = backend.observe()
        obs_mod.digest_matches(src, current)
        post = backend.observe()
        obs_mod.digest_matches(current, post)
        engine.verify(intent, src, post)
        comp2.append((time.perf_counter() - t0) * 1000.0)
    report["composite_three_observes_digest_verify"] = _ms(comp2)

    print(json.dumps({"mode": "fake backend, offline (WP11 r5-on-fake child)",
                      "iterations": iters, "stages": report}))


if __name__ == "__main__":
    main()
'''

IMPORT_CHILD = '''"""WP11 import-time child (generated by tools/perf/wp11_perf.py)."""
import time

t0 = time.perf_counter()
import {module}  # noqa: E402,F401

print(time.perf_counter() - t0)
'''

PROFILE_CHILD = '''"""WP11 cProfile child (generated by tools/perf/wp11_perf.py; STOP diagnosis
only): profiles the hot workload through run_single and prints top functions
plus RAW getstats call counts keyed by (normalized file, firstlineno,
co_qualname) — the same raw-count method as equivalence.py's D5."""
import asyncio
import base64
import cProfile
import io
import json
import pstats
from types import SimpleNamespace

from PIL import Image
''' + IMP_HELPER + '''
agent_mod = imp("computer_use_mcp.agent", "computer_use_mcp.pipeline.agent")
audit_mod = imp("computer_use_mcp.audit", "computer_use_mcp.security.audit")
backend_mod = imp("computer_use_mcp.backend", "computer_use_mcp.desktop.fake_backend")
limits_mod = imp("computer_use_mcp.limits", "computer_use_mcp.core.limits")
models_mod = imp("computer_use_mcp.models", "computer_use_mcp.core.models")
obs_mod = imp("computer_use_mcp.observation", "computer_use_mcp.vision.observation")
safety_mod = imp("computer_use_mcp.safety", "computer_use_mcp.security.safety")
state_mod = imp("computer_use_mcp.state", "computer_use_mcp.core.state")

NOTEPAD = models_mod.WindowInfo(hwnd=12, pid=500, process_name="notepad.exe",
                                window_class="Notepad", title="Untitled - Notepad")
FLOWS = [
    models_mod.GroundedAction(action="click", point=models_mod.Point(x=640, y=400), confidence=1.0),
    models_mod.GroundedAction(action="move", point=models_mod.Point(x=300, y=200), confidence=1.0),
    models_mod.GroundedAction(action="type", text="notepad", confidence=1.0),
    models_mod.GroundedAction(action="keypress", keys=["enter"], confidence=1.0),
    models_mod.GroundedAction(action="hotkey", keys=["win", "r"], confidence=1.0),
    models_mod.GroundedAction(action="scroll", delta=3, confidence=1.0),
    models_mod.GroundedAction(action="wait", delta=0, confidence=1.0),
    models_mod.GroundedAction(action="focus_window", target="Untitled - Notepad", confidence=1.0),
]


class CountingFlowBackend(backend_mod.FakeComputerBackend):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.capture_count = 0

    def observe(self, monitor_index=None):
        self.capture_count += 1
        observation = super().observe(monitor_index)
        observation.active_window = NOTEPAD.title
        observation.active_window_info = NOTEPAD.model_copy()
        color = "white" if len(self.executed) % 2 == 0 else "black"
        image = Image.new("RGB", (self.width, self.height), color)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        observation.image_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
        return observation


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--execs", type=int, default={execs})
    args = parser.parse_args()
    backend = CountingFlowBackend()
    backend.set_windows([NOTEPAD])
    profiler = cProfile.Profile()
    profiler.enable()
    n = 0
    while n < args.execs:
        metrics = audit_mod.Metrics()
        agent = agent_mod.ComputerUseAgent(
            backend, provider=None, safety=safety_mod.SafetyPolicy(),
            task=state_mod.TaskState(), stop=state_mod.StopToken(),
            limits=limits_mod.Limits(max_actions=480, max_task_seconds=3600.0).validate(),
            metrics=metrics,
        )
        agent.observation = obs_mod.ObservationEngine(backend)
        state = SimpleNamespace(
            dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
            max_steps=480, step_count=0, require_approval=False,
            max_retries_per_action=1,
        )
        while n < args.execs and state.step_count < 400:
            for action in FLOWS:
                asyncio.run(agent.run_single(state, action))
                n += 1
    profiler.disable()
    stats = pstats.Stats(profiler, stream=io.StringIO())
    rows = sorted(stats.stats.items(), key=lambda kv: kv[1][3], reverse=True)[:25]
    src_prefix = r"{src_prefix}"
    top = []
    for (filename, lineno, fnname), (_cc, _nc, tt, ct, _callers) in rows:
        norm = equivalence_normalize(filename, src_prefix)
        top.append({"func": f"{norm}:{lineno}({fnname})", "cum_ms": round(ct * 1000.0, 3),
                    "tot_ms": round(tt * 1000.0, 3), "calls": _nc})
    counts = equivalence_raw_counts(profiler.getstats(), src_prefix)
    print(json.dumps({"executions": n, "top_by_cumtime": top, "call_counts": counts}))


def equivalence_normalize(filename, src_prefix):
    import sys as _sys
    _sys.path.insert(0, r"{tools_dir}")
    import equivalence as _eq
    return _eq.normalize_code_file(str(filename).replace(chr(92), "/"), src_prefix)


def equivalence_raw_counts(entries, src_prefix):
    import sys as _sys
    _sys.path.insert(0, r"{tools_dir}")
    import equivalence as _eq
    return _eq.raw_counts_from_entries(entries, src_prefix)


if __name__ == "__main__":
    main()
'''


# ---------------------------------------------------------------------------
# series store
# ---------------------------------------------------------------------------

def load_blob(before: Path, after: Path) -> dict:
    if SERIES_FILE.exists():
        blob = json.loads(SERIES_FILE.read_text(encoding="utf-8"))
        recorded = Path(blob["environment"]["before_tree"]).resolve()
        if recorded != before.resolve():
            raise SystemExit(f"series.json is bound to BEFORE {recorded}, not {before}")
        return blob
    blob = {
        "generation": 1,
        "environment": {
            "before_tree": str(before), "after_tree": str(after),
            "before_git": BEFORE_GIT, "after_git": AFTER_GIT,
            "python": str(GATE_PY), "python_version": sys.version.split()[0],
            "platform": platform.platform(),
            "ceiling": CEILING, "hot_rounds_contract_n": HOT_ROUNDS,
            "import_pairs_contract_n": IMPORT_PAIRS,
            "hot_child_repeats": HOT_CHILD_REPEATS, "r5_iterations": R5_ITERS,
            "sleep_knobs": dict(SLEEP_KNOBS),
            "import_entries": dict(IMPORT_ENTRIES),
            "cumcp_run_e2e_set": bool(os.environ.get("CUMCP_RUN_E2E")),
        },
        "phases": {}, "rounds_done": 0,
        "import_pairs": {"cold": 0, "warm": 0},
        "series": {}, "mix": {}, "totals": {},
    }
    save_blob(blob)
    return blob


def save_blob(blob: dict) -> None:
    SERIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SERIES_FILE.write_text(json.dumps(blob, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")


def push(blob: dict, metric: str, side: str, value: float) -> None:
    blob["series"].setdefault(metric, {"before": [], "after": []})[side].append(value)


def _run(cmd: list[str], env: dict, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(REPO), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def run_child(script: Path, args: list[str], tree: Path, timeout: float = 150) -> dict:
    proc = _run([str(GATE_PY), str(script)] + args, child_env(tree), timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"child failed on {tree.name}: rc={proc.returncode}\n"
                           f"{proc.stderr[-1500:]}")
    text = proc.stdout
    return json.loads(text[text.index("{"):text.rindex("}") + 1])


# ---------------------------------------------------------------------------
# phases
# ---------------------------------------------------------------------------

def hot_metrics_from(payload: dict) -> list[tuple[str, float]]:
    lat = payload["metrics_snapshot"]["latencies"]
    out = [("hot.captures_per_action", float(payload["captures_per_action"]))]
    for key in ("observation_ms", "execution_ms", "verification_ms"):
        bucket = lat.get(key, {})
        if isinstance(bucket, dict) and bucket.get("p50_ms") is not None:
            out.append((f"hot.{key}.p50", float(bucket["p50_ms"])))
            out.append((f"hot.{key}.p95", float(bucket.get("p95_ms") or 0.0)))
    return out


def r5_metrics_from(payload: dict) -> list[tuple[str, float]]:
    out = []
    for stage, stats in sorted(payload["stages"].items()):
        if not isinstance(stats, dict):
            continue
        for key in ("p50", "p95"):
            if key in stats:
                out.append((f"r5.{stage}.{key}", float(stats[key])))
    return out


def phase_dry(before: Path, after: Path) -> dict:
    WP11_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wp11-dry-") as tmp:
        hot = Path(tmp) / "_hot_child.py"
        r5 = Path(tmp) / "_r5_child.py"
        hot.write_text(HOT_CHILD, encoding="utf-8")
        r5.write_text(R5_CHILD, encoding="utf-8")
        dry = {}
        for side, tree in (("before", before), ("after", after)):
            hot_payload = run_child(hot, ["--repeats", str(HOT_CHILD_REPEATS)], tree)
            r5_payload = run_child(r5, ["--iterations", str(R5_ITERS)], tree)
            dry[side] = {"hot": hot_payload, "r5": r5_payload}
    dry_file = WP11_DIR / "dry.json"
    dry_file.write_text(json.dumps(dry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return dry


def phase_hot(before: Path, after: Path, blob: dict, rounds: int, rerun: bool) -> dict:
    rounds_dir = WP11_DIR / "rounds"
    rounds_dir.mkdir(parents=True, exist_ok=True)
    gen = blob.get("generation", 1)
    prefix = "rerun" if rerun else "hot"
    with tempfile.TemporaryDirectory(prefix="wp11-hot-") as tmp:
        hot = Path(tmp) / "_hot_child.py"
        r5 = Path(tmp) / "_r5_child.py"
        hot.write_text(HOT_CHILD, encoding="utf-8")
        r5.write_text(R5_CHILD, encoding="utf-8")
        for r in range(rounds):
            n = blob.get("rounds_done", 0) + r + 1
            for side, tree in (("before", before), ("after", after)):
                hot_payload = run_child(hot, ["--repeats", str(HOT_CHILD_REPEATS)], tree)
                r5_payload = run_child(r5, ["--iterations", str(R5_ITERS)], tree)
                (rounds_dir / f"gen{gen}_{prefix}_r{n:03d}_{side}_hot.json").write_text(
                    json.dumps(hot_payload, indent=2) + "\n", encoding="utf-8")
                (rounds_dir / f"gen{gen}_{prefix}_r{n:03d}_{side}_r5.json").write_text(
                    json.dumps(r5_payload, indent=2) + "\n", encoding="utf-8")
                for metric, value in hot_metrics_from(hot_payload) + r5_metrics_from(r5_payload):
                    push(blob, metric, side, value)
                blob["mix"].setdefault(side, {})
                merge_mix(blob["mix"][side], hot_payload["mix"])
                totals = blob["totals"].setdefault(side, {})
                totals["actions_run"] = totals.get("actions_run", 0) + int(hot_payload["actions_run"])
                totals["captures_total"] = totals.get("captures_total", 0) + int(hot_payload["captures_total"])
                totals["executed_total"] = totals.get("executed_total", 0) + int(hot_payload["executed_total"])
                totals["rounds"] = totals.get("rounds", 0) + 1
            blob["rounds_done"] = blob.get("rounds_done", 0) + 1
    key = "rerun-hot" if rerun else "hot"
    blob["phases"][key] = blob["phases"].get(key, 0) + rounds
    save_blob(blob)
    return {"verdict": "DEFERRED", "phase": key, "rounds_added": rounds}


def _import_once(child: Path, tree: Path, warm: bool, cache_dir: Path) -> float:
    extra: dict = {"PYTHONPATH": str(Path(tree) / "src")}
    if warm:
        # warm phase children MAY write .pyc (contract) — under the side's prefix
        extra["PYTHONPYCACHEPREFIX"] = str(cache_dir)
        env = equivalence.warm_child_env(equivalence._base_env(extra))
    else:
        # cold: never read stale bytecode — a fresh empty prefix per phase
        extra["PYTHONPYCACHEPREFIX"] = str(cache_dir)
        env = equivalence._base_env(extra)
    proc = _run([str(GATE_PY), str(child)], env, 120)
    if proc.returncode != 0:
        raise RuntimeError(f"import child failed ({tree.name}, warm={warm}): {proc.stderr[-500:]}")
    return float(proc.stdout.strip().splitlines()[-1]) * 1000.0


def phase_import(before: Path, after: Path, blob: dict, pairs: int,
                 warm: bool, rerun: bool) -> dict:
    cache_root = WP11_DIR / "pycache"
    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wp11-imp-") as tmp:
        tmp = Path(tmp)
        children = {}
        for side in ("before", "after"):
            child = tmp / f"_import_child_{side}.py"
            child.write_text(IMPORT_CHILD.format(module=IMPORT_ENTRIES[side]),
                             encoding="utf-8")
            children[side] = child
        if warm:
            # one priming import per side, then assert the cache actually holds
            # .pyc before any sample is taken (fresh prefix per invocation)
            for side, tree in (("before", before), ("after", after)):
                prefix = cache_root / f"{'rerun-' if rerun else ''}warm-{side}"
                equivalence.clear_dir(prefix)
                _import_once(children[side], tree, True, prefix)
                if not equivalence._pyc_written(prefix):
                    raise RuntimeError(
                        f"warm-import invalid: no .pyc written under the {side} prefix")
        key = "import_warm_ms" if warm else "import_cold_ms"
        cold_prefix = cache_root / f"{'rerun-' if rerun else ''}cold"
        equivalence.clear_dir(cold_prefix)
        for _ in range(pairs):
            for side, tree in (("before", before), ("after", after)):
                ms = _import_once(children[side], tree, warm,
                                  cache_root / f"{'rerun-' if rerun else ''}warm-{side}"
                                  if warm else cold_prefix)
                push(blob, key, side, ms)
    slot = blob["import_pairs"]
    name = ("rerun-" if rerun else "") + ("warm" if warm else "cold")
    slot[name] = slot.get(name, 0) + pairs
    phase_key = ("rerun-import-warm" if rerun and warm else
                 "rerun-import-cold" if rerun else
                 "import-warm" if warm else "import-cold")
    blob["phases"][phase_key] = blob["phases"].get(phase_key, 0) + pairs
    save_blob(blob)
    return {"verdict": "DEFERRED", "phase": phase_key, "pairs_added": pairs}


# ---------------------------------------------------------------------------
# compute + diagnose
# ---------------------------------------------------------------------------

def compute() -> dict:
    blob = json.loads(SERIES_FILE.read_text(encoding="utf-8"))
    series = blob.get("series", {})
    _PHASES_FOR_PENDING.clear()
    _PHASES_FOR_PENDING.update(blob.get("phases", {}))
    if not series:
        result = {"verdict": "NOT_RUN", "problems": ["no series collected"]}
    else:
        records = {m: metric_record(d["before"], d["after"])
                   for m, d in sorted(series.items())}
        noise_resolved = downgrade_noise(records, blob.get("phases", {}))
        ov = overall_verdict(records)
        result = {
            "verdict": ov["overall"],
            "regression_metrics": ov["regression_metrics"],
            "noise_unresolved": ov["noise_unresolved"],
            "rerun_required": ov["rerun_required"],
            "noise_downgraded_after_rerun": noise_resolved,
            "generation": blob.get("generation", 1),
            "environment": blob.get("environment", {}),
            "phases": blob.get("phases", {}),
            "metrics_first_pass": first_pass_records(series),
            "metrics_final": records,
            "verdict_mix": {
                side: {
                    "per_action_type": blob.get("mix", {}).get(side, {}),
                    "totals": blob.get("totals", {}).get(side, {}),
                }
                for side in ("before", "after")
            },
            "import_finding": {
                m: records[m] for m in IMPORT_METRICS if m in records
            },
            "hot_path_note": ("hot-path = hot.* (fake-backend captures/action + "
                              "Metrics p50/p95) and r5.* (r5 stages on the fake); "
                              "import_* is a finding, never a STOP"),
        }
        if ov["overall"] == "STOP":
            result["stop"] = ("hot-path REGRESSION at the 1.05 ceiling — STOP per "
                              "contract; run `diagnose` for the cProfile cause; "
                              "NOTHING was tuned")
    GATES_DIR.mkdir(parents=True, exist_ok=True)
    (GATES_DIR / "wp11-perf.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (GATES_DIR / "perf-after.txt").write_text(render_report(result, blob), encoding="utf-8")
    return result


def _fmt(rec: dict) -> str:
    return (f"before_median={rec['before_median']:.4g} after_median={rec['after_median']:.4g} "
            f"ratio={rec['ratio']:.4f} CI95=[{rec['ci95'][0]:.4f}, {rec['ci95'][1]:.4f}] "
            f"n={rec['n'][0]}x{rec['n'][1]} verdict={rec['verdict']}")


def render_report(result: dict, blob: dict) -> str:
    env = result.get("environment", {})
    lines = [
        "# WP11 PERF AFTER — 0.7.6 vs v0.7.5 (fresh interleaved, ceiling 1.05)",
        "",
        f"BEFORE: {env.get('before_tree')} @ {env.get('before_git')} (v0.7.5, flat)",
        f"AFTER:  {env.get('after_tree')} @ {env.get('after_git')} (0.7.6, layered)",
        f"interpreter: {env.get('python')} (CPython {env.get('python_version')}, {env.get('platform')})",
        f"sleep knobs (identical both sides): {env.get('sleep_knobs')}",
        f"import entries: {env.get('import_entries')}",
        f"contract N: hot {env.get('hot_rounds_contract_n')} rounds/side, "
        f"import {env.get('import_pairs_contract_n')} pairs/side; "
        f"hot child repeats={env.get('hot_child_repeats')}, r5 iterations={env.get('r5_iterations')}",
        f"bootstrap: {equivalence.BOOTSTRAP_RESAMPLES} resamples, seed {equivalence.BOOTSTRAP_SEED} "
        "(reused from tools/restructure/equivalence.py)",
        f"CUMCP_RUN_E2E set during collection: {env.get('cumcp_run_e2e_set')}",
        "",
        f"## OVERALL: {result.get('verdict')}",
        "",
    ]
    if result.get("regression_metrics"):
        lines += [f"STOP: hot-path REGRESSIONS: {result['regression_metrics']}",
                  "Run: python tools/perf/wp11_perf.py diagnose --before <worktree>", ""]
    fp = result.get("metrics_first_pass", {})
    fin = result.get("metrics_final", {})
    lines += ["| metric | first ratio (CI) | final medians before/after | final ratio (CI) | verdict | n/side |",
              "|---|---|---|---|---|---|"]
    for m in sorted(fin):
        rec = fin[m]
        f = fp.get(m, {})
        lines.append(
            f"| {m} | {f.get('ratio', 0):.4f} "
            f"[{f.get('ci95', [0, 0])[0]:.4f}, {f.get('ci95', [0, 0])[1]:.4f}] | "
            f"{rec['before_median']:.4g} / {rec['after_median']:.4g} | "
            f"{rec['ratio']:.4f} [{rec['ci95'][0]:.4f}, {rec['ci95'][1]:.4f}] | "
            f"{rec['verdict']} | {rec['n'][0]} |")
    lines += ["", "Verdict rule (ceiling 1.05): PASS ci_hi<=1.05; NOISE "
              "median<=1.05<ci_hi (2xN rerun once; after it: NOISE-UNRESOLVED, "
              "never coerced); REGRESSION ci_lo>1.05 -> STOP.", ""]
    mix = result.get("verdict_mix", {})
    if mix:
        lines.append("## verdict-mix + capture counts (accumulated over all rounds)")
        for side in ("before", "after"):
            slot = mix.get(side, {})
            lines.append(f"- {side}: totals {slot.get('totals', {})}")
            for name, s in sorted((slot.get("per_action_type") or {}).items()):
                lines.append(f"    - {name}: {s}")
        lines.append("")
    lines.append("Raw series: .orvex/mission/artifacts/perf/wp11/series.json; "
                 "per-round evidence: .orvex/mission/artifacts/perf/wp11/rounds/")
    return "\n".join(lines) + "\n"


def diagnose(before: Path, after: Path) -> dict:
    out_dir = WP11_DIR / "diag"
    out_dir.mkdir(parents=True, exist_ok=True)
    per_side = {}
    with tempfile.TemporaryDirectory(prefix="wp11-diag-") as tmp:
        child = Path(tmp) / "_profile_child.py"
        child.write_text(PROFILE_CHILD
                         .replace("{execs}", str(PROFILE_EXECS))
                         .replace("{src_prefix}",
                                  (Path("{tree}") / "src" / "computer_use_mcp").as_posix() + "/")
                         .replace("{tools_dir}", str(RESTRUCTURE_DIR)),
                         encoding="utf-8")
        for side, tree in (("before", before), ("after", after)):
            script = child.read_text(encoding="utf-8").replace(
                "{tree}", str(tree))
            side_child = Path(tmp) / f"_profile_child_{side}.py"
            side_child.write_text(script, encoding="utf-8")
            per_side[side] = run_child(side_child, [], tree, timeout=300)
            (out_dir / f"profile_{side}.json").write_text(
                json.dumps(per_side[side], indent=2) + "\n", encoding="utf-8")
    cb, ca = per_side["before"]["call_counts"], per_side["after"]["call_counts"]

    def name_key(k: str) -> tuple[str, str]:
        if ":" not in k:
            return (k, "")
        path_part, rest = k.rsplit(":", 1)
        qual = rest.split("(", 1)[1].rstrip(")") if "(" in rest else rest
        return (path_part, qual)

    def totals(counts: dict) -> dict:
        out: dict[tuple[str, str], int] = {}
        for k, v in counts.items():
            key = name_key(k)
            out[key] = out.get(key, 0) + v
        return out

    tb, ta = totals(cb), totals(ca)
    diffs = [{"func": f"{p}({fn})", "before": tb.get((p, fn), 0),
              "after": ta.get((p, fn), 0), "delta": ta.get((p, fn), 0) - tb.get((p, fn), 0)}
             for (p, fn) in sorted(set(tb) | set(ta))
             if tb.get((p, fn), 0) != ta.get((p, fn), 0)]
    diffs.sort(key=lambda d: -abs(d["delta"]))
    result = {"stage": "WP11-diagnose", "executions_per_side": PROFILE_EXECS,
              "top_by_cumtime": {s: per_side[s]["top_by_cumtime"] for s in per_side},
              "call_count_diffs_by_qualname": diffs[:60],
              "diff_count": len(diffs),
              "note": ("counts from RAW profiler.getstats() keyed by "
                       "(normalized co_filename, co_firstlineno, co_qualname); "
                       "name-keyed diff ignores line-number offsets")}
    (out_dir / "diagnose.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wp11_perf")
    parser.add_argument("--before", default=str(Path(os.environ["TEMP"]) / "orvex-108" / "wt-v075")
                        if os.environ.get("TEMP") else None,
                        help="BEFORE tree (the detached v0.7.5 worktree)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("phase", help="append one measurement phase")
    p.add_argument("--name", required=True,
                   choices=["dry", "hot", "import-cold", "import-warm",
                            "rerun-hot", "rerun-import-cold", "rerun-import-warm"])
    p.add_argument("--rounds", type=int, default=4)
    p.add_argument("--pairs", type=int, default=10)
    sub.add_parser("compute", help="statistics + verdicts; writes the gate artifacts")
    d = sub.add_parser("diagnose", help="cProfile comparison (STOP cause only)")
    args = parser.parse_args(argv)

    if not args.before:
        raise SystemExit("--before is required (the detached v0.7.5 worktree path)")
    before, after = Path(args.before).resolve(), REPO
    if args.command == "phase":
        blob = load_blob(before, after)
        if args.name == "dry":
            dry = phase_dry(before, after)
            print(json.dumps({"phase": "dry",
                              "before_captures_per_action": dry["before"]["hot"]["captures_per_action"],
                              "after_captures_per_action": dry["after"]["hot"]["captures_per_action"],
                              "dry_file": str(WP11_DIR / "dry.json")}))
            return 0
        if args.name in ("hot", "rerun-hot"):
            res = phase_hot(before, after, blob, args.rounds, rerun=args.name == "rerun-hot")
        elif args.name in ("import-cold", "rerun-import-cold"):
            res = phase_import(before, after, blob, args.pairs, warm=False,
                               rerun=args.name == "rerun-import-cold")
        else:
            res = phase_import(before, after, blob, args.pairs, warm=True,
                               rerun=args.name == "rerun-import-warm")
        print(json.dumps(res))
        return 0
    if args.command == "compute":
        result = compute()
        print(json.dumps({k: result.get(k) for k in
                          ("verdict", "regression_metrics", "noise_unresolved",
                           "rerun_required", "noise_downgraded_after_rerun")}))
        return 0
    if args.command == "diagnose":
        print(json.dumps(diagnose(before, after))[:1200])
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
