"""Wave 2 platform tests: backend Win32 identity, monitors/DPI, coordinate integrity, stops.

Two layers are covered:
- ``FakeComputerBackend`` — the faithful in-memory backend later waves build on: injected
  monitor sets at 100/125/150% DPI, window moves/focus swaps, coordinate transforms
  (including negative-origin secondary monitors), stop-token interrupts.
- ``LocalComputerBackend`` + module Win32 helpers — window/process identity via mocked
  user32/kernel32 (no real app needed), per-monitor DPI fallback paths, and read-only
  real-desktop smoke tests on this box (Windows only).
"""

from __future__ import annotations

import ctypes
import threading
import time
from types import SimpleNamespace

import pytest

import computer_use_mcp.backend as backend_module
from computer_use_mcp.backend import (
    CoordinateSpace,
    CoordinateSpaceError,
    CoordinateTransform,
    CoordinateVerdict,
    FakeComputerBackend,
    InputBlockedError,
    LocalComputerBackend,
    classify_coordinate_space,
    enumerate_monitors,
    interruptible_wait,
    query_foreground_window,
)
from computer_use_mcp.models import GroundedAction, MonitorInfo, Observation, WindowInfo
from computer_use_mcp.state import StopToken, TaskStopped

if backend_module.IS_WINDOWS:
    import ctypes.wintypes

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")

# The session-scoped ``real_backend`` fixture lives in tests/conftest.py: the process can
# set its DPI awareness only once, so every module shares ONE LocalComputerBackend.


# --- coordinate-space classification (shared by real and fake backends) --------------------


def test_classify_passthrough_when_screenshot_matches_monitor_at_100_percent() -> None:
    monitor = MonitorInfo(id="m", index=0, bounds=(0, 0, 1920, 1080), is_primary=True)
    verdict = classify_coordinate_space(1920, 1080, monitor)
    assert verdict.space is CoordinateSpace.VERIFIED_PASSTHROUGH
    assert (verdict.scale_x, verdict.scale_y) == (1.0, 1.0)
    assert (verdict.input_width, verdict.input_height) == (1920, 1080)


def test_classify_scaled_at_125_percent() -> None:
    monitor = MonitorInfo(id="m", index=0, bounds=(0, 0, 1920, 1080), dpi_scale_x=1.25, dpi_scale_y=1.25)
    verdict = classify_coordinate_space(1536, 864, monitor)
    assert verdict.space is CoordinateSpace.SCALED
    assert verdict.scale_x == pytest.approx(1.25)
    assert verdict.scale_y == pytest.approx(1.25)
    assert (verdict.input_width, verdict.input_height) == (1920, 1080)


def test_classify_scaled_at_150_percent() -> None:
    monitor = MonitorInfo(id="m", index=0, bounds=(0, 0, 1920, 1080), dpi_scale_x=1.5, dpi_scale_y=1.5)
    verdict = classify_coordinate_space(1280, 720, monitor)
    assert verdict.space is CoordinateSpace.SCALED
    assert verdict.scale_x == pytest.approx(1.5)


def test_classify_unverifiable_when_scale_has_no_dpi_match() -> None:
    monitor = MonitorInfo(id="m", index=0, bounds=(0, 0, 1920, 1080), dpi_scale_x=1.25, dpi_scale_y=1.25)
    verdict = classify_coordinate_space(1000, 720, monitor)
    assert verdict.space is CoordinateSpace.UNVERIFIABLE


def test_classify_unverifiable_when_dpi_estimated_even_if_scale_matches() -> None:
    """Estimated DPI must not validate a scaled verdict (fail closed)."""
    monitor = MonitorInfo(id="m", index=0, bounds=(0, 0, 1920, 1080), dpi_scale_x=1.25, dpi_scale_y=1.25)
    verdict = classify_coordinate_space(1536, 864, monitor, dpi_estimated=True)
    assert verdict.space is CoordinateSpace.UNVERIFIABLE


# --- coordinate transform math --------------------------------------------------------------


def test_transform_to_physical_with_scale() -> None:
    transform = CoordinateTransform(origin_x=0, origin_y=0, scale_x=1.25, scale_y=1.25)
    assert transform.to_physical(100, 64) == (125, 80)


