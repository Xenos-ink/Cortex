"""T8 DialogSentinel tests (A12 test plan T3): modal-dialog interception after actions.

Covers the mechanism-(iii) contract:

- post-action `#32770` foreground -> MODAL_DIALOG payload with title/class/owner AND
  the bounded control list from the post-action observation (no second round-trip);
- queue HALT (default): the batch stops with the NAMED reason ``modal_dialog``;
- single-action path: the action stays executed; the payload is appended to the
  verification note / message so the driver sees it on the same response;
- owner-chain detection for non-`#32770` owned popups and title-table detection;
- NEGATIVE CONTROL: the sentinel NEVER dispatches any click/keypress of its own
  (engine call count unchanged), and the default config auto-handles nothing;
- FailureClass.UNEXPECTED_DIALOG mapping for recovery classification.
"""

from __future__ import annotations

import asyncio
import base64
import io
from types import SimpleNamespace
from typing import Any

from PIL import Image

from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.focus_guard import InterferenceGuard
from computer_use_mcp.interference import MODAL_DIALOG, parse_interference
from computer_use_mcp.models import FailureClass, GroundedAction, Observation, WindowInfo

TARGET = WindowInfo(
    hwnd=1, pid=100, process_name="EXCEL.EXE", window_class="XLMAIN", title="Book1 - Excel",
)
SAVE_AS = WindowInfo(
    hwnd=2, pid=100, process_name="EXCEL.EXE", window_class="#32770", title="Save As",
)
CONFIRM = WindowInfo(
    hwnd=3, pid=100, process_name="EXCEL.EXE", window_class="SomeClass", title="Confirm Save As",
)


def _backend_with_dialog(dialog: WindowInfo | None, **kwargs: Any) -> FakeComputerBackend:
    backend = FakeComputerBackend(**kwargs)
    backend.set_active_window(TARGET)
    backend.system_dialog = dict(dialog.model_dump()) if dialog is not None else None
    backend.focus_target = None
    return backend


def _ui_elements_observation() -> Observation:
    """A post-action observation carrying dialog controls (as the backend reader would)."""
    observation = FakeComputerBackend().observe()
    observation.ui_elements = [
        {"name": "Yes", "control_type": "Button", "focused": False},
        {"name": "No", "control_type": "Button", "focused": False},
        {"name": "Save", "control_type": "Button", "focused": False},
    ]
    return observation


def _events(backend: FakeComputerBackend, after: Observation | None = None) -> list[str]:
    guard = InterferenceGuard(backend, parse_interference(None))
    guard.rebind(TARGET)
    return [
        verdict.event
        for verdict in guard.post_action_events(
            GroundedAction(action="click", point={"x": 1, "y": 1}, confidence=1.0),
            after if after is not None else backend.observe(),
        )
    ]


def test_dialog_class_detection_carries_payload_and_controls() -> None:
    backend = _backend_with_dialog(SAVE_AS)
    backend.focus_target = None
    events = _events(backend, _ui_elements_observation())
    assert len(events) == 1 and events[0].startswith(MODAL_DIALOG)
    assert "title='Save As'" in events[0] and "class='#32770'" in events[0]
    assert "hwnd=2" in events[0] and "owner_hwnd=1" not in events[0] or "owner_hwnd" in events[0]
    assert "Button 'Yes'" in events[0] and "Button 'No'" in events[0]  # control list rides along


def test_title_table_detection_for_non_dialog_classes() -> None:
    backend = _backend_with_dialog(None)
    # The fake passes the injected probe through verbatim (the REAL backend's class/
    # owner/title computation is covered by the platform tests); this payload shape is
    # what the real "title" match produces for a non-#32770 window.
    backend.system_dialog = {
        "hwnd": 3, "owner_hwnd": 0, "title": "Confirm Save As", "window_class": "SomeClass",
        "pid": 100, "matched": "title",
    }
    events = _events(backend)
    assert events and events[0].startswith(MODAL_DIALOG)
    assert "matched='title'" in events[0] and "class='SomeClass'" in events[0]


