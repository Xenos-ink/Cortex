"""T8 B11: guard capture-overhead micro-benchmark (zero additional captures in steady state).

The re-measurement attributed an h3/h6 wall-time regression to guard annotations adding
EXTRA screen captures per action (147-action h6 multiplied it) — the dominant term was
actually B10's false FOCUS_DRIFTED aborts, each triggering a recovery replan with fresh
captures plus a full driver round-trip. This benchmark pins BOTH:

- steady state: an armed guard performing N actions must produce EXACTLY the same
  number of backend captures as a guard-free run (the guard reads the EXISTING
  post-action/loop captures and cheap Win32 probes — it never captures);
- B10 before/after: with the PRE-B10 anchoring semantics simulated (focus-root
  equality only, dialog check on the focused CONTROL class), the same multi-surface
  typing flow false-aborts and the captures-per-action balloons; with the B10
  semantics it stays at the baseline. The measured numbers are printed and recorded in
  change-log-t8.md.
"""

from __future__ import annotations

import asyncio
import base64
import io
from types import SimpleNamespace
from typing import Any

from PIL import Image

from computer_use_mcp.agent import ComputerUseAgent
from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.interference import parse_interference
from computer_use_mcp.limits import Limits
from computer_use_mcp.models import GroundedAction, WindowInfo
from computer_use_mcp.observation import ObservationEngine
from computer_use_mcp.safety import SafetyPolicy
from computer_use_mcp.state import StopToken, TaskState

SHELL = WindowInfo(hwnd=10, pid=6964, process_name="explorer.exe", window_class="Progman", title="Program Manager")
RUN = WindowInfo(hwnd=11, pid=6964, process_name="explorer.exe", window_class="#32770", title="Run")
NOTEPAD_WIN = WindowInfo(hwnd=12, pid=500, process_name="notepad.exe", window_class="Notepad", title="Untitled - Notepad")

FLOWS = [
    GroundedAction(action="hotkey", keys=["win", "r"], confidence=1.0),
    GroundedAction(action="type", text="notepad", confidence=1.0),
    GroundedAction(action="keypress", keys=["enter"], confidence=1.0),
    GroundedAction(action="type", text="hello from the launched app", confidence=1.0),
]


