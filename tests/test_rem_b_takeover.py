"""REM-B takeover + verification/queue tests (master-mission Phase 2, ORVEX-CORTEX-055).

Fixes under test (log-forensics.md H2a/H2b/H2c, confirmed root causes):

- Fix 1 (H2a TAKEOVER): ``ensure_app`` CAN launch server-side by default. The
  ``attach_or_launch.launch`` default flips ``driver`` -> ``server`` (agent gate still
  requires the TARGET process to be allowlisted when an allowlist is configured, and
  ``launch="driver"`` stays selectable both explicitly and via the
  ``CORTEX_ATTACH_OR_LAUNCH=driver`` env knob).
- Fix 2 (H2a messaging): a ``process_not_allowed`` rejection names the ACTIVE process,
  states that the action TARGET differs, and teaches the concrete remedy (bring/focus
  an allowlisted app — e.g. ensure_app on an allowlisted target — or extend
  allowed_processes at session start). The gate's DECISION stays fail-closed.
- Fix 3 (H2b QUEUE-STOP): the follow-up queue CONTINUES past an item whose verification
  outcome is UNCERTAIN (no expectation stated / non-visual action judged unverifiable);
  named stops, safety rejections, approval requirements, and digest surprises still stop
  the queue. W-2/057 UPDATE: an EXECUTED item's DEFINITIVE "failed" verification no
  longer stops the queue by default either (the honest failed verdict rides the per-item
  entry); CORTEX_QUEUE_STRICT_VERIFY=1 restores the stop-on-failed behavior, and that
  restored stop is pinned here. Per-item results keep their honest per-item verdicts.
- Fix 4 (H2c FALSE NEGATIVES): a click with an ``expected_effect`` gets a deterministic
  verification tier (UIA focused-element change, active-window title/process change,
  observation digest change) BEFORE the pixel-diff failure. A deterministic change
  signal verifies the click; with no signal anywhere the W-1/057 contract degrades the
  flagged click's verdict to ``uncertain`` (never a false success, never a false
  failure).
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from computer_use_mcp.interference import NO_INSTANCE, parse_interference
from computer_use_mcp.models import (
    ActionSpec,
    GroundedAction,
    MonitorInfo,
    Observation,
    WindowInfo,
)
from computer_use_mcp.agent import ComputerUseAgent
from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.limits import Limits
from computer_use_mcp.observation import ObservationEngine
from computer_use_mcp.safety import SafetyPolicy
from computer_use_mcp.state import StopToken, TaskState
from computer_use_mcp.validator import GroundingValidator
from computer_use_mcp.verification import VerificationIntent, VerificationKind


def _png(color: str = "white", width: int = 64, height: int = 48) -> str:
    image = Image.new("RGB", (width, height), color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def _window(
    hwnd: int = 42,
    pid: int = 7,
    process_name: str | None = "notepad.exe",
    title: str = "Untitled - Notepad",
    bounds: tuple[int, int, int, int] | None = (0, 0, 400, 300),
) -> WindowInfo:
    return WindowInfo(hwnd=hwnd, pid=pid, process_name=process_name, title=title, bounds=bounds)


def _observation(
    color: str = "white",
    width: int = 100,
    height: int = 80,
    *,
    window: WindowInfo | None = None,
    active_window: str | None = None,
    ui_elements: list[Any] | None = None,
) -> Observation:
    return Observation(
        image_base64=_png(color, width, height),
        width=width,
        height=height,
        active_window=active_window,
        active_window_info=window,
        ui_elements=ui_elements,
    )


# --- Fix 1: ensure_app can launch by default -----------------------------------------------


def _agent_for_launch_gate(
    *,
    interference: Any = None,
    allowed_processes: list[str] | None = None,
) -> ComputerUseAgent:
    return ComputerUseAgent(
        FakeComputerBackend(),
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        limits=Limits().validate(),
        allowed_processes=allowed_processes,
        interference=interference,
    )


def test_fix1_default_policy_allows_launch_for_allowlisted_target() -> None:
    """H2a: the DEFAULT policy (no interference config) lets ensure_app LAUNCH a
    process whose target is allowlisted — Cortex can bootstrap control itself."""
    agent = _agent_for_launch_gate(allowed_processes=["mspaint.exe"])
    action = GroundedAction(action="ensure_app", target="mspaint.exe", confidence=1.0)
    assert agent._ensure_app_allow_launch(action) is True


def test_fix1_default_policy_launches_without_any_allowlist() -> None:
    agent = _agent_for_launch_gate()
    action = GroundedAction(action="ensure_app", target="mspaint.exe", confidence=1.0)
    assert agent._ensure_app_allow_launch(action) is True


def test_fix1_target_outside_allowlist_still_never_launches() -> None:
    """P0-G preserved: the allowlist gate on the TARGET process stays fail-closed."""
    agent = _agent_for_launch_gate(allowed_processes=["mspaint.exe"])
    action = GroundedAction(action="ensure_app", target="chrome.exe", confidence=1.0)
    assert agent._ensure_app_allow_launch(action) is False


def test_fix1_explicit_driver_policy_restores_no_launch() -> None:
    """An EXPLICIT launch="driver" policy keeps the old never-launch behavior."""
    agent = _agent_for_launch_gate(
        interference=parse_interference({"attach_or_launch": {"launch": "driver"}})
    )
    action = GroundedAction(action="ensure_app", target="mspaint.exe", confidence=1.0)
    assert agent._ensure_app_allow_launch(action) is False


def test_fix1_env_knob_restores_driver_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CORTEX_ATTACH_OR_LAUNCH=driver restores the pre-REM-B default (fail-safe:
    bogus values fall back to the new server default)."""
    monkeypatch.setenv("CORTEX_ATTACH_OR_LAUNCH", "driver")
    agent = _agent_for_launch_gate(allowed_processes=["mspaint.exe"])
    action = GroundedAction(action="ensure_app", target="mspaint.exe", confidence=1.0)
    assert agent._ensure_app_allow_launch(action) is False


