"""T8 anomaly-B5 regression: typing a raw single-backslash Windows path must complete.

The incident (evidence/perf-004/p3/runs/r1-run0-type-stall): a 52-char path typed into
the Run dialog never returned (>10 min, 0 CPU) while the same tree typed backslash
paths successfully in the very next run (r1-run1, precision 1.0) — an activation-race
wedge, not a text-content defect. Root-cause findings pinned here:

- the SendInput unicode batching handles every backslash path character (VK_PACKET
  units for the whole string, including '\\', ':' and '.') — unit-level;
- REAL dispatch of the exact incident payload completes well under 1 s (live test);
- a follow_ups chain containing the path-typing type action runs to completion;
- the B8 focus-transition settle paces keyboard input sent immediately after a window
  activation (win+r -> type with no settle was the wedge topology).

Real-input tests close every window they open (taskkill on the spawned Notepad PID)
and never park the mouse in a failsafe corner.
"""

from __future__ import annotations

import subprocess
import threading
import time

import pytest

import computer_use_mcp.backend as backend_module
from computer_use_mcp.backend import (
    FakeComputerBackend,
    LocalComputerBackend,
    _text_to_key_units,
    find_window_by_title,
)
from computer_use_mcp.models import GroundedAction

#: The exact incident payload shape: a raw single-backslash Windows path after a verb.
BS = chr(92)  # immune to source-encoding/shell mangling
INCIDENT_TEXT = (
    "notepad C:" + BS + "Users" + BS + "localadmin" + BS + "Desktop" + BS
    + "cortex-bench" + BS + "r1_note.txt"
)


def test_backslash_path_text_produces_complete_unicode_units() -> None:
    """Unit pin: every character (including backslashes) becomes a down+up VK_PACKET pair."""
    units = _text_to_key_units(INCIDENT_TEXT)
    assert len(units) == 2 * len(INCIDENT_TEXT)  # down+up per code unit, nothing dropped
    backslash_events = [u for u in units if u[1] == ord(BS)]
    assert len(backslash_events) == 10  # down+up per backslash (5 backslashes in the payload)


def test_follow_ups_chain_with_backslash_path_type_runs_to_completion() -> None:
    """The driver pattern that stalled pre-fix: [type path, keypress enter] as a batch."""
    import asyncio
    from io import BytesIO
    from types import SimpleNamespace

    from PIL import Image as PILImage

    from computer_use_mcp.agent import ComputerUseAgent
    from computer_use_mcp.limits import Limits
    from computer_use_mcp.models import ActionSpec
    from computer_use_mcp.observation import ObservationEngine
    from computer_use_mcp.safety import SafetyPolicy
    from computer_use_mcp.state import StopToken, TaskState

    backend = FakeComputerBackend()

    def _observe_flipped(monitor_index: object = None) -> object:
        observation = FakeComputerBackend.observe(backend, monitor_index)  # type: ignore[arg-type]
        color = "white" if len(backend.executed) % 2 == 0 else "black"
        image = PILImage.new("RGB", (backend.width, backend.height), color)
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        import base64

        observation.image_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
        return observation

    backend.observe = _observe_flipped  # type: ignore[method-assign]
    agent = ComputerUseAgent(
        backend, provider=None, safety=SafetyPolicy(), task=TaskState(), stop=StopToken(),
        limits=Limits(max_actions=5, max_task_seconds=60.0).validate(),
    )
    agent.observation = ObservationEngine(backend)
    state = SimpleNamespace(
        dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
        max_steps=5, step_count=0, require_approval=False, max_retries_per_action=1,
    )
    outcome = asyncio.run(
        agent.run_single(
            state,
            GroundedAction(action="type", text=INCIDENT_TEXT, confidence=1.0),
            approved=True,
            follow_ups=[ActionSpec(action="keypress", keys=["enter"])],
        )
    )
    # The chain runs to completion (the incident shape can never hang or half-run).
    assert outcome.kind == "executed"
    assert len(outcome.follow_up_results or []) == 2
    assert [item.action.value for item in backend.executed] == ["type", "keypress"]


@pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="Real Win32 dispatch requires Windows.")
def test_real_dispatch_of_backslash_path_completes_under_a_second(real_backend: LocalComputerBackend) -> None:
    """REAL dispatch: the incident payload typed into Notepad completes < 1 s."""
    result: dict[str, object] = {"done": False, "error": None}

    def worker() -> None:
        proc = None
        try:
            # Always OUR OWN Notepad instance: cleanup is exact (kill by PID).
            proc = subprocess.Popen(["notepad.exe"])
            window = None
            deadline = time.time() + 8
            while time.time() < deadline and window is None:
                time.sleep(0.2)
                window = find_window_by_title("Untitled - Notepad")
            assert window is not None, "Notepad could not be launched for the B5 test"
            real_backend.focus_window_title(window.title)
            time.sleep(0.6)  # the driver-protocol settle after activation
            action = GroundedAction(action="type", text=INCIDENT_TEXT, confidence=1.0)
            started = time.perf_counter()
            real_backend.execute(action, None)
            result["elapsed"] = time.perf_counter() - started
            result["done"] = True
        except Exception as exc:  # noqa: BLE001 - surfaced by the watchdog below
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if proc is not None:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/F"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=30)
    assert result["error"] is None, f"B5 real-dispatch worker failed: {result['error']}"
    assert result["done"] is True, "B5 STALL REPRODUCED: the path type did not finish in 30 s"
    assert result["elapsed"] < 1.0, f"path typing took {result['elapsed']:.2f}s (must be <1s)"
