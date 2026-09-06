"""Windows platform backend: identity, monitors/DPI, coordinate integrity, stop-checked input.

Conventions (binding for the runtime):
- Screenshot pixels are *screenshot-local*: ``(0, 0)`` is the top-left of the captured
  image. Action points validated by the runtime live in this space.
- Physical input coordinates are *virtual-screen pixels* (``SetCursorPos`` convention):
  secondary monitors may have negative origins. ``MonitorInfo.bounds`` uses the same
  ``(left, top, width, height)`` virtual-screen convention (mss convention).
- ``Observation.cursor_x/y`` are screenshot-local: the physical ``GetCursorPos`` position
  localized onto the captured monitor through the verified transform (they may be
  negative when the cursor sits outside the captured monitor).
- ``coordinate_scale_x/y`` are *input pixels per screenshot pixel*: multiply screenshot
  coordinates by the scale to obtain physical coordinates.

Coordinate-space integrity (``classify_coordinate_space``) is decided by measurement
first (screenshot dims vs. target-monitor physical dims): equal means
``verified_passthrough``; otherwise a uniform ratio that matches the monitor's queried
per-monitor DPI scale means ``scaled``; anything else (including DPI read from the
system fallback, i.e. estimated) is ``unverifiable`` and the executor refuses coordinate
input (fail closed).

Coordinate-transform invariant (F1, binding for every layer): EXACTLY ONE
screenshot-to-physical transform exists in the pipeline and it lives HERE —
``ComputerBackend._map_to_physical`` (``physical = origin + screenshot * scale`` via
:class:`CoordinateTransform`), applied exactly once at execution. Grounding validates
bounds in screenshot space and records the verified scale on the grounding result but
NEVER rewrites the point; no consumer may pre-scale or re-scale a point, or the executed
position lands at ``origin + screenshot * scale**2``.

The module imports cleanly on non-Windows: all Win32 calls are guarded by
``IS_WINDOWS`` and input libraries are imported lazily inside
:class:`LocalComputerBackend`. The Win32 entry points read the module-level
``_user32``/``_kernel32``/``_shcore`` bindings at call time so tests can monkeypatch
them without a real desktop.
"""

from __future__ import annotations

import base64
import ctypes
import io
import os
import platform
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from PIL import Image

from .models import CoordinateSpace, GroundedAction, MonitorInfo, Observation, WindowInfo
from .state import StopToken

IS_WINDOWS = platform.system() == "Windows"

GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MDT_EFFECTIVE_DPI = 0
MONITORINFOF_PRIMARY = 1
DEFAULT_DPI = 96
DPI_AWARENESS_CONTEXT_PER_MONITOR_V2 = -4
WAIT_MAX_SECONDS = 10.0
WAIT_SLICE_SECONDS = 0.1
_TYPE_INTERVAL_SECONDS = 0.01
_DRAG_SEGMENT_PIXELS = 40  # stroke interpolation granularity (~one segment per 40 px)
_DRAG_MIN_SEGMENTS = 4  # even tiny drags get a visibly interpolated stroke
_DRAG_STEP_PAUSE_SECONDS = 0.01  # per-segment pacing (same cadence as type)
_SCALE_TOLERANCE = 0.02
_MOVE_SETTLE_SECONDS = 0.05  # settle after a cursor reposition (not an input pause)
_SW_RESTORE = 9  # ShowWindow(nCmdShow) restore for a minimized window
_VK_MENU = 0x12  # ALT virtual key (foreground-switch nudge)
_KEYEVENTF_KEYUP = 0x02  # keybd_event flag: key release

if IS_WINDOWS:
    import ctypes.wintypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    try:
        _shcore = ctypes.windll.shcore
    except (AttributeError, OSError):  # pragma: no cover - pre-Win8.1 only
        _shcore = None

    class MONITORINFOEXW(ctypes.Structure):
        """``MONITORINFOEXW`` layout: bounds, work area, primary flag, device name."""

        _fields_ = [
            ("cbSize", ctypes.wintypes.DWORD),
            ("rcMonitor", ctypes.wintypes.RECT),
            ("rcWork", ctypes.wintypes.RECT),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("szDevice", ctypes.wintypes.WCHAR * 32),
        ]

    _MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.wintypes.BOOL,
        ctypes.c_void_p,  # hmonitor
        ctypes.c_void_p,  # hdc
        ctypes.POINTER(ctypes.wintypes.RECT),
        ctypes.c_void_p,  # dwData
    )

    # 64-bit safety: handle-sized return values must not be truncated to c_int.
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _user32.GetAncestor.restype = ctypes.c_void_p
    _user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
    _user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.RECT)]
    _user32.GetWindowRect.restype = ctypes.wintypes.BOOL
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(ctypes.wintypes.POINT)]
    _user32.GetCursorPos.restype = ctypes.wintypes.BOOL
    _user32.EnumDisplayMonitors.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        _MONITORENUMPROC,
        ctypes.c_void_p,
    ]
    _user32.EnumDisplayMonitors.restype = ctypes.wintypes.BOOL
    _user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFOEXW)]
    _user32.GetMonitorInfoW.restype = ctypes.wintypes.BOOL
    _user32.GetDpiForSystem.restype = ctypes.c_uint
    _user32.SetProcessDPIAware.restype = ctypes.wintypes.BOOL
    _user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    _user32.SetProcessDpiAwarenessContext.restype = ctypes.wintypes.BOOL
    _user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    _user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    _user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_uint
    _kernel32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.wintypes.DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = ctypes.wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    if _shcore is not None:
        _shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        _shcore.SetProcessDpiAwareness.restype = ctypes.HRESULT
        _shcore.GetDpiForMonitor.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        _shcore.GetDpiForMonitor.restype = ctypes.HRESULT
