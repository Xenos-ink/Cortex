"""T8 FocusGuard tests (A12 test plan T1): binding, pre-dispatch foreground check, policies.

Covers the mechanism-(i) contract:

- arming: the first ALLOWLISTED observation binds the session target; sessions without
  an allowlist stay dormant until an explicit focus bind (never binds the user's window);
- bound target foreground -> dispatch (guard returns None);
- foreign foreground -> blocking FOCUS_TAKEN_BY verdict; the backend execute is NOT
  called (asserted), and the controller rejection carries the payload + hint;
- owned #32770 dialog of the bound pid -> dispatch (allow_owned_dialogs) and rejection
  when the policy disables it;
- transient launcher (explorer.exe) -> dispatch; other foreign process -> reject;
- foreground identity unavailable -> fail-closed FOCUS_IDENTITY_UNKNOWN;
- refocus_then_abort: one verified reattach attempt dispatches; a refused reattach
  aborts with both payloads; at most ONE attempt per action instance;
- observe_only: non-blocking annotation;
- hwnd recycle recovered by pid+class+overlapping-title;
- controller-level: a FOCUS_TAKEN_BY on a queued item stops the queue (named stop),
  and a focus_window action re-binds and stays reachable while focus is stolen.
"""

from __future__ import annotations

import base64
import io
from typing import Any

from PIL import Image

from computer_use_mcp.backend import FakeComputerBackend, WindowFocusError
from computer_use_mcp.focus_guard import InterferenceGuard
from computer_use_mcp.interference import FOCUS_IDENTITY_UNKNOWN, FOCUS_TAKEN_BY, parse_interference
from computer_use_mcp.models import FailureClass, GroundedAction, WindowInfo

TARGET = WindowInfo(
    hwnd=1, pid=100, process_name="EXCEL.EXE", exe_path="C:\\apps\\EXCEL.EXE",
    window_class="XLMAIN", title="Book1 - Excel",
)
FOREIGN = WindowInfo(
    hwnd=9, pid=900, process_name="zcode.exe", window_class="CONSOLE", title="user console",
)
LAUNCHER = WindowInfo(
    hwnd=5, pid=500, process_name="explorer.exe", window_class="Explorer", title="Run",
)
OWNED_DIALOG = WindowInfo(
    hwnd=7, pid=100, process_name="EXCEL.EXE", window_class="#32770", title="Confirm Save As",
)
CLICK = GroundedAction(action="click", point={"x": 10, "y": 10}, confidence=1.0)


def _guard(backend: FakeComputerBackend, **policy: Any) -> InterferenceGuard:
    return InterferenceGuard(backend, parse_interference(policy or None))


def _armed_guard(backend: FakeComputerBackend, **policy: Any) -> InterferenceGuard:
    guard = _guard(backend, **policy)
    guard.rebind(TARGET)
    return guard


