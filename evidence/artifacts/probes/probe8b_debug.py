import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_lib as L
from computer_use_mcp import server
from computer_use_mcp.models import AgentDecision, GroundedAction

L.reset_server()
class InternalStopBackend(L.ScriptedBackend):
    def execute(self, action, stop=None):
        if action.action.value == "click":
            stop.stop()
        return super().execute(action, stop)

backend = InternalStopBackend(flip=True)
provider = L.ScriptedProvider([AgentDecision(status="action", action=GroundedAction(
    action="click", point={"x": 10, "y": 10}, confidence=1.0), summary="c")] * 3)
sid, bundle, backend, provider = L.make_session(backend, provider, dry_run=False,
    require_approval=False, limits={"min_screenshot_interval_ms": 0})
run = L.run(server.run_goal(session_id=sid, goal="internal stop"))
print("bundle still registered after run_goal:", sid in server._bundles)
print("token armed:", bundle.context.stop.stopped)
r = server.computer_observe(session_id=sid)
print("observe error:", r.get("error"))
path = bundle.auditor.path_for(sid)
print("audit file:", path, "exists:", path.exists())
for line in path.read_text(encoding="utf-8").splitlines():
    e = json.loads(line)
    print("  event:", e["event_type"], "| result:", e.get("result"), "| meta:", e.get("metadata"))
