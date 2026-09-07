"""E9 Re-validation probe: F2 (response redaction), F3 (honest provider-done), F7 (internal-stop hygiene) at HEAD e27026b."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

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
    # ---- F2: secret-shaped decision text must not appear raw in tool responses ----------
    def handler(request: httpx.Request) -> httpx.Response:
        decision = {
            "status": "action",
            "action": {"action": "type", "text": "logged in with password=hunter2",
                       "keys": [], "delta": 0, "reason": "enter credentials", "confidence": 0.9},
            "summary": "typing", "confidence": 0.9, "expected_effect": None,
            "suspicious_content": False, "verification_hint": None,
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(decision)}}]})

    L.reset_server()
    provider = L.ScriptedProvider([])
    from computer_use_mcp.provider import OpenAICompatibleVisionProvider
    provider = OpenAICompatibleVisionProvider(base_url="http://mock.local/v1", api_key="sk-T",
                                              transport=httpx.MockTransport(handler))
    server._backend_factory = lambda: L.ScriptedBackend(flip=True, active_window=None)
    server._provider_factory = lambda: provider
    resp = server.start_session(dry_run=False, require_approval=True,
                                limits={"min_screenshot_interval_ms": 0})
    sid = str(resp["session_id"])
    run = L.run(server.run_goal(session_id=sid, goal="use password=hunter2", approve_next_action=True))
    s = json.dumps(run, default=str)
    hits = []
    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items(): walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o): walk(v, f"{path}[{i}]")
        elif isinstance(o, str) and "hunter2" in o:
            hits.append((path, o[:80]))
    walk(run, "response")
    check("F2_response_no_raw_secret", not hits,
          f"raw secret absent from tool response (hits={hits}) "
          f"markers={s.count('[REDACTED:')}")
    check("F2_response_redaction_marker_present", "[REDACTED:password_assignment]" in s,
          f"{s.count('[REDACTED:')} redaction markers applied on the response path")

    # ---- F3: provider-done honest marking ------------------------------------------------
    L.reset_server()
    backend = L.ScriptedBackend(flip=True)
    provider3 = L.ScriptedProvider([AgentDecision(status="done", summary="we are finished")])
    sid3, bundle3, backend3, provider3 = L.make_session(
        backend, provider3, dry_run=False, require_approval=False,
        limits={"min_screenshot_interval_ms": 0},
    )
    run3 = L.run(server.run_goal(session_id=sid3, goal="done immediately"))
    items = run3.get("results", [])
    note = str((items[0].get("verification") or {}).get("note", "")) if items else ""
    check("F3_result_marks_model_declared", "model_declared" in note and "completion_evidence" in note,
          f"note={note[:130]!r}")
    audit_text = bundle3.auditor.path_for(sid3).read_text(encoding="utf-8")
    check("F3_audit_marks_model_declared",
          '"completion_evidence": "model_declared"' in audit_text
          and '"result": "model_declared"' in audit_text,
          "audit verification event records completion_evidence=model_declared")
    check("F3_termination_still_completed", run3.get("termination_reason") == "completed",
          f"termination={run3.get('termination_reason')} (CUA loop semantics preserved)")

    # ---- F7: internal-stop flavor -> all four tools refuse, bundle/registry clean --------
    L.reset_server()
    backend4 = L.ScriptedBackend(flip=True)

    class InternalStopBackend(L.ScriptedBackend):
        def execute(self, action, stop=None):
            if action.action.value == "click":
                stop.stop()  # kill path armed by an internal safety path, NOT stop_session
            return super().execute(action, stop)

    provider4 = L.ScriptedProvider([AgentDecision(status="action", action=GroundedAction(
        action="click", point={"x": 10, "y": 10}, confidence=1.0), summary="c")] * 3)
    sid4, bundle4, backend4, provider4 = L.make_session(
        InternalStopBackend(flip=True), provider4, dry_run=False, require_approval=False,
        limits={"min_screenshot_interval_ms": 0},
    )
    run4 = L.run(server.run_goal(session_id=sid4, goal="internal stop"))
    check("F7_run_goal_reports_stopped",
          run4.get("termination_reason") == "stopped_by_user",
          f"termination={run4.get('termination_reason')}")
    # Any subsequent tool call must now refuse with session_stopped and clean the bundle.
    r_obs = server.computer_observe(session_id=sid4)
    check("F7_observe_refuses", r_obs.get("error") == "session_stopped",
          f"error={r_obs.get('error')}")
    check("F7_bundle_removed_and_registry_clean",
          sid4 not in server._bundles and server._registry.get(sid4) is None
          and sid4 in server._stopped_sessions,
          f"bundles={sid4 in server._bundles} registry={server._registry.get(sid4) is not None} "
          f"stopped_memory={sid4 in server._stopped_sessions}")
    r_exec = L.run(server.computer_execute(session_id=sid4, action="click", x=1, y=1))
    check("F7_execute_refuses", r_exec.get("error") == "session_stopped",
          f"error={r_exec.get('error')}")
    r_goal = L.run(server.run_goal(session_id=sid4, goal="after internal stop"))
    check("F7_run_goal_refuses", r_goal.get("ok") is False and r_goal.get("stopped") is True,
          f"ok={r_goal.get('ok')} stopped={r_goal.get('stopped')}")
    r_stop = server.stop_session(session_id=sid4)
    check("F7_stop_session_idempotent", r_stop.get("ok") is True,
          f"re-stop response ok={r_stop.get('ok')} message={r_stop.get('message')!r}")
    audit_text4 = bundle4.auditor.path_for(sid4).read_text(encoding="utf-8")
    check("F7_internal_stop_audited",
          '"event_type": "emergency_stop"' in audit_text4.replace(" ", "")
          or '"emergency_stop"' in audit_text4,
          # In-run internal stop is audited as emergency_stop; run_goal then closes the
          # bundle (source=run_goal_kill_path, no duplicate event). The lazy-detection
          # marker (source=internal_kill_path) is exercised below.
          "in-run kill path audited as emergency_stop; bundle closed by run_goal")
    # Lazy-detection path: arm a token OUTSIDE any run (internal safety path), then a
    # tool call must detect it, refuse, audit source=internal_kill_path, and clean up.
    L.reset_server()
    backend5 = L.ScriptedBackend(flip=True)
    sid5, bundle5, backend5, provider5 = L.make_session(
        backend5, L.ScriptedProvider([]), dry_run=False, require_approval=False,
        limits={"min_screenshot_interval_ms": 0},
    )
    bundle5.context.stop.stop()  # internal safety path arms the token; no run is active
    r_obs5 = server.computer_observe(session_id=sid5)
    audit5 = bundle5.auditor.path_for(sid5).read_text(encoding="utf-8")
    check("F7_lazy_detection_refuses_and_audits",
          r_obs5.get("error") == "session_stopped" and "internal_kill_path" in audit5
          and sid5 not in server._bundles and server._registry.get(sid5) is None,
          f"error={r_obs5.get('error')} marker={'internal_kill_path' in audit5} "
          f"bundle_removed={sid5 not in server._bundles}")

    print("\n=== PROBE8 RE-VALIDATION (F2/F3/F7) ===")
    if fails:
        print(f"VERDICT: FAIL — {len(fails)} failing checks")
        for f in fails:
            print("  FAIL:", f)
        return 1
    print("VERDICT: PASS — F2 response-path redaction, F3 honest provider-done marking,")
    print("F7 unified internal/external stop hygiene all verified at HEAD.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