def test_transform_negative_origin_secondary_monitor() -> None:
    transform = CoordinateTransform(origin_x=-1920, origin_y=0, scale_x=1.0, scale_y=1.0)
    assert transform.to_physical(100, 50) == (-1820, 50)
    scaled = CoordinateTransform(origin_x=-1920, origin_y=-1080, scale_x=1.25, scale_y=1.25)
    assert scaled.to_physical(100, 100) == (-1795, -955)


def test_transform_round_trip() -> None:
    transform = CoordinateTransform(origin_x=-1920, origin_y=0, scale_x=1.5, scale_y=1.5)
    physical = transform.to_physical(321, 173)
    assert transform.to_screenshot(*physical) == (321, 173)


def test_transform_from_observation() -> None:
    monitor = MonitorInfo(id="m", index=0, bounds=(-1920, 0, 1920, 1080), dpi_scale_x=1.25, dpi_scale_y=1.25)
    observation = FakeComputerBackend(
        width=1536,
        height=864,
        monitors=[monitor],
    ).observe()
    transform = CoordinateTransform.from_observation(observation)
    assert transform.to_physical(0, 0) == (-1920, 0)


# --- FakeComputerBackend: verdicts through the full observe() path ---------------------------


def test_fake_backend_default_is_passthrough() -> None:
    observation = FakeComputerBackend().observe()
    assert observation.coordinate_space is CoordinateSpace.VERIFIED_PASSTHROUGH
    assert observation.coordinate_space_verified is True
    assert (observation.width, observation.height) == (1280, 720)


def test_fake_backend_scaled_observation_at_125_percent() -> None:
    monitor = MonitorInfo(id="m", index=0, bounds=(0, 0, 1920, 1080), dpi_scale_x=1.25, dpi_scale_y=1.25)
    backend = FakeComputerBackend(width=1536, height=864, monitors=[monitor])
    observation = backend.observe()
    assert observation.coordinate_space is CoordinateSpace.SCALED
    assert observation.coordinate_space_verified is True
    assert observation.coordinate_scale_x == pytest.approx(1.25)
    assert (observation.input_width, observation.input_height) == (1920, 1080)


def test_fake_backend_cursor_localized_through_scale_and_negative_origin() -> None:
    monitor = MonitorInfo(id="m", index=0, bounds=(-1920, 0, 1920, 1080), dpi_scale_x=1.25, dpi_scale_y=1.25)
    backend = FakeComputerBackend(width=1536, height=864, monitors=[monitor])
    backend.set_cursor((-1800, 250))  # physical virtual-screen coordinates
    observation = backend.observe()
    assert (observation.cursor_x, observation.cursor_y) == (96, 200)
    assert observation.monitor is not None
    assert observation.monitor.bounds == (-1920, 0, 1920, 1080)


def test_fake_backend_click_maps_to_physical_point_on_secondary_monitor() -> None:
    monitor = MonitorInfo(id="m", index=0, bounds=(-1920, 0, 1920, 1080), is_primary=True)
    backend = FakeComputerBackend(width=1920, height=1080, monitors=[monitor])
    backend.execute(GroundedAction(action="click", point={"x": 100, "y": 100}))
    assert backend._cursor_physical == (-1820, 100)


def test_fake_backend_click_moves_cursor_for_next_observation() -> None:
    backend = FakeComputerBackend(width=1920, height=1080)
    backend.execute(GroundedAction(action="click", point={"x": 300, "y": 400}))
    observation = backend.observe()
    assert (observation.cursor_x, observation.cursor_y) == (300, 400)


def test_fake_backend_execute_refuses_unverifiable_coordinates() -> None:
    """Coordinate-space mismatch without a known scale must block coordinate input."""
    monitor = MonitorInfo(id="m", index=0, bounds=(0, 0, 1920, 1080), dpi_scale_x=1.25, dpi_scale_y=1.25)
    backend = FakeComputerBackend(width=1536, height=864, monitors=[monitor])
    backend.set_screenshot_size(1000, 720)
    with pytest.raises(CoordinateSpaceError):
        backend.execute(GroundedAction(action="click", point={"x": 10, "y": 10}))
    assert backend.executed == []


