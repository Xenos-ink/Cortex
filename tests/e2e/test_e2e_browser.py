"""Real-Windows browser E2E: local page opened in Edge, verified via window state.

Setup launches Microsoft Edge (Server 2022 default enterprise browser) with a local HTML
file (``file:///`` URL, unique marker title, no first-run wizards). The runtime then runs
a direct ``computer_execute`` (``focus_window`` with the page marker as target), which
0.7.6 maps to a WINDOW_STATE intent (pipeline/agent.py maps focus_window's target to the
expected window title) that the built-in ``WindowStateStrategy`` verifies against the
post-action observation's REAL foreground title and the page's ``<title>`` — window
identity verification from observation fields, no OCR, no pixel diff (RETARGETED, loop
removal: the loop is gone; the window_state verification path is the direct path's.
WP12 live-run-2: the previous ``wait`` driver is by design a completion marker
(``no_op_marker`` — it makes no screen claim) and can never exercise window_state).

Environment notes (probed live): the Edge window carries a profile suffix
("... - Work - Microsoft Edge"), so verification uses contains-matching against the unique
page marker. Window ownership (A8 regression wave): when an Edge instance is ALREADY
running on the box (host-session browser), the launched process hands the URL off to the
existing singleton and exits, so the new window is NOT owned by the launcher PID. The
arrangement therefore waits on the UNIQUE per-run page-title marker (never on the
launcher PID alone) and identity is asserted against the window's REAL owning pid read
via Win32. Cleanup closes exactly the hwnd found and kills only the launched tree (a
handoff launcher is already dead or a childless stub — never the user's browser).

Window-isolation doctrine (D6, R-8): the page marker is run-UNIQUE
(``w32.unique_window_token``) and the wait goes through the marker-only attach path
(``wait_for_marked_window``), so the user's own Edge window can never match; teardown
closes exactly the found marker hwnd (exact-hwnd close, not a title match).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import helpers_runtime as rt
import helpers_win32 as w32
import pytest

import computer_use_mcp.server.execute as srv_server_execute
import computer_use_mcp.server.lifecycle as srv_server_lifecycle
import computer_use_mcp.server.observe as srv_server_observe

pytestmark = pytest.mark.e2e

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
# D6: run-UNIQUE page-title marker (legacy static marker "E2E Browser Verification
# Page 7391" was shared across runs; the token makes this run's window unambiguous
# and un-collidable with any user Edge window). The legacy string stays searchable.
PAGE_TITLE_MARKER = f"E2E Browser Verification Page {w32.unique_window_token('page7391')}"


def find_edge() -> str | None:
    for candidate in EDGE_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


@contextmanager
def edge_app(deadline: w32.Deadline, page_url: str):
    """Launch Edge on a local page; close the window we opened; kill only our tree."""
    edge = find_edge()
    if edge is None:
        pytest.skip("Microsoft Edge not found on this box; no other browser is sanctioned for E2E")
    proc = subprocess.Popen(
        [
            edge,
            "--new-window",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=msEdgeWelcome,msUndiciPoolRestrictions",
            "--window-size=1200,800",
            page_url,
        ]
    )
    hwnd: int | None = None
    try:
        # D6: attach by the run-UNIQUE page-title marker via the marker-only path —
        # with an Edge singleton already running, the launcher hands off and exits
        # before the window opens, AND the user's own Edge window (different title)
        # is invisible to this wait by construction.
        hwnd = w32.wait_for_marked_window(deadline, PAGE_TITLE_MARKER, timeout_s=45.0)
        assert PAGE_TITLE_MARKER.casefold() in w32.window_text(hwnd).casefold(), (
            w32.window_text(hwnd)
        )
        yield proc, hwnd
    finally:
        if hwnd is not None:
            # close_window is exact-hwnd (D6): closes only OUR marker window, never
            # any other same-name Edge window.
            w32.close_window(hwnd, wait_s=8.0)
        if proc.poll() is None:  # launcher still alive (no handoff): our tree, kill it
            w32.kill_process_tree(proc.pid)


def test_browser_local_page_window_state_verification(
    deadline: w32.Deadline, e2e_scratch: Path, make_session: Any, evidence: Any
) -> None:
    """Open a local HTML page in Edge; verify the runtime observes msedge.exe identity and
    verifies the page <title> via the window_state strategy."""
    page_path = e2e_scratch / "e2e_page.html"
    page_path.write_text(
        "<!DOCTYPE html><html><head><title>"
        + PAGE_TITLE_MARKER
        + "</title></head><body><h1>E2E target page</h1>"
        "<p>Local verification page for the computer-use-mcp E2E suite.</p></body></html>",
        encoding="utf-8",
    )
    page_url = "file:///" + str(page_path).replace("\\", "/")
    with edge_app(deadline, page_url) as (_proc, hwnd):
        provider = rt.E2EScriptedProvider(
            [
                rt.step(
                    rt.GroundedAction(
                        action="wait",
                        delta=1,
                        confidence=1.0,
                        expected_effect=PAGE_TITLE_MARKER,
                        reason="hold one beat, then verify the local page is the active window",
                    ),
                    verification_hint="window_state",
                ),
                rt.done("local page verified via window state"),
            ]
        )
        session_id, bundle = make_session(provider=provider, dry_run=False, require_approval=False)
        try:
            # The hosting console can steal foreground during fixture setup; make the
            # identity observation deterministic by explicitly focusing the browser.
            assert w32.focus_window(hwnd), "could not focus the Edge window"
            observation = w32.observe_tool_metadata(srv_server_observe.computer_observe(session_id))["observation"]
            info = observation["active_window_info"]
            if evidence is not None:
                evidence.save_observation("before", observation)
            assert str(info["process_name"]).casefold() == "msedge.exe", info
            # Identity is asserted against the window's REAL owning pid (ground truth by
            # construction): with a pre-running Edge singleton the owner is the browser
            # process, not the short-lived launcher.
            assert info["pid"] == w32.window_pid(hwnd), (info["pid"], w32.window_pid(hwnd))
            assert info["hwnd"] == hwnd, (info["hwnd"], hwnd)
            assert PAGE_TITLE_MARKER in info["title"], info
            assert observation["coordinate_space"] == "verified_passthrough", observation

            # THE ACTION UNDER TEST (WP12 live-run-2): a ``focus_window`` whose target
            # is the run-unique page marker. 0.7.6 maps focus_window to a WINDOW_STATE
            # intent (pipeline/agent.py:1059-1063 — window_title = action.target)
            # verified by WindowStateStrategy (pipeline/verification.py:734-749, title
            # contains-match case-insensitive) — a REAL checkable window-state claim.
            # The previous ``wait`` driver is BY DESIGN a completion marker
            # (verification_method 'no_op_marker'; it makes no screen claim), so it can
            # never exercise window_state; focus_window can, without weakening the
            # assertions below (all of them unchanged).
            response = rt.execute_payload(
                asyncio.run(
                    srv_server_execute.computer_execute(
                        session_id, "focus_window", target=PAGE_TITLE_MARKER
                    )
                )
            )
            if evidence is not None:
                evidence.record(
                    "computer_execute(focus_window)",
                    ok=response.get("ok"),
                )
            verification = response["verification"]
            assert response["ok"] is True, response
            assert verification["outcome"] == "verified", verification
            assert verification["verification_method"] == "window_state", verification
            assert PAGE_TITLE_MARKER.casefold() in verification["note"].casefold() or any(
                PAGE_TITLE_MARKER.casefold() in item.casefold() for item in verification["evidence"]
            ), verification

            if evidence is not None:
                evidence.assert_that("Edge window identity: pid/hwnd/title/process", True, info["title"])
                evidence.assert_that(
                    "window_state strategy verified the page title on the real observation",
                    True,
                    verification["verification_method"],
                )
                evidence.add_extra("window_title", info["title"])
                evidence.add_extra("verification", verification)
                evidence.save_audit(bundle, session_id)
                evidence.save_observation(
                    "after", w32.observe_tool_metadata(srv_server_observe.computer_observe(session_id))["observation"]
                )
        finally:
            asyncio.run(srv_server_lifecycle.stop_session(session_id))
