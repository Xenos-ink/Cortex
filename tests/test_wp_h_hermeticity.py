"""WP-H permanent hermeticity guards for the offline suite (DELTA 1).

OFFLINE-ONLY PROOFS (B1): this ENTIRE module is skipped when ``CUMCP_RUN_E2E ==
"1"`` — these tests exist to prove the tripwire is armed, so under the live gate
the tripwire is legitimately disarmed and running them would call real effect
functions. In the offline configuration every proof FIRST verifies the tripwire
object is installed (``isinstance`` / marker attributes) and FAILS WITHOUT
CALLING ANYTHING if it is not; every intentional trigger is wrapped in
``desktop_tripwire.expect_trips``; the Popen probe target does not exist (even
without a tripwire it could only raise ``FileNotFoundError``). Therefore nothing
in this file can perform a real desktop effect in ANY configuration.

Guards:

1. TRIPWIRE ARMED — every ``backend._user32`` effect member, the GUI-app Popen
   guard, ``os.startfile`` and (when loaded) pyautogui effect functions raise
   ``RealDesktopEffectInOfflineSuite``; prototype binding (``argtypes``=) on a
   trap member trips instead of raising AttributeError (m1).
2. LAUNCH VALIDATOR — ``check_launch`` rejects shell one-liners, foreign python
   children, ``python -c``, ``executable=`` overrides and console tools
   (git/uv/taskkill were dropped from the allowlist — nothing ungated spawns
   them) (m2).
3. SWALLOW-PROOF — a trip whose exception is swallowed still fails the test via
   the teardown trip-log assertion, proven end-to-end with pytester (M1).
4. AST LAUNCH GUARD — no ungated test code may contain a process-spawn call
   (M2); with in-memory self-tests for every evasion form.
5. NAME PINS (m3) — the effect-name sets are pinned as literals here.
6. DPI PIN (M3) — the fixture never imports pyautogui.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

import computer_use_mcp.backend as backend_module
import desktop_tripwire
from desktop_tripwire import (
    RealDesktopEffectInOfflineSuite,
    _EffectTrap,
    _PyautoguiTripwireFinder,
    _TripwirePopen,
    _TripwireUser32,
    check_launch,
    expect_trips,
)

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("CUMCP_RUN_E2E") == "1",
        reason="offline-only tripwire arming proofs (the live gate legitimately disarms the tripwire)",
    )
]

# --- m3: the effect-name sets are pinned HERE, as literals ----------------------------

_PINNED_USER32_EFFECTS = frozenset(
    {
        "SendInput",
        "keybd_event",
        "mouse_event",
        "SetCursorPos",
        "SetForegroundWindow",
        "ShowWindow",
        "AttachThreadInput",
        "BringWindowToTop",
        "OpenClipboard",
        "EmptyClipboard",
        "SetClipboardData",
    }
)
_PINNED_PYAUTOGUI_EFFECTS = frozenset(
    {
        "moveTo",
        "click",
        "mouseDown",
        "mouseUp",
        "keyDown",
        "keyUp",
        "press",
        "write",
        "typewrite",
        "hotkey",
        "dragTo",
        "scroll",
    }
)


def test_effect_name_sets_are_pinned() -> None:
    assert desktop_tripwire.EFFECT_USER32_NAMES == _PINNED_USER32_EFFECTS
    assert desktop_tripwire.EFFECT_PYAUTOGUI_NAMES == _PINNED_PYAUTOGUI_EFFECTS
    assert len(_PINNED_USER32_EFFECTS) == 11
    assert len(_PINNED_PYAUTOGUI_EFFECTS) == 12


# --- the tripwire is armed (every proof pre-checks BEFORE calling anything) -----------

_PROBE_EXE = "cortex-hermeticity-gui-probe.exe"  # not on PATH, not allowlisted


def _require_user32_tripwire() -> _TripwireUser32:
    user32 = backend_module._user32
    assert isinstance(user32, _TripwireUser32), (
        "tripwire NOT armed (backend._user32 is not the read-only passthrough); "
        "refusing to call anything"
    )
    return user32


def test_tripwire_blocks_every_user32_effect_entry_point() -> None:
    user32 = _require_user32_tripwire()
    for name in sorted(_PINNED_USER32_EFFECTS):
        trap = getattr(user32, name)
        assert isinstance(trap, _EffectTrap), f"{name} is not a trap; refusing to call"
        with expect_trips():
            # the trip label is the qualified "<module>.<dll>.<member>" (WP-R2a)
            with pytest.raises(RealDesktopEffectInOfflineSuite, match=rf"_user32\.{name}'"):
                trap()


def test_trap_members_accept_prototype_binding_m1() -> None:
    """``_clipboard_bind`` assigns argtypes/restype on members: a trap must TRIP
    on the call, not raise AttributeError on the assignment."""
    import ctypes

    user32 = _require_user32_tripwire()
    trap = getattr(user32, "OpenClipboard")
    assert isinstance(trap, _EffectTrap), "refusing to touch a non-trap member"
    trap.argtypes = [ctypes.c_void_p]  # must be accepted silently
    trap.restype = ctypes.c_int
    with expect_trips():
        with pytest.raises(RealDesktopEffectInOfflineSuite, match=r"_user32\.OpenClipboard'"):
            trap(None)


def test_tripwire_keeps_readonly_user32_calls_allowed() -> None:
    user32 = _require_user32_tripwire()
    passthrough = user32.GetForegroundWindow
    assert not isinstance(passthrough, _EffectTrap), "GetForegroundWindow must NOT be trapped"
    foreground = passthrough()
    assert foreground is None or isinstance(foreground, int)  # a real read-only call


def test_tripwire_blocks_gui_app_popen() -> None:
    assert subprocess.Popen is _TripwirePopen, "tripwire Popen not installed; refusing to call"
    with expect_trips(3):
        with pytest.raises(RealDesktopEffectInOfflineSuite, match="OFFLINE suite"):
            subprocess.Popen([_PROBE_EXE])
        with pytest.raises(RealDesktopEffectInOfflineSuite, match="OFFLINE suite"):
            subprocess.Popen([_PROBE_EXE, "C:\\x.txt"])
        with pytest.raises(RealDesktopEffectInOfflineSuite, match="OFFLINE suite"):
            subprocess.Popen(_PROBE_EXE)


def test_tripwire_blocks_os_startfile(monkeypatch: pytest.MonkeyPatch) -> None:
    trap = getattr(os, "startfile")
    assert getattr(trap, "_cortex_tripwire_trap", False), "startfile trap not installed"
    with expect_trips():
        with pytest.raises(RealDesktopEffectInOfflineSuite, match="startfile"):
            trap("anything.txt")


def test_tripwire_blocks_pyautogui_effects_when_loaded() -> None:
    pyautogui = sys.modules.get("pyautogui")
    if pyautogui is None:
        pytest.skip("pyautogui not loaded (the fixture must NOT import it — see M3 pin)")
    for name in sorted(_PINNED_PYAUTOGUI_EFFECTS):
        effect = getattr(pyautogui, name)
        assert getattr(effect, "_cortex_tripwire_trap", False), (
            f"pyautogui.{name} is not trapped; refusing to call"
        )
        with expect_trips():
            with pytest.raises(RealDesktopEffectInOfflineSuite, match=f"pyautogui\\.{name}"):
                effect()


def test_tripwire_covers_the_backend_launch_path_seam() -> None:
    """The backend launch path goes through the SAME patched stdlib subprocess."""
    assert backend_module.subprocess.Popen is subprocess.Popen


# --- m2: the launch validator ----------------------------------------------------------


def _rejected(argv: Any, **kwargs: Any) -> None:
    """Assert check_launch refuses this launch (the refusal RECORDS a trip, so the
    teardown log assertion needs it marked expected)."""
    with expect_trips():
        with pytest.raises(RealDesktopEffectInOfflineSuite):
            check_launch(argv, **kwargs)


def test_check_launch_rejects_shell_one_liners() -> None:
    _rejected("ver & notepad", shell=True)
    _rejected("notepad", shell=True)
    _rejected(["cmd", "/c", "ver"], shell=True)


def test_check_launch_allows_only_the_exact_ver_probe() -> None:
    check_launch("ver", shell=True)  # the stdlib platform.win32_ver probe: not recorded
    _rejected(["ver"])
    assert desktop_tripwire.SHELL_ONLY_ALLOWLIST == frozenset({"ver"})


def test_check_launch_rejects_foreign_python_children() -> None:
    _rejected(["python", "script.py"])
    _rejected([r"C:\\other\\python.exe", "-m", "pytest"])


def test_check_launch_rejects_python_dash_c_children() -> None:
    _rejected([sys.executable, "-c", "import os; os.startfile('x')"])
    # ... while the suite's own re-run form (-m, no -c) stays allowed:
    check_launch([sys.executable, "-m", "pytest", "-q", "tests/test_r20_reanchor_causality.py"])


def test_check_launch_rejects_executable_override() -> None:
    _rejected([sys.executable, "-m", "pytest"], executable=str(_PROBE_EXE))
    _rejected(["whatever"], executable="notepad.exe")


def test_check_launch_rejects_dropped_console_tools() -> None:
    # git/uv/taskkill were DROPPED from the allowlist (no ungated test spawns them;
    # the live-gated t8_b5 node runs only with the tripwire disarmed entirely).
    for argv in (
        ["git", "status"],
        ["git", "diff"],
        ["uv", "run", "pytest"],
        ["taskkill", "/PID", "1234", "/F"],
        ["notepad.exe"],
        ["explorer"],
    ):
        _rejected(argv)


def test_runtime_popen_goes_through_the_validator() -> None:
    assert subprocess.Popen is _TripwirePopen
    with expect_trips():
        with pytest.raises(RealDesktopEffectInOfflineSuite, match="OFFLINE suite"):
            subprocess.run([_PROBE_EXE], capture_output=True)  # never spawns: trap first


# --- WP8 carry-overs (VAL-WP-H-02 NITs, N1, N2) -----------------------------------------


def test_check_launch_rejects_dash_c_option_clusters() -> None:
    """WP-H NIT: an option cluster that smuggles 'c' (e.g. ``-Ic``) is a banned
    ``python -c`` child in disguise; the literal ``"-c"`` match missed it."""
    _rejected([sys.executable, "-Ic", "import os; os.startfile('x')"])
    _rejected([sys.executable, "-ccode"])
    # ... while the plain compliant forms stay allowed:
    check_launch([sys.executable, "-I", "somedir", "-m", "pytest", "-q", "tests/x.py"])


def test_check_launch_restricts_dash_m_to_pytest() -> None:
    """WP-H NIT + F12 (VAL-WP8-01): ``-m`` children are restricted to pytest —
    including GLUED (``-mcode``) and CLUSTERED (``-Im code``) spellings, and a
    bare ``-`` (stdin code) is rejected."""
    check_launch([sys.executable, "-m", "pytest", "-q", "tests/x.py"])
    check_launch([sys.executable, "-mpytest", "-q", "tests/x.py"])  # glued, still pytest
    # R3 (VAL-WP8-03): 'm' ANYWHERE in the cluster — mid-cluster glued modules
    # are caught, the isolated -I -m form stays allowed, -X values are exempt
    check_launch([sys.executable, "-I", "somedir", "-m", "pytest", "-q", "tests/x.py"])
    _rejected([sys.executable, "-Imwebbrowser", "http://example.invalid"])
    _rejected([sys.executable, "-Bmwebbrowser", "http://example.invalid"])
    _rejected([sys.executable, "-Im", "webbrowser"])
    check_launch([sys.executable, "-X", "frozen_modules=off", "-m", "pytest", "-q", "tests/x.py"])
    check_launch([sys.executable, "-Xfrozen_modules=off", "-m", "pytest", "-q", "tests/x.py"])
    _rejected([sys.executable, "-m", "code"])
    _rejected([sys.executable, "-mwebbrowser", "http://example.invalid"])
    _rejected([sys.executable, "-Im", "webbrowser"])
    _rejected([sys.executable, "-m"])
    _rejected([sys.executable, "-"])
    _rejected([sys.executable, "-m", "unittest", "discover"])


def test_runtime_exec_and_posix_spawn_traps_are_armed_and_trip() -> None:
    """F12 (VAL-WP8-01): ``os.exec*`` (process REPLACEMENT) and
    ``os.posix_spawn*`` (when present) are runtime-trapped and trip."""
    assert getattr(os.execv, "_cortex_tripwire_trap", False), "os.execv not trapped"
    with expect_trips():
        with pytest.raises(RealDesktopEffectInOfflineSuite, match="os\\.execv"):
            os.execv(_PROBE_EXE, [_PROBE_EXE])  # never execs: trap first
    posix_spawn = getattr(os, "posix_spawn", None)
    if posix_spawn is not None:
        assert getattr(posix_spawn, "_cortex_tripwire_trap", False)
        with expect_trips():
            with pytest.raises(RealDesktopEffectInOfflineSuite, match="os\\.posix_spawn"):
                posix_spawn(_PROBE_EXE, [_PROBE_EXE])


def test_runtime_os_spawn_traps_are_armed() -> None:
    """WP-H NIT: ``os.system`` / ``os.popen`` / ``os.spawn*`` / ``os.exec*`` /
    ``asyncio.create_subprocess_*`` are runtime-trapped (via monkeypatch, so the
    traps are restored after each test instead of persisting)."""
    import asyncio

    assert getattr(os.system, "_cortex_tripwire_trap", False), "os.system not trapped"
    assert getattr(os.popen, "_cortex_tripwire_trap", False), "os.popen not trapped"
    for name in ("spawnl", "spawnle", "spawnv", "spawnve", "spawnlp", "spawnvp", "spawnlpe", "spawnvpe",
                 "execl", "execle", "execv", "execve", "execvp", "execvpe"):
        trap = getattr(os, name, None)
        if trap is None:
            continue  # POSIX-only spawners are absent on Windows
        assert getattr(trap, "_cortex_tripwire_trap", False), f"os.{name} not trapped"
    assert getattr(asyncio.create_subprocess_exec, "_cortex_tripwire_trap", False)
    assert getattr(asyncio.create_subprocess_shell, "_cortex_tripwire_trap", False)


def test_runtime_os_spawn_traps_trip_without_acting() -> None:
    """The traps RAISE before any process is created or any shell command runs."""
    import asyncio

    with expect_trips():
        with pytest.raises(RealDesktopEffectInOfflineSuite, match="os\\.system"):
            os.system("notepad")  # never runs: the trap fires first
    with expect_trips():
        with pytest.raises(RealDesktopEffectInOfflineSuite, match="os\\.popen"):
            os.popen("notepad")
    with expect_trips():
        with pytest.raises(RealDesktopEffectInOfflineSuite, match="os\\.spawnl"):
            os.spawnl(0, _PROBE_EXE)  # never spawns: trap first
    with expect_trips():
        with pytest.raises(
            RealDesktopEffectInOfflineSuite, match="asyncio\\.create_subprocess_exec"
        ):
            asyncio.create_subprocess_exec(_PROBE_EXE)  # no coroutine is created


def test_pyautogui_traps_install_through_monkeypatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """WP-H NIT: the pyautogui effect traps are installed through ``monkeypatch``
    (restored after each test), never a bare ``setattr`` on the module."""
    import types as _types

    stub = _types.SimpleNamespace(click=lambda: None, moveTo=lambda: None)
    desktop_tripwire._patch_pyautogui_effects(monkeypatch, stub)
    assert getattr(stub.click, "_cortex_tripwire_trap", False)
    assert getattr(stub.moveTo, "_cortex_tripwire_trap", False)


def test_controlled_env_restores_an_ambient_cortex_key_also_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WP-H N1 (VAL-WP-H-02): an override key that is ALSO an ambient ``CORTEX_*``
    key must be restored to its AMBIENT value when the controlled env exits — the
    old second save overwrote the ambient value with ``None`` and DELETED the key."""
    from fresh_backend_import import _ControlledEnv

    key = "CORTEX_WP8_N1_PROBE"
    monkeypatch.setenv(key, "ambient-value")
    with _ControlledEnv({key: "override-value"}):
        assert os.environ[key] == "override-value"
    assert os.environ[key] == "ambient-value"


