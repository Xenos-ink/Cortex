"""WP12 offline guard: the e2e harness can no longer poison the process for the product.

First 0.7.6 live-gate findings pinned here (all hermetic — no app launch, no input):

- (A) ``tests/e2e/helpers_win32.py`` used to bind the SHARED ``ctypes.windll.*``
  objects and set ``EnumWindows.argtypes`` / ``EnumChildWindows.argtypes`` on them.
  The product binds the same shared object (``desktop/win32.py`` ``_user32``) and
  builds a FRESH ``ctypes.WINFUNCTYPE`` per call (``desktop/windows.py``
  ``find_window_by_title``), so after any e2e helper ran in-process the product's
  own window search died with ``ArgumentError: argument 1: TypeError: expected
  WinFunctionType instance instead of WinFunctionType`` — the live failure of
  ``tests/test_t8_b5_backslash_type.py::test_real_dispatch_of_backslash_path_
  completes_under_a_second``. The helpers now bind PRIVATE ``ctypes.WinDLL``
  instances; this module pins that forever: (1) a source/AST scan over ``tests/``
  fails on ANY argtypes/restype/errcheck assignment to a ``ctypes.windll`` chain;
  (2) importing ``helpers_win32`` leaves the shared prototypes untouched;
  (3) the helper's user32/kernel32 are not the shared objects.
- (B) ``unique_window_token`` always embeds the ``cumcp-e2e-`` marker regardless
  of the descriptive stem (the live notepad tests asserted the marker on files
  named ``identity-probe-...`` with no marker and failed before launching).
- (C) ``launch_marked_notepad`` survives the Windows 11 packaged-Notepad handoff
  (launcher stub exits; the window belongs to a different pid): the wait is
  marker-only and identity is asserted against the REAL owning pid's executable.
  Pinned hermetically with stubbed OS seams.
- (D) executed replies are unwrapped to their dict payload via
  ``helpers_runtime.execute_payload`` (``computer_execute`` returns MCP content
  blocks in 0.7.6, never the bare pre-0.4.1 result dict the browser test read
  with ``response.get("ok")`` and died on ``AttributeError: 'list' object has no
  attribute 'get'``). Pinned by a source scan: EVERY ``computer_execute`` call
  site in ``tests/e2e`` is wrapped in ``execute_payload``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

TESTS_DIR = Path(__file__).resolve().parent
E2E_DIR = TESTS_DIR / "e2e"
if str(E2E_DIR) not in sys.path:
    sys.path.insert(0, str(E2E_DIR))

PROTOTYPE_ATTRS = ("argtypes", "restype", "errcheck")


# --- (A1) source/AST scan: no test code mutates the shared windll prototypes --------


def _chain_parts(node: ast.expr) -> list[str]:
    """Flatten a call-target attribute chain, method name FIRST.

    For ``a.b.c(...)`` the chain walked from the call outwards is c, b, a — so
    parts[0] is the method name and parts[-1] the root name.
    """
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return parts


def _iter_test_sources() -> list[tuple[Path, str]]:
    sources: list[tuple[Path, str]] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.name == Path(__file__).name:
            continue  # this module's docstring mentions the patterns
        sources.append((path, path.read_text(encoding="utf-8")))
    return sources


def test_no_test_file_assigns_prototypes_on_shared_windll_objects() -> None:
    """AST pin (finding A): argtypes/restype/errcheck may never be ASSIGNED to any
    expression chain rooted at ``ctypes.windll`` — the shared DLL object is
    process-wide state that every consumer (including the product) binds."""
    violations: list[str] = []
    for path, source in _iter_test_sources():
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Attribute):
                    continue
                if target.attr not in PROTOTYPE_ATTRS:
                    continue
                parts = _chain_parts(target.value)
                if "windll" in parts:
                    dotted = ".".join(reversed(parts))
                    violations.append(f"{path.name}:{node.lineno}: {dotted}.{target.attr}")
    assert violations == [], (
        "test code assigns prototypes on the SHARED ctypes.windll objects (WP12 "
        "finding A — this poisons the product's per-call WINFUNCTYPE callbacks): "
        f"{violations}"
    )


def test_helpers_leave_shared_windll_prototypes_untouched() -> None:
    """Runtime pin: importing helpers_win32 (its module-level prototype setup runs
    here) must leave the SHARED ``ctypes.windll.user32`` EnumWindows and
    EnumChildWindows prototypes exactly as ctypes delivered them (no argtypes)."""
    import ctypes

    import helpers_win32  # noqa: F401

    assert ctypes.windll.user32.EnumWindows.argtypes is None
    assert ctypes.windll.user32.EnumChildWindows.argtypes is None


def test_helper_dll_bindings_are_private_instances() -> None:
    """Binding pin: the helpers' user32/kernel32 are PRIVATE WinDLL instances, not
    the shared windll objects — prototype writes on them cannot escape the suite."""
    import ctypes

    import helpers_win32 as w32

    assert isinstance(w32.user32, ctypes.WinDLL)
    assert isinstance(w32.kernel32, ctypes.WinDLL)
    assert w32.user32 is not ctypes.windll.user32
    assert w32.kernel32 is not ctypes.windll.kernel32


# --- (B) the token marker is unconditional -------------------------------------------


def test_unique_window_token_always_embeds_the_marker() -> None:
    """WP12 finding B: callers pass descriptive stems; the token must ALWAYS carry
    the ``cumcp-e2e-`` marker (the live notepad assertion rejected markerless
    stems before any launch), and a marker on the stem itself is not doubled."""
    import helpers_win32 as w32

    probe = w32.unique_window_token("identity-probe")
    assert probe.startswith("cumcp-e2e-"), probe
    assert "-identity-probe-" in probe, probe
    doubled = w32.unique_window_token("cumcp-e2e-right-click")
    assert doubled.startswith("cumcp-e2e-"), doubled
    assert "cumcp-e2e-cumcp-e2e-" not in doubled, doubled
    tokens = {w32.unique_window_token(f"pin{i}") for i in range(50)}
    assert len(tokens) == 50
    assert all(token.startswith("cumcp-e2e-") for token in tokens)


# --- (C) launch_marked_notepad survives the packaged-Notepad pid handoff --------------


class _FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid


@pytest.fixture
def launcher(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Stub every OS seam of ``launch_marked_notepad``; record what it kills.

    Scenario: the launcher stub gets pid 40080 and exits; the marker window
    (hwnd 4242) is owned by the packaged app's REAL pid 99 running notepad.exe.
    """
    import helpers_win32 as w32

    state = SimpleNamespace(kills=[], closed=[], hwnd=4242, real_pid=99, exe=None)

    monkeypatch.setattr(w32, "wait_for_marked_window", lambda *args, **kwargs: state.hwnd)
    monkeypatch.setattr(w32, "window_pid", lambda hwnd: state.real_pid)
    monkeypatch.setattr(w32, "close_window", lambda hwnd, wait_s=5.0: state.closed.append(hwnd) or True)
    monkeypatch.setattr(
        w32,
        "process_exe",
        lambda pid: state.exe
        if state.exe is not None
        else ("C:\\Windows\\System32\\notepad.exe" if pid == state.real_pid else None),
    )
    monkeypatch.setattr(w32, "kill_process_tree", lambda pid: state.kills.append(pid))
    monkeypatch.setattr(w32.subprocess, "Popen", lambda argv: _FakeProc(pid=40080))
    return state