class ExecuteCounter(FakeComputerBackend):
    """Fake backend that counts engine-level execute calls (never called on rejection)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.execute_calls = 0
        self.set_windows([TARGET, FOREIGN])  # the B6 liveness probe enumerates these

    def execute(self, action: GroundedAction, stop: Any = None, **kwargs: Any) -> str:
        self.execute_calls += 1
        return super().execute(action, stop)


# --- arming doctrine -------------------------------------------------------------------------


def test_guard_is_dormant_without_allowlist_and_until_explicit_bind() -> None:
    backend = ExecuteCounter()
    backend.set_active_window(FOREIGN)
    guard = _guard(backend)
    observation = backend.observe()
    guard.maybe_bind(observation, [])  # no allowlist: dormant
    assert guard.armed is False
    assert guard.verify_pre_dispatch(CLICK) is None  # inert while dormant


def test_guard_binds_from_first_allowlisted_observation() -> None:
    backend = ExecuteCounter()
    backend.set_active_window(TARGET)
    guard = _guard(backend)
    guard.maybe_bind(backend.observe(), ["excel.exe"])
    assert guard.armed is True
    assert guard.bound is not None and guard.bound.hwnd == 1


def test_guard_ignores_non_allowlisted_first_observation() -> None:
    backend = ExecuteCounter()
    backend.set_active_window(FOREIGN)
    guard = _guard(backend)
    guard.maybe_bind(backend.observe(), ["excel.exe"])
    assert guard.armed is False


# --- matching ------------------------------------------------------------------------------


def test_bound_target_foreground_dispatches() -> None:
    backend = ExecuteCounter()
    backend.set_active_window(TARGET)
    guard = _armed_guard(backend)
    assert guard.verify_pre_dispatch(CLICK) is None


def test_foreign_foreground_rejects_with_focus_taken_by_and_no_execute() -> None:
    backend = ExecuteCounter()
    backend.set_active_window(TARGET)
    guard = _armed_guard(backend)
    backend.set_active_window(FOREIGN)  # the steal
    verdict = guard.verify_pre_dispatch(CLICK)
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith(FOCUS_TAKEN_BY)
    assert "title='user console'" in verdict.event and "process=zcode.exe" in verdict.event
    assert verdict.failure_class is FailureClass.WRONG_WINDOW
    assert any("re_ground_or_refocus" in hint for hint in verdict.hints)
    backend.execute(CLICK)  # only runs when the caller ignores the verdict
    assert backend.execute_calls == 1  # the guard itself dispatched nothing


def test_owned_dialog_of_bound_pid_dispatches_and_can_be_disabled() -> None:
    backend = ExecuteCounter()
    backend.set_active_window(OWNED_DIALOG)
    guard = _armed_guard(backend)
    assert guard.verify_pre_dispatch(CLICK) is None  # allow_owned_dialogs default true
    strict = _armed_guard(backend, focus_guard={"allow_owned_dialogs": False})
    verdict = strict.verify_pre_dispatch(CLICK)
    assert verdict is not None and verdict.event.startswith(FOCUS_TAKEN_BY)


def test_transient_launcher_dispatches_and_other_foreign_process_rejects() -> None:
    backend = ExecuteCounter()
    guard = _armed_guard(backend)
    backend.set_active_window(LAUNCHER)
    assert guard.verify_pre_dispatch(CLICK) is None  # explorer.exe default transient list
    backend.set_active_window(FOREIGN)
    assert guard.verify_pre_dispatch(CLICK) is not None
    custom = _armed_guard(backend, focus_guard={"transient_launch_processes": ["explorer.exe", "zcode.exe"]})
    assert custom.verify_pre_dispatch(CLICK) is None  # configurable, app-agnostic list


def test_identity_unavailable_fails_closed() -> None:
    backend = ExecuteCounter()
    backend.set_active_window(None)
    guard = _armed_guard(backend)
    verdict = guard.verify_pre_dispatch(CLICK)
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith(FOCUS_IDENTITY_UNKNOWN)


def test_hwnd_recycle_recovered_by_pid_class_title() -> None:
    backend = ExecuteCounter()
    guard = _armed_guard(backend)
    recycled = WindowInfo(
        hwnd=777,  # NEW hwnd (recycled), same pid+class, overlapping title
        pid=TARGET.pid,
        process_name="EXCEL.EXE",
        window_class="XLMAIN",
        title="Book1 - Excel (Recovered)",
    )
    backend.set_active_window(recycled)
    assert guard.verify_pre_dispatch(CLICK) is None
    # A different pid with the same class is NOT a recycle: reject.
    impostor = recycled.model_copy(update={"pid": 999, "hwnd": 778})
    backend.set_active_window(impostor)
    assert guard.verify_pre_dispatch(CLICK) is not None


# --- policies --------------------------------------------------------------------------------


def test_refocus_then_abort_dispatches_after_a_verified_refocus() -> None:
    class RefocusBackend(ExecuteCounter):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.refocus_calls: list[str] = []

        def focus_window_title(self, title: str) -> str:
            self.refocus_calls.append(title)
            self.set_active_window(TARGET)  # the verified reattach succeeds
            return f"Focused window '{title}'."

    backend = RefocusBackend()
    backend.set_active_window(TARGET)
    guard = _armed_guard(backend, focus_guard={"policy": "refocus_then_abort"})
    backend.set_active_window(FOREIGN)
    assert guard.verify_pre_dispatch(CLICK) is None
    assert backend.refocus_calls == ["Book1 - Excel"]  # aimed at the BOUND title only


def test_refocus_then_abort_aborts_after_a_refused_refocus_once_per_action() -> None:
    class RefusedFocusBackend(ExecuteCounter):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.refocus_calls = 0

        def focus_window_title(self, title: str) -> str:
            self.refocus_calls += 1
            raise WindowFocusError(f"SetForegroundWindow refused focus for '{title}'")

    backend = RefusedFocusBackend()
    backend.set_active_window(TARGET)
    guard = _armed_guard(backend, focus_guard={"policy": "refocus_then_abort"})
    backend.set_active_window(FOREIGN)
    verdict = guard.verify_pre_dispatch(CLICK)
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith(FOCUS_TAKEN_BY)
    assert any("refocus" in hint for hint in verdict.hints)
    assert backend.refocus_calls == 1
    # A bounded retry of the SAME action instance never re-attempts the refocus.
    assert guard.verify_pre_dispatch(CLICK) is not None
    assert backend.refocus_calls == 1


def test_observe_only_policy_annotates_without_blocking() -> None:
    backend = ExecuteCounter()
    guard = _armed_guard(backend, focus_guard={"policy": "observe_only"})
    backend.set_active_window(FOREIGN)
    verdict = guard.verify_pre_dispatch(CLICK)
    assert verdict is not None and not verdict.blocking
    assert verdict.event.startswith(FOCUS_TAKEN_BY)


# --- controller integration -------------------------------------------------------------------


def test_rejected_outcome_carries_payload_and_backend_never_runs() -> None:
    """The controller turns a blocking verdict into a structured rejection (kind=rejected)."""
    from types import SimpleNamespace

    from computer_use_mcp.agent import ComputerUseAgent
    from computer_use_mcp.limits import Limits
    from computer_use_mcp.observation import ObservationEngine
    from computer_use_mcp.safety import SafetyPolicy
    from computer_use_mcp.state import StopToken, TaskState

    backend = ExecuteCounter()
    backend.set_active_window(TARGET)
    agent = ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        # zcode is allowlisted too so the VALIDATOR passes and the GUARD is the gate
        # under test (the validator's own foreign-process rejection is covered by P0-G).
        allowed_processes=["excel.exe", "zcode.exe"],
        limits=Limits(max_actions=5, max_task_seconds=60.0).validate(),
    )
    agent.observation = ObservationEngine(backend)
    # Arm the binding from an allowlisted TARGET observation (as a prior step would),
    # then steal the foreground; the click is the first queued work item.
    agent.guard.maybe_bind(backend.observe(), agent.allowed_processes)
    backend.set_active_window(FOREIGN)  # steal BEFORE the action
    state = SimpleNamespace(
        dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
        max_steps=5, step_count=0, require_approval=False, max_retries_per_action=1,
    )
    outcome = asyncio_run(agent.run_single(state, CLICK))
    assert outcome.kind == "rejected"
    assert any(r.startswith(FOCUS_TAKEN_BY) for r in outcome.reasons)
    assert backend.execute_calls == 0  # the foreign window was never acted on


def test_focus_window_action_re_binds_and_stays_reachable_under_steal() -> None:
    from types import SimpleNamespace

    from computer_use_mcp.agent import ComputerUseAgent
    from computer_use_mcp.limits import Limits
    from computer_use_mcp.observation import ObservationEngine
    from computer_use_mcp.safety import SafetyPolicy
    from computer_use_mcp.state import StopToken, TaskState

    backend = ExecuteCounter()
    backend.set_active_window(TARGET)
    backend.windows = [TARGET, FOREIGN]
    agent = ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        allowed_processes=["excel.exe", "zcode.exe"],
        limits=Limits(max_actions=5, max_task_seconds=60.0).validate(),
    )
    agent.observation = ObservationEngine(backend)
    backend.set_active_window(FOREIGN)
    state = SimpleNamespace(
        dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
        max_steps=5, step_count=0, require_approval=False, max_retries_per_action=1,
    )
    # focus_window is EXEMPT from the guard: the reattachment path stays reachable.
    outcome = asyncio_run(
        agent.run_single(state, GroundedAction(action="focus_window", target="Book1 - Excel", confidence=1.0))
    )
    assert outcome.kind == "executed" and outcome.result is not None
    assert outcome.result.ok is True
    assert agent.guard.armed is True
    assert agent.guard.bound is not None and agent.guard.bound.hwnd == TARGET.hwnd


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


# --- B6 (T8 coordinator finding): bound window GONE -> structured TARGET_GONE, no deadlock ----


def test_target_gone_reports_and_unbinds_by_default() -> None:
    """A closed bound window must NOT deadlock the session with endless rejections."""
    backend = ExecuteCounter()
    backend.set_windows([FOREIGN])  # TARGET is NOT in the population: it is gone
    backend.set_active_window(FOREIGN)
    guard = _armed_guard(backend)
    assert guard.armed is True
    verdict = guard.verify_pre_dispatch(CLICK)
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith("TARGET_GONE")
    assert any("never relaunch blindly" in hint for hint in verdict.hints)
    # The binding is CLEARED: the guard is dormant again, so the driver's
    # re-ground / reattach actions (focus_window / ensure_app) proceed.
    assert guard.armed is False
    assert guard.verify_pre_dispatch(CLICK) is None


def test_target_gone_keep_binding_policy_keeps_rejecting() -> None:
    backend = ExecuteCounter()
    backend.set_windows([FOREIGN])
    backend.set_active_window(FOREIGN)
    guard = _armed_guard(backend, focus_guard={"on_target_gone": "keep_binding"})
    verdict = guard.verify_pre_dispatch(CLICK)
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith("TARGET_GONE")
    assert guard.armed is True  # the host chose to keep the dead binding
    assert guard.verify_pre_dispatch(CLICK) is not None


def test_alive_bound_window_still_rejects_with_focus_taken_by() -> None:
    """The liveness probe must not weaken the foreign-foreground rejection."""
    backend = ExecuteCounter()  # TARGET registered -> alive
    guard = _armed_guard(backend)
    backend.set_active_window(FOREIGN)
    verdict = guard.verify_pre_dispatch(CLICK)
    assert verdict is not None and verdict.event.startswith(FOCUS_TAKEN_BY)


# --- T8 anomaly-B10: Win+R / ctrl+L flows complete in blocking mode with zero false rejections


class FlowBackend(FakeComputerBackend):
    """Scripted multi-surface world: observe() reports the current scene, and the
    keyboard-focus probe follows the focused surface's ROOT window."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.scene: WindowInfo | None = None
        self.focus_root: WindowInfo | None = None
        # executed-count -> (scene, focus_root): transitions happen DURING the action
        # (the launch the action caused), so the pipeline's post-action observe sees
        # the new surface exactly like the real desktop.
        self.transitions: dict[int, tuple[WindowInfo, WindowInfo]] = {}

    def execute(self, action: GroundedAction, stop: Any = None, **kwargs: Any) -> str:
        message = super().execute(action, stop)
        transition = self.transitions.get(len(self.executed))
        if transition is not None:
            self.set_scene(transition[0], transition[1])
        return message

    def set_scene(self, window: WindowInfo, focus_root: WindowInfo | None = None) -> None:
        self.scene = window
        self.set_active_window(window)
        self.focus_root = focus_root or window

    def observe(self, monitor_index: Any = None) -> Any:
        observation = super().observe(monitor_index)
        observation.active_window = (self.scene.title or None) if self.scene else None
        observation.active_window_info = self.scene.model_copy() if self.scene else None
        # Real screens change on keyboard input: alternate colors per executed action so
        # each action's diff tier verifies (the guard rules are what this test targets).
        color = "white" if len(self.executed) % 2 == 0 else "black"
        image = Image.new("RGB", (self.width, self.height), color)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        observation.image_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
        return observation

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