def test_fresh_module_user32_is_trapped_when_the_tripwire_is_armed() -> None:
    """WP-H N2 (VAL-WP-H-02): the fresh module's PRIVATE ``_user32`` is replaced
    with a tripwire wrapper when the desktop tripwire is armed — a future
    fresh-exec expression cannot act on the desktop untrapped."""
    from fresh_backend_import import fresh_backend

    module = fresh_backend({})
    user32 = getattr(module, "_user32", None)
    assert user32 is not None, "the fresh exec bound no _user32"
    assert isinstance(user32, _TripwireUser32), (
        "the fresh module's _user32 is UNTRAPPED while the tripwire is armed"
    )
    trap = getattr(user32, "SetCursorPos")
    assert isinstance(trap, _EffectTrap), "refusing to touch a non-trap member"
    with expect_trips():
        with pytest.raises(RealDesktopEffectInOfflineSuite, match=r"_user32\.SetCursorPos'"):
            trap(10, 10)


# --- M3: the fixture never imports pyautogui -------------------------------------------


def test_fixture_never_imports_pyautogui(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "pyautogui", raising=False)
    monkeypatch.setattr(backend_module, "_user32", None)
    # Re-install directly (the same call the autouse fixture makes) with pyautogui
    # ABSENT from sys.modules, then prove the install did not import it and that a
    # meta-path finder is on duty for the first real import instead.
    desktop_tripwire.install_tripwire(monkeypatch, "m3-pin-node")
    assert "pyautogui" not in sys.modules, "install_tripwire must not import pyautogui"
    assert any(isinstance(finder, _PyautoguiTripwireFinder) for finder in sys.meta_path)