def test_fix1_env_knob_fail_safe_on_bogus_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for bogus in ("", "  ", "nonsense", "SERVER", "server ", "0", "false"):
        monkeypatch.setenv("CORTEX_ATTACH_OR_LAUNCH", bogus)
        agent = _agent_for_launch_gate(allowed_processes=["mspaint.exe"])
        action = GroundedAction(action="ensure_app", target="mspaint.exe", confidence=1.0)
        # Anything unrecognized fails safe to the NEW (server) default: launch allowed.
        assert agent._ensure_app_allow_launch(action) is True, bogus


def test_fix1_no_instance_with_allowlisted_target_launches_end_to_end() -> None:
    """The logged failure end-to-end: no window exists, the target IS allowlisted,
    the default policy lets the backend spawn it (NO_INSTANCE payload gains
    ``launched=``) instead of leaving the driver to launch out-of-band."""
    from computer_use_mcp.backend import FakeComputerBackend

    backend = FakeComputerBackend()  # no app_windows: nothing to attach to
    # A foreign foreground (the logged ZCode console) is fine now: the process
    # allowlist gate applies to ensure_app's TARGET, and the target is allowlisted.
    backend.set_active_window(
        WindowInfo(hwnd=55, pid=55, process_name="zcode.exe", title="driver console")
    )
    agent = ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        allowed_processes=["mspaint.exe", "zcode.exe"],
        limits=Limits(max_actions=5, max_task_seconds=60.0).validate(),
    )
    agent.observation = ObservationEngine(backend)
    state = SimpleNamespace(
        dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
        max_steps=5, step_count=0, require_approval=False, max_retries_per_action=1,
    )
    outcome = asyncio.run(
        agent.run_single(
            state, GroundedAction(action="ensure_app", target="mspaint.exe", confidence=1.0)
        )
    )
    assert outcome.kind == "executed" and outcome.result is not None
    assert backend.launched_processes == ["mspaint.exe"]  # the spawn happened
    assert "launched=mspaint.exe" in outcome.result.message


# --- Fix 2: the process_not_allowed rejection teaches the remedy ---------------------------


def _rejected_for_active_process(
    allowed: list[str], active: WindowInfo | None = None
) -> Any:
    source = _observation(window=active or _window(process_name="zcode.exe", title="driver console"))
    action = GroundedAction(
        action="click",
        point={"x": 10, "y": 10},
        confidence=0.9,
        source_observation_id=source.observation_id,
    )
    return GroundingValidator().validate(action, source, None, allowed_processes=allowed)


def test_fix2_rejection_names_active_process_and_remedy() -> None:
    outcome = _rejected_for_active_process(["mspaint.exe"])
    assert outcome.valid is False
    assert "process_not_allowed" in outcome.codes
    reason = outcome.reasons[0]
    # Names the ACTIVE process (the logged message stopped here)...
    assert "zcode.exe" in reason
    # ...states that the action TARGET differs (the gate checks the foreground, not
    # the intended target)...
    assert "target" in reason.casefold()
    # ...and teaches the concrete remedy: bring/focus an allowlisted app (ensure_app
    # can launch one now) or extend allowed_processes at session start.
    assert "ensure_app" in reason
    assert "allowed_processes" in reason


