"""Real-Win32 helpers for the E2E suite (E7-owned, tests/e2e only).

Everything here talks to the LIVE desktop via ctypes Win32 calls. The E2E suite drives
applications through the runtime's own input path (LocalComputerBackend -> pyautogui);
these helpers exist only for ARRANGEMENT and ASSERTION of real-world state:

- launching/killing scratch applications this suite started (never anything else);
- waiting for and identifying real windows (hwnd/pid/exe/class/title/bounds);
- reading real window text (Notepad Edit control, Calculator display Statics);
- enumerating Calculator button grids and mapping labels to screen coordinates;
- deterministic window moves (MoveWindow) for fault injection (never drag simulation).

Window-isolation doctrine (D6, R-8): this suite must NEVER attach to, focus, type
into, or close a window it did not itself launch. Every helper that finds a window
by name therefore goes through :func:`attach_window_by_unique_title`, which matches
ONLY windows whose title carries the run's UNIQUE marker (and, when given, whose
PID is one this suite launched). A same-application window without the marker — the
user's own open Notepad, for example — is INVISIBLE to the attach path: it is never
returned, so a wrong-window attach fails loudly instead of typing into the user's
text. Teardown closes/kill exactly the launched process tree / found-marker hwnd.

DPI doctrine: Win32 rect reads in this module must be in PHYSICAL pixels to match
the runtime's capture space (verified_passthrough on this box at 125% scaling). The
per-monitor-v2 awareness call is therefore LAZY (:func:`ensure_dpi_awareness`, invoked
by the conftest autouse fixture and by rect-sensitive entry points) — importing this
module has NO global side effect. Rationale (E6 finding D10): a process-global
awareness set at import/collection time downgrades ``LocalComputerBackend``'s own
awareness resolution to "system" (its fallback ladder re-sets via SetProcessDPIAware),
which flips ``dpi_estimated`` and breaks tests/test_platform_backend's
``dpi_estimated is False`` assertion in mixed runs. Only e2e-marked tests opt in.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import time
from collections.abc import Callable
from typing import Any

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

GA_ROOT = 2
WM_CLOSE = 0x0010
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_DPI_AWARENESS_DONE = False
_WINDOW_TOKEN_COUNTER = 0


def observe_tool_metadata(response: Any) -> dict[str, Any]:
    """Parse a ``computer_observe``/``computer_screenshot`` response into its metadata dict.

    Test-side compat adaptation (A8 regression wave): committed fix b1d873e (v0.4.1
    vision fix, part of the release-candidate base) changed the tool contract from a
    bare metadata dict to MCP content blocks — ``[TextContent(metadata JSON),
    ImageContent(png)]`` — and the desktop E2E helpers predate that change (they were
    written against the pre-0.4.1 dict shape). The production contract change is
    intentional and documented (README/ARCHITECTURE); this helper only re-parses the
    TextContent metadata block (observation / observation_id / active_app / digest /
    text_summary) so the desktop tests keep driving the REAL tool surface. A legacy
    dict response (defensive) passes through unchanged.
    """
    if isinstance(response, dict):
        return response
    for block in response:
        if getattr(block, "type", None) == "text":
            metadata = json.loads(block.text)
            assert metadata.get("observation_id"), metadata
            return metadata
    raise AssertionError(f"observation response carried no TextContent metadata: {response!r}")


def ensure_dpi_awareness() -> None:
    """Set per-monitor-v2 awareness once, lazily (idempotent, best effort).

    Called by the conftest autouse fixture for e2e-marked tests and by rect-sensitive
    entry points below — NEVER at import time (E6 finding D10): importing this module
    must not mutate process-global DPI state, because ``LocalComputerBackend`` must be
    able to resolve awareness itself for the standard suite's assertions to hold.
    Calling this after awareness was already set (by the backend or by a previous call)
    is a suppressed no-op.
    """
    global _DPI_AWARENESS_DONE
    if _DPI_AWARENESS_DONE:
        return
    _DPI_AWARENESS_DONE = True
    with contextlib.suppress(Exception):
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # DPI_AWARENESS_CONTEXT_PMv2

user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
user32.GetAncestor.restype = ctypes.c_void_p
user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.IsWindowVisible.restype = wt.BOOL
user32.IsWindow.argtypes = [ctypes.c_void_p]
user32.IsWindow.restype = wt.BOOL
user32.MoveWindow.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.BOOL]
user32.MoveWindow.restype = wt.BOOL
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.SetForegroundWindow.restype = wt.BOOL
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p]
kernel32.GetCurrentThreadId.restype = wt.DWORD


class Deadline:
    """Per-test deadline used to fail fast instead of hanging on a dead desktop."""

    def __init__(self, seconds: float) -> None:
        self.expiry = time.monotonic() + seconds

    def remaining(self) -> float:
        return self.expiry - time.monotonic()

    def check(self, what: str) -> None:
        if self.remaining() <= 0:
            raise TimeoutError(f"E2E deadline exceeded while waiting for: {what}")


def window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def window_pid(hwnd: int) -> int:
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


def is_visible(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(hwnd))


def process_exe(pid: int) -> str | None:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wt.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value or None
        return None
    finally:
        kernel32.CloseHandle(handle)


def top_level_windows() -> list[int]:
    """All visible top-level window handles."""
    found: list[int] = []
    protocol = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def on_window(hwnd, _lparam) -> bool:  # type: ignore[no-untyped-def]
        hwnd = int(hwnd)
        if is_visible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows.argtypes = [protocol, ctypes.c_void_p]
    user32.EnumWindows(protocol(on_window), None)
    return found


def find_windows(
    *,
    pid: int | None = None,
    class_name: str | None = None,
    title_needle: str | None = None,
) -> list[int]:
    """Find visible top-level windows matching any of the given identity criteria.

    Parameter names deliberately differ from the module functions (which the criteria
    would otherwise shadow inside ``matches``).
    """

    def matches(hwnd: int) -> bool:
        return (
            (pid is None or window_pid(hwnd) == pid)
            and (class_name is None or window_class(hwnd) == class_name)
            and (
                title_needle is None
                or title_needle.casefold() in window_text(hwnd).casefold()
            )
        )

    return [hwnd for hwnd in top_level_windows() if matches(hwnd)]


def find_marked_windows(
    unique_marker: str,
    *,
    pid: int | None = None,
    class_name: str | None = None,
) -> list[int]:
    """Find visible top-level windows carrying the run's UNIQUE title marker (D6).

    This is the ONLY sanctioned window-search primitive for attach (focusing,
    typing, closing) in this suite. Unlike :func:`find_windows`, a window whose
    title lacks the marker is INVISIBLE here even when its class/pid match — the
    user's own open Notepad/Calculator/Edge window can never be returned, because
    it cannot carry this run's marker. Callers that need to inspect windows they
    did not launch must not exist.
    """
    return find_windows(pid=pid, class_name=class_name, title_needle=unique_marker)


def _reject_ambiguous_windows(
    hwnds: list[int],
    what: str,
    unique_marker: str,
) -> None:
    """Fail closed when more than one marker-carrying window matches.

    Two windows with the same marker should be impossible (marker is run-unique),
    but a wrong-window discipline that silently picks the first match is exactly
    what D6 exists to prevent — so ambiguity is a loud error, never a guess.
    """
    if len(hwnds) > 1:
        titles = [window_text(hwnd) for hwnd in hwnds]
        raise RuntimeError(
            f"ambiguous attach for {what}: {len(hwnds)} windows carry marker "
            f"{unique_marker!r} ({titles}); refusing to pick one blindly."
        )


def wait_for_marked_window(
    deadline: Deadline,
    unique_marker: str,
    *,
    pid: int | None = None,
    class_name: str | None = None,
    timeout_s: float = 30.0,
) -> int:
    """Poll for a window carrying the run's unique title marker; fail loudly otherwise.

    D6 wrong-window defense: matches ONLY marker-carrying windows (see
    :func:`find_marked_windows`); a user's pre-existing same-application window is
    never a candidate, so a failure to launch/find OUR instance surfaces as a
    TimeoutError instead of silently attaching to the user's window. Raises
    RuntimeError on ambiguous matches (more than one marker window).
    """
    expiry = time.monotonic() + timeout_s
    while time.monotonic() < expiry:
        deadline.check(f"marked window marker~={unique_marker}")
        found = find_marked_windows(unique_marker, pid=pid, class_name=class_name)
        if found:
            _reject_ambiguous_windows(found, "wait_for_marked_window", unique_marker)
            return found[0]
        time.sleep(0.25)
    raise TimeoutError(
        f"No marker-carrying window appeared within {timeout_s}s "
        f"(marker={unique_marker!r}, pid={pid}, class={class_name}). The test's OWN "
        "instance never showed up; a same-app window WITHOUT the marker (e.g. the "
        "user's own) is deliberately invisible to this wait."
    )


def attach_window_by_unique_title(
    deadline: Deadline,
    unique_marker: str,
    *,
    pid: int | None = None,
    class_name: str | None = None,
    timeout_s: float = 30.0,
) -> int:
    """Attach (focus-target lookup) STRICTLY to the test's own unique-marker window.

    D6 pin target: this is the attach helper every e2e app-launch path must use.
    It rejects non-marker windows BY CONSTRUCTION (the enumeration predicate requires
    the marker in the title) and fails closed on zero or ambiguous matches — it can
    never hand back a window the suite did not itself title with this run's marker.
    """
    return wait_for_marked_window(
        deadline, unique_marker, pid=pid, class_name=class_name, timeout_s=timeout_s
    )


def wait_for_window(
    deadline: Deadline,
    *,
    pid: int | None = None,
    class_name: str | None = None,
    title_needle: str | None = None,
    timeout_s: float = 30.0,
) -> int:
    """Poll for a matching top-level window; raise TimeoutError when none appears."""
    expiry = time.monotonic() + timeout_s
    while time.monotonic() < expiry:
        deadline.check(f"window pid={pid} class={class_name} title~={title_needle}")
        found = find_windows(pid=pid, class_name=class_name, title_needle=title_needle)
        if found:
            return found[0]
        time.sleep(0.25)
    raise TimeoutError(
        f"No window appeared within {timeout_s}s (pid={pid}, class={class_name}, "
        f"title~={title_needle!r})."
    )


def child_controls(hwnd: int) -> list[dict[str, Any]]:
    """Enumerate visible child controls: class, text, and absolute (physical) rect."""
    controls: list[dict[str, Any]] = []
    protocol = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def on_child(child, _lparam) -> bool:  # type: ignore[no-untyped-def]
        child = int(child)
        if not is_visible(child):
            return True
        rect = wt.RECT()
        user32.GetWindowRect(child, ctypes.byref(rect))
        controls.append(
            {
                "hwnd": child,
                "cls": window_class(child),
                "text": window_text(child),
                "rect": (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top),
            }
        )
        return True

    user32.EnumChildWindows.argtypes = [ctypes.c_void_p, protocol, ctypes.c_void_p]
    user32.EnumChildWindows(hwnd, protocol(on_child), None)
    return controls


def read_edit_text(hwnd: int) -> str:
    """Concatenated WM_GETTEXT text of all Edit-class children (Notepad's edit area)."""
    chunks: list[str] = []
    for control in child_controls(hwnd):
        if control["cls"] != "Edit":
            continue
        length = user32.SendMessageW(control["hwnd"], WM_GETTEXTLENGTH, 0, None)
        if length <= 0:
            continue
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.SendMessageW(control["hwnd"], WM_GETTEXT, length + 1, buffer)
        chunks.append(buffer.value)
    return "\n".join(chunks)


def move_window(hwnd: int, x: int, y: int, width: int, height: int) -> None:
    """Deterministically move/resize a window (fault injection; never drag simulation)."""
    if not user32.MoveWindow(hwnd, x, y, width, height, True):
        raise OSError(f"MoveWindow failed for hwnd={hwnd}")


def focus_window(hwnd: int, attempts: int = 5) -> bool:
    """Bring a window to the foreground (AttachThreadInput trick); returns success."""
    for _ in range(attempts):
        if user32.SetForegroundWindow(hwnd) and int(user32.GetForegroundWindow() or 0) == hwnd:
            return True
        foreground = int(user32.GetForegroundWindow() or 0)
        if foreground == hwnd:
            return True
        this_thread = kernel32.GetCurrentThreadId()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        attached_a = attached_b = False
        try:
            if foreground_thread and foreground_thread != this_thread:
                attached_a = bool(user32.AttachThreadInput(this_thread, foreground_thread, True))
            if target_thread and target_thread != this_thread:
                attached_b = bool(user32.AttachThreadInput(this_thread, target_thread, True))
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached_a:
                user32.AttachThreadInput(this_thread, foreground_thread, False)
            if attached_b:
                user32.AttachThreadInput(this_thread, target_thread, False)
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            return True
        time.sleep(0.3)
    return int(user32.GetForegroundWindow() or 0) == hwnd


def close_window(hwnd: int, wait_s: float = 5.0) -> bool:
    """Politely close EXACTLY this hwnd with WM_CLOSE; returns True when it is gone.

    D6 hardening: the old implementation polled ``find_windows(class_name=...,
    title_needle=...)`` — same class AND same title as the target — which could
    observe some OTHER window (the user's own, restored under the same title) and
    misreport. The check is now window-identity-exact: this specific hwnd must
    stop being a visible window. Only ever called with an hwnd this suite found via
    its unique marker (or an enum the suite itself launched).
    """
    user32.PostMessageW(hwnd, WM_CLOSE, None, None)
    expiry = time.monotonic() + wait_s
    while time.monotonic() < expiry:
        if not window_exists(hwnd):
            return True
        time.sleep(0.2)
    return not window_exists(hwnd)


def window_exists(hwnd: int) -> bool:
    """True while hwnd is still a live Win32 window (used by exact-hwnd close waits)."""
    return bool(user32.IsWindow(hwnd))


def kill_process_tree(pid: int) -> None:
    """Kill a process this suite started (taskkill /T, argv list — no shell mangling)."""
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        capture_output=True,
        timeout=15,
        check=False,
    )


