"""E9 Probe 2b: tool behavior on stopped sessions (both stop flavors)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_lib as L
from computer_use_mcp import server
from computer_use_mcp.models import AgentDecision, GroundedAction

L.reset_server()
backend = L.ScriptedBackend(flip=True)
provider = L.ScriptedProvider([AgentDecision(status="action", action=GroundedAction(
    action="click", point={"x": 10, "y": 10}, confidence=1.0), summary="c"),
    AgentDecision(status="done", summary="done")])
sid, bundle, backend, provider = L.make_session(backend, provider, dry_run=False,
    require_approval=False, limits={"min_screenshot_interval_ms": 0})
stop_result = server.stop_session(session_id=sid)
print("stop_session:", stop_result)
r1 = L.run(server.run_goal(session_id=sid, goal="after stop_session"))
print("run_goal on D1-stopped:", {k: r1.get(k) for k in ("ok", "stopped", "error", "termination_reason")})
r2 = L.run(server.computer_execute(session_id=sid, action="click", x=1, y=1))
print("computer_execute on D1-stopped:", r2)
r3 = server.computer_observe(session_id=sid)
print("computer_observe on D1-stopped:", {"ok": r3.get("ok"), "error": r3.get("error")})
assert r1.get("stopped") is True and r1.get("ok") is False
assert r2.get("error") == "session_stopped" and r2.get("ok") is False
assert r3.get("error") == "session_stopped"
print("PROBE2b VERDICT: PASS — stopped session refuses run_goal/computer_execute/computer_observe (D1).")
