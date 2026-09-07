"""E9 Probe 7a: coordinate integrity in the SCALED (125%) space — is the scale applied twice?

Trace under test (values chosen so any double application is obvious):
- screenshot 1280x720, monitor bounds (0,0,1600,900), dpi_scale 1.25  -> SCALED, scale 1.25
- model proposes click at SCREENSHOT (100, 200)
- correct physical target: (100*1.25, 200*1.25) = (125, 250)
- suspected double-scaling: grounding normalizes point -> (125, 250), then backend
  _map_to_physical multiplies by 1.25 AGAIN -> (156, 312/313)  [scale^2 = 1.5625]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_lib as L  # noqa: E402

from computer_use_mcp.models import AgentDecision, GroundedAction, MonitorInfo  # noqa: E402


def main() -> int:
    L.reset_server()
    monitor = MonitorInfo(
        id="m0", index=0, bounds=(0, 0, 1600, 900), is_primary=True,
        dpi_scale_x=1.25, dpi_scale_y=1.25,
    )
    backend = L.ScriptedBackend(width=1280, height=720, monitors=[monitor])
    provider = L.ScriptedProvider(
        [
            AgentDecision(
                status="action",
                action=GroundedAction(
                    action="click", point={"x": 100, "y": 200}, confidence=1.0,
                    expected_effect="click lands",
                ),
                summary="click the target",
            ),
            AgentDecision(status="done", summary="done"),
        ]
    )
    sid, bundle, backend, provider = L.make_session(
        backend, provider, dry_run=False, require_approval=False,
        limits={"min_screenshot_interval_ms": 0},
    )
    response = L.run(server_run_goal(sid))
    print("=== run_goal termination:", response.get("termination_reason"), "ok:", response.get("ok"))
    for item in response.get("results", []):
        print(
            "  result ok=%s action=%s point=%s verification=%s/%s" % (
                item.get("ok"),
                (item.get("action") or {}).get("action"),
                (item.get("action") or {}).get("point"),
                ((item.get("verification") or {}).get("outcome")),
                ((item.get("verification") or {}).get("verification_method")),
            )
        )
    executed = getattr(backend, "executed_actions", []) or backend.executed
    for action in executed:
        print("  EXECUTED:", action.action.value, "point:", None if action.point is None else (action.point.x, action.point.y))
    print("  backend._cursor_physical:", backend._cursor_physical)
    expected = (125, 250)
    actual = backend._cursor_physical
    verdict = "PASS (single transform)" if actual == expected else (
        "FAIL (double-transform) expected %s got %s" % (expected, actual)
    )
    print("PROBE7a VERDICT:", verdict)
    return 0 if actual == expected else 1


def server_run_goal(sid):  # avoid name shadow confusion
    from computer_use_mcp import server as s

    return s.run_goal(session_id=sid, goal="click the target at (100,200) in the screenshot")


if __name__ == "__main__":
    raise SystemExit(main())
