"""REM-D bootstrap exemption tests (master-mission Phase 2, ORVEX-CORTEX-055).

Fix under test (live-test root cause, Commander-proven via stdio probe + Kimi Code
live run): a FRESH session with ``allowed_processes=["mspaint.exe","notepad.exe"]``
cannot bootstrap takeover because ``ensure_app`` on the allowlisted target is
rejected by the validator's FOREGROUND process gate — the gate checks the CURRENT
foreground process (the driver terminal), which is by definition not yet the
target on a fresh session (chicken-and-egg). REM-B's message taught "use
ensure_app on an allowlisted target" but ensure_app itself was blocked by the
same gate.

The fix (validator.py): when the action is ``ensure_app`` AND its TARGET process
matches the configured ``allowed_processes`` (same matcher semantics as the
launch gate — case-insensitive, ``.exe``-tolerant, doc-token prefix stripped),
the ACTIVE-process allowlist check is SKIPPED for that action. The target-side
gate in ``agent.py _ensure_app_allow_launch`` (launch allowed only for
allowlisted targets) remains the real security gate. Every other action keeps
the foreground gate unchanged — fail-closed.

Pins:
- pin 1: fresh session, foreign foreground, allowlisted ensure_app target ->
  validation PASSES and the launch path runs end-to-end (NO_INSTANCE -> launched).
- pin 2: same setup, ensure_app on a NON-allowlisted target -> STILL REJECTED
  (``process_not_allowed``; no widening).
- pin 3: same setup, a click with foreign foreground -> STILL REJECTED (the
  foreground gate is unchanged for every non-ensure_app action).
- pin 4: end-to-end agent level — the logged fresh-session ensure_app now
  executes (reattached-or-launched), ``ok=True``.
- pin 5: ``allowed_processes=None`` + ensure_app -> behavior UNCHANGED (the
  foreground gate never applies without a configured allowlist).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.interference import NO_INSTANCE, REATTACHED
from computer_use_mcp.models import GroundedAction, Observation, WindowInfo
from computer_use_mcp.agent import ComputerUseAgent
from computer_use_mcp.limits import Limits
from computer_use_mcp.observation import ObservationEngine
from computer_use_mcp.safety import SafetyPolicy
from computer_use_mcp.state import StopToken, TaskState
from computer_use_mcp.validator import GroundingValidator


def _foreign_foreground_observation() -> Observation:
    """The fresh-session screen: the DRIVER TERMINAL is in the foreground
    (the logged WindowsTerminal.exe/ZCode.exe), not any allowlisted app."""
    from computer_use_mcp.backend import FakeComputerBackend

    backend = FakeComputerBackend()
    backend.set_active_window(
        WindowInfo(
            hwnd=55, pid=55, process_name="windowsterminal.exe", title="driver console"
        )
    )
    return backend.observe()


def _validate_allowlisted_ensure_app(allowed: list[str], target: str) -> Any:
    """Validate an ensure_app (foreign foreground, fresh session) against ``allowed``."""
    source = _foreign_foreground_observation()
    action = GroundedAction(action="ensure_app", target=target, confidence=1.0)
    return GroundingValidator().validate(action, source, None, allowed_processes=allowed)


# --- pin 1: allowlisted ensure_app bootstraps from a foreign foreground ----------------------


def test_pin1_ensure_app_allowlisted_target_passes_foreground_gate() -> None:
    """The chicken-and-egg fix: on a FRESH session (foreign foreground), an
    ensure_app whose TARGET is allowlisted must NOT be rejected by the
    ACTIVE-process gate — the validator outcome is valid."""
    outcome = _validate_allowlisted_ensure_app(["mspaint.exe"], "mspaint.exe")
    assert outcome.valid is True
    assert "process_not_allowed" not in outcome.codes


def test_pin1_exe_tolerance_and_doc_token_forms_pass() -> None:
    """Matcher parity with the launch gate: bare process name and doc-token
    form ('mspaint|untitled') both match the allowlisted 'mspaint.exe' entry."""
    for target in ("mspaint", "mspaint.exe", "MSPAINT.EXE", "mspaint|untitled"):
        outcome = _validate_allowlisted_ensure_app(["mspaint.exe"], target)
        assert outcome.valid is True, (target, outcome.reasons)


def test_pin1_allowlisted_target_launch_reaches_backend_end_to_end() -> None:
    """Full pipeline: allow_launch=True reaches the backend and a NO_INSTANCE
    probe launches the allowlisted target (the spawn happens server-side)."""
    backend = FakeComputerBackend()  # no app_windows: nothing to attach to
    backend.set_active_window(
        WindowInfo(
            hwnd=55, pid=55, process_name="windowsterminal.exe", title="driver console"
        )
    )
    agent = ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        allowed_processes=["mspaint.exe"],
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
    assert outcome.result.ok is True
    assert backend.launched_processes == ["mspaint.exe"]  # allow_launch reached the backend
    assert "launched=mspaint.exe" in outcome.result.message  # NO_INSTANCE -> launched


# --- pin 2: non-allowlisted ensure_app target stays rejected --------------------------------


def test_pin2_ensure_app_non_allowlisted_target_still_rejected() -> None:
    """No widening: an ensure_app on a target OUTSIDE the allowlist is still
    rejected by the foreground gate with ``process_not_allowed`` (fail-closed)."""
    outcome = _validate_allowlisted_ensure_app(["mspaint.exe"], "someotherapp.exe")
    assert outcome.valid is False
    assert "process_not_allowed" in outcome.codes


# --- pin 3: the foreground gate is unchanged for every other action --------------------------


def test_pin3_click_with_foreign_foreground_still_rejected() -> None:
    """Non-ensure_app actions keep the foreground gate verbatim: a click while a
    foreign process owns the foreground is still rejected with
    ``process_not_allowed``."""
    source = _foreign_foreground_observation()
    action = GroundedAction(
        action="click",
        point={"x": 10, "y": 10},
        confidence=0.9,
        source_observation_id=source.observation_id,
    )
    outcome = GroundingValidator().validate(
        action, source, None, allowed_processes=["mspaint.exe"]
    )
    assert outcome.valid is False
    assert "process_not_allowed" in outcome.codes


# --- pin 4: end-to-end agent level (the logged fresh-session run) ----------------------------


def test_pin4_fresh_session_ensure_app_reattaches_end_to_end() -> None:
    """End-to-end with an EXISTING instance: the fresh-session ensure_app on the
    allowlisted target executes (REATTACHED), ok=True — exactly the live-test flow
    that died in the FailedLog."""
    from computer_use_mcp.backend import AppWindowCandidate, doc_token_from_title, is_unsaved_candidate_title

    backend = FakeComputerBackend()
    paint = WindowInfo(
        hwnd=9, pid=99, process_name="mspaint.exe", title="Untitled - Paint"
    )
    backend.app_windows = [
        AppWindowCandidate(
            window=paint,
            doc_token=doc_token_from_title(paint.title),
            unsaved_candidate=is_unsaved_candidate_title(paint.title),
        )
    ]
    # The top-level window list is what focus resolves against (same as the
    # _backend_with helper in test_attach_or_launch.py): include the Paint window.
    backend.windows = [paint]
    backend.set_active_window(
        WindowInfo(
            hwnd=55, pid=55, process_name="windowsterminal.exe", title="driver console"
        )
    )
    agent = ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        allowed_processes=["mspaint.exe"],
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
    assert outcome.result.ok is True
    assert outcome.result.message.startswith((REATTACHED, NO_INSTANCE))
    assert backend.launched_processes == []  # an instance existed: reattach, no spawn


# --- pin 5: no allowlist configured -> behavior unchanged ------------------------------------


def test_pin5_no_allowlist_ensure_app_unchanged() -> None:
    """``allowed_processes=None``: the foreground gate never applies, so the fix
    changes nothing — ensure_app executes exactly as before the exemption."""
    backend = FakeComputerBackend()  # foreign foreground, no allowlist
    backend.set_active_window(
        WindowInfo(
            hwnd=55, pid=55, process_name="windowsterminal.exe", title="driver console"
        )
    )
    agent = ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
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
    assert outcome.result.ok is True
    assert outcome.result.message.startswith(NO_INSTANCE)
    assert "launched=mspaint.exe" in outcome.result.message
