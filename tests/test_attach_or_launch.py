"""T8 AttachOrLaunch tests (A12 test plan T2): ``ensure_app`` reattach-before-launch.

Covers the mechanism-(ii) contract:

- identity match -> REATTACHED (focused via the existing verified primitive, guard
  re-bound, NO launch, NO window closed);
- doc-token match preferred over a bare process match;
- unsaved-candidate windows with no identity match -> AMBIGUOUS_INSTANCE payload
  (discovery only — nothing focused, closed, or launched);
- no match -> NO_INSTANCE with launch=driver (the DEFAULT policy never spawns);
- server-side launch requires BOTH the ``launch="server"`` policy AND the allowlist
  gate on the agent;
- doc-token/unsaved title heuristics are generic (no per-app tables in code);
- the controller turns a REATTACHED ensure_app into a verified result and a
  NO_INSTANCE/AMBIGUOUS_INSTANCE probe into an evidence-carrying result.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from computer_use_mcp.backend import (
    FakeComputerBackend,
    doc_token_from_title,
    is_unsaved_candidate_title,
)
from computer_use_mcp.interference import (
    AMBIGUOUS_INSTANCE,
    NO_INSTANCE,
    REATTACHED,
    parse_interference,
)
from computer_use_mcp.models import GroundedAction, WindowInfo

EXCEL = WindowInfo(
    hwnd=1, pid=100, process_name="EXCEL.EXE", window_class="XLMAIN", title="Book1 - Excel",
)
DOC = WindowInfo(
    hwnd=2, pid=100, process_name="EXCEL.EXE", window_class="XLMAIN", title="t1_source.xlsx - Excel",
)
STALE_RESTORE = WindowInfo(
    hwnd=3, pid=100, process_name="EXCEL.EXE", window_class="XLMAIN",
    title="t4_out_summary.xlsx1.xlsx - Excel",
)


def _backend_with(*windows: WindowInfo) -> FakeComputerBackend:
    backend = FakeComputerBackend()
    backend.windows = list(windows)
    from computer_use_mcp.backend import AppWindowCandidate

    backend.app_windows = [
        AppWindowCandidate(
            window=w,
            doc_token=doc_token_from_title(w.title),
            unsaved_candidate=is_unsaved_candidate_title(w.title),
        )
        for w in windows
    ]
    backend.set_active_window(windows[0] if windows else None)
    return backend


def _ensure(backend: FakeComputerBackend, target: str, allow_launch: bool = False) -> str:
    return backend.ensure_app(target, allow_launch=allow_launch)


# --- title heuristics (generic, app-agnostic) -----------------------------------------------


def test_doc_token_and_unsaved_heuristics() -> None:
    assert doc_token_from_title("Book1 - Excel") == "Book1"
    assert doc_token_from_title("t1_source.txt - Notepad") == "t1_source.txt"
    assert doc_token_from_title("Untitled - Paint") == "Untitled"
    assert is_unsaved_candidate_title("Book1 - Excel")
    assert is_unsaved_candidate_title("Untitled - Notepad")
    assert is_unsaved_candidate_title("t4_out_summary.xlsx1.xlsx - Excel")  # restore suffix
    assert not is_unsaved_candidate_title("t1_source.xlsx - Excel")
    assert not is_unsaved_candidate_title("t6_inbox - File Explorer")


# --- reattach ------------------------------------------------------------------------------


def test_identity_match_reattaches_without_launching() -> None:
    backend = _backend_with(DOC)
    payload = _ensure(backend, "excel.exe|t1_source")
    assert payload.startswith(REATTACHED)
    assert "title='t1_source.xlsx - Excel'" in payload
    assert backend.focused == ["t1_source.xlsx - Excel"]  # focused via the verified primitive
    assert backend.launched_processes == []  # never launches


def test_doc_token_match_preferred_over_bare_process_match() -> None:
    backend = _backend_with(EXCEL, DOC)  # EXCEL first in z-order
    payload = _ensure(backend, "excel|t1_source")
    assert payload.startswith(REATTACHED) and "t1_source.xlsx" in payload
    assert backend.focused == ["t1_source.xlsx - Excel"]


def test_process_only_match_attaches_any_instance() -> None:
    backend = _backend_with(EXCEL, DOC)
    payload = _ensure(backend, "excel")
    assert payload.startswith(REATTACHED) and "Book1 - Excel" in payload  # z-order first


def test_reattach_rebinds_the_focus_guard() -> None:
    from computer_use_mcp.focus_guard import InterferenceGuard

    backend = _backend_with(DOC)
    guard = InterferenceGuard(backend, parse_interference(None))
    guard.note_ensure_app_outcome(_ensure(backend, "excel|t1_source"))
    assert guard.bound is not None and guard.bound.hwnd == DOC.hwnd


def test_unsaved_candidate_yields_ambiguous_instance_and_never_launches() -> None:
    backend = _backend_with(STALE_RESTORE)  # restore-suffixed duplicate, doc needle misses
    payload = _ensure(backend, "excel|different_doc")
    assert payload.startswith(AMBIGUOUS_INSTANCE)
    assert "t4_out_summary.xlsx1.xlsx - Excel" in payload
    assert "unsaved=true" in payload and "do_not_launch=true" in payload
    assert backend.focused == []  # nothing focused...
    assert backend.launched_processes == []  # ...and nothing launched
    assert backend.windows, "discovery only: no window was closed"


def test_no_instance_payload_with_driver_launch_default() -> None:
    backend = _backend_with()
    payload = _ensure(backend, "excel.exe")
    assert payload.startswith(NO_INSTANCE)
    assert "launch=driver" in payload
    assert backend.launched_processes == []


def test_server_launch_requires_explicit_allow_launch() -> None:
    backend = _backend_with()
    denied = _ensure(backend, "excel.exe", allow_launch=False)
    assert "launch=driver" in denied and backend.launched_processes == []
    launched = _ensure(backend, "definitely-not-a-real-app-xyz", allow_launch=True)
    assert "launched=definitely-not-a-real-app-xyz" in launched


def test_agent_launch_gate_requires_server_policy_and_allowlist() -> None:
    from computer_use_mcp.agent import ComputerUseAgent
    from computer_use_mcp.limits import Limits
    from computer_use_mcp.safety import SafetyPolicy
    from computer_use_mcp.state import StopToken, TaskState

    backend = _backend_with()
    action = GroundedAction(action="ensure_app", target="excel.exe", confidence=1.0)

    default_agent = ComputerUseAgent(
        backend, provider=None, safety=SafetyPolicy(), task=TaskState(), stop=StopToken(),
        limits=Limits().validate(),
    )
    assert default_agent._ensure_app_allow_launch(action) is False  # launch="driver" default

    server_policy_agent = ComputerUseAgent(
        backend, provider=None, safety=SafetyPolicy(), task=TaskState(), stop=StopToken(),
        limits=Limits().validate(),
        interference=parse_interference({"attach_or_launch": {"launch": "server"}}),
    )
    assert server_policy_agent._ensure_app_allow_launch(action) is True  # no allowlist configured

    allowlisted_agent = ComputerUseAgent(
        backend, provider=None, safety=SafetyPolicy(), task=TaskState(), stop=StopToken(),
        allowed_processes=["EXCEL.EXE"],
        limits=Limits().validate(),
        interference=parse_interference({"attach_or_launch": {"launch": "server"}}),
    )
    assert allowlisted_agent._ensure_app_allow_launch(action) is True
    other = allowlisted_agent._ensure_app_allow_launch(
        GroundedAction(action="ensure_app", target="chrome.exe", confidence=1.0)
    )
    assert other is False  # target process outside the allowlist: launch denied


def test_non_matching_windows_of_the_process_are_ambiguous() -> None:
    backend = _backend_with(EXCEL)
    payload = _ensure(backend, "excel|nonexistent_doc")
    assert payload.startswith(AMBIGUOUS_INSTANCE)  # driver decides; no blind launch
    assert "Book1 - Excel" in payload


# --- controller integration -------------------------------------------------------------------


def _run(backend: FakeComputerBackend, action: GroundedAction) -> Any:
    from computer_use_mcp.agent import ComputerUseAgent
    from computer_use_mcp.limits import Limits
    from computer_use_mcp.observation import ObservationEngine
    from computer_use_mcp.safety import SafetyPolicy
    from computer_use_mcp.state import StopToken, TaskState

    agent = ComputerUseAgent(
        backend, provider=None, safety=SafetyPolicy(), task=TaskState(), stop=StopToken(),
        limits=Limits(max_actions=5, max_task_seconds=60.0).validate(),
    )
    agent.observation = ObservationEngine(backend)
    state = SimpleNamespace(
        dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
        max_steps=5, step_count=0, require_approval=False, max_retries_per_action=1,
    )
    return asyncio.run(agent.run_single(state, action))


def test_ensure_app_reattached_verifies_via_process_identity() -> None:
    backend = _backend_with(EXCEL)
    outcome = _run(
        backend, GroundedAction(action="ensure_app", target="excel", confidence=1.0)
    )
    assert outcome.kind == "executed" and outcome.result is not None
    assert outcome.result.ok is True
    assert outcome.result.message.startswith(REATTACHED)


def test_ensure_app_probe_outcome_is_evidence_not_a_screen_claim() -> None:
    backend = _backend_with()
    outcome = _run(
        backend, GroundedAction(action="ensure_app", target="excel", confidence=1.0)
    )
    assert outcome.kind == "executed" and outcome.result is not None
    assert outcome.result.ok is True
    assert outcome.result.message.startswith(NO_INSTANCE)
    assert outcome.result.verification is not None
    assert outcome.result.verification.verification_method == "ensure_app_probe"
    assert "NO_INSTANCE" in outcome.result.verification.note


def test_ensure_app_without_target_is_rejected_fail_closed() -> None:
    from computer_use_mcp.models import GroundedAction as GA

    with pytest.raises(ValueError):
        GA(action="ensure_app", confidence=1.0)  # target required (model validator)
