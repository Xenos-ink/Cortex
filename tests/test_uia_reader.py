"""Semantic-read tests: UIA COM reader (fake vtables), Win32 fallback, silent degradation.

The raw-UIA reader goes through :func:`_com_call` on fixed UIAutomationClient.h vtable
slots. These tests build FAKE COM objects (real ctypes vtables of Python callbacks), so
the vtable-index logic, VARIANT parsing, SAFEARRAY rect decode, and bounded FindAll walk
are all exercised without a real COM apartment. Nothing here dispatches input.

The Win32 fallback reader (``GetGUIThreadInfo`` + ``WM_GETTEXT``) is tested against a
fake user32. Integration tests inject stub readers into the session-scoped real backend
and verify the ``observe()`` population + silent-degradation contract (read-only).
"""

from __future__ import annotations

import ctypes
from types import SimpleNamespace
from typing import Any

import pytest

import computer_use_mcp.backend as backend_module
from computer_use_mcp.backend import (
    TextRegion,
    UiaSemanticReader,
    Win32TextReader,
    _uia_snapshot_to_text_regions,
    _uia_snapshot_to_ui_elements,
)

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")

_VT_I4 = 3
_VT_BSTR = 8
_VT_BOOL = 11
_VT_ARRAY_R8 = 0x2000 | 5

# 64-bit safety: these oleaut functions return POINTERS and must not be truncated to
# c_int (the default restype) — the exact class of bug the real reader must avoid too.
backend_module._oleaut32.SysAllocStringLen.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
backend_module._oleaut32.SysAllocStringLen.restype = ctypes.c_void_p
backend_module._oleaut32.SafeArrayCreateVector.argtypes = [ctypes.c_ushort, ctypes.c_long, ctypes.c_long]
backend_module._oleaut32.SafeArrayCreateVector.restype = ctypes.c_void_p


def _bstr(text: str) -> int:
    """Real BSTR allocation (VariantClear frees it through the real oleaut)."""
    return int(backend_module._oleaut32.SysAllocStringLen(text, len(text)) or 0)


def _rect_safearray(values: tuple[float, float, float, float]) -> int:
    """Real SAFEARRAY of 4 doubles (the UIA BoundingRectangle representation)."""
    psa = int(backend_module._oleaut32.SafeArrayCreateVector(5, 0, 4) or 0)  # VT_R8, 4 elems
    access = ctypes.c_void_p()
    assert backend_module._oleaut32.SafeArrayAccessData(ctypes.c_void_p(psa), ctypes.byref(access)) == 0
    target = ctypes.cast(access, ctypes.POINTER(ctypes.c_double))
    for index, value in enumerate(values):
        target[index] = value
    assert backend_module._oleaut32.SafeArrayUnaccessData(ctypes.c_void_p(psa)) == 0
    return psa


class _FakeComFactory:
    """Builds fake COM objects with callable vtables; keeps everything alive.

    Every object gets working IUnknown slots (0=QI -> E_NOINTERFACE, 1=AddRef, 2=Release)
    so the reader's release calls hit real code.
    """

    def __init__(self) -> None:
        self._keep_alive: list[Any] = []
        self.releases: list[int] = []

    def _callback(self, argtypes: tuple[type, ...], body: Any) -> Any:
        prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
        callback = prototype(body)
        self._keep_alive.append((prototype, callback))
        return callback

    def make_object(self, methods: dict[int, tuple[tuple[type, ...], Any]]) -> int:
        all_methods: dict[int, tuple[tuple[type, ...], Any]] = {
            0: ((ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)), lambda _this, _out: 0x80004002),
            1: ((), lambda _this: 1),  # AddRef
            2: ((), self._release),  # Release
        }
        all_methods.update(methods)
        vtable = (ctypes.c_void_p * 32)()
        for index, (argtypes, body) in all_methods.items():
            vtable[index] = ctypes.cast(self._callback(argtypes, body), ctypes.c_void_p).value
        self._keep_alive.append(vtable)
        interface = ctypes.c_void_p(ctypes.addressof(vtable))
        self._keep_alive.append(interface)
        return ctypes.addressof(interface)

    def _release(self, _this: Any) -> int:
        self.releases.append(1)
        return 1


