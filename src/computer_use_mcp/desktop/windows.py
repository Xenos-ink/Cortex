"""Window/process queries, DPI awareness, monitor enumeration, and cursor position."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import os

from ..core.models import MonitorInfo, WindowInfo
from .errors import BackendError, DisplayUnavailableError
from .launch import AppWindowCandidate, doc_token_from_title, is_unsaved_candidate_title

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation-only names (lazy under __future__ annotations)
    from collections.abc import Sequence
from .win32 import (
    DEFAULT_DPI,
    DPI_AWARENESS_CONTEXT_PER_MONITOR_V2,
    GA_ROOT,
    IS_WINDOWS,
    MDT_EFFECTIVE_DPI,
    MONITORINFOEXW,
    MONITORINFOF_PRIMARY,
    PROCESS_QUERY_LIMITED_INFORMATION,
    _GUITHREADINFO,
    _MONITORENUMPROC,
    _kernel32,
    _shcore,
    _user32,
)


# --- interference-probe constants (T8; A12 mechanisms i-v) ----------------------------------
_GW_OWNER = 4  # GetWindow(): retrieves the window's owner (owned-dialog chains)
_DIALOG_WINDOW_CLASS = "#32770"  # system dialog class (Save As, Confirm Save As, ...)


def _set_process_dpi_awareness() -> str:
    """Set the process DPI awareness mode, strongest first; returns the recorded mode.

    Modes: ``per_monitor_v2``, ``per_monitor``, ``system``, ``unaware``, ``unavailable``.
    Never silent: the result is stored on the backend and feeds the DPI-estimated flag.
    When the process already has an awareness mode set by another component, the
    effective mode is queried instead of blindly reporting failure.
    """
    if not IS_WINDOWS or _user32 is None:
        return "unavailable"
    set_context = getattr(_user32, "SetProcessDpiAwarenessContext", None)
    if set_context is not None:
        try:
            if set_context(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_V2)):
                return "per_monitor_v2"
        except OSError:
            pass
    try:
        if _shcore is not None and _shcore.SetProcessDpiAwareness(2) == 0:
            return "per_monitor"
    except (OSError, AttributeError):
        pass
    try:
        if _user32.SetProcessDPIAware():
            return "system"
    except (OSError, AttributeError):
        pass
    return _query_current_dpi_awareness()


def _query_current_dpi_awareness() -> str:
    """Report the DPI awareness the process already runs under (already-set case)."""
    if not IS_WINDOWS or _user32 is None:
        return "unavailable"
    get_context = getattr(_user32, "GetThreadDpiAwarenessContext", None)
    get_awareness = getattr(_user32, "GetAwarenessFromDpiAwarenessContext", None)
    if get_context is None or get_awareness is None:
        return "unavailable"
    try:
        context = get_context()
        if not context:
            return "unavailable"
        return {0: "unaware", 1: "system", 2: "per_monitor"}.get(int(get_awareness(context)), "unavailable")
    except (OSError, AttributeError):
        return "unavailable"


def _window_text(hwnd: int) -> str:
    try:
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value
    except (OSError, AttributeError):
        return ""


def _window_class_name(hwnd: int) -> str | None:
    try:
        buffer = ctypes.create_unicode_buffer(256)
        if _user32.GetClassNameW(hwnd, buffer, 256):
            return buffer.value or None
        return None
    except (OSError, AttributeError):
        return None


def _window_pid(hwnd: int) -> int | None:
    try:
        pid = ctypes.wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value or None
    except (OSError, AttributeError):
        return None


def _process_image_path(pid: int) -> str | None:
    """Executable path via OpenProcess + QueryFullProcessImageNameW; closes the handle."""
    if _kernel32 is None:
        return None
    handle = None
    try:
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        size = ctypes.wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value or None
        return None
    except (OSError, AttributeError):
        return None
    finally:
        if handle:
            try:
                _kernel32.CloseHandle(handle)
            except (OSError, AttributeError):
                pass


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Window rect as ``(left, top, width, height)`` in physical pixels (DPI-aware process)."""
    try:
        rect = ctypes.wintypes.RECT()
        if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
        return None
    except (OSError, AttributeError):
        return None


