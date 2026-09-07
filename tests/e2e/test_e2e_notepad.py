"""Real-Windows Notepad E2E: identity proof, semantic typing, fault injection.

Drives the LIVE desktop through the runtime's own tool surface (``start_session`` ->
``computer_observe`` -> ``run_goal``) with a deterministic scripted provider — the vision
model plays no role (E2E validates the RUNTIME: observation → validate → risk → approval
→ execute → re-observe → verify → recovery).

Acceptance evidence produced here:

- P0-G/I: real ``WindowInfo`` (hwnd, pid, process_name, exe_path, window_class, title,
  bounds) + monitor/DPI/coordinate-space classification dumped to evidence.
- P0-A: typed text verified by the ``window_text`` strategy reading the real Edit control
  (WM_GETTEXT) — semantic window-text verification, not pixel diff alone.
- P0-B: moved-window fault → the stale click lands on a decoy Notepad → semantic
  verification fails (WRONG_WINDOW) → bounded recovery re-decides from fresh reality →
  task completes (recovery path exercised on real Windows).
- P0-H: window switch between propose and validate → ``STALE_OBSERVATION`` rejection →
  automatic re-observe + re-decide (the click is never executed on stale coordinates).

Window geometry doctrine: fault tests use two non-overlapping Notepad windows we own, so
every scripted click lands inside a window of ours — never on arbitrary desktop content
(PowerToys overlays and similar live-desktop unknowns are deliberately avoided).
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import helpers_runtime as rt
import helpers_win32 as w32
import pytest

from computer_use_mcp import server

pytestmark = pytest.mark.e2e

TYPED_MARKER = "e2e-typed-7391 quick brown fox"
RECOVERY_MARKER = "moved-window-recovered-7391"
STALE_MARKER = "stale-reject-marker-7391"


@contextmanager
def notepad_app(deadline: w32.Deadline, file_path: Path):
    """Launch Notepad with a scratch file; kill ONLY the process we started."""
    proc, hwnd = w32.launch_gui(
        deadline, ["notepad.exe", str(file_path)], title_contains=file_path.name, timeout_s=30.0
    )
    try:
        yield proc, hwnd
    finally:
        w32.kill_process_tree(proc.pid)


def observe(evidence: Any, session_id: str, when: str) -> dict[str, Any]:
    """Capture an observation through the MCP tool and save it as evidence."""
    response = w32.observe_tool_metadata(server.computer_observe(session_id))
    observation = response["observation"]
    if evidence is not None:
        evidence.save_observation(when, observation)
        evidence.record(
            f"observe_{when}",
            observation_id=response["observation_id"],
            active_app=response["active_app"],
            window=observation.get("active_window_info"),
            coordinate_space=str(observation.get("coordinate_space")),
        )
    return observation


def window_identity_ok(observation: dict[str, Any], proc: Any, file_name: str) -> dict[str, Any]:
    """Assert strong window/process identity (P0-G) on a dumped observation."""
    info = observation.get("active_window_info")
    assert info, "observation carries no active_window_info"
    assert info["process_name"] == "notepad.exe", info
    assert info["exe_path"] and info["exe_path"].casefold().endswith("notepad.exe"), info
    assert info["hwnd"], info
    assert info["pid"] == proc.pid, (info["pid"], proc.pid)
    assert info["window_class"] == "Notepad", info
    assert file_name.casefold() in info["title"].casefold(), info
    assert info["bounds"] and info["bounds"][2] > 0 and info["bounds"][3] > 0, info
    return info


def test_notepad_window_identity_observation(
    deadline: w32.Deadline, e2e_scratch: Path, make_session: Any, evidence: Any
) -> None:
    """P0-G/I evidence: strong window/process identity + DPI/coordinate-space on real Windows."""
    file_path = e2e_scratch / "identity_probe.txt"
    file_path.write_text("", encoding="utf-8")
    with notepad_app(deadline, file_path) as (proc, hwnd):
        session_id, bundle = make_session()  # real LocalComputerBackend, dry_run default
        try:
            # The hosting console can steal foreground during fixture setup; make the
            # identity observation deterministic by explicitly focusing the target.
            assert w32.focus_window(hwnd), "could not focus Notepad for the identity check"
            observation = observe(evidence, session_id, "before")
            info = window_identity_ok(observation, proc, file_path.name)
            monitor = observation["monitor"]
            assert observation["coordinate_space"] == "verified_passthrough", observation
            assert (observation["width"], observation["height"]) == (
                monitor["bounds"][2],
                monitor["bounds"][3],
            )
            assert monitor["dpi_scale_x"] == pytest.approx(1.25), monitor
            assert monitor["is_primary"] is True, monitor
            assert w32.window_pid(hwnd) == proc.pid
            if evidence is not None:
                evidence.assert_that("process_name == notepad.exe", True)
                evidence.assert_that("exe_path populated", True, info["exe_path"])
                evidence.assert_that("hwnd/pid match the launched process", True, info["pid"])
                evidence.assert_that("window_class/title/bounds populated", True, info["title"])
                evidence.assert_that("coordinate_space verified_passthrough @ 125% DPI", True)
                evidence.add_extra("window_identity", info)
                evidence.add_extra("monitor", monitor)
                evidence.save_audit(bundle, session_id)
        finally:
            server.stop_session(session_id)


def test_notepad_type_semantic_verification(
    deadline: w32.Deadline, e2e_scratch: Path, make_session: Any, with_verifier: Any, evidence: Any
) -> None:
    """P0-A: run_goal types into Notepad; verification reads the REAL Edit control text."""
    file_path = e2e_scratch / "typed.txt"
    file_path.write_text("", encoding="utf-8")
    with notepad_app(deadline, file_path) as (proc, hwnd):
        strategy = rt.WindowTextPredicateStrategy()
        provider = rt.E2EScriptedProvider(
            [
                # NOTE: no expected_effect — the expected_text intent must carry the typed text.
                rt.step(rt.type_text(TYPED_MARKER), verification_hint="expected_text"),
                rt.done("typed and verified"),
            ]
        )
        session_id, bundle = make_session(provider=provider, dry_run=False, require_approval=True)
        with_verifier(session_id, bundle, strategy)
        try:
            assert w32.focus_window(hwnd), "could not focus Notepad before typing"
            observation = observe(evidence, session_id, "before")
            window_identity_ok(observation, proc, file_path.name)

            response = asyncio.run(
                server.run_goal(
                    session_id, f"Type {TYPED_MARKER!r} into Notepad", approve_next_action=True
                )
            )
            if evidence is not None:
                evidence.record(
                    "run_goal",
                    ok=response.get("ok"),
                    termination=response.get("termination_reason"),
                    budget_remaining=response.get("approval_budget_remaining"),
                )

            assert response["ok"] is True, response
            assert response["termination_reason"] == "completed", response
            assert response["approval_budget_remaining"] == 0, response
            first = response["results"][0]
            verification = first["verification"]
            assert verification["outcome"] == "verified", verification
            assert verification["verification_method"] == "window_text", verification
            assert strategy.calls and strategy.calls[-1]["matched"], strategy.calls

            # Independent (outside the runtime) proof: the real Edit control holds the text.
            edit_text = w32.read_edit_text(hwnd)
            assert TYPED_MARKER in edit_text, edit_text

            if evidence is not None:
                evidence.assert_that("run_goal completed; approval budget consumed (1)", True)
                evidence.assert_that(
                    "semantic verification method == window_text (real Edit text)", True,
                    verification["note"],
                )
                evidence.assert_that("independent Edit-control text contains the marker", True)
                evidence.add_extra("edit_text", edit_text)
                evidence.add_extra("verification", verification)
                evidence.add_extra("metrics", response.get("metrics"))
                evidence.save_audit(bundle, session_id)
                observe(evidence, session_id, "after")
        finally:
            server.stop_session(session_id)


def test_notepad_moved_window_recovery(
    deadline: w32.Deadline, e2e_scratch: Path, make_session: Any, with_verifier: Any, evidence: Any
) -> None:
    """P0-B: the target window MOVES after the click is proposed. The stale click hits a
    decoy Notepad instead; semantic verification fails (WRONG_WINDOW); bounded recovery
    re-decides from fresh reality; the task completes."""
    target_path = e2e_scratch / "moved.txt"
    decoy_path = e2e_scratch / "decoy_b.txt"
    decoy_path.write_text("", encoding="utf-8")
    target_path.write_text("", encoding="utf-8")
    with notepad_app(deadline, decoy_path) as (_proc_b, hwnd_b), notepad_app(
        deadline, target_path
    ) as (_proc_a, hwnd_a):
        # Decoy first (lower z-order), then the target on top, non-overlapping rects.
        w32.move_window(hwnd_b, 300, 200, 700, 450)
        w32.move_window(hwnd_a, 60, 120, 700, 450)
        assert w32.focus_window(hwnd_a), "could not focus the target Notepad"

        strategy = rt.WindowTextPredicateStrategy()

        def initial_click(observation: Any) -> dict[str, Any]:
            center = rt.observation_center(observation)
            return rt.step(
                rt.click(
                    *center,
                    expected_effect="moved.txt",  # title needle for the window_state intent
                    reason="click grounded on the pre-move observation",
                ),
                verification_hint="window_state",
            )

        def fault_move(goal: str, observation: Any, history: list[str]) -> None:
            before = w32.window_rect(hwnd_a)
            w32.move_window(hwnd_a, 1200, 600, 420, 380)
            provider.fault_log.append(
                {"hook": "move_window", "before": before, "after": w32.window_rect(hwnd_a)}
            )

        def redecide(observation: Any) -> dict[str, Any]:
            left, top, width, height = w32.window_rect(hwnd_a)
            return rt.step(
                rt.click(
                    left + width // 2,
                    top + height // 2,
                    expected_effect="moved.txt",
                    reason="re-grounded click on the moved window",
                ),
                verification_hint="window_state",
            )

        provider = rt.E2EScriptedProvider(
            [initial_click, redecide, lambda _obs: rt.step(rt.type_text(RECOVERY_MARKER)),
             rt.done("recovered and typed")],
            hooks=[fault_move, None, None, None],
        )
        session_id, bundle = make_session(provider=provider, dry_run=False, require_approval=False)
        with_verifier(session_id, bundle, strategy)
        try:
            observe(evidence, session_id, "before")
            response = asyncio.run(
                server.run_goal(session_id, "Click into the moved.txt Notepad and type the marker")
            )
            if evidence is not None:
                evidence.record(
                    "run_goal",
                    ok=response.get("ok"),
                    termination=response.get("termination_reason"),
                    steps=response.get("step_count"),
                )

            assert provider.fault_log, "the move fault never fired"
            assert provider.fault_log[0]["after"] == (1200, 600, 420, 380), provider.fault_log
            assert response["termination_reason"] == "completed", response
            events = evidence.audit_events(bundle, session_id) if evidence is not None else []
            failed_verifications = [
                event for event in events
                if event["event_type"] == "verification" and event.get("result") == "failed"
            ]
            recovery_events = [event for event in events if event["event_type"] == "recovery"]
            classes = {event.get("metadata", {}).get("failure_class") for event in recovery_events}
            assert failed_verifications, "expected a failed verification for the stale click"
            assert recovery_events, "expected recovery events after the moved-window fault"
            assert classes & {"wrong_window", "moved_ui", "stale_coordinates"}, classes
            target_text = w32.read_edit_text(hwnd_a)
            decoy_text = w32.read_edit_text(hwnd_b)
            assert RECOVERY_MARKER in target_text, (RECOVERY_MARKER, target_text)
            assert RECOVERY_MARKER not in decoy_text, decoy_text

            if evidence is not None:
                evidence.assert_that("window actually moved between propose and execute", True,
                                     provider.fault_log[0])
                evidence.assert_that("stale click's verification FAILED (semantic, not pixel)", True)
                evidence.assert_that("bounded recovery classified and re-decided", True,
                                     sorted(x or "" for x in classes))
                evidence.assert_that("task completed; marker only in the target window", True)
                evidence.note(
                    "Window-bounds-only moves do NOT trip STALE_OBSERVATION (identity checks are "
                    "hwnd/pid/process/monitor/dimensions/coordinate-space); the semantic "
                    "verification layer is what catches the miss — documented runtime behavior."
                )
                evidence.add_extra("recovery_classes", sorted(x or "" for x in classes))
                evidence.add_extra("provider_fault_log", provider.fault_log)
                evidence.add_extra("metrics", response.get("metrics"))
                evidence.save_audit(bundle, session_id)
                observe(evidence, session_id, "after")
        finally:
            server.stop_session(session_id)


def test_notepad_window_switch_stale_observation(
    deadline: w32.Deadline, e2e_scratch: Path, make_session: Any, with_verifier: Any, evidence: Any
) -> None:
    """P0-H: between propose and validate the foreground window is switched to a decoy
    Notepad; the click is REJECTED as STALE_OBSERVATION (never executed), the runtime
    re-observes and re-decides, and the marker lands only in the intended window."""
    target_path = e2e_scratch / "target_a.txt"
    decoy_path = e2e_scratch / "decoy_b.txt"
    target_path.write_text("", encoding="utf-8")
    decoy_path.write_text("", encoding="utf-8")
    with notepad_app(deadline, decoy_path) as (_proc_b, hwnd_b), notepad_app(
        deadline, target_path
    ) as (_proc_a, hwnd_a):
        w32.move_window(hwnd_a, 60, 120, 700, 450)
        w32.move_window(hwnd_b, 900, 120, 700, 450)
        assert w32.focus_window(hwnd_a), "could not focus the target Notepad"

        strategy = rt.WindowTextPredicateStrategy()
        stale_click_center: list[tuple[int, int]] = []

        def initial_click(observation: Any) -> dict[str, Any]:
            center = rt.observation_center(observation)
            stale_click_center.append(center)
            return rt.step(
                rt.click(*center, expected_effect="target_a.txt",
                         reason="click grounded while the target window was focused"),
                verification_hint="window_state",
            )

        def fault_switch_window(goal: str, observation: Any, history: list[str]) -> None:
            before = w32.window_rect(hwnd_a)
            w32.move_window(hwnd_a, 210, 220, 700, 450)
            focused = w32.focus_window(hwnd_b)
            foreground = w32.window_class(int(w32.user32.GetForegroundWindow() or 0))
            provider.fault_log.append(
                {
                    "hook": "switch_to_decoy",
                    "moved_target": (before, w32.window_rect(hwnd_a)),
                    "focus_decoy_ok": focused,
                    "foreground_class_now": foreground,
                }
            )

        def redecide(observation: Any) -> dict[str, Any]:
            focused = w32.focus_window(hwnd_a)
            provider.fault_log.append({"hook": "refocus_target", "focus_ok": focused})
            left, top, width, height = w32.window_rect(hwnd_a)
            return rt.step(
                rt.click(left + width // 2, top + height // 2, expected_effect="target_a.txt",
                         reason="re-grounded after staleness rejection"),
                verification_hint="window_state",
            )

        def redecide_again(observation: Any) -> dict[str, Any]:
            left, top, width, height = w32.window_rect(hwnd_a)
            return rt.step(
                rt.click(left + width // 2, top + height // 2, expected_effect="target_a.txt",
                         reason="re-grounded click, target now focused"),
                verification_hint="window_state",
            )

        provider = rt.E2EScriptedProvider(
            [initial_click, redecide, redecide_again,
             lambda _obs: rt.step(rt.type_text(STALE_MARKER)),
             rt.done("typed after staleness recovery")],
            hooks=[fault_switch_window, None, None, None, None],
        )
        session_id, bundle = make_session(provider=provider, dry_run=False, require_approval=False)
        with_verifier(session_id, bundle, strategy)
        try:
            observe(evidence, session_id, "before")
            response = asyncio.run(
                server.run_goal(session_id, "Click into the target Notepad and type the marker")
            )
            if evidence is not None:
                evidence.record(
                    "run_goal",
                    ok=response.get("ok"),
                    termination=response.get("termination_reason"),
                    steps=response.get("step_count"),
                )

            fault = provider.fault_log[0] if provider.fault_log else {}
            assert fault.get("focus_decoy_ok") is True, provider.fault_log
            assert response["termination_reason"] == "completed", response
            events = evidence.audit_events(bundle, session_id) if evidence is not None else []
            stale_rejections = [
                event for event in events
                if event["event_type"] == "validation"
                and "STALE_OBSERVATION" in (event.get("metadata") or {}).get("codes", [])
            ]
            recovery_events = [event for event in events if event["event_type"] == "recovery"]
            assert stale_rejections, (
                "expected a STALE_OBSERVATION validation rejection; validation metadata: "
                f"{[event.get('metadata') for event in events if event['event_type'] == 'validation']}"
            )
            assert recovery_events, "expected recovery after the staleness rejection"
            target_text = w32.read_edit_text(hwnd_a)
            decoy_text = w32.read_edit_text(hwnd_b)
            assert STALE_MARKER in target_text, (STALE_MARKER, target_text)
            assert STALE_MARKER not in decoy_text, decoy_text

            if evidence is not None:
                evidence.assert_that("decoy window focused between propose and validate", True)
                evidence.assert_that(
                    "click rejected as STALE_OBSERVATION before execution", True,
                    f"{len(stale_rejections)} rejection(s)",
                )
                evidence.assert_that("runtime re-observed and re-decided (bounded recovery)", True)
                evidence.assert_that("marker landed only in the target window", True)
                evidence.add_extra("stale_click_center", stale_click_center)
                evidence.add_extra("provider_fault_log", provider.fault_log)
                evidence.add_extra("metrics", response.get("metrics"))
                evidence.save_audit(bundle, session_id)
                observe(evidence, session_id, "after")
        finally:
            server.stop_session(session_id)