def test_fake_backend_input_blocked_raises_and_records_nothing() -> None:
    backend = FakeComputerBackend(input_blocked=True)
    with pytest.raises(InputBlockedError):
        backend.execute(GroundedAction(action="keypress", keys=["ctrl", "c"]))
    assert backend.executed == []


# --- FakeComputerBackend: window identity moves/swap detection ------------------------------


def test_fake_backend_window_move_detected_between_observations() -> None:
    backend = FakeComputerBackend(active_window=WindowInfo(title="Notepad", bounds=(0, 0, 800, 600)))
    before = backend.observe()
    backend.set_window_bounds((100, 40, 800, 600))
    after = backend.observe()
    assert before.active_window_info is not None and after.active_window_info is not None
    assert before.active_window_info.bounds == (0, 0, 800, 600)
    assert after.active_window_info.bounds == (100, 40, 800, 600)
    assert before.observation_id != after.observation_id


def test_fake_backend_foreground_window_swap_between_observations() -> None:
    backend = FakeComputerBackend(active_window=WindowInfo(title="Notepad", bounds=(0, 0, 800, 600)))
    before = backend.observe()
    backend.set_active_window(WindowInfo(title="Calculator", bounds=(200, 200, 400, 400)))
    after = backend.observe()
    assert before.active_window == "Notepad"
    assert after.active_window == "Calculator"
    assert after.active_window_info is not None
    assert after.active_window_info.title == "Calculator"


def test_fake_backend_screenshot_size_change_is_reflected() -> None:
    backend = FakeComputerBackend()
    backend.set_screenshot_size(800, 600)
    observation = backend.observe()
    assert (observation.width, observation.height) == (800, 600)


# --- stop-token interrupts -------------------------------------------------------------------


def test_fake_execute_with_prestopped_token_raises_and_records_nothing() -> None:
    backend = FakeComputerBackend()
    stop = StopToken()
    stop.stop()
    with pytest.raises(TaskStopped):
        backend.execute(GroundedAction(action="click", point={"x": 1, "y": 2}), stop=stop)
    assert backend.executed == []


@WINDOWS_ONLY
def test_real_execute_with_prestopped_token_raises_before_any_input(real_backend) -> None:
    stop = StopToken()
    stop.stop()
    with pytest.raises(TaskStopped):
        real_backend.execute(GroundedAction(action="click", point={"x": 1, "y": 2}), stop=stop)


def test_fake_wait_action_interrupted_midway() -> None:
    backend = FakeComputerBackend()
    stop = StopToken()
    timer = threading.Timer(0.25, stop.stop)
    timer.start()
    started = time.perf_counter()
    with pytest.raises(TaskStopped):
        backend.execute(GroundedAction(action="wait", delta=5), stop=stop)
    elapsed = time.perf_counter() - started
    timer.join()
    assert elapsed < 2.0


def test_wait_returns_early_when_stopped() -> None:
    stop = StopToken()
    timer = threading.Timer(0.25, stop.stop)
    timer.start()
    started = time.perf_counter()
    waited = FakeComputerBackend().wait(2.0, stop=stop)
    elapsed = time.perf_counter() - started
    timer.join()
    assert 0.2 <= waited < 1.5
    assert elapsed < 1.5


def test_wait_full_duration_without_stop() -> None:
    started = time.perf_counter()
    waited = interruptible_wait(0.2)
    assert waited >= 0.2
    assert time.perf_counter() - started >= 0.2


def test_wait_with_never_fired_token_completes() -> None:
    waited = FakeComputerBackend().wait(0.2, stop=StopToken())
    assert waited >= 0.2


# --- WindowInfo extraction via mocked Win32 --------------------------------------------------