def query_foreground_window() -> WindowInfo | None:
    """Strong identity of the foreground window's root owner; None when unavailable.

    Populates hwnd (root owner via ``GetAncestor(GA_ROOT)``), pid, process name (exe
    basename), exe path, window class, title, and physical bounds. Individual field
    failures degrade to None instead of crashing observation.
    """
    if not IS_WINDOWS or _user32 is None:
        return None
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return None
        root = _user32.GetAncestor(hwnd, GA_ROOT) or hwnd
        root = int(root)
    except (OSError, AttributeError):
        return None
    pid = _window_pid(root)
    exe_path = _process_image_path(pid) if pid else None
    return WindowInfo(
        hwnd=root,
        pid=pid,
        process_name=os.path.basename(exe_path) if exe_path else None,
        exe_path=exe_path,
        window_class=_window_class_name(root),
        title=_window_text(root),
        bounds=_window_rect(root),
    )


def _title_match_rank(title: str, needle: str) -> int | None:
    """Match rank of a window title against a casefolded needle, or None.

    Precedence (higher wins): 3 = exact, 2 = prefix, 1 = substring — all
    case-insensitive. Callers keep the FIRST candidate of the highest rank (Z-order).
    """
    folded = title.casefold()
    if folded == needle:
        return 3
    if folded.startswith(needle):
        return 2
    if needle in folded:
        return 1
    return None


def find_window_by_title(title: str) -> WindowInfo | None:
    """Find a top-level window by title; None when unavailable or unmatched.

    Enumerates top-level windows via ``EnumWindows`` (first in Z-order wins within a
    match class), resolves each to its root owner via ``GetAncestor(GA_ROOT)``, and
    matches titles case-insensitively with precedence exact > prefix > substring. The
    winner is populated exactly like :func:`query_foreground_window` (pid, process name,
    exe path, window class, title, physical bounds). Reads the module-level ``_user32``
    binding at call time so tests can monkeypatch it without a real desktop. Backends
    without window enumeration return ``None`` (``ComputerBackend.find_window_by_title``
    default).
    """
    if not IS_WINDOWS or _user32 is None:
        return None
    needle = (title or "").strip().casefold()
    if not needle:
        return None
    candidates: list[tuple[int, str]] = []  # (root hwnd, title), EnumWindows Z-order

    def _on_window(hwnd: object, _lparam: object) -> bool:
        try:
            root = int(_user32.GetAncestor(int(hwnd), GA_ROOT) or int(hwnd))  # type: ignore[union-attr]
        except (OSError, AttributeError, TypeError, ValueError):
            return True  # keep enumerating; an unreadable window is simply not a candidate
        text = _window_text(root)
        if text:
            candidates.append((root, text))
        return True

    enum_proc = ctypes.WINFUNCTYPE(
        ctypes.wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p
    )(_on_window)
    try:
        _user32.EnumWindows(enum_proc, 0)
    except (OSError, AttributeError):
        return None
    best: tuple[int, int, int] | None = None  # (rank, z_index, hwnd)
    for z_index, (hwnd, window_title) in enumerate(candidates):
        rank = _title_match_rank(window_title, needle)
        if rank is None:
            continue
        if best is None or rank > best[0]:  # strictly greater: first-in-Z-order wins ties
            best = (rank, z_index, hwnd)
    if best is None:
        return None
    hwnd = best[2]
    pid = _window_pid(hwnd)
    exe_path = _process_image_path(pid) if pid else None
    return WindowInfo(
        hwnd=hwnd,
        pid=pid,
        process_name=os.path.basename(exe_path) if exe_path else None,
        exe_path=exe_path,
        window_class=_window_class_name(hwnd),
        title=_window_text(hwnd),
        bounds=_window_rect(hwnd),
    )