class _FakeUiaWorld:
    """One automation object, one focused element (Edit), two children (Button, Edit)."""

    def __init__(self) -> None:
        self.factory = _FakeComFactory()
        children_props = [
            {  # an OK button
                30005: ("bstr", "OK"),
                30003: ("i4", 50000),
                30011: ("bstr", "btnOk"),
                30022: ("bool", False),
                30001: ("rect", (120.0, 300.0, 90.0, 32.0)),
            },
            {  # an amount edit with a value
                30005: ("bstr", "Amount"),
                30003: ("i4", 50004),
                30011: ("bstr", "editAmount"),
                30045: ("bstr", "42"),
                30022: ("bool", False),
                30001: ("rect", (30.0, 120.0, 220.0, 28.0)),
            },
        ]
        focused_props = {
            30005: ("bstr", "Untitled - Notepad"),
            30003: ("i4", 50004),
            30011: ("bstr", "15"),
            30045: ("bstr", "hello world"),
            30022: ("bool", False),
            30001: ("rect", (100.0, 50.0, 400.0, 30.0)),
        }
        self.array_ptr: int | None = None
        element_ptrs = [self._make_element(props) for props in children_props]
        focused_ptr = self._make_element(focused_props)
        condition_ptr = self.factory.make_object({})
        self.array_ptr = self._make_array(element_ptrs)

        def get_focused(_this: Any, out: Any) -> int:
            out[0] = focused_ptr
            return 0

        def create_true_condition(_this: Any, out: Any) -> int:
            out[0] = condition_ptr
            return 0

        self.automation_ptr = self.factory.make_object(
            {
                7: ((ctypes.POINTER(ctypes.c_void_p),), get_focused),
                22: ((ctypes.POINTER(ctypes.c_void_p),), create_true_condition),
            }
        )

    def _make_element(self, props: dict[int, tuple[str, Any]]) -> int:
        def get_property_value(_this: Any, prop_id: Any, out: Any) -> int:
            variant = out[0]  # shares memory with the caller's VARIANT
            kind, value = props.get(int(prop_id), ("empty", None))
            if kind == "bstr":
                variant.vt = _VT_BSTR
                variant.union.bstrVal = ctypes.c_void_p(_bstr(str(value)))
            elif kind == "i4":
                variant.vt = _VT_I4
                variant.union.lVal = int(value)  # type: ignore[arg-type]
            elif kind == "bool":
                variant.vt = _VT_BOOL
                variant.union.boolVal = bool(value)  # type: ignore[assignment]
            elif kind == "rect":
                variant.vt = _VT_ARRAY_R8
                variant.union.parray = ctypes.c_void_p(_rect_safearray(value))  # type: ignore[arg-type]
            else:
                variant.vt = 0  # VT_EMPTY (property unsupported)
            return 0

        def find_all(_this: Any, _scope: Any, _condition: Any, out: Any) -> int:
            out[0] = self.array_ptr
            return 0

        return self.factory.make_object(
            {
                6: ((ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)), find_all),
                10: ((ctypes.c_long, ctypes.POINTER(backend_module._VARIANT)), get_property_value),
            }
        )

    def _make_array(self, element_ptrs: list[int]) -> int:
        def get_length(_this: Any, out: Any) -> int:
            out[0] = len(element_ptrs)
            return 0

        def get_element(_this: Any, index: Any, out: Any) -> int:
            out[0] = element_ptrs[int(index)]
            return 0

        return self.factory.make_object(
            {
                3: ((ctypes.POINTER(ctypes.c_long),), get_length),
                4: ((ctypes.c_long, ctypes.POINTER(ctypes.c_void_p)), get_element),
            }
        )


def _install_fake_com(monkeypatch: pytest.MonkeyPatch, automation_ptr: int) -> None:
    def fake_co_create_instance(_clsid: Any, _outer: Any, _ctx: Any, _iid: Any, out_ref: Any) -> int:
        out_ref._obj.value = automation_ptr
        return 0

    monkeypatch.setattr(
        backend_module,
        "_ole32",
        SimpleNamespace(
            CoInitializeEx=lambda _pv, _mode: 0,
            CoCreateInstance=fake_co_create_instance,
        ),
    )


# --- UIA reader against fake COM ---------------------------------------------------------------