# --- Calculator (classic win32calc: CalcFrame) ----------------------------------------------

CALC_WINDOW_CLASS = "CalcFrame"

#: Classic Calculator client layout (label -> (grid column, grid row)); the tall "=" button
#: spans rows 4-5 and the wide "0" spans columns 0-1, expressed as (col, row, col_span, row_span).
CALC_BUTTON_LAYOUT: dict[str, tuple[int, int, int, int]] = {
    "MC": (0, 0, 1, 1), "MR": (1, 0, 1, 1), "MS": (2, 0, 1, 1), "M+": (3, 0, 1, 1), "M-": (4, 0, 1, 1),
    "<-": (0, 1, 1, 1), "CE": (1, 1, 1, 1), "C": (2, 1, 1, 1), "+/-": (3, 1, 1, 1), "sqrt": (4, 1, 1, 1),
    "7": (0, 2, 1, 1), "8": (1, 2, 1, 1), "9": (2, 2, 1, 1), "/": (3, 2, 1, 1), "%": (4, 2, 1, 1),
    "4": (0, 3, 1, 1), "5": (1, 3, 1, 1), "6": (2, 3, 1, 1), "*": (3, 3, 1, 1), "1/x": (4, 3, 1, 1),
    "1": (0, 4, 1, 1), "2": (1, 4, 1, 1), "3": (2, 4, 1, 1), "-": (3, 4, 1, 1), "=": (4, 4, 1, 2),
    "0": (0, 5, 2, 1), ".": (2, 5, 1, 1), "+": (3, 5, 1, 1),
}