def test_fix2_rejection_decision_stays_fail_closed() -> None:
    """Message-only change: the gate still rejects exactly the same inputs."""
    outcome = _rejected_for_active_process(["mspaint.exe"])
    assert outcome.valid is False  # decision unchanged
    allowed = _rejected_for_active_process(["ZCode.exe"])  # matching name still passes
    assert allowed.valid is True


def test_fix2_message_mentions_active_process_role() -> None:
    outcome = _rejected_for_active_process(["mspaint.exe", "explorer.exe"])
    reason = outcome.reasons[0]
    # The message must make clear the check applies to the FOREGROUND process, not
    # to whatever the action intends to act on.
    assert "foreground" in reason.casefold() or "active" in reason.casefold()


# --- Fix 3: the queue continues past an UNCERTAIN verification ------------------------------


class FocusTierBackend(FakeComputerBackend):
    """A backend whose focus state changes on the first click, invisibly to pixels.

    The screenshot never changes color (the pixel-diff alone would find nothing),
    but the focused UIA control moves — the H2c Paint hex-field signature.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.executes = 0
        self._focus_before = {
            "control_type": "Edit", "name": "", "automation_id": "", "focused": True
        }
        self._focus_after = {
            "control_type": "Edit", "name": "Hex color input", "automation_id": "hexInput",
            "focused": True,
        }

    def observe(self, monitor_index: Any = None) -> Any:
        observation = super().observe(monitor_index)
        observation.ui_elements = [
            dict(self._focus_after if self.executes >= 1 else self._focus_before)
        ]
        return observation

    def execute(self, action: GroundedAction, stop: Any = None, **kwargs: Any) -> str:
        message = super().execute(action, stop)
        self.executes += 1
        return message


class UncertainFirstActionBackend(FakeComputerBackend):
    """Item 0 (a hotkey with NO expected_effect) verifies UNCERTAIN: pixels stay
    IDENTICAL between the before/after captures (the logged Paint behavior — a
    ctrl+a in a text field moves no measurable pixels). Item 1 (a click) then
    flips the screen and verifies normally."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.executes = 0

    def observe(self, monitor_index: Any = None) -> Any:
        observation = super().observe(monitor_index)
        if self.executes >= 2:  # only AFTER item 1's click does the screen change
            observation.image_base64 = _png("black", observation.width, observation.height)
        return observation

    def execute(self, action: GroundedAction, stop: Any = None, **kwargs: Any) -> str:
        message = super().execute(action, stop)
        self.executes += 1
        return message


def _queue_agent(backend: Any, *, allowed: list[str] | None = None) -> ComputerUseAgent:
    agent = ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        allowed_processes=allowed,
        limits=Limits(max_actions=10, max_task_seconds=60.0).validate(),
    )
    agent.observation = ObservationEngine(backend)
    return agent


def _state() -> Any:
    return SimpleNamespace(
        dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
        max_steps=10, step_count=0, require_approval=False, max_retries_per_action=1,
    )


def test_fix3_queue_continues_past_uncertain_verification() -> None:
    """H2b: the logged ctrl+a kill — an UNCERTAIN item must not flush the rest of
    the queue. Item 0 is a no-expectation hotkey (uncertain on identical pixels);
    item 1 (type) is ALSO uncertain; item 2's keypress then flips the screen. All
    three must run."""
    backend = UncertainFirstActionBackend()
    agent = _queue_agent(backend)
    outcome = asyncio.run(
        agent.run_single(
            _state(),
            GroundedAction(action="hotkey", keys=["ctrl", "a"], confidence=1.0),
            follow_ups=[
                ActionSpec(action="type", text="C8C3B2"),
                ActionSpec(action="keypress", keys=["enter"]),
            ],
        )
    )
    assert outcome.kind == "executed"
    assert outcome.follow_ups_stopped_reason is None  # the queue completed
    entries = outcome.follow_up_results or []
    assert len(entries) == 3
    # Per-item honesty preserved: item 0 records its own uncertain verdict + ok=False.
    assert entries[0]["verification_outcome"] == "uncertain"
    assert entries[0]["ok"] is False
    # The remaining keystrokes DID run (0 of 5 ran in the failed log).
    assert [e["action_type"] for e in entries[1:]] == ["type", "keypress"]
    assert backend.executes == 3


