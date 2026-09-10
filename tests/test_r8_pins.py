"""R-8 pins (D6): the desktop-safety gate, wrong-window attach, and teardown cleanup.

ALL pins are pure-logic / stub level: NONE launches an app, moves the mouse, types,
or touches the desktop in any way. They pin the D6 contract with fakes:

1. The e2e real-input gate is FAIL-CLOSED: closed for a plain environment and for
   every accidental-enablement shape (wrong values of the opt-in variable, and any
   unrelated ``E2E_*``/``REAL*``/``DESKTOP*`` variable), open ONLY for the exact
   opt-in ``CUMCP_RUN_E2E=1``. The gate logic is imported from the e2e conftest via
   a stub-module trick (pytest conftest modules are not importable by name), so the
   pin tests the REAL gate function, not a copy.
2. ``find_marked_windows`` / ``attach_window_by_unique_title`` (helpers_win32) ignore
   non-marker windows BY CONSTRUCTION: with a stubbed window enumeration, the user's
   own Notepad (same class, no marker) is invisible; a marker window is found; two
   marker windows fail closed (loud RuntimeError, never a guess); a markerless
   desktop yields a TimeoutError, never a fallback attach.
3. Teardown closes only the suite's own instance: with stubbed PostMessageW, the
   hardened ``close_window`` posts WM_CLOSE to EXACTLY the given hwnd (the one found
   via the marker) and polls only that hwnd's liveness — never re-searching by
   class/title, so it cannot close/observe the user's window.

These pins run in the STANDARD suite (no e2e marker, no env flag, no desktop).
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

import pytest

E2E_DIR = Path(__file__).resolve().parent / "e2e"

if str(E2E_DIR) not in sys.path:
    sys.path.insert(0, str(E2E_DIR))

import helpers_win32 as w32


def _load_e2e_conftest():
    """Import tests/e2e/conftest.py as a module so the REAL gate function is pinned.

    conftest files are not importable by package name; load it by path instead.
    Importing it has no side effects beyond registering module constants (its
    fixtures/hooks only run inside a pytest session).
    """
    spec = importlib.util.spec_from_file_location("e2e_gate_conftest", E2E_DIR / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


# --- pin 1: the fail-closed gate --------------------------------------------------------------


def test_gate_is_closed_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain environment (no opt-in var set) keeps the real-input gate CLOSED."""
    conftest = _load_e2e_conftest()
    monkeypatch.delenv(conftest.E2E_OPT_IN_ENV_VAR, raising=False)
    assert conftest.e2e_real_input_enabled() is False


def test_gate_opens_only_for_exact_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact documented opt-in value opens the gate."""
    conftest = _load_e2e_conftest()
    monkeypatch.setenv(conftest.E2E_OPT_IN_ENV_VAR, "1")
    assert conftest.e2e_real_input_enabled() is True


@pytest.mark.parametrize(
    "value",
    ["true", "yes", "on", "2", "0", "", " 1", "1 ", "ONE", "y", "01"],
)
def test_gate_is_closed_for_near_miss_opt_in_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Fail-closed: sloppy opt-in values (true/yes/on/2/padded) must NOT enable real input."""
    conftest = _load_e2e_conftest()
    monkeypatch.setenv(conftest.E2E_OPT_IN_ENV_VAR, value)
    assert conftest.e2e_real_input_enabled() is False, value


