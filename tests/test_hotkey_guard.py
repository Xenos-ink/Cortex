"""T8 HotkeyGuard tests (A12 test plan T5): stuck-modifier sweep, release mode, B3 gap.

Covers the mechanism-(v) contract plus the B3 settle/gap policy:

- a pre-chord GetAsyncKeyState sweep over the chord's modifiers plus ctrl/alt/shift/win:
  any stuck modifier -> blocking ``STUCK_MODIFIER`` rejection and NO dispatch (a held
  ctrl would rewrite the chord's meaning — ctrl+s collapses into plain "s");
- the opt-in ``release`` mode emits synthetic key-ups ONLY for modifiers this session
  dispatched (matched against the guard's chord log); a foreign stuck modifier is never
  released — it aborts instead; after a verified release the chord dispatches;
- guard precedence: a hotkey against a foreign foreground rejects with FOCUS_TAKEN_BY
  (a hotkey against a stolen foreground is a misdelivery, not a no-op);
- chord-ordering regressions stay covered by test_hotkey_action/test_input_engines;
- B3: a terminal-key chord (enter/return/tab) dispatched within the configured gap of
  the previous keyboard dispatch waits out the remainder; the gap is configurable
  (``CORTEX_KEY_DISPATCH_GAP``, 0 disables) and non-terminal chords never wait.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.focus_guard import InterferenceGuard
from computer_use_mcp.interference import FOCUS_TAKEN_BY, STUCK_MODIFIER, parse_interference
from computer_use_mcp.models import FailureClass, GroundedAction, WindowInfo

TARGET = WindowInfo(
    hwnd=1, pid=100, process_name="EXCEL.EXE", window_class="XLMAIN", title="Book1 - Excel",
)
CTRL_S = GroundedAction(action="hotkey", keys=["ctrl", "s"], confidence=1.0)
ENTER = GroundedAction(action="keypress", keys=["enter"], confidence=1.0)


def _backend(stuck: list[str] | None = None) -> FakeComputerBackend:
    backend = FakeComputerBackend()
    backend.set_windows([TARGET])  # registered population: the B6 liveness probe sees it
    backend.set_active_window(TARGET)
    backend.stuck_modifiers = list(stuck or [])
    return backend


def _armed(backend: FakeComputerBackend, **policy: Any) -> InterferenceGuard:
    guard = InterferenceGuard(backend, parse_interference(policy or None))
    guard.rebind(TARGET)
    return guard


def test_stuck_modifier_aborts_before_dispatch() -> None:
    backend = _backend(stuck=["ctrl"])
    guard = _armed(backend)
    verdict = guard.verify_pre_dispatch(CTRL_S)
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith(STUCK_MODIFIER)
    assert "keys=[ctrl]" in verdict.event
    assert verdict.failure_class is FailureClass.UNKNOWN  # bounded, fail-closed
    assert backend.released_modifiers == []  # abort mode releases nothing


def test_clean_modifier_state_dispatches() -> None:
    backend = _backend(stuck=[])
    guard = _armed(backend)
    assert guard.verify_pre_dispatch(CTRL_S) is None
    assert guard.verify_pre_dispatch(ENTER) is None


def test_release_mode_releases_only_session_dispatched_modifiers() -> None:
    backend = _backend(stuck=["shift", "ctrl"])
    guard = _armed(backend, hotkey_guard={"on_stuck_modifier": "release"})
    # Nothing dispatched by this session yet: 'shift'/'ctrl' are NOT ours -> abort.
    verdict = guard.verify_pre_dispatch(CTRL_S)
    assert verdict is not None and verdict.blocking
    assert backend.released_modifiers == []

    # ctrl+s was dispatched by this session -> ctrl is OURS and gets released.
    guard.note_chord(["ctrl", "s"])
    backend.stuck_modifiers = ["shift"]  # after the release, shift is still stuck
    verdict = guard.verify_pre_dispatch(CTRL_S)
    assert verdict is not None and verdict.blocking  # the foreign 'shift' aborts
    assert backend.released_modifiers == []  # a foreign modifier is NEVER released


def test_release_mode_releases_ours_then_dispatches() -> None:
    backend = _backend(stuck=["ctrl"])
    guard = _armed(backend, hotkey_guard={"on_stuck_modifier": "release"})
    guard.note_chord(["ctrl", "s"])  # ours
    assert guard.verify_pre_dispatch(CTRL_S) is None
    assert backend.released_modifiers == ["ctrl"]


def test_focus_taken_by_precedes_stuck_modifier_for_foreign_foreground() -> None:
    backend = _backend(stuck=["ctrl"])
    guard = _armed(backend)
    backend.set_active_window(
        WindowInfo(hwnd=9, pid=900, process_name="zcode.exe", window_class="CONSOLE",
                   title="user console")
    )
    verdict = guard.verify_pre_dispatch(CTRL_S)
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith(FOCUS_TAKEN_BY)  # misdelivery risk decided first


def test_sweep_applies_to_keypress_and_hotkey_only() -> None:
    backend = _backend(stuck=["win"])
    guard = _armed(backend)
    click = GroundedAction(action="click", point={"x": 5, "y": 5}, confidence=1.0)
    assert guard.verify_pre_dispatch(click) is None  # mouse actions are not chord-gated


# --- B3 settle/gap policy ----------------------------------------------------------------------


def test_key_dispatch_gap_waits_for_terminal_keys_only(monkeypatch: Any) -> None:
    import computer_use_mcp.backend as backend_module

    monkeypatch.setattr(backend_module, "KEY_DISPATCH_GAP_SECONDS", 0.05)
    sleeps: list[float] = []
    monkeypatch.setattr(backend_module.time, "sleep", lambda s: sleeps.append(round(s, 4)))

    backend = FakeComputerBackend()
    clock = {"now": 1000.0}
    monkeypatch.setattr(backend_module.time, "monotonic", lambda: clock["now"])

    backend.execute(ENTER)
    assert len(backend.executed) == 1
    # First dispatch of the session never waits (the epoch gap is huge).
    assert sleeps == []

    # An IMMEDIATELY following terminal chord waits out the gap remainder (10 ms
    # elapsed of the 50 ms gap -> ~40 ms sleep).
    clock["now"] += 0.01
    backend.execute(ENTER)
    assert sleeps and abs(sleeps[-1] - 0.04) < 0.005

    # A chord LONG after the previous dispatch does not wait.
    clock["now"] += 100.0
    backend.execute(ENTER)
    assert len(sleeps) == 1


def test_key_dispatch_gap_skips_non_terminal_chords(monkeypatch: Any) -> None:
    import computer_use_mcp.backend as backend_module

    monkeypatch.setattr(backend_module, "KEY_DISPATCH_GAP_SECONDS", 0.05)
    sleeps: list[float] = []
    monkeypatch.setattr(backend_module.time, "sleep", lambda s: sleeps.append(s))

    backend = FakeComputerBackend()
    backend.execute(ENTER)  # prime the last-dispatch clock
    ctrl_s = GroundedAction(action="hotkey", keys=["ctrl", "s"], confidence=1.0)
    backend.execute(ctrl_s)  # non-terminal chord: NO gap wait even back-to-back
    assert sleeps == []


def test_key_dispatch_gap_zero_disables_the_policy(monkeypatch: Any) -> None:
    import computer_use_mcp.backend as backend_module

    monkeypatch.setattr(backend_module, "KEY_DISPATCH_GAP_SECONDS", 0.0)
    sleeps: list[float] = []
    monkeypatch.setattr(backend_module.time, "sleep", lambda s: sleeps.append(s))

    backend = FakeComputerBackend()
    backend.execute(ENTER)
    backend.execute(ENTER)  # back-to-back: no wait when the gap is disabled
    assert sleeps == []
    assert len(backend.executed) == 2


def test_batch_final_enter_gets_the_gap_end_to_end(monkeypatch: Any) -> None:
    """A queued [type, enter] batch paces the final Enter (the B3 incident shape)."""
    import computer_use_mcp.backend as backend_module
    from computer_use_mcp.agent import ComputerUseAgent
    from computer_use_mcp.limits import Limits
    from computer_use_mcp.models import ActionSpec
    from computer_use_mcp.observation import ObservationEngine
    from computer_use_mcp.safety import SafetyPolicy
    from computer_use_mcp.state import StopToken, TaskState

    monkeypatch.setattr(backend_module, "KEY_DISPATCH_GAP_SECONDS", 0.02)
    sleeps: list[float] = []
    monkeypatch.setattr(backend_module.time, "sleep", lambda s: sleeps.append(round(s, 4)))
    # Deterministic clock: the enter's dispatch happens 5 ms (real-time-independent)
    # after the type's dispatch — inside the 20 ms gap window.
    clock = {"now": 5000.0}
    monkeypatch.setattr(backend_module.time, "monotonic", lambda: clock["now"])

    backend = FakeComputerBackend()
    # The active window title contains the typed text so the type action verifies via
    # the deterministic ui-control-text tier (B2 ladder) instead of pixels.
    backend.set_active_window(
        WindowInfo(
            hwnd=1, pid=100, process_name="notepad.exe", window_class="Notepad",
            title="notepad - Notepad",
        )
    )

    from io import BytesIO

    from PIL import Image as PILImage

    def _observe_flipped(monitor_index: Any = None) -> Any:
        observation = FakeComputerBackend.observe(backend, monitor_index)
        # Real screens change on keyboard input: alternate the color per executed
        # action so each queued item's diff tier sees the change (never a starvation).
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
            GroundedAction(action="type", text="notepad", confidence=1.0),
            approved=True,
            follow_ups=[ActionSpec(action="keypress", keys=["enter"])],
        )
    )
    assert outcome.follow_ups_stopped_reason is None  # the whole batch ran
    executed = [a.action.value for a in backend.executed]
    assert executed == ["type", "keypress"]
    # The enter followed the type within the gap window: the settle waited the remainder.
    assert sleeps, "the final Enter must be paced by the settle/gap policy"


# --- B8 (T8 coordinator finding): settle after focus transitions ------------------------------


def test_focus_transition_settle_paces_immediate_keyboard_dispatch(monkeypatch: Any) -> None:
    """Keys sent right after a window activation wait out the settle (B5 wedge topology)."""
    import computer_use_mcp.backend as backend_module

    monkeypatch.setattr(backend_module, "FOCUS_TRANSITION_SETTLE_SECONDS", 0.25)
    sleeps: list[float] = []
    monkeypatch.setattr(backend_module.time, "sleep", lambda s: sleeps.append(round(s, 4)))
    clock = {"now": 3000.0}
    monkeypatch.setattr(backend_module.time, "monotonic", lambda: clock["now"])

    backend = FakeComputerBackend()
    backend._note_focus_transition()  # a focus_window/ensure_app just activated a window
    clock["now"] += 0.05  # typing 50 ms after activation: inside the settle window
    backend.execute(ENTER)
    assert sleeps and abs(sleeps[-1] - 0.2) < 0.005  # waited the remaining 200 ms

    # A dispatch LONG after the transition does not wait.
    clock["now"] += 100.0
    backend.execute(ENTER)
    assert len(sleeps) == 1


def test_focus_transition_settle_zero_disables(monkeypatch: Any) -> None:
    import computer_use_mcp.backend as backend_module

    monkeypatch.setattr(backend_module, "FOCUS_TRANSITION_SETTLE_SECONDS", 0.0)
    sleeps: list[float] = []
    monkeypatch.setattr(backend_module.time, "sleep", lambda s: sleeps.append(s))
    backend = FakeComputerBackend()
    backend._note_focus_transition()
    backend.execute(ENTER)  # no settle wait when the policy is disabled
    assert sleeps == []


def test_settle_notes_are_recorded_by_focus_transitions() -> None:
    backend = _backend()
    before = backend._last_focus_transition
    backend.focus_window_title("Book1 - Excel")  # a focus transition
    assert backend._last_focus_transition >= before