SHELL = WindowInfo(hwnd=10, pid=6964, process_name="explorer.exe", window_class="Progman", title="Program Manager")
RUN = WindowInfo(hwnd=11, pid=6964, process_name="explorer.exe", window_class="#32770", title="Run")
NOTEPAD_WIN = WindowInfo(hwnd=12, pid=500, process_name="notepad.exe", window_class="Notepad", title="Untitled - Notepad")
EDGE = WindowInfo(hwnd=20, pid=700, process_name="msedge.exe", window_class="Chrome_WidgetWin_1", title="Bench - Microsoft Edge")


def _flow_agent(backend: FlowBackend, processes: list[str]) -> Any:
    from types import SimpleNamespace

    from computer_use_mcp.agent import ComputerUseAgent
    from computer_use_mcp.limits import Limits
    from computer_use_mcp.observation import ObservationEngine
    from computer_use_mcp.safety import SafetyPolicy
    from computer_use_mcp.state import StopToken, TaskState

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
    return agent, state


def test_win_r_flow_blocking_mode_zero_false_rejections() -> None:
    """anchor(shell) -> Run dialog -> type -> app launch -> re-anchor -> type: all clean."""
    import asyncio

    backend = FlowBackend()
    backend.set_windows([SHELL, RUN, NOTEPAD_WIN])
    backend.set_scene(SHELL)
    # win+r -> the Run dialog takes the foreground (same process); enter -> the
    # launched app takes over (new process, launched by OUR action).
    backend.transitions = {1: (RUN, RUN), 3: (NOTEPAD_WIN, NOTEPAD_WIN)}
    agent, state = _flow_agent(backend, ["explorer.exe", "notepad.exe"])
    outcomes = []
    for action in (
        GroundedAction(action="hotkey", keys=["win", "r"], confidence=1.0),
        GroundedAction(action="type", text="notepad", confidence=1.0),
        GroundedAction(action="keypress", keys=["enter"], confidence=1.0),
        GroundedAction(action="type", text="hello from the launched app", confidence=1.0),
    ):
        outcomes.append(asyncio.run(agent.run_single(state, action)))
    # ZERO false rejections: every action executed and verified.
    assert [o.kind for o in outcomes] == ["executed"] * 4
    assert all(o.result is not None and o.result.ok for o in outcomes)
    assert all(not (o.interference_events or []) for o in outcomes)
    # The anchor tracked the surfaces: same-process dialog re-anchor, then the launch.
    assert agent.guard.bound is not None
    assert agent.guard.bound.hwnd == NOTEPAD_WIN.hwnd