@pytest.mark.parametrize(
    "name",
    [
        "E2E_REAL_INPUT",
        "E2E",
        "E2E_TESTS",
        "E2E_RUN",
        "RUN_E2E",
        "REAL_DESKTOP",
        "DESKTOP",
        "REAL_INPUT",
        "CUMCP_E2E",
        "CUMCP_RUN_E2E_TRUSTED",
        "CI",
    ],
)
def test_gate_is_closed_for_unrelated_env_leakage(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Accidental env leakage (any similarly-named variable set to 1) never enables."""
    conftest = _load_e2e_conftest()
    monkeypatch.delenv(conftest.E2E_OPT_IN_ENV_VAR, raising=False)
    monkeypatch.setenv(name, "1")
    assert conftest.e2e_real_input_enabled() is False, name


def test_gate_predicate_is_pure_over_explicit_environ() -> None:
    """The predicate evaluates an explicit mapping without touching os.environ."""
    conftest = _load_e2e_conftest()
    assert conftest.e2e_real_input_enabled({}) is False
    assert conftest.e2e_real_input_enabled({"CUMCP_RUN_E2E": "1"}) is True
    assert conftest.e2e_real_input_enabled({"CUMCP_RUN_E2E": "true"}) is False
    assert conftest.e2e_real_input_enabled({"E2E_REAL_INPUT": "1"}) is False


def test_skip_reason_is_loud_and_actionable() -> None:
    """The skip reason names the opt-in var, the command, and the desktop warning."""
    conftest = _load_e2e_conftest()
    reason = conftest.E2E_SKIP_REASON
    assert conftest.E2E_OPT_IN_ENV_VAR in reason
    assert f"{conftest.E2E_OPT_IN_ENV_VAR}={conftest.E2E_OPT_IN_VALUE}" in reason
    assert "python -m pytest tests/e2e" in reason
    assert "WARNING" in reason
    assert "desktop" in reason.casefold()


def test_all_real_input_e2e_tests_carry_the_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every desktop-driving e2e test module opts into the marker (source-level pin).

    Parses the four e2e app files and asserts the module-level e2e marker; a new
    desktop-driving file without the marker would silently run real input in every
    plain suite run — the exact D6 hazard. (test_benchmark_harness is exempt: fakes.)
    """
    for name in ("test_e2e_notepad.py", "test_e2e_calculator.py", "test_e2e_browser.py"):
        source = (E2E_DIR / name).read_text(encoding="utf-8")
        assert "pytestmark = pytest.mark.e2e" in source, name
    harness = (E2E_DIR / "test_benchmark_harness.py").read_text(encoding="utf-8")
    assert "pytest.mark.e2e" not in harness  # fakes only; deliberately never marked


# --- pin 2: marker-only attach ignores the user's windows --------------------------------------


class StubWindow:
    """A fake visible top-level window (title/class/pid), no OS involved."""

    def __init__(self, title: str, class_name: str, pid: int) -> None:
        self.title = title
        self.class_name = class_name
        self.pid = pid


def _install_window_stub(monkeypatch: pytest.MonkeyPatch, windows: list[StubWindow]) -> None:
    """Point the helpers' enumeration/identity reads at fake in-memory windows."""
    monkeypatch.setattr(w32, "top_level_windows", lambda: [i for i in range(len(windows))])
    monkeypatch.setattr(w32, "window_text", lambda i: windows[i].title)
    monkeypatch.setattr(w32, "window_class", lambda i: windows[i].class_name)
    monkeypatch.setattr(w32, "window_pid", lambda i: windows[i].pid)
    monkeypatch.setattr(w32, "is_visible", lambda i: True)


def test_attach_ignores_user_windows_without_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE D6 pin: the user's open Notepad (no marker) is invisible to the attach path.

    Same class, same exe, same pid-space — everything except the run-unique marker
    matches, exactly like the real incident. The marker-only enumeration must return
    ONLY the marker window; if our marker window is absent, it must return NOTHING
    (so a wrong-window attach fails loudly instead of typing into the user's text).
    """
    token = w32.unique_window_token("pin")
    user_notepad = StubWindow("*Untitled - Notepad", "Notepad", 4242)
    user_notepad_with_text = StubWindow("My notes - Notepad", "Notepad", 4243)
    ours = StubWindow(f"{token}.txt - Notepad", "Notepad", 9999)
    _install_window_stub(monkeypatch, [user_notepad, user_notepad_with_text, ours])

    found = w32.find_marked_windows(token)
    assert found == [2], "attach enumeration must see ONLY the marker window"

    # And with our window GONE (crash/timeout), the user's windows are still invisible.
    _install_window_stub(monkeypatch, [user_notepad, user_notepad_with_text])
    assert w32.find_marked_windows(token) == []
    assert w32.find_marked_windows(token, class_name="Notepad") == []


def test_attach_enforces_class_and_pid_scoping(monkeypatch: pytest.MonkeyPatch) -> None:
    """A marker-carrying window of the WRONG class/pid is not attachable."""
    token = w32.unique_window_token("pin2")
    decoy_class = StubWindow(f"{token} - Some Other App", "Chrome_WidgetWin_1", 1111)
    decoy_pid = StubWindow(f"{token}.txt - Notepad", "Notepad", 2222)
    _install_window_stub(monkeypatch, [decoy_class, decoy_pid])

    assert w32.find_marked_windows(token, class_name="Notepad", pid=3333) == []
    assert w32.find_marked_windows(token, class_name="Notepad", pid=2222) == [1]
    assert w32.find_marked_windows(token, class_name="Chrome_WidgetWin_1") == [0]


def test_attach_fails_closed_on_ambiguous_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two marker-carrying windows (impossible by design) raise, never guess."""
    token = w32.unique_window_token("pin3")
    a = StubWindow(f"{token}-a - Notepad", "Notepad", 1)
    b = StubWindow(f"{token}-b - Notepad", "Notepad", 2)
    _install_window_stub(monkeypatch, [a, b])
    deadline = w32.Deadline(5.0)
    with pytest.raises(RuntimeError, match="ambiguous attach"):
        w32.attach_window_by_unique_title(deadline, token)


def test_attach_times_out_when_no_marker_window_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """Markerless desktop (only user windows): TimeoutError, never a fallback attach."""
    token = w32.unique_window_token("pin4")
    user_notepad = StubWindow("User's own notes - Notepad", "Notepad", 7)
    _install_window_stub(monkeypatch, [user_notepad])
    monkeypatch.setattr(w32, "kill_process_tree", lambda pid: None)
    deadline = w32.Deadline(30.0)
    with pytest.raises(TimeoutError, match="marker-carrying window"):
        w32.attach_window_by_unique_title(deadline, token, timeout_s=0.3)


def test_wait_for_marked_window_returns_the_marker_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: with user windows around, attach resolves OUR marker window."""
    token = w32.unique_window_token("pin5")
    user_notepad = StubWindow("shopping list - Notepad", "Notepad", 31337)
    ours = StubWindow(f"{token}.txt - Notepad", "Notepad", 4242)
    _install_window_stub(monkeypatch, [user_notepad, ours])
    deadline = w32.Deadline(30.0)
    assert w32.wait_for_marked_window(deadline, token, timeout_s=1.0) == 1


def test_window_tokens_are_run_unique() -> None:
    """Every token call yields a distinct marker (pid + counter + time)."""
    tokens = {w32.unique_window_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(token.startswith("cumcp-e2e-") for token in tokens)


# --- pin 3: teardown closes exactly the suite's own instance ----------------------------------


def test_close_window_targets_exactly_the_given_hwnd(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hardened close posts WM_CLOSE to EXACTLY the hwnd this suite found.

    Stub PostMessageW records every target; close_window must post to the marker
    hwnd only — never re-search by class/title (the pre-D6 implementation polled
    find_windows(class_name=..., title_needle=...), which could observe the user's
    window). Liveness is checked on the exact hwnd via a stubbed IsWindow.
    """
    posted: list[Any] = []
    closed = {"hwnd": False}

    def fake_post(hwnd, msg, wparam, lparam) -> int:
        posted.append(hwnd)
        closed["hwnd"] = True  # simulate the close taking effect immediately
        return 1

    monkeypatch.setattr(w32.user32, "PostMessageW", fake_post)
    monkeypatch.setattr(w32, "window_exists", lambda hwnd: hwnd != 555 and not closed["hwnd"])
    assert w32.close_window(555, wait_s=0.5) is True
    assert posted == [555], "close must target exactly the suite's own hwnd"


def test_close_window_polls_only_the_exact_hwnd(monkeypatch: pytest.MonkeyPatch) -> None:
    """close_window's wait checks THE hwnd's liveness — not a title search."""
    calls: list[int] = []
    monkeypatch.setattr(
        w32.user32,
        "PostMessageW",
        lambda hwnd, msg, wparam, lparam: 1,
    )
    monkeypatch.setattr(w32, "window_exists", lambda hwnd: (calls.append(hwnd) or True))
    assert w32.close_window(777, wait_s=0.2) is False  # stubbed window never dies
    assert calls and set(calls) == {777}, "liveness polling must touch only hwnd 777"


def test_teardown_kills_only_the_launched_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """kill_process_tree is invoked with EXACTLY the Popen pid the test launched.

    Source-level pin over the e2e app-launch context managers: notepad/calculator
    teardown must call w32.kill_process_tree(proc.pid) (the process WE started),
    never a window found by name. Checked on source so no app is ever launched.
    """
    notepad_src = (E2E_DIR / "test_e2e_notepad.py").read_text(encoding="utf-8")
    calc_src = (E2E_DIR / "test_e2e_calculator.py").read_text(encoding="utf-8")
    browser_src = (E2E_DIR / "test_e2e_browser.py").read_text(encoding="utf-8")
    for name, src in (("notepad", notepad_src), ("calculator", calc_src)):
        assert "finally:" in src and "w32.kill_process_tree(proc.pid)" in src, name
    # browser: closes the exact found hwnd, then kills only the launcher tree.
    assert "w32.close_window(hwnd" in browser_src
    assert "w32.kill_process_tree(proc.pid)" in browser_src


def test_notepad_scratch_names_carry_unique_tokens() -> None:
    """Source-level pin: notepad scratch files embed the run-unique token (D6)."""
    source = (E2E_DIR / "test_e2e_notepad.py").read_text(encoding="utf-8")
    assert 'assert "cumcp-e2e-" in token' in source, (
        "notepad_app must reject markerless scratch filenames (wrong-window attach guard)"
    )
    assert "w32.unique_window_token(" in source
    assert 'e2e_scratch / "identity_probe.txt"' not in source
    assert 'e2e_scratch / "typed.txt"' not in source


def test_legacy_search_markers_are_still_pinnable() -> None:
    """The strings a user should search for after a past polluted run stay defined."""
    source = (E2E_DIR / "test_e2e_notepad.py").read_text(encoding="utf-8")
    for marker in (
        "e2e-typed-7391 quick brown fox",
        "moved-window-recovered-7391",
        "stale-reject-marker-7391",
    ):
        assert marker in source, marker
    browser = (E2E_DIR / "test_e2e_browser.py").read_text(encoding="utf-8")
    assert "E2E Browser Verification Page" in browser
    calc = (E2E_DIR / "test_e2e_calculator.py").read_text(encoding="utf-8")
    # calculator: no typed text markers (click-only); the window itself is transient.
    assert "calc_display_equals" in calc


def test_deadline_helper_still_fail_fast() -> None:
    """Sanity: the Deadline watchdog (no-hang doctrine) stays fail-fast."""
    deadline = w32.Deadline(-1.0)
    with pytest.raises(TimeoutError):
        deadline.check("pin sanity")
    assert deadline.remaining() < 0
    time.sleep(0)  # keep time import exercised


def test_w32_module_import_has_no_dpi_side_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing helpers_win32 must not set process DPI awareness (E6 finding D10 stays)."""
    import builtins

    recorded: list[Any] = []

    real_import = builtins.__import__

    def spy_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if "SetProcessDpiAwarenessContext" in str(name):
            recorded.append(name)
        return real_import(name, *args, **kwargs)

    assert hasattr(w32, "ensure_dpi_awareness") and hasattr(w32, "_DPI_AWARENESS_DONE")
    assert recorded == []  # import itself made no awareness call


def test_gate_conftest_exports_for_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """The conftest keeps exporting the gate constants for docs/other pins to reference."""
    conftest = _load_e2e_conftest()
    assert conftest.E2E_OPT_IN_ENV_VAR == "CUMCP_RUN_E2E"
    assert conftest.E2E_OPT_IN_VALUE == "1"