def get_cursor_position() -> tuple[int, int] | None:
    """Physical virtual-screen cursor position via ``GetCursorPos``; None when unavailable."""
    if not IS_WINDOWS or _user32 is None:
        return None
    try:
        point = ctypes.wintypes.POINT()
        if _user32.GetCursorPos(ctypes.byref(point)):
            return (point.x, point.y)
        return None
    except (OSError, AttributeError):
        return None


# --- interference probes (T8; A12 mechanisms i-v, read-only, sub-ms) -------------------------


def query_focus_target() -> dict[str, object] | None:
    """Who would receive keyboard input RIGHT NOW: ``GetGUIThreadInfo`` focus target.

    Returns ``{"hwnd_focus", "root_hwnd", "window_class", "text", "pid",
    "root_window_class", "process_name"}`` for the foreground thread's focused control
    (mechanism iv's cheap, exact probe), or ``None`` when unavailable (non-Windows, no
    foreground, or a failed query — callers treat ``None`` as "cannot verify" and leave
    the check inert rather than guessing). ``window_class`` is the FOCUSED CONTROL's
    class (e.g. "Edit" inside a dialog); ``root_window_class`` is the focus ROOT
    window's class (e.g. "#32770" for the hosting Run dialog) and ``process_name`` the
    root's process — the B10 anchoring semantics key on the ROOT surface, not the
    focused control.
    """
    if not IS_WINDOWS or _user32 is None:
        return None
    try:
        foreground = int(_user32.GetForegroundWindow() or 0)
        if not foreground:
            return None
        thread_id = int(_user32.GetWindowThreadProcessId(ctypes.c_void_p(foreground), None) or 0)
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(_GUITHREADINFO)
        # idThread=0 asks for the foreground thread; an explicit id is equally valid.
        if not _user32.GetGUIThreadInfo(ctypes.wintypes.DWORD(thread_id), ctypes.byref(info)):
            return None
        hwnd_focus = int(info.hwndFocus or info.hwndActive or 0)
        if not hwnd_focus:
            return None
        root = int(_user32.GetAncestor(ctypes.c_void_p(hwnd_focus), GA_ROOT) or hwnd_focus)
        pid = _window_pid(root)
        exe_path = _process_image_path(pid) if pid else None
        return {
            "hwnd_focus": hwnd_focus,
            "root_hwnd": root,
            "window_class": _window_class_name(hwnd_focus),
            "text": _window_text_via_message(hwnd_focus),
            "pid": pid,
            "root_window_class": _window_class_name(root),
            "process_name": os.path.basename(exe_path) if exe_path else None,
        }
    except (OSError, AttributeError, ValueError):
        return None


def is_window_owned_by(hwnd: int | None, ancestor_hwnd: int | None) -> bool:
    """Whether ``hwnd``'s GW_OWNER chain roots at ``ancestor_hwnd`` (SHARED ownership rule).

    The ONE ownership computation for the whole guard: DialogSentinel classifies a
    foreground window as an owned dialog with it, and the pre-dispatch FocusGuard
    exempts owner-chained same-target windows with it — the two can never diverge
    again (B11/A7b: the sentinel matched an Excel `bosa_sdm_XL9` owned modal as
    `owner_chain` while the guard's class-keyed check rejected the same window).
    Read-only, bounded, never raises (False on any failure).
    """
    if hwnd is None or ancestor_hwnd is None or not IS_WINDOWS or _user32 is None:
        return False
    try:
        if not _user32.IsWindow(ctypes.c_void_p(int(hwnd))) or not _user32.IsWindow(
            ctypes.c_void_p(int(ancestor_hwnd))
        ):
            return False
        return int(ancestor_hwnd) in _owner_chain_roots(int(hwnd))
    except (OSError, AttributeError, TypeError, ValueError):
        return False


