"""No-bare-re-export guard (WP-R2a plan §4(3), decision D7 extension).

For EVERY relative ``from .x import name`` under ``src/computer_use_mcp`` the
importing module's own code must READ the name (any ``ast.Name`` load anywhere in
the module — module body, function/class bodies, annotations — is a read; the
import statements themselves are not). A pure pass-through import (a compatibility
re-export shim) fails. Subpackage ``__init__.py`` files stay EMPTY apart from an
optional docstring; the root ``__init__.py`` may carry only its docstring and the
``__version__`` assignment.

Self-tests prove the guard has teeth: a synthetic pass-through module fails, the
same import consumed by a function body passes, and an ``__init__`` with logic
fails.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "src" / "computer_use_mcp"

#: Pre-existing bare re-ports on the UNsplit tree, reviewed and recorded for WP10a
#: (which deletes the dead bindings together with their import lines). Equality-
#: checked: any NEW bare re-export fails, a pruned entry must be removed here.
REVIEWED_BARE_REEXPORTS: set[tuple[str, str]] = {
    # focus_guard re-imports the interference event-name constants but only ever
    # names them in docstrings; nothing imports them from focus_guard (dead).
    ("desktop/focus_guard.py", "FOCUS_DRIFTED"),
    ("desktop/focus_guard.py", "FOCUS_TAKEN_BY"),
    ("desktop/focus_guard.py", "MODAL_DIALOG"),
    ("desktop/focus_guard.py", "STUCK_MODIFIER"),
    ("desktop/focus_guard.py", "TARGET_GONE"),
    # server imports the planning failure-reason set without reading it (dead).
    ("server.py", "PLAN_FAILURE_REASONS"),
}


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level >= 1:
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _read_names(tree: ast.Module) -> set[str]:
    reads: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            reads.add(node.id)
    return reads


def reexport_violations(source: str) -> list[str]:
    """Bare re-export names in one module's source (empty when clean)."""
    tree = ast.parse(source)
    return sorted(_imported_names(tree) - _read_names(tree))


def init_violations(path: Path, is_root: bool) -> list[str]:
    """Top-level statements an __init__ may not carry (docstring/__version__ only)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue  # module docstring
        if is_root and isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            continue
        bad.append(type(node).__name__)
    return bad


def test_every_relative_import_is_read_by_its_own_module() -> None:
    violations: dict[str, list[str]] = {}
    reviewed: set[tuple[str, str]] = set()
    for path in sorted(PKG.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        relpath = path.relative_to(PKG).as_posix()
        found = reexport_violations(path.read_text(encoding="utf-8"))
        for name in found:
            if (relpath, name) in REVIEWED_BARE_REEXPORTS:
                reviewed.add((relpath, name))
            else:
                violations.setdefault(relpath, []).append(name)
    assert not violations, (
        "BARE RE-EXPORTS: relative imports no code in the importing module reads "
        f"(compatibility shims are forbidden): {violations}"
    )
    assert reviewed == REVIEWED_BARE_REEXPORTS, (
        "reviewed re-export list is stale — prune entries whose site disappeared: "
        f"{sorted(REVIEWED_BARE_REEXPORTS - reviewed)}"
    )


def test_package_init_files_stay_empty() -> None:
    violations: dict[str, list[str]] = {}
    for path in sorted(PKG.rglob("__init__.py")):
        is_root = path == PKG / "__init__.py"
        found = init_violations(path, is_root)
        if found:
            violations[path.relative_to(PKG).as_posix()] = found
    assert not violations, f"__init__ files must stay empty (docstring/__version__ only): {violations}"


def test_selftest_pass_through_import_fails() -> None:
    assert reexport_violations("from .errors import BackendError\n") == ["BackendError"]


def test_selftest_consumed_import_passes() -> None:
    module = (
        "from .errors import BackendError\n"
        "def f() -> None:\n"
        "    raise BackendError('x')\n"
    )
    assert reexport_violations(module) == []


def test_selftest_annotation_read_counts() -> None:
    module = "from __future__ import annotations\nfrom .backend import ComputerBackend\nX = ComputerBackend | None\n"
    assert reexport_violations(module) == []


def test_selftest_init_with_logic_fails() -> None:
    init = Path(PKG.parent / "_selftest_pkg_init.py")
    init.write_text("from .backend import ComputerBackend\n", encoding="utf-8")
    try:
        assert init_violations(init, is_root=False) == ["ImportFrom"]
    finally:
        init.unlink()


def test_selftest_root_version_assignment_is_allowed() -> None:
    init = Path(PKG.parent / "_selftest_root_init.py")
    init.write_text('"""Doc."""\n\n__version__ = "1.2.3"\n', encoding="utf-8")
    try:
        assert init_violations(init, is_root=True) == []
    finally:
        init.unlink()


def test_selftest_absolute_imports_are_out_of_scope() -> None:
    assert reexport_violations("import computer_use_mcp.desktop.backend\n") == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from . import backend\n", ["backend"]),
        ("from .win32 import _user32\n\n\ndef f() -> None:\n    return _user32\n", []),
    ],
)
def test_selftest_from_package_and_same_name_forms(source: str, expected: list[str]) -> None:
    assert reexport_violations(source) == expected