# --- M1: a swallowed trip still fails the test (proven end-to-end with pytester) -------


def test_swallowed_trip_fails_the_test_at_teardown(tmp_path: Any) -> None:
    """End-to-end M1 proof (nested pytest run under pytest's own tmp_path — the
    pytester plugin is unavailable on this pytest 9 gate venv, so the nested run
    is driven directly): a test that swallows the trip error with ``except
    Exception`` FAILS at the fixture teardown with the trip-log assertion."""
    tests_dir = Path(__file__).parent
    source_root = tests_dir.parent / "src"
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent(
            f"""
            import sys

            import pytest

            sys.path.insert(0, r"{tests_dir}")
            sys.path.insert(0, r"{source_root}")

            from desktop_tripwire import assert_no_unexpected_trips, install_tripwire

            @pytest.fixture(autouse=True)
            def _trip(request, monkeypatch):
                install_tripwire(monkeypatch, request.node.nodeid)
                yield
                assert_no_unexpected_trips(request.node.nodeid)
            """
        ).lstrip("\n"),
        encoding="utf-8",
    )
    (tmp_path / "test_swallow.py").write_text(
        textwrap.dedent(
            """
            import subprocess

            def test_swallow_the_trip():
                try:
                    subprocess.Popen(["cortex-hermeticity-gui-probe.exe"])
                except Exception:
                    pass  # exactly the swallow class M1 forbids
            """
        ).lstrip("\n"),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(source_root), str(tests_dir), env.get("PYTHONPATH", "")])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 1, f"expected a failing run:\n{completed.stdout}"
    # the CALL phase passed (the swallow hid the trip); the TEARDOWN failed it:
    assert "1 passed, 1 error" in completed.stdout, completed.stdout
    assert "SWALLOWED REAL DESKTOP TRIP" in completed.stdout, completed.stdout