else:  # non-Windows: bindings stay absent, module stays importable for tests
    _user32 = None
    _kernel32 = None
    _shcore = None


class BackendError(RuntimeError):
    """Base class for backend failures the controller can classify (Goal.md section 8)."""


class InputBlockedError(BackendError):
    """Physical input was blocked (pyautogui failsafe corner, blocked desktop, fake block).

    Recovery mapping hint: ``FailureClass.BLOCKED_UI``.
    """


class DisplayUnavailableError(BackendError):
    """Screen capture or monitor enumeration failed.

    Recovery mapping hint: ``FailureClass.UNRECOVERABLE`` (or ``APP_CRASH`` when
    transient capture failures repeat).
    """


class CoordinateSpaceError(BackendError):
    """Coordinate-space integrity could not be verified, so coordinate input was refused.

    Recovery mapping hint: re-observe and re-ground (``FailureClass.STALE_COORDINATES``).
    """


class UnsupportedActionError(BackendError):
    """The backend received an action type it cannot execute."""


class WindowFocusError(BackendError):
    """A focus_window action could not resolve or activate the requested window.

    Deliberately NOT mapped into any recovery name-set: it classifies as
    ``FailureClass.UNKNOWN`` (bounded, fail-closed) like any unrecognized backend
    failure. Documented residual: the previously-focused window is not restored on a
    refusal — the caller re-observes and re-decides from actual state.
    """


@dataclass(frozen=True)
class CoordinateTransform:
    """Maps screenshot-local pixels to/from physical virtual-screen pixels for one monitor.

    ``physical = origin + screenshot * scale`` where ``origin`` is the monitor's
    virtual-screen ``(left, top)`` and ``scale`` is input pixels per screenshot pixel.
    This is the ONLY screenshot-to-physical transform in the pipeline (see module
    docstring invariant): it is owned by the backend and applied exactly once at
    execution. Values are rounded to whole pixels (round-half-even, matching
    ``int(round(...))``).
    """

    origin_x: int = 0
    origin_y: int = 0
    scale_x: float = 1.0
    scale_y: float = 1.0

    def to_physical(self, x: float, y: float) -> tuple[int, int]:
        """Convert screenshot-local pixels to physical virtual-screen pixels."""
        return (
            round(self.origin_x + x * self.scale_x),
            round(self.origin_y + y * self.scale_y),
        )

    def to_screenshot(self, x: float, y: float) -> tuple[int, int]:
        """Convert physical virtual-screen pixels to screenshot-local pixels."""
        return (
            round((x - self.origin_x) / self.scale_x),
            round((y - self.origin_y) / self.scale_y),
        )

    @classmethod
    def from_observation(cls, observation: Observation) -> CoordinateTransform:
        """Build the transform recorded on an observation; identity when no monitor."""
        monitor = observation.monitor
        if monitor is None:
            return cls()
        return cls(
            origin_x=monitor.bounds[0],
            origin_y=monitor.bounds[1],
            scale_x=observation.coordinate_scale_x,
            scale_y=observation.coordinate_scale_y,
        )


@dataclass(frozen=True)
class CoordinateVerdict:
    """Result of coordinate-space integrity classification for one capture."""

    space: CoordinateSpace
    scale_x: float
    scale_y: float
    input_width: int
    input_height: int