def test_ctrl_l_flow_no_false_drift() -> None:
    """ctrl+L address-bar flow: focus stays in the anchored window root — never drift."""
    import asyncio

    backend = FlowBackend()
    backend.set_windows([EDGE])
    backend.set_scene(EDGE, EDGE)
    agent, state = _flow_agent(backend, ["msedge.exe"])
    for action in (
        GroundedAction(action="hotkey", keys=["ctrl", "l"], confidence=1.0),
        GroundedAction(action="type", text="file:///C:/bench/page.html", confidence=1.0),
    ):
        outcome = asyncio.run(agent.run_single(state, action))
        assert outcome.kind == "executed", outcome.reasons
        assert outcome.result is not None and outcome.result.ok
    assert agent.guard.bound is not None and agent.guard.bound.hwnd == EDGE.hwnd


def test_untitled_shell_surface_never_anchors() -> None:
    """(c) an untitled/anonymous shell surface must never become the anchor."""
    backend = FlowBackend()
    untitled = WindowInfo(hwnd=30, pid=6964, process_name="explorer.exe", window_class="WorkerW", title="")
    backend.set_windows([untitled])
    backend.set_scene(untitled)
    guard = InterferenceGuard(backend, parse_interference(None))
    guard.maybe_bind(backend.observe(), ["explorer.exe"])
    assert guard.armed is False  # dormant, never anchored to the anonymous surface