def test_fix3_definitive_failure_still_stops_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop softening is UNCERTAIN-only and (since W-2/057) failed-verdict-only
    under the CORTEX_QUEUE_STRICT_VERIFY=1 escape hatch: an EXECUTED item with a
    DEFINITIVE failed verification stops the queue again (zero bypass on real
    failures). RC-D11 (058) update: a hotkey with a pixel-shaped stated effect can
    no longer produce a DEFINITIVE failed (absent pixels degrade to uncertain), so
    the failing item is a keypress whose launch-prefix effect ("open Calculator")
    promotes the intent to the deterministic window_state tier — the window title
    never appears on the never-changing screen -> definitive failed, the honest
    real-failure evidence class this stop contract is pinned with."""

    class FailDefinitivelyBackend(FakeComputerBackend):
        """The screen NEVER changes and the window title never becomes the expected
        one; the deterministic window_state tier fails definitively (RC-D11 update:
        a pixel-shaped stated effect can no longer fail definitively — absent
        pixels degrade to uncertain — so the deterministic tier carries the pin)."""

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(
                active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="Main"),
                **kwargs,
            )
            self.executes = 0

        def execute(self, action: GroundedAction, stop: Any = None, **kwargs: Any) -> str:
            message = super().execute(action, stop)
            self.executes += 1
            return message

    monkeypatch.setenv("CORTEX_QUEUE_STRICT_VERIFY", "1")
    backend = FailDefinitivelyBackend()
    agent = _queue_agent(backend)
    outcome = asyncio.run(
        agent.run_single(
            _state(),
            GroundedAction(
                action="keypress",
                keys=["enter"],
                confidence=1.0,
                expected_effect="open Calculator",
            ),
            follow_ups=[
                ActionSpec(action="type", text="C8C3B2"),
                ActionSpec(action="keypress", keys=["enter"]),
            ],
        )
    )
    assert outcome.follow_ups_stopped_reason == "verification_failed"
    assert (outcome.follow_up_results or [])[0]["verification_outcome"] == "failed"
    assert backend.executes == 1  # the follow-ups were flushed


def test_fix3_default_queue_continues_past_executed_failed_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W-2 (057) default contract: an EXECUTED item whose verification outcome is
    definitively ``failed`` no longer flushes the batch — the honest failed verdict
    rides the per-item entry and the remaining items still run (the strict env knob
    is left at its default)."""
    monkeypatch.delenv("CORTEX_QUEUE_STRICT_VERIFY", raising=False)

    class FailDefinitivelyBackend(FakeComputerBackend):
        """The screen NEVER changes and the window title never becomes the expected
        one; the deterministic window_state tier fails definitively (RC-D11 update:
        a pixel-shaped stated effect can no longer fail definitively — absent
        pixels degrade to uncertain — so the deterministic tier carries the pin)."""

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(
                active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="Main"),
                **kwargs,
            )
            self.executes = 0

        def execute(self, action: GroundedAction, stop: Any = None, **kwargs: Any) -> str:
            message = super().execute(action, stop)
            self.executes += 1
            return message

    backend = FailDefinitivelyBackend()
    agent = _queue_agent(backend)
    outcome = asyncio.run(
        agent.run_single(
            _state(),
            GroundedAction(
                action="keypress",
                keys=["enter"],
                confidence=1.0,
                expected_effect="open Calculator",
            ),
            follow_ups=[
                ActionSpec(action="type", text="C8C3B2"),
                ActionSpec(action="keypress", keys=["enter"]),
            ],
        )
    )
    assert outcome.kind == "executed"
    assert outcome.follow_ups_stopped_reason is None  # the batch completed
    entries = outcome.follow_up_results or []
    assert len(entries) == 3
    # Per-item honesty preserved: item 0 records its own DEFINITIVE failed verdict.
    assert entries[0]["verification_outcome"] == "failed"
    assert entries[0]["ok"] is False
    assert [e["action_type"] for e in entries[1:]] == ["type", "keypress"]
    assert backend.executes == 3


# --- Fix 4: deterministic verification tier for focus-type click expectations ---------------


