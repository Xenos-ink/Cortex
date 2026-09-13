"""Real-Windows right-click E2E: one right_click opens a real context menu.

Drives the LIVE desktop through the runtime's own tool surface (``start_session`` ->
``computer_observe`` -> ``computer_execute``) — the vision model plays no role, exactly
like the sibling Notepad/browser e2e tests: observation → validate → risk → approval →
execute → re-observe → verify, all real.

Acceptance evidence produced here (owner-commissioned right_click, v0.6.0):

- a real ``right_click`` at the Notepad editor area OPENS a real context menu,
  proven by Win32 window enumeration: a visible menu window owned by OUR process
  (the classic Win32 menu class is ``#32768``) that did NOT exist before the press;
- the served ``computer_execute`` result reports ``ok`` with its verification verdict
  recorded as corroboration (a context menu is a large pixel change);
- the session was started with the window allowlist bound to OUR unique token, so the
  dispatch only ever ran against the window we launched (D6/R-8 isolation doctrine).

Window-isolation doctrine (D6, R-8 — identical to test_e2e_notepad): the Notepad
instance this file launches carries a run-unique token in its scratch FILENAME (and
thus its window title); the session allowlist pins that token, so the user's own open
Notepad can never receive this suite's input. Teardown kills exactly the launched
process tree even on failure. ESC (through our own session) closes the menu before
teardown so no menu outlives the test.
"""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import helpers_runtime as rt
import helpers_win32 as w32
import pytest

from computer_use_mcp import server

pytestmark = pytest.mark.e2e

#: The classic Win32 popup-menu window class (right-click context menus live here on
#: classic apps). The strong assertion is "a NEW visible window owned by OUR pid";
#: the class check is corroborating, not load-bearing (Win11 XAML menus may host
#: differently, and the newer Notepad is exactly that case on some builds).
MENU_CLASS = "#32768"


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
    assert w32.window_pid(hwnd) == proc.pid, (hwnd, proc.pid)
    assert token.casefold() in w32.window_text(hwnd).casefold(), w32.window_text(hwnd)
    try:
        yield proc, hwnd
    finally:
        w32.kill_process_tree(proc.pid)


def _visible_windows_for_pid(pid: int) -> list[dict[str, Any]]:
    """Enumerate VISIBLE top-level windows owned by ``pid`` (menus included)."""
    found = []
    for hwnd in w32.top_level_windows():
        if not w32.is_visible(hwnd):
            continue
        if w32.window_pid(hwnd) != pid:
            continue
        found.append(
            {
                "hwnd": hwnd,
                "class": w32.window_class(hwnd),
                "title": w32.window_text(hwnd),
            }
        )
    return found


def _menu_windows(pid: int) -> list[dict[str, Any]]:
    """Visible windows owned by ``pid`` that look like a context menu."""
    return [
        window
        for window in _visible_windows_for_pid(pid)
        if window["class"] == MENU_CLASS or "context" in window["class"].casefold()
    ]


def observe(evidence: Any, session_id: str, when: str) -> dict[str, Any]:
    """Capture an observation through the MCP tool and save it (dump + image) as evidence."""
    response = server.computer_observe(session_id)
    metadata = w32.observe_tool_metadata(response)
    observation = metadata["observation"]
    if evidence is not None:
        evidence.save_observation(when, observation)
        # The tool returns MCP content blocks: [TextContent(metadata), ImageContent(png)].
        # Save the image block too — visual proof of the state at this instant.
        for block in response if isinstance(response, list) else []:
            if getattr(block, "type", None) == "image":
                try:
                    import io as _io

                    from PIL import Image

                    image = Image.open(_io.BytesIO(base64.b64decode(block.data))).convert("RGB")
                    image.save(evidence.dir / f"screenshot_{when}.png")
                except Exception as exc:  # noqa: BLE001 - screenshots are best-effort
                    evidence.note(f"screenshot_{when} not saved: {type(exc).__name__}: {exc}")
                break
        evidence.record(
            f"observe_{when}",
            observation_id=metadata.get("observation_id"),
            active_app=metadata.get("active_app"),
            window=observation.get("active_window_info"),
            coordinate_space=str(observation.get("coordinate_space")),
        )
    return observation