class CountingFlowBackend(FakeComputerBackend):
    """Multi-surface fake that COUNTS captures and transitions scenes mid-action."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.scene: WindowInfo | None = None
        self.focus_root: WindowInfo | None = None
        self.transitions: dict[int, tuple[WindowInfo, WindowInfo]] = {}
        self.capture_count = 0

    def set_scene(self, window: WindowInfo, focus_root: WindowInfo | None = None) -> None:
        self.scene = window
        self.set_active_window(window)
        self.focus_root = focus_root or window

    def observe(self, monitor_index: Any = None) -> Any:
        self.capture_count += 1
        observation = super().observe(monitor_index)
        observation.active_window = (self.scene.title or None) if self.scene else None
        observation.active_window_info = self.scene.model_copy() if self.scene else None
        # Real screens change on keyboard input: alternate colors per executed action.
        color = "white" if len(self.executed) % 2 == 0 else "black"
        image = Image.new("RGB", (self.width, self.height), color)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        observation.image_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
        return observation

    def execute(self, action: GroundedAction, stop: Any = None, **kwargs: Any) -> str:
        message = super().execute(action, stop)
        transition = self.transitions.get(len(self.executed))
        if transition is not None:
            self.set_scene(transition[0], transition[1])
        return message

    def query_focus_target(self) -> dict[str, object] | None:
        if self.focus_root is None:
            return None
        root = self.focus_root
        return {
            "hwnd_focus": (root.hwnd or 0) + 5,
            "root_hwnd": root.hwnd,
            "window_class": "Edit",
            "root_window_class": root.window_class,
            "text": "",
            "pid": root.pid,
            "process_name": root.process_name,
        }


def _run_flow(backend: CountingFlowBackend, processes: list[str]) -> list[str]:
    agent = ComputerUseAgent(
        backend, provider=None, safety=SafetyPolicy(), task=TaskState(), stop=StopToken(),
        allowed_processes=processes,
        limits=Limits(max_actions=12, max_task_seconds=120.0).validate(),
    )
    agent.observation = ObservationEngine(backend)
    state = SimpleNamespace(
        dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
        max_steps=10, step_count=0, require_approval=False, max_retries_per_action=1,
    )
    kinds: list[str] = []
    for action in FLOWS:
        outcome = asyncio.run(agent.run_single(state, action))
        kinds.append(outcome.kind)
    return kinds


def test_steady_state_guard_adds_zero_captures() -> None:
    """Steady state: armed guard == guard-free capture count, per action."""
    backend_guard_free = CountingFlowBackend()
    backend_guard_free.set_windows([SHELL, RUN, NOTEPAD_WIN])
    backend_guard_free.set_scene(NOTEPAD_WIN)
    backend_guard_free.transitions = {3: (NOTEPAD_WIN, NOTEPAD_WIN)}

    backend_armed = CountingFlowBackend()
    backend_armed.set_windows([SHELL, RUN, NOTEPAD_WIN])
    backend_armed.set_scene(NOTEPAD_WIN)
    backend_armed.transitions = {3: (NOTEPAD_WIN, NOTEPAD_WIN)}

    # Guard-free baseline: build an agent with every mechanism disabled.
    baseline = CountingFlowBackend()
    baseline.set_windows([SHELL, RUN, NOTEPAD_WIN])
    baseline.set_scene(NOTEPAD_WIN)
    baseline.transitions = {3: (NOTEPAD_WIN, NOTEPAD_WIN)}
    agent_off = ComputerUseAgent(
        baseline, provider=None, safety=SafetyPolicy(), task=TaskState(), stop=StopToken(),
        limits=Limits(max_actions=12, max_task_seconds=120.0).validate(),
        interference=parse_interference(
            {
                "focus_guard": {"enabled": False},
                "dialog_sentinel": {"enabled": False},
                "focus_continuity": {"enabled": False},
                "hotkey_guard": {"enabled": False},
            }
        ),
    )
    agent_off.observation = ObservationEngine(baseline)
    state = SimpleNamespace(
        dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
        max_steps=10, step_count=0, require_approval=False, max_retries_per_action=1,
    )
    for action in FLOWS:
        asyncio.run(agent_off.run_single(state, action))
    baseline_per_action = baseline.capture_count / len(FLOWS)

    # Guard ARMED with protective defaults: same flow, same capture count.
    kinds = _run_flow(backend_armed, ["explorer.exe", "notepad.exe"])
    armed_per_action = backend_armed.capture_count / len(FLOWS)
    _ = backend_guard_free  # the disabled-policy run above IS the baseline
    assert kinds == ["executed"] * 4
    assert armed_per_action == baseline_per_action, (
        f"guard added captures: baseline {baseline_per_action:.2f}/action, "
        f"armed {armed_per_action:.2f}/action"
    )


def test_b10_false_drift_storm_capture_cost_before_and_after(
    monkeypatch: Any, capsys: Any
) -> None:
    """Before (pre-B10 semantics): the multi-surface flow false-aborts and the
    captures-per-action balloons with recovery replans. After (B10): baseline."""
    # --- BEFORE: simulate the pre-B10 anchoring semantics -------------------------------
    backend_before = CountingFlowBackend()
    backend_before.set_windows([SHELL, RUN, NOTEPAD_WIN])
    backend_before.set_scene(SHELL)
    backend_before.transitions = {1: (RUN, RUN), 3: (NOTEPAD_WIN, NOTEPAD_WIN)}
    agent_before = ComputerUseAgent(
        backend_before, provider=None, safety=SafetyPolicy(), task=TaskState(), stop=StopToken(),
        allowed_processes=["explorer.exe", "notepad.exe"],
        limits=Limits(max_actions=12, max_task_seconds=120.0).validate(),
    )
    agent_before.observation = ObservationEngine(backend_before)
    agent_before.guard.rebind(SHELL)  # the untitled-shell anchor from the incident

    def _pre_b10_semantics(target: dict[str, object]) -> bool:
        # Pre-B10: the check keyed on focus-ROOT equality with the anchor; the dialog
        # check keyed on the focused CONTROL class ("Edit") — a focus inside a dialog
        # never matched, so dialog typing false-drifted.
        bound = agent_before.guard.bound
        root = target.get("root_hwnd") if bound is not None else None
        return bool(
            bound is not None
            and root is not None
            and bound.hwnd is not None
            and int(root) == int(bound.hwnd)
        )

    monkeypatch.setattr(agent_before.guard, "_focus_surface_is_ours", _pre_b10_semantics)
    # Pre-B10 also lacked the verified re-anchor (B10 (b)) — disable it so the
    # simulation reproduces the incident: the anchor stays on the shell while the
    # focus moves into the Run dialog.
    monkeypatch.setattr(agent_before, "_verified_reanchor", lambda *a, **k: None)
    state = SimpleNamespace(
        dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
        max_steps=10, step_count=0, require_approval=False, max_retries_per_action=1,
    )
    kinds_before: list[str] = []
    for action in FLOWS:
        outcome = asyncio.run(agent_before.run_single(state, action))
        kinds_before.append(outcome.kind)
    per_action_before = backend_before.capture_count / len(FLOWS)

    # --- AFTER: the fixed B10 semantics --------------------------------------------------
    backend_after = CountingFlowBackend()
    backend_after.set_windows([SHELL, RUN, NOTEPAD_WIN])
    backend_after.set_scene(SHELL)
    backend_after.transitions = {1: (RUN, RUN), 3: (NOTEPAD_WIN, NOTEPAD_WIN)}
    agent_after = ComputerUseAgent(
        backend_after, provider=None, safety=SafetyPolicy(), task=TaskState(), stop=StopToken(),
        allowed_processes=["explorer.exe", "notepad.exe"],
        limits=Limits(max_actions=12, max_task_seconds=120.0).validate(),
    )
    agent_after.observation = ObservationEngine(backend_after)
    agent_after.guard.rebind(SHELL)
    kinds_after: list[str] = []
    for action in FLOWS:
        outcome = asyncio.run(agent_after.run_single(state, action))
        kinds_after.append(outcome.kind)
    per_action_after = backend_after.capture_count / len(FLOWS)

    # Wall-time cost model (why captures alone understate it): each BEFORE rejection
    # still burned its validate capture AND forced a full driver round-trip + recovery
    # re-observe; h6's 147 actions multiplied that. AFTER: zero rejections.
    wasted_before = sum(1 for kind in kinds_before if kind != "executed")
    wasted_after = sum(1 for kind in kinds_after if kind != "executed")
    with capsys.disabled():  # keep the recorded numbers visible in the output
        print(
            f"\nB11 capture micro-benchmark: before(pre-B10 semantics) "
            f"{per_action_before:.2f} captures/action, {wasted_before}/4 actions "
            f"wasted on false rejections; after(B10) {per_action_after:.2f} "
            f"captures/action, {wasted_after}/4 wasted (steady-state baseline: "
            f"guard adds ZERO captures per action)"
        )
    assert wasted_before == 3, "the pre-B10 simulation must reproduce the false aborts"
    assert wasted_after == 0, "the B10 flow must be rejection-free"
