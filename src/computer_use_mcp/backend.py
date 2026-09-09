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

Physical input is dispatched by pluggable :class:`InputEngine` implementations
(:class:`SendInputEngine` — raw Win32 ``SendInput`` via ctypes, the default — and
:class:`PyAutoGuiInputEngine`, the selectable pyautogui fallback). Both honor the same
safety contract: failsafe screen-corner checks raise :class:`InputBlockedError`, the
stop token is checked before every physical input by ``execute``, and a blocked
injection (``SendInput`` returning 0) fails closed. The engine is selected at backend
construction from ``CORTEX_INPUT_BACKEND`` (``sendinput`` | ``pyautogui``).

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
import re
import shutil
import subprocess
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PIL import Image

from .models import (
    CoordinateSpace,
    GroundedAction,
    MonitorInfo,
    Observation,
    TextRegion,
    WindowInfo,
)
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
_TYPE_INTERVAL_SECONDS = 0.01  # legacy per-char pacing (pyautogui fallback parity)
_DRAG_SEGMENT_PIXELS = 40  # stroke interpolation granularity (~one segment per 40 px)
_DRAG_MIN_SEGMENTS = 4  # even tiny drags get a visibly interpolated stroke
_DRAG_STEP_PAUSE_SECONDS = 0.01  # per-segment pacing (same cadence as type)
_SCALE_TOLERANCE = 0.02
_MOVE_SETTLE_SECONDS = 0.05  # settle after a cursor reposition (not an input pause)
_SW_RESTORE = 9  # ShowWindow(nCmdShow) restore for a minimized window
_VK_MENU = 0x12  # ALT virtual key (foreground-switch nudge)
_KEYEVENTF_KEYUP = 0x02  # keybd_event flag: key release

# --- input engine configuration (env-overridable; PERF-004) ---------------------------------
INPUT_BACKEND_ENV = "CORTEX_INPUT_BACKEND"  # "sendinput" (default) | "pyautogui"
PYAUTOGUI_PAUSE_ENV = "CORTEX_PYAUTOGUI_PAUSE"  # fallback-path PAUSE (default 0.0)
TYPE_INTERVAL_ENV = "CORTEX_TYPE_INTERVAL"  # per-chunk typing pacing (default 0.0)
DRAG_INTERPOLATE_ENV = "CORTEX_DRAG_INTERPOLATE"  # "1" interpolates the drag stroke
PNG_OPTIMIZE_ENV = "CORTEX_PNG_OPTIMIZE"  # "1" restores optimize=True (default off)
UIA_READ_ENV = "CORTEX_UIA_READ"  # "0" disables the UIA semantic read (default on)
SENDINPUT_TYPE_INTERVAL = 0.0  # batched whole-string typing: no per-char cost
SENDINPUT_DRAG_STEP_PAUSE = 0.0  # minimal-segment drag: no per-segment cost
_SENDINPUT_CHUNK_EVENTS = 1000  # max events per SendInput call (typing chunks)
_WHEEL_DELTA = 120

# --- UIA semantic read bounds (PERF-004; research digest Q3) --------------------------------
UIA_MAX_ELEMENTS = 30  # bounded element list (visible buttons/fields)
UIA_MAX_DEPTH = 2  # focused element + its direct children
UIA_READ_BUDGET_SECONDS = 0.05  # hard wall for one semantic read; past it: partial

# --- interference-probe constants (T8; A12 mechanisms i-v) ----------------------------------
_GW_OWNER = 4  # GetWindow(): retrieves the window's owner (owned-dialog chains)
_DIALOG_WINDOW_CLASS = "#32770"  # system dialog class (Save As, Confirm Save As, ...)

#: Settle/gap policy between queued final-Enter-ish keystrokes (B3): a terminal-key
#: chord dispatched within this window of the previous keyboard dispatch waits out the
#: remainder first, so a fast follow_ups batch cannot drop the final Enter into an
#: input-stack race. ``CORTEX_KEY_DISPATCH_GAP`` restores/overrides (0 disables).
TERMINAL_KEYS: frozenset[str] = frozenset({"enter", "return", "numpadenter", "tab"})

#: Generic unsaved-document title conventions for attach-or-launch discovery (T8):
#: leading tokens apps give brand-new unsaved docs + restore-suffixed file patterns
#: (``<name>.xlsx1`` windows Excel restores beside a crashed session). Title
#: heuristics only — DATA, never per-app CODE (A12 mechanism ii).
UNSAVED_DOC_LEADING_TOKENS: frozenset[str] = frozenset(
    {"book", "untitled", "document", "presentation", "workbook", "sheet", "drawing", "image"}
)
_UNSAVED_RESTORE_SUFFIX = re.compile(
    r"\.(xlsx|xlsm|xls|docx|doc|pptx|ppt|txt|csv|rtf|png|jpg|jpeg|bmp|one|odt|ods)\d+(\.|$)",
    re.IGNORECASE,
)

# Win32 SendInput / virtual-screen constants
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_VIRTUALDESK = 0x4000
_MOUSEEVENTF_ABSOLUTE = 0x8000
_KEYEVENTF_UNICODE = 0x0004
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_COINIT_APARTMENTTHREADED = 0x2
_RPC_E_CHANGED_MODE = -2147417850  # HRESULT 0x80010106 as c_int
_CLSCTX_INPROC_SERVER = 0x1
_TREE_SCOPE_CHILDREN = 0x2
_VT_EMPTY = 0
_VT_I4 = 3
_VT_BSTR = 8
_VT_BOOL = 11
_VT_ARRAY = 0x2000
_VT_R8 = 5

# UIA property IDs (UIAutomationClient.h)
_UIA_PROP_BOUNDING_RECTANGLE = 30001
_UIA_PROP_CONTROL_TYPE = 30003
_UIA_PROP_NAME = 30005
_UIA_PROP_AUTOMATION_ID = 30011
_UIA_PROP_VALUE = 30045
_UIA_PROP_IS_OFFSCREEN = 30022

# CLSID/IID for the raw-ctypes UIA COM client (UIAutomationClient.dll)
_CLSID_CUIAUTOMATION = "ff48dba4-60ef-4201-aa87-54103eef594e"
_IID_IUIAUTOMATION = "30cbe57d-d9d3-4eac-bca0-31ca10efc51e"

# IUIAutomation / IUIAutomationElement / IUIAutomationElementArray vtable slots
# (IUnknown occupies 0-2; order fixed by UIAutomationClient.h).
_UIA_GET_FOCUSED_ELEMENT = 7
_UIA_CREATE_TRUE_CONDITION = 22
_UIA_ELEMENT_FIND_ALL = 6
_UIA_ELEMENT_GET_PROPERTY_VALUE = 10
_UIA_ARRAY_GET_LENGTH = 3
_UIA_ARRAY_GET_ELEMENT = 4
_COM_RELEASE = 2

UIA_CONTROLTYPE_NAMES: dict[int, str] = {
    50000: "Button",
    50001: "Calendar",
    50002: "CheckBox",
    50003: "ComboBox",
    50004: "Edit",
    50005: "Hyperlink",
    50006: "Image",
    50007: "ListItem",
    50008: "List",
    50009: "Menu",
    50010: "MenuBar",
    50011: "MenuItem",
    50012: "ProgressBar",
    50013: "RadioButton",
    50014: "ScrollBar",
    50015: "Slider",
    50016: "Spinner",
    50017: "StatusBar",
    50018: "Tab",
    50019: "TabItem",
    50020: "Text",
    50021: "ToolBar",
    50022: "ToolTip",
    50023: "Tree",
    50024: "TreeItem",
    50025: "Custom",
    50026: "Group",
    50027: "Thumb",
    50028: "DataGrid",
    50029: "DataItem",
    50030: "Document",
    50031: "SplitButton",
    50032: "Window",
    50033: "Pane",
    50034: "Header",
    50035: "HeaderItem",
    50036: "Table",
    50037: "TitleBar",
    50038: "Separator",
}

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
    # Interference-probe bindings (T8): stuck-modifier sweep + foreground focus target.
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _user32.IsWindowVisible.restype = ctypes.wintypes.BOOL
    _user32.IsWindow.argtypes = [ctypes.c_void_p]
    _user32.IsWindow.restype = ctypes.wintypes.BOOL
    _user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _user32.GetWindow.restype = ctypes.c_void_p
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
    try:
        _ole32 = ctypes.windll.ole32
        _oleaut32 = ctypes.windll.oleaut32
    except (AttributeError, OSError):  # pragma: no cover - ole32 is always present on Windows
        _ole32 = None
        _oleaut32 = None

    class _MOUSEINPUT(ctypes.Structure):
        """``MOUSEINPUT`` layout (x64)."""

        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class _KEYBDINPUT(ctypes.Structure):
        """``KEYBDINPUT`` layout (x64)."""

        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class _INPUT_UNION(ctypes.Union):
        """``INPUT`` union: MOUSEINPUT is the largest member (32 bytes on x64)."""

        _fields_ = [
            ("mi", _MOUSEINPUT),
            ("ki", _KEYBDINPUT),
            ("padding", ctypes.c_ubyte * 32),
        ]

    class _INPUT(ctypes.Structure):
        """``INPUT`` layout (x64): 4-byte type + 4-byte alignment pad + 32-byte union."""

        _fields_ = [("type", ctypes.c_ulong), ("union", _INPUT_UNION)]

    _INPUT_MOUSE = 0  # INPUT_MOUSE
    _INPUT_KEYBOARD = 1  # INPUT_KEYBOARD

    class _VARIANT(ctypes.Structure):
        """Minimal ``VARIANT`` (16 bytes on x64) for UIA property reads."""

        class _VARIANT_UNION(ctypes.Union):
            _fields_ = [
                ("lVal", ctypes.c_long),
                ("bstrVal", ctypes.c_void_p),
                ("boolVal", ctypes.wintypes.VARIANT_BOOL),
                ("parray", ctypes.c_void_p),
            ]

        _fields_ = [
            ("vt", ctypes.c_ushort),
            ("wReserved1", ctypes.c_ushort),
            ("wReserved2", ctypes.c_ushort),
            ("wReserved3", ctypes.c_ushort),
            ("union", _VARIANT_UNION),
        ]

    class _GUID(ctypes.Structure):
        """Windows ``GUID`` layout (ctypes.wintypes.GUID is unavailable on this Python)."""

        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class _GUITHREADINFO(ctypes.Structure):
        """``GUITHREADINFO`` layout: the foreground thread's focus/caret state."""

        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("flags", ctypes.c_ulong),
            ("hwndActive", ctypes.c_void_p),
            ("hwndFocus", ctypes.c_void_p),
            ("hwndCapture", ctypes.c_void_p),
            ("hwndMenuOwner", ctypes.c_void_p),
            ("hwndMoveSize", ctypes.c_void_p),
            ("hwndCaret", ctypes.c_void_p),
            ("rcCaret", ctypes.wintypes.RECT),
        ]

    _GW_CHILD = 5
    _GW_HWNDNEXT = 2
    _WM_GETTEXT = 0x000D
    _WM_GETTEXTLENGTH = 0x000E
    _SMTO_ABORTIFHUNG = 0x0002
    _SENDMESSAGE_TIMEOUT_MS = 100
    _user32.GetGUIThreadInfo.argtypes = [ctypes.wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO)]
    _user32.GetGUIThreadInfo.restype = ctypes.wintypes.BOOL

else:  # non-Windows: bindings stay absent, module stays importable for tests
    _user32 = None
    _kernel32 = None
    _shcore = None
    _ole32 = None
    _oleaut32 = None


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


