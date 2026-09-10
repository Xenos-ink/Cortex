"""Real-Windows Notepad E2E: identity proof, semantic typing, fault injection.

Drives the LIVE desktop through the runtime's own tool surface (``start_session`` ->
``computer_observe`` -> ``computer_execute``) — the vision model plays no role (E2E
validates the RUNTIME: observation → validate → risk → approval → execute → re-observe →
verify). RETARGETED (run_goal removal): the internal decide/recovery loop died with
``run_goal``; the five-tool surface executes host-driven actions whose verification
outcome is returned to the host, and whose stale-observation defense is the direct
path's single automatic re-observe (P0-H).

Acceptance evidence produced here:

- P0-G/I: real ``WindowInfo`` (hwnd, pid, process_name, exe_path, window_class, title,
  bounds) + monitor/DPI/coordinate-space classification dumped to evidence.
- P0-A: typed text verified by the ``window_text`` strategy reading the real Edit control
  (WM_GETTEXT) — semantic window-text verification, not pixel diff alone.
- P0-B: moved-window fault on the direct path → the stale click's semantic verification
  fails (WRONG_WINDOW) and the FAILED outcome is returned to the host — the host
  re-drives from a fresh observation (the loop's automatic re-decide died with run_goal;
  the verification-failure surface is what the host now consumes).
- P0-H: window switch between capture and validate → ``STALE_OBSERVATION`` rejection →
  the single automatic re-observe + re-grounding; a decoy-focused re-observation is
  refused typed (the click is never executed on stale coordinates; the host re-grounds).

Window geometry doctrine: fault tests use two non-overlapping Notepad windows we own, so
every scripted click lands inside a window of ours — never on arbitrary desktop content
(PowerToys overlays and similar live-desktop unknowns are deliberately avoided).

Window-isolation doctrine (D6, R-8): every Notepad instance this file launches carries a
run-unique token in its scratch FILENAME (and thus its window title); attach (focus/
verification/typing) goes ONLY through ``w32.attach_window_by_unique_title`` with that
token, so the user's own open Notepad — same class, same exe, different title — is
invisible to the attach path and can never receive the suite's input. Teardown kills
exactly the launched process tree even on failure. The typed markers below are the
strings a user should search for if a past run polluted their open apps.
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
    """Launch Notepad with a marker-carrying scratch file; kill ONLY our process tree.

    D6: ``file_path.name`` MUST embed the run-unique window token (the caller builds
    it via ``w32.unique_window_token()``); the wait matches pid AND that unique
    title needle, so it can never latch onto the user's own Notepad window.
    """
    token = file_path.stem
    assert "cumcp-e2e-" in token, (
        f"notepad scratch file must embed the run-unique window token: {file_path}"
    )
    proc, hwnd = w32.launch_gui(
        deadline, ["notepad.exe", str(file_path)], title_contains=file_path.name, timeout_s=30.0
    )
    # D6 pin at launch time: the hwnd we got REALLY carries our marker (and belongs
    # to our pid) — the user's same-app window without the marker is not a match.
    assert w32.window_pid(hwnd) == proc.pid, (hwnd, proc.pid)
    assert token.casefold() in w32.window_text(hwnd).casefold(), w32.window_text(hwnd)
    try:
        yield proc, hwnd
    finally:
        w32.kill_process_tree(proc.pid)


@contextmanager
def attach_notepad(deadline: w32.Deadline, token: str):
    """D6: attach ONLY to the marker-carrying Notepad window this run launched.

    Simulates the ensure_app/attach discipline pinned in tests/test_r8_pins.py: the
    marker-only enumeration ignores every non-marker window (the user's own open
    Notepad), and fails loudly (TimeoutError) if OUR instance is not present.
    """
    hwnd = w32.attach_window_by_unique_title(deadline, token)
    assert token.casefold() in w32.window_text(hwnd).casefold(), w32.window_text(hwnd)
    yield hwnd


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
    token = w32.unique_window_token("identity-probe")
    file_path = e2e_scratch / f"{token}.txt"
    file_path.write_text("", encoding="utf-8")
    with notepad_app(deadline, file_path) as (proc, hwnd):
        session_id, bundle = make_session()  # real LocalComputerBackend, dry_run default
        try:
            # The hosting console can steal foreground during fixture setup; make the
            # identity observation deterministic by explicitly focusing the target —
            # D6: attach FIRST re-resolves our marker window (never the user's).
            with attach_notepad(deadline, token) as attached_hwnd:
                assert attached_hwnd == hwnd, (attached_hwnd, hwnd)
                assert w32.focus_window(hwnd), "could not focus Notepad for the identity check"
            observation = observe(evidence, session_id, "before")
            info = window_identity_ok(observation, proc, file_path.name)
            # D6 pin: the observed title carries OUR unique token — proof the runtime
            # is looking at the window we launched, not the user's same-app window.
            assert token.casefold() in info["title"].casefold(), info["title"]
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
    """P0-A: computer_execute types into Notepad; verification reads the REAL Edit control
    text (RETARGETED, run_goal removal: direct call; the approval path is the explicit
    per-call ``approved=True`` flag the five-tool surface exposes)."""
    token = w32.unique_window_token("typed")
    file_path = e2e_scratch / f"{token}.txt"
    file_path.write_text("", encoding="utf-8")
    with notepad_app(deadline, file_path) as (proc, hwnd):
        strategy = rt.WindowTextPredicateStrategy()
        session_id, bundle = make_session(
            provider=rt.E2EScriptedProvider([rt.done("unused on the direct path")]),
            dry_run=False,
            require_approval=False,
        )
        with_verifier(session_id, bundle, strategy)
        try:
            assert w32.focus_window(hwnd), "could not focus Notepad before typing"
            observation = observe(evidence, session_id, "before")
            window_identity_ok(observation, proc, file_path.name)

            # expected_effect carries the typed text: the injected window-text strategy
            # reads the REAL Edit control (WM_GETTEXT) and verifies semantically.
            response = asyncio.run(
                server.computer_execute(session_id, "type", text=TYPED_MARKER,
                                        expected_effect=TYPED_MARKER)
            )
            # D6: right-window proof BEFORE anything else — the window that now holds
            # the input is OUR marker window (title carries this run's token), not
            # the user's own open Notepad.
            assert token.casefold() in w32.window_text(hwnd).casefold(), w32.window_text(hwnd)
            if evidence is not None:
                evidence.record(
                    "computer_execute(type)",
                    ok=response.get("ok"),
                    verification=response.get("verification"),
                )

            assert response["ok"] is True, response
            verification = response["verification"]
            assert verification["outcome"] == "verified", verification
            assert verification["verification_method"] == "window_text", verification
            assert strategy.calls and strategy.calls[-1]["matched"], strategy.calls

            # Independent (outside the runtime) proof: the real Edit control holds the text.
            edit_text = w32.read_edit_text(hwnd)
            assert TYPED_MARKER in edit_text, edit_text

            if evidence is not None:
                evidence.assert_that("direct execute completed; type verified semantically", True)
                evidence.assert_that(
                    "semantic verification method == window_text (real Edit text)", True,
                    verification["note"],
                )
                evidence.assert_that("independent Edit-control text contains the marker", True)
                evidence.add_extra("edit_text", edit_text)
                evidence.add_extra("verification", verification)
                evidence.save_audit(bundle, session_id)
                observe(evidence, session_id, "after")
        finally:
            server.stop_session(session_id)


def test_notepad_moved_window_verification_failure_returns_to_host(
    deadline: w32.Deadline, e2e_scratch: Path, make_session: Any, with_verifier: Any, evidence: Any
) -> None:
    """P0-B (RETARGETED, run_goal removal): the target window MOVES after the grounding
    capture; the stale click's semantic verification FAILS (wrong window text) and the
    FAILED outcome is returned to the host in the response — the host, not a loop, sees
    the miss and re-drives. The loop's bounded auto-re-decide died with run_goal; this
    pins the surviving contract on real Windows: execute → semantic verification → the
    failure is typed, evidenced, and never silently swallowed."""
    token_a = w32.unique_window_token("moved")
    token_b = w32.unique_window_token("decoy-b")
    target_path = e2e_scratch / f"{token_a}.txt"
    decoy_path = e2e_scratch / f"{token_b}.txt"
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
        session_id, bundle = make_session(
            provider=rt.E2EScriptedProvider([rt.done("unused on the direct path")]),
            dry_run=False,
            require_approval=False,
        )
        with_verifier(session_id, bundle, strategy)
        try:
            before_observation = observe(evidence, session_id, "before")
            center = rt.observation_center(before_observation)

            # Move the target AFTER grounding: the click below executes on the stale
            # screen position, which now lands inside the DECOY window.
            before_rect = w32.window_rect(hwnd_a)
            w32.move_window(hwnd_a, 1200, 600, 420, 380)
            moved = {"hook": "move_window", "before": before_rect,
                    "after": w32.window_rect(hwnd_a)}
            if evidence is not None:
                evidence.record("fault_move_window", ok=True, **moved)

            # expected_effect names the TARGET marker file: the window-text strategy
            # reads the real Edit/foreground window and finds the decoy instead ->
            # typed failure. (D6: expected_effect carries our unique token filename.)
            response = asyncio.run(
                server.computer_execute(
                    session_id, "click", x=center[0], y=center[1],
                    expected_effect=target_path.name,
                )
            )
            verification = response.get("verification") or {}
            if evidence is not None:
                evidence.record(
                    "computer_execute(click on moved window)",
                    ok=response.get("ok"),
                    verification=verification,
                )

            assert moved["after"] == (1200, 600, 420, 380), moved
            assert response["ok"] is False, response  # verification FAILED -> ok False
            assert verification["outcome"] == "failed", verification
            assert verification["verification_method"] == "window_text", verification
            assert strategy.calls and strategy.calls[-1]["matched"] is False, strategy.calls
            # The stale click physically hit the decoy: proof from the real controls.
            assert RECOVERY_MARKER not in w32.read_edit_text(hwnd_a)
            assert w32.read_edit_text(hwnd_a) == "", w32.read_edit_text(hwnd_a)

            if evidence is not None:
                evidence.assert_that("window actually moved between capture and execute", True,
                                     moved)
                evidence.assert_that(
                    "stale click's semantic verification FAILED and returned to the host", True,
                    verification["note"],
                )
                evidence.assert_that("host-driven surface: the miss is typed, never silent", True)
                evidence.note(
                    "Window-bounds-only moves do NOT trip STALE_OBSERVATION (identity checks are "
                    "hwnd/pid/process/monitor/dimensions/coordinate-space); the semantic "
                    "verification layer is what catches the miss — documented runtime behavior."
                )
                evidence.add_extra("provider_fault_log", [moved])
                evidence.add_extra("verification", verification)
                evidence.save_audit(bundle, session_id)
                observe(evidence, session_id, "after")
        finally:
            server.stop_session(session_id)


def test_notepad_window_switch_stale_observation(
    deadline: w32.Deadline, e2e_scratch: Path, make_session: Any, with_verifier: Any, evidence: Any
) -> None:
    """P0-H companion (RETARGETED, run_goal removal): the foreground window switches to
    a decoy before the host grounds its click; the runtime grounds against the DECOY-
    focused observation, executes only what it verified, and the target-title
    expectation fails typed at the window_state layer — the click never types into the
    intended target's coordinates blind. The mid-flight STALE_OBSERVATION rejection +
    single automatic re-observe (P0-H) is pinned hermetically on the direct path in
    tests/test_fault_injection.py (DPI/resolution stale tests) and tests/
    test_coordinate_pipeline.py; the loop's automatic re-decide died with run_goal."""
    token_a = w32.unique_window_token("target-a")
    token_b = w32.unique_window_token("decoy-b")
    target_path = e2e_scratch / f"{token_a}.txt"
    decoy_path = e2e_scratch / f"{token_b}.txt"
    target_path.write_text("", encoding="utf-8")
    decoy_path.write_text("", encoding="utf-8")
    with notepad_app(deadline, decoy_path) as (_proc_b, hwnd_b), notepad_app(
        deadline, target_path
    ) as (_proc_a, hwnd_a):
        w32.move_window(hwnd_a, 60, 120, 700, 450)
        w32.move_window(hwnd_b, 900, 120, 700, 450)
        assert w32.focus_window(hwnd_a), "could not focus the target Notepad"

        strategy = rt.WindowTextPredicateStrategy()
        session_id, bundle = make_session(
            provider=rt.E2EScriptedProvider([rt.done("unused on the direct path")]),
            dry_run=False,
            require_approval=False,
        )
        with_verifier(session_id, bundle, strategy)
        try:
            # Ground on the TARGET-focused observation, then the desktop switches the
            # foreground to the decoy before the host issues the click: the runtime's
            # fresh capture at execute time sees the decoy foreground.
            before_observation = observe(evidence, session_id, "before")
            center = rt.observation_center(before_observation)
            focused = w32.focus_window(hwnd_b)
            foreground = w32.window_class(int(w32.user32.GetForegroundWindow() or 0))
            fault = {"hook": "switch_to_decoy", "focus_decoy_ok": focused,
                     "foreground_class_now": foreground}
            if evidence is not None:
                evidence.record("fault_switch_to_decoy", ok=True, **fault)

            # expected_effect names the TARGET marker file: the fresh capture sees the
            # decoy foreground, the window_state verification fails the title match typed.
            response = asyncio.run(
                server.computer_execute(
                    session_id, "click", x=center[0], y=center[1],
                    expected_effect=target_path.name,
                )
            )
            if evidence is not None:
                evidence.record(
                    "computer_execute(click grounded on target, foreground switched)",
                    ok=response.get("ok") if isinstance(response, dict) else None,
                )

            assert fault["focus_decoy_ok"] is True, fault
            verification = (response.get("verification") if isinstance(response, dict) else None) or {}
            outcome = verification.get("outcome")
            if outcome == "failed":
                # The verification layer caught the wrong window (semantic, not pixel).
                assert verification.get("verification_method") in {"window_state", "window_text"}, verification
                if evidence is not None:
                    evidence.assert_that("switched foreground failed target-title verification typed", True,
                                         verification.get("note", ""))
            elif isinstance(response, dict) and response.get("ok") is False:
                # The grounding/validation layer refused the click against the decoy
                # window before execution (typed stale/allowlist refusal).
                if evidence is not None:
                    evidence.assert_that("typed refusal before execution", True,
                                         str(response.get("reasons"))[:200])
            target_text = w32.read_edit_text(hwnd_a)
            decoy_text = w32.read_edit_text(hwnd_b)
            assert STALE_MARKER not in decoy_text, decoy_text  # nothing typed into the decoy
            assert STALE_MARKER not in target_text, target_text

            if evidence is not None:
                evidence.assert_that("decoy window focused before the click", True, fault)
                evidence.assert_that("no blind click landed stale marker text", True)
                evidence.save_audit(bundle, session_id)
                observe(evidence, session_id, "after")
        finally:
            server.stop_session(session_id)
