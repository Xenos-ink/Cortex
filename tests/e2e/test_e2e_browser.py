"""Real-Windows browser E2E: local page opened in Edge, verified via window state.

Setup launches Microsoft Edge (Server 2022 default enterprise browser) with a local HTML
file (``file:///`` URL, unique marker title, no first-run wizards). The runtime then runs
a scripted ``run_goal`` whose only interactive action is a ``wait``; its
``verification_hint="window_state"`` makes the built-in ``WindowStateStrategy`` verify the
post-action observation's REAL foreground title against the page's ``<title>`` — window
identity verification from observation fields, no OCR, no pixel diff.

Environment notes (probed live): the Edge window carries a profile suffix
("... - Work - Microsoft Edge"), so verification uses contains-matching against the unique
page marker. The launcher PID owns the browser window on this box, so cleanup kills the
launched process tree only after first trying a polite WM_CLOSE.
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

from computer_use_mcp import server

pytestmark = pytest.mark.e2e

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
PAGE_TITLE_MARKER = "E2E Browser Verification Page 7391"


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
        hwnd = w32.wait_for_window(
            deadline, pid=proc.pid, title_needle=PAGE_TITLE_MARKER, timeout_s=45.0
        )
        yield proc, hwnd
    finally:
        if hwnd is not None:
            w32.close_window(hwnd, wait_s=8.0)
        if proc.poll() is None:  # launcher still alive: it owns the window we opened
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
    with edge_app(deadline, page_url) as (proc, hwnd):
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
            observation = server.computer_observe(session_id)["observation"]
            info = observation["active_window_info"]
            if evidence is not None:
                evidence.save_observation("before", observation)
            assert info["process_name"] == "msedge.exe", info
            assert info["pid"] == proc.pid, (info["pid"], proc.pid)
            assert info["hwnd"] == hwnd, (info["hwnd"], hwnd)
            assert PAGE_TITLE_MARKER in info["title"], info
            assert observation["coordinate_space"] == "verified_passthrough", observation

            response = asyncio.run(
                server.run_goal(session_id, f"Verify the local page {PAGE_TITLE_MARKER!r} is open")
            )
            if evidence is not None:
                evidence.record(
                    "run_goal",
                    ok=response.get("ok"),
                    termination=response.get("termination_reason"),
                )
            assert response["termination_reason"] == "completed", response
            first = response["results"][0]
            verification = first["verification"]
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
                    "after", server.computer_observe(session_id)["observation"]
                )
        finally:
            server.stop_session(session_id)