def _fake_user32(
    foreground: int,
    ancestor: int | None = None,
    *,
    title: str = "Test Window",
    window_class: str = "Notepad",
    pid: int = 4321,
    rect: tuple[int, int, int, int] = (10, 20, 330, 220),
) -> SimpleNamespace:
    root = ancestor if ancestor is not None else foreground

    def get_text_length(hwnd: int) -> int:
        return len(title) if hwnd == root else 0

    def get_text(hwnd: int, buffer, size: int) -> int:  # type: ignore[no-untyped-def]
        if hwnd == root:
            buffer.value = title
            return len(title)
        return 0

    def get_class(hwnd: int, buffer, size: int) -> int:  # type: ignore[no-untyped-def]
        if hwnd == root:
            buffer.value = window_class
            return 1
        return 0

    def get_pid(hwnd: int, pid_ref) -> int:  # type: ignore[no-untyped-def]
        pid_ref._obj.value = pid
        return 1

    def get_rect(hwnd: int, rect_ref) -> int:  # type: ignore[no-untyped-def]
        left, top, right, bottom = rect
        rect_ref._obj.left, rect_ref._obj.top = left, top
        rect_ref._obj.right, rect_ref._obj.bottom = right, bottom
        return 1

    return SimpleNamespace(
        GetForegroundWindow=lambda: foreground,
        GetAncestor=lambda hwnd, flag: root if hwnd == foreground else 0,
        GetWindowTextLengthW=get_text_length,
        GetWindowTextW=get_text,
        GetClassNameW=get_class,
        GetWindowThreadProcessId=get_pid,
        GetWindowRect=get_rect,
    )


def _fake_kernel32(
    *,
    open_result: int | None = 777,
    exe_path: str = "C:\\Apps\\Notepad\\notepad.exe",
) -> tuple[SimpleNamespace, list[int]]:
    closed: list[int] = []

    def query_image(handle: int, flags: int, buffer, size_ref) -> int:  # type: ignore[no-untyped-def]
        buffer.value = exe_path
        size_ref._obj.value = len(exe_path)
        return 1

    return (
        SimpleNamespace(
            OpenProcess=lambda access, inherit, pid: open_result,
            QueryFullProcessImageNameW=query_image,
            CloseHandle=lambda handle: closed.append(int(handle)) or 1,
        ),
        closed,
    )


@WINDOWS_ONLY
def test_window_info_extraction_via_mocked_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    user32 = _fake_user32(foreground=111, ancestor=222)
    kernel32, closed = _fake_kernel32()
    monkeypatch.setattr(backend_module, "_user32", user32)
    monkeypatch.setattr(backend_module, "_kernel32", kernel32)
    window = query_foreground_window()
    assert window is not None
    assert window.hwnd == 222  # root owner, not the child foreground handle
    assert window.pid == 4321
    assert window.process_name == "notepad.exe"
    assert window.exe_path == "C:\\Apps\\Notepad\\notepad.exe"
    assert window.window_class == "Notepad"
    assert window.title == "Test Window"
    assert window.bounds == (10, 20, 320, 200)
    assert closed == [777]  # process handle was closed properly


@WINDOWS_ONLY
def test_window_info_graceful_when_open_process_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    user32 = _fake_user32(foreground=111)
    kernel32, _closed = _fake_kernel32(open_result=0)
    monkeypatch.setattr(backend_module, "_user32", user32)
    monkeypatch.setattr(backend_module, "_kernel32", kernel32)
    window = query_foreground_window()
    assert window is not None
    assert window.pid == 4321
    assert window.exe_path is None
    assert window.process_name is None
    assert window.title == "Test Window"


@WINDOWS_ONLY
def test_window_info_returns_none_when_no_foreground(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend_module, "_user32", _fake_user32(foreground=0))
    assert query_foreground_window() is None


# --- monitor enumeration: per-monitor DPI + fallback paths -----------------------------------


@WINDOWS_ONLY
def test_enumerate_monitors_with_per_monitor_dpi(monkeypatch: pytest.MonkeyPatch) -> None:
    rect = ctypes.wintypes.RECT(0, 0, 1920, 1080)

    def fake_enum(_hdc, _clip, callback, _data) -> int:
        callback(4242, None, ctypes.pointer(rect), None)
        return 1

    def fake_monitor_info(hmonitor: int, info_ref) -> int:
        info = info_ref._obj
        info.dwFlags = backend_module.MONITORINFOF_PRIMARY
        info.szDevice = "\\\\.\\DISPLAY1"
        return 1

    def fake_dpi_for_monitor(hmonitor: int, dpi_type: int, x_ref, y_ref) -> int:
        x_ref._obj.value = 120
        y_ref._obj.value = 120
        return 0  # S_OK

    monkeypatch.setattr(
        backend_module,
        "_user32",
        SimpleNamespace(
            EnumDisplayMonitors=fake_enum,
            GetMonitorInfoW=fake_monitor_info,
            GetDpiForSystem=lambda: 96,
        ),
    )
    monkeypatch.setattr(
        backend_module, "_shcore", SimpleNamespace(GetDpiForMonitor=fake_dpi_for_monitor)
    )
    monitors, estimated = enumerate_monitors(None)
    assert estimated is False
    assert len(monitors) == 1
    assert monitors[0].bounds == (0, 0, 1920, 1080)
    assert monitors[0].is_primary is True
    assert monitors[0].dpi_scale_x == pytest.approx(1.25)
    assert monitors[0].dpi_scale_y == pytest.approx(1.25)
    assert monitors[0].id == "monitor-0"
    assert monitors[0].index == 0


