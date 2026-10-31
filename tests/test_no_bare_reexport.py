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

#: D1 launch entries (WP-R2b plan §2/§9.3): package __init__ files allowed to
#: carry EXACTLY one launch shim — ``def main(): from .app import main as
#: _main; return _main()`` — plus a docstring, nothing else. Reviewed set:
#: equality-checked by the self-tests. Inert on the UNsplit tree (server/ does
#: not exist yet); the shim becomes live at the step-1 move.
LAUNCH_ENTRY_INITS = {"server/__init__.py"}

#: The exact D1 shim source (for the self-tests).
LAUNCH_SHIM_SOURCE = (
    '"""Console-script launch shim (D1)."""\n'
    "\n"
    "def main():\n"
    "    from .app import main as _main\n"
    "    return _main()\n"
)


def _is_launch_shim(node: ast.stmt) -> bool:
    """True for EXACTLY the D1 shim shape: a bare zero-arg ``def main`` whose
    body is the relative app re-import plus ``return _main()`` — nothing else."""
    if not (isinstance(node, ast.FunctionDef) and node.name == "main"):
        return False
    if node.decorator_list or node.returns is not None or getattr(node, "type_params", None):
        return False
    a = node.args
    if a.posonlyargs or a.args or a.kwonlyargs or a.vararg or a.kwarg \
            or a.defaults or a.kw_defaults:
        return False
    if len(node.body) != 2:
        return False
    imp, ret = node.body
    if not (isinstance(imp, ast.ImportFrom) and imp.level == 1 and imp.module == "app"
            and len(imp.names) == 1 and imp.names[0].name == "main"
            and imp.names[0].asname == "_main"):
        return False
    if not (isinstance(ret, ast.Return) and isinstance(ret.value, ast.Call)
            and len(ret.value.args) == 0 and not ret.value.keywords):
        return False
    func = ret.value.func
    return isinstance(func, ast.Name) and isinstance(func.ctx, ast.Load) and func.id == "_main"


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


def init_violations_source(source: str, relpath: str, is_root: bool) -> list[str]:
    """Top-level statements an __init__ may not carry (docstring/__version__ only).

    A package listed in ``LAUNCH_ENTRY_INITS`` may additionally carry EXACTLY
    one launch shim (``_is_launch_shim``)."""
    tree = ast.parse(source)
    launch_entry = relpath in LAUNCH_ENTRY_INITS
    bad: list[str] = []
    shim_seen = False
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue  # module docstring
        if is_root and isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            continue
        if launch_entry and not shim_seen and _is_launch_shim(node):
            shim_seen = True
            continue
        bad.append(type(node).__name__)
    return bad


def init_violations(path: Path, is_root: bool) -> list[str]:
    try:
        relpath = path.relative_to(PKG).as_posix()
    except ValueError:
        relpath = ""  # outside the package: never a launch entry
    return init_violations_source(path.read_text(encoding="utf-8"), relpath, is_root)


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


def test_selftest_launch_shim_passes_for_the_reviewed_entry() -> None:
    assert init_violations_source(LAUNCH_SHIM_SOURCE, "server/__init__.py", is_root=False) == []


def test_selftest_launch_exception_is_inert_on_the_unsplit_tree() -> None:
    """server/ does not exist yet, so NO existing __init__ is a launch entry —
    the mechanism must not loosen any current package guard."""
    assert not (PKG / "server").exists()
    assert not (PKG / "server" / "__init__.py").exists()
    for path in sorted(PKG.rglob("__init__.py")):
        assert path.relative_to(PKG).as_posix() not in LAUNCH_ENTRY_INITS, path
        is_root = path == PKG / "__init__.py"
        assert init_violations(path, is_root) == [], path


def test_selftest_any_other_statement_in_a_launch_init_fails() -> None:
    assert init_violations_source(
        LAUNCH_SHIM_SOURCE + "\nX = 1\n", "server/__init__.py", is_root=False
    ) == ["Assign"]
    assert init_violations_source(
        LAUNCH_SHIM_SOURCE + "from .app import main\n", "server/__init__.py", is_root=False
    ) == ["ImportFrom"]
    assert init_violations_source(
        LAUNCH_SHIM_SOURCE
        + "\ndef main():\n    from .app import main as _main\n    return _main()\n",
        "server/__init__.py", is_root=False,
    ) == ["FunctionDef"]  # a second shim-shaped def


def test_selftest_shim_shape_deviations_fail() -> None:
    wrong_name = LAUNCH_SHIM_SOURCE.replace("def main():", "def run():")
    assert init_violations_source(wrong_name, "server/__init__.py", is_root=False) == ["FunctionDef"]
    wrong_arg = LAUNCH_SHIM_SOURCE.replace("def main():", "def main(argv=None):")
    assert init_violations_source(wrong_arg, "server/__init__.py", is_root=False) == ["FunctionDef"]
    absolute = LAUNCH_SHIM_SOURCE.replace(
        "from .app import main as _main",
        "from computer_use_mcp.server.app import main as _main",
    )
    assert init_violations_source(absolute, "server/__init__.py", is_root=False) == ["FunctionDef"]


def test_selftest_shim_shape_in_any_other_package_fails() -> None:
    assert init_violations_source(LAUNCH_SHIM_SOURCE, "desktop/__init__.py", is_root=False) == [
        "FunctionDef"
    ]


@pytest.mark.parametrize(
    ("shim", "label"),
    [
        ('"""D."""\n\n@deco\ndef main():\n    from .app import main as _main\n    return _main()\n', "decorated"),
        ('"""D."""\n\nasync def main():\n    from .app import main as _main\n    return _main()\n', "async"),
        ('"""D."""\n\ndef main() -> int:\n    from .app import main as _main\n    return _main()\n', "annotated"),
        ('"""D."""\n\ndef main():\n    from ..app import main as _main\n    return _main()\n', "level2"),
        ('"""D."""\n\ndef main():\n    from .app import *\n    return main()\n', "star"),
    ],
)
def test_selftest_shim_deviation_forms_fail(shim: str, label: str) -> None:
    node_type = type(ast.parse(shim).body[-1]).__name__
    assert init_violations_source(shim, "server/__init__.py", is_root=False) == [node_type], label


def test_selftest_shim_with_type_params_is_rejected() -> None:
    """3.12 generic-function shape (pinned via a synthesized node: the
    ``def main[T]()`` source does not parse on Python 3.11)."""
    node = ast.parse(LAUNCH_SHIM_SOURCE).body[-1]
    assert _is_launch_shim(node)
    node.type_params = [ast.Name(id="T", ctx=ast.Load())]
    assert not _is_launch_shim(node)


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
