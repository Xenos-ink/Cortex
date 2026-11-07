"""Win32 bindings: DLL handles, prototypes, and structures shared by the desktop modules."""

from __future__ import annotations

import ctypes
import platform

IS_WINDOWS = platform.system() == "Windows"

GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MDT_EFFECTIVE_DPI = 0
MONITORINFOF_PRIMARY = 1
DEFAULT_DPI = 96
DPI_AWARENESS_CONTEXT_PER_MONITOR_V2 = -4
_SW_RESTORE = 9  # ShowWindow(nCmdShow) restore for a minimized window
_VK_MENU = 0x12  # ALT virtual key (foreground-switch nudge)
_KEYEVENTF_KEYUP = 0x02  # keybd_event flag: key release

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
    # D8 (VAL-WP-R2a-1 F1): consumers import these at top level, and at HEAD they
    # were only read inside IS_WINDOWS-guarded bodies — so the package imported
    # cleanly off Windows. These None placeholders keep that contract.
    MONITORINFOEXW = None
    _MONITORENUMPROC = None
    _MOUSEINPUT = None
    _KEYBDINPUT = None
    _INPUT_UNION = None
    _INPUT = None
    _INPUT_MOUSE = None
    _INPUT_KEYBOARD = None
    _VARIANT = None
    _GUID = None
    _GUITHREADINFO = None
    _GW_CHILD = None
    _GW_HWNDNEXT = None
    _WM_GETTEXT = None
    _SMTO_ABORTIFHUNG = None
    _SENDMESSAGE_TIMEOUT_MS = None