# --- re-anchor rules (B10 (b)) --------------------------------------------------------------


def test_reanchor_after_verified_same_process_surface() -> None:
    backend = FlowBackend()
    guard = _armed_guard(backend)
    backend.set_windows([TARGET, RUN])
    guard.reanchor_after_success(RUN)  # same pid as TARGET? no — explorer vs excel
    # Different pid AND the anchor (Excel) is alive and not a launcher: NOT re-anchored.
    assert guard.bound is not None and guard.bound.hwnd == TARGET.hwnd


def test_reanchor_after_verified_follows_own_launch() -> None:
    backend = FlowBackend()
    guard = InterferenceGuard(backend, parse_interference(None))
    guard.rebind(RUN)  # the anchor is the Run dialog (a launcher surface)
    guard.reanchor_after_success(NOTEPAD_WIN)  # the app WE launched took over
    assert guard.bound is not None and guard.bound.hwnd == NOTEPAD_WIN.hwnd


def test_reanchor_skipped_for_unverified_outcomes() -> None:
    """The agent only re-anchors on VERIFIED actions (helper contract)."""
    from types import SimpleNamespace

    from computer_use_mcp.agent import ComputerUseAgent

    backend = FlowBackend()
    guard = InterferenceGuard(backend, parse_interference(None))
    guard.rebind(TARGET)
    observation = SimpleNamespace(active_window_info=TARGET)
    # The agent-level gate: re-anchor ONLY on verified outcomes.
    assert ComputerUseAgent._verified_reanchor(guard, "uncertain", observation) is None
    assert ComputerUseAgent._verified_reanchor(guard, "failed", observation) is None
    assert ComputerUseAgent._verified_reanchor(guard, "verified", observation) is not None