def test_fix4_click_verifies_via_focused_element_change_not_pixels() -> None:
    """H2c: the Paint hex-input click — pixels do not move (mean diff 0), but the
    focused UIA control changed. The deterministic tier verifies the click."""
    before = _observation(
        window=_window(),
        ui_elements=[{"control_type": "Edit", "name": "", "automation_id": "", "focused": True}],
    )
    after = _observation(  # SAME pixels (color unchanged) -> pixel-diff would fail
        window=_window(),
        ui_elements=[
            {"control_type": "Edit", "name": "Hex input", "automation_id": "hex", "focused": True}
        ],
    )
    intent = VerificationIntent(
        kind=VerificationKind.VISUAL_CHANGE.value,
        expected_change=True,
        expected_effect="Hex input focused",
    )
    # The plain pixel-diff strategy defers honestly on unchanged pixels for a
    # stated effect (W-1 flagged degrade; RC-D11/058 extended it to any stated
    # effect — a sub-threshold diff is no-data, not proof of absence)...
    from computer_use_mcp.verification import ScreenshotDiffStrategy

    assert ScreenshotDiffStrategy().verify(intent, before, after).outcome == "uncertain"
    # ...but the full agent pipeline verifies via the deterministic focus tier.
    backend = FocusTierBackend()
    agent = _queue_agent(backend)
    outcome = asyncio.run(
        agent.run_single(
            _state(),
            GroundedAction(
                action="click",
                point={"x": 10, "y": 10},
                confidence=1.0,
                expected_effect="Hex input focused",
            ),
        )
    )
    assert outcome.kind == "executed" and outcome.result is not None
    verification = outcome.result.verification
    assert verification is not None
    assert verification.outcome == "verified"
    assert verification.verification_method == "focus_change"
    assert outcome.result.ok is True


def test_fix4_click_verifies_via_active_window_title_change() -> None:
    """Deterministic signal (b): the active window title changed between before and
    after (dialog open signature) even when pixels are identical."""

    class TitleChangeBackend(FakeComputerBackend):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.executes = 0

        def observe(self, monitor_index: Any = None) -> Any:
            observation = super().observe(monitor_index)
            if self.executes >= 1:
                observation.active_window_info = WindowInfo(
                    hwnd=77, pid=7, process_name="mspaint.exe", title="Edit Colors"
                )
            return observation

        def execute(self, action: GroundedAction, stop: Any = None, **kwargs: Any) -> str:
            message = super().execute(action, stop)
            self.executes += 1
            return message

    backend = TitleChangeBackend()
    agent = _queue_agent(backend)
    outcome = asyncio.run(
        agent.run_single(
            _state(),
            GroundedAction(
                action="click",
                point={"x": 10, "y": 10},
                confidence=1.0,
                expected_effect="Edit colors dialog opens",
            ),
        )
    )
    verification = outcome.result.verification
    assert verification is not None and verification.outcome == "verified"
    assert verification.verification_method == "focus_change"
    # The note NAMES the deterministic signal that fired (the window identity change).
    assert "window" in verification.note.casefold()


def test_fix4_click_keeps_honest_verdict_when_no_signal_anywhere() -> None:
    """Honesty preserved: no focus change, no window change, unchanged pixels ->
    the flagged click's verdict is never a false success. W-1 (057) contract update:
    with the FocusChangeStrategy signals abstaining, the pixel tier's absent-evidence
    case now degrades to ``uncertain`` (ok stays False — uncertain is never success)
    instead of the old definitive false failure; the unflagged legacy ``failed``
    semantics are pinned in tests/test_w1_focus_click_false_failure.py."""
    backend = FakeComputerBackend()  # nothing ever changes
    agent = _queue_agent(backend)
    outcome = asyncio.run(
        agent.run_single(
            _state(),
            GroundedAction(
                action="click",
                point={"x": 10, "y": 10},
                confidence=1.0,
                expected_effect="the dialog opens",
            ),
        )
    )
    verification = outcome.result.verification
    assert verification is not None
    assert verification.outcome == "uncertain"
    assert outcome.result.ok is False


def test_fix4_visual_change_intent_without_effect_is_not_upgraded() -> None:
    """The deterministic tier applies to CLICK intents with a stated expected_effect;
    an unstated expectation (expected_change=None) still yields uncertain, never a
    free verified from the focus tier alone."""
    backend = FocusTierBackend()
    agent = _queue_agent(backend)
    outcome = asyncio.run(
        agent.run_single(
            _state(),
            GroundedAction(action="click", point={"x": 10, "y": 10}, confidence=1.0),
        )
    )
    verification = outcome.result.verification
    assert verification is not None and verification.outcome == "uncertain"