# --- M2: the AST launch guard ----------------------------------------------------------

#: spawn functions flagged wherever they appear (regardless of base/alias): the
#: names are unambiguous process spawners.
_REGARDLESS_BASE = frozenset(
    {
        "Popen",
        "startfile",
        "popen",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "ShellExecute",
        "ShellExecuteEx",
        "ShellExecuteW",
        "create_subprocess_exec",
        "create_subprocess_shell",
        # F12 residual (VAL-WP8-02): native process creation the runtime traps
        # cannot cover. ``multiprocessing.Process(...)`` spawns a sys.executable
        # child via _winapi.CreateProcess; ``_winapi.CreateProcess`` IS that
        # transport — trapping it at runtime would break the ALLOWED
        # sys.executable children (subprocess.Popen's Windows path), so these
        # are guarded STATICALLY: any call so named is a violation, wherever it
        # appears in ungated test code.
        "CreateProcess",
        "Process",
    }
)
#: generic names flagged ONLY when the base resolves to a process-spawning module
#: or the name is bound FROM such a module — `run`/`call`/`system` are ubiquitous
#: method names (asyncio.run, runner.run, platform.system()) and flagging them
#: regardless of base would drown the guard in false positives. `Popen`,
#: `startfile` and the exotic spawners stay regardless-of-base.
_RESOLVED_ONLY = frozenset({"run", "call", "check_call", "check_output", "system"})
_SPAWNING_MODULES = frozenset({"subprocess", "os", "asyncio.subprocess"})
#: helpers only consumed by e2e-MARKED tests (D6 fail-closed live gate — the same
#: gate as LIVE_DESKTOP); their spawn helpers take dynamic argv by design.
_E2E_PROXY_GATED_FILES = frozenset({"e2e/helpers_win32.py"})


