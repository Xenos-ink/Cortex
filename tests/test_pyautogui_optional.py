"""WP-M (ORVEX-2026-109) Addendum 4: pyautogui is optional, imported lazily.

Hermetic: blocks ``import pyautogui`` via ``sys.modules["pyautogui"] = None``
(monkeypatch) and proves the default SendInput engine builds and dispatches
through the fake user32, while selecting the pyautogui engine fails closed
with the typed actionable ValueError. No real SendInput, no desktop input.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import computer_use_mcp.desktop.backend as backend_module
import computer_use_mcp.desktop.input_engine as input_engine_module
from computer_use_mcp.desktop.backend import LocalComputerBackend
from computer_use_mcp.desktop.input_engine import SendInputEngine

EXPECTED_ERROR = (
    "the pyautogui input engine (CORTEX_INPUT_BACKEND=pyautogui "
    "or input_engine='pyautogui') needs the optional extra: "
    'python -m pip install -e ".[pyautogui]" (from a clone) or '
    'pip install "computer-use-mcp[pyautogui] @ '
    'git+https://github.com/Xenos-ink/Cortex.git" (from git); '
    "with uv: uv sync --extra pyautogui"
)


def _bare_backend() -> LocalComputerBackend:
    """A LocalComputerBackend shell with no real __init__ (no mss, no UIA)."""
    backend = object.__new__(LocalComputerBackend)
    backend._pyautogui = None
    return backend


class _FakeUser32:
    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []

    def SendInput(self, count: int, events: Any, size: int) -> int:
        self.batches.append(list(events[:count]))
        return count

    def GetSystemMetrics(self, index: int) -> int:
        return {0: 0, 1: 0, 76: 0, 77: 0, 78: 1920, 79: 1080}.get(index, 0)


def test_default_engine_builds_and_dispatches_without_pyautogui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "pyautogui", None)  # blocks the import
    monkeypatch.delenv("CORTEX_INPUT_BACKEND", raising=False)
    backend = _bare_backend()
    engine = backend._build_input_engine(None, None, None, None)
    assert isinstance(engine, SendInputEngine)
    assert backend._pyautogui is None  # the default path never binds pyautogui
    fake = _FakeUser32()
    monkeypatch.setattr(input_engine_module, "_user32", fake)
    engine.move(100, 200)
    assert len(fake.batches) == 1  # dispatched through the fake, no real input


def test_selecting_pyautogui_without_the_extra_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "pyautogui", None)  # blocks the import
    backend = _bare_backend()
    with pytest.raises(ValueError) as excinfo:
        backend._build_input_engine("pyautogui", None, None, None)
    assert str(excinfo.value) == EXPECTED_ERROR
    assert backend._pyautogui is None  # fail closed: no engine, no binding


def test_unknown_engine_error_unchanged() -> None:
    backend = _bare_backend()
    with pytest.raises(ValueError, match="unknown input engine"):
        backend._build_input_engine("telepathy", None, None, None)


# --- m8 (WP-L/M DELTA 1): "never attempted", not merely "works when blocked" ----

SRC = backend_module.__file__  # .../src/computer_use_mcp/desktop/backend.py
REPO_SRC = Path(SRC).parent.parent.parent  # .../src


#: DELTA 3 n-d: the whole pyautogui family, matched on the top-level module name.
_FAMILY = (
    "pyautogui",
    "mouseinfo",
    "pymsgbox",
    "pyscreeze",
    "pygetwindow",
    "pytweening",
    "pyperclip",
    "pyrect",
)


def _is_family(modname: str) -> bool:
    return modname.split(".")[0] in _FAMILY


def _pyautogui_import_sites(src_dir: Path) -> list[tuple[Path, int, str]]:
    """(file, lineno, description) of every family import in src/.

    Covers static imports AND dynamic string calls
    (``importlib.import_module("<name>")`` / ``__import__("<name>")``).
    """
    sites: list[tuple[Path, int, str]] = []
    for path in src_dir.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_family(alias.name):
                        sites.append((path, node.lineno, f"import {alias.name}"))
            elif isinstance(node, ast.ImportFrom):
                if node.module and _is_family(node.module):
                    sites.append((path, node.lineno, f"from {node.module} import …"))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "importlib"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and _is_family(node.args[0].value)
            ):
                sites.append(
                    (path, node.lineno, f"importlib.import_module({node.args[0].value!r})")
                )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "__import__"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and _is_family(node.args[0].value)
            ):
                sites.append((path, node.lineno, f"__import__({node.args[0].value!r})"))
    return sites


def test_ast_only_import_site_is_inside_build_input_engine() -> None:
    sites = _pyautogui_import_sites(REPO_SRC)
    assert sites, "the pyautogui branch import must still exist"
    tree = ast.parse(Path(SRC).read_text(encoding="utf-8"))
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_build_input_engine"
    )
    for path, lineno, what in sites:
        assert path == Path(SRC), f"family import outside backend.py: {path} ({what})"
        assert fn.lineno <= lineno <= fn.end_lineno, (
            f"family import at {path}:{lineno} ({what}) is outside _build_input_engine"
        )


class _RecordingFinder:
    """sys.meta_path finder that records every family module lookup."""

    def __init__(self) -> None:
        self.lookups: list[str] = []

    def find_spec(self, fullname: str, path: object = None, target: object = None) -> object:
        if _is_family(fullname):
            self.lookups.append(fullname)
        return None


def test_default_engine_never_even_looks_up_pyautogui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CORTEX_INPUT_BACKEND", raising=False)
    # DELTA 2: a cached family module would never reach the finder — drop it so
    # a real lookup would be recorded. monkeypatch puts any removed entry back.
    for name in [mod for mod in list(sys.modules) if _is_family(mod)]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    finder = _RecordingFinder()
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])
    backend = _bare_backend()
    backend._build_input_engine(None, None, None, None)
    assert finder.lookups == []


# --- DELTA 4 (m-2): negative self-test for the AST import-site scanner ----------


def test_ast_scanner_reports_dynamic_imports(tmp_path: Any) -> None:
    (tmp_path / "a_mod.py").write_text(
        'import importlib\n\n\nimportlib.import_module("pymsgbox")\n', encoding="utf-8"
    )
    (tmp_path / "b_mod.py").write_text('__import__("pyrect.sub")\n', encoding="utf-8")
    (tmp_path / "c_mod.py").write_text("from pyscreeze import x\n", encoding="utf-8")
    (tmp_path / "clean_mod.py").write_text("import json\n", encoding="utf-8")
    sites = {(path.name, what) for path, _, what in _pyautogui_import_sites(tmp_path)}
    assert ("a_mod.py", "importlib.import_module('pymsgbox')") in sites
    assert ("b_mod.py", "__import__('pyrect.sub')") in sites
    assert ("c_mod.py", "from pyscreeze import …") in sites
    assert all(name != "clean_mod.py" for name, _ in sites)


# --- DELTA 4 (MAJOR-1 fix): clean CHILD process via the sanctioned nested run ----
# The in-process purge/re-import re-ran desktop/win32.py, which re-set argtypes
# on PROCESS-SHARED ctypes function objects to pointers to NEW struct classes —
# un-undoable by restoring sys.modules. Replaced by the suite's sanctioned
# pattern (test_wp_h_hermeticity.py test_swallowed_trip_fails_the_test_at_
# teardown): a nested `python -m pytest` run over files written under tmp_path.
# DELTA 5 (MAJOR-1 of VAL-WP-L-04): the recording finder now lives at the very
# TOP of the child conftest — BEFORE the sys.path inserts and the
# desktop_tripwire import (which imports computer_use_mcp.desktop.backend and
# .windows) — so the WHOLE import chain is covered, with a conftest-time
# ordering assertion proving it.

_CHILD_CONFTEST = '''\
import sys

# FIRST STATEMENTS: the recorder must exist before ANY computer_use_mcp import
# (desktop_tripwire, imported below, pulls in desktop.backend and .windows).
FAMILY = (
    "pyautogui",
    "mouseinfo",
    "pymsgbox",
    "pyscreeze",
    "pygetwindow",
    "pytweening",
    "pyperclip",
    "pyrect",
)


class _RecordingFinder:
    _cortex_family_recorder = True

    def __init__(self):
        self.lookups = []

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in FAMILY:
            self.lookups.append(fullname)
        return None


_recorder = _RecordingFinder()
sys.meta_path.insert(0, _recorder)

_preloaded = [
    m
    for m in sys.modules
    if m == "computer_use_mcp"
    or m.startswith("computer_use_mcp.")
    or m.split(".")[0] in FAMILY
]
assert _preloaded == [], "loaded before the family recorder: " + repr(_preloaded)

sys.path.insert(0, r"{tests_dir}")
sys.path.insert(0, r"{source_root}")

import pytest

from desktop_tripwire import assert_no_unexpected_trips, install_tripwire


@pytest.fixture(autouse=True)
def _trip(request, monkeypatch):
    install_tripwire(monkeypatch, request.node.nodeid)
    yield
    assert_no_unexpected_trips(request.node.nodeid)
'''

_CHILD_TESTS = '''\
import importlib.util
import sys

FAMILY = (
    "pyautogui",
    "mouseinfo",
    "pymsgbox",
    "pyscreeze",
    "pygetwindow",
    "pytweening",
    "pyperclip",
    "pyrect",
)


def _family_recorder():
    for finder in sys.meta_path:
        if getattr(finder, "_cortex_family_recorder", False):
            return finder
    raise AssertionError("family recorder finder not found on sys.meta_path")


def test_importing_server_and_cli_never_looks_up_the_family():
    import computer_use_mcp.cli  # noqa: F401
    import computer_use_mcp.server  # noqa: F401

    loaded = [name for name in sys.modules if name.split(".")[0] in FAMILY]
    assert loaded == [], "family modules imported: " + repr(loaded)
    assert _family_recorder().lookups == [], (
        "family lookups: " + repr(_family_recorder().lookups)
    )


def test_positive_control_find_spec_is_recorded():
    """The recording finder must SEE a family lookup — without this control
    the assertion above could pass vacuously. n-1: scoped to this test via a
    length bookmark, so it stays correct in any execution order."""
    recorder = _family_recorder()
    before = len(recorder.lookups)
    importlib.util.find_spec("pymsgbox")
    assert "pymsgbox" in recorder.lookups[before:]
    del recorder.lookups[before:]
'''


def test_importing_server_and_cli_never_looks_up_the_family_child(
    tmp_path: Any,
) -> None:
    """DELTA 4/5: the WHOLE computer_use_mcp import chain is proven family-free
    in a CLEAN child process — including the conftest-time import of
    desktop_tripwire (which imports computer_use_mcp.desktop.backend and
    .windows): the child conftest installs the recording finder as its very
    first statements and asserts nothing from computer_use_mcp or the family
    is in sys.modules at that point, so the finder is live for the entire
    chain. A positive control proves the finder sees family lookups."""
    tests_dir = Path(__file__).parent
    source_root = tests_dir.parent / "src"
    (tmp_path / "conftest.py").write_text(
        _CHILD_CONFTEST.format(tests_dir=tests_dir, source_root=source_root),
        encoding="utf-8",
    )
    (tmp_path / "test_family_free_import.py").write_text(
        _CHILD_TESTS, encoding="utf-8"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(source_root), str(tests_dir)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, (
        f"child run failed:\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert "2 passed" in completed.stdout, completed.stdout