# --- B11 (A7b confirmation): class-AGNOSTIC ownership for the pre-dispatch gate --------------
# Excel's native modals (class bosa_sdm_XL9, owner == bound hwnd, same process) were
# falsely rejected by FOCUS_TAKEN_BY while the sentinel matched the same window as
# owner_chain. Guard and sentinel now share ONE ownership helper; foreign windows
# stay rejected.


EXCEL = WindowInfo(hwnd=40, pid=200, process_name="EXCEL.EXE", window_class="XLMAIN", title="Book1 - Excel")
BOSA_MODAL = WindowInfo(hwnd=41, pid=200, process_name="EXCEL.EXE", window_class="bosa_sdm_XL9", title="Microsoft Excel")
CUSTOM_OWNED = WindowInfo(hwnd=42, pid=999, process_name="vendor.exe", window_class="VendorDialog", title="Vendor modal")


def _excel_guard(backend: ExecuteCounter, **policy: Any) -> InterferenceGuard:
    """Guard bound to the EXCEL window (the B6 liveness probe needs it registered)."""
    guard = InterferenceGuard(backend, parse_interference(policy or None))
    guard.rebind(EXCEL)
    return guard


def test_owned_native_modal_bosa_class_dispatches() -> None:
    """The A7b incident: an Excel bosa_sdm_XL9 owned modal (same pid) is OURS."""
    backend = ExecuteCounter()
    backend.set_windows([EXCEL, BOSA_MODAL])
    backend.set_active_window(BOSA_MODAL)
    guard = _excel_guard(backend)  # bound to EXCEL (same pid 200)
    assert guard.verify_pre_dispatch(CLICK) is None  # NO FOCUS_TAKEN_BY