class _ModuleImports:
    """Alias resolution for one module tree."""

    def __init__(self, tree: ast.Module) -> None:
        self.module_aliases: dict[str, str] = {}
        self.from_bindings: dict[str, str] = {}
        self.live_names: set[str] = set()
        self._collect(tree)
        self._drop_shadowed(tree)

    def _collect(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.module_aliases[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    bound = f"{node.module}.{alias.name}"
                    local = alias.asname or alias.name
                    self.from_bindings[local] = bound
                    if node.module == "desktop_tripwire" and alias.name in {
                        "LIVE_DESKTOP",
                        "LIVE_CLIPBOARD",
                    }:
                        self.live_names.add(local)

    def _drop_shadowed(self, tree: ast.Module) -> None:
        """A module-level assignment shadows an imported LIVE marker (NIT: a local
        ``LIVE_DESKTOP = pytest.mark.skipif(False)`` does NOT gate anything)."""
        for node in tree.body:
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in self.live_names:
                    self.live_names.discard(target.id)


def _is_sys_executable_node(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
        and node.attr == "executable"
    )


def _kw_is_true(call: ast.Call, name: str) -> bool:
    for keyword in call.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            return keyword.value.value is True
    return False


def _has_dash_c(call: ast.Call) -> bool:
    if isinstance(call.args[0], (ast.List, ast.Tuple)):
        return any(
            isinstance(el, ast.Constant) and isinstance(el.value, str) and el.value == "-c"
            for el in call.args[0].elts
        )
    return False


def _target_violation(call: ast.Call, label: str) -> str | None:
    """Fail-closed first-argument analysis for Popen/run/call/check_*/startfile."""
    if not call.args:
        return f"{label}() with no arguments"
    if _kw_is_true(call, "shell"):
        first = call.args[0]
        if (
            isinstance(first, ast.Constant)
            and isinstance(first.value, str)
            and first.value.strip() == "ver"
        ):
            return None  # the stdlib platform probe, the ONLY allowed shell form
        return f"{label}(..., shell=True) at line {call.lineno} — only 'ver' is allowed"
    if any(kw.arg == "executable" for kw in call.keywords):
        return f"{label}(..., executable=...) at line {call.lineno} — not allowed in tests"
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return f"{label}({first.value!r}) at line {call.lineno} — not an allowlisted target"
    if isinstance(first, (ast.List, ast.Tuple)):
        if not first.elts:
            return f"{label}([]) with an empty argv"
        element = first.elts[0]
        if _is_sys_executable_node(element):
            if _has_dash_c(call):
                return f"{label}([sys.executable, '-c', ...]) at line {call.lineno} — -c children are banned"
            return None  # sys.executable without -c: the suite's own re-run form
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            return f"{label}([{element.value!r}, ...]) at line {call.lineno} — not an allowlisted target"
        return (
            f"{label} with a NON-CONSTANT argv[0] at line {call.lineno} — the target "
            "cannot be proven safe; use a constant, sys.executable, or gate with LIVE_DESKTOP"
        )
    return (
        f"{label} with a NON-CONSTANT target at line {call.lineno} — cannot be proven safe"
    )


class _SpawnScanner:
    """Collect process-spawn calls outside LIVE/e2e-gated functions."""

    def __init__(self, tree: ast.Module, in_e2e_dir: bool) -> None:
        imports = _ModuleImports(tree)
        self.module_aliases = imports.module_aliases
        self.from_bindings = imports.from_bindings
        self.live_names = imports.live_names
        self.in_e2e_dir = in_e2e_dir
        self.file_e2e_gated = self._module_level_e2e_marker(tree)
        self.violations: list[str] = []

    def _module_level_e2e_marker(self, tree: ast.Module) -> bool:
        """``pytestmark = pytest.mark.e2e`` (or a list containing it): every test in
        the file is D6-gated and cannot run offline."""
        for node in tree.body:
            if not (isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
            )):
                continue
            for candidate in ast.walk(node.value):
                if (
                    isinstance(candidate, ast.Attribute)
                    and candidate.attr == "e2e"
                    and self.in_e2e_dir
                ):
                    return True
        return False

    def scan(self, tree: ast.Module) -> None:
        self._walk(tree, gated=False)

    def _walk(self, node: ast.AST, gated: bool) -> None:
        """Depth-first walk that skips the ENTIRE subtree of a gated function
        (nested worker functions included) and checks every other Call."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._walk(child, gated or self.file_e2e_gated or self._is_gated(child))
                continue
            if not gated and isinstance(child, ast.Call):
                self._check_call(child)
            self._walk(child, gated)

    def _is_gated(self, node: ast.AST) -> bool:
        for decorator in getattr(node, "decorator_list", []):
            names: list[str] = []
            if isinstance(decorator, ast.Name):
                names = [decorator.id]
            elif isinstance(decorator, ast.Attribute):
                names = [decorator.attr]
            if any(name in self.live_names for name in names):
                return True
            if any(name == "e2e" for name in names) and self.in_e2e_dir:
                return True  # D6 fail-closed gate: cannot run offline
        return False

    def _call_label(self, call: ast.Call) -> str | None:
        func = call.func
        if isinstance(func, ast.Name):
            bound = self.from_bindings.get(func.id)
            if bound is not None:
                module, _, attr = bound.rpartition(".")
                if attr in _REGARDLESS_BASE:
                    return bound
                if attr in _RESOLVED_ONLY and module in _SPAWNING_MODULES:
                    return bound
            if func.id in _REGARDLESS_BASE:
                return func.id
            return None
        if isinstance(func, ast.Attribute):
            attr = func.attr
            if attr in _REGARDLESS_BASE:
                return attr
            if attr in _RESOLVED_ONLY:
                base = func.value
                if isinstance(base, ast.Name):
                    module = self.module_aliases.get(base.id)
                    if module in _SPAWNING_MODULES:
                        return f"{module}.{attr}"
                    # a bare local name (e.g. an in-test fake runner object) is NOT
                    # a spawning module — asyncio.run/runner.run stay unflagged
            return None
        return None

    def _check_call(self, call: ast.Call) -> None:
        label = self._call_label(call)
        if label is None:
            return
        violation = _target_violation(call, label)
        if violation is not None:
            self.violations.append(violation)


def _scan_source(source: str) -> list[str]:
    tree = ast.parse(source)
    scanner = _SpawnScanner(tree, in_e2e_dir=False)
    scanner.scan(tree)
    return scanner.violations


# --- M2 self-tests: every evasion form over in-memory snippets ------------------------


def test_ast_guard_catches_evasion_forms() -> None:
    caught = [
        'import subprocess\nsubprocess.Popen(["notepad.exe"])\n',
        'import subprocess as sp\nsp.Popen(["notepad.exe"])\n',
        "from subprocess import Popen as P\nP(['notepad.exe'])\n",
        "import subprocess\nprocs.append(subprocess.Popen(['notepad.exe']))\n",
        'import subprocess\nsubprocess.run("notepad", shell=True)\n',
        "import os\nos.system('notepad')\n",
        "import os\nos.startfile('thing.txt')\n",
        "import os\nos.popen('notepad')\n",
        "import os\nos.spawnlp(0, 'notepad')\n",
        "import asyncio\nasyncio.create_subprocess_exec('notepad')\n",
        "import subprocess\nsubprocess.check_output('notepad', shell=True)\n",
        "import subprocess\nsubprocess.Popen([SOME_VARIABLE])\n",  # non-constant: fail-closed
        "import subprocess\nsubprocess.run([sys.executable, '-c', 'x'])\n",
        "import subprocess\nsubprocess.Popen(['notepad'], executable='notepad.exe')\n",
        "import subprocess\nsubprocess.Popen(['notepad'], shell=True)\n",
        'import ctypes\n_windll = ctypes\n_windll.ShellExecuteW(0, "open", "notepad")\n',
        # F12 residual (VAL-WP8-02): multiprocessing / _winapi native creation
        "import multiprocessing\nmultiprocessing.Process(target=f).start()\n",
        "import multiprocessing as mp\nmp.Process(target=f)\n",
        "import _winapi\n_winapi.CreateProcess(None, 'notepad', None, None, 0, 0, None, None, b'')\n",
    ]
    for snippet in caught:
        violations = _scan_source(snippet)
        assert violations, f"evasion NOT caught: {snippet!r}"


def test_ast_guard_allows_the_compliant_forms() -> None:
    allowed = [
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-m', 'pytest', '-q', 'tests/x.py'])\n",
        'import subprocess\nsubprocess.run("ver", shell=True)\n',
        "import asyncio\nasyncio.run(coro())\n",  # not a spawning module
        "class R:\n    def run(self, a):\n        pass\nR().run(make_task())\n",
        "def run(a):\n    return a\nrun(compute())\n",
    ]
    for snippet in allowed:
        assert not _scan_source(snippet), f"false positive: {snippet!r}"


def test_ast_guard_gating_rules() -> None:
    header = "from desktop_tripwire import LIVE_DESKTOP\n"
    # the SHARED marker gates the subtree (nested Popen included):
    gated = (
        header
        + "import subprocess\n"
        + "@LIVE_DESKTOP\n"
        + "def test_live():\n"
        + "    def worker():\n"
        + "        subprocess.Popen(['notepad.exe'])\n"
        + "    worker()\n"
    )
    assert not _scan_source(gated)
    # a module-local shadow of the name does NOT gate (NIT fix):
    shadowed = (
        header
        + "import pytest\n"
        + "LIVE_DESKTOP = pytest.mark.skipif(False, reason='x')\n"
        + "import subprocess\n"
        + "@LIVE_DESKTOP\n"
        + "def test_fake_live():\n"
        + "    subprocess.Popen(['notepad.exe'])\n"
    )
    assert _scan_source(shadowed), "shadowed LIVE_DESKTOP must not count as gated"
    # an unrelated helper called from ungated code does not inherit gating:
    helper = (
        header
        + "import subprocess\n"
        + "@LIVE_DESKTOP\n"
        + "def _launch_helper():\n"
        + "    subprocess.Popen(['notepad.exe'])\n"
        + "def test_ungated():\n"
        + "    _launch_helper()\n"
    )
    assert not _scan_source(helper)  # the Popen lives INSIDE the gated helper


def test_no_process_spawns_in_ungated_test_code() -> None:
    """The file-tree guard: tests/ (INCLUDING tests/e2e offline-running modules,
    e.g. test_benchmark_harness.py) minus the justified exclusions:
    - e2e/helpers_win32.py: consumed ONLY by e2e-marked tests (D6 fail-closed
      live gate — the same gate as LIVE_DESKTOP); its spawn helpers take
      dynamic argv by design.
    - this guard file: its own snippets are the in-memory self-tests above.
    """
    tests_dir = Path(__file__).parent
    violations: list[str] = []
    for path in sorted(tests_dir.rglob("*.py")):
        relative = path.relative_to(tests_dir).as_posix()
        if relative in _E2E_PROXY_GATED_FILES or relative == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        scanner = _SpawnScanner(tree, in_e2e_dir=relative.startswith("e2e/"))
        scanner.scan(tree)
        violations.extend(f"{relative}: {violation}" for violation in scanner.violations)
    assert not violations, (
        "WP-H AST guard: process spawns in ungated test code:\n" + "\n".join(violations)
    )