def _owner_chain_roots(hwnd: int, limit: int = 8) -> list[int]:
    """Root hwnds of the owner chain of ``hwnd`` (GetWindow(GW_OWNER), bounded)."""
    chain: list[int] = []
    current = hwnd
    for _ in range(limit):
        try:
            owner = int(_user32.GetWindow(ctypes.c_void_p(current), _GW_OWNER) or 0)  # type: ignore[union-attr]
        except (OSError, AttributeError, TypeError, ValueError):
            break
        if not owner:
            break
        try:
            root = int(_user32.GetAncestor(ctypes.c_void_p(owner), GA_ROOT) or owner)  # type: ignore[union-attr]
        except (OSError, AttributeError, TypeError, ValueError):
            root = owner
        chain.append(root)
        current = owner
    return chain


def detect_system_dialog(
    bound_hwnd: int | None, title_table: Sequence[str] | None = None
) -> dict[str, object] | None:
    """Detect a modal/system dialog holding the foreground (T8 mechanism iii).

    Cheap class/owner/title query — NO UIA, NO screenshots. A dialog is reported when
    the foreground root window differs from ``bound_hwnd`` (when provided) AND any of:

    - its window class is the system dialog class ``#32770``;
    - its owner chain reaches ``bound_hwnd`` (an owned popup of the session target);
    - its title matches one of the configured dialog-title conventions.

    Returns ``{"hwnd", "owner_hwnd", "title", "window_class", "pid", "matched"}`` or
    ``None`` (no dialog / nothing detectable).
    """
    if not IS_WINDOWS or _user32 is None:
        return None
    try:
        foreground = int(_user32.GetForegroundWindow() or 0)
        if not foreground:
            return None
        root = int(_user32.GetAncestor(ctypes.c_void_p(foreground), GA_ROOT) or foreground)
        if bound_hwnd is not None and root == int(bound_hwnd):
            return None  # the session target itself is foreground: no interloper
        window_class = _window_class_name(root) or ""
        title = _window_text(root)
        matched: str | None = None
        if window_class == _DIALOG_WINDOW_CLASS:
            matched = "class"
        else:
            if bound_hwnd is not None and is_window_owned_by(root, int(bound_hwnd)):
                # B11: the SAME shared ownership helper the pre-dispatch guard uses —
                # sentinel and guard can never diverge on ownership again.
                matched = "owner_chain"
            if matched is None:
                folded = title.casefold()
                for needle in title_table or ():
                    text = str(needle).strip().casefold()
                    if text and text in folded:
                        matched = "title"
                        break
        if matched is None:
            return None
        return {
            "hwnd": root,
            "owner_hwnd": (_owner_chain_roots(root, limit=1) or [0])[0],
            "title": title,
            "window_class": window_class,
            "pid": _window_pid(root),
            "matched": matched,
        }
    except (OSError, AttributeError, ValueError):
        return None


def enumerate_app_windows(process_name: str) -> list[AppWindowCandidate]:
    """Visible top-level windows of ``process_name`` for attach-or-launch (mechanism ii).

    EnumWindows (Z-order), filtered to visible top-level windows whose pid resolves to
    the given process basename (case-insensitive, ``.exe``-tolerant). Each candidate
    carries its derived doc token and the generic unsaved-candidate flag. Non-Windows
    or an empty needle returns ``[]``.
    """
    if not IS_WINDOWS or _user32 is None:
        return []
    needle = (process_name or "").strip().casefold().removesuffix(".exe")
    if not needle:
        return []
    candidates: list[AppWindowCandidate] = []

    def _on_window(hwnd: object, _lparam: object) -> bool:
        try:
            handle = int(hwnd)  # type: ignore[arg-type]
            if not _user32.IsWindow(ctypes.c_void_p(handle)) or not _user32.IsWindowVisible(
                ctypes.c_void_p(handle)
            ):
                return True
            root = int(_user32.GetAncestor(ctypes.c_void_p(handle), GA_ROOT) or handle)
            pid = _window_pid(root)
            if not pid:
                return True
            exe_path = _process_image_path(pid)
            base = os.path.basename(exe_path) if exe_path else ""
            if base.casefold().removesuffix(".exe") != needle:
                return True
            title = _window_text(root)
            window = WindowInfo(
                hwnd=root,
                pid=pid,
                process_name=base or None,
                exe_path=exe_path,
                window_class=_window_class_name(root),
                title=title,
                bounds=_window_rect(root),
            )
            candidates.append(
                AppWindowCandidate(
                    window=window,
                    doc_token=doc_token_from_title(title),
                    unsaved_candidate=is_unsaved_candidate_title(title),
                )
            )
        except (OSError, AttributeError, TypeError, ValueError):
            return True  # an unreadable window is simply not a candidate
        return True

    enum_proc = ctypes.WINFUNCTYPE(
        ctypes.wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p
    )(_on_window)
    try:
        _user32.EnumWindows(enum_proc, 0)
    except (OSError, AttributeError):
        return []
    return candidates


