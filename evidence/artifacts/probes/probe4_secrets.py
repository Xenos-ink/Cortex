"""E9 Probe 4: secret leak scan.

Runs the REAL OpenAICompatibleVisionProvider against an httpx.MockTransport that captures
every provider request body, inside a full server session whose goal/history/actions carry
secrets. Then scans: captured provider request bodies, ALL audit JSONL files, all tool
responses, exception messages. Raw secrets must appear nowhere; redaction markers must be
present where the secret traveled.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import probe_lib as L  # noqa: E402

from computer_use_mcp import server  # noqa: E402
from computer_use_mcp.models import AgentDecision, GroundedAction  # noqa: E402
from computer_use_mcp.provider import OpenAICompatibleVisionProvider  # noqa: E402

SECRET_PASSWORD = "hunter2"
SECRET_JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
SECRET_AWS = "AKIAIOSFODNN7EXAMPLE"
GOAL = (
    f"Open the app and use password={SECRET_PASSWORD} and token {SECRET_JWT} "
    f"then continue with {SECRET_AWS} to finish the login"
)
fails: list[str] = []


def check(name: str, condition: bool, detail: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}: {detail}")
    if not condition:
        fails.append(f"{name}: {detail}")


def main() -> int:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.append(body)
        decision = {
            "status": "action",
            "action": {"action": "type", "text": "logged in with password=hunter2",
                       "keys": [], "delta": 0, "reason": "enter credentials",
                       "confidence": 0.9},
            "summary": "typing the password=hunter2 the user gave me",
            "confidence": 0.9,
            "expected_effect": "token eyJhbGciOi appears in the field",
            "suspicious_content": False,
            "verification_hint": None,
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(decision)}}]})

    L.reset_server()
    backend = L.ScriptedBackend(flip=True)
    provider = OpenAICompatibleVisionProvider(
        base_url="http://mock.local/v1", api_key="sk-TEST-KEY-123",
        transport=httpx.MockTransport(handler),
    )
    server._backend_factory = lambda: backend
    server._provider_factory = lambda: provider
    response = server.start_session(dry_run=False, require_approval=True,
                                    limits={"min_screenshot_interval_ms": 0})
    sid = str(response["session_id"])
    bundle = server._get_bundle(sid)
    run = L.run(server.run_goal(session_id=sid, goal=GOAL, approve_next_action=True))

    # --- provider request bodies -----------------------------------------------------------
    print(f"captured {len(captured)} provider request body/bodies")
    serialized_bodies = json.dumps(captured)
    check("provider_body_no_password", SECRET_PASSWORD not in serialized_bodies,
          f"'{SECRET_PASSWORD}' absent from provider request bodies "
          f"(redaction markers present: {serialized_bodies.count('[REDACTED:')} markers)")
    check("provider_body_no_jwt", SECRET_JWT not in serialized_bodies, "raw JWT absent")
    check("provider_body_no_aws", SECRET_AWS not in serialized_bodies, "raw AWS key absent")
    check("provider_body_no_api_key", "sk-TEST-KEY-123" not in serialized_bodies,
          "API key never appears in any request body or error")
    check("provider_body_redaction_markers", "[REDACTED:password_assignment]" in serialized_bodies
          and "[REDACTED:jwt]" in serialized_bodies and "[REDACTED:aws_access_key]" in serialized_bodies,
          "expected redaction markers present in the payload")

    # --- tool responses -----------------------------------------------------------------
    run_json = json.dumps(run, default=str)
    check("run_goal_response_no_raw_secrets",
          SECRET_PASSWORD not in run_json and SECRET_JWT not in run_json and SECRET_AWS not in run_json,
          "tool response payload carries no raw secrets")

    # --- audit JSONL files ---------------------------------------------------------------
    audit_root = Path(__import__("os").environ["COMPUTER_USE_MCP_LOG_DIR"])
    all_audit = ""
    files = sorted(audit_root.rglob("*.jsonl"))
    for f in files:
        all_audit += f.read_text(encoding="utf-8")
    print(f"scanned {len(files)} audit JSONL file(s), {len(all_audit)} chars")
    check("audit_no_password", SECRET_PASSWORD not in all_audit, "raw password absent from audit")
    check("audit_no_jwt", SECRET_JWT not in all_audit, "raw JWT absent from audit")
    check("audit_no_aws", SECRET_AWS not in all_audit, "raw AWS key absent from audit")
    markers = all_audit.count("[REDACTED:")
    check("audit_redaction_markers_present", markers > 0,
          f"{markers} redaction markers applied at the sink")
    check("sensitive_typed_text_blocked",
          run.get("termination_reason") == "blocked_safety",
          f"TYPE text resembling a secret/credential is blocked by policy "
          f"(termination={run.get('termination_reason')})")

    # --- exception messages (force a provider HTTP error) ---------------------------------
    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    provider2 = OpenAICompatibleVisionProvider(
        base_url="http://mock.local/v1", api_key="sk-TEST-KEY-123",
        transport=httpx.MockTransport(failing_handler), retry_backoff=(0.0, 0.0),
    )
    L.reset_server()
    server._backend_factory = lambda: L.ScriptedBackend(flip=True)
    server._provider_factory = lambda: provider2
    resp = server.start_session(dry_run=False, require_approval=False,
                                limits={"min_screenshot_interval_ms": 0})
    sid2 = str(resp["session_id"])
    run2 = L.run(server.run_goal(session_id=sid2, goal=GOAL))
    errs = json.dumps(run2, default=str)
    check("provider_error_no_api_key", "sk-TEST-KEY-123" not in errs,
          "API key absent from error payloads")
    check("provider_error_no_secrets",
          SECRET_PASSWORD not in errs and SECRET_JWT not in errs,
          "secrets absent from provider-failure error payload")

    print("\n=== PROBE4 SECRET LEAK RESULTS ===")
    if fails:
        print(f"VERDICT: FAIL — {len(fails)} failing checks")
        for f in fails:
            print("  FAIL:", f)
        return 1
    print("VERDICT: PASS — no raw secret in provider payloads, tool responses, error")
    print("messages, or audit JSONL; redaction markers present; secret-like TYPE blocked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