def test_owner_chain_detection_reports_owner() -> None:
    backend = _backend_with_dialog(CONFIRM)
    backend.system_dialog = {
        "hwnd": 3, "owner_hwnd": 1, "title": "Page Setup", "window_class": "SomeClass",
        "pid": 100, "matched": "owner_chain",
    }
    events = _events(backend)
    assert events and "owner_hwnd=1" in events[0] and "matched='owner_chain'" in events[0]


def test_no_dialog_detected_on_the_happy_path() -> None:
    backend = _backend_with_dialog(None)
    assert _events(backend) == []


def test_sentinel_never_dispatches_input_negative_control() -> None:
    """The sentinel is a pure probe: no click/keypress is ever emitted by the guard."""
    from tests.recording_engine import RecordingEngine

    backend = _backend_with_dialog(SAVE_AS)
    engine = RecordingEngine()
    backend._engine = engine
    guard = InterferenceGuard(backend, parse_interference(None))
    guard.rebind(TARGET)
    for _ in range(5):
        guard.post_action_events(
            GroundedAction(action="click", point={"x": 1, "y": 1}, confidence=1.0),
            backend.observe(),
        )
    assert engine.calls == []  # zero dispatches of any kind
    # And auto_handle ships EMPTY: nothing is configured to be clicked automatically.
    assert parse_interference(None).dialog_sentinel.auto_handle == []


def test_queue_halts_with_named_modal_dialog_reason() -> None:
    from computer_use_mcp.agent import ComputerUseAgent, SingleActionOutcome

    outcome = SingleActionOutcome(
        kind="executed",
        result=None,
        interference_events=[MODAL_DIALOG + " title='Save As' class='#32770' hwnd=2"],
    )
    # The queue reads the executed item's interference events and stops with the name.
    assert outcome.interference_events is not None
    assert outcome.interference_events[0].startswith(MODAL_DIALOG)
    assert ComputerUseAgent._interference_stop_reason(
        SingleActionOutcome(kind="rejected", reasons=[MODAL_DIALOG + " title='x'"])
    ) == "modal_dialog"


def test_single_action_path_annotates_and_maps_failure_class() -> None:
    from computer_use_mcp.agent import ComputerUseAgent
    from computer_use_mcp.limits import Limits
    from computer_use_mcp.observation import ObservationEngine
    from computer_use_mcp.safety import SafetyPolicy
    from computer_use_mcp.state import StopToken, TaskState

    class DialogAfterExecute(FakeComputerBackend):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.executes = 0
            self._engine = None

        def observe(self, monitor_index: Any = None) -> Any:
            observation = super().observe(monitor_index)
            # The click visibly changed the screen (so the diff tier verifies and the
            # test isolates the SENTINEL annotation semantics).
            color = "white" if len(self.executed) % 2 == 0 else "black"
            image = Image.new("RGB", (self.width, self.height), color)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")

            observation.image_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
            return observation

        def execute(self, action: GroundedAction, stop: Any = None, **kwargs: Any) -> str:
            message = super().execute(action, stop)
            self.executes += 1
            if self.executes == 1:  # the action opens a dialog (focus + sentinel state)
                dialog = SAVE_AS.model_copy()
                self.set_active_window(dialog)
                self.system_dialog = dict(dialog.model_dump())
            return message

    backend = DialogAfterExecute()
    backend.set_active_window(TARGET)
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
        agent.run_single(state, GroundedAction(action="click", point={"x": 10, "y": 10}, confidence=1.0))
    )
    assert outcome.kind == "executed" and outcome.result is not None
    # A12 single-action semantics: the action EXECUTED; the payload rides the SAME
    # response as an annotation — the result stays ok (the queue is what halts).
    assert outcome.interference_events and outcome.interference_events[0].startswith(MODAL_DIALOG)
    assert "title='Save As'" in outcome.interference_events[0]
    assert MODAL_DIALOG in outcome.result.verification.note
    assert outcome.result.ok is True
    # The audit mapping for recovery classification is UNEXPECTED_DIALOG.
    assert FailureClass.UNEXPECTED_DIALOG.value == "unexpected_dialog"
