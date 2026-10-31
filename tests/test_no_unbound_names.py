"""Free-name gate: every runtime-referenced global name must be bound (F3).

For every module under ``src/computer_use_mcp``: every name referenced at
RUNTIME must be bound in its scope, imported, or a builtin — otherwise it is a
swallowed-``NameError`` hazard (the ``_VARIANT`` precedent: text_readers'
``_read_property`` raised inside a broad ``except`` and only one test noticed).
Scope model: function/lambda scopes bind function-wide (params + every
non-``global`` Store in the body, any order — a pre-assignment load is
UnboundLocal, not NameError; a ``global X`` Store binds at MODULE level, so
X is not a function local); class bodies DO see enclosing function locals
(as closures) and module globals, but the class's OWN bindings are visible
only in the class body itself — NOT inside its methods, lambdas, or
comprehension element/condition scopes (only a comprehension's FIRST
iterable sees class names, matching Python); nested functions see enclosing
locals as closures.
Names used ONLY in annotations are excluded: every module carries ``from
__future__ import annotations``, so annotations are lazy (F2's TYPE_CHECKING
concern). Decorators and default values execute at definition time and ARE
checked in the enclosing scope. Class-body bindings are NOT visible inside
that class's methods, lambdas, or comprehension element/condition scopes (a
bare class attribute there is a NameError; only a comprehension's FIRST
iterable sees class names); comprehension targets are scoped to their
comprehension; ``global X`` makes X a MODULE binding — it is not a local one.
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


def _binds(node: ast.AST, bound: set[str], glob: set[str]) -> None:
    """Collect names this scope binds (nested scopes bind only their own name).

    ``glob`` collects ``global``-declared names: their Store targets bind at
    MODULE level, not in this scope, so they are not local bindings."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        bound.add(node.name)
        return
    if isinstance(node, ast.Lambda):
        return
    if isinstance(node, ast.Name):
        if isinstance(node.ctx, (ast.Store, ast.Del)) and node.id not in glob:
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
        glob.update(node.names)
        return
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        # the iterables run in THIS scope; targets/elt/ifs live in the
        # comprehension's own scope (modelled by the _walk branch)
        for gen in node.generators:
            _binds(gen.iter, bound, glob)
        return
    if isinstance(node, ast.ExceptHandler):
        if node.name:
            bound.add(node.name)
        for child in ast.iter_child_nodes(node):
            _binds(child, bound, glob)
        return
    for child in ast.iter_child_nodes(node):
        _binds(child, bound, glob)


def _walk(
    node: ast.AST, bound: set[str], closure: frozenset[str], free: set[str],
    in_class: bool = False,
) -> None:
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
        _binds(ast.Module(body=node.body, type_ignores=[]), inner, set())
        # in a class body a def/lambda must NOT see the class's own bindings:
        visible = frozenset(closure if in_class else bound | closure)
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
        _binds(node.body, inner, set())
        visible = frozenset(closure if in_class else bound | closure)
        _walk(node.body, inner, visible, free)
        return
    if isinstance(node, ast.ClassDef):
        for dec in node.decorator_list:
            _walk(dec, bound, closure, free)
        inner: set[str] = set()  # the class's own bindings
        _binds(ast.Module(body=node.body, type_ignores=[]), inner, set())
        # the class body sees the enclosing function's locals (`bound` here) and
        # module globals as closures; its OWN bindings stay in `inner` and are
        # NOT passed down (methods/lambdas/comps cannot see them)
        class_closure = frozenset(bound | closure)
        for stmt in node.body:
            _walk(stmt, inner, class_closure, free, in_class=True)
        return
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        # in a class body the comprehension scope sees NONE of the class's own
        # names — only the FIRST iterable does (it runs in the class body)
        comp: set[str] = set() if in_class else set(bound)
        for index, gen in enumerate(node.generators):
            it_bound = bound if (in_class and index == 0) else comp
            _walk(gen.iter, it_bound, closure, free)
            for child in ast.walk(gen.target):
                if isinstance(child, ast.Name):
                    comp.add(child.id)
        for gen in node.generators:
            for cond in gen.ifs:
                _walk(cond, comp, closure, free)
        for result in ([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]):
            _walk(result, comp, closure, free)
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
        _walk(child, bound, closure, free, in_class)


def free_names(source: str) -> set[str]:
    """Runtime-referenced module-global names with no binding/import/builtin."""
    tree = ast.parse(source)
    module_bound: set[str] = set()
    _binds(ast.Module(body=tree.body, type_ignores=[]), module_bound, set())
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


def test_selftest_class_body_sees_enclosing_function_locals() -> None:
    module = "def outer():\n    x = 1\n    class C:\n        y = x\n    return C\n"
    assert free_names(module) == set()


def test_selftest_method_sees_function_locals_through_the_class() -> None:
    module = ("def outer():\n    x = 1\n    class C:\n"
              "        def m(self):\n            return x\n    return C\n")
    assert free_names(module) == set()


def test_selftest_comprehension_in_class_body_cannot_see_class_names() -> None:
    module = "class C:\n    xs = [1]\n    ys = [v for v in xs]\n    leaked = v\n"
    assert free_names(module) == {"v"}


def test_selftest_lambda_in_class_comprehension_cannot_see_class_names() -> None:
    module = "class C:\n    a = 1\n    fs = [lambda: a for _ in range(1)]\n"
    assert free_names(module) == {"a"}


def test_selftest_class_comprehension_first_iterable_sees_class_names() -> None:
    module = "class C:\n    xs = [1, 2]\n    ys = [v for v in xs]\n"
    assert free_names(module) == set()


def test_selftest_method_cannot_use_a_bare_class_attribute() -> None:
    module = "class C:\n    attr = 1\n\n    def m(self):\n        return attr\n"
    assert free_names(module) == {"attr"}


def test_selftest_lambda_in_class_body_cannot_use_a_class_attribute() -> None:
    module = "class C:\n    attr = 1\n    f = lambda self: attr\n"
    assert free_names(module) == {"attr"}


def test_selftest_comprehension_target_is_scoped_to_the_comprehension() -> None:
    module = "def f(items):\n    squares = [key * key for key in items]\n    return key\n"
    assert free_names(module) == {"key"}


def test_selftest_comprehension_elts_still_see_their_own_targets() -> None:
    module = "def f(pairs):\n    return {key: value for key, value in pairs}\n"
    assert free_names(module) == set()


def test_selftest_second_iterable_sees_earlier_targets() -> None:
    module = "def f(rows):\n    return [cell for row in rows for cell in row]\n"
    assert free_names(module) == set()


def test_selftest_global_declared_name_is_not_auto_bound() -> None:
    module = "def f() -> None:\n    global X\n    X = 1\n    return X\n"
    assert free_names(module) == {"X"}


def test_selftest_global_declared_name_bound_at_module_level_passes() -> None:
    module = "X = 0\n\n\ndef f() -> None:\n    global X\n    X = 1\n    return X\n"
    assert free_names(module) == set()
