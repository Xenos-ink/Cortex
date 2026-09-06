"""Real-Windows application lifecycle for benchmark ``env`` mode (this box only).

Lean duplication of the E2E suite's Win32 helpers on purpose: benchmarks/ is a standalone
harness that must not import from tests/. Only applications this harness LAUNCHED are
ever killed. All rect reads assume per-monitor-v2 DPI awareness (set at import), matching
the runtime backend's physical-pixel capture space.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes as wt
import os
import re
import subprocess
import time
from typing import Any

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_CLOSE = 0x0010
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

def set_dpi_awareness() -> None:
    """Set per-monitor-v2 awareness so rect reads are physical pixels.

    NOT called at import time: ``LocalComputerBackend`` must be the process's FIRST
    awareness setter (its fallback ladder downgrades a pre-set process to "system"),
    and importing this module must stay side-effect-free for the standard test suite.
    The runner calls this explicitly in ``--mode env`` before any app lifecycle runs.
    """
    with contextlib.suppress(Exception):
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor-v2

user32.GetForegroundWindow.restype = ctypes.c_void_p
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
user32.MoveWindow.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.BOOL
]
user32.MoveWindow.restype = wt.BOOL
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.SetForegroundWindow.restype = wt.BOOL
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p]


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


def top_level_windows() -> list[int]:
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


def child_controls(hwnd: int) -> list[dict[str, Any]]:
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


def find_window(
    *, pid: int | None = None, class_name: str | None = None, title_needle: str | None = None
) -> int | None:
    """Find one visible top-level window by pid/class/title (names avoid shadowing)."""
    for hwnd in top_level_windows():
        if pid is not None and window_pid(hwnd) != pid:
            continue
        if class_name is not None and window_class(hwnd) != class_name:
            continue
        if title_needle is not None and title_needle.casefold() not in window_text(hwnd).casefold():
            continue
        return hwnd
    return None


def wait_for_window(timeout_s: float, **criteria: Any) -> int:
    expiry = time.monotonic() + timeout_s
    while time.monotonic() < expiry:
        hwnd = find_window(**criteria)
        if hwnd is not None:
            return hwnd
        time.sleep(0.25)
    raise TimeoutError(f"window not found within {timeout_s}s: {criteria}")


def focus_window(hwnd: int, attempts: int = 5) -> bool:
    """Foreground a window via SetForegroundWindow (AttachThreadInput fallback)."""
    this_thread = kernel32.GetCurrentThreadId()
    for _ in range(attempts):
        if user32.SetForegroundWindow(hwnd) and int(user32.GetForegroundWindow() or 0) == hwnd:
            return True
        foreground = int(user32.GetForegroundWindow() or 0)
        if foreground == hwnd:
            return True
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
        time.sleep(0.3)
    return int(user32.GetForegroundWindow() or 0) == hwnd


def move_window(hwnd: int, x: int, y: int, width: int, height: int) -> None:
    if not user32.MoveWindow(hwnd, x, y, width, height, True):
        raise OSError(f"MoveWindow failed for hwnd={hwnd}")


def close_window(hwnd: int, wait_s: float = 6.0) -> None:
    user32.PostMessageW(hwnd, WM_CLOSE, None, None)
    time.sleep(min(wait_s, 1.0))
    del wait_s


def kill_process_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=15, check=False)


def read_edit_text(hwnd: int) -> str:
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


# --- Calculator (classic win32calc: CalcFrame) ------------------------------------------------

CALC_WINDOW_CLASS = "CalcFrame"
CALC_BUTTON_LAYOUT: dict[str, tuple[int, int, int, int]] = {
    "MC": (0, 0, 1, 1), "MR": (1, 0, 1, 1), "MS": (2, 0, 1, 1), "M+": (3, 0, 1, 1), "M-": (4, 0, 1, 1),
    "<-": (0, 1, 1, 1), "CE": (1, 1, 1, 1), "C": (2, 1, 1, 1), "+/-": (3, 1, 1, 1), "sqrt": (4, 1, 1, 1),
    "7": (0, 2, 1, 1), "8": (1, 2, 1, 1), "9": (2, 2, 1, 1), "/": (3, 2, 1, 1), "%": (4, 2, 1, 1),
    "4": (0, 3, 1, 1), "5": (1, 3, 1, 1), "6": (2, 3, 1, 1), "*": (3, 3, 1, 1), "1/x": (4, 3, 1, 1),
    "1": (0, 4, 1, 1), "2": (1, 4, 1, 1), "3": (2, 4, 1, 1), "-": (3, 4, 1, 1), "=": (4, 4, 1, 2),
    "0": (0, 5, 2, 1), ".": (2, 5, 1, 1), "+": (3, 5, 1, 1),
}


def calc_button_grid(hwnd: int) -> dict[str, tuple[int, int]]:
    buttons = [c for c in child_controls(hwnd) if c["cls"] == "Button"]
    if not buttons:
        raise RuntimeError(f"no Button children on Calculator hwnd={hwnd}")
    unit_w = min(b["rect"][2] for b in buttons)
    unit_h = min(b["rect"][3] for b in buttons)
    small = [b for b in buttons if b["rect"][2] == unit_w and b["rect"][3] == unit_h]
    columns = sorted({b["rect"][0] for b in small})
    rows = sorted({b["rect"][1] for b in small})
    if len(columns) != 5 or len(rows) < 5:
        raise RuntimeError(f"unexpected Calculator grid cols={columns} rows={rows}")
    col_pitch = columns[1] - columns[0]
    row_pitch = rows[1] - rows[0]

    def center(label: str) -> tuple[int, int]:
        col, row, col_span, row_span = CALC_BUTTON_LAYOUT[label]
        return (
            columns[0] + col * col_pitch + (unit_w * col_span) // 2,
            rows[0] + row * row_pitch + (unit_h * row_span) // 2,
        )

    return {label: center(label) for label in CALC_BUTTON_LAYOUT}


_DISPLAY_NUMERIC = re.compile(r"^-?\d[\d,]*\.?\d*$")


def calc_display_value(hwnd: int) -> str:
    values = [
        c["text"].strip()
        for c in child_controls(hwnd)
        if c["cls"] == "Static" and c["text"].strip() and _DISPLAY_NUMERIC.match(c["text"].strip())
    ]
    if not values:
        raise RuntimeError(f"Calculator display not found on hwnd={hwnd}")
    return values[-1].replace(",", "")


# --- app lifecycle -----------------------------------------------------------------------------

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


class LaunchedApp:
    """A GUI application this harness started; the only cleanup authority for it."""

    def __init__(self, proc: subprocess.Popen[bytes], hwnd: int, kind: str) -> None:
        self.proc = proc
        self.hwnd = hwnd
        self.kind = kind

    def close(self) -> None:
        with contextlib.suppress(Exception):
            close_window(self.hwnd, wait_s=2.0)
        if self.proc.poll() is None:
            kill_process_tree(self.proc.pid)


def launch_notepad(file_path: str, timeout_s: float = 30.0) -> LaunchedApp:
    proc = subprocess.Popen(["notepad.exe", file_path])
    hwnd = wait_for_window(
        timeout_s, pid=proc.pid, title_needle=os.path.basename(file_path)
    )
    focus_window(hwnd)
    return LaunchedApp(proc, hwnd, "notepad")


def launch_calculator(timeout_s: float = 30.0) -> LaunchedApp:
    win32calc = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "win32calc.exe")
    if not os.path.exists(win32calc):
        raise RuntimeError(f"classic Calculator binary not present: {win32calc}")
    proc = subprocess.Popen([win32calc])
    hwnd = wait_for_window(timeout_s, pid=proc.pid, class_name=CALC_WINDOW_CLASS)
    focus_window(hwnd)
    return LaunchedApp(proc, hwnd, "calculator")


def launch_edge(page_url: str, title_marker: str, timeout_s: float = 45.0) -> LaunchedApp:
    edge = next((path for path in EDGE_CANDIDATES if os.path.exists(path)), None)
    if edge is None:
        raise RuntimeError("Microsoft Edge not found on this box")
    proc = subprocess.Popen(
        [
            edge,
            "--new-window",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=msEdgeWelcome",
            "--window-size=1200,800",
            page_url,
        ]
    )
    hwnd = wait_for_window(timeout_s, pid=proc.pid, title_needle=title_marker)
    return LaunchedApp(proc, hwnd, "browser")
