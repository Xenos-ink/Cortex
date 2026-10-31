"""WP-H hermetic-suite tripwire v2 (DELTA 1) + the shared LIVE_DESKTOP marker.

The offline suite must NEVER touch the user's desktop. The autouse fixture in
``tests/conftest.py`` arms this tripwire for every test whenever
``CUMCP_RUN_E2E != "1"``:

- EVERY module-level effect-DLL binding of every loaded ``computer_use_mcp.*``
  module (``_user32``/``_kernel32``/``_ole32``/``_oleaut32`` — see
  :data:`EFFECT_DLL_BINDINGS`) is wrapped in :class:`_TripwireDll`: user32's
  effect members become :class:`_EffectTrap` callables that raise
  :class:`RealDesktopEffectInOfflineSuite` on call (they ACCEPT attribute
  assignment, so ``_clipboard_bind``'s ``argtypes``/``restype`` writes trip
  instead of raising ``AttributeError``); read-only members pass through, and
  kernel32/ole32/oleaut32 are pure read-only passthrough.
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
import ctypes
import importlib.abc
import importlib.util
import os
import subprocess
import sys
import threading
import traceback
from types import ModuleType
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

#: The module-level binding NAMES any desktop module may bind an effect DLL
#: through. ``install_tripwire`` wraps EVERY ``computer_use_mcp.*`` module
#: attribute carrying one of these names (WP-R2a: the tripwire must be
#: independent of how backend.py is split). ``_shcore`` is deliberately NOT
#: here: it only carries read-only DPI queries (see the reviewed binding
#: allow-list in tests/test_tripwire_bindings_guard.py).
_TRIPWIRE_DLL_EFFECTS: dict[str, frozenset[str]] = {
    "_user32": EFFECT_USER32_NAMES,
    # kernel32/ole32/oleaut32 are read-only passthrough: their members are
    # process-local memory / COM plumbing; every desktop-reaching effect rides
    # user32. Attribute assignment (prototype argtypes/restype writes) is
    # accepted and trips on USE exactly like on the user32 traps.
    "_kernel32": frozenset(),
    "_ole32": frozenset(),
    "_oleaut32": frozenset(),
}
EFFECT_DLL_BINDINGS = frozenset(_TRIPWIRE_DLL_EFFECTS)

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


class _TripwireDll:
    """Read-only passthrough for ANY module-level effect-DLL binding.

    Effect members of the wrapped DLL (user32's pinned 11; none yet for
    kernel32/ole32/oleaut32) are :class:`_EffectTrap` callables whose trip
    label is ``"<module>.<dll>.<member>"`` so the log names the binding
    module. Read-only members pass through, and the resolved attribute is
    cached as an instance attribute so ctypes prototypes bound on the real
    DLL keep applying.
    """

    def __init__(self, real: Any, dll_name: str, node_id: str, module_name: str) -> None:
        self.__dict__["_real"] = real
        self.__dict__["_cortex_tripwire"] = True
        self.__dict__["_cortex_tripwire_dll"] = dll_name
        self.__dict__["_cortex_tripwire_module"] = module_name
        self.__dict__["_node_id"] = node_id
        for name in _TRIPWIRE_DLL_EFFECTS.get(dll_name, frozenset()):
            self.__dict__[name] = _EffectTrap(f"{module_name}.{dll_name}.{name}")

    def __getattr__(self, name: str) -> Any:
        # Read-only passthrough; the resolved attribute is cached as an instance
        # attribute so ctypes prototypes bound on the real DLL keep applying.
        value = getattr(self.__dict__["_real"], name)
        self.__dict__[name] = value
        return value


#: Backward-compat alias: the arming-proof tests isinstance against the
#: original user32-only wrapper name.
_TripwireUser32 = _TripwireDll


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
        for position, part in enumerate(args[1:]):
            text = str(part)
            # WP-H NIT (VAL-WP-H-02) + F12 (VAL-WP8-01): option clusters are
            # parsed, not matched literally. A single-dash cluster carrying 'c'
            # is a banned ``python -c`` child in disguise (``-Ic``); a cluster
            # carrying 'm' selects a module — the module name is either glued to
            # the cluster (``-mwebbrowser``) or the next argument (``-Im
            # webbrowser``) — and ONLY pytest is allowed; a bare ``-`` reads the
            # program from stdin (arbitrary code, like -c).
            if text == "-":
                _record_trip("subprocess spawn (python stdin code)")
                raise RealDesktopEffectInOfflineSuite(
                    f"python child argument {text!r} reads the program from stdin "
                    f"(arbitrary-code children are banned) ({where})."
                )
            if text.startswith("-") and not text.startswith("--"):
                flags = text[1:]
                # DELTA 3 R3 (VAL-WP8-03): 'm' is found ANYWHERE in the cluster —
                # ``-Imwebbrowser`` parses as -I -m webbrowser. The glued tail
                # after the 'm' (or the next argv when the tail is empty) is the
                # module and must be pytest. EXCEPTION: a cluster whose FIRST
                # letter is ``X`` is ``-X<value>`` — the whole tail is the
                # option's VALUE, never a flag list (``-Xfrozen_modules=off``
                # stays allowed).
                if flags[:1] != "X":
                    if "c" in flags:
                        _record_trip("subprocess spawn (python -c option cluster)")
                        raise RealDesktopEffectInOfflineSuite(
                            f"python child argument {text!r} carries 'c' as an option "
                            f"cluster (a banned ``python -c`` in disguise) ({where})."
                        )
                    index = flags.find("m")
                    if index != -1:
                        tail = flags[index + 1 :]
                        module = tail if tail else (
                            str(args[position + 2]) if position + 2 < len(args) else ""
                        )
                        if module != "pytest":
                            _record_trip("subprocess spawn (python -m outside pytest)")
                            raise RealDesktopEffectInOfflineSuite(
                                f"python -m child {module!r} attempted in the OFFLINE "
                                f"suite ({where}): '-m' is restricted to 'pytest'."
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


def _patch_pyautogui_effects(monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
    """Trap every effect function on an ALREADY-LOADED pyautogui module.

    WP-H NIT (VAL-WP-H-02): the traps are installed through ``monkeypatch`` so they
    are RESTORED after each test instead of persisting on pyautogui for the rest
    of the process.
    """
    for name in EFFECT_PYAUTOGUI_NAMES:
        monkeypatch.setattr(module, name, _EffectTrap(f"pyautogui.{name}"), raising=False)


class _TrapLoader:
    """Loader wrapper: mirrors the production import order, then traps.

    Production (backend.py:4291 → :4299) sets process DPI awareness BEFORE
    importing pyautogui; pyautogui's import itself calls SetProcessDPIAware.
    Replaying the backend's own setter here keeps the process DPI state exactly
    as production leaves it, whichever import path fires first.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, inner: Any) -> None:
        self._monkeypatch = monkeypatch
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
        _patch_pyautogui_effects(self._monkeypatch, module)