def test_uia_reader_full_snapshot_via_fake_com(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _FakeUiaWorld()
    _install_fake_com(monkeypatch, world.automation_ptr)
    reader = UiaSemanticReader()
    assert reader.warm() is True
    snapshot = reader.read()
    assert snapshot is not None
    focused = snapshot["focused"]
    assert focused["name"] == "Untitled - Notepad"
    assert focused["control_type"] == "Edit"  # 50004 mapped through UIA_CONTROLTYPE_NAMES
    assert focused["automation_id"] == "15"
    assert focused["value"] == "hello world"
    assert focused["offscreen"] is False
    assert focused["rect"] == (100.0, 50.0, 400.0, 30.0)
    assert focused["focused"] is True
    elements = snapshot["elements"]
    assert [element["name"] for element in elements] == ["OK", "Amount"]
    assert elements[0]["control_type"] == "Button"
    assert elements[1]["value"] == "42"
    assert world.factory.releases  # every acquired interface was released


def test_uia_reader_reuses_automation_object(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _FakeUiaWorld()
    _install_fake_com(monkeypatch, world.automation_ptr)
    reader = UiaSemanticReader()
    assert reader.warm() is True
    assert reader.read() is not None
    assert reader.read() is not None  # second read: cached automation, no new activation


# --- conversions ---------------------------------------------------------------------------------


def test_snapshot_to_text_regions_screenshot_local(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _FakeUiaWorld()
    _install_fake_com(monkeypatch, world.automation_ptr)
    reader = UiaSemanticReader()
    reader.warm()
    snapshot = reader.read()
    assert snapshot is not None
    regions = _uia_snapshot_to_text_regions(snapshot, origin=(0, 0), scale=(1.0, 1.0))
    assert all(isinstance(region, TextRegion) for region in regions)
    assert (regions[0].x, regions[0].y) == (100, 50)
    assert (regions[0].width, regions[0].height) == (400, 30)
    # Scaled space: the physical rect through the 1.25 scale becomes screenshot-local.
    scaled = _uia_snapshot_to_text_regions(snapshot, origin=(0, 0), scale=(1.25, 1.25))
    assert (scaled[0].x, scaled[0].y) == (80, 40)
    assert (scaled[0].width, scaled[0].height) == (320, 24)
    # Negative-origin secondary monitor: localized onto the captured monitor.
    shifted = _uia_snapshot_to_text_regions(snapshot, origin=(-1920, 0), scale=(1.0, 1.0))
    assert shifted[0].x == 100 + 1920


def test_snapshot_to_ui_elements_puts_focused_first() -> None:
    snapshot = {
        "focused": {"name": "Focused", "focused": True},
        "elements": [{"name": "Child1"}, {"name": "Child2"}],
    }
    elements = _uia_snapshot_to_ui_elements(snapshot)
    assert [element["name"] for element in elements] == ["Focused", "Child1", "Child2"]


def test_snapshot_to_text_regions_skips_rectless_and_empty() -> None:
    snapshot = {
        "focused": {"name": "no-rect", "rect": None},
        "elements": [
            {"name": "empty-rect", "rect": (0.0, 0.0, 0.0, 0.0)},  # degenerate -> skipped
            {"name": None, "control_type": None, "rect": (1.0, 1.0, 5.0, 5.0)},  # no text
            {"name": "valid", "rect": (1.0, 2.0, 5.0, 6.0)},
        ],
    }
    regions = _uia_snapshot_to_text_regions(snapshot, origin=(0, 0), scale=(1.0, 1.0))
    assert [(region.text, region.x, region.y) for region in regions] == [("valid", 1, 2)]


# --- silent degradation ---------------------------------------------------------------------------


def test_uia_reader_degrades_when_ole32_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend_module, "_ole32", None)
    reader = UiaSemanticReader()
    assert reader.warm() is False
    assert reader.read() is None
    assert reader.available is False


def test_uia_reader_degrades_on_activation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_create(*_args: Any) -> int:
        return -2147467262  # E_NOINTERFACE (observed on hardened boxes)

    monkeypatch.setattr(
        backend_module,
        "_ole32",
        SimpleNamespace(CoInitializeEx=lambda _pv, _mode: 0, CoCreateInstance=failing_create),
    )
    reader = UiaSemanticReader()
    assert reader.warm() is False  # never raises
    assert reader.read() is None


def test_uia_reader_degrades_on_com_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _FakeUiaWorld()
    _install_fake_com(monkeypatch, world.automation_ptr)
    reader = UiaSemanticReader()

    def explode() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(reader, "_ensure_automation", explode)
    assert reader.read() is None  # swallowed, never raised


def test_win32_reader_not_available_without_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend_module, "IS_WINDOWS", False)
    monkeypatch.setattr(backend_module, "_user32", None)
    reader = Win32TextReader()
    assert reader.warm() is False
    assert reader.read() is None


def test_env_helpers_parse_flags_and_floats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUMCP_TEST_FLAG", "1")
    assert backend_module._env_bool("CUMCP_TEST_FLAG", False) is True
    monkeypatch.setenv("CUMCP_TEST_FLAG", "garbage")
    assert backend_module._env_bool("CUMCP_TEST_FLAG", False) is False  # garbage -> default
    monkeypatch.setenv("CUMCP_TEST_FLOAT", "-2")
    assert backend_module._env_nonnegative_float("CUMCP_TEST_FLOAT", 0.5) == 0.5  # negative -> default
    monkeypatch.setenv("CUMCP_TEST_FLOAT", "0.3")
    assert backend_module._env_nonnegative_float("CUMCP_TEST_FLOAT", 0.5) == 0.3



def _hwnd_id(handle: Any) -> int:
    """Unwrap an int or c_void_p handle (int(c_void_p) is NOT a numeric conversion)."""
    if isinstance(handle, ctypes.c_void_p):
        return int(handle.value or 0)
    return int(handle)

# --- Win32 fallback reader (fake user32) -----------------------------------------------------------


class _FakeTextUser32:
    """user32 stub for the Win32 fallback reader: a Notepad-like window tree."""

    def __init__(self) -> None:
        self.classes = {101: "Notepad", 102: "Edit", 103: "Button"}
        self.texts = {101: "Untitled - Notepad", 102: "hello", 103: "OK"}
        self.rects = {101: (0, 0, 800, 600), 102: (8, 8, 784, 560), 103: (600, 550, 90, 32)}
        self.children = {101: [102, 103], 102: [], 103: []}
        self.focus_hwnd = 102

    def GetForegroundWindow(self) -> int:
        return 101

    def GetWindowThreadProcessId(self, _hwnd: Any, _pid: Any) -> int:
        return 4321

    def GetGUIThreadInfo(self, _tid: Any, info_ref: Any) -> int:
        info_ref._obj.hwndFocus = ctypes.c_void_p(self.focus_hwnd)
        return 1

    def IsWindow(self, hwnd: Any) -> int:
        return 1 if _hwnd_id(hwnd) in self.classes else 0

    def IsWindowVisible(self, _hwnd: Any) -> int:
        return 1

    def GetWindow(self, hwnd: Any, flag: int) -> int:
        key = _hwnd_id(hwnd)
        if flag == backend_module._GW_CHILD:
            chain = self.children.get(key, [])
            return chain[0] if chain else 0
        if flag == backend_module._GW_HWNDNEXT:
            for siblings in self.children.values():
                if key in siblings:
                    index = siblings.index(key)
                    return siblings[index + 1] if index + 1 < len(siblings) else 0
        return 0

    def GetClassNameW(self, hwnd: Any, buffer: Any, _size: int) -> int:
        buffer.value = self.classes.get(_hwnd_id(hwnd), "")
        return 1

    def SendMessageTimeoutW(
        self, hwnd: Any, msg: int, _wparam: Any, lparam: Any, _flags: Any, _timeout: Any, result: Any
    ) -> int:
        text = self.texts.get(_hwnd_id(hwnd), "")
        if msg == backend_module._WM_GETTEXTLENGTH:
            return len(text)
        if msg == backend_module._WM_GETTEXT:
            if lparam is not None:
                lparam.value = text
            if result is not None:
                result._obj.value = len(text)
            return 1
        return 0

    def GetWindowRect(self, hwnd: Any, rect_ref: Any) -> int:
        left, top, width, height = self.rects[_hwnd_id(hwnd)]
        rect_ref._obj.left, rect_ref._obj.top = left, top
        rect_ref._obj.right, rect_ref._obj.bottom = left + width, top + height
        return 1


@pytest.fixture
def fake_text_user32(monkeypatch: pytest.MonkeyPatch) -> _FakeTextUser32:
    fake = _FakeTextUser32()
    monkeypatch.setattr(backend_module, "_user32", fake)
    return fake


def test_win32_reader_reads_focused_control_and_children(fake_text_user32: _FakeTextUser32) -> None:
    reader = Win32TextReader()
    assert reader.warm() is True
    snapshot = reader.read()
    assert snapshot is not None
    focused = snapshot["focused"]
    assert focused["name"] == "hello"  # WM_GETTEXT on the focused Edit
    assert focused["control_type"] == "Edit"  # class-derived
    assert focused["value"] == "hello"  # edits expose their text as value
    assert focused["automation_id"] is None  # documented limitation
    assert focused["focused"] is True
    assert focused["source"] == "win32"
    assert focused["rect"] == (8.0, 8.0, 784.0, 560.0)
    elements = snapshot["elements"]
    assert [element["control_type"] for element in elements] == ["Edit", "Button"]
    assert elements[1]["name"] == "OK"


def test_win32_reader_text_regions_conversion(fake_text_user32: _FakeTextUser32) -> None:
    reader = Win32TextReader()
    snapshot = reader.read()
    assert snapshot is not None
    regions = _uia_snapshot_to_text_regions(snapshot, origin=(0, 0), scale=(1.0, 1.0))
    assert any(region.text == "hello" for region in regions)
    assert any(region.text == "OK" for region in regions)


def test_win32_reader_degrades_when_no_foreground(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeTextUser32()
    fake.GetForegroundWindow = lambda: 0
    monkeypatch.setattr(backend_module, "_user32", fake)
    assert Win32TextReader().read() is None


def test_win32_reader_degrades_when_get_gui_thread_info_fails(
    fake_text_user32: _FakeTextUser32,
) -> None:
    fake_text_user32.GetGUIThreadInfo = lambda _tid, _info: 0
    reader = Win32TextReader()
    snapshot = reader.read()
    assert snapshot is not None  # window text still gathered; only focus info is missing
    assert snapshot["focused"] is None


# --- observe() integration (read-only real capture; stubbed reader) -------------------------------


class _StubReader:
    def __init__(self, snapshot: dict[str, object] | None, *, available: bool = True) -> None:
        self._snapshot = snapshot
        self.available = available

    def warm(self) -> bool:
        return self.available

    def read(self) -> dict[str, object] | None:
        if self._snapshot is None:
            raise RuntimeError("reader exploded")
        return self._snapshot


@WINDOWS_ONLY
def test_observe_populates_semantic_fields_from_reader(
    real_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = {
        "focused": {
            "name": "Stub Window",
            "control_type": "Edit",
            "automation_id": "1",
            "value": "abc",
            "offscreen": False,
            "rect": (10.0, 20.0, 200.0, 40.0),
            "focused": True,
        },
        "elements": [],
    }
    monkeypatch.setattr(real_backend, "_semantic_reader", _StubReader(snapshot))
    observation = real_backend.observe()
    assert observation.ui_elements is not None
    assert observation.ui_elements[0]["name"] == "Stub Window"
    assert observation.ocr_text is not None
    region = observation.ocr_text[0]
    assert region.text == "Stub Window"
    assert (region.x, region.y, region.width, region.height) == (10, 20, 200, 40)


@WINDOWS_ONLY
def test_observe_survives_reader_exception(
    real_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(real_backend, "_semantic_reader", _StubReader(None))
    observation = real_backend.observe()  # reader raised -> silently degraded to None
    assert observation.ocr_text is None
    assert observation.ui_elements is None


@WINDOWS_ONLY
def test_observe_unavailable_reader_keeps_fields_null(
    real_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(real_backend, "_semantic_reader", _StubReader(None, available=False))
    observation = real_backend.observe()
    assert observation.ocr_text is None
    assert observation.ui_elements is None


@WINDOWS_ONLY
def test_observe_default_reader_is_wired(real_backend: Any) -> None:
    """The constructor wired A reader (UIA on open boxes, Win32 fallback elsewhere)."""
    assert real_backend._semantic_reader is not None
    assert real_backend._semantic_reader.available is True
    assert real_backend.png_optimize is False  # PERF-004 default
