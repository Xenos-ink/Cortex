"""Free-name gate: every runtime-referenced global name must be bound (F3).

For every module under ``src/computer_use_mcp``: every name referenced at
RUNTIME must be bound in its scope, imported, or a builtin — otherwise it is a
swallowed-``NameError`` hazard (the ``_VARIANT`` precedent: text_readers'
``_read_property`` raised inside a broad ``except`` and only one test noticed).
Scope model: function/lambda scopes bind function-wide (params + every Store in
the body, any order — a pre-assignment load is UnboundLocal, not NameError);
comprehensions bind their targets in their own scope; class bodies canNOT close
over function locals; nested functions see enclosing locals as closures.
Names used ONLY in annotations are excluded: every module carries ``from
__future__ import annotations``, so annotations are lazy (F2's TYPE_CHECKING
concern). Decorators and default values execute at definition time and ARE
checked in the enclosing scope.
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "src" / "computer_use_mcp"

#: Free names reviewed at HEAD (pre-split — not caused by WP-R2a). Equality-
#: checked: a NEW free name fails; a pruned entry must be removed here.
REVIEWED_FREE_NAMES: dict[tuple[str, str], str] = {}

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _binds(node: ast.AST, bound: set[str]) -> None:
    """Collect names this scope binds (nested scopes bind only their own name)."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        bound.add(node.name)
        return
    if isinstance(node, ast.Lambda):
        return
    if isinstance(node, ast.Name):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        return
    if isinstance(node, ast.Import):
        for alias in node.names:
            bound.add((alias.asname or alias.name).split(".")[0])
        return
    if isinstance(node, ast.ImportFrom):
        for alias in node.names:
            bound.add(alias.asname or alias.name)
        return
    if isinstance(node, ast.Global):
        bound.update(node.names)  # resolves at module level; lenient here
        return
    if isinstance(node, ast.ExceptHandler):
        if node.name:
            bound.add(node.name)
        for child in ast.iter_child_nodes(node):
            _binds(child, bound)
        return
    for child in ast.iter_child_nodes(node):
        _binds(child, bound)


def _walk(node: ast.AST, bound: set[str], closure: frozenset[str], free: set[str]) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for dec in node.decorator_list:
            _walk(dec, bound, closure, free)
        for default in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
            _walk(default, bound, closure, free)
        inner = set(closure)  # enclosing locals become closures for the body
        for arg in list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs):
            inner.add(arg.arg)
        if node.args.vararg:
            inner.add(node.args.vararg.arg)
        if node.args.kwarg:
            inner.add(node.args.kwarg.arg)
        _binds(ast.Module(body=node.body, type_ignores=[]), inner)
        visible = frozenset(bound | closure)  # enclosing locals become closures
        for stmt in node.body:
            _walk(stmt, inner, visible, free)
        return
    if isinstance(node, ast.Lambda):
        for default in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
            _walk(default, bound, closure, free)
        inner = set()
        for arg in list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs):
            inner.add(arg.arg)
        if node.args.vararg:
            inner.add(node.args.vararg.arg)
        if node.args.kwarg:
            inner.add(node.args.kwarg.arg)
        _binds(node.body, inner)
        visible = frozenset(bound | closure)
        _walk(node.body, inner, visible, free)
        return
    if isinstance(node, ast.ClassDef):
        for dec in node.decorator_list:
            _walk(dec, bound, closure, free)
        inner: set[str] = set()  # class bodies canNOT close over function locals
        _binds(ast.Module(body=node.body, type_ignores=[]), inner)
        for stmt in node.body:
            _walk(stmt, inner, frozenset(), free)
        return
    if isinstance(node, ast.comprehension):
        comp: set[str] = set()
        for child in ast.walk(node.target):
            if isinstance(child, ast.Name):
                comp.add(child.id)
        _walk(node.iter, bound, closure, free)  # the iterable runs in THIS scope
        for stmt in [node.target] + node.ifs:
            _walk(stmt, comp, frozenset(bound | closure), free)
        return
    if isinstance(node, ast.Name):
        if isinstance(node.ctx, ast.Load) and node.id not in bound and node.id not in closure:
            free.add(node.id)
        return
    if isinstance(node, ast.AnnAssign):
        _walk(node.target, bound, closure, free)
        if node.value is not None:
            _walk(node.value, bound, closure, free)
        return  # annotations are lazy under __future__ annotations
    if isinstance(node, ast.arg):
        return  # arg annotations are lazy
    for child in ast.iter_child_nodes(node):
        _walk(child, bound, closure, free)


