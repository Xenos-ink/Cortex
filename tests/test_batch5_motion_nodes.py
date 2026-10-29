"""Batch-5 motion-node regressions (WP2 port of the input-engine/motion nodes of
the dev tree's test_batch5_user_decisions.py).

Only the nodes that need NO plans, waits, leases, or evidence machinery are ported:
the motion env envelopes, the screen-bounded bend clamp, and the timed-move cursor
interference evidence. The remaining dev-tree batch5 nodes (the stepwise handshake,
the session lease, the limit cutoff truth fields, MAX_PLAN_POINTS at the plan
boundary, the JSON-serializable handshake payload) are DEFERRED to WP7 — they are
plan-surface tests, and the plan surface does not exist in the stable tree yet.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from recording_engine import RecordingEngine

import computer_use_mcp.backend as backend_module
from computer_use_mcp.backend import LocalComputerBackend, _motion_path_points
from computer_use_mcp.models import GroundedAction

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")


# --- decision 4/decision 5 envelopes ---------------------------------------------------------


def _env_probe(key: str, value: str, expression: str) -> str:
    source_root = str(Path(backend_module.__file__).resolve().parents[1])
    env = {k: v for k, v in os.environ.items() if not k.startswith("CORTEX_")}
    env["PYTHONPATH"] = source_root
    env[key] = value
    completed = subprocess.run(
        [sys.executable, "-c", f"import computer_use_mcp.backend as b; print({expression})"],
        cwd=str(Path(source_root).parent),
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        # CORTEX_MOVE_DURATION_MS: band 200..500, default 0.
        ("CORTEX_MOVE_DURATION_MS", "199", "0"),
        ("CORTEX_MOVE_DURATION_MS", "200", "200"),
        ("CORTEX_MOVE_DURATION_MS", "500", "500"),
        ("CORTEX_MOVE_DURATION_MS", "501", "0"),
        # CORTEX_PLAN_MOVE_DURATION_MS: SPEC 5.2 — band 0..500, default 0 (the
        # constant ships in WP2; its consumer, the plan normalizer, is WP7).
        ("CORTEX_PLAN_MOVE_DURATION_MS", "0", "0"),
        ("CORTEX_PLAN_MOVE_DURATION_MS", "199", "199"),
        ("CORTEX_PLAN_MOVE_DURATION_MS", "200", "200"),
        ("CORTEX_PLAN_MOVE_DURATION_MS", "500", "500"),
        ("CORTEX_PLAN_MOVE_DURATION_MS", "501", "0"),
        # CORTEX_MOVE_SETTLE_MS: SPEC 5.1 — band 0..200, default 0.0, so "49" and "0"
        # are IN band and apply, and an out-of-band value falls back to 0.0 (K-12).
        ("CORTEX_MOVE_SETTLE_MS", "0", "0.0"),
        ("CORTEX_MOVE_SETTLE_MS", "49", "49.0"),
        ("CORTEX_MOVE_SETTLE_MS", "50", "50.0"),
        ("CORTEX_MOVE_SETTLE_MS", "200", "200.0"),
        ("CORTEX_MOVE_SETTLE_MS", "201", "0.0"),
        ("CORTEX_PLAN_SAMPLE_INTERVAL_MS", "1e400", "12"),
    ],
)
def test_motion_env_ranges_and_overflow_fallback(key: str, value: str, expected: str) -> None:
    """The four motion env keys' bands, each row a clean subprocess."""
    expression = {
        "CORTEX_MOVE_DURATION_MS": "b.MOVE_DURATION_MS",
        "CORTEX_PLAN_MOVE_DURATION_MS": "b.PLAN_MOVE_DURATION_MS",
        "CORTEX_MOVE_SETTLE_MS": "b.MOVE_SETTLE_MS",
        "CORTEX_PLAN_SAMPLE_INTERVAL_MS": "b.PLAN_SAMPLE_INTERVAL_MS",
    }[key]
    assert _env_probe(key, value, expression) == expected


@pytest.mark.parametrize("shape", ["linear", "quadratic_bezier"])
@pytest.mark.parametrize("bend", [10**12, -(10**12)])
def test_bend_is_silently_clamped_to_world_bounds(shape: str, bend: int) -> None:
    points = _motion_path_points(
        (100, 100), (900, 700), 240, shape=shape, bend_px=bend, bounds=(0, 0, 1000, 800)
    )
    assert all(0 <= x < 1000 and 0 <= y < 800 for x, y in points)
    assert points[-1] == (900, 700)
    assert all(isinstance(x, int) and isinstance(y, int) for x, y in points)
    assert all(math.isfinite(x) and math.isfinite(y) for x, y in points)


# --- decision 2: timed cursor interference ---------------------------------------------------


@WINDOWS_ONLY
def test_timed_move_repositions_immediately_and_records_evidence(
    real_backend: LocalComputerBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    probes = iter([(10, 10), (900, 900), (14, 17), (18, 24), (22, 31), (30, 45)])
    monkeypatch.setattr(real_backend, "probe_cursor_position", lambda: next(probes, None))
    monkeypatch.setattr(backend_module, "get_cursor_position", lambda: (10, 10))
    monkeypatch.setattr(backend_module.time, "sleep", lambda _seconds: None)
    action = GroundedAction(action="move", point={"x": 30, "y": 45}, duration_ms=60, confidence=1.0)
    real_backend.execute(action)
    assert action._repositions
    assert action._repositions[0]["expected"] == [14, 17]
    assert action._repositions[0]["observed"] == [900, 900]
    assert engine.calls[-1] == ("move", 30, 45)