def test_launch_marked_notepad_survives_packaged_handoff(launcher: Any) -> None:
    """Win11 packaged Notepad: the stub pid (40080) differs from the real window
    owner (99); the launch still succeeds via the marker-only wait plus the
    real-owner identity check, and nothing is killed on the happy path."""
    import helpers_win32 as w32

    proc, hwnd = w32.launch_marked_notepad(
        w32.Deadline(5.0),
        Path("scratch") / "cumcp-e2e-right-click-probe.txt",
        timeout_s=1.0,
    )
    assert proc.pid == 40080
    assert hwnd == 4242
    assert launcher.kills == [], launcher.kills


def test_launch_marked_notepad_refuses_a_non_notepad_marker_window(launcher: Any) -> None:
    """If the marker window is owned by something that is NOT Notepad, the launch
    fails loudly, kills ONLY the stub pid WE spawned and closes only our marker
    hwnd — NEVER the real owning pid (WP12 live-run-2: with the packaged app that
    pid may be the USER'S shared Notepad process)."""
    import helpers_win32 as w32

    launcher.exe = "C:\\Windows\\System32\\cmd.exe"
    with pytest.raises(RuntimeError, match="not Notepad"):
        w32.launch_marked_notepad(
            w32.Deadline(5.0), Path("scratch") / "cumcp-e2e-probe.txt", timeout_s=1.0
        )
    assert launcher.kills == [40080], launcher.kills
    assert launcher.closed == [], launcher.closed  # WP12b: never closed, left recorded