class FocusDriftError(BackendError):
    """Keyboard focus moved away from the session target mid-dispatch (T8 mechanism iv).

    Raised by a ``type`` action's per-chunk focus hook when the focused control stops
    belonging to the bound window: the in-flight ``type`` is aborted immediately (no
    further chunks are dispatched — text must never land in a foreign field). Carries
    the FOCUS_DRIFTED event payload for the controller to surface verbatim.
    """


class LaunchTargetError(BackendError):
    """An ``ensure_app`` launch target (process needle) failed launch validation.

    REM-C (V-2 F3/B7.b): ``_launch_process`` once spawned the needle through
    ``subprocess.Popen([needle], shell=True)``, handing a quote-breakout needle
    (``x" & victim.bat``) to cmd.exe. Validation now runs BEFORE any spawn and
    rejects everything outside ``^[A-Za-z0-9._ -]+`` (no path separators, colons,
    quotes, or cmd.exe metacharacters) — a hostile target never reaches a process
    and surfaces as ``launch_rejected=LaunchTargetError`` in the NO_INSTANCE
    payload instead of spawning.
    """

    def __init__(self, needle: str) -> None:
        self.needle = needle
        super().__init__(
            f"launch target {needle!r} failed validation; only process-name "
            "characters [A-Za-z0-9._ -] are allowed (no paths, quotes, or "
            "shell metacharacters)"
        )


#: REM-C (V-2 F3): the launch-needle allowlist — alphanumerics, dot, underscore,
#: space, hyphen ONLY. Everything else (path separators ``/`` ``\\``, ``:`` drive
#: syntax, quotes, cmd.exe metacharacters ``& | > < % ^ ( ) ; , = ! @ # $ * + ? [ ] { } ` ~``
#: and other punctuation) is a typed rejection BEFORE anything spawns.
LAUNCH_NEEDLE_PATTERN = re.compile(r"^[A-Za-z0-9._ -]+$")


def validate_launch_needle(needle: str) -> str:
    """Validate an ``ensure_app`` launch needle; return it unchanged when valid.

    Accepts exactly the process-name charset ``^[A-Za-z0-9._ -]+$`` (alphanumerics,
    dot, underscore, space, hyphen) so no path, quote, or cmd.exe metacharacter can
    ride through to a spawn; anything else raises :class:`LaunchTargetError`
    (fail-closed, before any process is created).
    """
    if not isinstance(needle, str) or not LAUNCH_NEEDLE_PATTERN.match(needle):
        raise LaunchTargetError(needle if isinstance(needle, str) else repr(needle))
    return needle


class AppWindowCandidate:
    """One visible top-level window of a process, for attach-or-launch discovery (T8).

    ``doc_token`` is the leading segment of the title before the ``" - "`` app suffix
    (``"Book1 - Excel"`` -> ``"Book1"``); ``unsaved_candidate`` flags generic
    unsaved-document title conventions (leading "Book"/"Untitled"/... tokens or a
    restore-suffixed ``<file>.xlsx1`` pattern). Title heuristics only — DATA, never
    per-app code (A12 mechanism ii).
    """

    __slots__ = ("doc_token", "unsaved_candidate", "window")

    def __init__(self, window: WindowInfo, doc_token: str | None, unsaved_candidate: bool) -> None:
        self.window = window
        self.doc_token = doc_token
        self.unsaved_candidate = unsaved_candidate

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"AppWindowCandidate(title={self.window.title!r}, doc_token={self.doc_token!r}, "
            f"unsaved_candidate={self.unsaved_candidate!r})"
        )


def doc_token_from_title(title: str) -> str | None:
    """Leading segment of a window title before the ``" - "`` app suffix, or the title.

    ``"Book1 - Excel"`` -> ``"Book1"``; ``"t1_source.txt - Notepad"`` -> ``"t1_source.txt"``;
    a title without the suffix is its own doc token. Empty titles yield ``None``.
    """
    text = (title or "").strip()
    if not text:
        return None
    return text.split(" - ", 1)[0].strip() or None


def is_unsaved_candidate_title(title: str) -> bool:
    """True when a title matches generic unsaved/restore conventions (heuristic flag)."""
    token = doc_token_from_title(title)
    if token is None:
        return False
    first_word = token.split(" ", 1)[0].casefold()
    stripped = first_word.rstrip("0123456789")  # "book1" -> "book", "untitled" -> "untitled"
    if first_word in UNSAVED_DOC_LEADING_TOKENS or stripped in UNSAVED_DOC_LEADING_TOKENS:
        return True
    return bool(_UNSAVED_RESTORE_SUFFIX.search(token))


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
    is exactly ``end``. Shared by the real backend's interpolated drag mode (and the
    fake backend's simulated cursor walk, so both stroke identically).
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


# --- input engines (PERF-004): raw SendInput default, pyautogui fallback ----------------------

#: Virtual-key codes for the named keys accepted in ``keypress``/``hotkey`` actions
#: (the provider contract passes keys verbatim in pyautogui vocabulary). Letter/digit
#: VKs are layout-independent; other printable characters resolve through
#: ``VkKeyScanW`` exactly like pyautogui's ``VkKeyScanA`` mapping (layout parity).
_VK_NAMED_KEYS: dict[str, int] = {
    "ctrl": 0x11, "ctrlleft": 0xA2, "ctrlright": 0xA3,
    "alt": 0x12, "altleft": 0xA4, "altright": 0xA5,
    "shift": 0x10, "shiftleft": 0xA0, "shiftright": 0xA1,
    "win": 0x5B, "winleft": 0x5B, "winright": 0x5C, "cmd": 0x5B,
    "enter": 0x0D, "return": 0x0D, "numpadenter": 0x0D, "\n": 0x0D, "\r": 0x0D,
    "tab": 0x09, "\t": 0x09, "space": 0x20, " ": 0x20,
    "backspace": 0x08, "del": 0x2E, "delete": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pgup": 0x21, "pageup": 0x21, "pgdn": 0x22, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "esc": 0x1B, "escape": 0x1B, "capslock": 0x14, "numlock": 0x90,
    "scrolllock": 0x91, "pause": 0x13, "print": 0x2A,
    "printscreen": 0x2C, "prntscrn": 0x2C, "prtsc": 0x2C, "prtscr": 0x2C,
    "apps": 0x5D, "help": 0x2F, "execute": 0x2B, "select": 0x29,
    "sleep": 0x5F, "cancel": 0x03, "clear": 0x0C,
    "accept": 0x1E, "convert": 0x1C, "nonconvert": 0x1D, "final": 0x18,
    "modechange": 0x1F, "kana": 0x15, "hanguel": 0x15, "hangul": 0x15,
    "hanja": 0x19, "kanji": 0x19,
    "num0": 0x60, "num1": 0x61, "num2": 0x62, "num3": 0x63, "num4": 0x64,
    "num5": 0x65, "num6": 0x66, "num7": 0x67, "num8": 0x68, "num9": 0x69,
    "multiply": 0x6A, "add": 0x6B, "sep": 0x6C, "separator": 0x6C,
    "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
    "nexttrack": 0xB0, "prevtrack": 0xB1, "stop": 0xB2, "playpause": 0xB3,
    "volumemute": 0xAD, "volumeup": 0xAF, "volumedown": 0xAE,
    "launchmail": 0xB4, "launchmediaselect": 0xB5, "launchapp1": 0xB6, "launchapp2": 0xB7,
    "browserback": 0xA6, "browserforward": 0xA7, "browserrefresh": 0xA8,
    "browserstop": 0xA9, "browsersearch": 0xAA, "browserfavorites": 0xAB,
    "browserhome": 0xAC,
}
_VK_NAMED_KEYS.update({f"f{index}": 0x70 + index - 1 for index in range(1, 25)})
_VK_NAMED_KEYS.update({chr(0x61 + index): 0x41 + index for index in range(26)})  # a-z
_VK_NAMED_KEYS.update({str(digit): 0x30 + digit for digit in range(10)})  # 0-9
# Uppercase letters are deliberately NOT in the table: 'A' resolves through the
# lowercase entry with needs_shift=True (pyautogui needsShift parity).