#: Modifier key names swept before a chord dispatch (HotkeyGuard, mechanism v).
_MODIFIER_KEY_NAMES: dict[str, int] = {
    "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
}


def query_stuck_modifiers(keys: Sequence[str]) -> list[str]:
    """Modifiers currently HELD DOWN (GetAsyncKeyState high bit) among ``keys`` + base set.

    The sweep covers the chord's own modifiers plus ctrl/alt/shift/win (A12 mechanism
    v): an untracked held modifier rewrites the chord's meaning (ctrl down + "s" is
    save; plain "s" is a letter into a cell). Returns canonical key NAMES; empty when
    nothing is stuck or the probe is unavailable.
    """
    sweep: set[str] = set()
    for key in keys:
        name = str(key).strip().casefold()
        if name in _MODIFIER_KEY_NAMES:
            sweep.add(name)
    sweep.update(_MODIFIER_KEY_NAMES)
    if not IS_WINDOWS or _user32 is None:
        return []
    stuck: list[str] = []
    for name in sorted(sweep):
        try:
            state = int(_user32.GetAsyncKeyState(int(_MODIFIER_KEY_NAMES[name])))
        except (OSError, AttributeError, TypeError, ValueError):
            continue
        if state & 0x8000:
            stuck.append(name)
    return stuck


def _system_dpi_scale() -> float:
    """System DPI scale via ``GetDpiForSystem``; 1.0 when it cannot be queried."""
    try:
        get_dpi = getattr(_user32, "GetDpiForSystem", None)
        if get_dpi is not None:
            dpi = int(get_dpi())
            if dpi > 0:
                return dpi / DEFAULT_DPI
    except (OSError, AttributeError, ValueError):
        pass
    return 1.0


def _monitor_is_primary(hmonitor: int) -> bool:
    try:
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if _user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            return bool(info.dwFlags & MONITORINFOF_PRIMARY)
    except (OSError, AttributeError, ValueError):
        pass
    return False


def _monitors_via_enum_display_monitors(allow_per_monitor_dpi: bool) -> tuple[list[MonitorInfo], bool]:
    """Enumerate monitors via EnumDisplayMonitors; per-monitor DPI via GetDpiForMonitor.

    Returns ``(monitors, dpi_estimated)`` where ``dpi_estimated`` is True when any
    monitor's scale came from the system fallback instead of per-monitor query.
    """
    hmonitors: list[int] = []
    rects: list[tuple[int, int, int, int]] = []

    def _on_monitor(hmonitor, _hdc, lprect, _data) -> bool:  # type: ignore[no-untyped-def]
        # Callback order is (HMONITOR, HDC, LPRECT, LPARAM) — matches mss's prototype.
        if lprect:
            rect = lprect.contents
            rects.append((rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top))
            hmonitors.append(int(hmonitor) if hmonitor is not None else len(hmonitors))
        return True

    callback = _MONITORENUMPROC(_on_monitor)
    try:
        if not _user32.EnumDisplayMonitors(None, None, callback, None):
            return [], False
    except (OSError, AttributeError):
        return [], False
    monitors: list[MonitorInfo] = []
    dpi_estimated = False
    for index, hmonitor in enumerate(hmonitors):
        scale_x: float | None = None
        scale_y: float | None = None
        if allow_per_monitor_dpi and _shcore is not None:
            dpi_x, dpi_y = ctypes.c_uint(), ctypes.c_uint()
            try:
                ok = _shcore.GetDpiForMonitor(
                    hmonitor, MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)
                ) == 0
            except (OSError, AttributeError):
                ok = False
            if ok and dpi_x.value > 0 and dpi_y.value > 0:
                scale_x, scale_y = dpi_x.value / DEFAULT_DPI, dpi_y.value / DEFAULT_DPI
        if scale_x is None or scale_y is None:
            scale_x = scale_y = _system_dpi_scale()
            dpi_estimated = True
        monitors.append(
            MonitorInfo(
                id=f"monitor-{index}",
                index=index,
                bounds=rects[index],
                is_primary=_monitor_is_primary(hmonitor),
                dpi_scale_x=scale_x,
                dpi_scale_y=scale_y,
            )
        )
    return monitors, dpi_estimated


