"""WP-H hermetic-suite tripwire v2 (DELTA 1) + the shared LIVE_DESKTOP marker.

The offline suite must NEVER touch the user's desktop. The autouse fixture in
``tests/conftest.py`` arms this tripwire for every test whenever
``CUMCP_RUN_E2E != "1"``:

- ``backend._user32`` effect members become :class:`_EffectTrap` callables that
  raise :class:`RealDesktopEffectInOfflineSuite` on call (they ACCEPT attribute
  assignment, so ``_clipboard_bind``'s ``argtypes``/``restype`` writes trip
  instead of raising ``AttributeError``); read-only members pass through.
- ``subprocess.Popen`` becomes :class:`_TripwirePopen`: a strict validator
  (:func:`check_launch`) allows ONLY the spawns the suite actually needs —
  ``sys.executable`` children without ``-c``, and the stdlib ``platform``
  probe (``ver`` via shell). ``git``/``uv``/``taskkill`` were DROPPED (no
  ungated test spawns them; the live-gated t8_b5 node runs only with the
  tripwire disarmed).
- ``os.startfile`` always trips.
- pyautogui effect functions are trapped when pyautogui is ALREADY imported;
  otherwise a meta-path import hook traps it at first import, AFTER calling the
  backend's own ``_set_process_dpi_awareness()`` — mirroring the production
  import order (backend.py:4291 setter, :4299 import) so the process DPI state
  ends exactly as production sets it. The FIXTURE ITSELF NEVER IMPORTS
  pyautogui (DPI hazard: pyautogui's import calls SetProcessDPIAware).

Swallow-proofing (DELTA 1 M1): every trip is recorded in a module-level,
thread-safe log BEFORE raising, and the autouse fixture asserts at TEARDOWN
that this test left no unexpected entries — so ``except Exception`` swallowers
(backend._launch's degraded outcome, agent.run_single error kinds, focus_guard
refocus errors, worker threads) still fail the test that caused them. A test
that needs to trigger a trap wraps the trigger in :func:`expect_trips`.

Ordering contract: installed BEFORE the test's own fixtures, so a test's own
stubs at or above these points override the tripwire unchanged.
"""

from __future__ import annotations

import contextlib
import importlib.abc
import importlib.util
import os
import subprocess
import sys
import threading
import traceback
from typing import Any

import pytest

import computer_use_mcp.backend as backend_module


class RealDesktopEffectInOfflineSuite(AssertionError):
    """Raised INSTEAD of acting when the offline suite reaches a real desktop effect."""


#: The shared gate for every test that performs a REAL desktop effect (real input,
#: real system clipboard, real app launch): skipped offline, runs only under the
#: WP12 live gate (CUMCP_RUN_E2E=1, the same gate as tests/e2e).
LIVE_DESKTOP = pytest.mark.skipif(
    os.environ.get("CUMCP_RUN_E2E") != "1",
    reason=(
        "touches the REAL desktop (real input / real system clipboard / real app "
        "launch); live opt-in only (CUMCP_RUN_E2E=1, the tests/e2e / WP12 live gate)"
    ),
)