class _PyautoguiTripwireFinder(importlib.abc.MetaPathFinder):
    """Intercept the FIRST ``import pyautogui`` (the fixture never imports it)."""

    _cortex_tripwire_finder = True

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # WP-H NIT (VAL-WP-H-02): effect traps are applied through monkeypatch so
        # they are restored after the test instead of persisting on the module.
        self._monkeypatch = monkeypatch

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
        spec.loader = _TrapLoader(self._monkeypatch, spec.loader)
        return spec


# --- install --------------------------------------------------------------------------


def effect_binding_wrappers(
    module: Any, node_id: str, cache: dict[tuple[int, str], "_TripwireDll"] | None = None
) -> list[tuple[str, "_TripwireDll"]]:
    """Tripwire wrappers for ONE module's effect-DLL bindings (the shared helper).

    Every module attribute named in :data:`EFFECT_DLL_BINDINGS` whose current
    value is a real ctypes DLL instance (``ctypes.CDLL`` — covers WinDLL and
    OleDLL) gets a :class:`_TripwireDll` wrapper; ``None`` bindings and
    already-wrapped bindings (idempotent re-install) are skipped. With a
    shared ``cache`` (keyed by ``(id(real), binding name)``) the SAME wrapper
    object is returned for every module binding of the same real DLL — F6a
    (VAL-WP-R2a-0-01): a member patch through any one binding must be seen by
    every module, exactly like production where all bindings share one DLL
    object. The CALLER applies them — through ``monkeypatch.setattr``
    (restored per test) or a plain setattr (the throwaway fresh-exec probe
    module).
    """
    if cache is None:
        cache = {}
    module_name = getattr(module, "__name__", "<unknown>")
    out: list[tuple[str, _TripwireDll]] = []
    for name in sorted(EFFECT_DLL_BINDINGS):
        current = getattr(module, name, None)
        if current is None or isinstance(current, _TripwireDll):
            continue
        if not isinstance(current, ctypes.CDLL):
            continue
        key = (id(current), name)
        if key not in cache:
            cache[key] = _TripwireDll(current, name, node_id, module_name)
        out.append((name, cache[key]))
    return out


