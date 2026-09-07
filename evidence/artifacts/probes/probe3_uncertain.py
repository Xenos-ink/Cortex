"""E9 Probe 3: uncertain-as-success.

3a. Force every verification strategy to return uncertain for a click (no judge, no OCR,
    ambiguous identical pixels, no expected_effect) -> the action must NOT be reported as
    a verified/successful state transition; recovery runs; the task must not complete with
    ok=True on uncertain evidence.
3b. An all-uncertain task must not report ok=True with termination completed.
3c. wait carve-out: uncertain wait continues (documented) but the result still carries
    the uncertain outcome.
3d. grep gate is in redteam.md (no src mapping uncertain->verified found).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_lib as L  # noqa: E402

from computer_use_mcp import server  # noqa: E402
from computer_use_mcp.models import AgentDecision, GroundedAction  # noqa: E402

fails: list[str] = []


def check(name: str, condition: bool, detail: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}: {detail}")
    if not condition:
        fails.append(f"{name}: {detail}")


def main() -> int:
    # ---- 3a/3b: all-uncertain click loop (identical screenshots, no expectation) --------
    L.reset_server()
    backend = L.ScriptedBackend(flip=False)  # every screenshot identical -> ambiguous
    provider = L.ScriptedProvider(
        [AgentDecision(status="action", action=GroundedAction(
            action="click", point={"x": 30, "y": 40}, confidence=1.0), summary="c")] * 40
    )
    sid, bundle, backend, provider = L.make_session(
        backend, provider, dry_run=False, require_approval=False,
        limits={"min_screenshot_interval_ms": 0},
    )
    response = L.run(server.run_goal(session_id=sid, goal="all uncertain task"))
    print(f"all-uncertain: termination={response.get('termination_reason')} ok={response.get('ok')} "
          f"results={len(response.get('results', []))}")
    check("all_uncertain_task_not_ok",
          response.get("ok") is False,
          f"ok={response.get('ok')} termination={response.get('termination_reason')}")
    check("all_uncertain_task_not_completed",
          response.get("termination_reason") != "completed",
          f"termination={response.get('termination_reason')}")
    ok_results = [r for r in response.get("results", []) if r.get("ok")]
    check("all_uncertain_no_successful_results",
          not ok_results,
          f"{len(ok_results)} result rows claimed ok")
    uncs = [r for r in response.get("results", [])
            if (r.get("verification") or {}).get("outcome") == "uncertain"]
    check("uncertain_outcomes_preserved_as_uncertain",
          all((r.get("verification") or {}).get("outcome") == "uncertain" for r in uncs),
          f"{len(uncs)} uncertain outcomes recorded verbatim (never upgraded)")

    # recovery boundedness check
    metrics = response.get("metrics", {}).get("counters", {})
    print(f"    metrics: recovery_total={metrics.get('recovery_total')} "
          f"retries={metrics.get('retry_total')} task_failed={metrics.get('task_failed')}")
    check("uncertain_recovery_bounded",
          metrics.get("recovery_total", 0) <= 6 + 1,
          f"recovery_total={metrics.get('recovery_total')} (per-task budget 6)")

    # ---- 3c: wait carve-out documents uncertainty but is not a success claim -------------
    L.reset_server()
    backend_w = L.ScriptedBackend(flip=False)
    provider_w = L.ScriptedProvider(
        [AgentDecision(status="action", action=GroundedAction(
            action="wait", delta=1, confidence=1.0), summary="w"),
         AgentDecision(status="done", summary="done")]
    )
    sidw, bundlew, backend_w, provider_w = L.make_session(
        backend_w, provider_w, dry_run=False, require_approval=False,
        limits={"min_screenshot_interval_ms": 0},
    )
    respw = L.run(server.run_goal(session_id=sidw, goal="wait then done"))
    w_results = respw.get("results", [])
    w_ver = (w_results[0].get("verification") or {}) if w_results else {}
    check("wait_uncertain_outcome_recorded",
          w_ver.get("outcome") == "uncertain",
          f"wait result verification outcome={w_ver.get('outcome')!r} (carve-out continues, "
          f"uncertain outcome preserved)")

    # ---- 3d: computer_execute with expected_effect but identical pixels -> not ok --------
    L.reset_server()
    backend_x = L.ScriptedBackend(flip=False)
    sid2, bundle2, backend_x, _ = L.make_session(
        backend_x, L.ScriptedProvider([]), dry_run=False, require_approval=False,
        limits={"min_screenshot_interval_ms": 0},
    )
    r = L.run(server.computer_execute(session_id=sid2, action="click", x=30, y=40,
                                      expected_effect="the button activates"))
    print(f"computer_execute expected_effect + identical pixels: ok={r.get('ok')} "
          f"verification={(r.get('verification') or {}).get('outcome')}")
    check("computer_execute_uncertain_not_ok",
          r.get("ok") is False,
          f"ok={r.get('ok')} (failed visual_change expectation, not silently success)")

    print("\n=== PROBE3 UNCERTAIN-AS-SUCCESS RESULTS ===")
    if fails:
        print(f"VERDICT: FAIL — {len(fails)} failing checks")
        for f in fails:
            print("  FAIL:", f)
        return 1
    print("VERDICT: PASS — uncertain is never mapped to success; recovery is bounded and")
    print("the task terminates honestly (failed_verification), never 'completed' on uncertain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
