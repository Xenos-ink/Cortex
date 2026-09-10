#!/usr/bin/env python
"""R-7 AFTER-STATE RE-MEASURE (ORVEX-CORTEX-056-LIVEFIX, mission goal section 7).

Same measurement contract as benchmarks/r6_remeasure.py — real desktop, INPUT
STUBBED (backend.execute replaced with a no-op: ZERO OS input; screen READING
only), REAL capture, REAL encode/key, REAL digest, REAL verification pixel-diff
(now with the R-7 fast diff), REAL identity probe, REAL audit + metrics bookkeeping
— restricted to the mission's DEFAULT-MODEL PROFILE (config C: CORTEX_CAPTURE=dxgi
+ raw-key text payloads) plus the A/B references for context.

Each config runs in its OWN SUBPROCESS (one dxgi duplication per process).
Pacing: 300 ms between calls (live-like fast host).

Env knobs honored: CORTEX_CAPTURE, CORTEX_INTERNAL_FRAME_REUSE, CORTEX_TEXT_PNG,
CORTEX_DIFF_FAST (the R-7 kill-switch — remeasured in both states).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

CHILD_SOURCE = r'''
import asyncio, json, os, sys, tempfile, time
REPO_ROOT = sys.argv[1]
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
label, capture_env, raw_keys, diff_fast = sys.argv[2], sys.argv[3], sys.argv[4] == "1", sys.argv[5]

def ms(xs):
    xs = sorted(xs)
    return {"n": len(xs), "p50": xs[len(xs)//2],
            "p95": xs[min(len(xs)-1, int(round(0.95*(len(xs)-1))))],
            "mean": sum(xs)/len(xs), "max": xs[-1]}

async def run_config():
    from computer_use_mcp.agent import ComputerUseAgent
    from computer_use_mcp.audit import AuditLogger, Metrics
    from computer_use_mcp.backend import LocalComputerBackend
    from computer_use_mcp.limits import LimitEnforcer, Limits
    from computer_use_mcp.models import GroundedAction, SessionState
    from computer_use_mcp.safety import SafetyPolicy
    from computer_use_mcp.state import StopToken, TaskState
    from computer_use_mcp.validator import GroundingValidator
    from computer_use_mcp.verification import VerificationEngine

    if capture_env == "-":
        os.environ.pop("CORTEX_CAPTURE", None)
    else:
        os.environ["CORTEX_CAPTURE"] = capture_env
    if diff_fast == "-":
        os.environ.pop("CORTEX_DIFF_FAST", None)
    else:
        os.environ["CORTEX_DIFF_FAST"] = diff_fast
    backend = LocalComputerBackend()
    backend.execute = lambda *a, **k: "Simulated click (input stubbed: no OS input dispatched)."
    if raw_keys:
        backend._raw_payload_keys = True
    report = {}

    def build_agent():
        log_dir = tempfile.mkdtemp(prefix="r7_remeasure_")
        return ComputerUseAgent(
            backend, None, safety=SafetyPolicy(),
            validator=GroundingValidator(), verifier=VerificationEngine(),
            session_id=label, task=TaskState(session_id=label),
            stop=StopToken(), limits=Limits(),
            enforcer=LimitEnforcer(Limits().validate()),
            auditor=AuditLogger(log_dir), metrics=Metrics(),
        )

    state = SessionState(session_id=label, dry_run=False, require_approval=False)

    async def run_actions(agent, count, gap):
        times = []
        for i in range(count):
            if gap is not None and i:
                await asyncio.sleep(gap)
            action = GroundedAction(action="click", point={"x": 100, "y": 100},
                                    reason="re-measure", confidence=1.0,
                                    expected_effect="the screen changes")
            t0 = time.perf_counter()
            outcome = await agent.run_single(state, action, approved=True)
            times.append((time.perf_counter() - t0) * 1000.0)
            assert outcome.kind == "executed", (outcome.kind, outcome.message)
        return times

    agent_a = build_agent()
    await run_actions(agent_a, 3, 0.3)
    report["paced_300ms"] = ms(await run_actions(agent_a, 12, 0.3))

    snap = agent_a.metrics.snapshot()["latencies"]
    for name in ("observation_ms", "verification_ms"):
        if snap.get(name) and snap[name].get("count"):
            st = snap[name]
            report["audit_" + name] = {"n": st["count"], "p50": st["p50_ms"] or 0.0,
                                       "p95": st["p95_ms"] or 0.0, "mean": st["avg_ms"] or 0.0,
                                       "max": st["max_ms"] or 0.0}

    report["_resolved"] = {"capture_backend": backend._capture_backend,
                           "dxgi_live": backend._dxgi is not None,
                           "raw_key_enabled": backend._raw_key_enabled,
                           "diff_fast_env": os.getenv("CORTEX_DIFF_FAST", "<unset>")}
    print("JSON:" + json.dumps(report))

asyncio.run(run_config())
'''


def _run_child(capture_env: str, raw_keys: bool, diff_fast: str, label: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", CHILD_SOURCE, REPO_ROOT, label, capture_env,
         "1" if raw_keys else "0", diff_fast],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    for line in proc.stdout.splitlines():
        if line.startswith("JSON:"):
            return json.loads(line[5:])
    raise RuntimeError(f"config {label} produced no report:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")


def main() -> int:
    matrix: dict[str, dict] = {}
    # mission profile C (dxgi + raw key) with fast diff ON, then kill-switch OFF for the delta
    matrix["C_dxgi_key_fast"] = _run_child("dxgi", True, "1", "C_fast")
    matrix["C_dxgi_key_legacy"] = _run_child("dxgi", True, "0", "C_legacy")
    # references
    matrix["A_blt_png_fast"] = _run_child("-", False, "1", "A_fast")

    print("\n=== R-7 re-measure (real desktop, input stubbed, subprocess-isolated) ===")
    for label, rep in matrix.items():
        res = rep.get("_resolved", {})
        print(
            f"  {label:18s} capture={res.get('capture_backend')} dxgi_live={res.get('dxgi_live')} "
            f"raw_key={res.get('raw_key_enabled')} diff_fast={res.get('diff_fast_env')}"
        )
    print("\n=== per-action p50 ===")
    for label, rep in matrix.items():
        paced = rep["paced_300ms"]["p50"]
        obs = rep.get("audit_observation_ms", {}).get("p50", 0.0)
        ver = rep.get("audit_verification_ms", {}).get("p50", 0.0)
        print(
            f"  {label:18s} paced p50={paced:7.1f}ms ({paced/52.0:5.1f}x-52ms-bar) "
            f"| audit obs p50={obs:6.1f} ver p50={ver:6.1f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