def classify_coordinate_space(
    screenshot_width: int,
    screenshot_height: int,
    monitor: MonitorInfo,
    *,
    dpi_estimated: bool = False,
) -> CoordinateVerdict:
    """Classify screenshot-vs-input coordinate integrity for a captured monitor.

    Measurement first: screenshot dims equal to the monitor's physical dims means
    ``VERIFIED_PASSTHROUGH``. Otherwise a uniform ratio matching the monitor's queried
    per-monitor DPI scale (within tolerance, DPI not estimated) means ``SCALED`` with the
    measured scale stored. Everything else is ``UNVERIFIABLE`` (fail closed).
    """
    monitor_width, monitor_height = monitor.bounds[2], monitor.bounds[3]
    if monitor_width <= 0 or monitor_height <= 0:
        return CoordinateVerdict(
            space=CoordinateSpace.UNVERIFIABLE,
            scale_x=1.0,
            scale_y=1.0,
            input_width=max(screenshot_width, 1),
            input_height=max(screenshot_height, 1),
        )
    input_width, input_height = monitor_width, monitor_height
    if screenshot_width == input_width and screenshot_height == input_height:
        return CoordinateVerdict(
            space=CoordinateSpace.VERIFIED_PASSTHROUGH,
            scale_x=1.0,
            scale_y=1.0,
            input_width=input_width,
            input_height=input_height,
        )
    scale_x = input_width / screenshot_width
    scale_y = input_height / screenshot_height
    uniform = abs(scale_x - scale_y) <= _SCALE_TOLERANCE
    matches_dpi = uniform and not dpi_estimated and all(
        abs(measured - expected) <= max(_SCALE_TOLERANCE, abs(expected) * _SCALE_TOLERANCE)
        for measured, expected in (
            (scale_x, monitor.dpi_scale_x),
            (scale_y, monitor.dpi_scale_y),
        )
    )
    if matches_dpi:
        return CoordinateVerdict(
            space=CoordinateSpace.SCALED,
            scale_x=scale_x,
            scale_y=scale_y,
            input_width=input_width,
            input_height=input_height,
        )
    return CoordinateVerdict(
        space=CoordinateSpace.UNVERIFIABLE,
        scale_x=scale_x,
        scale_y=scale_y,
        input_width=input_width,
        input_height=input_height,
    )


def interruptible_wait(seconds: float, stop: StopToken | None = None) -> float:
    """Sleep sliced into 100 ms slices, aborting early when ``stop`` fires.

    Returns the number of seconds actually waited (slice-granular when interrupted).
    Never raises: a fired stop simply ends the wait early; callers check the token.
    """
    if seconds <= 0:
        return 0.0
    if stop is None:
        time.sleep(seconds)
        return seconds
    remaining = seconds
    waited = 0.0
    while remaining > 0:
        slice_seconds = min(WAIT_SLICE_SECONDS, remaining)
        if stop.wait(slice_seconds):
            return waited + slice_seconds
        waited += slice_seconds
        remaining -= slice_seconds
    return waited


def _sleep_for_wait_action(total_seconds: float, stop: StopToken) -> None:
    """Slice-sleep for ``wait`` actions; raises ``TaskStopped`` as soon as stop fires."""
    remaining = total_seconds
    while remaining > 0:
        stop.ensure_live()
        slice_seconds = min(WAIT_SLICE_SECONDS, remaining)
        if stop.wait(slice_seconds):
            stop.ensure_live()  # stop fired mid-slice: raise immediately
        remaining -= slice_seconds


