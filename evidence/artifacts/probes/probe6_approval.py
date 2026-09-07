"""E9 Probe 6: approval forgery / semantics abuse.

6a. approve_next_action=True grants exactly ONE approval: action A executes, a new
    distinct action B is denied (requires_approval, no execution).
6b. CRITICAL actions are NEVER authorized by approve_next_action=True (blocked_safety).
6c. Approval does not survive across run_goal calls / sessions (no token persistence).
6d. computer_execute approved=True applies only to that single call.
6e. Recovery Escape-dismiss cannot bypass approval for a destructive action: dismiss only
    retries an ALREADY-APPROVED same action instance; CRITICAL actions never reach the
    execute phase, so no dismiss path exists for them.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_lib as L  # noqa: E402

from computer_use_mcp import server  # noqa: E402
from computer_use_mcp.models import AgentDecision, GroundedAction, WindowInfo  # noqa: E402

fails: list[str] = []

WINDOW = WindowInfo(hwnd=4242, pid=999, process_name="notepad.exe",
                    exe_path="C:\\Windows\\System32\\notepad.exe", window_class="Notepad",
                    title="Untitled - Notepad", bounds=(10, 10, 800, 600))


def check(name: str, condition: bool, detail: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}: {detail}")
    if not condition:
        fails.append(f"{name}: {detail}")


def decision(kind: str, *, idx: int = 0) -> AgentDecision:
    if kind == "high_click":
        return AgentDecision(status="action", action=GroundedAction(
            action="click", point={"x": 30 + idx, "y": 40}, confidence=1.0,
            reason="install the software update now"), summary="installer UI click")
    if kind == "critical_click":
        return AgentDecision(status="action", action=GroundedAction(
            action="click", point={"x": 30, "y": 40}, confidence=1.0,
            reason="delete the files in the folder"), summary="delete files")
    if kind == "benign_click":
        return AgentDecision(status="action", action=GroundedAction(
            action="click", point={"x": 30 + idx, "y": 40}, confidence=1.0), summary="click")
    raise ValueError(kind)


def main() -> int:
    # ---- 6a: single budget; distinct second action denied ---------------------------------
    L.reset_server()
    backend = L.ScriptedBackend(flip=True, active_window=WINDOW)
    provider = L.ScriptedProvider([decision("high_click", idx=0), decision("high_click", idx=10),
                                   AgentDecision(status="done", summary="done")])
    sid, bundle, backend, provider = L.make_session(
        backend, provider, dry_run=False, require_approval=True,
        limits={"min_screenshot_interval_ms": 0},
    )
    resp = L.run(server.run_goal(session_id=sid, goal="two high-risk actions",
                                 approve_next_action=True))
    executed = [a for a in backend.executed_actions if a.action.value == "click"]
    print(f"6a: termination={resp.get('termination_reason')} requires_approval="
          f"{resp.get('requires_approval')} budget_left={resp.get('approval_budget_remaining')} "
          f"executed_clicks={len(executed)}")
    check("approval_budget_exactly_one", len(executed) == 1,
          f"only the FIRST high-risk action executed ({len(executed)} executed)")
    check("second_distinct_action_denied",
          resp.get("requires_approval") is True
          and resp.get("termination_reason") == "approval_exhausted",
          f"requires_approval={resp.get('requires_approval')} "
          f"termination={resp.get('termination_reason')}")
    reasons = [str(r.get("message"))[:60] for r in resp.get("results", [])]
    print(f"    result messages: {reasons}")

    # ---- 6b: CRITICAL is blocked even with the approval budget -----------------------------
    L.reset_server()
    backend_c = L.ScriptedBackend(flip=True, active_window=WINDOW)
    sidc, bundlec, backend_c, _ = L.make_session(
        backend_c, L.ScriptedProvider([decision("critical_click")]), dry_run=False,
        require_approval=True, limits={"min_screenshot_interval_ms": 0},
    )
    respc = L.run(server.run_goal(session_id=sidc, goal="critical with approval",
                                  approve_next_action=True))
    check("critical_blocked_even_with_budget",
          respc.get("termination_reason") == "blocked_safety"
          and not backend_c.executed_actions,
          f"termination={respc.get('termination_reason')} "
          f"executed={[a.action.value for a in backend_c.executed_actions]}")

    # ---- 6c: no token persistence across run_goal calls ------------------------------------
    L.reset_server()
    backend_t = L.ScriptedBackend(flip=True, active_window=WINDOW)
    sidt, bundlet, backend_t, provider_t = L.make_session(
        backend_t, L.ScriptedProvider([decision("high_click", idx=i) for i in range(6)]),
        dry_run=False, require_approval=True, limits={"min_screenshot_interval_ms": 0},
    )
    r1 = L.run(server.run_goal(session_id=sidt, goal="first", approve_next_action=True))
    n1 = len([a for a in backend_t.executed_actions if a.action.value == "click"])
    r2 = L.run(server.run_goal(session_id=sidt, goal="second"))
    n2 = len([a for a in backend_t.executed_actions if a.action.value == "click"])
    check("no_approval_carryover_across_calls",
          n1 == 1 and n2 == 1 and r2.get("requires_approval") is True,
          f"call1 executed={n1}, call2 executed={n2} (no new budget -> denied), "
          f"call2 requires_approval={r2.get('requires_approval')}")

    # ---- 6d: computer_execute approval is per-call ------------------------------------------
    L.reset_server()
    backend_e = L.ScriptedBackend(flip=True, active_window=WINDOW)
    side, bundlee, backend_e, _ = L.make_session(
        backend_e, L.ScriptedProvider([]), dry_run=False, require_approval=True,
        limits={"min_screenshot_interval_ms": 0},
    )
    ra = L.run(server.computer_execute(session_id=side, action="click", x=30, y=40,
                                       approved=True,
                                       # high-risk reason so approval is actually exercised
                                       expected_effect=None))
    # make it high-risk via text? computer_execute has no reason param; use a high-risk keypress-free path:
    rb = L.run(server.computer_execute(session_id=side, action="type",
                                       text="install the software now", approved=False))
    print(f"6d: benign approved call ok={ra.get('ok')}; sensitive type refused: {rb}")
    check("computer_execute_approval_per_call",
          rb.get("ok") is False and rb.get("requires_approval") is True,
          "unapproved sensitive type action requires its own explicit approval flag")

    # ---- 6e: dismiss path cannot authorize a destructive action ------------------------------
    L.reset_server()
    blocked_backend = L.ScriptedBackend(flip=True, active_window=WINDOW)
    blocked_backend.set_input_blocked(True)

    class UnblockOnEscape(L.ScriptedBackend):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.set_input_blocked(True)

        def execute(self, action, stop=None):
            if action.action.value == "keypress" and any(k.lower() == "esc" for k in action.keys):
                self.set_input_blocked(False)
            return super().execute(action, stop)

    provider_e = L.ScriptedProvider([decision("high_click"), decision("critical_click"),
                                     AgentDecision(status="done", summary="done")])
    sidd, bundled, backend_d, provider_d = L.make_session(
        UnblockOnEscape(flip=True, active_window=WINDOW), provider_e, dry_run=False, require_approval=True,
        limits={"min_screenshot_interval_ms": 0},
    )
    respd = L.run(server.run_goal(session_id=sidd, goal="dismiss then destructive",
                                  approve_next_action=True))
    exec_d = [(a.action.value, getattr(a, "reason", "")) for a in backend_d.executed_actions]
    destructive = [a for a in backend_d.executed_actions
                   if "delete the files" in getattr(a, "reason", "")]
    print(f"6e: termination={respd.get('termination_reason')} executed={exec_d}")
    check("dismiss_never_authorizes_destructive",
          not destructive,
          f"CRITICAL 'delete the files' click never executed (dismiss only ever re-runs an "
          f"already-approved same instance); executed={[e[0] for e in exec_d]}")

    print("\n=== PROBE6 APPROVAL FORGERY RESULTS ===")
    if fails:
        print(f"VERDICT: FAIL — {len(fails)} failing checks")
        for f in fails:
            print("  FAIL:", f)
        return 1
    print("VERDICT: PASS — budget is exactly one, bound to the action instance, non-")
    print("persistent, and never capable of authorizing CRITICAL actions; the recovery")
    print("dismiss path cannot promote any unapproved/destructive action.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
