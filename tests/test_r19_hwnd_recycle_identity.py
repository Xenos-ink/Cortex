"""R-19: the equal-hwnd match verifies process/class/title identity (RT2-1 closure).

Red-team finding RT2-1: the OS recycles hwnd VALUES, so a recycled hwnd can belong to
a FOREIGN process; ``_matches_binding`` used to bless the foreground by hwnd equality
alone. The equal-hwnd path now cross-checks the strongest identity evidence both sides
provide (pid > process name > window class > title overlap); a recycled hwnd owned by
a foreign process falls through to the pid-verified rules and rejects with
``FOCUS_TAKEN_BY``. Stubbed enumeration only — no live input.
"""

from __future__ import annotations

from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.focus_guard import InterferenceGuard
from computer_use_mcp.interference import FOCUS_TAKEN_BY, parse_interference
from computer_use_mcp.models import FailureClass, GroundedAction, WindowInfo

TARGET = WindowInfo(
    hwnd=1, pid=100, process_name="EXCEL.EXE", exe_path="C:\\apps\\EXCEL.EXE",
    window_class="XLMAIN", title="Book1 - Excel",
)
CLICK = GroundedAction(action="click", point={"x": 10, "y": 10}, confidence=1.0)


def _armed(backend: FakeComputerBackend) -> InterferenceGuard:
    backend.set_windows([TARGET])
    guard = InterferenceGuard(backend, parse_interference(None))
    guard.rebind(TARGET)
    return guard


def test_recycled_hwnd_handed_to_foreign_owner_rejects_with_focus_taken_by() -> None:
    """The accept-test: the SAME hwnd value now owned by a foreign process must never
    dispatch — the guard reports FOCUS_TAKEN_BY and the backend runs nothing."""
    backend = FakeComputerBackend()
    guard = _armed(backend)
    recycled = WindowInfo(
        hwnd=1,  # SAME hwnd, totally different owner
        pid=4242,
        process_name="evil.exe",
        window_class="Malware",
        title="Totally Malicious",
    )
    backend.set_active_window(recycled)
    verdict = guard.verify_pre_dispatch(CLICK)
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith(FOCUS_TAKEN_BY)
    assert verdict.failure_class is FailureClass.WRONG_WINDOW
    assert "process=evil.exe" in verdict.event
    assert backend.executed == []  # the foreign window was never acted on


def test_recycled_hwnd_same_class_foreign_pid_still_rejects() -> None:
    """A recycle that even KEEPS the window class is caught by the pid check."""
    backend = FakeComputerBackend()
    guard = _armed(backend)
    recycled = TARGET.model_copy(update={"pid": 4242, "title": "Not The Same"})
    backend.set_active_window(recycled)
    verdict = guard.verify_pre_dispatch(CLICK)
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith(FOCUS_TAKEN_BY)


def test_equal_hwnd_with_equal_pid_still_dispatches() -> None:
    """Regression: the legitimate equal-hwnd match (same process — a doc rename, a
    probe copy) is untouched by the identity cross-check."""
    backend = FakeComputerBackend()
    guard = _armed(backend)
    backend.set_active_window(TARGET.model_copy(update={"title": "Report - Excel"}))
    assert guard.verify_pre_dispatch(CLICK) is None


def test_equal_hwnd_degraded_pid_unknown_process_mismatch_rejects() -> None:
    """Degraded probes (no pid): a process-name contradiction defeats the match."""
    backend = FakeComputerBackend()
    guard = _armed(backend)
    degraded = WindowInfo(hwnd=1, pid=None, process_name="notepad.exe", title="whatever")
    backend.set_active_window(degraded)
    verdict = guard.verify_pre_dispatch(CLICK)
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith(FOCUS_TAKEN_BY)


def test_equal_hwnd_degraded_matching_process_dispatches() -> None:
    backend = FakeComputerBackend()
    guard = _armed(backend)
    degraded = WindowInfo(hwnd=1, pid=None, process_name="excel.exe", title="")
    backend.set_active_window(degraded)
    assert guard.verify_pre_dispatch(CLICK) is None


def test_equal_hwnd_degraded_class_mismatch_rejects_and_class_match_dispatches() -> None:
    """No pid, no process name on either side: window class decides."""
    backend = FakeComputerBackend()
    guard = _armed(backend)
    foreign_class = WindowInfo(hwnd=1, window_class="OtherClass")
    backend.set_active_window(foreign_class)
    verdict = guard.verify_pre_dispatch(CLICK)
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith(FOCUS_TAKEN_BY)

    same_class = WindowInfo(hwnd=1, window_class="XLMAIN")
    backend.set_active_window(same_class)
    assert guard.verify_pre_dispatch(CLICK) is None


def test_equal_hwnd_with_no_comparable_identity_stands() -> None:
    """When NO identity field is comparable (hwnd-only identities), the hwnd is the
    only evidence — the legacy single-probe behavior is preserved, never weakened."""
    backend = FakeComputerBackend()
    guard = _armed(backend)
    bare = WindowInfo(hwnd=1)
    backend.set_active_window(bare)
    assert guard.verify_pre_dispatch(CLICK) is None