def calc_button_grid(hwnd: int) -> dict[str, tuple[int, int]]:
    """Map Calculator button labels to absolute screen-center coordinates.

    Buttons are owner-drawn (no captions), so labels come from the documented classic
    layout; the grid origin/pitch is derived from the REAL enumerated button rects of the
    live window so coordinates always match the actual instance (charter: coordinates from
    a real observation, normalized via grounding).
    """
    ensure_dpi_awareness()  # physical-pixel rects (lazy, E6 finding D10)
    buttons = [c for c in child_controls(hwnd) if c["cls"] == "Button"]
    if not buttons:
        raise RuntimeError(f"No Button children found on Calculator window hwnd={hwnd}")
    unit_widths = sorted({b["rect"][2] for b in buttons})
    unit_heights = sorted({b["rect"][3] for b in buttons})
    unit_w = unit_widths[0]
    unit_h = unit_heights[0]
    small = [b for b in buttons if b["rect"][2] == unit_w and b["rect"][3] == unit_h]
    columns = sorted({b["rect"][0] for b in small})
    rows = sorted({b["rect"][1] for b in small})
    if len(columns) != 5 or len(rows) < 5:
        raise RuntimeError(
            f"Unexpected Calculator grid: cols={columns} rows={rows} (hwnd={hwnd}); "
            "the classic CalcFrame layout is required."
        )
    col_pitch = columns[1] - columns[0]

    def center(label: str) -> tuple[int, int]:
        col, row, col_span, row_span = CALC_BUTTON_LAYOUT[label]
        x = columns[0] + col * col_pitch + (unit_w * col_span) // 2
        y = rows[0] + row * (rows[1] - rows[0]) + (unit_h * row_span) // 2
        return (x, y)

    return {label: center(label) for label in CALC_BUTTON_LAYOUT}


