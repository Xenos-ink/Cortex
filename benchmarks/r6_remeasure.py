#!/usr/bin/env python
"""R-6 AFTER-STATE RE-MEASURE (ORVEX-CORTEX-056-LIVEFIX, mission goal section 7).

Honest per-action mechanical measurement on the REAL desktop, INPUT STUBBED
(the backend's ``execute`` is replaced with a no-op — ZERO OS input; screen
READING only). REAL capture, REAL encode/key, REAL digest, REAL verification
pixel-diff, REAL identity probe, REAL audit + metrics + rate-gate bookkeeping.

Measures the full computer_execute pipeline per action:
  direct_request capture + identity-probe validate + [exec stub] +
  post_action capture + verification (raw-frame fast path).

Configurations (the R-6 matrix) — each config runs in its OWN SUBPROCESS:
Windows allows ONE IDXGIOutputDuplication per output per process, and the
matrix holds one backend per config, so in-process accumulation would break
the dxgi configs (measured: the second live duplication fails construction
and the backend silently takes its blt fallback — the very state the first
matrix run accidentally measured).

  A. blt  + PNG  (R-5 after-state — the regression baseline)
  B. dxgi + PNG  (the capture swap alone)
  C. dxgi + raw-key (text-session encode-skip — the R-6 after-state)
  D. blt  + raw-key (encode-skip without the capture swap)

Each subprocess runs two pacing profiles:
  paced (300 ms between calls — live-like fast host; gate rarely fires)
  back-to-back (exposes the min_screenshot_interval_ms=250 gate)

Env knobs honored (documented, not required): CORTEX_CAPTURE, CORTEX_INTERNAL_FRAME_REUSE,
CORTEX_TEXT_PNG, CORTEX_PNG_COMPRESS_LEVEL.
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
import asyncio, io, json, os, sys, tempfile, time
REPO_ROOT = sys.argv[1]
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
label, capture_env, raw_keys = sys.argv[2], sys.argv[3], sys.argv[4] == "1"

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
    backend = LocalComputerBackend()
    backend.execute = lambda *a, **k: "Simulated click (input stubbed: no OS input dispatched)."
    if raw_keys:
        backend._raw_payload_keys = True
    report = {}

    def build_agent():
        log_dir = tempfile.mkdtemp(prefix="r6_remeasure_")
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

    agent_b = build_agent()
    await run_actions(agent_b, 2, None)
    report["back_to_back"] = ms(await run_actions(agent_b, 8, None))

    snap = agent_a.metrics.snapshot()["latencies"]
    for name in ("observation_ms", "verification_ms"):
        if snap.get(name) and snap[name].get("count"):
            st = snap[name]
            report["audit_" + name] = {"n": st["count"], "p50": st["p50_ms"] or 0.0,
                                       "p95": st["p95_ms"] or 0.0, "mean": st["avg_ms"] or 0.0,
                                       "max": st["max_ms"] or 0.0}

    xs = []
    for _ in range(10):
        t0 = time.perf_counter(); backend.observe(); xs.append((time.perf_counter()-t0)*1000.0)
    report["observe_full"] = ms(xs)

    before = backend.observe(); after = backend.observe()
    from computer_use_mcp.verification import VerificationIntent, VerificationKind
    engine = VerificationEngine()
    intent = VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=True)
    xs = []
    for _ in range(8):
        t0 = time.perf_counter(); engine.verify(intent, before, after); xs.append((time.perf_counter()-t0)*1000.0)
    report["verify_fastpath"] = ms(xs)

    frame = getattr(after, "_frame", None)
    if frame is not None:
        import base64 as b64mod
        if raw_keys:
            import hashlib
            raw = frame.tobytes()
            xs = []
            for _ in range(8):
                t0 = time.perf_counter()
                "RAW:" + b64mod.b64encode(hashlib.sha256(raw).digest()).decode("ascii")
                xs.append((time.perf_counter()-t0)*1000.0)
            report["payload_key_hash"] = ms(xs)
        else:
            xs = []
            for _ in range(8):
                out = io.BytesIO()
                t0 = time.perf_counter()
                frame.save(out, format="PNG")
                xs.append((time.perf_counter()-t0)*1000.0)
            report["payload_png_encode"] = ms(xs)

    report["_resolved"] = {"capture_backend": backend._capture_backend,
                           "dxgi_live": backend._dxgi is not None,
                           "raw_key_enabled": backend._raw_key_enabled}
    print("JSON:" + json.dumps(report))

asyncio.run(run_config())
'''


def _run_child(capture_env: str, raw_keys: bool, label: str) -> dict:
    """One matrix cell in its own process (one duplication per process)."""
    proc = subprocess.run(
        [sys.executable, "-c", CHILD_SOURCE, REPO_ROOT, label, capture_env,
         "1" if raw_keys else "0"],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    for line in proc.stdout.splitlines():
        if line.startswith("JSON:"):
            return json.loads(line[5:])
    raise RuntimeError(f"config {label} produced no report:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")


def main() -> int:
    matrix: dict[str, dict] = {}
    matrix["A_blt_png"] = _run_child("-", False, "A_blt_png")
    matrix["B_dxgi_png"] = _run_child("dxgi", False, "B_dxgi_png")
    matrix["C_dxgi_key"] = _run_child("dxgi", True, "C_dxgi_key")
    matrix["D_blt_key"] = _run_child("-", True, "D_blt_key")

    print("\n=== R-6 matrix (real desktop, input stubbed, subprocess-isolated) ===")
    for label, rep in matrix.items():
        res = rep.get("_resolved", {})
        print(
            f"  {label:12s} resolved: capture={res.get('capture_backend')} "
            f"dxgi_live={res.get('dxgi_live')} raw_key={res.get('raw_key_enabled')}"
        )
    print("\n=== R-6 per-action p50 comparison ===")
    for label, rep in matrix.items():
        paced = rep["paced_300ms"]["p50"]
        b2b = rep["back_to_back"]["p50"]
        obs = rep.get("audit_observation_ms", {}).get("p50", 0.0)
        ver = rep.get("audit_verification_ms", {}).get("p50", 0.0)
        enc = rep.get("payload_png_encode", {}).get("p50", 0.0) or 0.0
        key = rep.get("payload_key_hash", {}).get("p50", 0.0) or 0.0
        print(
            f"  {label:12s} paced p50={paced:7.1f}ms ({paced/52.0:5.1f}x-52ms-bar) "
            f"b2b p50={b2b:7.1f}ms | audit obs p50={obs:6.1f} ver p50={ver:6.1f} "
            f"encode p50={enc:5.1f} key p50={key:4.1f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