def wrap_loaded_effect_bindings(
    monkeypatch: pytest.MonkeyPatch, node_id: str, cache: dict[tuple[int, str], "_TripwireDll"] | None = None
) -> list[str]:
    """Wrap the effect-DLL bindings of EVERY loaded ``computer_use_mcp.*`` module.

    Iterates ``sys.modules`` so the tripwire is independent of how backend.py
    is split (WP-R2a): after the split this traps every ``_user32`` /
    ``_kernel32`` / ``_ole32`` / ``_oleaut32`` binding in every desktop
    module — ONE shared wrapper per real DLL (F6a) — and nothing can reach a
    real effect through any module. Returns the wrapped ``<module>.<binding>``
    names and REGISTERS them in :data:`_WRAPPED_THIS_INSTALL` (F13: the
    teardown re-scan exempts bindings the install itself wrapped — a test's
    own ``monkeypatch.undo()`` removes the fixture's wrappers too, which is
    the pre-existing undo semantics, not a late-import escape)."""
    if cache is None:
        cache = {}
    wrapped: list[str] = []
    for module_name, module in sorted(sys.modules.items()):
        if not module_name.startswith("computer_use_mcp"):
            continue
        if not isinstance(module, ModuleType):
            continue
        for name, wrapper in effect_binding_wrappers(module, node_id, cache):
            monkeypatch.setattr(module, name, wrapper)
            wrapped.append(f"{module_name}.{name}")
            _WRAPPED_THIS_INSTALL.add((module_name, name))
    return wrapped


#: (module, binding) pairs the CURRENT install wrapped — see F13 above.
_WRAPPED_THIS_INSTALL: set[tuple[str, str]] = set()


def find_unwrapped_effect_bindings() -> list[str]:
    """Every loaded ``computer_use_mcp.*`` module binding that holds a REAL,
    UNWRAPPED ctypes DLL under an EFFECT_DLL_BINDINGS name and was NOT wrapped
    by the current install (F13, VAL-WP-R2a-0-01).

    The teardown re-scan: the install-time wrap only sees modules loaded at
    that moment; a module FIRST imported mid-test (after install) binds real
    DLLs no trap covers, and a fixture wrapper on it would never be undone.
    The autouse fixture calls this at teardown, BEFORE the monkeypatch undo,
    and fails the test on any violation. Bindings the install itself wrapped
    are exempt: a test's own ``monkeypatch.undo()`` legitimately removes the
    fixture's wrappers (the fixture's own undo restores the originals after
    the test, exactly as before this guard existed)."""
    violations: list[str] = []
    for module_name, module in sorted(sys.modules.items()):
        if not module_name.startswith("computer_use_mcp"):
            continue
        if not isinstance(module, ModuleType):
            continue
        for name in sorted(EFFECT_DLL_BINDINGS):
            value = getattr(module, name, None)
            if value is None or isinstance(value, _TripwireDll):
                continue
            if not isinstance(value, ctypes.CDLL):
                continue
            if (module_name, name) in _WRAPPED_THIS_INSTALL:
                continue  # undone by the test itself — not a late-import escape
            violations.append(
                f"{module_name}.{name}: UNWRAPPED real DLL bound at module level "
                "(module first imported after the tripwire was installed, or the "
                "install-time wrap missed this binding)"
            )
    return violations


