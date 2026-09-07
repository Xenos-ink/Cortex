import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx, probe_lib as L
from computer_use_mcp import server
from computer_use_mcp.provider import OpenAICompatibleVisionProvider

def handler(request):
    decision = {"status": "action", "action": {"action": "type", "text": "logged in with password=hunter2",
        "keys": [], "delta": 0, "reason": "enter credentials", "confidence": 0.9},
        "summary": "typing", "confidence": 0.9, "expected_effect": None,
        "suspicious_content": False, "verification_hint": None}
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(decision)}}]})

L.reset_server()
provider = OpenAICompatibleVisionProvider(base_url="http://mock.local/v1", api_key="sk-T",
    transport=httpx.MockTransport(handler))
server._backend_factory = lambda: L.ScriptedBackend(flip=True)
server._provider_factory = lambda: provider
resp = server.start_session(dry_run=False, require_approval=True, limits={"min_screenshot_interval_ms": 0})
sid = str(resp["session_id"])
run = L.run(server.run_goal(session_id=sid, goal="use password=hunter2", approve_next_action=True))
s = json.dumps(run, default=str)
print("raw in response:", "hunter2" in s)
def walk(o, path):
    if isinstance(o, dict):
        for k, v in o.items(): walk(v, f"{path}.{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o): walk(v, f"{path}[{i}]")
    elif isinstance(o, str) and "hunter2" in o:
        print("HIT:", path, "=", o[:120])
walk(run, "response")