def free_names(source: str) -> set[str]:
    """Runtime-referenced module-global names with no binding/import/builtin."""
    tree = ast.parse(source)
    module_bound: set[str] = set()
    _binds(ast.Module(body=tree.body, type_ignores=[]), module_bound)
    allowed = module_bound | set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__class__"}
    free: set[str] = set()
    top_visible = frozenset(module_bound)  # module globals resolve at call time
    for stmt in tree.body:
        _walk(stmt, module_bound, top_visible, free)
    return free - allowed


def test_every_runtime_global_name_is_bound_in_every_src_module() -> None:
    violations: dict[str, set[str]] = {}
    reviewed: set[tuple[str, str]] = set()
    for path in sorted(PKG.rglob("*.py")):
        relpath = path.relative_to(PKG).as_posix()
        found = free_names(path.read_text(encoding="utf-8"))
        for name in sorted(found):
            if (relpath, name) in REVIEWED_FREE_NAMES:
                reviewed.add((relpath, name))
            else:
                violations.setdefault(relpath, set()).add(name)
    assert not violations, (
        "UNBOUND RUNTIME NAMES: a module references a global it never binds, imports, "
        "or gets as a builtin — a swallowed NameError waiting for a caller with no "
        f"test witness (annotations excluded: lazy under __future__ annotations): {violations}"
    )
    assert reviewed == set(REVIEWED_FREE_NAMES), (
        f"reviewed free-name list is stale — prune: {sorted(set(REVIEWED_FREE_NAMES) - reviewed)}"
    )


def test_selftest_unbound_runtime_name_fails() -> None:
    assert free_names("def f() -> None:\n    return _nowhere()\n") == {"_nowhere"}


def test_selftest_module_level_binding_passes() -> None:
    assert free_names("_x = 1\n_y = _x\n") == set()


def test_selftest_imported_name_passes() -> None:
    assert free_names("import os\n\n\ndef f() -> None:\n    return os.getcwd()\n") == set()


def test_selftest_annotation_only_reference_passes() -> None:
    module = (
        "from __future__ import annotations\n\n\n"
        "def f() -> Missing:\n"
        "    raise NotImplementedError\n"
    )
    assert free_names(module) == set()


def test_selftest_local_binding_does_not_satisfy_module_use() -> None:
    module = "def f() -> None:\n    local = 1\n    return local\n\n\nother = missing_name\n"
    assert free_names(module) == {"missing_name"}


def test_selftest_local_load_is_not_a_module_reference() -> None:
    assert free_names("def f() -> None:\n    local = 1\n    return local\n") == set()


def test_selftest_comprehension_target_binds() -> None:
    module = "def f(items):\n    return [key for key, value in items]\n"
    assert free_names(module) == set()


def test_selftest_comprehension_still_catches_a_free_name() -> None:
    module = "def f(items):\n    return [key for key, value in items if _pred(key)]\n"
    assert free_names(module) == {"_pred"}


def test_selftest_decorator_and_default_are_checked() -> None:
    module = "def deco(fn):\n    return fn\n\n\n@deco\ndef g(_x=missing_default):\n    return 0\n"
    assert free_names(module) == {"missing_default"}


def test_selftest_class_body_binding_is_not_module_binding() -> None:
    module = "class C:\n    attr = 1\n\n\nref = attr\n"
    assert free_names(module) == {"attr"}


def test_selftest_closure_load_is_not_free() -> None:
    module = "def outer():\n    x = 1\n    def inner():\n        return x\n    return inner\n"
    assert free_names(module) == set()


def test_selftest_class_body_cannot_see_function_locals() -> None:
    module = "def outer():\n    x = 1\n    class C:\n        y = x\n    return C\n"
    assert free_names(module) == {"x"}