def calc_display_values(hwnd: int) -> list[str]:
    """Non-empty Static control texts of the Calculator window (display + expression)."""
    return [c["text"].strip() for c in child_controls(hwnd) if c["cls"] == "Static" and c["text"].strip()]


def calc_display_value(hwnd: int) -> str:
    """The Calculator's main display: the last purely numeric/signed Static text."""
    import re

    numeric = re.compile(r"^-?\d[\d,]*\.?\d*$")
    values = [value for value in calc_display_values(hwnd) if numeric.match(value)]
    if not values:
        raise RuntimeError(f"Calculator display not found in {calc_display_values(hwnd)} (hwnd={hwnd})")
    return values[-1].replace(",", "")


def launch_gui(
    deadline: Deadline,
    argv: list[str],
    *,
    window_class: str | None = None,
    title_contains: str | None = None,
    timeout_s: float = 30.0,
) -> tuple[subprocess.Popen[bytes], int]:
    """Start a GUI app and wait for its window; returns (proc, hwnd).

    The Popen handle is the ONLY cleanup authority: cleanup kills exactly this process
    tree. For stub launchers (calc.exe) prefer launching the real binary directly so the
    tracked PID owns the window.

    D6 discipline: ``title_contains`` must be (or contain) the run's UNIQUE window
    token when the app titles its window from its file/URL — the hwnd is then found
    via the marker-only path (:func:`wait_for_marked_window`), so the wait can never
    latch onto the user's own same-application window.
    """
    ensure_dpi_awareness()  # physical-pixel waits/rects (lazy, E6 finding D10)
    proc = subprocess.Popen(argv)
    try:
        hwnd = wait_for_window(
            deadline,
            pid=proc.pid,
            class_name=window_class,
            title_needle=title_contains,
            timeout_s=timeout_s,
        )
    except Exception:
        kill_process_tree(proc.pid)
        raise
    return proc, hwnd


