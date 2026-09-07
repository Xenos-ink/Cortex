"""E9 Probe 7b: executor must refuse direct computer_execute on an unverifiable fake.

Fake monitors: screenshot 1000x720 vs monitor 1920x1080 @ dpi 1.25 -> ratio 1.92 != 1.25
-> UNVERIFIABLE. computer_execute must refuse (coordinate_space_unverifiable) and the
backend must record nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_lib as L  # noqa: E402

from computer_use_mcp import server  # noqa: E402
from computer_use_mcp.models import MonitorInfo  # noqa: E402


def main() -> int:
    L.reset_server()
    monitor = MonitorInfo(id="m0", index=0, bounds=(0, 0, 1920, 1080), is_primary=True,
                          dpi_scale_x=1.25, dpi_scale_y=1.25)
    backend = L.ScriptedBackend(width=1000, height=720, monitors=[monitor])
    sid, bundle, backend, _ = L.make_session(
        backend, L.ScriptedProvider([]), dry_run=False, require_approval=False,
        limits={"min_screenshot_interval_ms": 0},
    )
    r = L.run(server.computer_execute(session_id=sid, action="click", x=100, y=100))
    print("computer_execute on unverifiable:", r)
    ok = (r.get("ok") is False and "unverifiable" in str(r.get("reasons", r.get("message", ""))).lower()
          and backend.executed == [])
    print("PROBE7b VERDICT:", "PASS" if ok else "FAIL",
          f"(executed={backend.executed})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