#: backend._user32 members that ACT on the desktop (11). Everything else
#: (GetCursorPos, EnumWindows, GetForegroundWindow, GetWindowTextW, GetWindowRect,
#: GetAsyncKeyState, IsIconic, GetWindowThreadProcessId, GetMonitorInfoW,
#: GetSystemMetrics, SetProcessDpiAwareness*, screen capture, clipboard READS)
#: is read-only and passes through.
EFFECT_USER32_NAMES = frozenset(
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

#: pyautogui members that act on the desktop (12). Reads (size/position/...) stay.
EFFECT_PYAUTOGUI_NAMES = frozenset(
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

#: Non-python launch targets the offline suite may spawn — ONLY the stdlib
#: ``platform.win32_ver`` probe (``ver`` via shell=True, reached from
#: benchmarks/runner.py:707 in fake-mode harness tests). Spawned apps (notepad,
#: calc, ...) and console tools (git/uv/taskkill — no ungated test spawns them)
#: are NOT on the list: the offline suite never launches anything else.
SHELL_ONLY_ALLOWLIST = frozenset({"ver"})

# --- the trip log (DELTA 1 M1): a raised trip is never the only record ---------------

_TRIP_LOCK = threading.Lock()
_TRIP_LOG: list[dict[str, Any]] = []
_EXPECTED: dict[str, int] = {}
_CURRENT_NODE: str = "<no node>"


def clear_trip_log() -> None:
    with _TRIP_LOCK:
        _TRIP_LOG.clear()
        _EXPECTED.clear()


def trip_log_snapshot() -> list[dict[str, Any]]:
    with _TRIP_LOCK:
        return [dict(entry) for entry in _TRIP_LOG]


def expect_trips(count: int = 1):
    """Mark ``count`` trips as EXPECTED for the current test (used by the guard
    tests that must trigger a trap on purpose; the teardown assertion tolerates
    exactly the expected number and still fails on anything beyond it)."""
    node = _CURRENT_NODE

    @contextlib.contextmanager
    def _ctx():
        with _TRIP_LOCK:
            _EXPECTED[node] = _EXPECTED.get(node, 0) + count
        try:
            yield
        finally:
            with _TRIP_LOCK:
                _EXPECTED[node] = _EXPECTED.get(node, 0) - count
                if _EXPECTED[node] <= 0:
                    _EXPECTED.pop(node, None)

    return _ctx()


def assert_no_unexpected_trips(node: str) -> None:
    """Teardown assertion: this test recorded no UNEXPECTED trip (a swallowed
    trip — the error was caught by an ``except Exception`` — still fails HERE)."""
    unexpected = [entry for entry in trip_log_snapshot() if entry["node"] == node and not entry["expected"]]
    if unexpected:
        lines = "\n".join(
            f"  - {entry['call']}: {entry['stack']}" for entry in unexpected
        )
        raise AssertionError(
            "SWALLOWED REAL DESKTOP TRIP: the offline tripwire raised but the "
            f"exception was swallowed (test node: {node}). The real desktop was "
            f"NOT touched (the trap raised first), but the test must not pass "
            f"while reaching for one. Trips:\n{lines}"
        )


def _record_trip(call: str) -> None:
    node = _CURRENT_NODE
    frames = traceback.format_stack(limit=7)[:-1]
    stack_summary = " <- ".join(
        frame.strip().splitlines()[-1] for frame in reversed(frames[-5:])
    )
    with _TRIP_LOCK:
        expected = _EXPECTED.get(node, 0) > 0
        if expected:
            _EXPECTED[node] -= 1
        _TRIP_LOG.append(
            {"call": call, "node": node, "expected": expected, "stack": stack_summary}
        )


def _trip_raise(call: str) -> None:
    _record_trip(call)
    raise RealDesktopEffectInOfflineSuite(
        f"REAL desktop effect '{call}' attempted in the OFFLINE suite "
        f"(CUMCP_RUN_E2E != '1'), test node: {_CURRENT_NODE}. The offline suite "
        "must never act on the user's desktop: stub the effect at the lowest seam, "
        "or gate the test with the shared LIVE_DESKTOP marker "
        "(tests/desktop_tripwire.py)."
    )


# --- user32 traps ---------------------------------------------------------------------


class _EffectTrap:
    """Callable trap for one effect member.

    Accepts attribute assignment (``argtypes``/``restype``/``errcheck``) so a
    prototype-binding call (``_clipboard_bind``) TRIPS like any other use instead
    of raising ``AttributeError``. Instances carry the ``_cortex_tripwire_trap``
    marker so the arming-proof tests can verify the trap is installed BEFORE
    calling anything.
    """

    _cortex_tripwire_trap = True

    def __init__(self, name: str) -> None:
        self.__name__ = name
        self._name = name

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        _trip_raise(self._name)


class _TripwireUser32:
    """Read-only passthrough for ``backend._user32``; effect members are traps."""

    def __init__(self, real: Any, node_id: str) -> None:
        self.__dict__["_real"] = real
        self.__dict__["_cortex_tripwire"] = True
        self.__dict__["_node_id"] = node_id
        for name in EFFECT_USER32_NAMES:
            self.__dict__[name] = _EffectTrap(name)

    def __getattr__(self, name: str) -> Any:
        # Read-only passthrough; the resolved attribute is cached as an instance
        # attribute so ctypes prototypes bound on the real DLL keep applying.
        value = getattr(self.__dict__["_real"], name)
        self.__dict__[name] = value
        return value


# --- launch validation (pure; the Popen subclass delegates) ---------------------------


def launch_target(argv: Any) -> str | None:
    """The executable a ``Popen``-style argument list would spawn (best effort)."""
    if isinstance(argv, (str, bytes)):
        text = os.fsdecode(argv)
        first = text.split()[0] if text.split() else ""
        return first.strip('"')
    if isinstance(argv, (list, tuple)):
        if not argv:
            return None
        first = argv[0]
        if isinstance(first, bytes):
            return os.fsdecode(first)
        if isinstance(first, str):
            return first
        try:
            return os.fspath(first)
        except TypeError:
            return None
    return None


def _is_sys_executable(target: str) -> bool:
    try:
        return os.path.abspath(target).casefold() == os.path.abspath(sys.executable).casefold()
    except (TypeError, ValueError):  # pragma: no cover - pathological argv
        return False


def _basename(target: str) -> str:
    basename = os.path.basename(target).strip().lower()
    return basename[:-4] if basename.endswith(".exe") else basename


def check_launch(args: Any, *, shell: bool = False, executable: Any = None) -> None:
    """Raise unless this launch is one the offline suite may perform.

    Allowed, exhaustively:
    - ``shell=True`` with the EXACT string ``"ver"`` (the stdlib platform probe);
    - python children whose argv[0] IS ``sys.executable`` and whose argv contains
      NO ``-c`` (``-m`` children such as the suite's own re-runs stay allowed);
    - an ``executable=`` kwarg only when it IS ``sys.executable``.
    Everything else — GUI apps, taskkill, git, uv, shell one-liners, any child
    interpreter that is not this one — raises.
    """
    where = f"test node: {_CURRENT_NODE}"
    if shell:
        if isinstance(args, str) and args.strip() in SHELL_ONLY_ALLOWLIST:
            return
        _record_trip("subprocess spawn (shell)")
        raise RealDesktopEffectInOfflineSuite(
            f"shell launch {args!r} attempted in the OFFLINE suite ({where}): "
            f"shell=True is allowed ONLY for exactly 'ver' ({sorted(SHELL_ONLY_ALLOWLIST)})."
        )
    if executable is not None:
        exe = executable if isinstance(executable, str) else os.fspath(executable)
        if not _is_sys_executable(exe):
            _record_trip("subprocess spawn (executable=)")
            raise RealDesktopEffectInOfflineSuite(
                f"subprocess executable={exe!r} attempted in the OFFLINE suite "
                f"({where}): only sys.executable is allowed."
            )
    target = launch_target(args)
    if target is None:
        _record_trip("subprocess spawn (unresolvable argv)")
        raise RealDesktopEffectInOfflineSuite(
            f"subprocess launch with an unresolvable argv {args!r} ({where})."
        )
    basename = _basename(target)
    if basename.startswith("python"):
        if not isinstance(args, (list, tuple)) or not _is_sys_executable(str(args[0])):
            _record_trip("subprocess spawn (foreign python)")
            raise RealDesktopEffectInOfflineSuite(
                f"python child {args!r} attempted in the OFFLINE suite ({where}): "
                "python children must use sys.executable itself."
            )
        if any(str(part) == "-c" for part in args[1:]):
            _record_trip("subprocess spawn (python -c)")
            raise RealDesktopEffectInOfflineSuite(
                f"python -c child attempted in the OFFLINE suite ({where}): -c "
                "children are banned (arbitrary-code children are invisible to "
                "static analysis); use an in-process fresh import "
                "(tests/fresh_backend_import.py)."
            )
        return
    if basename in SHELL_ONLY_ALLOWLIST:
        _record_trip("subprocess spawn (ver without shell)")
        raise RealDesktopEffectInOfflineSuite(
            f"'ver' is allowed ONLY via shell=True (the stdlib platform probe) ({where})."
        )
    _record_trip(f"subprocess spawn ({target!r})")
    raise RealDesktopEffectInOfflineSuite(
        f"REAL app launch via subprocess (target={target!r}) attempted in the "
        f"OFFLINE suite (CUMCP_RUN_E2E != '1'), {where}. The offline suite must "
        "never launch apps: stub the launch, or gate the test with the shared "
        "LIVE_DESKTOP marker (tests/desktop_tripwire.py)."
    )


class _TripwirePopen(subprocess.Popen):
    """``subprocess.Popen`` refusing every launch :func:`check_launch` rejects.

    Because ``subprocess.run``/``call``/``check_*`` all construct a ``Popen``,
    patching the class covers every spawn path in the process — including the
    backend launch path (``backend.py`` ``subprocess.Popen([target])``) and
    launches made directly from test code.
    """

    def __init__(self, args: Any, *positional: Any, **kwargs: Any) -> None:
        check_launch(
            args,
            shell=bool(kwargs.get("shell", False)),
            executable=kwargs.get("executable"),
        )
        super().__init__(args, *positional, **kwargs)


def _startfile_trap(*args: Any, **kwargs: Any) -> None:
    _record_trip("os.startfile")
    raise RealDesktopEffectInOfflineSuite(
        f"REAL shell launch via os.startfile({args!r}) attempted in the OFFLINE "
        f"suite (CUMCP_RUN_E2E != '1'), test node: {_CURRENT_NODE}."
    )


_startfile_trap._cortex_tripwire_trap = True  # type: ignore[attr-defined]


# --- pyautogui (DELTA 1 M3): never imported by the fixture ----------------------------


def _patch_pyautogui_effects(module: Any) -> None:
    """Trap every effect function on an ALREADY-LOADED pyautogui module."""
    for name in EFFECT_PYAUTOGUI_NAMES:
        setattr(module, name, _EffectTrap(f"pyautogui.{name}"))


class _TrapLoader:
    """Loader wrapper: mirrors the production import order, then traps.

    Production (backend.py:4291 → :4299) sets process DPI awareness BEFORE
    importing pyautogui; pyautogui's import itself calls SetProcessDPIAware.
    Replaying the backend's own setter here keeps the process DPI state exactly
    as production leaves it, whichever import path fires first.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def create_module(self, spec: Any) -> Any:
        return self._inner.create_module(spec)

    def exec_module(self, module: Any) -> None:
        try:
            backend_module._set_process_dpi_awareness()
        except Exception:  # noqa: BLE001 - the setter is best-effort in production too
            pass
        self._inner.exec_module(module)
        _patch_pyautogui_effects(module)


class _PyautoguiTripwireFinder(importlib.abc.MetaPathFinder):
    """Intercept the FIRST ``import pyautogui`` (the fixture never imports it)."""

    _cortex_tripwire_finder = True

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname != "pyautogui":
            return None
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(fullname)
        finally:
            sys.meta_path.insert(0, self)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _TrapLoader(spec.loader)
        return spec


# --- install --------------------------------------------------------------------------


def install_tripwire(monkeypatch: pytest.MonkeyPatch, node_id: str) -> None:
    """Arm every real-desktop EFFECT entry point for ONE test (undo by monkeypatch)."""
    global _CURRENT_NODE
    _CURRENT_NODE = node_id
    clear_trip_log()

    real_user32 = backend_module._user32
    if real_user32 is not None:
        monkeypatch.setattr(backend_module, "_user32", _TripwireUser32(real_user32, node_id))

    monkeypatch.setattr(subprocess, "Popen", _TripwirePopen)
    monkeypatch.setattr(os, "startfile", _startfile_trap, raising=False)

    pyautogui = sys.modules.get("pyautogui")
    if pyautogui is not None:
        _patch_pyautogui_effects(pyautogui)
    else:
        # DELTA 1 M3: do NOT import pyautogui here. Trap it at first import via a
        # meta-path finder (which replays the backend's DPI ordering first).
        monkeypatch.setattr(sys, "meta_path", [_PyautoguiTripwireFinder(), *sys.meta_path])