@WINDOWS_ONLY
def test_enumerate_monitors_dpi_falls_back_to_system_when_getdpiformonitor_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rect = ctypes.wintypes.RECT(0, 0, 1920, 1080)

    def fake_enum(_hdc, _clip, callback, _data) -> int:
        callback(4242, None, ctypes.pointer(rect), None)
        return 1

    monkeypatch.setattr(
        backend_module,
        "_user32",
        SimpleNamespace(
            EnumDisplayMonitors=fake_enum,
            GetMonitorInfoW=lambda hmonitor, info_ref: 1,
            GetDpiForSystem=lambda: 120,
        ),
    )
    monkeypatch.setattr(
        backend_module,
        "_shcore",
        SimpleNamespace(GetDpiForMonitor=lambda hmonitor, dpi_type, x_ref, y_ref: 1),
    )
    monitors, estimated = enumerate_monitors(None)
    assert estimated is True
    assert monitors[0].dpi_scale_x == pytest.approx(1.25)
    assert monitors[0].dpi_scale_y == pytest.approx(1.25)


@WINDOWS_ONLY
def test_enumerate_monitors_falls_back_to_mss_list_and_system_dpi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backend_module,
        "_user32",
        SimpleNamespace(
            EnumDisplayMonitors=lambda hdc, clip, callback, data: 1,  # no monitors reported
            GetDpiForSystem=lambda: 120,
        ),
    )
    mss_monitors = [
        {"left": 0, "top": 0, "width": 3840, "height": 1080},  # virtual-desktop union
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
    ]
    monitors, estimated = enumerate_monitors(mss_monitors)
    assert estimated is True
    assert len(monitors) == 1
    assert monitors[0].bounds == (0, 0, 1920, 1080)
    assert monitors[0].is_primary is True
    assert monitors[0].dpi_scale_x == pytest.approx(1.25)


@WINDOWS_ONLY
def test_enumerate_monitors_raises_when_nothing_available(monkeypatch: pytest.MonkeyPatch) -> None:
    from computer_use_mcp.backend import DisplayUnavailableError

    monkeypatch.setattr(
        backend_module,
        "_user32",
        SimpleNamespace(EnumDisplayMonitors=lambda hdc, clip, callback, data: 1),
    )
    with pytest.raises(DisplayUnavailableError):
        enumerate_monitors(None)


# --- DPI awareness recording -----------------------------------------------------------------


@WINDOWS_ONLY
def test_dpi_awareness_fallback_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        backend_module,
        "_user32",
        SimpleNamespace(SetProcessDpiAwarenessContext=lambda ctx: 0, SetProcessDPIAware=lambda: 1),
    )
    monkeypatch.setattr(
        backend_module, "_shcore", SimpleNamespace(SetProcessDpiAwareness=lambda mode: 0)
    )
    assert backend_module._set_process_dpi_awareness() == "per_monitor"

    monkeypatch.setattr(
        backend_module,
        "_user32",
        SimpleNamespace(
            SetProcessDpiAwarenessContext=lambda ctx: 0,
            SetProcessDPIAware=lambda: 0,
            GetThreadDpiAwarenessContext=lambda: 1234,
            GetAwarenessFromDpiAwarenessContext=lambda ctx: 2,
        ),
    )
    monkeypatch.setattr(
        backend_module, "_shcore", SimpleNamespace(SetProcessDpiAwareness=lambda mode: 1)
    )
    assert backend_module._set_process_dpi_awareness() == "per_monitor"

    monkeypatch.setattr(
        backend_module,
        "_user32",
        SimpleNamespace(SetProcessDpiAwarenessContext=lambda ctx: 0, SetProcessDPIAware=lambda: 0),
    )
    monkeypatch.setattr(
        backend_module, "_shcore", SimpleNamespace(SetProcessDpiAwareness=lambda mode: 1)
    )
    assert backend_module._set_process_dpi_awareness() == "unavailable"


