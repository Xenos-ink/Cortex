"""Non-Windows importability of the desktop package (VAL-WP-R2a-1 F1, D8).

At HEAD ``backend.py`` imported cleanly off Windows: the Windows-only ctypes
names were read only inside ``IS_WINDOWS``-guarded bodies. The split moved
``from .win32 import ...`` bindings to module top level, so win32's ``else:``
branch must define EVERY name consumers import (None off Windows — decision D8).

This test fresh-execs ``win32`` with ``platform.system`` patched to a non-Windows
value (the exact thing win32 reads to decide), registers each fresh module in
``sys.modules`` ONLY through ``monkeypatch.setitem`` (restored on teardown), and
then fresh-execs every desktop module (including ``backend``/``fake_backend``).
No DLL can load: with ``IS_WINDOWS`` false no ``ctypes.windll`` attribute is
touched, and the modules only ever bind/import None placeholders. The real
package modules are never replaced after the test.

Self-check (by reasoning, no probe file): removing any ONE ``NAME = None``
placeholder from win32's ``else`` branch makes win32 itself import fine, but the
dependent module's ``from .win32 import NAME`` raises ImportError during its
fresh exec — ``test_desktop_modules_import_without_windows`` fails on exactly
that module. Removing the whole block fails on the first dependent.
"""

from __future__ import annotations

import platform
import sys
import types
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "src" / "computer_use_mcp"
DESKTOP = PKG / "desktop"

#: win32 names bound ONLY inside ``if IS_WINDOWS:`` and imported by consumers —
#: all of them must exist (None) in the non-Windows else branch (D8).
D8_PLACEHOLDERS = (
    "MONITORINFOEXW",
    "_MONITORENUMPROC",
    "_MOUSEINPUT",
    "_KEYBDINPUT",
    "_INPUT_UNION",
    "_INPUT",
    "_INPUT_MOUSE",
    "_INPUT_KEYBOARD",
    "_VARIANT",
    "_GUID",
    "_GUITHREADINFO",
    "_GW_CHILD",
    "_GW_HWNDNEXT",
    "_WM_GETTEXT",
    "_SMTO_ABORTIFHUNG",
    "_SENDMESSAGE_TIMEOUT_MS",
)

#: Fresh-exec order = dependency order (bottom cross-imports included).
#: ``windows`` runs BEFORE ``text_readers`` so text_readers' bottom
#: ``from .windows import ...`` resolves to the FRESH windows module (the
#: pairing is exercised); fresh windows' own bottom ``from .text_readers
#: import ...`` resolves to the real text_readers (preloaded — precondition
#: asserted in the fixture).
MODULES = (
    "win32",
    "errors",
    "coordinates",
    "motion",
    "launch",
    "clipboard",
    "windows",
    "text_readers",
    "input_engine",
    "capture",
    "backend",
    "fake_backend",
)


class _NonWindowsPlatform:
    @staticmethod
    def system() -> str:
        return "Linux"


def _fresh_exec(monkeypatch: pytest.MonkeyPatch, dotted: str) -> types.ModuleType:
    mod = types.ModuleType(f"computer_use_mcp.desktop.{dotted}")
    mod.__file__ = str(DESKTOP / f"{dotted}.py")
    mod.__package__ = "computer_use_mcp.desktop"
    monkeypatch.setitem(sys.modules, mod.__name__, mod)
    code = compile(mod.__file__ and Path(mod.__file__).read_text(encoding="utf-8"), mod.__file__, "exec")
    exec(code, mod.__dict__)  # noqa: S102 - the point of the test
    return mod


@pytest.fixture()
def non_windows_desktop(monkeypatch: pytest.MonkeyPatch) -> dict[str, types.ModuleType]:
    """Fresh-exec the whole desktop package as if platform.system() != 'Windows'."""
    monkeypatch.setitem(sys.modules, "platform", _NonWindowsPlatform)
    # the windows <-> text_readers bottom cross-import: fresh windows' bottom
    # `from .text_readers import ...` must not import text_readers against the
    # fresh win32 (which would leave a non-Windows module in sys.modules after
    # teardown) — both peers must be loaded for real BEFORE the fresh execs.
    for peer in ("windows", "text_readers"):
        assert f"computer_use_mcp.desktop.{peer}" in sys.modules, peer
    loaded: dict[str, types.ModuleType] = {}
    for name in MODULES:
        loaded[name] = _fresh_exec(monkeypatch, name)
    return loaded


def test_win32_binds_every_d8_placeholder_as_none_off_windows(
    non_windows_desktop: dict[str, types.ModuleType],
) -> None:
    win32 = non_windows_desktop["win32"]
    assert win32.IS_WINDOWS is False
    for name in D8_PLACEHOLDERS:
        assert getattr(win32, name, "MISSING") is None, name
    assert win32._user32 is None and win32._kernel32 is None
    assert win32._shcore is None and win32._ole32 is None and win32._oleaut32 is None


def test_desktop_modules_import_without_windows(
    non_windows_desktop: dict[str, types.ModuleType],
) -> None:
    for name in MODULES:
        assert name in non_windows_desktop, f"fresh-exec of {name} failed"
    # the placeholders are what the consumers actually bound
    assert non_windows_desktop["windows"].MONITORINFOEXW is None
    assert non_windows_desktop["input_engine"]._INPUT is None
    assert non_windows_desktop["text_readers"]._VARIANT is None
    assert non_windows_desktop["backend"].ComputerBackend is not None
    assert non_windows_desktop["fake_backend"].FakeComputerBackend is not None


def test_real_modules_are_restored_after_the_fresh_execs() -> None:
    """Runs AFTER the fixture tests (file order): monkeypatch teardown has restored
    sys.modules, so this import resolves to the REAL package module, not a fresh one."""
    import computer_use_mcp.desktop.win32 as real  # noqa: PLC0415 - the point

    # platform-relative: this file must also pass on a non-Windows host
    assert real.IS_WINDOWS is (platform.system() == "Windows")
    assert real.__file__ == str(DESKTOP / "win32.py")
    assert sys.modules["computer_use_mcp.desktop.win32"] is real