def _monitors_via_mss_list(mss_monitors: list[dict[str, int]] | None) -> list[MonitorInfo]:
    """Fallback monitor list from mss's monitors (index 0 is the virtual-desktop union)."""
    if not mss_monitors:
        return []
    per_monitor = list(mss_monitors)[1:] or list(mss_monitors)
    system_scale = _system_dpi_scale()
    return [
        MonitorInfo(
            id=f"monitor-{index}",
            index=index,
            bounds=(monitor["left"], monitor["top"], monitor["width"], monitor["height"]),
            is_primary=(index == 0),
            dpi_scale_x=system_scale,
            dpi_scale_y=system_scale,
        )
        for index, monitor in enumerate(per_monitor)
    ]


def enumerate_monitors(
    mss_monitors: list[dict[str, int]] | None = None,
    *,
    per_monitor_dpi: bool = True,
) -> tuple[list[MonitorInfo], bool]:
    """Enumerate physical monitors with bounds, primary flag, and DPI scale.

    Prefers ``EnumDisplayMonitors`` + ``GetDpiForMonitor``; falls back to the supplied
    mss monitors list (skipping its virtual-desktop union entry at index 0) plus
    ``GetDpiForSystem``, marking DPI as estimated in that case (fail-closed for the
    ``scaled`` verdict). ``per_monitor_dpi=False`` skips per-monitor queries outright
    (system-aware processes cannot trust them). Raises ``DisplayUnavailableError`` when
    nothing can be enumerated.
    """
    if not IS_WINDOWS or _user32 is None:
        raise DisplayUnavailableError("Monitor enumeration requires Windows.")
    monitors, dpi_estimated = _monitors_via_enum_display_monitors(per_monitor_dpi)
    if not monitors:
        monitors = _monitors_via_mss_list(mss_monitors)
        if not monitors:
            raise DisplayUnavailableError("No monitors could be enumerated on this system.")
        dpi_estimated = True
    return monitors, dpi_estimated


def _select_target_monitor(
    monitors: list[MonitorInfo],
    cursor_physical: tuple[int, int] | None,
    preferred_index: int | None,
) -> MonitorInfo:
    """Default capture targeting: monitor containing the cursor, else primary, else first.

    This is the documented smallest-robust-choice default; ``preferred_index`` overrides.
    """
    if not monitors:
        raise DisplayUnavailableError("No monitors available for capture.")
    if preferred_index is not None:
        try:
            return monitors[preferred_index]
        except IndexError as exc:
            raise BackendError(f"Monitor index {preferred_index} is out of range.") from exc
    if cursor_physical is not None:
        x, y = cursor_physical
        for monitor in monitors:
            left, top, width, height = monitor.bounds
            if left <= x < left + width and top <= y < top + height:
                return monitor
    for monitor in monitors:
        if monitor.is_primary:
            return monitor
    return monitors[0]


# Bottom-of-module import: text_readers' Win32 reader reads this module's class/rect
# queries and query_focus_target above reads its WM_GETTEXT reader — call-time-only
# mutual use, resolved by importing each side after both modules are defined.
from .text_readers import _window_text_via_message  # noqa: E402

