"""T8 FocusContinuity tests (A12 test plan T4): keyboard-focus continuity around dispatch.

Covers the mechanism-(iv) contract:

- the focused control inside the bound window -> dispatch (probe returns None);
- foreign keyboard focus -> blocking FOCUS_DRIFTED (abort policy, the default);
- focus on an owned `#32770` dialog of the bound pid -> allowed;
- the opt-in ``warn`` policy annotates and proceeds;
- LONG-TYPE mid-string drift: the per-chunk hook aborts the in-flight type (no further
  chunks dispatched), AFTER the stop-token hook (stop discipline keeps precedence);
- post-type drift is REPORTED; ``resend_terminal_key=false`` never re-sends a terminal
  key (a single chord dispatch is asserted);
- an unavailable focus probe leaves the check inert (the foreground gate still applies).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from computer_use_mcp.backend import FakeComputerBackend, FocusDriftError
from computer_use_mcp.focus_guard import InterferenceGuard
from computer_use_mcp.interference import FOCUS_DRIFTED, parse_interference
from computer_use_mcp.models import FailureClass, GroundedAction, WindowInfo
from computer_use_mcp.state import StopToken

TARGET = WindowInfo(
    hwnd=1, pid=100, process_name="EXCEL.EXE", window_class="XLMAIN", title="Book1 - Excel",
)


def _backend_with_focus(root_hwnd: int | None, **kwargs: Any) -> FakeComputerBackend:
    backend = FakeComputerBackend(**kwargs)
    backend.set_active_window(TARGET)
    backend.focus_target = (
        None
        if root_hwnd is None
        else {
            "hwnd_focus": root_hwnd + 5,
            "root_hwnd": root_hwnd,
            "window_class": "Edit",
            "text": "",
            # A DIFFERENT process from the bound window (pid 100): the pre-B10 tests
            # modeled a foreign surface, and the B10 same-process rule must not
            # accidentally wave it through.
            "pid": 31337 if root_hwnd != TARGET.hwnd else 100,
            "process_name": "somegame.exe" if root_hwnd != TARGET.hwnd else "EXCEL.EXE",
        }
    )
    return backend


def _armed(backend: FakeComputerBackend, **policy: Any) -> InterferenceGuard:
    guard = InterferenceGuard(backend, parse_interference(policy or None))
    guard.rebind(TARGET)
    return guard


TYPE_ACTION = GroundedAction(action="type", text="quarterly totals", confidence=1.0)
ENTER = GroundedAction(action="keypress", keys=["enter"], confidence=1.0)


def test_focus_inside_bound_window_dispatches() -> None:
    backend = _backend_with_focus(root_hwnd=TARGET.hwnd)
    guard = _armed(backend)
    assert guard.verify_pre_dispatch(TYPE_ACTION) is None
    assert guard.verify_pre_dispatch(ENTER) is None


def test_foreign_keyboard_focus_aborts_with_focus_drifted() -> None:
    backend = _backend_with_focus(root_hwnd=999)  # some other window's control
    guard = _armed(backend)
    verdict = guard.verify_pre_dispatch(TYPE_ACTION)
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith(FOCUS_DRIFTED)
    assert "expected=" in verdict.event and "actual=" in verdict.event
    assert verdict.failure_class is FailureClass.WRONG_WINDOW


def test_owned_dialog_focus_is_allowed() -> None:
    backend = _backend_with_focus(root_hwnd=TARGET.hwnd)
    backend.focus_target = {
        "hwnd_focus": 77, "root_hwnd": 88, "window_class": "#32770", "text": "", "pid": 100,
    }
    guard = _armed(backend)
    assert guard.verify_pre_dispatch(TYPE_ACTION) is None  # owned dialog of the bound pid


def test_warn_policy_annotates_and_proceeds() -> None:
    backend = _backend_with_focus(root_hwnd=999)
    guard = _armed(backend, focus_continuity={"on_drift": "warn"})
    verdict = guard.verify_pre_dispatch(TYPE_ACTION)
    assert verdict is not None and not verdict.blocking
    assert verdict.event.startswith(FOCUS_DRIFTED)


def test_unavailable_probe_leaves_check_inert() -> None:
    backend = _backend_with_focus(root_hwnd=None)
    guard = _armed(backend)
    assert guard.verify_pre_dispatch(TYPE_ACTION) is None


def test_dormant_guard_skips_continuity() -> None:
    backend = _backend_with_focus(root_hwnd=999)
    guard = InterferenceGuard(backend, parse_interference(None))  # never bound
    assert guard.verify_pre_dispatch(TYPE_ACTION) is None


def test_mid_type_drift_aborts_remaining_chunks_and_preserves_stop_precedence() -> None:
    """The per-chunk hook fires after the stop-token hook; drift raises FocusDriftError."""
    from tests.recording_engine import RecordingEngine

    backend = _backend_with_focus(root_hwnd=TARGET.hwnd)
    guard = _armed(backend)
    engine = RecordingEngine()
    backend._engine = engine

    chunks_dispatched: list[str] = []

    def _chunked_type_text(text: str, before_chunk: Any = None) -> None:
        for char in text:  # per-character chunking like the paced engines
            if before_chunk is not None:
                before_chunk()
            chunks_dispatched.append(char)

    engine.type_text = _chunked_type_text  # type: ignore[method-assign]

    drift_after = 4

    def _hook() -> None:
        # The controller composes the hook to run AFTER the stop-token check; here the
        # drift fires after ``drift_after`` chunks to simulate a mid-string steal.
        if len(chunks_dispatched) >= drift_after:
            backend.focus_target = {
                "hwnd_focus": 5, "root_hwnd": 999, "window_class": "Edit", "text": "", "pid": 1,
            }
        guard.verify_mid_type(TYPE_ACTION)

    # LocalComputerBackend.execute chains stop.ensure_live BEFORE the hook; use the real
    # backend so the ordering contract is exercised end-to-end.
    from computer_use_mcp.backend import LocalComputerBackend

    real = LocalComputerBackend.__new__(LocalComputerBackend)  # type: ignore[call-arg]
    real._engine = engine
    real._active_context = None
    real._last_key_dispatch = 0.0
    real._last_focus_transition = 0.0
    try:
        real.execute(TYPE_ACTION, StopToken(), focus_hook=_hook)
        raise AssertionError("expected FocusDriftError to propagate out of execute")
    except FocusDriftError as exc:
        assert str(exc).startswith(FOCUS_DRIFTED)
    # The in-flight type aborted at the drift point: no further chunks were dispatched.
    assert len(chunks_dispatched) == drift_after

    # Stop-token precedence: a fired stop raises TaskStopped before the hook ever runs.
    stopped = StopToken()
    stopped.stop()
    engine2 = RecordingEngine()
    backend._engine = engine2
    real._engine = engine2
    from computer_use_mcp.state import TaskStopped

    try:
        real.execute(TYPE_ACTION, stopped, focus_hook=_hook)
        raise AssertionError("expected TaskStopped")
    except TaskStopped:  # the stop wins over the focus hook (stop discipline precedence)
        pass
    assert engine2.calls == []


def test_controller_maps_mid_type_drift_to_rejection_without_retype() -> None:
    from computer_use_mcp.agent import ComputerUseAgent
    from computer_use_mcp.limits import Limits
    from computer_use_mcp.observation import ObservationEngine
    from computer_use_mcp.safety import SafetyPolicy
    from computer_use_mcp.state import StopToken, TaskState

    class DriftMidType(FakeComputerBackend):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.execute_calls = 0

        def execute(self, action: GroundedAction, stop: Any = None, **kwargs: Any) -> str:
            self.execute_calls += 1
            raise FocusDriftError("FOCUS_DRIFTED expected='Book1 - Excel (EXCEL.EXE)' actual='steal'")

    backend = DriftMidType()
    backend.set_active_window(TARGET)
    agent = ComputerUseAgent(
        backend, provider=None, safety=SafetyPolicy(), task=TaskState(), stop=StopToken(),
        limits=Limits(max_actions=5, max_task_seconds=60.0).validate(),
    )
    agent.observation = ObservationEngine(backend)
    agent.guard.rebind(TARGET)
    state = SimpleNamespace(
        dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
        max_steps=5, step_count=0, require_approval=False, max_retries_per_action=1,
    )
    outcome = asyncio.run(agent.run_single(state, TYPE_ACTION))
    assert outcome.kind == "rejected"
    assert any(r.startswith(FOCUS_DRIFTED) for r in outcome.reasons)
    assert backend.execute_calls == 1  # aborted, not retried (a retype would duplicate text)


def test_post_type_drift_reported_and_terminal_key_never_resent() -> None:
    from computer_use_mcp.agent import ComputerUseAgent
    from computer_use_mcp.limits import Limits
    from computer_use_mcp.observation import ObservationEngine
    from computer_use_mcp.safety import SafetyPolicy
    from computer_use_mcp.state import StopToken, TaskState

    class DriftAfterType(FakeComputerBackend):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.execute_calls = 0

        def execute(self, action: GroundedAction, stop: Any = None, **kwargs: Any) -> str:
            self.execute_calls += 1
            if self.execute_calls == 1:  # the type lands, THEN focus drifts away
                self.focus_target = {
                    "hwnd_focus": 5, "root_hwnd": 999, "window_class": "Edit",
                    "text": "", "pid": 1,
                }
            return super().execute(action, stop)

    backend = DriftAfterType()
    backend.set_active_window(TARGET)
    agent = ComputerUseAgent(
        backend, provider=None, safety=SafetyPolicy(), task=TaskState(), stop=StopToken(),
        limits=Limits(max_actions=5, max_task_seconds=60.0).validate(),
    )
    agent.observation = ObservationEngine(backend)
    agent.guard.rebind(TARGET)
    state = SimpleNamespace(
        dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
        max_steps=5, step_count=0, require_approval=False, max_retries_per_action=1,
    )
    outcome = asyncio.run(agent.run_single(state, TYPE_ACTION))
    assert outcome.kind == "executed" and outcome.result is not None
    # Post-drift (abort policy) is an honest not-ok: the text may have landed elsewhere.
    assert outcome.result.ok is False
    assert FOCUS_DRIFTED in outcome.result.verification.note
    # resend_terminal_key=false (default): the Enter was never re-sent by the guard.
    assert agent.interference.focus_continuity.resend_terminal_key is False
    assert backend.execute_calls == 1


# --- T8 anomaly-B10: anchoring semantics (same-process/launcher surfaces are not drift) ------


def _flow_focus(root_hwnd: int, pid: int, root_class: str, process: str) -> dict[str, object]:
    return {
        "hwnd_focus": root_hwnd + 5,
        "root_hwnd": root_hwnd,
        "window_class": "Edit",
        "root_window_class": root_class,
        "text": "",
        "pid": pid,
        "process_name": process,
    }


def test_same_process_dialog_focus_is_not_drift() -> None:
    """(a) explorer shell -> its Run dialog child, app -> its own dialogs: NOT drift."""
    backend = _backend_with_focus(root_hwnd=TARGET.hwnd)
    # Focus is in a same-process dialog (root hwnd differs, pid matches).
    backend.focus_target = _flow_focus(888, TARGET.pid, "#32770", "EXCEL.EXE")
    guard = _armed(backend)
    assert guard.verify_pre_dispatch(TYPE_ACTION) is None


def test_dialog_hosted_focus_is_not_drift_even_across_processes() -> None:
    """Typing into a Run dialog while an app is anchored: the #32770 host is a launcher
    surface — keyboard input there is deliberate driver work, never drift."""
    backend = _backend_with_focus(root_hwnd=TARGET.hwnd)
    backend.focus_target = _flow_focus(777, 4242, "#32770", "explorer.exe")
    guard = _armed(backend)
    assert guard.verify_pre_dispatch(TYPE_ACTION) is None


def test_transient_launcher_focus_is_not_drift() -> None:
    backend = _backend_with_focus(root_hwnd=TARGET.hwnd)
    backend.focus_target = _flow_focus(555, 6964, "CabinetWClass", "explorer.exe")
    guard = _armed(backend)
    assert guard.verify_pre_dispatch(TYPE_ACTION) is None


def test_foreign_app_focus_still_drifts_protection_preserved() -> None:
    """A foreign app's normal window (different process, not a dialog) still drifts."""
    backend = _backend_with_focus(root_hwnd=TARGET.hwnd)
    backend.focus_target = _flow_focus(999, 31337, "Notepad", "somegame.exe")
    guard = _armed(backend)
    verdict = guard.verify_pre_dispatch(TYPE_ACTION)
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith(FOCUS_DRIFTED)