def _runtime_os_trap(name: str) -> Any:
    """Build a trip-raising stand-in for an ``os``/``asyncio`` spawn entry point."""

    def _trap(*args: Any, **kwargs: Any) -> None:
        _record_trip(name)
        raise RealDesktopEffectInOfflineSuite(
            f"{name}(...) attempted in the OFFLINE suite (CUMCP_RUN_E2E != '1'), "
            f"test node: {_CURRENT_NODE}. Runtime-only escape hatch for the static "
            "scanner (VAL-WP-H-02 NIT) — use an allowed subprocess form instead."
        )

    _trap._cortex_tripwire_trap = True  # type: ignore[attr-defined]
    return _trap


def install_tripwire(monkeypatch: pytest.MonkeyPatch, node_id: str) -> None:
    """Arm every real-desktop EFFECT entry point for ONE test (undo by monkeypatch)."""
    global _CURRENT_NODE
    _CURRENT_NODE = node_id
    clear_trip_log()

    # WP-R2a: EVERY module-level effect-DLL binding of EVERY loaded
    # computer_use_mcp.* module is wrapped (previously only
    # backend_module._user32); on the unsplit tree this wraps exactly the
    # bindings it wrapped before, plus the passthrough kernel32/ole32/oleaut32.
    _WRAPPED_THIS_INSTALL.clear()  # F13: the registry is per-install
    wrap_loaded_effect_bindings(monkeypatch, node_id)

    monkeypatch.setattr(subprocess, "Popen", _TripwirePopen)
    monkeypatch.setattr(os, "startfile", _startfile_trap, raising=False)
    # WP-H NIT (VAL-WP-H-02): the static scanner cannot see getattr/partial shapes,
    # and os.spawn*/os.system/os.popen/asyncio subprocesses were previously caught
    # by NEITHER the scanner NOR a runtime trap. Trap them at runtime via
    # monkeypatch (restored after each test, unlike a bare setattr).
    for name in sorted(n for n in dir(os) if n.startswith("spawn") or n.startswith("exec")):
        # F12 (VAL-WP8-01): os.exec* replaces THIS process with another program —
        # as much a real-desktop effect as a spawn; os.posix_spawn* likewise when
        # present. All runtime-trapped via monkeypatch (restored per test).
        if name in ("execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp",
                    "execvpe", "posix_spawn", "posix_spawnp",
                    "spawnl", "spawnle", "spawnlp", "spawnlpe",
                    "spawnv", "spawnve", "spawnvp", "spawnvpe"):
            monkeypatch.setattr(os, name, _runtime_os_trap(f"os.{name}"), raising=False)
    for name in ("system", "popen"):
        monkeypatch.setattr(os, name, _runtime_os_trap(f"os.{name}"), raising=False)
    import asyncio

    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        if hasattr(asyncio, name):
            monkeypatch.setattr(
                asyncio, name, _runtime_os_trap(f"asyncio.{name}"), raising=False
            )

    pyautogui = sys.modules.get("pyautogui")
    if pyautogui is not None:
        _patch_pyautogui_effects(monkeypatch, pyautogui)
    else:
        # DELTA 1 M3: do NOT import pyautogui here. Trap it at first import via a
        # meta-path finder (which replays the backend's DPI ordering first).
        monkeypatch.setattr(sys, "meta_path", [_PyautoguiTripwireFinder(monkeypatch), *sys.meta_path])