def unique_window_token(prefix: str = "cumcp-e2e") -> str:
    """Run-unique window token for D6 window isolation.

    Every app instance this suite opens carries this token in its window title
    (Notepad: the scratch FILENAME; Edge: the page <title>; Calculator: cannot set
    a title, so attach there is PID+class scoped to the process WE launched — see
    test_e2e_calculator.py). The token embeds pid, wall time AND a per-call counter,
    making a collision with any user window (or a second call in the same
    millisecond) impossible; markers logged to evidence let a user find and delete
    stray text from a past polluted run (search for the token).
    """
    global _WINDOW_TOKEN_COUNTER
    _WINDOW_TOKEN_COUNTER += 1
    return f"{prefix}-{os.getpid()}-{_WINDOW_TOKEN_COUNTER:03d}-{int(time.time() * 1000) % 100000000}"


def wait_until(deadline: Deadline, predicate: Callable[[], bool], what: str, timeout_s: float = 15.0) -> None:
    """Poll a predicate until true or timeout; fails fast on the per-test deadline."""
    expiry = time.monotonic() + timeout_s
    while time.monotonic() < expiry:
        deadline.check(what)
        if predicate():
            return
        time.sleep(0.2)
    raise TimeoutError(f"Condition not met within {timeout_s}s: {what}")