def test_custom_class_owner_chained_modal_dispatches() -> None:
    """A cross-process owner-chained modal of ANY class is exempted via the shared probe."""
    backend = ExecuteCounter()
    backend.set_windows([EXCEL, CUSTOM_OWNED])
    backend.owned_windows = {(CUSTOM_OWNED.hwnd, EXCEL.hwnd)}  # GW_OWNER chain roots at bound
    backend.set_active_window(CUSTOM_OWNED)
    guard = _excel_guard(backend)
    assert guard.verify_pre_dispatch(CLICK) is None


def test_owned_dialog_32770_still_dispatches_regression() -> None:
    backend = ExecuteCounter()
    backend.set_windows([EXCEL, OWNED_DIALOG])
    backend.owned_windows = {(OWNED_DIALOG.hwnd, EXCEL.hwnd)}  # owner-chained to bound
    backend.set_active_window(OWNED_DIALOG)
    guard = _excel_guard(backend)
    assert guard.verify_pre_dispatch(CLICK) is None


def test_foreign_same_class_window_still_rejected() -> None:
    """A foreign app's window (different pid, NOT owner-chained) is still a steal."""
    backend = ExecuteCounter()
    foreign_bosa = BOSA_MODAL.model_copy(update={"pid": 31337, "hwnd": 43})
    backend.set_windows([EXCEL, foreign_bosa])  # no ownership pair registered
    backend.set_active_window(foreign_bosa)
    guard = _excel_guard(backend)
    verdict = guard.verify_pre_dispatch(CLICK)
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith(FOCUS_TAKEN_BY)
    assert "bosa_sdm_XL9" in verdict.event


def test_allow_owned_dialogs_false_still_rejects_owned_modal() -> None:
    """The policy gate still governs the whole exemption (no semantics weakened)."""
    backend = ExecuteCounter()
    backend.set_windows([EXCEL, BOSA_MODAL])
    backend.set_active_window(BOSA_MODAL)
    guard = _excel_guard(backend, focus_guard={"allow_owned_dialogs": False})
    verdict = guard.verify_pre_dispatch(CLICK)
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith(FOCUS_TAKEN_BY)


def test_guard_and_sentinel_share_one_ownership_helper(monkeypatch: Any) -> None:
    """Guard and sentinel MUST agree on ownership (the A7b divergence is impossible)."""
    import computer_use_mcp.backend as backend_module

    calls: list[tuple[int | None, int | None]] = []

    def _spy(hwnd: object, ancestor: object) -> bool:
        calls.append((hwnd, ancestor))
        return int(hwnd or 0) == CUSTOM_OWNED.hwnd and int(ancestor or 0) == EXCEL.hwnd

    monkeypatch.setattr(backend_module, "is_window_owned_by", _spy)
    from computer_use_mcp.backend import LocalComputerBackend

    backend = LocalComputerBackend.__new__(LocalComputerBackend)  # probes only, no init
    guard = InterferenceGuard(backend, parse_interference(None))
    guard.rebind(EXCEL)
    # The cross-pid owned case is the one that MUST consult the shared helper (the
    # same-pid rule short-circuits before it).
    assert guard._is_owned_dialog(CUSTOM_OWNED, EXCEL) is True  # helper scripted True
    assert calls == [(CUSTOM_OWNED.hwnd, EXCEL.hwnd)]