def _drag_segment_points(
    start: tuple[int, int], end: tuple[int, int]
) -> list[tuple[int, int]]:
    """Interpolated stroke waypoints from ``start`` to ``end`` (inclusive).

    Segments are ~``_DRAG_SEGMENT_PIXELS`` px long (Chebyshev distance) with a floor of
    ``_DRAG_MIN_SEGMENTS`` so even short drags draw a smooth stroke. The final waypoint
    is exactly ``end``. Shared by the real backend (pyautogui ``moveTo`` targets) and the
    fake backend (simulated cursor walk), so both stroke identically.
    """
    distance = max(abs(end[0] - start[0]), abs(end[1] - start[1]))
    segments = max(_DRAG_MIN_SEGMENTS, -(-distance // _DRAG_SEGMENT_PIXELS))
    return [
        (
            round(start[0] + (end[0] - start[0]) * index / segments),
            round(start[1] + (end[1] - start[1]) * index / segments),
        )
        for index in range(1, segments + 1)
    ]


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


@dataclass(frozen=True)
class _CaptureContext:
    """Coordinate context recorded by the most recent observe() on this backend."""

    monitor: MonitorInfo
    verdict: CoordinateVerdict

    @property
    def transform(self) -> CoordinateTransform:
        return CoordinateTransform(
            origin_x=self.monitor.bounds[0],
            origin_y=self.monitor.bounds[1],
            scale_x=self.verdict.scale_x,
            scale_y=self.verdict.scale_y,
        )


class ComputerBackend(ABC):
    """Platform contract: observation capture, stop-checked input, interruptible waiting."""

    _active_context: _CaptureContext | None = None

    @abstractmethod
    def observe(self) -> Observation:
        """Capture a full Observation of the current computer state."""

    @abstractmethod
    def execute(self, action: GroundedAction, stop: StopToken | None = None) -> str:
        """Execute one grounded action and return a human-readable result message.

        ``stop`` is optional for backward compatibility: when None no cancellation checks
        are possible and the action still executes. Implementations must call
        ``StopToken.ensure_live()`` immediately before every physical input call.
        Raises ``TaskStopped`` when a provided stop token has fired (zero inputs are
        performed in that case).
        """

    def wait(self, seconds: float, stop: StopToken | None = None) -> float:
        """Interruptible sleep; returns the seconds actually waited.

        Early-returns (without raising) when the stop token fires; callers detect the
        abort via the returned duration or by checking the token themselves.
        """
        return interruptible_wait(seconds, stop)

    def find_window_by_title(self, target: str) -> WindowInfo | None:
        """Resolve a window title to strong identity; ``None`` when it cannot be found.

        Documented default: backends without window enumeration report no windows (the
        agent-level focus allowlist then fails closed with
        ``process_identity_unavailable``). Platform backends override this.
        """
        return None

    def _map_to_physical(self, x: int, y: int) -> tuple[int, int]:
        """Map screenshot-local coordinates to physical input coordinates.

        This is the ONLY screenshot-to-physical transform in the pipeline (module
        docstring invariant, F1): ``physical = origin + screenshot * scale``, applied
        exactly once here at execution. Action points therefore always arrive in
        screenshot space — grounding validates bounds and records the scale but never
        pre-scales the point.

        Uses the context recorded by the most recent ``observe()`` (the observation the
        action was grounded from); refuses coordinate input when that context is
        ``UNVERIFIABLE``. Without any prior observation, coordinates are assumed to be
        physical virtual-screen pixels (legacy passthrough behavior).
        """
        context = self._active_context
        if context is None:
            context = self._build_default_context()
            self._active_context = context
        if context.verdict.space is CoordinateSpace.UNVERIFIABLE:
            raise CoordinateSpaceError(
                "Coordinate-space integrity could not be verified; refusing coordinate input."
            )
        return context.transform.to_physical(x, y)

    @abstractmethod
    def _build_default_context(self) -> _CaptureContext:
        """Best-effort coordinate context when execute() runs before any observe()."""


class LocalComputerBackend(ComputerBackend):
    """Real Windows backend: Win32 identity/DPI/monitors + stop-checked pyautogui input."""

    def __init__(self) -> None:
        self._system = platform.system()
        if self._system != "Windows":
            raise RuntimeError(
                "LocalComputerBackend currently requires Windows. "
                "Use FakeComputerBackend for tests or add a platform adapter."
            )
        self.dpi_awareness: str = _set_process_dpi_awareness()
        self.dpi_estimated: bool = False
        self._monitors: list[MonitorInfo] = []
        import mss  # type: ignore[import-not-found]

        self.mss_dpi_neutralized = self._neutralize_mss_dpi_awareness(mss)
        # mss.MSS is the supported factory on mss >= 10.2; mss.mss() is the legacy path.
        self._mss_factory: Callable[[], object] = getattr(mss, "MSS", None) or mss.mss
        import pyautogui  # type: ignore[import-not-found]

        self._pyautogui = pyautogui
        pyautogui.FAILSAFE = True
        self._refresh_monitors()

    @staticmethod
    def _neutralize_mss_dpi_awareness(mss_module: object) -> bool:
        """Stop mss from re-setting process DPI awareness; this backend owns it.

        mss (10.x) calls ``shcore.SetProcessDpiAwareness(2)`` in every instance
        constructor and crashes with ``PermissionError`` (E_ACCESSDENIED) whenever
        awareness is already set — which this backend deliberately does first
        (per-monitor-v2). Returns True when the neutralization applied.
        """
        try:
            from mss.windows import gdi  # type: ignore[import-not-found]

            def _no_awareness(self: object) -> None:
                return None

            gdi.MSSImplGdi._set_dpi_awareness = _no_awareness  # type: ignore[attr-defined]
            return True
        except (AttributeError, ImportError, TypeError):
            return False

    def _per_monitor_dpi_available(self) -> bool:
        return self.dpi_awareness in {"per_monitor_v2", "per_monitor"}

    def _mss_monitors(self) -> list[dict[str, int]] | None:
        try:
            with self._mss_factory() as capture:
                return list(capture.monitors)
        except Exception:  # noqa: BLE001 - any mss failure degrades to the enum path
            return None

    def _refresh_monitors(self) -> None:
        monitors, dpi_estimated = enumerate_monitors(
            self._mss_monitors(), per_monitor_dpi=self._per_monitor_dpi_available()
        )
        self._monitors = monitors
        self.dpi_estimated = dpi_estimated

    def observe(self, monitor_index: int | None = None) -> Observation:
        """Capture a full Observation of the current computer state.

        Targets the monitor containing the cursor (fallback: primary monitor), or
        ``monitor_index`` when provided. Raises ``DisplayUnavailableError`` when capture
        fails; window/cursor identity degrades to None instead of failing.
        """
        if self._system != "Windows":
            raise DisplayUnavailableError("Screen capture requires Windows.")
        self._refresh_monitors()
        cursor_physical = get_cursor_position()
        monitor = _select_target_monitor(self._monitors, cursor_physical, monitor_index)
        left, top, width, height = monitor.bounds
        if width <= 0 or height <= 0:
            raise DisplayUnavailableError(f"Monitor {monitor.id} has invalid bounds {monitor.bounds}.")
        encoded, image_width, image_height = self._grab_png(left, top, width, height)
        window_info = query_foreground_window()
        verdict = classify_coordinate_space(
            image_width, image_height, monitor, dpi_estimated=self.dpi_estimated
        )
        cursor_local: tuple[int, int] | None = None
        if cursor_physical is not None:
            cursor_local = CoordinateTransform(
                origin_x=left, origin_y=top, scale_x=verdict.scale_x, scale_y=verdict.scale_y
            ).to_screenshot(*cursor_physical)
        self._active_context = _CaptureContext(monitor=monitor, verdict=verdict)
        return Observation(
            image_base64=encoded,
            width=image_width,
            height=image_height,
            active_window=(window_info.title or None) if window_info else None,
            cursor_x=cursor_local[0] if cursor_local else None,
            cursor_y=cursor_local[1] if cursor_local else None,
            input_width=verdict.input_width,
            input_height=verdict.input_height,
            coordinate_scale_x=verdict.scale_x,
            coordinate_scale_y=verdict.scale_y,
            coordinate_space=verdict.space,
            monitor=monitor,
            active_window_info=window_info,
            redactions_applied=False,
        )

    def _grab_png(self, left: int, top: int, width: int, height: int) -> tuple[str, int, int]:
        """Capture a monitor region and return ``(base64_png, width, height)``."""
        try:
            with self._mss_factory() as capture:
                raw = capture.grab({"left": left, "top": top, "width": width, "height": height})
            image = Image.frombytes("RGB", raw.size, raw.rgb)
        except Exception as exc:
            raise DisplayUnavailableError(f"Screen capture failed: {exc}") from exc
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return base64.b64encode(output.getvalue()).decode("ascii"), image.width, image.height

    def execute(self, action: GroundedAction, stop: StopToken | None = None) -> str:
        """Execute one action; the stop token is checked before every physical input."""
        if stop is not None:
            stop.ensure_live()
        if action.action == "wait":
            total = min(max(action.delta, 0), WAIT_MAX_SECONDS)
            if stop is None:
                time.sleep(total)
            else:
                _sleep_for_wait_action(total, stop)
            return f"Executed {action.action}."
        if action.action in {"click", "double_click"}:
            if action.point is None:
                raise ValueError("A point is required for click actions")
            physical = self._map_to_physical(action.point.x, action.point.y)
            if stop is not None:
                stop.ensure_live()
            clicks = 2 if action.action == "double_click" else 1
            self._perform(
                lambda: self._pyautogui.click(physical[0], physical[1], clicks=clicks, interval=0.08)
            )
        elif action.action == "drag":
            if action.point is None or action.to_point is None:
                raise ValueError("Both a start point and an end point are required for drag actions")
            start = self._map_to_physical(action.point.x, action.point.y)
            end = self._map_to_physical(action.to_point.x, action.to_point.y)
            if stop is not None:
                stop.ensure_live()  # before moveTo(start)
            self._perform(lambda: self._pyautogui.moveTo(start[0], start[1]))
            if stop is not None:
                stop.ensure_live()  # before mouseDown
            button_down = False
            drag_error: BaseException | None = None
            try:
                self._perform(lambda: self._pyautogui.mouseDown(button="left"))
                button_down = True
                for segment_x, segment_y in _drag_segment_points(start, end):
                    if stop is not None:
                        stop.ensure_live()  # before every stroke segment
                    self._perform(lambda sx=segment_x, sy=segment_y: self._pyautogui.moveTo(sx, sy))
                    time.sleep(_DRAG_STEP_PAUSE_SECONDS)
                if stop is not None:
                    stop.ensure_live()  # before mouseUp
            except BaseException as error:
                drag_error = error
                raise
            finally:
                # The button is ALWAYS released — including on a mid-stroke TaskStopped
                # (P0-C: the stop is the kill path and must never leave input held). A
                # failing release never masks an in-flight error such as TaskStopped.
                if button_down:
                    try:
                        self._perform(lambda: self._pyautogui.mouseUp(button="left"))
                    except Exception:
                        if drag_error is None:
                            raise
        elif action.action == "move":
            if action.point is None:
                raise ValueError("A point is required for move actions")
            physical = self._map_to_physical(action.point.x, action.point.y)
            if stop is not None:
                stop.ensure_live()  # before the single cursor reposition
            self._perform(lambda: self._pyautogui.moveTo(*physical))
            time.sleep(_MOVE_SETTLE_SECONDS)  # settle only; a move is not an input press
        elif action.action == "type":
            if action.text is None:
                raise ValueError("Text is required for type actions")
            for char in action.text:
                if stop is not None:
                    stop.ensure_live()
                self._perform(lambda char=char: self._pyautogui.write(char))
                time.sleep(_TYPE_INTERVAL_SECONDS)
        elif action.action == "keypress":
            if not action.keys:
                raise ValueError("At least one key is required")
            if stop is not None:
                stop.ensure_live()
            self._perform(lambda: self._pyautogui.hotkey(*action.keys))
        elif action.action == "hotkey":
            # Sibling of keypress for COMPOUND chords (2..12 keys, passed verbatim in
            # pyautogui vocabulary); single-key presses deliberately stay on keypress.
            guard_keys = [key.strip() for key in action.keys]
            if not 2 <= len(guard_keys) <= 12 or any(not key for key in guard_keys):
                raise ValueError("Hotkey actions require 2 to 12 non-empty key names")
            if stop is not None:
                stop.ensure_live()  # immediately before the single chord input
            self._perform(lambda: self._pyautogui.hotkey(*action.keys))
        elif action.action == "scroll":
            if stop is not None:
                stop.ensure_live()
            self._perform(lambda: self._pyautogui.scroll(action.delta))
        elif action.action == "focus_window":
            return self._execute_focus_window(action)
        elif action.action == "done":
            return "No computer action requested."
        else:
            raise UnsupportedActionError(f"Unsupported action: {action.action}")
        return f"Executed {action.action}."

    def _perform(self, input_call: Callable[[], None]) -> None:
        """Run one pyautogui input, mapping FailSafeException onto InputBlockedError."""
        try:
            input_call()
        except self._pyautogui.FailSafeException as exc:
            raise InputBlockedError(
                "pyautogui failsafe triggered (mouse in a screen corner); physical input halted."
            ) from exc

    def _execute_focus_window(self, action: GroundedAction) -> str:
        """Bring the window matching ``action.target`` to the foreground (Win32 sequence).

        Sequence: restore when minimized (``IsIconic`` -> ``ShowWindow(SW_RESTORE)``),
        then the standard ``AttachThreadInput`` foreground switch with an ALT
        keybd_event nudge, then verify ``GetForegroundWindow()`` actually moved — a
        refusal raises :class:`WindowFocusError` (documented residual: the previously
        focused window is not restored; the caller re-observes). The stop token is
        checked once by the ``execute`` header; this path performs no pyautogui input.
        """
        target = (action.target or "").strip()
        if not target:
            raise ValueError("A target window title is required for focus_window actions")
        if not IS_WINDOWS or _user32 is None:
            raise WindowFocusError("Window focus requires Windows.")
        candidate = find_window_by_title(target)
        if candidate is None or candidate.hwnd is None:
            raise WindowFocusError(f"no window matching '{target}'")
        hwnd = int(candidate.hwnd)
        try:
            if _user32.IsIconic(hwnd):
                _user32.ShowWindow(hwnd, _SW_RESTORE)
            foreground = _user32.GetForegroundWindow()
            fg_thread = _user32.GetWindowThreadProcessId(int(foreground or 0), None)
            me = _kernel32.GetCurrentThreadId()
            _user32.AttachThreadInput(me, fg_thread, True)
            try:
                _user32.keybd_event(_VK_MENU, 0, 0, 0)
                _user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)  # ALT nudge
                _user32.SetForegroundWindow(hwnd)
            finally:
                _user32.AttachThreadInput(me, fg_thread, False)
            focused = int(_user32.GetForegroundWindow() or 0)
        except (OSError, AttributeError) as exc:
            raise WindowFocusError(f"Window focus failed: {exc}") from exc
        if focused != hwnd:
            raise WindowFocusError(
                f"SetForegroundWindow refused focus for '{candidate.title or target}'; "
                "the foreground window did not change."
            )
        return f"Focused window '{candidate.title or target}'."

    def _build_default_context(self) -> _CaptureContext:
        """Passthrough-by-measurement context when executing before any observation."""
        self._refresh_monitors()
        monitor = _select_target_monitor(self._monitors, get_cursor_position(), None)
        verdict = classify_coordinate_space(
            monitor.bounds[2], monitor.bounds[3], monitor, dpi_estimated=self.dpi_estimated
        )
        return _CaptureContext(monitor=monitor, verdict=verdict)


class FakeComputerBackend(ComputerBackend):
    """Faithful in-memory backend mirroring LocalComputerBackend's contracts for tests.

    Implements the same observe/execute/wait contracts, StopToken checks, coordinate
    space classification (the shared ``classify_coordinate_space``), coordinate
    refusal, cursor localization, and wait slicing as the real backend — driven by
    injectable state instead of a real desktop.

    Constructor params:
    - ``width``/``height``: screenshot dimensions (legacy positionals preserved).
    - ``monitors``: fake monitor set (``MonitorInfo`` list, physical bounds + DPI scale).
      Defaults to one primary monitor exactly matching the screenshot dimensions.
    - ``active_window``: fake foreground window identity (``WindowInfo``), or None.
    - ``windows``: fake top-level window list for title lookup (``WindowInfo`` list,
      first entry = top of Z-order). Defaults to ``[active_window]`` when an active
      window is configured, else ``[]``. ``set_windows`` replaces it.
    - ``cursor``: fake cursor in *physical virtual-screen coordinates* (GetCursorPos
      convention — may be negative on secondary monitors), or None for no cursor.
    - ``input_blocked``: when True, ``execute`` raises ``InputBlockedError``.

    Simulation methods: ``set_active_window`` (focus change), ``set_window_bounds``
    (window move), ``set_monitors`` (display/DPI change), ``set_cursor``,
    ``set_screenshot_size`` (resolution change / space mismatch), ``set_input_blocked``,
    ``set_windows`` (window population for focus_window / allowlist probes).

    Recorded state: ``executed`` (actions whose simulated input completed — a pre-stopped,
    input-blocked, or coordinate-refused execute records nothing, and neither does a wait
    interrupted by the stop token), ``observed`` (every observation produced),
    ``width``/``height`` (current screenshot dims). For drag actions the stroke is
    simulated as a stop-checked cursor walk (identical waypoint policy to the real
    backend): ``drags`` records the mapped physical ``(start, end)`` pairs of completed
    drags, the cursor ends at the drag end point, and ``_drag_button_down`` mirrors the
    held mouse button (always ``False`` after execute, including on a mid-stroke stop —
    the release guarantee is part of the contract). Drawing pixels in the fake canvas is
    not required; drag verification in tests uses visual_change with a flip or predicate.
    ``move`` actions reposition the simulated cursor to the mapped physical point
    (unverifiable coordinate spaces raise ``CoordinateSpaceError`` identically).
    ``focus_window`` resolves ``action.target`` through ``find_window_by_title`` (same
    exact > prefix > substring precedence as the real backend): a miss raises
    ``WindowFocusError`` and records nothing; a hit swaps ``active_window`` (a copy) and
    appends the target to ``focused``.
    """

    def __init__(
        self,
        width: int = 1280,
        height: int = 720,
        *,
        monitors: list[MonitorInfo] | None = None,
        active_window: WindowInfo | None = None,
        windows: list[WindowInfo] | None = None,
        cursor: tuple[int, int] | None = None,
        input_blocked: bool = False,
    ) -> None:
        self.width = width
        self.height = height
        self.monitors = (
            list(monitors)
            if monitors
            else [
                MonitorInfo(
                    id="monitor-0",
                    index=0,
                    bounds=(0, 0, width, height),
                    is_primary=True,
                )
            ]
        )
        self.active_window = active_window
        self.windows: list[WindowInfo] = (
            list(windows) if windows is not None else ([active_window] if active_window else [])
        )
        self.focused: list[str] = []
        self._cursor_physical = cursor
        self._input_blocked = input_blocked
        self.executed: list[GroundedAction] = []
        self.drags: list[tuple[tuple[int, int], tuple[int, int]]] = []
        self._drag_button_down = False
        self.observed: list[Observation] = []
        self._active_context: _CaptureContext | None = None

    def set_windows(self, windows: list[WindowInfo]) -> None:
        """Replace the fake top-level window list (first entry = top of Z-order)."""
        self.windows = list(windows)

    def find_window_by_title(self, target: str) -> WindowInfo | None:
        """Same matching rule as the real backend: exact > prefix > substring, Z-order."""
        needle = (target or "").strip().casefold()
        if not needle:
            return None
        best: tuple[int, int, WindowInfo] | None = None  # (rank, z_index, window)
        for z_index, window in enumerate(self.windows):
            rank = _title_match_rank(window.title, needle)
            if rank is None:
                continue
            if best is None or rank > best[0]:  # first-in-Z-order wins ties
                best = (rank, z_index, window)
        return best[2] if best is not None else None

    def set_active_window(self, window: WindowInfo | None) -> None:
        """Swap the fake foreground window (simulates focus change between observations)."""
        self.active_window = window

    def set_window_bounds(self, bounds: tuple[int, int, int, int]) -> None:
        """Move/resize the fake foreground window (simulates a window move)."""
        if self.active_window is None:
            raise ValueError("No active window configured; call set_active_window first.")
        self.active_window = self.active_window.model_copy(update={"bounds": bounds})

    def set_monitors(self, monitors: list[MonitorInfo]) -> None:
        """Replace the fake monitor set (simulates a display or DPI change)."""
        if not monitors:
            raise ValueError("At least one monitor is required.")
        self.monitors = list(monitors)

    def set_cursor(self, position: tuple[int, int]) -> None:
        """Set the fake cursor in physical virtual-screen coordinates (GetCursorPos convention)."""
        self._cursor_physical = position

    def set_screenshot_size(self, width: int, height: int) -> None:
        """Change the fake screenshot dimensions (simulates resolution change/mismatch)."""
        if width <= 0 or height <= 0:
            raise ValueError("Screenshot dimensions must be positive.")
        self.width = width
        self.height = height

    def set_input_blocked(self, blocked: bool) -> None:
        """When True, execute() raises InputBlockedError (simulates a blocked desktop)."""
        self._input_blocked = blocked

    def observe(self, monitor_index: int | None = None) -> Observation:
        """Produce a full Observation from the injected fake state (same contract as real)."""
        cursor_physical = self._cursor_physical
        monitor = _select_target_monitor(self.monitors, cursor_physical, monitor_index)
        verdict = classify_coordinate_space(self.width, self.height, monitor, dpi_estimated=False)
        cursor_local: tuple[int, int] | None = None
        if cursor_physical is not None:
            cursor_local = CoordinateTransform(
                origin_x=monitor.bounds[0],
                origin_y=monitor.bounds[1],
                scale_x=verdict.scale_x,
                scale_y=verdict.scale_y,
            ).to_screenshot(*cursor_physical)
        window = self.active_window.model_copy() if self.active_window else None
        self._active_context = _CaptureContext(monitor=monitor, verdict=verdict)
        observation = Observation(
            image_base64=self._white_png(),
            width=self.width,
            height=self.height,
            active_window=(window.title or None) if window else None,
            cursor_x=cursor_local[0] if cursor_local else None,
            cursor_y=cursor_local[1] if cursor_local else None,
            input_width=verdict.input_width,
            input_height=verdict.input_height,
            coordinate_scale_x=verdict.scale_x,
            coordinate_scale_y=verdict.scale_y,
            coordinate_space=verdict.space,
            monitor=monitor.model_copy(),
            active_window_info=window,
            redactions_applied=False,
        )
        self.observed.append(observation)
        return observation

    def execute(self, action: GroundedAction, stop: StopToken | None = None) -> str:
        """Record and simulate one action; the stop token gates every simulated input."""
        if stop is not None:
            stop.ensure_live()
        if action.action == "wait":
            total = min(max(action.delta, 0), WAIT_MAX_SECONDS)
            if stop is None:
                time.sleep(total)
            else:
                _sleep_for_wait_action(total, stop)
            self.executed.append(action)
            return f"Simulated {action.action}."
        if self._input_blocked:
            raise InputBlockedError("FakeComputerBackend is configured to block physical input.")
        if action.action in {"click", "double_click"}:
            if action.point is None:
                raise ValueError("A point is required for click actions")
            self._cursor_physical = self._map_to_physical(action.point.x, action.point.y)
        elif action.action == "move":
            if action.point is None:
                raise ValueError("A point is required for move actions")
            # Unverifiable coordinate spaces raise CoordinateSpaceError identically to
            # click (shared _map_to_physical refusal) and record nothing.
            self._cursor_physical = self._map_to_physical(action.point.x, action.point.y)
        elif action.action == "focus_window":
            target = (action.target or "").strip()
            candidate = self.find_window_by_title(target)
            if candidate is None:
                raise WindowFocusError(f"no window matching '{target}'")
            self.active_window = candidate.model_copy()
            self.focused.append(target)
        elif action.action == "drag":
            if action.point is None or action.to_point is None:
                raise ValueError("Both a start point and an end point are required for drag actions")
            start = self._map_to_physical(action.point.x, action.point.y)
            end = self._map_to_physical(action.to_point.x, action.to_point.y)
            if stop is not None:
                stop.ensure_live()  # before moveTo(start)
            self._cursor_physical = start
            if stop is not None:
                stop.ensure_live()  # before mouseDown
            self._drag_button_down = True
            try:
                for segment_x, segment_y in _drag_segment_points(start, end):
                    if stop is not None:
                        stop.ensure_live()  # before every stroke segment
                    self._cursor_physical = (segment_x, segment_y)
                if stop is not None:
                    stop.ensure_live()  # before mouseUp
            finally:
                # Same release guarantee as the real backend: the button is never left
                # held, including on a mid-stroke TaskStopped.
                self._drag_button_down = False
            self.drags.append((start, end))
        self.executed.append(action)
        return f"Simulated {action.action}."

    def _white_png(self) -> str:
        image = Image.new("RGB", (self.width, self.height), "white")
        output = io.BytesIO()
        image.save(output, format="PNG")
        return base64.b64encode(output.getvalue()).decode("ascii")

    def _build_default_context(self) -> _CaptureContext:
        monitor = _select_target_monitor(self.monitors, self._cursor_physical, None)
        verdict = classify_coordinate_space(self.width, self.height, monitor, dpi_estimated=False)
        return _CaptureContext(monitor=monitor, verdict=verdict)