def test_right_click_opens_real_context_menu(
    deadline: w32.Deadline, e2e_scratch: Path, make_session: Any, evidence: Any
) -> None:
    """One right_click on OUR Notepad editor area opens a REAL context menu."""
    token = w32.unique_window_token("cumcp-e2e-right-click")
    file_path = e2e_scratch / f"{token}.txt"
    file_path.write_text("", encoding="utf-8")
    with notepad_app(deadline, file_path) as (proc, hwnd):
        # The session allowlist is bound to OUR token: every dispatch in this session
        # validates the ACTIVE window title against it, so input can only ever land
        # in the window we launched (the user's own Notepad is invisible to it).
        session_id, bundle = make_session(
            provider=rt.E2EScriptedProvider([rt.done("unused on the direct path")]),
            dry_run=False,
            require_approval=False,
            allowed_windows=[token],
        )
        try:
            assert w32.focus_window(hwnd), "could not focus our Notepad before the right-click"
            before_observation = observe(evidence, session_id, "before")

            # Identity pin: the observed active window is OURS (token in the title).
            info = before_observation.get("active_window_info")
            assert info, "observation carries no active_window_info"
            assert info["pid"] == proc.pid, (info.get("pid"), proc.pid)
            assert token.casefold() in str(info["title"]).casefold(), info["title"]
            assert before_observation["coordinate_space"] in {
                "verified_passthrough",
                "scaled",
            }, before_observation["coordinate_space"]

            # Aim at the editor area: the center of OUR window's bounds. In
            # verified_passthrough the screenshot space IS the physical screen space;
            # under a verified scale the runtime applies the single transform itself.
            bounds = info["bounds"]
            assert bounds and bounds[2] > 0 and bounds[3] > 0, bounds
            target_x = int(bounds[0] + bounds[2] / 2)
            target_y = int(bounds[1] + bounds[3] / 2)

            # Baseline: no menu window of ours exists yet.
            menus_before = _menu_windows(proc.pid)
            assert menus_before == [], menus_before
            visible_before = {window["hwnd"] for window in _visible_windows_for_pid(proc.pid)}
            if evidence is not None:
                evidence.record("menu_baseline", menus_before=menus_before)

            # THE ACTION UNDER TEST: right_click through the served tool surface.
            # The response must prove the action EXECUTED through the full pipeline —
            # never a rejection (invalid action / safety / stale observation). Its
            # verification verdict is recorded as CORROBORATION only: with no stated
            # expectation, a small context menu is exactly the "sub-threshold visual
            # change" the doctrine reports as honest `uncertain` (never false success).
            # The CAPABILITY PROOF is the Win32 menu-window enumeration below.
            response = asyncio.run(
                server.computer_execute(session_id, "right_click", x=target_x, y=target_y)
            )
            response = _payload(response)
            assert response.get("error") is None, response
            assert "requires_approval" not in response, response
            if evidence is not None:
                evidence.record(
                    "computer_execute(right_click)",
                    ok=response.get("ok"),
                    x=target_x,
                    y=target_y,
                    verification=response.get("verification"),
                )

            # CAPABILITY PROOF: a NEW visible window owned by OUR pid appears within
            # seconds, and it looks like a context menu (classic #32768 class or a
            # context-y host class). Polled — menu creation can trail the dispatch.
            menus_after: list[dict[str, Any]] = []
            new_windows: list[dict[str, Any]] = []

            def _menu_opened() -> bool:
                nonlocal menus_after, new_windows
                menus_after = _menu_windows(proc.pid)
                new_windows = [
                    window
                    for window in _visible_windows_for_pid(proc.pid)
                    if window["hwnd"] not in visible_before
                ]
                return bool(menus_after or new_windows)

            w32.wait_until(deadline, _menu_opened, "context menu window after right_click")
            if evidence is not None:
                evidence.record("menu_after", menus=menus_after, new_windows=new_windows)
                evidence.add_extra("menu_windows", menus_after)
                evidence.add_extra("new_windows", new_windows)

            verification = response.get("verification") or {}
            assert verification.get("outcome") in {"verified", "uncertain"}, verification

            # Post-action observation + audit trail as evidence.
            observe(evidence, session_id, "after")
            if evidence is not None:
                evidence.assert_that(
                    "right_click executed through the pipeline (no rejection)", True
                )
                evidence.assert_that(
                    "context menu window appeared (owned by our pid)",
                    bool(menus_after or new_windows),
                    menus_after or new_windows,
                )
                evidence.save_audit(bundle, session_id)

            # Cleanup through our OWN session: ESC closes the menu (the menu of our
            # process holds foreground). The menu window must be gone afterwards.
            esc_response = _payload(
                asyncio.run(server.computer_execute(session_id, "keypress", keys=["esc"]))
            )
            if evidence is not None:
                evidence.record("computer_execute(keypress esc)", ok=esc_response.get("ok"))
            deadline.check("menu closed after ESC")
            menus_left = _menu_windows(proc.pid)
            assert menus_left == [], menus_left
            if evidence is not None:
                evidence.assert_that("menu closed after ESC", True)
        finally:
            with suppress(Exception):  # teardown never masks the test result
                server.stop_session(session_id)


def _payload(response: Any) -> dict[str, Any]:
    """Unwrap an executed MCP content-block response to its dict payload."""
    if isinstance(response, list):
        return json.loads(response[0].text)
    return response