# --- (D) every e2e computer_execute call site unwraps the payload --------------------


def test_every_e2e_execute_site_unwraps_the_reply_payload() -> None:
    """Source pin (finding D): ``computer_execute`` returns MCP content blocks in
    0.7.6 (the browser test died reading ``response.get("ok")`` off a list), so
    every call site in tests/e2e must assign through ``rt.execute_payload``."""
    unwrapped: list[str] = []
    for path in sorted(E2E_DIR.glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            parts = _chain_parts(node.func)
            if not parts or parts[0] != "computer_execute":
                continue
            start = max(0, node.lineno - 13)
            end = min(len(lines), (node.end_lineno or node.lineno) + 2)
            if "execute_payload" not in "\n".join(lines[start:end]):
                unwrapped.append(f"{path.name}:{node.lineno}")
    assert unwrapped == [], (
        "computer_execute call sites that read the reply WITHOUT unwrapping the "
        f"MCP content-block payload via rt.execute_payload: {unwrapped}"
    )


# --- (WP12 live-run 2/3) D6-safe, TABS-AWARE teardown --------------------------------


class _FakeReleaseUser32:
    """Records the user32 calls release_notepad makes (SendMessage/PostMessage/keys)."""

    def __init__(self, state: SimpleNamespace) -> None:
        self._state = state

    def SendMessageW(self, hwnd: int, msg: int, wparam: Any, lparam: Any) -> int:
        self._state.sent.append(("send", msg))
        return 1

    def PostMessageW(self, hwnd: int, msg: int, wparam: Any, lparam: Any) -> int:
        self._state.sent.append(("post", msg))
        return 1

    def keybd_event(self, vk: int, scan: int, flags: int, extra: int) -> None:
        self._state.sent.append(("key", vk, flags))

    def SendInput(self, count: int, array: Any, cb_size: int) -> int:
        # WP12c guard (VAL MAJOR): the size argument must be the PRODUCT'S full
        # 40-byte record — a keyboard-only 32-byte struct is rejected by the real
        # SendInput (the live-run-6 / Ctrl+W failure class).
        import ctypes

        import computer_use_mcp.desktop.input_engine as _ie

        assert cb_size == ctypes.sizeof(_ie._INPUT), (
            f"SendInput cbSize {cb_size} != sizeof(_INPUT) {ctypes.sizeof(_ie._INPUT)}"
        )
        self._state.sent.append(("sendinput", count))
        return count

    def GetForegroundWindow(self) -> int:
        return self._state.fg_hwnd


@pytest.fixture
def releaser(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Stub every OS seam of the tabs-aware ``release_notepad``; record everything."""
    import helpers_win32 as w32

    state = SimpleNamespace(
        kills=[], closed=[], sent=[], window_pid=0, texts={},
        alive=True, focus_ok=True, fg_hwnd=4242,
    )
    monkeypatch.setattr(w32, "child_controls", lambda hwnd: [])
    monkeypatch.setattr(w32, "close_window", lambda hwnd, wait_s=5.0: state.closed.append(hwnd) or True)
    monkeypatch.setattr(w32, "kill_process_tree", lambda pid: state.kills.append(pid))
    monkeypatch.setattr(w32, "window_pid", lambda hwnd: state.window_pid)
    monkeypatch.setattr(w32, "window_exists", lambda hwnd: state.alive)
    monkeypatch.setattr(w32, "focus_window", lambda hwnd, attempts=5: state.focus_ok)
    monkeypatch.setattr(w32, "window_text", lambda hwnd: state.texts.get(hwnd, ""))
    monkeypatch.setattr(w32, "user32", _FakeReleaseUser32(state))
    return state


MARKER = "cumcp-e2e-typed-123-001-999"


def test_release_shared_host_with_user_window_kills_only_the_stub(releaser: Any) -> None:
    """THE D6 pin (live-run 2): the shared host owns a NON-marker window (the
    user's own) — the host is never killed, the top-level window is never
    WM_CLOSEd; only our marker TAB is closed (Ctrl+W) and the spawned stub killed."""
    import helpers_win32 as w32

    releaser.window_pid = 200
    releaser.texts = {4242: f"{MARKER}.txt - Notepad"}
    w32.release_notepad(_FakeProc(pid=100), 4242, MARKER)
    assert releaser.kills == [100], releaser.kills
    assert releaser.closed == [], releaser.closed  # no WM_CLOSE on a shared window
    # WP12b: the tab close is ONE atomic SendInput batch (never 4 keybd_events).
    assert ("sendinput", 4) in releaser.sent, releaser.sent
    assert all(entry[0] != "key" for entry in releaser.sent), releaser.sent
    assert all(entry != ("post", 0x10) for entry in releaser.sent), releaser.sent


def test_release_shared_host_with_only_visible_marker_windows_is_never_killed(releaser: Any) -> None:
    """Live-run 3 tightening: restored USER TABS are invisible to Win32, so a host
    that VISIBLY owns only marker windows may still hide user tabs — 'only ours'
    is not provable, so a shared host is NEVER killed (only the stub is)."""
    import helpers_win32 as w32

    releaser.window_pid = 200
    releaser.texts = {4242: f"{MARKER}.txt - Notepad"}
    w32.release_notepad(_FakeProc(pid=100), 4242, MARKER)
    assert releaser.kills == [100], releaser.kills
    assert ("sendinput", 4) in releaser.sent  # our tab close still attempted


def test_release_sends_nothing_when_the_marker_tab_is_not_active(releaser: Any) -> None:
    """Ctrl+W closes the ACTIVE tab: if the window title no longer carries our
    marker (another tab is active), teardown must send NO keystroke at all."""
    import helpers_win32 as w32

    releaser.window_pid = 200
    releaser.alive = True
    releaser.texts = {4242: "User's restored unsaved tab - Notepad"}
    w32.release_notepad(_FakeProc(pid=100), 4242, MARKER)
    assert releaser.kills == [100], releaser.kills
    assert all(entry[0] != "key" for entry in releaser.sent), releaser.sent


def test_release_sends_nothing_when_focus_fails(releaser: Any) -> None:
    """The guard chain requires a won focus before the keystroke — a stolen focus
    must abort the tab close (never type into the user's window)."""
    import helpers_win32 as w32

    releaser.window_pid = 200
    releaser.focus_ok = False
    releaser.texts = {4242: f"{MARKER}.txt - Notepad"}
    w32.release_notepad(_FakeProc(pid=100), 4242, MARKER)
    assert releaser.kills == [100], releaser.kills
    assert all(entry[0] != "key" for entry in releaser.sent), releaser.sent


def test_release_sends_nothing_when_the_window_is_already_gone(releaser: Any) -> None:
    """Shared topology with the window already closed: no input, stub kill only."""
    import helpers_win32 as w32

    releaser.window_pid = 200
    releaser.alive = False
    w32.release_notepad(_FakeProc(pid=100), 4242, MARKER)
    assert releaser.kills == [100], releaser.kills
    assert all(entry[0] != "key" for entry in releaser.sent), releaser.sent


def test_release_classic_spawned_owner_closes_window_and_kills_once(releaser: Any) -> None:
    """Classic Notepad: the window's real owner IS the pid we spawned — one
    process, one document, no tabs: exact-hwnd WM_CLOSE (close_window) and the
    spawned pid killed exactly once."""
    import helpers_win32 as w32

    releaser.window_pid = 100  # == proc.pid
    releaser.texts = {4242: "cumcp-e2e-probe.txt - Notepad"}
    w32.release_notepad(_FakeProc(pid=100), 4242, "cumcp-e2e-probe")
    assert releaser.kills == [100], releaser.kills
    assert releaser.closed == [4242], releaser.closed
    assert all(entry[0] != "key" for entry in releaser.sent), releaser.sent


# --- (WP12 live-run 3) text reading: packaged-Notepad RichEditD2DPT -------------------


class _FakeTextUser32:
    """Serves WM_GETTEXTLENGTH/WM_GETTEXT from a texts map (byref-safe)."""

    def __init__(self, texts: dict[int, str]) -> None:
        self.texts = texts

    def SendMessageW(self, hwnd: int, msg: int, wparam: Any, lparam: Any) -> int:
        if msg == 0x000E:  # WM_GETTEXTLENGTH
            return len(self.texts.get(hwnd, ""))
        if msg == 0x000D:  # WM_GETTEXT
            lparam._obj.value = self.texts.get(hwnd, "")
            return 1
        return 0


def test_read_edit_text_reads_the_classic_edit_control(monkeypatch: Any) -> None:
    """Classic Notepad shape: an 'Edit' child is read via WM_GETTEXT."""
    import helpers_win32 as w32

    monkeypatch.setattr(w32, "user32", _FakeTextUser32({10: "typed text"}))
    monkeypatch.setattr(
        w32, "child_controls", lambda hwnd: [{"cls": "Edit", "hwnd": 10}]
    )
    assert w32.read_edit_text(1) == "typed text"


def test_read_edit_text_reads_the_packaged_richedit_control(monkeypatch: Any) -> None:
    """Live-run 3 shape: Windows 11 packaged Notepad renders into RichEditD2DPT —
    the reader must find and read it (this exact gap made the live semantic check
    read window_text='' and fail the type test)."""
    import helpers_win32 as w32

    monkeypatch.setattr(w32, "user32", _FakeTextUser32({20: "e2e-typed-7391 quick brown fox"}))
    monkeypatch.setattr(
        w32, "child_controls", lambda hwnd: [{"cls": "RichEditD2DPT", "hwnd": 20}]
    )
    assert w32.read_edit_text(1) == "e2e-typed-7391 quick brown fox"


def test_read_edit_text_ignores_non_text_controls_and_unreadable_ones(monkeypatch: Any) -> None:
    """Non-text controls (Button/Static) are skipped; a text control with no
    readable length falls out cleanly; RICHEDIT50W is also recognized."""
    import helpers_win32 as w32

    monkeypatch.setattr(
        w32,
        "user32",
        _FakeTextUser32({30: "richedit50w text", 40: "", 50: "button"}),
    )
    monkeypatch.setattr(
        w32,
        "child_controls",
        lambda hwnd: [
            {"cls": "Button", "hwnd": 50},
            {"cls": "RICHEDIT50W", "hwnd": 30},
            {"cls": "RichEditD2DPT", "hwnd": 40},
        ],
    )
    assert w32.read_edit_text(1) == "richedit50w text"


# --- (WP12 live-run 6) raw diagnostic legs ride the PRODUCT'S input engine ------------


def test_type_diagnostic_raw_legs_use_the_product_input_engine() -> None:
    """Source pin (live-run 6): the diagnostic's RAW legs must NOT define their own
    INPUT structs — the run-6 test-side struct had a wrong size and SendInput
    rejected every event (0/60). They must drive desktop/input_engine.py's own
    event builders + _send/_dispatch (the structs the product itself sends).
    WP12b B2: every raw leg also passes the per-chunk FOCUS GUARD
    (``before_chunk``) so a mid-burst focus change aborts the keystrokes."""
    source = (E2E_DIR / "test_e2e_type_diagnostic.py").read_text(encoding="utf-8")
    assert "SendInputEngine" in source, "raw legs must use the product's engine"
    assert "from computer_use_mcp.desktop.input_engine import SendInputEngine" in source
    for banned in ("class _KI(", "class _INP(", "_KEYBDINPUT", "_fields_ = ["):
        assert banned not in source, f"custom INPUT structs are banned: {banned}"
    assert "engine.type_text(TEXT, before_chunk=_focus_guard)" in source, (
        "B2: every raw leg must ride the per-chunk focus guard"
    )


def test_pace_calibration_keeps_the_d6_discipline() -> None:
    """WP12b NIT: the pace-calibration file uses the same D6 launch/teardown
    helpers, never spawns or kills directly, and keeps its gate explicit."""
    source = (E2E_DIR / "test_e2e_pace_calibration.py").read_text(encoding="utf-8")
    assert "w32.launch_marked_notepad(" in source
    assert "w32.release_notepad(" in source
    assert "kill_process_tree" not in source
    assert "subprocess" not in source
    assert "CUMCP_RUN_E2E" in source


def test_live_clipboard_tests_are_text_only_skip_and_restore() -> None:
    """WP12e P1 (VAL MAJOR): EVERY LIVE clipboard test must (1) run the text-only
    precondition helper (read-only format enumeration — any rich format would be
    DESTROYED by a write+restore cycle) and (2) restore the user's text in a
    ``finally`` with a read-back verification. Source/AST pin over the stacked
    ``@LIVE_CLIPBOARD`` test functions."""
    import ast

    source = (TESTS_DIR / "test_v071_type_via_clipboard.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="test_v071_type_via_clipboard.py")
    live_tests = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        live = any(
            (isinstance(dec, ast.Name) and dec.id == "LIVE_CLIPBOARD")
            or (isinstance(dec, ast.Attribute) and dec.attr == "LIVE_CLIPBOARD")
            for dec in node.decorator_list
        )
        if live:
            live_tests.append(node)
    assert len(live_tests) == 5, (
        f"expected 5 LIVE clipboard tests, found {[n.name for n in live_tests]}"
    )
    problems: list[str] = []
    for node in live_tests:
        segment = ast.get_source_segment(source, node) or ""
        if "_require_text_only_clipboard()" not in segment:
            problems.append(f"{node.name}: missing the text-only precondition")
        # the refused-write test never writes, so it needs no finally restore;
        # every test that WRITES must restore the user's text in a finally.
        writes = node.name != "test_real_win32_refused_write_leaves_user_clipboard_intact"
        if writes and "finally:" not in segment:
            problems.append(f"{node.name}: missing the finally restore")
    assert not problems, f"LIVE clipboard tests missing the P1 precondition/restore: {problems}"


def test_ctrl_w_helper_uses_the_product_sendinput_record() -> None:
    """WP12c (VAL MAJOR): the Ctrl+W helper must build the chord with the
    PRODUCT'S OWN record type (``input_engine._INPUT`` / ``_key_event``) — the
    WP12b private struct was 32 bytes on x64 and the real SendInput rejected the
    whole batch. No private SendInput struct may exist anywhere in tests/e2e, and
    the size passed to SendInput must be ``sizeof(_INPUT)`` (asserted at runtime
    by the releaser fake's SendInput stub)."""
    source = (E2E_DIR / "helpers_win32.py").read_text(encoding="utf-8")
    assert "from computer_use_mcp.desktop.input_engine import _INPUT, _key_event" in source
    assert "ctypes.sizeof(_INPUT)" in source
    for banned in ("_NotepadSendInput", "_NotepadKeybdInput", "_NotepadInputUnion"):
        assert banned not in source, f"private SendInput struct is banned: {banned}"


def test_t8_b5_live_test_is_marker_scoped() -> None:
    """WP12d (VAL R6 BLOCKER-class): the LIVE t8_b5 dispatch test must be
    D6-scoped — it launches via the shared marker helpers (``launch_marked_
    notepad`` + ``release_notepad``) and may NEVER attach to an unmarked Notepad
    title ('Untitled - Notepad') or bare-kill a Notepad pid: the user's own
    untitled Notepad used to be a valid attach target for a 60-character
    payload."""
    source = (TESTS_DIR / "test_t8_b5_backslash_type.py").read_text(encoding="utf-8")
    assert "w32.launch_marked_notepad(" in source, "must launch via the D6 helper"
    assert "w32.release_notepad(" in source, "must tear down via the D6-safe helper"
    assert "Untitled - Notepad" not in source, "no unmarked-Notepad attach"
    assert "find_window_by_title" not in source, "no bare title search for Notepad"
    assert "taskkill" not in source, "no bare kills — teardown goes through the helper"
    assert "type_interval" in source, "the stall test runs its engine UNPACED (WP12d)"


def test_sendinput_engine_sendinput_cbsize_and_pacing(monkeypatch: Any) -> None:
    """The raw legs' engine semantics, pinned offline with a stubbed OS seam: every
    SendInput call receives cbSize == ctypes.sizeof(_INPUT) (the run-6 failure was
    a wrong struct size → 0 events accepted); interval 0 dispatches the whole text
    as ONE batch; a paced engine dispatches one char (down+up) per chunk
    (input_engine.py:663-707, chunk_size-2 path at :668)."""
    import ctypes

    from computer_use_mcp.desktop import input_engine as ie

    calls: list[tuple[int, int, bytes]] = []

    class _FakeUser32:
        def SendInput(self, count: int, array: Any, cb_size: int) -> int:
            calls.append((count, cb_size, bytes(array)))
            return count

        def GetCursorPos(self, lp_point: Any) -> int:
            point = lp_point._obj
            point.x, point.y = 960, 540  # center: NOT a failsafe corner
            return 1

        def GetSystemMetrics(self, index: int) -> int:
            return (1920, 1080)[index]

    monkeypatch.setattr(ie, "_user32", _FakeUser32())

    engine = ie.SendInputEngine(failsafe=False)  # offline: no real cursor (the
    # failsafe's GetCursorPos reads the REAL mouse outside the stubbed seam)
    engine.type_text("abc")
    assert len(calls) == 1, "interval 0 must dispatch ONE batch"
    count, cb_size, raw = calls[0]
    assert cb_size == ctypes.sizeof(ie._INPUT), (
        f"SendInput cbSize must be sizeof(_INPUT)={ctypes.sizeof(ie._INPUT)}, got {cb_size}"
    )
    assert count == 6 and len(raw) == 6 * cb_size  # 3 chars -> down+up each

    calls.clear()
    paced = ie.SendInputEngine(type_interval=0.02, failsafe=False)
    paced.type_text("abc")
    assert [entry[0] for entry in calls] == [2, 2, 2], "paced: one char per chunk"
    assert all(entry[1] == ctypes.sizeof(ie._INPUT) for entry in calls)


# --- (WP12 live-run 4) diagnostic discipline pin --------------------------------------


def test_type_diagnostic_keeps_the_d6_launch_and_teardown_discipline() -> None:
    """Source pin (live-run 4): the LIVE_DESKTOP-gated corruption diagnostic must use
    the same D6 launch/teardown helpers as the rest of the suite — no direct
    subprocess launches, no kills, and every real keystroke rides the gated legs
    (its own SendInput helper is the diagnostic's whole point and is gated by
    LIVE_DESKTOP + the marker-active pre-checks)."""
    source = (E2E_DIR / "test_e2e_type_diagnostic.py").read_text(encoding="utf-8")
    assert "w32.launch_marked_notepad(" in source, "must launch through the D6 helper"
    assert "w32.release_notepad(" in source, "must tear down through the D6-safe helper"
    assert "kill_process_tree" not in source, "no kills outside the D6-safe helper"
    assert "subprocess" not in source, "no direct app launches in test files"
    assert "CUMCP_RUN_E2E" in source, "the LIVE_DESKTOP gate must stay explicit"


def test_branch_vocabulary_in_the_docs_matches_the_code() -> None:
    """WP12c MINOR: the `branch=` list is DERIVED from the code and checked in
    BOTH directions across all four homes — every token the code can emit
    (backend `_unverified(...)` + the fake's `branch=` literals) must appear in
    glossary.md, docs/CONFIGURATION.md and VERSIONS.md; every token the glossary
    lists must be emitted by the code (no invented tokens)."""
    import re

    root = TESTS_DIR.parent
    backend_source = (
        root / "src" / "computer_use_mcp" / "desktop" / "backend.py"
    ).read_text(encoding="utf-8")
    fake_source = (
        root / "src" / "computer_use_mcp" / "desktop" / "fake_backend.py"
    ).read_text(encoding="utf-8")
    code_tokens = set(re.findall(r'_unverified\("([a-z_]+)"\)', backend_source))
    # The fake's literals are " branch=…"-style (leading space inside the string),
    # so the token regex must NOT require a leading quote (WP12d NIT — the old
    # `"branch=…"` pattern never matched and that half of the scan checked nothing).
    code_tokens |= set(re.findall(r'branch=([a-z_]+)"', fake_source))
    assert len(code_tokens) >= 13, f"scanner found too few branch tokens: {sorted(code_tokens)}"

    glossary = (
        root / "skills" / "cortex-skill" / "references" / "glossary.md"
    ).read_text(encoding="utf-8")
    configuration = (root / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
    versions = (root / "VERSIONS.md").read_text(encoding="utf-8")
    contract = (root / "docs" / "REPLY-CONTRACT.md").read_text(encoding="utf-8")
    assert "branch=" in contract, "REPLY-CONTRACT.md must name the branch= suffix"

    for home_name, home_text in (
        ("glossary.md", glossary),
        ("CONFIGURATION.md", configuration),
        ("VERSIONS.md", versions),
    ):
        missing = [token for token in sorted(code_tokens) if f"`{token}`" not in home_text]
        assert not missing, f"{home_name} is missing branch tokens: {missing}"

    # direction 2: every token the glossary lists in its unverified bullet must be
    # emitted by the code (no invented/dropped tokens on the doc side).
    bullet = re.search(r"integrity=unverified.*?NOT a veto", glossary, re.S)
    assert bullet, "glossary unverified bullet not found"
    listed = set(re.findall(r"`([a-z_]+)`", bullet.group(0)))
    invented = sorted(listed - code_tokens - {"expected_effect"})
    assert not invented, f"glossary lists branch tokens the code never emits: {invented}"

    # WP12d NIT: the unpaced deadline fallback token is derived from the code and
    # documented in both docs homes.
    assert '" paced=partial"' in backend_source, (
        "backend.py must carry the ' paced=partial' message token"
    )
    for home_name, home_text in (
        ("CONFIGURATION.md", configuration),
        ("VERSIONS.md", versions),
    ):
        assert "paced=partial" in home_text, f"{home_name} must document paced=partial"


# --- (WP12 live-run 2) source guards: launcher-pid confusion is structurally dead ----


def _is_launcher_pid(node: ast.expr) -> bool:
    """``proc.pid`` / ``_proc_a.pid`` — the pid of a process the TEST spawned."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "pid"
        and isinstance(node.value, ast.Name)
        and node.value.id.startswith("proc")
    )


def _is_window_derived_pid(node: ast.expr) -> bool:
    """A pid read from a WINDOW: ``window_pid(...)`` or ``info["pid"]``."""
    if isinstance(node, ast.Call):
        parts = _chain_parts(node.func)
        return bool(parts) and parts[0] == "window_pid"
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value == "pid"
    return False


#: test_e2e_calculator.py is EXEMPT by documented construction: classic win32calc is
#: spawned DIRECTLY (no launcher stub — helpers_win32.launch_gui docstring), so the
#: tracked pid owns the window BY CONSTRUCTION and the file's pid-scoped attach
#: doctrine (Calculator cannot carry a title marker) DEPENDS on that equality.
_PID_GUARD_EXEMPT = {"test_e2e_calculator.py"}


def test_no_e2e_assertion_compares_a_launcher_pid_to_a_window_pid() -> None:
    """WP12 live-run-2 invariant: the launcher stub's pid is never a window pid
    (the packaged app hands off), so an assertion equating them is structurally
    wrong. Flag any Compare pairing ``proc.pid`` with ``window_pid(...)`` or an
    ``info["pid"]``-style window-derived pid."""
    violations: list[str] = []
    for path in sorted(E2E_DIR.glob("test_*.py")):
        if path.name in _PID_GUARD_EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or len(node.ops) != 1:
                continue
            left, right = node.left, node.comparators[0]
            if (
                _is_launcher_pid(left)
                and _is_window_derived_pid(right)
            ) or (
                _is_launcher_pid(right)
                and _is_window_derived_pid(left)
            ):
                violations.append(f"{path.name}:{node.lineno}")
    assert violations == [], (
        "e2e assertions compare a launcher-stub pid to a window-derived pid "
        f"(WP12 live-run-2: these are different pids for the packaged apps): "
        f"{violations}"
    )


def test_e2e_never_kills_a_window_derived_pid_outside_the_d6_safe_helper() -> None:
    """WP12 live-run-2 invariant: e2e TEST files may kill ONLY pids they spawned
    themselves (``proc.pid`` / ``_proc_a.pid``). Any other kill argument — a
    window-derived pid in particular — must go through the D6-safe
    ``w32.release_notepad`` ownership check (unit-pinned above)."""
    violations: list[str] = []
    for path in sorted(E2E_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            parts = _chain_parts(node.func)
            if not parts or parts[0] != "kill_process_tree":
                continue
            if not node.args or not _is_launcher_pid(node.args[0]):
                violations.append(f"{path.name}:{node.lineno}")
    assert violations == [], (
        "kill_process_tree called on a non-spawned (window-derived?) pid outside "
        f"the D6-safe release helper: {violations}"
    )
