"""E9 Probe 5: limit escape via tool parameters and session cap.

- max_steps=999999 / negative / zero -> rejected by SessionState bounds (ge=1, le=500);
- limits dict with absurd / negative / wrong-type / unknown-field values -> clamped or
  rejected by Limits.validate via _parse_limits (fail closed);
- concurrent sessions beyond the registry cap -> session_limit_exceeded (no eviction);
- run_goal internal loop IS rate-limited (screenshot gate) while computer_observe is an
  explicit unthrottled client tool (finding F5, assessed for documentation).
"""

from __future__ import annotations

import sys
import time
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
    # ---- absurd / negative / wrong-type limits ------------------------------------------
    L.reset_server()
    r1 = server.start_session(max_steps=999999, dry_run=False)
    check("max_steps_999999_rejected", r1.get("ok") is False and not r1.get("session_id"),
          f"error={r1.get('error')} message={str(r1.get('message'))[:80]!r}")
    r2 = server.start_session(max_steps=-5, dry_run=False)
    check("max_steps_negative_rejected", r2.get("ok") is False, f"error={r2.get('error')}")
    r3 = server.start_session(max_steps=0, dry_run=False)
    check("max_steps_zero_rejected", r3.get("ok") is False, f"error={r3.get('error')}")

    r4 = server.start_session(limits={"max_actions": 999999}, dry_run=False)
    sid4 = r4.get("session_id")
    check("limits_absurd_clamped", sid4 and server._get_bundle(sid4).limits.max_actions == 500,
          f"max_actions clamped to {server._get_bundle(sid4).limits.max_actions if sid4 else None}")
    r5 = server.start_session(limits={"max_actions": -50, "max_task_seconds": -1}, dry_run=False)
    sid5 = r5.get("session_id")
    b5 = server._get_bundle(sid5)
    check("limits_negative_clamped_to_minimum",
          b5.limits.max_actions == 1 and b5.limits.max_task_seconds == 1.0,
          f"max_actions={b5.limits.max_actions} max_task_seconds={b5.limits.max_task_seconds} (floor, fail closed)")
    r6 = server.start_session(limits={"max_actions": "many"}, dry_run=False)
    check("limits_wrong_type_rejected", r6.get("ok") is False and r6.get("error") == "invalid_limits",
          f"error={r6.get('error')}")
    r7 = server.start_session(limits={"not_a_limit": 5}, dry_run=False)
    check("limits_unknown_field_rejected", r7.get("ok") is False and r7.get("error") == "invalid_limits",
          f"error={r7.get('error')}")
    r8 = server.start_session(limits=[("max_actions", 5)], dry_run=False)
    check("limits_non_dict_rejected", r8.get("ok") is False, f"error={r8.get('error')}")

    # ---- max_model_calls trip through the loop -------------------------------------------
    L.reset_server()
    backend = L.ScriptedBackend(flip=True)
    provider = L.ScriptedProvider(
        [AgentDecision(status="action", action=GroundedAction(
            action="click", point={"x": 10, "y": 10}, confidence=1.0), summary="c")] * 100
    )
    sid, bundle, backend, provider = L.make_session(
        backend, provider, dry_run=False, require_approval=False,
        limits={"min_screenshot_interval_ms": 0, "max_model_calls": 3},
    )
    resp = L.run(server.run_goal(session_id=sid, goal="model call limit"))
    check("max_model_calls_trips_fail_closed",
          resp.get("termination_reason") == "limit_exceeded" and resp.get("ok") is False,
          f"termination={resp.get('termination_reason')} ok={resp.get('ok')} "
          f"model_calls={resp.get('metrics', {}).get('counters', {}).get('model_calls')}")

    # ---- concurrent sessions beyond cap ---------------------------------------------------
    L.reset_server()
    import computer_use_mcp.state as cstate
    server._registry = cstate.SessionRegistry(max_sessions=4)  # production default cap
    ids = []
    results = []
    for i in range(6):
        results.append(server.start_session(dry_run=True))
        if results[-1].get("session_id"):
            ids.append(results[-1]["session_id"])
    exceeded = [r for r in results if r.get("error") == "session_limit_exceeded"]
    check("session_cap_enforced", len(ids) == 4 and len(exceeded) == 2,
          f"created={len(ids)}/6, refused={len(exceeded)} (default cap 4, no eviction)")
    check("live_sessions_not_evicted", all(server._registry.get(i) is not None for i in ids),
          "the 4 live sessions remain registered (fail-closed, never evicted)")

    # ---- computer_observe flooding: explicit tool, measured --------------------------------
    L.reset_server()
    backend_f = L.ScriptedBackend(flip=True)
    sidf, bundlef, backend_f, _ = L.make_session(
        backend_f, L.ScriptedProvider([]), dry_run=False, require_approval=False,
        limits={"min_screenshot_interval_ms": 0},
    )
    n = 200
    t0 = time.perf_counter()
    for _ in range(n):
        server.computer_observe(session_id=sidf)
    dt = time.perf_counter() - t0
    shots = bundlef.metrics.snapshot()["counters"]["screenshot_count"]
    print(f"    computer_observe x{n}: {dt:.2f}s ({n / dt:.0f}/s) — unthrottled by design "
          f"(F5: explicit client tool)")
    check("computer_observe_unthrottled_documented_finding", shots == n,
          f"{shots}/{n} captures with zero interval gating (accepted-with-documentation "
          f"candidate; run_goal's INTERNAL loop IS rate-gated)")

    # ---- run_goal internal loop IS rate-limited --------------------------------------------
    L.reset_server()
    backend_r = L.ScriptedBackend(flip=True)
    sidr, bundler, backend_r, provider_r = L.make_session(
        backend_r, L.ScriptedProvider(
            [AgentDecision(status="action", action=GroundedAction(
                action="click", point={"x": 10, "y": 10}, confidence=1.0), summary="c")] * 6
            + [AgentDecision(status="done", summary="done")]
        ), dry_run=False, require_approval=False, limits={},
    )
    t0r = time.perf_counter()
    tres = L.run(server.run_goal(session_id=sidr, goal="rate limit check"))
    elapsed_r = time.perf_counter() - t0r
    internal_shots = tres.get("metrics", {}).get("counters", {}).get("screenshot_count", 0)
    # 6 actions x 3 captures each (loop_top/validate/post_action) = 18 gated captures;
    # the 250ms gate must make the run take at least (captures-1) * 0.25s.
    min_expected_seconds = 0.20 * (internal_shots - 1)
    check("run_goal_internal_loop_rate_gated",
          tres.get("termination_reason") == "completed" and elapsed_r >= min_expected_seconds,
          f"{internal_shots} internal captures for 6 actions in {elapsed_r:.2f}s "
          f"(min_screenshot_interval_ms=250 enforced -> >= {min_expected_seconds:.1f}s expected)")

    print("\n=== PROBE5 LIMIT ESCAPE RESULTS ===")
    if fails:
        print(f"VERDICT: FAIL — {len(fails)} failing checks")
        for f in fails:
            print("  FAIL:", f)
        return 1
    print("VERDICT: PASS — limits clamp/reject fail-closed; session cap refuses without")
    print("eviction; loop rate-gated. F5 (computer_observe unthrottled) recorded as finding.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
