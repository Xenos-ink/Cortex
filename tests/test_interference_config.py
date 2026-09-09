"""T8 interference-policy config tests (A12 test plan T6): fail-closed parsing + budget.

Covers:

- the policy defaults equal A12 section 4's table exactly (every default protective);
- ``parse_interference`` is fail-closed like ``server._parse_limits``: unknown
  sections/fields, wrong types, and invalid enum values are REJECTED;
- ``start_session(interference=...)`` is trailing-optional; malformed policies are
  refused with the typed ``invalid_interference`` error; legacy callers (no param)
  get the same protective defaults;
- the Interference Guard's per-action overhead stays inside the sub-5 ms budget
  (measured in-test with stubs, mirroring the A12 cost table);
- the event-payload formatters produce the single parseable strings the driver
  protocol teaches (FOCUS_TAKEN_BY / MODAL_DIALOG / REATTACHED / ...).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

# Shared server-reset fixture: re-exported under its own name so tests in this module
# can request it by parameter name (plain alias — no wrapper, no shadowing).
from test_controller_integration import fresh_server as _shared_fresh_server_fixture

fresh_server = _shared_fresh_server_fixture

from computer_use_mcp import server
from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.focus_guard import InterferenceGuard
from computer_use_mcp.interference import (
    AMBIGUOUS_INSTANCE,
    DEFAULT_DIALOG_TITLE_TABLE,
    DEFAULT_TRANSIENT_LAUNCH_PROCESSES,
    FOCUS_DRIFTED,
    FOCUS_IDENTITY_UNKNOWN,
    FOCUS_TAKEN_BY,
    MODAL_DIALOG,
    NO_INSTANCE,
    REATTACHED,
    STUCK_MODIFIER,
    InterferencePolicy,
    format_ambiguous_instance,
    format_focus_drifted,
    format_focus_identity_unknown,
    format_focus_taken_by,
    format_modal_dialog,
    format_no_instance,
    format_reattached,
    format_stuck_modifier,
    parse_interference,
)
from computer_use_mcp.models import GroundedAction, WindowInfo

# --- defaults (A12 section 4 policy surface) -------------------------------------------------


def test_defaults_match_a12_policy_table() -> None:
    policy = parse_interference(None)
    assert policy.focus_guard.enabled is True
    assert policy.focus_guard.policy == "abort"
    assert policy.focus_guard.allow_owned_dialogs is True
    assert policy.focus_guard.transient_launch_processes == DEFAULT_TRANSIENT_LAUNCH_PROCESSES
    assert DEFAULT_TRANSIENT_LAUNCH_PROCESSES == ["explorer.exe"]
    assert policy.focus_guard.on_identity_unknown == "abort"

    assert policy.attach_or_launch.enabled is True
    # REM-B (H2a): the launch default flipped driver -> server so ensure_app can
    # bootstrap control (target still allowlist-gated on the agent). The env knob
    # CORTEX_ATTACH_OR_LAUNCH=driver restores the old default for legacy hosts.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.delenv("CORTEX_ATTACH_OR_LAUNCH", raising=False)
    try:
        policy = parse_interference(None)
        assert policy.attach_or_launch.launch == "server"
    finally:
        monkeypatch.undo()

    assert policy.dialog_sentinel.enabled is True
    assert policy.dialog_sentinel.policy == "halt"
    assert policy.dialog_sentinel.auto_handle == []  # no auto-clicks, ever, by default
    assert policy.dialog_sentinel.title_table == DEFAULT_DIALOG_TITLE_TABLE
    assert "save as" in policy.dialog_sentinel.title_table

    assert policy.focus_continuity.enabled is True
    assert policy.focus_continuity.on_drift == "abort"
    assert policy.focus_continuity.resend_terminal_key is False

    assert policy.hotkey_guard.enabled is True
    assert policy.hotkey_guard.on_stuck_modifier == "abort"


def test_parse_round_trip_and_partial_overrides() -> None:
    policy = parse_interference(
        {
            "focus_guard": {"policy": "refocus_then_abort"},
            "dialog_sentinel": {"policy": "report", "title_table": ["save as", "overwrite?"]},
            "hotkey_guard": {"on_stuck_modifier": "release"},
        }
    )
    assert policy.focus_guard.policy == "refocus_then_abort"
    assert policy.dialog_sentinel.policy == "report"
    assert policy.dialog_sentinel.title_table == ["save as", "overwrite?"]
    assert policy.hotkey_guard.on_stuck_modifier == "release"
    # Untouched sections keep their protective defaults (REM-B: launch default is
    # server — see test_defaults_match_a12_policy_table for the flip rationale).
    assert policy.attach_or_launch.launch == "server"
    assert policy.focus_continuity.on_drift == "abort"


def test_parse_rejects_unknown_sections_and_fields_fail_closed() -> None:
    with pytest.raises(ValueError, match="Unknown interference policy sections"):
        parse_interference({"bogus_section": {}})
    with pytest.raises(ValueError):
        parse_interference({"focus_guard": {"bogus_field": True}})
    with pytest.raises(ValueError):
        parse_interference({"focus_guard": {"policy": "not_a_policy"}})
    with pytest.raises(TypeError):
        parse_interference("not-a-dict")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        parse_interference({"dialog_sentinel": {"auto_handle": [{"title": "x"}]}})  # button missing


def test_auto_handle_spec_shape_is_validated() -> None:
    policy = parse_interference(
        {"dialog_sentinel": {"auto_handle": [{"title": "Confirm Save As", "button": "Yes"}]}}
    )
    spec = policy.dialog_sentinel.auto_handle[0]
    assert spec.title == "Confirm Save As"
    assert spec.button == "Yes"


# --- start_session surface ---------------------------------------------------------------------


def test_start_session_interference_param_is_trailing_optional() -> None:
    import inspect

    parameters = list(inspect.signature(server.start_session).parameters.values())
    assert [p.name for p in parameters][-1] == "interference"
    assert parameters[-1].default is None


def test_start_session_rejects_malformed_interference_fail_closed() -> None:
    response = server.start_session(interference={"bogus_section": {}})
    assert response["ok"] is False
    assert response["error"] == "invalid_interference"
    assert "bogus_section" in str(response["message"])


def test_start_session_legacy_callers_get_protective_defaults(fresh_server: Any) -> None:
    response = server.start_session(dry_run=True)
    assert "session_id" in response  # success shape (no ok key on legacy success payloads)
    bundle = server._get_bundle(str(response["session_id"]))
    policy = bundle.agent.interference
    assert isinstance(policy, InterferencePolicy)
    assert policy.focus_guard.policy == "abort"
    assert policy.dialog_sentinel.auto_handle == []
    assert policy.hotkey_guard.on_stuck_modifier == "abort"


def test_start_session_custom_policy_reaches_the_agent(fresh_server: Any) -> None:
    response = server.start_session(
        dry_run=True, interference={"focus_guard": {"policy": "observe_only"}}
    )
    assert "session_id" in response
    bundle = server._get_bundle(str(response["session_id"]))
    assert bundle.agent.interference.focus_guard.policy == "observe_only"
    assert bundle.extra["interference_policy"] is bundle.agent.interference


# --- overhead budget (A12: sub-5 ms per action with stubs) --------------------------------------


def test_guard_overhead_stays_under_budget_with_stubs() -> None:
    backend = FakeComputerBackend()
    guard = InterferenceGuard(backend, None)
    backend.active_window = WindowInfo(
        hwnd=1, pid=100, process_name="excel.exe", window_class="XLMAIN", title="Book1 - Excel"
    )
    guard.rebind(backend.active_window)
    action = GroundedAction(action="click", point={"x": 10, "y": 10}, confidence=1.0)
    # Warm up, then measure the pre-dispatch + post-action probe pair.
    guard.verify_pre_dispatch(action)
    guard.post_action_events(action, None)
    started = time.perf_counter()
    for _ in range(1000):
        guard.verify_pre_dispatch(action)
        guard.post_action_events(action, None)
    elapsed_ms = (time.perf_counter() - started) * 1000.0 / 1000.0
    assert elapsed_ms < 5.0, f"guard overhead {elapsed_ms:.3f} ms/action exceeds the 5 ms budget"


# --- event payload formatters (driver-parsed single strings) ------------------------------------


def test_event_payload_formatters() -> None:
    info = WindowInfo(
        hwnd=42, pid=7, process_name="zcode.exe", window_class="CONSOLE", title="user console"
    )
    payload = format_focus_taken_by(info)
    assert payload.startswith(FOCUS_TAKEN_BY)
    assert "title='user console'" in payload and "process=zcode.exe" in payload and "hwnd=42" in payload

    assert format_focus_identity_unknown().startswith(FOCUS_IDENTITY_UNKNOWN)

    drifted = format_focus_drifted("Book1 - Excel", "somewhere else")
    assert drifted.startswith(FOCUS_DRIFTED) and "expected=" in drifted and "actual=" in drifted

    class _Dialog:
        title = "Confirm Save As"
        window_class = "#32770"
        hwnd = 99
        owner_hwnd = 42
        matched = "class"

    modal = format_modal_dialog(_Dialog())
    assert modal.startswith(MODAL_DIALOG)
    assert "class='#32770'" in modal and "hwnd=99" in modal and "owner_hwnd=42" in modal

    assert format_reattached("Book1 - Excel", 42).startswith(REATTACHED)
    assert "title='Book1 - Excel'" in format_reattached("Book1 - Excel", 42)

    class _Candidate:
        title = "Book1 - Excel"
        unsaved_candidate = True

    ambiguous = format_ambiguous_instance([_Candidate()])
    assert ambiguous.startswith(AMBIGUOUS_INSTANCE)
    assert "unsaved=true" in ambiguous and "do_not_launch=true" in ambiguous

    no_instance = format_no_instance("excel.exe", "driver")
    assert no_instance.startswith(NO_INSTANCE) and "launch=driver" in no_instance

    stuck = format_stuck_modifier(["ctrl"])
    assert stuck.startswith(STUCK_MODIFIER) and "keys=[ctrl]" in stuck


def test_ensure_app_probe_outcomes_are_recognized_by_the_agent_helper() -> None:
    from computer_use_mcp.agent import ComputerUseAgent

    assert ComputerUseAgent._ensure_app_probe_outcome("NO_INSTANCE target='excel.exe' launch=driver")
    assert ComputerUseAgent._ensure_app_probe_outcome("AMBIGUOUS_INSTANCE candidates=[...]")
    assert not ComputerUseAgent._ensure_app_probe_outcome("REATTACHED title='Book1' hwnd=1")


def test_guard_rejection_maps_to_named_queue_stop_reasons() -> None:
    from computer_use_mcp.agent import ComputerUseAgent, SingleActionOutcome

    def _rejected(reasons: list[str]) -> SingleActionOutcome:
        return SingleActionOutcome(kind="rejected", reasons=reasons)

    assert (
        ComputerUseAgent._interference_stop_reason(_rejected(["FOCUS_TAKEN_BY title='x'"]))
        == "focus_taken_by"
    )
    assert (
        ComputerUseAgent._interference_stop_reason(_rejected(["STUCK_MODIFIER keys=[ctrl]"]))
        == "stuck_modifier"
    )
    assert (
        ComputerUseAgent._interference_stop_reason(_rejected(["FOCUS_DRIFTED expected='x'"]))
        == "focus_drifted"
    )
    assert ComputerUseAgent._interference_stop_reason(_rejected(["Grounding failed: boom"])) is None
    assert (
        ComputerUseAgent._interference_stop_reason(SingleActionOutcome(kind="executed")) is None
    )


def test_full_queue_with_a_focus_steal_between_items_stops_named() -> None:
    """A focus steal between queue items halts the batch with follow_ups_stopped_reason."""
    from io import BytesIO
    from types import SimpleNamespace

    from PIL import Image as PILImage

    from computer_use_mcp.agent import ComputerUseAgent
    from computer_use_mcp.limits import Limits
    from computer_use_mcp.models import ActionSpec, WindowInfo
    from computer_use_mcp.observation import ObservationEngine
    from computer_use_mcp.safety import SafetyPolicy
    from computer_use_mcp.state import StopToken, TaskState

    class StealAfterFirstClick(FakeComputerBackend):
        """The screen flips once after the first execute (so item 0 verifies) and a
        foreign (allowlisted, to isolate the guard from the validator) window steals
        the foreground at the same moment."""

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.executes = 0
            self._flipped = False

        def _png(self, color: str) -> str:
            image = PILImage.new("RGB", (self.width, self.height), color)
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            import base64

            return base64.b64encode(buffer.getvalue()).decode("ascii")

        def observe(self, monitor_index: Any = None) -> Any:
            observation = super().observe(monitor_index)
            if self._flipped:
                observation.image_base64 = self._png("black")
            return observation

        def execute(self, action: GroundedAction, stop: Any = None, **kwargs: Any) -> str:
            message = super().execute(action, stop)
            self.executes += 1
            if self.executes == 1:
                self._flipped = True  # the screen changes -> item 0 verifies via diff
                # The steal: a foreign (but allowlisted, to isolate the guard from the
                # validator's process gate) window takes the foreground.
                self.set_active_window(
                    WindowInfo(
                        hwnd=999,
                        pid=999,
                        process_name="zcode.exe",
                        window_class="CONSOLE",
                        title="user console",
                    )
                )
            return message

    backend = StealAfterFirstClick()
    target = WindowInfo(
        hwnd=1, pid=100, process_name="EXCEL.EXE", window_class="XLMAIN", title="Book1 - Excel"
    )
    backend.set_windows([target])  # registered population: the B6 liveness probe sees it
    backend.set_active_window(target)
    agent = ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        allowed_processes=["EXCEL.EXE", "zcode.exe"],
        limits=Limits(max_actions=10, max_task_seconds=60.0).validate(),
    )
    agent.observation = ObservationEngine(backend)
    state = SimpleNamespace(
        dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
        max_steps=5, step_count=0, require_approval=False, max_retries_per_action=1,
    )
    outcome = asyncio.run(
        agent.run_single(
            state,
            GroundedAction(action="click", point={"x": 10, "y": 10}, confidence=1.0),
            approved=True,
            follow_ups=[
                ActionSpec(action="click", x=12, y=12),
                ActionSpec(action="click", x=14, y=14),
            ],
        )
    )
    assert outcome.follow_ups_stopped_reason == "focus_taken_by"
    assert len(outcome.follow_up_results or []) == 2  # primary + the rejected follow-up
    # The foreign window was never acted on: only the first click executed.
    assert backend.executes == 1
    entry = outcome.follow_up_results[1]
    assert any(str(r).startswith("FOCUS_TAKEN_BY") for r in entry["reasons"])