_MOUSE_BUTTON_FLAGS: dict[str, tuple[int, int]] = {
    "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
    "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
    "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
}


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean env flag; garbage values fall back to the default."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _env_nonnegative_float(name: str, default: float) -> float:
    """Read a non-negative float env value; garbage/negative falls back to default."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


#: Settle/gap policy between queued final-Enter-ish keystrokes (B3, T8): a terminal-key
#: chord dispatched within this many seconds of the previous keyboard dispatch waits
#: out the remainder first, so a fast follow_ups batch cannot drop the final Enter into
#: an input-stack race. ``CORTEX_KEY_DISPATCH_GAP`` overrides (0 disables).
KEY_DISPATCH_GAP_SECONDS = _env_nonnegative_float("CORTEX_KEY_DISPATCH_GAP", 0.05)

#: B8 settle policy (T8): keyboard input is paced behind a RECENT focus transition
#: (focus_window / ensure_app reattach) for this many seconds — the first keys sent
#: while the activated window's thread input queue is still settling are the ones that
#: drop or misdeliver (the B5 wedge topology; the A5 residual risk class).
#: ``CORTEX_FOCUS_SETTLE_SECONDS`` overrides (0 disables). Re-dispatch of the dropped
#: keys is deliberately NOT automatic (a re-issued Enter can double-submit).
FOCUS_TRANSITION_SETTLE_SECONDS = _env_nonnegative_float("CORTEX_FOCUS_SETTLE_SECONDS", 0.3)


def _virtual_screen_metrics() -> tuple[int, int, int, int]:
    """Virtual-desktop ``(x, y, width, height)`` via ``GetSystemMetrics`` (fail closed)."""
    if IS_WINDOWS and _user32 is not None:
        get_metrics = getattr(_user32, "GetSystemMetrics", None)
        if get_metrics is not None:
            try:
                x = int(get_metrics(_SM_XVIRTUALSCREEN))
                y = int(get_metrics(_SM_YVIRTUALSCREEN))
                width = int(get_metrics(_SM_CXVIRTUALSCREEN))
                height = int(get_metrics(_SM_CYVIRTUALSCREEN))
            except (OSError, AttributeError, ValueError):
                x = y = width = height = 0
            if width > 0 and height > 0:
                return (x, y, width, height)
    raise InputBlockedError(
        "Virtual-desktop metrics are unavailable; refusing physical input (fail closed)."
    )


def _primary_screen_size() -> tuple[int, int]:
    """Primary-monitor pixel size via ``GetSystemMetrics`` (pyautogui's ``size()`` parity)."""
    if IS_WINDOWS and _user32 is not None:
        get_metrics = getattr(_user32, "GetSystemMetrics", None)
        if get_metrics is not None:
            try:
                width = int(get_metrics(0))
                height = int(get_metrics(1))
                if width > 0 and height > 0:
                    return (width, height)
            except (OSError, AttributeError, ValueError):
                pass
    return (1920, 1080)


def _to_absolute_65535(value: int, origin: int, extent: int) -> int:
    """Normalize one physical axis to ``MOUSEEVENTF_ABSOLUTE``'s 0-65535 range.

    Bijection over the axis: ``0 -> 0`` and ``origin + extent - 1 -> 65535`` so the
    inverse (``_from_absolute_65535``) restores the exact pixel. ``extent <= 1``
    degenerates to 0.
    """
    if extent <= 1:
        return 0
    clamped = min(max(value, origin), origin + extent - 1)
    return round((clamped - origin) * 65535 / (extent - 1))


def _from_absolute_65535(normalized: int, origin: int, extent: int) -> int:
    """Inverse of :func:`_to_absolute_65535` (used by the parity tests)."""
    if extent <= 1:
        return origin
    return origin + round(normalized * (extent - 1) / 65535)


def _mouse_move_event(x: int, y: int, metrics: tuple[int, int, int, int]) -> _INPUT:
    """Absolute ``MOUSEEVENTF_MOVE`` event over the whole virtual desktop.

    ``MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK`` maps 0-65535 onto the entire
    virtual desktop (multi-monitor correct; pyautogui's own normalization uses primary
    metrics only — a latent bug the SendInput engine deliberately does not copy).
    """
    origin_x, origin_y, width, height = metrics
    return _INPUT(
        type=_INPUT_MOUSE,
        union=_INPUT_UNION(
            mi=_MOUSEINPUT(
                dx=_to_absolute_65535(x, origin_x, width),
                dy=_to_absolute_65535(y, origin_y, height),
                mouseData=0,
                dwFlags=_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK,
                time=0,
                dwExtraInfo=0,
            )
        ),
    )


def _mouse_flag_event(dw_flags: int, mouse_data: int = 0) -> _INPUT:
    """Relative-flag mouse event (button/wheel); dx/dy unused at (0, 0) with no MOVE."""
    return _INPUT(
        type=_INPUT_MOUSE,
        union=_INPUT_UNION(
            mi=_MOUSEINPUT(
                dx=0, dy=0, mouseData=mouse_data & 0xFFFFFFFF, dwFlags=dw_flags, time=0, dwExtraInfo=0
            )
        ),
    )


def _key_event(vk: int, scan: int, dw_flags: int) -> _INPUT:
    """One ``KEYBDINPUT`` event (``KEYEVENTF_UNICODE`` uses ``scan`` as the code unit)."""
    return _INPUT(
        type=_INPUT_KEYBOARD,
        union=_INPUT_UNION(
            ki=_KEYBDINPUT(wVk=vk & 0xFFFF, wScan=scan & 0xFFFF, dwFlags=dw_flags, time=0, dwExtraInfo=0)
        ),
    )


def _text_to_key_units(text: str) -> list[tuple[int, int, int]]:
    """Whole-string typing as ``KEYEVENTF_UNICODE`` event triples ``(vk, scan, flags)``.

    Layout-proof: every character is injected as a UTF-16 code unit (VK_PACKET), so
    keyboard-layout remapping (e.g. Arabic) cannot garble it. ``'\\n'``/``'\\r'`` become
    ``VK_RETURN`` and ``'\\t'`` becomes ``VK_TAB`` (pyautogui ``write`` parity). Astral
    characters split into surrogate pairs.
    """
    units: list[tuple[int, int, int]] = []
    for char in text:
        if char in ("\n", "\r"):
            units.extend(((0x0D, 0, 0), (0x0D, 0, _KEYEVENTF_KEYUP)))
            continue
        if char == "\t":
            units.extend(((0x09, 0, 0), (0x09, 0, _KEYEVENTF_KEYUP)))
            continue
        encoded = char.encode("utf_16_le")
        for index in range(0, len(encoded), 2):
            code_unit = int.from_bytes(encoded[index : index + 2], "little")
            units.append((0, code_unit, _KEYEVENTF_UNICODE))
            units.append((0, code_unit, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP))
    return units


def _resolve_key_events(key: str) -> tuple[int, bool]:
    """Resolve one key name to ``(virtual_key, needs_shift)``; unknown -> ValueError.

    Named keys use the static table; other printable characters resolve via
    ``VkKeyScanW`` (pyautogui parity: layout-dependent, shift flag honored).
    """
    named = _VK_NAMED_KEYS.get(key)
    if named is not None:
        return (named, False)
    if len(key) == 1:
        lowered = _VK_NAMED_KEYS.get(key.lower())
        if lowered is not None:
            return (lowered, key.lower() != key)  # uppercase letter: inject shift too
    if not IS_WINDOWS or _user32 is None:
        raise ValueError(f"unsupported key: {key!r}")
    try:
        result = int(_user32.VkKeyScanW(ctypes.c_wchar(key))) & 0xFFFF
    except (OSError, AttributeError, ValueError, TypeError):
        result = 0xFFFF  # VkKeyScanW returns -1 (0xFFFF) for unmappable characters
    if result == 0xFFFF:
        raise ValueError(f"unsupported keyboard character or key name: {key!r}")
    vk = result & 0xFF
    needs_shift = bool(result & 0x0100)
    return (vk, needs_shift)


def _chord_key_units(keys: list[str]) -> list[tuple[int, int, int]]:
    """Chord events ``(vk, scan, flags)``: press in order, release in reverse order.

    All events land in ONE SendInput call — injected input is serialized into the input
    stream (never interleaved with real user input), which makes batched chords safer
    than four separate ``keybd_event`` calls. Shift that a member requires (via
    ``VkKeyScanW``) is pressed first and released last (stuck-modifier hygiene).
    """
    resolved = [_resolve_key_events(key) for key in keys]
    units: list[tuple[int, int, int]] = []
    shift_pressed = False
    for vk, needs_shift in resolved:
        if needs_shift and not shift_pressed:
            units.append((0x10, 0, 0))
            shift_pressed = True
        units.append((vk, 0, 0))
    for vk, needs_shift in reversed(resolved):
        units.append((vk, 0, _KEYEVENTF_KEYUP))
        if needs_shift and shift_pressed:
            units.append((0x10, 0, _KEYEVENTF_KEYUP))
            shift_pressed = False
    if shift_pressed:  # unreachable by construction; belt-and-braces release
        units.append((0x10, 0, _KEYEVENTF_KEYUP))
    return units


class InputEngine(ABC):
    """Physical-input contract used by :class:`LocalComputerBackend.execute`.

    Implementations must raise :class:`InputBlockedError` when input is blocked
    (failsafe corner, blocked desktop/UIPI) and must never partially dispatch silently
    (a zero-event success is a contract violation). Pacing attributes
    (``click_interval``/``type_interval``/``drag_interpolate``/``drag_step_pause``)
    are read by the backend to keep stroke/stop-check policy engine-agnostic.
    """

    click_interval: float = 0.0
    type_interval: float = 0.0
    drag_interpolate: bool = False
    drag_step_pause: float = 0.0

    @abstractmethod
    def move(self, x: int, y: int) -> None:
        """Reposition the cursor to physical virtual-screen ``(x, y)``."""

    @abstractmethod
    def click(self, x: int, y: int, clicks: int = 1) -> None:
        """Move to ``(x, y)`` and press/release the left button ``clicks`` times."""

    @abstractmethod
    def mouse_down(self, button: str = "left") -> None:
        """Press a mouse button (drag start)."""

    @abstractmethod
    def mouse_up(self, button: str = "left") -> None:
        """Release a mouse button (drag end; the backend guarantees this runs)."""

    @abstractmethod
    def type_text(self, text: str, before_chunk: Callable[[], None] | None = None) -> None:
        """Type a whole string; ``before_chunk`` runs before every dispatch chunk."""

    @abstractmethod
    def chord(self, keys: list[str]) -> None:
        """Press and release a chord of key names (pyautogui vocabulary)."""

    @abstractmethod
    def scroll(self, delta: int) -> None:
        """Scroll by ``delta`` wheel notches (positive = up, pyautogui parity)."""

    def release_modifiers(self, keys: list[str]) -> None:
        """Synthetic key-up for the NAMED modifiers only (HotkeyGuard ``release`` mode).

        Concrete engines override this; the base implementation is a deliberate no-op
        so existing engine implementations keep working unchanged. Callers must only
        ever name modifiers this session dispatched (the guard matches against its own
        chord log) — a key-up for a key we did not press is never sent.
        """
        return


class PyAutoGuiInputEngine(InputEngine):
    """pyautogui-backed engine: the selectable fallback (same contract, legacy pacing).

    Preserves the pre-SendInput behavior exactly (click interval 0.08 s, per-char
    write + 0.01 s sleep, interpolated drag stroke) except ``PAUSE``: the mission's
    PAUSE economics set it to 0 by default (``CORTEX_PYAUTOGUI_PAUSE`` restores 0.1).
    ``FailSafeException`` maps onto :class:`InputBlockedError` as before.
    """

    def __init__(
        self,
        pyautogui_module: object,
        *,
        pause: float = 0.0,
        click_interval: float = 0.08,
        type_interval: float = _TYPE_INTERVAL_SECONDS,
        drag_interpolate: bool = True,
        drag_step_pause: float = _DRAG_STEP_PAUSE_SECONDS,
        failsafe: bool = True,
    ) -> None:
        self._pa = pyautogui_module
        self._pa.FAILSAFE = failsafe
        self._pa.PAUSE = pause
        self.click_interval = click_interval
        self.type_interval = type_interval
        self.drag_interpolate = drag_interpolate
        self.drag_step_pause = drag_step_pause

    def _perform(self, input_call: Callable[[], None]) -> None:
        """Run one pyautogui input, mapping FailSafeException onto InputBlockedError."""
        try:
            input_call()
        except self._pa.FailSafeException as exc:
            raise InputBlockedError(
                "pyautogui failsafe triggered (mouse in a screen corner); physical input halted."
            ) from exc

    def move(self, x: int, y: int) -> None:
        self._perform(lambda: self._pa.moveTo(x, y))

    def click(self, x: int, y: int, clicks: int = 1) -> None:
        self._perform(lambda: self._pa.click(x, y, clicks=clicks, interval=self.click_interval))

    def mouse_down(self, button: str = "left") -> None:
        self._perform(lambda: self._pa.mouseDown(button=button))

    def mouse_up(self, button: str = "left") -> None:
        self._perform(lambda: self._pa.mouseUp(button=button))

    def type_text(self, text: str, before_chunk: Callable[[], None] | None = None) -> None:
        for char in text:
            if before_chunk is not None:
                before_chunk()
            self._perform(lambda char=char: self._pa.write(char))
            if self.type_interval > 0:
                time.sleep(self.type_interval)

    def chord(self, keys: list[str]) -> None:
        self._perform(lambda: self._pa.hotkey(*keys))

    def release_modifiers(self, keys: list[str]) -> None:
        """Key-up each named modifier via pyautogui (release mode, T8)."""
        for key in keys:
            self._perform(lambda key=key: self._pa.keyUp(key))

    def scroll(self, delta: int) -> None:
        self._perform(lambda: self._pa.scroll(delta))


class SendInputEngine(InputEngine):
    """Raw Win32 SendInput engine (stdlib ctypes, zero new dependencies).

    - Mouse: absolute ``MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK`` events
      normalized 0-65535 over the WHOLE virtual desktop (multi-monitor correct).
    - Keyboard: batched ``KEYEVENTF_UNICODE`` (VK_PACKET) for typing — layout-proof,
      fixes keyboard-layout-dependent garbled input (e.g. Arabic); chords are batched
      into one call and modifiers are always released in reverse order.
    - Failsafe parity: the pyautogui screen-corner check is replicated before every
      dispatch batch and raises the same :class:`InputBlockedError`.
    - Fail-closed: a ``SendInput`` return of 0 (blocked by UIPI or another thread)
      raises :class:`InputBlockedError` — strictly stronger than pyautogui, which
      silently swallows ``PermissionError``/``OSError`` from ``mouse_event``.

    Pacing defaults are zero (batched whole-string typing, minimal drag segments);
    ``type_interval``/``drag_step_pause``/``click_interval`` restore per-event cadence
    when a consumer needs the paced profile. ``_dispatch`` is the injectable thunk for
    tests (mock ``backend_module._user32`` instead — the binding is read at call time).
    """

    def __init__(
        self,
        *,
        failsafe: bool = True,
        click_interval: float = 0.0,
        type_interval: float = SENDINPUT_TYPE_INTERVAL,
        drag_interpolate: bool = False,
        drag_step_pause: float = SENDINPUT_DRAG_STEP_PAUSE,
    ) -> None:
        if not IS_WINDOWS or _user32 is None:
            raise RuntimeError("SendInputEngine requires Windows.")
        self.failsafe = failsafe
        self.click_interval = click_interval
        self.type_interval = type_interval
        self.drag_interpolate = drag_interpolate
        self.drag_step_pause = drag_step_pause

    # -- dispatch plumbing ---------------------------------------------------------------

    def _dispatch(self, events: list[_INPUT]) -> int:
        """The injectable SendInput thunk: returns the number of events accepted."""
        array = (_INPUT * len(events))(*events)
        return int(_user32.SendInput(len(array), array, ctypes.sizeof(_INPUT)))

    def _cursor_in_failsafe_corner(self) -> bool:
        position = get_cursor_position()
        if position is None:
            return False
        width, height = _primary_screen_size()
        corners = {(0, 0), (0, height - 1), (width - 1, 0), (width - 1, height - 1)}
        return position in corners

    def _send(self, events: list[_INPUT]) -> None:
        """Failsafe-check, dispatch one batch, and fail closed on a zero result."""
        if not events:
            return
        if not IS_WINDOWS or _user32 is None:
            raise InputBlockedError("Physical input requires Windows.")
        if self.failsafe and self._cursor_in_failsafe_corner():
            raise InputBlockedError(
                "SendInput failsafe triggered (mouse in a screen corner); physical input halted."
            )
        if self._dispatch(events) == 0:
            raise InputBlockedError(
                "SendInput was blocked (UIPI or another input source); physical input halted."
            )

    # -- InputEngine interface -------------------------------------------------------------

    def move(self, x: int, y: int) -> None:
        self._send([_mouse_move_event(x, y, _virtual_screen_metrics())])

    def click(self, x: int, y: int, clicks: int = 1) -> None:
        if clicks < 1:
            raise ValueError("clicks must be >= 1")
        metrics = _virtual_screen_metrics()
        if clicks == 1 or self.click_interval <= 0:
            events = [_mouse_move_event(x, y, metrics)]
            for _ in range(clicks):
                events.append(_mouse_flag_event(_MOUSEEVENTF_LEFTDOWN))
                events.append(_mouse_flag_event(_MOUSEEVENTF_LEFTUP))
            self._send(events)
            return
        self._send([_mouse_move_event(x, y, metrics),
                    _mouse_flag_event(_MOUSEEVENTF_LEFTDOWN),
                    _mouse_flag_event(_MOUSEEVENTF_LEFTUP)])
        for _ in range(clicks - 1):
            time.sleep(self.click_interval)
            self._send([_mouse_flag_event(_MOUSEEVENTF_LEFTDOWN),
                        _mouse_flag_event(_MOUSEEVENTF_LEFTUP)])

    def mouse_down(self, button: str = "left") -> None:
        try:
            down_flag, _up_flag = _MOUSE_BUTTON_FLAGS[button]
        except KeyError as exc:
            raise ValueError(f"unsupported mouse button: {button!r}") from exc
        self._send([_mouse_flag_event(down_flag)])

    def mouse_up(self, button: str = "left") -> None:
        try:
            _down_flag, up_flag = _MOUSE_BUTTON_FLAGS[button]
        except KeyError as exc:
            raise ValueError(f"unsupported mouse button: {button!r}") from exc
        self._send([_mouse_flag_event(up_flag)])

    def type_text(self, text: str, before_chunk: Callable[[], None] | None = None) -> None:
        if not text:
            return
        units = _text_to_key_units(text)
        if self.type_interval > 0:
            chunk_size = 2  # one character (down+up) per paced chunk
        else:
            chunk_size = _SENDINPUT_CHUNK_EVENTS - (_SENDINPUT_CHUNK_EVENTS % 2)
        events = [_key_event(vk, scan, flags) for vk, scan, flags in units]
        for start in range(0, len(events), chunk_size):
            if before_chunk is not None:
                before_chunk()
            self._send(events[start : start + chunk_size])
            if self.type_interval > 0:
                time.sleep(self.type_interval)

    def chord(self, keys: list[str]) -> None:
        self._send([_key_event(vk, scan, flags) for vk, scan, flags in _chord_key_units(keys)])

    def release_modifiers(self, keys: list[str]) -> None:
        """Key-up each named modifier in ONE SendInput batch (release mode, T8)."""
        events: list[_INPUT] = []
        for key in keys:
            vk, _needs_shift = _resolve_key_events(key)
            events.append(_key_event(vk, 0, _KEYEVENTF_KEYUP))
        self._send(events)

    def scroll(self, delta: int) -> None:
        self._send([_mouse_flag_event(_MOUSEEVENTF_WHEEL, (delta * _WHEEL_DELTA) & 0xFFFFFFFF)])


# --- UIA semantic read (PERF-004): raw ctypes COM, zero dependencies --------------------------


def _guid_from_string(value: str) -> _GUID:
    """Windows ``GUID`` struct from its canonical string form (little-endian layout)."""
    return _GUID.from_buffer_copy(uuid.UUID(value).bytes_le)


def _com_call(pointer: int, index: int, argtypes: tuple[type, ...], *args: object) -> int:
    """Call method ``index`` on a COM interface pointer (stdcall; returns HRESULT).

    Failed HRESULTs (negative) are RETURNED, not raised: property getters legitimately
    fail per-property (unsupported value patterns) and the snapshot degrades per field.
    Unrecoverable plumbing failures (bad vtable access) raise and are caught by callers.
    """
    vtable_ptr = ctypes.cast(ctypes.c_void_p(pointer), ctypes.POINTER(ctypes.c_void_p)).contents
    function_addr = ctypes.cast(ctypes.c_void_p(vtable_ptr.value), ctypes.POINTER(ctypes.c_void_p))[index]
    prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    function = prototype(function_addr)
    try:
        return int(function(ctypes.c_void_p(pointer), *args))
    except OSError as exc:  # WINFUNCTYPE auto-raises on a failed HRESULT
        return int(exc.winerror) if exc.winerror else -1


def _com_release(pointer: int | None) -> None:
    """Release a COM interface pointer (IUnknown::Release, slot 2); never raises."""
    if not pointer:
        return
    try:
        _com_call(pointer, _COM_RELEASE, ())
    except Exception:  # noqa: BLE001,S110 - a failed release must never break an observation
        pass


class UiaSemanticReader:
    """Warm UIA snapshot reader: focused element + bounded children (raw ctypes COM).

    - ``warm()`` at backend init pays the one-time COM/class-object cost (~200 ms) so
      per-observation reads stay in the ~1-10 ms range (research digest Q3).
    - ``read()`` returns ``{"focused": {...}, "elements": [...]}`` bounded by
      ``UIA_MAX_ELEMENTS`` / ``UIA_MAX_DEPTH`` and a hard wall-clock budget; every
      failure degrades to ``None`` — an observation must never fail because of UIA.
    - All calls go through :func:`_com_call` on fixed UIAutomationClient.h vtable
      slots (IUnknown 0-2; IUIAutomation::GetFocusedElement=7,
      CreateTrueCondition=22; IUIAutomationElement::FindAll=6,
      GetCurrentPropertyValue=10; IUIAutomationElementArray::get_Length=3,
      GetElement=4). Property reads use ``GetCurrentPropertyValue`` exclusively so
      only five vtable slots are load-bearing.
    """

    def __init__(self) -> None:
        self._automation: int | None = None
        self._available = False
        self._lock = threading.Lock()
        self._com_initialized_threads: set[int] = set()

    @property
    def available(self) -> bool:
        return self._available

    def _ensure_com(self) -> bool:
        """CoInitializeEx on the calling thread (idempotent; mode-change tolerated)."""
        if not IS_WINDOWS or _ole32 is None:
            return False
        thread_id = threading.get_ident()
        if thread_id in self._com_initialized_threads:
            return True
        try:
            result = int(_ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED))
        except (OSError, AttributeError):
            return False
        if result not in (0, _RPC_E_CHANGED_MODE):
            return False
        self._com_initialized_threads.add(thread_id)
        return True

    def _ensure_automation(self) -> int | None:
        """CoCreateInstance(CLSID_CUIAutomation) once; returns the cached interface pointer."""
        with self._lock:
            if self._automation:
                return self._automation
            if not self._ensure_com() or _ole32 is None:
                return None
            clsid = _guid_from_string(_CLSID_CUIAUTOMATION)
            iid = _guid_from_string(_IID_IUIAUTOMATION)
            out = ctypes.c_void_p()
            try:
                result = int(
                    _ole32.CoCreateInstance(
                        ctypes.byref(clsid), None, _CLSCTX_INPROC_SERVER, ctypes.byref(iid), ctypes.byref(out)
                    )
                )
            except (OSError, AttributeError):
                return None
            if result != 0 or not out.value:
                return None
            self._automation = int(out.value)
            self._available = True
            return self._automation

    def warm(self) -> bool:
        """Warm the COM apartment + automation object; one throwaway focused read.

        Never raises: any failure leaves the reader permanently unavailable and the
        observation's semantic fields stay ``None`` (silent degradation).
        """
        try:
            automation = self._ensure_automation()
            if not automation:
                return False
            self.read()  # one throwaway read; warms focused-element + property paths
            return self._available
        except Exception:  # noqa: BLE001 - warm must never break backend construction
            return False

    # -- property reads ------------------------------------------------------------------

    def _read_property(self, element: int, property_id: int) -> tuple[int, object]:
        """Raw ``GetCurrentPropertyValue`` result as ``(variant_type, python_value)``."""
        variant = _VARIANT()
        result = _com_call(
            element, _UIA_ELEMENT_GET_PROPERTY_VALUE, (ctypes.c_long, ctypes.POINTER(_VARIANT)),
            property_id, ctypes.byref(variant),
        )
        if result != 0 or variant.vt == _VT_EMPTY:
            return (_VT_EMPTY, None)
        variant_type = int(variant.vt)  # captured before VariantClear resets it
        if variant_type == _VT_BSTR:
            value = ctypes.c_wchar_p(variant.union.bstrVal).value if variant.union.bstrVal else None
        elif variant_type == _VT_I4:
            value = int(variant.union.lVal)
        elif variant_type == _VT_BOOL:
            value = bool(variant.union.boolVal)
        elif variant_type == (_VT_ARRAY | _VT_R8):
            value = _safe_array_doubles(variant.union.parray)
        else:
            value = None
        try:
            _oleaut32.VariantClear(ctypes.byref(variant))
        except (OSError, AttributeError, ValueError):
            pass
        return (variant_type, value)

    def _element_summary(
        self, element: int, deadline: float, *, focused: bool = False
    ) -> dict[str, object]:
        """Bounded summary dict for one element (property reads + rect)."""
        _vtype, name = self._read_property(element, _UIA_PROP_NAME)
        if time.perf_counter() > deadline:
            name = None  # budget exceeded: still return the partial summary
        _vtype, control_type = self._read_property(element, _UIA_PROP_CONTROL_TYPE)
        _vtype, automation_id = self._read_property(element, _UIA_PROP_AUTOMATION_ID)
        _vtype, value = self._read_property(element, _UIA_PROP_VALUE)
        _vtype, offscreen = self._read_property(element, _UIA_PROP_IS_OFFSCREEN)
        _vtype, rect = self._read_property(element, _UIA_PROP_BOUNDING_RECTANGLE)
        rect_tuple = (
            tuple(float(item) for item in rect[:4]) if isinstance(rect, tuple) and len(rect) >= 4 else None
        )
        return {
            "name": name if isinstance(name, str) else None,
            "control_type": UIA_CONTROLTYPE_NAMES.get(int(control_type)) if isinstance(control_type, int) else None,
            "automation_id": automation_id if isinstance(automation_id, str) else None,
            "value": value if isinstance(value, str) else None,
            "offscreen": bool(offscreen) if isinstance(offscreen, bool) else None,
            "rect": rect_tuple,
            "focused": focused,
        }

    def read(self) -> dict[str, object] | None:
        """One bounded semantic snapshot; ``None`` on any failure (silent degradation)."""
        if not IS_WINDOWS or _ole32 is None or _oleaut32 is None:
            return None
        deadline = time.perf_counter() + UIA_READ_BUDGET_SECONDS
        try:
            automation = self._ensure_automation()
            if not automation:
                return None
            focused_ptr = ctypes.c_void_p()
            if _com_call(automation, _UIA_GET_FOCUSED_ELEMENT, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(focused_ptr)) != 0:
                return None
            if not focused_ptr.value:
                return None
            snapshot: dict[str, object] = {
                "focused": self._element_summary(int(focused_ptr.value), deadline, focused=True),
                "elements": [],
            }
            condition_ptr = ctypes.c_void_p()
            if _com_call(automation, _UIA_CREATE_TRUE_CONDITION, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(condition_ptr)) == 0 and condition_ptr.value:
                array_ptr = ctypes.c_void_p()
                found = _com_call(
                    int(focused_ptr.value),
                    _UIA_ELEMENT_FIND_ALL,
                    (ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
                    _TREE_SCOPE_CHILDREN,
                    ctypes.c_void_p(condition_ptr.value),
                    ctypes.byref(array_ptr),
                )
                if found == 0 and array_ptr.value:
                    elements: list[dict[str, object]] = []
                    length = ctypes.c_long(0)
                    if _com_call(int(array_ptr.value), _UIA_ARRAY_GET_LENGTH, (ctypes.POINTER(ctypes.c_long),), ctypes.byref(length)) == 0:
                        for index in range(min(int(length.value), UIA_MAX_ELEMENTS)):
                            if time.perf_counter() > deadline:
                                break
                            child_ptr = ctypes.c_void_p()
                            if _com_call(int(array_ptr.value), _UIA_ARRAY_GET_ELEMENT, (ctypes.c_long, ctypes.POINTER(ctypes.c_void_p)), index, ctypes.byref(child_ptr)) == 0 and child_ptr.value:
                                summary = self._element_summary(int(child_ptr.value), deadline)
                                if summary.get("offscreen") is not True:
                                    elements.append(summary)
                                _com_release(int(child_ptr.value))
                    snapshot["elements"] = elements
                    _com_release(int(array_ptr.value))
                _com_release(int(condition_ptr.value))
            _com_release(int(focused_ptr.value))
            return snapshot
        except Exception:  # noqa: BLE001 - observation must never fail because of UIA
            return None


#: Window-class -> UIA-like control-type name (Win32 fallback; best-effort mapping).
_WIN32_CLASS_CONTROL_TYPES: dict[str, str] = {
    "EDIT": "Edit",
    "RICHEDIT": "Edit",
    "RICHEDIT50W": "Edit",
    "BUTTON": "Button",
    "STATIC": "Text",
    "COMBOBOX": "ComboBox",
    "LISTBOX": "List",
    "SYSLISTVIEW32": "List",
    "SYSHEADER32": "Header",
    "TOOLBARWINDOW32": "ToolBar",
    "STATUSCLASSNAME": "StatusBar",
    "MSCOMCTL.SLIDER": "Slider",
    "MSCTLS_TRACKBAR32": "Slider",
    "MSCTLS_UPDOWN32": "Spinner",
    "MSCTLS_PROGRESS32": "ProgressBar",
    "PROGRESSCLASS": "ProgressBar",
    "TABCONTROL": "Tab",
    "SYSTABCONTROL32": "Tab",
    "SYSDATAGRID": "DataGrid",
    "SCROLLBAR": "ScrollBar",
    "TREEVIEW": "Tree",
    "SYSTREEVIEW32": "Tree",
    "DATETIMEPICK": "Calendar",
    "SYSMONTHCAL32": "Calendar",
}


class Win32TextReader:
    """Win32-window-text semantic reader: the sanctioned fallback when raw UIA COM is
    unavailable (see the module-level rationale on :class:`UiaSemanticReader`).

    Emits the SAME snapshot shape as the UIA reader (``focused`` + bounded ``elements``
    list, every element carrying ``name``/``control_type``/``automation_id``/``value``/
    ``offscreen``/``rect``/``focused``/``source``), sourced from:

    - ``GetGUIThreadInfo`` — the foreground thread's focused control (``hwndFocus``);
    - ``WM_GETTEXT``/``WM_GETTEXTLENGTH`` via ``SendMessageTimeoutW`` (aborts on a hung
      target) — control text becomes ``name`` (buttons/menus) and ``value`` (edits);
    - a BOUNDED child walk (``GetWindow(GW_CHILD)``/``GW_HWNDNEXT``): direct children of
      the foreground root, then the children of the first few of those — at most
      ``UIA_MAX_DEPTH`` levels, ``UIA_MAX_ELEMENTS`` elements, and one wall-clock budget,
      same as the UIA reader.

    Honest limitations (documented in the change-log): ``automation_id`` is always None
    (no Win32 equivalent), ``control_type`` is derived from the window class name, and
    names are limited to text the app exposes as window text.
    """

    def __init__(self) -> None:
        self._available = IS_WINDOWS and _user32 is not None

    @property
    def available(self) -> bool:
        return self._available

    def warm(self) -> bool:
        """No COM apartment to warm; availability is the Win32 binding check."""
        return self._available

    def read(self) -> dict[str, object] | None:
        """One bounded snapshot of the foreground window's focused control + children."""
        if not IS_WINDOWS or _user32 is None:
            return None
        deadline = time.perf_counter() + UIA_READ_BUDGET_SECONDS
        try:
            root = int(_user32.GetForegroundWindow() or 0)
            if not root:
                return None
            focused_summary = self._focused_control_summary(root)
            return {
                "focused": focused_summary,
                "elements": self._bounded_children(root, deadline),
                "source": "win32",
            }
        except Exception:  # noqa: BLE001 - observation must never fail because of UIA
            return None

    def _focused_control_summary(self, root: int) -> dict[str, object] | None:
        """The foreground thread's focused control via ``GetGUIThreadInfo``."""
        try:
            thread_id = _user32.GetWindowThreadProcessId(ctypes.c_void_p(root), None)
            info = _GUITHREADINFO()
            info.cbSize = ctypes.sizeof(_GUITHREADINFO)
            if not thread_id or not _user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
                return None
            hwnd_focus = int(info.hwndFocus or 0)
            if not hwnd_focus:
                return None
            return self._control_summary(hwnd_focus, focused=True)
        except (OSError, AttributeError, ValueError):
            return None

    def _bounded_children(self, root: int, deadline: float) -> list[dict[str, object]]:
        """Two-level, count- and budget-bounded walk of the foreground window's children."""
        elements: list[dict[str, object]] = []
        try:
            level1 = _window_child_chain(root, limit=UIA_MAX_ELEMENTS)
            for index, child in enumerate(level1):
                if len(elements) >= UIA_MAX_ELEMENTS or time.perf_counter() > deadline:
                    break
                summary = self._control_summary(child)
                if summary is not None:
                    elements.append(summary)
                if index < 3:  # budget guard: only the first few hosts get a second level
                    level2 = _window_child_chain(child, limit=UIA_MAX_ELEMENTS - len(elements))
                    for grandchild in level2:
                        if len(elements) >= UIA_MAX_ELEMENTS or time.perf_counter() > deadline:
                            break
                        summary = self._control_summary(grandchild)
                        if summary is not None:
                            elements.append(summary)
        except (OSError, AttributeError, ValueError):
            return elements
        return elements

    def _control_summary(self, hwnd: int, *, focused: bool = False) -> dict[str, object] | None:
        """One control's summary dict; None when the window is gone or inaccessible."""
        try:
            if not _user32.IsWindow(ctypes.c_void_p(hwnd)):
                return None
            class_name = _window_class_name(hwnd)
            text = _window_text_via_message(hwnd)
            visible = bool(_user32.IsWindowVisible(ctypes.c_void_p(hwnd)))
            rect = _window_rect(hwnd)
            control_type = _WIN32_CLASS_CONTROL_TYPES.get((class_name or "").upper(), class_name)
            value = text if (focused or (class_name or "").upper() in {"EDIT", "RICHEDIT", "RICHEDIT50W"}) else None
            return {
                "name": text or None,
                "control_type": control_type,
                "automation_id": None,
                "value": value or None,
                "offscreen": not visible,
                "rect": tuple(float(item) for item in rect) if rect else None,
                "focused": focused,
                "source": "win32",
            }
        except (OSError, AttributeError, ValueError):
            return None


def _window_child_chain(parent: int, *, limit: int) -> list[int]:
    """Sibling chain of ``parent``'s direct children via GetWindow (bounded, read-only)."""
    chain: list[int] = []
    if not IS_WINDOWS or _user32 is None or limit <= 0:
        return chain
    try:
        handle = int(_user32.GetWindow(ctypes.c_void_p(parent), _GW_CHILD) or 0)
        while handle and len(chain) < limit:
            chain.append(handle)
            handle = int(_user32.GetWindow(ctypes.c_void_p(handle), _GW_HWNDNEXT) or 0)
    except (OSError, AttributeError, ValueError):
        return chain
    return chain


def _window_text_via_message(hwnd: int, max_chars: int = 256) -> str:
    """Control text via WM_GETTEXT (SendMessageTimeoutW, abort-if-hung); '' on failure."""
    if not IS_WINDOWS or _user32 is None or not hwnd:
        return ""
    try:
        length = int(_user32.SendMessageTimeoutW(ctypes.c_void_p(hwnd), _WM_GETTEXTLENGTH, 0, 0, _SMTO_ABORTIFHUNG, _SENDMESSAGE_TIMEOUT_MS, None) or 0)
        if length <= 0:
            return ""
        size = min(length, max_chars) + 1
        buffer = ctypes.create_unicode_buffer(size)
        result = ctypes.c_size_t(0)
        copied = int(_user32.SendMessageTimeoutW(
            ctypes.c_void_p(hwnd), _WM_GETTEXT, size, buffer, _SMTO_ABORTIFHUNG, _SENDMESSAGE_TIMEOUT_MS, ctypes.byref(result)
        ) or 0)
        if not copied:
            return ""
        return buffer.value[:max_chars]
    except (OSError, AttributeError, ValueError):
        return ""


def _safe_array_doubles(pointer: int | None) -> tuple[float, ...] | None:
    """Read a 1-D SAFEARRAY of doubles (UIA BoundingRectangle); None on any failure."""
    if not pointer or _oleaut32 is None:
        return None
    access = ctypes.c_void_p()
    try:
        if int(_oleaut32.SafeArrayAccessData(ctypes.c_void_p(pointer), ctypes.byref(access))) != 0:
            return None
        if not access.value:
            return None
        values = tuple(float(ctypes.cast(ctypes.c_void_p(access.value), ctypes.POINTER(ctypes.c_double))[index]) for index in range(4))
        return values
    except (OSError, AttributeError, ValueError, IndexError):
        return None
    finally:
        try:
            _oleaut32.SafeArrayUnaccessData(ctypes.c_void_p(pointer))
        except (OSError, AttributeError, ValueError):
            pass


def _uia_rect_to_screenshot(
    rect: tuple[float, float, float, float] | None,
    origin: tuple[int, int],
    scale: tuple[float, float],
) -> tuple[int, int, int, int] | None:
    """Convert a physical UIA rect to screenshot-local ``(x, y, w, h)`` (None if empty)."""
    if rect is None:
        return None
    left, top, width, height = rect
    if width <= 0 or height <= 0:
        return None
    scale_x = scale[0] if scale[0] > 0 else 1.0
    scale_y = scale[1] if scale[1] > 0 else 1.0
    x = max(0, round((left - origin[0]) / scale_x))
    y = max(0, round((top - origin[1]) / scale_y))
    return (x, y, max(1, round(width / scale_x)), max(1, round(height / scale_y)))


def _uia_snapshot_to_ui_elements(snapshot: dict[str, object]) -> list[dict[str, object]]:
    """Snapshot -> ``Observation.ui_elements`` payload (focused element first)."""
    elements: list[dict[str, object]] = []
    focused = snapshot.get("focused")
    if isinstance(focused, dict):
        elements.append(focused)
    children = snapshot.get("elements")
    if isinstance(children, list):
        for element in children:
            if isinstance(element, dict):
                elements.append(element)
    return elements[: UIA_MAX_ELEMENTS + 1]


def _uia_snapshot_to_text_regions(
    snapshot: dict[str, object],
    origin: tuple[int, int],
    scale: tuple[float, float],
) -> list[TextRegion]:
    """Snapshot -> ``Observation.ocr_text`` regions (screenshot-local, bounded)."""
    regions: list[TextRegion] = []
    candidates: list[object] = []
    focused = snapshot.get("focused")
    if isinstance(focused, dict):
        candidates.append(focused)
    children = snapshot.get("elements")
    if isinstance(children, list):
        candidates.extend(item for item in children if isinstance(item, dict))
    for element in candidates:
        if len(regions) >= UIA_MAX_ELEMENTS:
            break
        rect = element.get("rect")
        if not isinstance(rect, tuple) or len(rect) != 4:
            continue
        converted = _uia_rect_to_screenshot(
            (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])), origin, scale
        )
        if converted is None:
            continue
        text = element.get("name") or element.get("control_type") or ""
        if not text:
            continue
        x, y, width, height = converted
        regions.append(TextRegion(text=text[:200], x=x, y=y, width=width, height=height))
    return regions


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
    def execute(
        self,
        action: GroundedAction,
        stop: StopToken | None = None,
        focus_hook: Callable[[], None] | None = None,
        allow_launch: bool = False,
    ) -> str:
        """Execute one grounded action and return a human-readable result message.

        ``stop`` is optional for backward compatibility: when None no cancellation checks
        are possible and the action still executes. Implementations must call
        ``StopToken.ensure_live()`` immediately before every physical input call.
        Raises ``TaskStopped`` when a provided stop token has fired (zero inputs are
        performed in that case).

        ``focus_hook`` (T8, trailing optional): for ``type`` actions, a zero-arg callable
        invoked before every dispatch chunk AFTER the stop-token check (the FocusGuard's
        per-chunk focus-continuity probe piggybacks this cadence). Implementations
        without chunking invoke it once before dispatching; the hook may raise
        :class:`FocusDriftError` to abort the in-flight type. Omitted (legacy callers)
        keeps behavior byte-identical.

        ``allow_launch`` (T8, trailing optional): the ONLY path through which
        ``ensure_app`` may spawn a process (host-authorized server-side launch policy);
        the default False keeps ensure_app a pure attach-probe.
        """

    def query_foreground_window(self) -> WindowInfo | None:
        """Strong identity of the current foreground window; ``None`` when unavailable."""
        return None

    def is_window_alive(self, hwnd: int | None) -> bool:
        """Whether a previously bound window still exists (B6 TARGET_GONE probe).

        Default ``True``: backends that cannot track liveness keep the conservative
        rejection semantics (TARGET_GONE never fires on a guess).
        """
        return True

    def is_window_owned_by(self, hwnd: int | None, ancestor_hwnd: int | None) -> bool:
        """GW_OWNER-chain ownership probe (B11 shared rule); default ``False``.

        Backends that cannot compute ownership keep the conservative semantics (the
        guard then relies on the same-process rule only).
        """
        return False

    def query_focus_target(self) -> dict[str, object] | None:
        """Keyboard-focus target probe (T8 mechanism iv); ``None`` = cannot verify."""
        return None

    def detect_system_dialog(
        self, bound_hwnd: int | None, title_table: Sequence[str] | None = None
    ) -> dict[str, object] | None:
        """Modal-dialog foreground probe (T8 mechanism iii); ``None`` = no dialog found."""
        return None

    def enumerate_app_windows(self, process_name: str) -> list[AppWindowCandidate]:
        """Process-filtered window enumeration for attach-or-launch (T8 mechanism ii)."""
        return []

    def query_stuck_modifiers(self, keys: Sequence[str]) -> list[str]:
        """Modifiers currently held down before a chord (T8 mechanism v); empty = clear."""
        return []

    def release_modifiers(self, keys: list[str]) -> None:
        """Synthetic key-up for NAMED session-dispatched modifiers (release mode, T8)."""
        return

    def focus_window_title(self, title: str) -> str:
        """Refocus primitive for the guard's ``refocus_then_abort`` policy (T8).

        Reuses the verified foreground-switch sequence; raises
        :class:`WindowFocusError` on refusal (never a silent failure).
        """
        raise UnsupportedActionError("This backend cannot focus windows by title.")

    def ensure_app(self, target: str, allow_launch: bool = False) -> str:
        """Attach-or-launch probe (T8 mechanism ii); REATTACHED/AMBIGUOUS_INSTANCE/NO_INSTANCE."""
        raise UnsupportedActionError("This backend cannot ensure applications.")

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
    """Real Windows backend: Win32 identity/DPI/monitors + stop-checked engine input.

    The physical-input hot path is a pluggable :class:`InputEngine`:

    - ``sendinput`` (default): raw Win32 ``SendInput`` via ctypes — batched absolute
      mouse moves/clicks, layout-proof ``KEYEVENTF_UNICODE`` typing, minimal-segment
      drags; no per-call pacing cost (PERF-004).
    - ``pyautogui`` (selectable fallback, ``CORTEX_INPUT_BACKEND=pyautogui``): the
      legacy path with ``PAUSE=0`` by default (``CORTEX_PYAUTOGUI_PAUSE`` restores it).

    Both engines honor the same contract: failsafe screen-corner checks raise
    :class:`InputBlockedError`, and ``execute`` checks the stop token before every
    physical input.
    """

    def __init__(
        self,
        *,
        input_engine: str | None = None,
        png_optimize: bool | None = None,
        uia_read: bool | None = None,
        type_interval: float | None = None,
        pyautogui_pause: float | None = None,
        drag_interpolate: bool | None = None,
    ) -> None:
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
        self._engine = self._build_input_engine(
            input_engine, type_interval, pyautogui_pause, drag_interpolate
        )
        self.png_optimize: bool = (
            _env_bool(PNG_OPTIMIZE_ENV, False) if png_optimize is None else png_optimize
        )
        self._uia_enabled: bool = _env_bool(UIA_READ_ENV, True) if uia_read is None else uia_read
        self._semantic_reader: UiaSemanticReader | Win32TextReader | None = None
        if self._uia_enabled:
            uia_reader = UiaSemanticReader()
            # Raw UIA COM first; the Win32 window-text reader is the sanctioned fallback
            # (PERF-004: this hardening-affected box refuses IUIAutomation from
            # CUIActivation via raw COM in every process — see change-log).
            self._semantic_reader = uia_reader if uia_reader.warm() else Win32TextReader()
        if self._semantic_reader is not None:
            self._semantic_reader.warm()  # one-time warm-up; never raises
        self._refresh_monitors()
        # B3 settle/gap policy: monotonic timestamp of the last keyboard dispatch
        # (0.0 = epoch; the first dispatch of a session never waits).
        self._last_key_dispatch: float = 0.0
        # B8 settle policy: monotonic timestamp of the last focus transition.
        self._last_focus_transition: float = 0.0

    def _build_input_engine(
        self,
        input_engine: str | None,
        type_interval: float | None,
        pyautogui_pause: float | None,
        drag_interpolate: bool | None,
    ) -> InputEngine:
        """Resolve the input engine from the argument/env/default precedence."""
        name = (input_engine or os.getenv(INPUT_BACKEND_ENV) or "sendinput").strip().lower()
        if name not in {"sendinput", "pyautogui"}:
            raise ValueError(
                f"unknown input engine {name!r}: expected 'sendinput' or 'pyautogui'"
            )
        interval = (
            _env_nonnegative_float(TYPE_INTERVAL_ENV, SENDINPUT_TYPE_INTERVAL)
            if type_interval is None
            else max(0.0, type_interval)
        )
        engine: InputEngine
        if name == "pyautogui":
            pause = (
                _env_nonnegative_float(PYAUTOGUI_PAUSE_ENV, 0.0)
                if pyautogui_pause is None
                else max(0.0, pyautogui_pause)
            )
            engine = PyAutoGuiInputEngine(self._pyautogui, pause=pause, type_interval=interval)
        else:
            engine = SendInputEngine(type_interval=interval)
        if drag_interpolate is not None:
            engine.drag_interpolate = drag_interpolate
        else:
            env_flag = os.getenv(DRAG_INTERPOLATE_ENV)
            if env_flag is not None and env_flag.strip():
                engine.drag_interpolate = _env_bool(DRAG_INTERPOLATE_ENV, engine.drag_interpolate)
        return engine

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
        fails; window/cursor identity and the UIA semantic read degrade to None instead
        of failing.
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
        ocr_text, ui_elements = self._uia_semantic_fields(monitor, verdict)
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
            ocr_text=ocr_text,
            ui_elements=ui_elements,
            redactions_applied=False,
        )

    def _uia_semantic_fields(
        self, monitor: MonitorInfo, verdict: CoordinateVerdict
    ) -> tuple[list[TextRegion] | None, list[dict[str, object]] | None]:
        """Semantic snapshot -> the optional ``ocr_text``/``ui_elements`` fields.

        Silent degradation everywhere: an unavailable reader, a failed COM call, or an
        exceeded read budget leaves both fields ``None`` (or returns the partial data
        already gathered) and never raises into the observation loop.
        """
        reader = self._semantic_reader
        if reader is None or not reader.available:
            return (None, None)
        try:
            snapshot = reader.read()
        except Exception:  # noqa: BLE001 - a broken reader must never break an observation
            return (None, None)
        if snapshot is None:
            return (None, None)
        origin = (monitor.bounds[0], monitor.bounds[1])
        scale = (verdict.scale_x, verdict.scale_y)
        try:
            ui_elements = _uia_snapshot_to_ui_elements(snapshot)
            ocr_text = _uia_snapshot_to_text_regions(snapshot, origin, scale)
        except Exception:  # noqa: BLE001 - conversion must never break an observation
            return (None, None)
        return (ocr_text or None, ui_elements or None)

    def _grab_png(self, left: int, top: int, width: int, height: int) -> tuple[str, int, int]:
        """Capture a monitor region and return ``(base64_png, width, height)``.

        PNG encoding defaults to ``optimize=False`` (PERF-004: −201 ms/frame measured,
        and a slightly SMALLER payload on real UI content); ``CORTEX_PNG_OPTIMIZE=1``
        (or the ``png_optimize`` constructor argument) restores the old behavior.
        """
        try:
            with self._mss_factory() as capture:
                raw = capture.grab({"left": left, "top": top, "width": width, "height": height})
            image = Image.frombytes("RGB", raw.size, raw.rgb)
        except Exception as exc:
            raise DisplayUnavailableError(f"Screen capture failed: {exc}") from exc
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=self.png_optimize)
        return base64.b64encode(output.getvalue()).decode("ascii"), image.width, image.height

    def execute(
        self,
        action: GroundedAction,
        stop: StopToken | None = None,
        focus_hook: Callable[[], None] | None = None,
        allow_launch: bool = False,
    ) -> str:
        """Execute one action; the stop token is checked before every physical input.

        The physical input itself is dispatched by the selected :class:`InputEngine`
        (SendInput by default, pyautogui fallback). Every engine dispatch is preceded by
        a stop-token check (per typing chunk / drag segment, exactly like per pyautogui
        call before), and engines raise :class:`InputBlockedError` on failsafe/blocked
        input, preserving the pre-SendInput error semantics. ``focus_hook`` (T8) runs
        before every ``type`` dispatch chunk after the stop check (focus continuity).
        """
        if stop is not None:
            stop.ensure_live()
        engine = self._engine
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
            engine.click(physical[0], physical[1], clicks=clicks)
        elif action.action == "drag":
            if action.point is None or action.to_point is None:
                raise ValueError("Both a start point and an end point are required for drag actions")
            start = self._map_to_physical(action.point.x, action.point.y)
            end = self._map_to_physical(action.to_point.x, action.to_point.y)
            if stop is not None:
                stop.ensure_live()  # before move(start)
            engine.move(start[0], start[1])
            if stop is not None:
                stop.ensure_live()  # before mouseDown
            button_down = False
            drag_error: BaseException | None = None
            try:
                engine.mouse_down("left")
                button_down = True
                for segment_x, segment_y in self._drag_waypoints(start, end):
                    if stop is not None:
                        stop.ensure_live()  # before every stroke segment
                    engine.move(segment_x, segment_y)
                    if engine.drag_step_pause > 0:
                        time.sleep(engine.drag_step_pause)
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
                        engine.mouse_up("left")
                    except Exception:
                        if drag_error is None:
                            raise
        elif action.action == "move":
            if action.point is None:
                raise ValueError("A point is required for move actions")
            physical = self._map_to_physical(action.point.x, action.point.y)
            if stop is not None:
                stop.ensure_live()  # before the single cursor reposition
            engine.move(physical[0], physical[1])
            time.sleep(_MOVE_SETTLE_SECONDS)  # settle only; a move is not an input press
        elif action.action == "type":
            if action.text is None:
                raise ValueError("Text is required for type actions")
            if stop is not None:
                stop.ensure_live()
            self._apply_focus_settle()  # B8 settle after a recent focus transition
            before_chunk: Callable[[], None] | None = stop.ensure_live if stop is not None else None
            if focus_hook is not None:
                # T8 mechanism iv: the focus-continuity probe piggybacks the per-chunk
                # cadence AFTER the stop-token hook (stop discipline keeps precedence).
                def _chained() -> None:
                    if stop is not None:
                        stop.ensure_live()
                    focus_hook()

                before_chunk = _chained
            engine.type_text(action.text, before_chunk=before_chunk)
            self._last_key_dispatch = time.monotonic()
        elif action.action == "keypress":
            if not action.keys:
                raise ValueError("At least one key is required")
            if stop is not None:
                stop.ensure_live()
            self._apply_focus_settle()  # B8 settle after a recent focus transition
            self._apply_key_dispatch_gap(action.keys)  # B3 settle/gap policy
            engine.chord(list(action.keys))
            self._last_key_dispatch = time.monotonic()
        elif action.action == "hotkey":
            # Sibling of keypress for COMPOUND chords (2..12 keys, passed verbatim in
            # pyautogui vocabulary); single-key presses deliberately stay on keypress.
            guard_keys = [key.strip() for key in action.keys]
            if not 2 <= len(guard_keys) <= 12 or any(not key for key in guard_keys):
                raise ValueError("Hotkey actions require 2 to 12 non-empty key names")
            if stop is not None:
                stop.ensure_live()  # immediately before the single chord input
            self._apply_focus_settle()  # B8 settle after a recent focus transition
            self._apply_key_dispatch_gap(guard_keys)  # B3 settle/gap policy
            engine.chord(list(action.keys))
            self._last_key_dispatch = time.monotonic()
        elif action.action == "scroll":
            if stop is not None:
                stop.ensure_live()
            engine.scroll(action.delta)
        elif action.action == "focus_window":
            return self._execute_focus_window(action)
        elif action.action == "ensure_app":
            # T8 mechanism ii: attach-or-launch probe. Target format
            # ``process[|doc-token]``; NEVER launches unless the host explicitly
            # authorized server-side launches (policy + allow_launch) — the default
            # outcome set is REATTACHED / AMBIGUOUS_INSTANCE / NO_INSTANCE.
            target = (action.target or "").strip()
            if not target:
                raise ValueError("A target is required for ensure_app actions")
            return self.ensure_app(target, allow_launch=allow_launch)
        elif action.action == "done":
            return "No computer action requested."
        else:
            raise UnsupportedActionError(f"Unsupported action: {action.action}")
        return f"Executed {action.action}."

    def _drag_waypoints(self, start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
        """Stroke waypoints for one drag (engine-selected policy).

        Minimal by default on the SendInput engine (move-press-move-release in three
        dispatch batches); ``CORTEX_DRAG_INTERPOLATE=1`` (or ``drag_interpolate``)
        restores the ~40 px interpolated stroke (the pyautogui fallback engine keeps it
        by default for legacy parity). Either way the stop token is checked before every
        segment and the button is always released.
        """
        if self._engine.drag_interpolate:
            return _drag_segment_points(start, end)
        return [end]

    def find_window_by_title(self, target: str) -> WindowInfo | None:
        """Strong identity of the best-matching top-level window (delegates to the resolver).

        PERF-004 P0 bug fix: this method previously never overrode the
        :class:`ComputerBackend` stub and therefore ALWAYS returned ``None`` — the
        working module-level :func:`find_window_by_title` resolver existed but was only
        reachable through ``focus_window``'s internal call. Any consumer probing via the
        backend got false negatives.
        """
        return find_window_by_title(target)

    # --- interference probes + attach-or-launch (T8) -----------------------------------------

    def query_foreground_window(self) -> WindowInfo | None:
        """Strong identity of the current foreground window (the guard's per-dispatch probe)."""
        return query_foreground_window()

    def is_window_alive(self, hwnd: int | None) -> bool:
        """B6: ``IsWindow`` liveness probe for the FocusGuard's bound target."""
        if hwnd is None:
            return False
        try:
            if IS_WINDOWS and _user32 is not None and _user32.IsWindow(ctypes.c_void_p(int(hwnd))):
                return True
        except (OSError, AttributeError, TypeError, ValueError):
            return False
        return False

    def is_window_owned_by(self, hwnd: int | None, ancestor_hwnd: int | None) -> bool:
        """B11: the shared GW_OWNER-chain ownership probe (guard + sentinel aligned)."""
        return is_window_owned_by(hwnd, ancestor_hwnd)

    def query_focus_target(self) -> dict[str, object] | None:
        """Keyboard-focus target via ``GetGUIThreadInfo`` (T8 mechanism iv)."""
        return query_focus_target()

    def detect_system_dialog(
        self, bound_hwnd: int | None, title_table: Sequence[str] | None = None
    ) -> dict[str, object] | None:
        """Modal-dialog foreground probe (T8 mechanism iii); class/owner/title only."""
        return detect_system_dialog(bound_hwnd, title_table)

    def enumerate_app_windows(self, process_name: str) -> list[AppWindowCandidate]:
        """Process-filtered visible-window enumeration for attach-or-launch (T8)."""
        return enumerate_app_windows(process_name)

    def query_stuck_modifiers(self, keys: Sequence[str]) -> list[str]:
        """Pre-chord stuck-modifier sweep via ``GetAsyncKeyState`` (T8 mechanism v)."""
        return query_stuck_modifiers(keys)

    def release_modifiers(self, keys: list[str]) -> None:
        """Synthetic key-up for the named (session-dispatched) modifiers (release mode)."""
        self._engine.release_modifiers(list(keys))

    def focus_window_title(self, title: str) -> str:
        """Refocus primitive reused by the guard's ``refocus_then_abort`` policy."""
        action = GroundedAction(
            action="focus_window", target=title, reason="InterferenceGuard refocus", confidence=1.0
        )
        return self._execute_focus_window(action)

    def ensure_app(self, target: str, allow_launch: bool = False) -> str:
        """Attach-or-launch probe (T8 mechanism ii) — bind to an EXISTING instance.

        Target format: ``process[|doc-token]`` (case-insensitive; the doc token is the
        leading title segment, e.g. ``excel|book1``). Resolution order:

        1. doc-identity match (when a doc token is given) or any visible window of the
           process (when not) -> focus it via the verified foreground switch ->
           ``REATTACHED title=... hwnd=...`` — never launches.
        2. No identity match but unsaved-candidate windows exist -> the structured
           ``AMBIGUOUS_INSTANCE`` payload (unsaved-work risk; the driver decides) —
           never launches, never closes anything.
        3. Nothing matches -> ``NO_INSTANCE target=... launch=...``. Only when the host
           explicitly authorized server-side launches (``allow_launch=True`` AND the
           session policy ``attach_or_launch.launch == "server"`` — enforced by the
           caller) AND the process is resolvable is ``os.startfile`` used; the DEFAULT
           path never spawns a process.
        """
        from .interference import format_ambiguous_instance, format_no_instance, format_reattached

        process_needle, _, doc_needle = target.partition("|")
        process_needle = process_needle.strip()
        doc_needle = doc_needle.strip().casefold()
        candidates = self.enumerate_app_windows(process_needle)
        if not candidates:
            payload = format_no_instance(target, "server" if allow_launch else "driver")
            if allow_launch:
                # REM-C (V-2 F3): a typed needle rejection folds into the NO_INSTANCE
                # payload — nothing is spawned, ``launched=`` stays absent.
                try:
                    launched = self._launch_process(process_needle)
                except LaunchTargetError:
                    return f"{payload} launch_rejected=LaunchTargetError"
                if launched:
                    payload = f"{payload} launched={launched}"
            return payload

        def _doc_matches(candidate: AppWindowCandidate) -> bool:
            if not doc_needle:
                return True
            token = (candidate.doc_token or "").casefold()
            title = (candidate.window.title or "").casefold()
            return doc_needle in token or doc_needle in title

        matches = [item for item in candidates if _doc_matches(item)]
        if matches:
            window = matches[0].window
            self.focus_window_title(window.title)
            return format_reattached(window.title or target, window.hwnd)
        unsaved = [item for item in candidates if item.unsaved_candidate]
        if unsaved:
            return format_ambiguous_instance(unsaved)
        return format_ambiguous_instance(candidates)

    def _launch_process(self, process_needle: str) -> str | None:
        """Best-effort direct launch of an explicitly-authorized process.

        REM-C (V-2 F3/B7.b) hardening:

        - the needle is validated FIRST (:func:`validate_launch_needle`,
          ``^[A-Za-z0-9._ -]+$`` only) — an invalid needle raises
          :class:`LaunchTargetError` BEFORE any spawn (nothing is created);
        - the validated needle is resolved via ``shutil.which`` and spawned as a
          plain one-element argv with ``shell=False`` (the cmd.exe quote-breakout
          surface is gone);
        - any soft failure (``FileNotFoundError`` etc.) keeps the REM-B contract:
          ``None`` (payload degrade), never an exception — only an INVALID needle
          is a typed rejection.

        REM-E (live-test gap, Store execution aliases): on this machine Paint and
        other Microsoft Store apps are reachable ONLY through the execution-alias
        reparse point under ``%LOCALAPPDATA%\\Microsoft\\WindowsApps\\<name>``
        (``shutil.which`` returns None for the bare name — the alias dir is not on
        the resolved PATH as a normal executable — and a raw-name Popen raises
        FileNotFoundError, so the launch degraded to the NO_INSTANCE probe with
        nothing spawned). The alias is now tried BETWEEN ``which`` and the
        raw-name fallback: order is (1) ``shutil.which(needle)``; (2) the Store
        alias path when it EXISTS under ``%LOCALAPPDATA%`` (alias resolution runs
        only on win32); (3) the raw-name fallback (Windows CreateProcess PATH
        search) unchanged. The needle is already charset-validated by then, so
        the alias join can never smuggle a path or metacharacter; launching
        through the alias stays a plain ``shell=False`` Popen on the alias path.
        """
        process_needle = validate_launch_needle(process_needle)  # raises before any spawn
        resolved = shutil.which(process_needle)
        spawn_target = resolved if resolved else process_needle
        if resolved is None and IS_WINDOWS:
            # REM-E: Windows Store execution alias (%LOCALAPPDATA%\Microsoft\
            # WindowsApps\<needle>) — a reparse-point symlink into WindowsApps
            # that which() does not surface. Only the per-user alias dir is
            # readable; C:\Program Files\WindowsApps is ACL-locked, never probed.
            localappdata = os.environ.get("LOCALAPPDATA", "")
            if localappdata:
                alias_path = os.path.join(
                    localappdata, "Microsoft", "WindowsApps", process_needle
                )
                if os.path.exists(alias_path):
                    spawn_target = alias_path
        try:
            subprocess.Popen([spawn_target], shell=False)
            return process_needle
        except Exception:  # noqa: BLE001 - a launch failure degrades to the payload
            return None

    def _apply_key_dispatch_gap(self, keys: list[str]) -> None:
        """B3 settle/gap policy: pace terminal-key chords behind the previous dispatch.

        When ``keys`` contains a terminal key (enter/return/tab) and the previous
        keyboard dispatch happened less than ``KEY_DISPATCH_GAP_SECONDS`` ago, sleep out
        the remainder first. A dropped final Enter in a fast batch (anomaly B3) is an
        input-stack race; the gap is the deliberate, configurable exception to the
        zero-pacing doctrine (default 0.05 s; ``CORTEX_KEY_DISPATCH_GAP=0`` disables).
        """
        if KEY_DISPATCH_GAP_SECONDS <= 0:
            return
        folded = {str(key).strip().casefold() for key in keys}
        if not (folded & TERMINAL_KEYS):
            return
        elapsed = time.monotonic() - self._last_key_dispatch
        remaining = KEY_DISPATCH_GAP_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _execute_focus_window(self, action: GroundedAction) -> str:
        """Bring the window matching ``action.target`` to the foreground (Win32 sequence).

        Sequence: restore when minimized (``IsIconic`` -> ``ShowWindow(SW_RESTORE)``),
        then the standard ``AttachThreadInput`` foreground switch with an ALT
        keybd_event nudge, then verify ``GetForegroundWindow()`` actually moved — a
        refusal raises :class:`WindowFocusError` (documented residual: the previously
        focused window is not restored; the caller re-observes). The stop token is
        checked once by the ``execute`` header; this path performs no pyautogui input.

        B7 (T8): focusing the ALREADY-foreground window is an idempotent no-op success —
        running the AttachThreadInput/ALT-nudge sequence against the current foreground
        can refuse and turn a legitimate re-bind into a spurious WindowFocusError.
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
            elif int(_user32.GetForegroundWindow() or 0) == hwnd:
                self._note_focus_transition()
                return f"Focused window '{candidate.title or target}'."
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
        self._note_focus_transition()
        return f"Focused window '{candidate.title or target}'."

    def _note_focus_transition(self) -> None:
        """B8: record a focus-transition instant for the post-activation settle policy.

        The first keyboard dispatch following a window activation races the freshly
        activated thread's input queue (the A5 residual risk class: dropped/misdelivered
        first keys, the B5 wedge topology). Keyboard actions pace themselves behind
        ``FOCUS_TRANSITION_SETTLE_SECONDS`` (``CORTEX_FOCUS_SETTLE_SECONDS``).
        """
        self._last_focus_transition = time.monotonic()

    def _apply_focus_settle(self) -> None:
        """B8: pace keyboard input behind a recent focus transition (configurable)."""
        if FOCUS_TRANSITION_SETTLE_SECONDS <= 0:
            return
        elapsed = time.monotonic() - self._last_focus_transition
        remaining = FOCUS_TRANSITION_SETTLE_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

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
    simulated as a stop-checked cursor walk (the real backend's INTERPOLATED waypoint
    policy — the SendInput default dispatches a minimal stroke instead, but the
    stop-per-segment and always-release contract is identical): ``drags`` records the mapped physical ``(start, end)`` pairs of completed
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
        # --- T8 interference-probe surface (inert by default; tests inject state) ---
        self.focus_target: dict[str, object] | None = None
        self.system_dialog: dict[str, object] | None = None
        self.app_windows: list[AppWindowCandidate] = []
        self.stuck_modifiers: list[str] = []
        self.released_modifiers: list[str] = []
        # (child_hwnd, ancestor_hwnd) pairs the fake reports as GW_OWNER-owned.
        self.owned_windows: set[tuple[int, int]] = set()
        self.ensure_app_calls: list[str] = []
        self.launched_processes: list[str] = []
        self._last_key_dispatch: float = 0.0
        self._last_focus_transition: float = 0.0

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

    # --- T8 interference-probe surface (mirrors LocalComputerBackend's contract) -----

    def query_foreground_window(self) -> WindowInfo | None:
        """The fake foreground window (``active_window``), like the real identity probe."""
        return self.active_window.model_copy() if self.active_window else None

    def is_window_alive(self, hwnd: int | None) -> bool:
        """A fake window is alive while it is active or listed in ``windows``."""
        if hwnd is None:
            return False
        if self.active_window is not None and self.active_window.hwnd == hwnd:
            return True
        for window in self.windows:
            if isinstance(window, WindowInfo) and window.hwnd == hwnd:
                return True
        return False

    def is_window_owned_by(self, hwnd: int | None, ancestor_hwnd: int | None) -> bool:
        """Injected ownership probe (set ``owned_windows`` pairs to simulate modals)."""
        if hwnd is None or ancestor_hwnd is None:
            return False
        return (int(hwnd), int(ancestor_hwnd)) in self.owned_windows

    def _note_focus_transition(self) -> None:
        """Same B8 transition record as :class:`LocalComputerBackend`."""
        self._last_focus_transition = time.monotonic()

    def _apply_focus_settle(self) -> None:
        """Same B8 settle contract as :class:`LocalComputerBackend`."""
        if FOCUS_TRANSITION_SETTLE_SECONDS <= 0:
            return
        elapsed = time.monotonic() - self._last_focus_transition
        remaining = FOCUS_TRANSITION_SETTLE_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def query_focus_target(self) -> dict[str, object] | None:
        """Injected focus-target probe (set ``focus_target`` to simulate keyboard focus)."""
        return dict(self.focus_target) if self.focus_target is not None else None

    def detect_system_dialog(
        self, bound_hwnd: int | None, title_table: Sequence[str] | None = None
    ) -> dict[str, object] | None:
        """Injected modal-dialog probe (set ``system_dialog`` to simulate a dialog)."""
        return dict(self.system_dialog) if self.system_dialog is not None else None

    def enumerate_app_windows(self, process_name: str) -> list[AppWindowCandidate]:
        """Injected app-window population (set ``app_windows`` to simulate instances)."""
        return list(self.app_windows)

    def query_stuck_modifiers(self, keys: Sequence[str]) -> list[str]:
        """Injected stuck-modifier sweep (set ``stuck_modifiers`` to simulate sticks)."""
        return list(self.stuck_modifiers)

    def release_modifiers(self, keys: list[str]) -> None:
        """Record a release-mode key-up sweep and CLEAR the simulated sticks (T8)."""
        self.released_modifiers.extend(keys)
        self.stuck_modifiers = [name for name in self.stuck_modifiers if name not in set(keys)]

    def _apply_key_dispatch_gap(self, keys: list[str]) -> None:
        """Same B3 settle/gap contract as :class:`LocalComputerBackend`."""
        if KEY_DISPATCH_GAP_SECONDS <= 0:
            return
        folded = {str(key).strip().casefold() for key in keys}
        if not (folded & TERMINAL_KEYS):
            return
        elapsed = time.monotonic() - self._last_key_dispatch
        remaining = KEY_DISPATCH_GAP_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def focus_window_title(self, title: str) -> str:
        """Same verified-refocus contract: a miss raises ``WindowFocusError``."""
        candidate = self.find_window_by_title(title)
        if candidate is None:
            raise WindowFocusError(f"no window matching '{title}'")
        self.active_window = candidate.model_copy()
        self.focused.append(title)
        return f"Focused window '{candidate.title or title}'."

    def ensure_app(self, target: str, allow_launch: bool = False) -> str:
        """Simulated attach-or-launch over the injected ``app_windows`` population."""
        from .interference import format_ambiguous_instance, format_no_instance, format_reattached

        self.ensure_app_calls.append(target)
        process_needle, _, doc_needle = target.partition("|")
        process_needle = process_needle.strip()
        doc_needle = doc_needle.strip().casefold()
        candidates = self.enumerate_app_windows(process_needle)
        if not candidates:
            payload = format_no_instance(target, "server" if allow_launch else "driver")
            if allow_launch:
                # REM-C (V-2 F3): same typed validation as the real backend — a
                # hostile needle is never recorded as spawned and never reaches
                # ``launched=`` (launch_rejected=LaunchTargetError folds into the
                # NO_INSTANCE payload instead).
                try:
                    validate_launch_needle(process_needle)
                except LaunchTargetError:
                    return f"{payload} launch_rejected=LaunchTargetError"
                self.launched_processes.append(process_needle)
                payload = f"{payload} launched={process_needle}"
            return payload

        def _doc_matches(candidate: AppWindowCandidate) -> bool:
            if not doc_needle:
                return True
            token = (candidate.doc_token or "").casefold()
            title = (candidate.window.title or "").casefold()
            return doc_needle in token or doc_needle in title

        matches = [item for item in candidates if _doc_matches(item)]
        if matches:
            window = matches[0].window
            self.focus_window_title(window.title)
            return format_reattached(window.title or target, window.hwnd)
        unsaved = [item for item in candidates if item.unsaved_candidate]
        if unsaved:
            return format_ambiguous_instance(unsaved)
        return format_ambiguous_instance(candidates)

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

    def execute(
        self,
        action: GroundedAction,
        stop: StopToken | None = None,
        focus_hook: Callable[[], None] | None = None,
        allow_launch: bool = False,
    ) -> str:
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
            self._note_focus_transition()  # B8 parity: a fake focus switch is a transition
        elif action.action == "ensure_app":
            target = (action.target or "").strip()
            if not target:
                raise ValueError("A target is required for ensure_app actions")
            return self.ensure_app(target, allow_launch=allow_launch)
        elif action.action in {"keypress", "hotkey"}:
            # B3/B8 settle parity: the fake models both pacing contracts.
            keys = list(action.keys)
            if stop is not None:
                stop.ensure_live()
            self._apply_focus_settle()
            self._apply_key_dispatch_gap(keys)
            self._last_key_dispatch = time.monotonic()
        elif action.action == "type":
            if action.text is None:
                raise ValueError("Text is required for type actions")
            if stop is not None:
                stop.ensure_live()
            self._apply_focus_settle()
            self._last_key_dispatch = time.monotonic()  # B3 clock parity
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