def test_guard_and_sentinel_agree_on_one_real_window_table(monkeypatch: Any) -> None:
    """One window table, three consumers, ONE ownership answer: guard exempt, sentinel
    owner_chain match, shared helper verdict — the A7b divergence is structurally dead."""
    import ctypes

    import computer_use_mcp.backend as backend_module
    from computer_use_mcp.backend import LocalComputerBackend

    hwnds: dict[int, dict[str, object]] = {
        40: {"class": "XLMAIN", "text": "Book1 - Excel", "pid": 200, "owner": 0},
        41: {"class": "bosa_sdm_XL9", "text": "Microsoft Excel",
             "pid": 200, "owner": 40},  # Excel-native owned modal
    }

    class _FakeUser32:
        @staticmethod
        def _h(hwnd: Any) -> int:
            value = getattr(hwnd, "value", hwnd)
            return int(value or 0)

        def IsWindow(self, hwnd: Any) -> bool:
            return self._h(hwnd) in hwnds

        def GetForegroundWindow(self) -> int:
            return 41

        def GetAncestor(self, hwnd: Any, flag: int) -> int:
            return self._h(hwnd)

        def GetWindow(self, hwnd: Any, flag: int) -> int:
            return int(hwnds.get(self._h(hwnd), {}).get("owner", 0)) if flag == 4 else 0

        def GetWindowTextLengthW(self, hwnd: Any) -> int:
            return len(str(hwnds.get(self._h(hwnd), {}).get("text", "")))

        def GetWindowTextW(self, hwnd: Any, buffer: Any, size: int) -> int:
            text = str(hwnds.get(self._h(hwnd), {}).get("text", ""))
            value = ctypes.create_unicode_buffer(text)
            buffer.value = value.value
            return len(text)

        def GetClassNameW(self, hwnd: Any, buffer: Any, size: int) -> int:
            name = str(hwnds.get(self._h(hwnd), {}).get("class", ""))
            value = ctypes.create_unicode_buffer(name)
            buffer.value = value.value
            return len(name)

        def GetWindowThreadProcessId(self, hwnd: Any, pointer: Any) -> int:
            pid = int(hwnds.get(self._h(hwnd), {}).get("pid", 0))
            if pointer is not None:
                pointer._obj.value = pid  # byref(DWORD) pointer
            return 1

    monkeypatch.setattr(backend_module, "_user32", _FakeUser32())
    backend = LocalComputerBackend.__new__(LocalComputerBackend)  # probes only, no init

    modal = WindowInfo(hwnd=41, pid=200, process_name="EXCEL.EXE",
                       window_class=str(hwnds[41]["class"]), title="Microsoft Excel")
    bound = WindowInfo(hwnd=40, pid=200, process_name="EXCEL.EXE",
                       window_class="XLMAIN", title="Book1 - Excel")
    # 1) shared helper: owned.
    assert backend_module.is_window_owned_by(41, 40) is True
    # 2) the GUARD exempts the very same window (cross-pid not needed; same pid):
    guard = InterferenceGuard(backend, parse_interference(None))
    guard.rebind(bound)
    assert guard._is_owned_dialog(modal, bound) is True
    # 3) the SENTINEL classifies the same window as owner_chain:
    dialog = backend.detect_system_dialog(40, [])
    assert dialog is not None and dialog["matched"] == "owner_chain"
    assert dialog["hwnd"] == 41 and dialog["window_class"] == str(hwnds[41]["class"])


def test_shared_ownership_helper_traverses_owner_chain(monkeypatch: Any) -> None:
    """The shared GW_OWNER traversal roots multi-hop chains at the bound window."""
    import computer_use_mcp.backend as backend_module

    class _FakeUser32:
        def __init__(self, chain: dict[int, int]) -> None:
            self._chain = chain
            self.hwnds = set(chain) | set(chain.values())

        @staticmethod
        def _as_hwnd(hwnd: Any) -> int:
            # ctypes calls pass c_void_p objects; normalize like the argtypes would.
            value = getattr(hwnd, "value", hwnd)
            return int(value or 0)

        def IsWindow(self, hwnd: Any) -> bool:
            return self._as_hwnd(hwnd) in self.hwnds

        def GetWindow(self, hwnd: Any, flag: int) -> int:
            return self._chain.get(self._as_hwnd(hwnd), 0) if flag == 4 else 0

        def GetAncestor(self, hwnd: Any, flag: int) -> int:
            return self._as_hwnd(hwnd)

    fake = _FakeUser32({41: 40})  # modal 41 owned by 40
    monkeypatch.setattr(backend_module, "_user32", fake)
    assert backend_module.is_window_owned_by(41, 40) is True
    assert backend_module.is_window_owned_by(41, 999) is False
    assert backend_module.is_window_owned_by(None, 40) is False
