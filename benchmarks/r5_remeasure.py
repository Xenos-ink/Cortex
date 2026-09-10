#!/usr/bin/env python
"""R-5 AFTER-STATE RE-MEASURE (ORVEX-CORTEX-056-LIVEFIX).

Honest per-action mechanical measurement on the REAL desktop:
- REAL capture (mss BitBlt), REAL encode/digest, REAL verification pixel-diff,
  REAL identity probe, REAL audit + metrics + rate-gate bookkeeping.
- INPUT STUBBED: the backend's ``execute`` is replaced with a no-op — ZERO OS input
  is injected (screen READING only). The live-measured execution stage (p50 18.6 ms,
  from the L-1 audit) is added separately in the projection.

Measures per action (computer_execute shape):
  direct_request capture + identity-probe (validate, no capture when fresh) +
  [exec stub] + post_action capture + verification (raw-frame fast path).

Two pacing profiles:
  A) live-like pacing (300 ms between calls — a fast host; rate gate rarely fires)
  B) back-to-back calls (exposes the min_screenshot_interval_ms=250 gate cost)

Also re-measures the isolated stages (observe, verify fast/slow, outbound bound).
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))


def _ms(xs: list[float]) -> dict[str, float]:
    xs = sorted(xs)
    return {
        "n": len(xs),
        "p50": xs[len(xs) // 2],
        "p95": xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))],
        "mean": sum(xs) / len(xs),
        "max": xs[-1],
    }


async def main() -> int:
    from computer_use_mcp.agent import ComputerUseAgent
    from computer_use_mcp.audit import AuditLogger, Metrics
    from computer_use_mcp.backend import LocalComputerBackend
    from computer_use_mcp.limits import LimitEnforcer, Limits
    from computer_use_mcp.models import GroundedAction, SessionState
    from computer_use_mcp.safety import SafetyPolicy
    from computer_use_mcp.state import StopToken, TaskState
    from computer_use_mcp.validator import GroundingValidator
    from computer_use_mcp.verification import VerificationEngine

    backend = LocalComputerBackend()
    backend.execute = lambda *a, **k: "Simulated click (input stubbed: no OS input dispatched)."

    report: dict[str, dict] = {}

    def build_agent() -> ComputerUseAgent:
        log_dir = tempfile.mkdtemp(prefix="r5_remeasure_")
        return ComputerUseAgent(
            backend,
            None,  # provider unused on the direct path
            safety=SafetyPolicy(),
            validator=GroundingValidator(),
            verifier=VerificationEngine(),
            session_id="r5-rem",
            task=TaskState(session_id="r5-rem"),
            stop=StopToken(),
            limits=Limits(),  # LIVE defaults incl. min_screenshot_interval_ms=250
            enforcer=LimitEnforcer(Limits().validate()),
            auditor=AuditLogger(log_dir),
            metrics=Metrics(),
        )

    state = SessionState(session_id="r5-rem", dry_run=False, require_approval=False)

    async def run_actions(agent: ComputerUseAgent, count: int, gap: float | None) -> list[float]:
        times: list[float] = []
        for i in range(count):
            if gap is not None and i:
                await asyncio.sleep(gap)
            action = GroundedAction(
                action="click", point={"x": 100, "y": 100},
                reason="re-measure", confidence=1.0,
                expected_effect="the screen changes",
            )
            t0 = time.perf_counter()
            outcome = await agent.run_single(state, action, approved=True)
            times.append((time.perf_counter() - t0) * 1000.0)
            assert outcome.kind == "executed", (outcome.kind, outcome.message)
        return times

    # Profile A: live-like pacing (a fast host; model gap >> gate interval)
    agent_a = build_agent()
    await run_actions(agent_a, 3, 0.3)  # warm-up
    paced = await run_actions(agent_a, 12, 0.3)
    report["per_action_paced_300ms"] = _ms(paced)

    # Profile B: back-to-back (fastest possible host — exposes the 250ms gate)
    agent_b = build_agent()
    await run_actions(agent_b, 2, None)
    back_to_back = await run_actions(agent_b, 8, None)
    report["per_action_back_to_back"] = _ms(back_to_back)

    # Audit-stage view of the paced profile (the mechanical stage labels)
    snapshot = agent_a.metrics.snapshot()["latencies"]
    for name in ("observation_ms", "execution_ms", "verification_ms"):
        if snapshot.get(name) and snapshot[name].get("count"):
            stats = snapshot[name]
            report[f"audit_{name}"] = {
                "n": stats["count"], "p50": stats["p50_ms"] or 0.0,
                "p95": stats["p95_ms"] or 0.0, "mean": stats["avg_ms"] or 0.0,
                "max": stats["max_ms"] or 0.0,
            }
    counters = agent_a.metrics.snapshot()["counters"]
    report["counters"] = {"n": 1, "p50": float(counters.get("observation_reuse", 0)),
                          "p95": 0.0, "mean": 0.0, "max": 0.0}
    report["probe_drift_count"] = {"n": 1, "p50": float(counters.get("identity_probe_drift", 0)),
                                   "p95": 0.0, "mean": 0.0, "max": 0.0}

    # Isolated stages after R-5
    xs = []
    for _ in range(10):
        t0 = time.perf_counter(); backend.observe(); xs.append((time.perf_counter() - t0) * 1000.0)
    report["observe_full_after"] = _ms(xs)

    ref = backend.observe()
    xs = []
    for _ in range(10):
        t0 = time.perf_counter(); backend.identity_probe(ref); xs.append((time.perf_counter() - t0) * 1000.0)
    report["identity_probe_after"] = _ms(xs)

    before = backend.observe()
    after = backend.observe()
    from computer_use_mcp.verification import VerificationIntent, VerificationKind
    engine = VerificationEngine()
    intent = VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=True)
    xs = []
    for _ in range(10):
        t0 = time.perf_counter(); engine.verify(intent, before, after); xs.append((time.perf_counter() - t0) * 1000.0)
    report["verify_fastpath_after"] = _ms(xs)

    from computer_use_mcp import server as srv
    xs = []
    for _ in range(8):
        t0 = time.perf_counter()
        srv._bound_outbound_image(after.image_base64, frame=getattr(after, "_frame", None))
        xs.append((time.perf_counter() - t0) * 1000.0)
    report["bound_outbound_after"] = _ms(xs)

    print("\n=== R-5 AFTER-STATE (real desktop, input stubbed) ===")
    for key, stats in report.items():
        print(f"  {key:36s} n={stats['n']:>2} p50={stats['p50']:8.1f}ms p95={stats['p95']:8.1f} mean={stats['mean']:8.1f} max={stats['max']:8.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
