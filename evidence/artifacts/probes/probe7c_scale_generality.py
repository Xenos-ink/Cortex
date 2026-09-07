"""E9 Probe 7c: F1 double-transform generality (150% + negative-origin secondary)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_lib as L
from computer_use_mcp import server
from computer_use_mcp.models import AgentDecision, GroundedAction, MonitorInfo

for name, bounds, shot, dpi in [
    ("150% primary", (0, 0, 1920, 1080), (1280, 720), 1.5),
    ("125% secondary negative-origin", (-2400, 0, 2400, 1350), (1920, 1080), 1.25),
]:
    L.reset_server()
    mon = MonitorInfo(id="m0", index=0, bounds=bounds, is_primary=True,
                      dpi_scale_x=dpi, dpi_scale_y=dpi)
    backend = L.ScriptedBackend(width=shot[0], height=shot[1], monitors=[mon])
    # model clicks the CENTER of the screenshot -> expected physical = origin + center*scale
    cx, cy = shot[0] // 2, shot[1] // 2
    expected = (bounds[0] + round(cx * dpi), bounds[1] + round(cy * dpi))
    provider = L.ScriptedProvider([
        AgentDecision(status="action", action=GroundedAction(
            action="click", point={"x": cx, "y": cy}, confidence=1.0, expected_effect="x")),
        AgentDecision(status="done", summary="done")])
    sid, bundle, backend, provider = L.make_session(backend, provider, dry_run=False,
        require_approval=False, limits={"min_screenshot_interval_ms": 0})
    L.run(server.run_goal(session_id=sid, goal="center click"))
    actual = backend._cursor_physical
    double = (bounds[0] + round(cx * dpi * dpi), bounds[1] + round(cy * dpi * dpi))
    verdict = "PASS" if actual == expected else f"FAIL (double-transform matches {double})"
    print(f"{name}: screenshot_click=({cx},{cy}) expected_physical={expected} "
          f"actual_physical={actual} -> {verdict}")