@WINDOWS_ONLY
def test_real_backend_records_dpi_awareness(real_backend: LocalComputerBackend) -> None:
    assert real_backend.dpi_awareness in {"per_monitor_v2", "per_monitor", "system"}
    assert real_backend.dpi_estimated is False  # Server 2022 box: shcore per-monitor works


# --- real-desktop read-only smoke (this box: 1920x1080 @ 125%) -------------------------------


@WINDOWS_ONLY
def test_real_observe_populates_full_observation(real_backend: LocalComputerBackend) -> None:
    observation = real_backend.observe()
    assert observation.observation_id
    assert observation.timestamp.tzinfo is not None
    assert observation.width > 0 and observation.height > 0
    assert observation.monitor is not None
    assert observation.monitor.bounds[2] > 0
    assert (observation.width, observation.height) == (
        observation.monitor.bounds[2],
        observation.monitor.bounds[3],
    )
    assert observation.coordinate_space is CoordinateSpace.VERIFIED_PASSTHROUGH
    assert observation.monitor.dpi_scale_x == pytest.approx(1.25)  # 125% box
    assert observation.active_window_info is not None
    assert observation.active_window_info.pid is not None
    assert observation.active_window_info.process_name is not None
    assert observation.active_window_info.bounds is not None
    assert observation.cursor_x is not None and observation.cursor_y is not None
    assert observation.redactions_applied is False


@WINDOWS_ONLY
def test_real_enumerate_monitors_lists_this_box(monkeypatch: pytest.MonkeyPatch) -> None:
    import mss

    with mss.MSS() as capture:
        mss_monitors = list(capture.monitors)
    monitors, _estimated = enumerate_monitors(mss_monitors)
    assert len(monitors) >= 1
    assert any(monitor.is_primary for monitor in monitors)
    for monitor in monitors:
        _left, _top, width, height = monitor.bounds
        assert width > 0 and height > 0
        assert monitor.dpi_scale_x >= 1.0


# --- observation field completeness ----------------------------------------------------------


def test_fake_observation_field_completeness() -> None:
    monitor = MonitorInfo(
        id="m", index=0, bounds=(0, 0, 1920, 1080), is_primary=True, dpi_scale_x=1.25, dpi_scale_y=1.25
    )
    backend = FakeComputerBackend(
        width=1536,
        height=864,
        monitors=[monitor],
        active_window=WindowInfo(
            hwnd=99,
            pid=1000,
            process_name="notepad.exe",
            exe_path="C:\\Apps\\notepad.exe",
            window_class="Notepad",
            title="Notepad",
            bounds=(0, 0, 800, 600),
        ),
    )
    backend.set_cursor((100, 200))
    observation = backend.observe()
    assert isinstance(observation, Observation)
    assert observation.observation_id
    assert observation.timestamp.tzinfo is not None
    assert observation.image_base64
    assert (observation.width, observation.height) == (1536, 864)
    assert observation.monitor is not None and observation.monitor.id == "m"
    assert observation.active_window == "Notepad"
    assert observation.active_window_info is not None
    assert observation.active_window_info.pid == 1000
    # 100/200 are physical virtual-screen pixels; the scaled screenshot localizes them.
    assert (observation.cursor_x, observation.cursor_y) == (80, 160)
    assert (observation.input_width, observation.input_height) == (1920, 1080)
    assert observation.coordinate_scale_x == pytest.approx(1.25)
    assert observation.coordinate_space is CoordinateSpace.SCALED
    assert observation.coordinate_space_verified is True
    assert observation.redactions_applied is False


def test_coordinate_verdict_dataclass_shape() -> None:
    verdict = CoordinateVerdict(
        space=CoordinateSpace.VERIFIED_PASSTHROUGH, scale_x=1.0, scale_y=1.0, input_width=1, input_height=1
    )
    assert verdict.space == "verified_passthrough"
