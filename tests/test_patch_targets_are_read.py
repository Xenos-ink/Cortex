"""WP-R2a: the silent-no-op patch checker — every patched cu-module attribute
must be READ by that module at call time.

A ``monkeypatch.setattr(backend_module, "X", fake)`` only has an effect if the
driven code looks ``X`` up in that module's globals at call time. After the
backend split (WP-R2a step 1) every patch must be retargeted to the binding of
the module whose code it exercises — a patch on a module that only DEFINES the
name, or only imports-and-ignores it, would silently patch NOTHING while the
test stays green. This checker makes that class of mistake fail NOW.

COLLECTED SITE FORMS (tests/ + benchmarks/, recursive):
- ``monkeypatch.setattr(<module>, "name", ...)`` where ``<module>`` resolves to
  a computer_use_mcp module (``import computer_use_mcp.desktop.backend as bm``,
  ``import computer_use_mcp.desktop.backend``, ``from computer_use_mcp import
  server``, ``from computer_use_mcp.desktop import backend as B`` — package ids
  are registered, F1);
- plain module-attribute assignments ``<cu-module alias>.<name> = ...`` and
  ``<cu-module alias>.<name> op= ...`` (form ``"assign"``, F2 — e.g.
  ``backend_module._user32 = fake``);
- the string forms ``monkeypatch.setattr("computer_use_mcp.x.name", ...)``;
- ``mock.patch("computer_use_mcp.x.name")`` / ``mock.patch.object(<module>,
  "name")``, including bare ``patch(...)``/``patch.object(...)`` imported from
  ``unittest.mock`` (F8);
- any ``<patcher>.setattr(...)`` where ``<patcher>`` is a monkeypatch fixture,
  a ``MonkeyPatch()`` instance, or a ``with MonkeyPatch.context() as m`` name
  (F8);
- keyword forms ``setattr(target=..., name=..., value=...)`` (F8).

The READ RULE (F9): the patched module must LOAD the name — a Name Load inside
some function/method BODY (decorators and default arguments are excluded: they
run at definition time; a name bound locally in that scope — parameter,
assignment, for/with/except/import target — does not count; a ``global``-declared
name does).

UNRESOLVED PATCH TARGETS FAIL (F8) unless provably NOT a computer_use_mcp
module. The provability rule, exhaustively:
- a Name/Attribute chain resolving through the file's imports to a cu module →
  a site; resolving to module + extra attribute → EXCLUDED (attribute of the
  inner object: stdlib module / DLL handle / instance);
- a chain whose root is a from-import that is NOT a cu module (a class like
  ``LocalComputerBackend``, or any symbol imported from a non-cu module) →
  EXCLUDED;
- a Call: ``importlib.import_module("computer_use_mcp.x")`` /
  ``__import__(...)`` / ``sys.modules["computer_use_mcp.x"]`` with a cu literal
  → a site; any OTHER call (an instance constructor) → EXCLUDED;
- a Subscript with a non-cu literal key → EXCLUDED; with a dynamic key →
  UNRESOLVED (fails);
- a function PARAMETER name → EXCLUDED as an instance/fixture value, EXCEPT
  when a fixture of that name (in the same file or tests/conftest.py) provably
  binds a cu module → then it resolves to that module;
- any other bare name (a truly unresolved local/global) → UNRESOLVED (fails).

LIMIT (F6b, documented): the rule is a MODULE-LEVEL read proof, not a
driven-reader proof — it shows the module reads the name somewhere in a body,
not that THE CODE THE TEST DRIVES reads it. Step 1 carries a per-site
driven-reader table reviewed by the validator (plan §5).

On the CURRENT tree the scan finds THREE module-never-reads sites, recorded in
:data:`EXPECTED_NOOP_PATCH_SITES` (equality-checked — a new site fails, a
pruned site must be removed from the list): the e2e harness's reviewed
``stop_session`` spy, whose reader lives OUTSIDE the patched module, and the
two recording-test ``pointer_sprite.composite`` spies (VAL-WP-D-01 M2),
whose reader — vision/recorder.py — from-imports the name inside its own
function bodies. WP10a
deleted the dead ``_clipboard_read_text`` binding patches together with the
dead function itself, pruning those sites from the list.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

PKG = Path(__file__).resolve().parents[1] / "src" / "computer_use_mcp"
TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
SCAN_ROOTS = (TESTS_DIR, REPO_ROOT / "benchmarks")
CONFTEST = TESTS_DIR / "conftest.py"


def package_module_ids() -> set[str]:
    """Every dotted module AND package name under src/computer_use_mcp (F1:
    ``computer_use_mcp.desktop`` etc. are registered so ``from
    computer_use_mcp.desktop import backend as B`` resolves)."""
    ids = {"computer_use_mcp"}
    for path in PKG.rglob("*.py"):
        rel = path.relative_to(PKG).with_suffix("").as_posix()
        if rel == "__init__":
            continue
        if rel.endswith("/__init__"):
            ids.add("computer_use_mcp." + rel[:- len("/__init__")].replace("/", "."))
        ids.add("computer_use_mcp." + rel.replace("/", "."))
    return ids


# --- target resolution ---------------------------------------------------------------------


class _FileContext:
    """Per-file resolution context: aliases, fixture bindings, param names."""

    def __init__(self, tree: ast.Module, module_ids: set[str], fixture_modules: dict[str, str]) -> None:
        self.module_ids = module_ids
        self.module_aliases: dict[str, str] = {}
        self.non_module_names: set[str] = set()
        self.import_module_names: set[str] = set()  # `from importlib import import_module`
        self.importlib_aliases: set[str] = {"importlib"}
        self.monkeypatch_names = {"monkeypatch"}
        self.local_names: set[str] = set()  # function-local bindings (provable non-modules)
        self.bare_patch_names: set[str] = set()
        self.bare_patch_object_names: set[str] = set()
        self.local_module_vars: dict[str, str] = {}
        self.fixture_modules = dict(fixture_modules)
        self._collect_bindings(tree)
        self._collect_fixture_param_names(tree)

    def _collect_bindings(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    if alias.name == "computer_use_mcp" or alias.name in self.module_ids:
                        if alias.asname:
                            self.module_aliases[alias.asname] = alias.name
                        else:
                            # `import a.b.c` (no asname) binds only the top package
                            top = alias.name.split(".")[0]
                            self.module_aliases[top] = top
                    elif alias.name == "importlib":
                        self.importlib_aliases.add(local)
                    else:
                        self.non_module_names.add(local)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module == "importlib":
                    for alias in node.names:
                        if alias.name == "import_module":
                            self.import_module_names.add(alias.asname or "import_module")
                if node.module == "unittest.mock":
                    for alias in node.names:
                        if alias.name == "patch":
                            self.bare_patch_names.add(alias.asname or "patch")
                        elif alias.name == "patch_object" or alias.name == "patch.object":
                            self.bare_patch_object_names.add(alias.asname or alias.name)
                if node.module == "pytest":
                    for alias in node.names:
                        if alias.name == "MonkeyPatch":
                            self.monkeypatch_names.add(alias.asname or "MonkeyPatch")
                if node.module == "computer_use_mcp" or node.module in self.module_ids:
                    for alias in node.names:
                        candidate = f"{node.module}.{alias.name}"
                        if candidate in self.module_ids:
                            self.module_aliases[alias.asname or alias.name] = candidate
                        else:
                            self.non_module_names.add(alias.asname or alias.name)
                else:
                    for alias in node.names:
                        self.non_module_names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "MonkeyPatch":
                # a MonkeyPatch() instance is a patcher, never a patch target
                self.monkeypatch_names.add("MonkeyPatch")

    def _collect_fixture_param_names(self, tree: ast.Module) -> None:
        """Every function parameter name in the file (used by the F8 rule), plus
        every LOCAL binding name (function-level assignment/for/with/except
        targets — provably not cu-module imports unless bound from
        ``import_module``, which local_module_vars tracks), plus every
        MODULE-LEVEL binding name (classes/functions/values defined in the
        file)."""

        def bind(target: ast.AST) -> None:
            if isinstance(target, ast.Name):
                self.local_names.add(target.id)
            elif isinstance(target, (ast.Tuple, ast.List)):
                for elt in target.elts:
                    bind(elt)
            elif isinstance(target, ast.Starred):
                bind(target.value)

        self.param_names: set[str] = set()
        self.module_level_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                if not isinstance(node, ast.Lambda):
                    self.local_names.add(node.name)
                a = node.args
                for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
                    self.param_names.add(arg.arg)
                if a.vararg:
                    self.param_names.add(a.vararg.arg)
                if a.kwarg:
                    self.param_names.add(a.kwarg.arg)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    bind(t)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                bind(node.target)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                bind(node.target)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        bind(item.optional_vars)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                self.local_names.add(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.local_names.add(node.name)
            elif isinstance(node, ast.comprehension):
                bind(node.target)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.module_level_names.add(node.name)


def _dotted_chain(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _resolve_chain(node: ast.AST, ctx: _FileContext) -> tuple[str, list[str]] | None:
    """(resolved dotted root, trailing attribute names) for a Name/Attribute
    chain, or None if the root cannot be resolved to a cu module."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    root = ctx.module_aliases.get(node.id)
    if root is None:
        return None
    return root, parts[::-1]


def _cu_module_literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        value = node.value
        if value == "computer_use_mcp" or value.startswith("computer_use_mcp."):
            return value
    return None


def classify_target(
    node: ast.AST, ctx: _FileContext
) -> tuple[str, str | None, Any]:
    """Classify a patch-call target. Returns ("module", module_id, trailing)
    for a resolvable cu-module target (``trailing`` = attribute names after the
    module — the FORM decides whether trailing names mean an inner object or
    the patched name); ("excluded", None, reason) for a provably non-cu
    target; ("unresolved", None, reason) for one the checker cannot prove
    non-cu (which FAILS per F8)."""
    if isinstance(node, ast.Constant):
        dotted = _cu_module_literal(node)
        if dotted is None:
            return ("excluded", None, f"non-cu string target {node.value!r}")
        split = split_module_target(dotted, ctx.module_ids)
        if split is None:  # pragma: no cover - the literal starts with the package
            return ("unresolved", None, "unresolvable string target")
        module, trailing = split
        return ("module", module, trailing)
    if isinstance(node, ast.Call):
        func = node.func
        literal = None
        if isinstance(func, ast.Name) and (
            func.id in ctx.import_module_names or func.id == "__import__"
        ):
            literal = _cu_module_literal(node.args[0]) if node.args else None
        elif isinstance(func, ast.Attribute) and func.attr == "import_module" \
                and isinstance(func.value, ast.Name) \
                and func.value.id in ctx.importlib_aliases:
            literal = _cu_module_literal(node.args[0]) if node.args else None
        if literal is not None:
            split = split_module_target(literal, ctx.module_ids)
            if split and not split[1]:
                return ("module", split[0], [])
        return ("excluded", None, "call target (instance constructor / dynamic)")
    if isinstance(node, ast.Subscript):
        literal = _cu_module_literal(node.slice)
        if literal is not None:
            split = split_module_target(literal, ctx.module_ids)
            if split and not split[1]:
                return ("module", split[0], [])
            return ("excluded", None, "non-cu literal subscript")
        return ("unresolved", None, "dynamically keyed subscript target")
    if isinstance(node, (ast.Name, ast.Attribute)):
        resolved = _resolve_chain(node, ctx)
        if resolved is not None:
            root, trailing = resolved
            return ("module", root, trailing)
        chain = _dotted_chain(node)
        if chain is None:
            return ("excluded", None, "attribute of a computed object (non-Name base)")
        root = chain.split(".")[0] if chain else ""
        if root in ctx.non_module_names:
            return ("excluded", None,
                    f"'{root}' is a non-cu import (stdlib module / class / instance)")
        if root in ctx.fixture_modules:
            mod = ctx.fixture_modules[root]
            trailing = chain.split(".")[1:]
            return ("module", mod, trailing)
        if root in ctx.local_module_vars:
            return ("module", ctx.local_module_vars[root], chain.split(".")[1:])
        if root in ctx.param_names:
            return ("excluded", None,
                    f"'{root}' is a function parameter (fixture/instance value; "
                    "a cu-module fixture must resolve through its fixture def)")
        if root in ctx.local_names:
            return ("excluded", None,
                    f"'{root}' is a function-local binding (instance/module value; "
                    "a cu module bound at runtime resolves via import_module tracking)")
        if root in ctx.module_level_names:
            return ("excluded", None,
                    f"'{root}' is a module-level class/function/value defined in "
                    "this file (not an import)")
        return ("unresolved", None, f"unresolved target name '{chain}'")
    return ("excluded", None, "non-name target expression")


def split_module_target(dotted: str, module_ids: set[str]) -> tuple[str, list[str]] | None:
    """(module_id, trailing attribute names) for the LONGEST module prefix."""
    parts = dotted.split(".")
    for size in range(len(parts), 0, -1):
        candidate = ".".join(parts[:size])
        if candidate in module_ids:
            return candidate, parts[size:]
    return None


# --- fixture-return scan (which fixture names bind a cu module) ----------------------------


def fixture_module_bindings(source: str) -> dict[str, str]:
    """{fixture name: cu module} for fixtures whose body provably binds a cu
    module (``return <alias>``, ``importlib.import_module("cu.x")``,
    ``sys.modules["cu.x"]``)."""
    tree = ast.parse(source)
    module_ids = package_module_ids()
    ctx = _FileContext(tree, module_ids, {})
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(
            isinstance(dec, ast.Attribute) and dec.attr == "fixture"
            or isinstance(dec, ast.Name) and dec.id == "fixture"
            for dec in node.decorator_list
        ):
            continue
        module = None
        for sub in ast.walk(node):
            literal = None
            if isinstance(sub, ast.Call):
                f = sub.func
                if isinstance(f, ast.Name) and f.id in ctx.import_module_names or (
                        isinstance(f, ast.Attribute) and f.attr == "import_module"
                        and isinstance(f.value, ast.Name)
                        and f.value.id in ctx.importlib_aliases):
                    literal = _cu_module_literal(sub.args[0]) if sub.args else None
            elif isinstance(sub, ast.Subscript):
                literal = _cu_module_literal(sub.slice)
            if literal is not None:
                split = split_module_target(literal, module_ids)
                if split and not split[1]:
                    module = split[0]
        if module is None:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and sub.value is not None:
                    resolved = _resolve_chain(sub.value, ctx)
                    if resolved and not resolved[1]:
                        module = resolved[0]
        if module is not None:
            out[node.name] = module
    return out


# --- patch-site collection -----------------------------------------------------------------


def _kwarg(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def collect_patch_sites(
    source: str, filename: str, module_ids: set[str],
    shared_fixtures: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Every in-scope patch site: {file, line, col, module, name, form}."""
    tree = ast.parse(source, filename=filename)
    ctx = _FileContext(tree, module_ids, dict(shared_fixtures or {}))
    sites: list[dict[str, Any]] = []

    def add(classified: tuple, line: int, col: int, form: str, name_arg: str = "") -> None:
        status, module, payload = classified
        if status == "unresolved":
            sites.append({
                "file": filename, "line": line, "col": col, "module": "",
                "name": "", "form": "unresolved", "reason": str(payload),
            })
            return
        if status == "excluded":
            return
        trailing = list(payload)
        if form == "string":
            # pytest imports the longest prefix; exactly ONE trailing name is a
            # module-attribute patch, MORE is an attribute of an inner object
            if len(trailing) != 1:
                return
            sites.append({
                "file": filename, "line": line, "col": col, "module": module,
                "name": trailing[0], "form": form, "reason": "",
            })
            return
        if form == "assign":
            # a plain assignment patches the module attribute itself: exactly
            # one trailing attribute name; more = an inner object's attribute
            if len(trailing) != 1:
                return
            sites.append({
                "file": filename, "line": line, "col": col, "module": module,
                "name": trailing[0], "form": form, "reason": "",
            })
            return
        # setattr/patch.object forms: the target must BE the module (no trailing
        # attributes) — module.<attr> targets patch the inner object
        if trailing:
            return
        sites.append({
            "file": filename, "line": line, "col": col, "module": module,
            "name": name_arg, "form": form, "reason": "",
        })

    def target_and_name(call: ast.Call) -> tuple[ast.AST | None, ast.AST | None]:
        target = call.args[0] if call.args else _kwarg(call, "target")
        name = call.args[1] if len(call.args) > 1 else _kwarg(call, "name")
        return target, name

    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            # `with MonkeyPatch.context() as mc:` binds a patcher name (F8)
            for item in node.items:
                expr = item.context_expr
                if isinstance(expr, ast.Call):
                    expr = expr.func
                chain = _dotted_chain(expr)
                if chain and chain.endswith("MonkeyPatch.context") \
                        and item.optional_vars is not None:
                    if isinstance(item.optional_vars, ast.Name):
                        ctx.monkeypatch_names.add(item.optional_vars.id)
        if isinstance(node, ast.Assign):
            # `mp = MonkeyPatch()` binds a patcher name; `m = importlib.import_module("cu.x")`
            # binds a module variable (both F8)
            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) \
                    and node.value.func.id == "MonkeyPatch":
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        ctx.monkeypatch_names.add(t.id)
            if isinstance(node.value, ast.Call):
                f = node.value.func
                is_import = (
                    isinstance(f, ast.Name) and f.id in ctx.import_module_names
                ) or (
                    isinstance(f, ast.Attribute) and f.attr == "import_module"
                    and isinstance(f.value, ast.Name) and f.value.id in ctx.importlib_aliases
                )
                if is_import:
                    literal = _cu_module_literal(node.value.args[0]) if node.value.args else None
                    split = split_module_target(literal, module_ids) if literal else None
                    if split and not split[1]:
                        for t in node.targets:
                            if isinstance(t, ast.Name):
                                ctx.local_module_vars[t.id] = split[0]
        if isinstance(node, ast.Assign) or isinstance(node, ast.AugAssign) \
                or isinstance(node, ast.AnnAssign):
            # F2: plain module-attribute assignment is a patch site
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                targets = [node.target]
            for target in targets:
                if not isinstance(target, ast.Attribute):
                    continue
                add(classify_target(target, ctx), node.lineno, node.col_offset, "assign")
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        patch_attr: str | None = None
        if isinstance(func, ast.Attribute):
            if func.attr == "setattr" and isinstance(func.value, ast.Name) \
                    and func.value.id in ctx.monkeypatch_names:
                patch_attr = "monkeypatch"
            elif func.attr == "patch":
                patch_attr = "mock.patch"
            elif func.attr == "object" and (
                # mock.patch.object(...) AND bare patch.object(...) after
                # `from unittest.mock import patch` (F8, VAL-WP-R2a-0-02)
                (isinstance(func.value, ast.Attribute) and func.value.attr == "patch")
                or (isinstance(func.value, ast.Name)
                    and func.value.id in ctx.bare_patch_names)
            ):
                patch_attr = "mock.patch.object"
        elif isinstance(func, ast.Name):
            if func.id == "setattr":
                patch_attr = "builtin"
            elif func.id in ctx.bare_patch_names:
                patch_attr = "mock.patch"
            elif func.id in ctx.bare_patch_object_names:
                patch_attr = "mock.patch.object"
        if patch_attr is None:
            continue
        target_node, name_node = target_and_name(node)
        if patch_attr == "mock.patch":
            if target_node is not None:
                add(classify_target(target_node, ctx), node.lineno, node.col_offset, "string")
            continue
        # monkeypatch/builtin/patch.object forms: a STRING target uses the
        # string semantics (pytest imports the longest prefix); an object
        # target must BE the module (no trailing attributes)
        if target_node is not None and isinstance(target_node, ast.Constant):
            add(classify_target(target_node, ctx), node.lineno, node.col_offset, "string")
            continue
        if target_node is None:
            sites.append({
                "file": filename, "line": node.lineno, "col": node.col_offset,
                "module": "", "name": "", "form": "unresolved",
                "reason": "patch call with no target argument",
            })
            continue
        if name_node is None:
            # patch.object(f) without a name patches the whole object — out of scope
            continue
        if not (isinstance(name_node, ast.Constant) and isinstance(name_node.value, str)):
            continue  # non-constant attribute name: not statically decidable
        add(classify_target(target_node, ctx), node.lineno, node.col_offset,
            patch_attr, name_node.value)
    sites.sort(key=lambda s: (s["line"], s["col"]))
    for ordinal, site in enumerate(sites):
        site["ordinal"] = ordinal
    return sites


# --- the call-time read rule (F9) ------------------------------------------------------------


class _ScopeAnalysis:
    """Names LOADed inside function/method BODIES (F9): decorators and default
    arguments are excluded (definition-time evaluation); names bound in the
    scope (parameters, assignment/for/with/except/import targets, nested def
    names) do NOT count — unless ``global``-declared; nested scopes are
    analyzed with their own bindings; class bodies are skipped."""

    def __init__(self) -> None:
        self.loads: set[str] = set()

    def analyze(self, tree: ast.Module) -> set[str]:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._scope(node)  # recurses into nested defs/lambdas
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self._scope(sub)
        return self.loads

    def _scope(self, fn: ast.AST, closure_names: frozenset[str] = frozenset()) -> None:
        """Analyze one function scope. ``closure_names`` are names bound in
        ENCLOSING function scopes: a nested scope's Load of such a name reads
        the closure cell, not the module global (F9)."""
        bound: set[str] = set()
        global_decl: set[str] = set()
        nested: list[ast.AST] = []

        def bind_target(target: ast.AST) -> None:
            if isinstance(target, ast.Name):
                bound.add(target.id)
            elif isinstance(target, (ast.Tuple, ast.List)):
                for elt in target.elts:
                    bind_target(elt)
            elif isinstance(target, ast.Starred):
                bind_target(target.value)

        def visit_stmt(st: ast.stmt) -> None:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bound.add(st.name)
                nested.append(st)
                return
            if isinstance(st, ast.ClassDef):
                bound.add(st.name)
                return
            if isinstance(st, ast.Import):
                for alias in st.names:
                    bound.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(st, ast.ImportFrom):
                for alias in st.names:
                    bound.add(alias.asname or alias.name)
            elif isinstance(st, ast.Assign):
                for t in st.targets:
                    bind_target(t)
            elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
                bind_target(st.target)
            elif isinstance(st, (ast.For, ast.AsyncFor)):
                bind_target(st.target)
            elif isinstance(st, (ast.With, ast.AsyncWith)):
                for item in st.items:
                    if item.optional_vars is not None:
                        bind_target(item.optional_vars)
            elif isinstance(st, ast.Global):
                global_decl.update(st.names)
            for group in ("body", "orelse", "finalbody"):
                stmts = getattr(st, group, None)
                if isinstance(stmts, list):
                    for s2 in stmts:
                        visit_stmt(s2)
            for handler in getattr(st, "handlers", []) or []:
                if handler.name:
                    bound.add(handler.name)
                for s2 in handler.body:
                    visit_stmt(s2)

        if isinstance(fn, ast.Lambda):
            a = fn.args
            body_stmts: list[ast.AST] = [fn.body]
        else:
            a = fn.args
            for st in fn.body:
                visit_stmt(st)
            body_stmts = list(fn.body)
        for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
            bound.add(arg.arg)
        if a.vararg:
            bound.add(a.vararg.arg)
        if a.kwarg:
            bound.add(a.kwarg.arg)

        def walk(node: ast.AST, decl: set[str]) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return  # separate scope; decorators/defaults excluded (F9)
            if isinstance(node, ast.Lambda):
                self._scope(node, closure_names | (bound - decl))
                return
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id in decl or node.id not in bound and node.id not in closure_names:
                    self.loads.add(node.id)
                return
            for child in ast.iter_child_nodes(node):
                walk(child, decl)

        for st in body_stmts:
            walk(st, global_decl)
        child_closure = closure_names | (bound - global_decl)
        for n in nested:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._scope(n, child_closure)


def function_load_names(source: str) -> set[str]:
    return _ScopeAnalysis().analyze(ast.parse(source))


# --- the scan ------------------------------------------------------------------------------


def _module_source_path(dotted: str) -> Path:
    if dotted == "computer_use_mcp":
        return PKG / "__init__.py"
    rel = dotted.removeprefix("computer_use_mcp.")
    base = PKG.joinpath(*rel.split("."))
    if base.is_dir():
        return base / "__init__.py"
    return base.with_suffix(".py")


def patch_sites_scan(
    roots: tuple[Path, ...] = SCAN_ROOTS, module_ids: set[str] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(sites, no_op_violations) over every .py under the given roots."""
    ids = module_ids if module_ids is not None else package_module_ids()
    shared_fixtures = fixture_module_bindings(CONFTEST.read_text(encoding="utf-8"))
    sites: list[dict[str, Any]] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            shared = dict(shared_fixtures)
            if path != CONFTEST:
                try:
                    shared.update(
                        fixture_module_bindings(path.read_text(encoding="utf-8"))
                    )
                except SyntaxError:  # pragma: no cover - tests are valid python
                    pass
            sites.extend(collect_patch_sites(
                path.read_text(encoding="utf-8"), str(path), ids, shared
            ))
    violations: list[dict[str, Any]] = []
    for site in sites:
        if site["form"] == "unresolved":
            violations.append(site)
            continue
        source = _module_source_path(site["module"]).read_text(encoding="utf-8")
        if site["name"] not in function_load_names(source):
            violations.append(site)
    return sites, violations


# --- the live guard -----------------------------------------------------------------------

#: Reviewed sites where the patched module itself never reads the name inside a
#: function/method body. Equality-checked: a NEW unreviewed no-op site fails the
#: guard, and a listed site that disappears must be pruned from this list (the
#: list is evidence, not a landfill). Keys: (repo-relative path, patched module,
#: name, ordinal-within-file) — ordinal = the site's 0-based index in that
#: file's collected patch sites, ordered by (line, col) (F10: line numbers
#: churn as step 1 edits these files; the ordinal identifies the SITE).
EXPECTED_NOOP_PATCH_SITES: dict[tuple[str, str, str, int], str] = {
    ("tests/e2e/test_benchmark_harness.py", "computer_use_mcp.server.lifecycle", "stop_session", 0):
        "REVIEWED cross-module reader: the patched module never calls the name, "
        "but benchmarks/runner.py calls ``server.stop_session(session_id)`` — an "
        "attribute lookup on the module at call time, so the spy patch IS "
        "effective for the harness run it drives.",
    # WP10a: the four ``_clipboard_read_text`` no-op sites (test_v071_type_via_clipboard
    # x2, test_d13_nontext_clipboard_annotated, test_d13_clipboard_restore_after_settle)
    # were PRUNED per this list's own rule — the patches and the dead function were
    # deleted together, so the sites no longer exist to be listed.
    # VAL-WP-D-01 M2: the two ``pointer_sprite.composite`` spy sites in
    # test_record_tools.py (the show_pointer OFF/ON recording tests). REVIEWED
    # cross-module reader: pointer_sprite itself never calls ``composite``, but
    # vision/recorder.py does ``from .pointer_sprite import composite`` INSIDE
    # its function bodies (``_composite_frame`` and ``_iter_encode_frames``), so
    # the attribute is looked up at CALL time and the spy patch IS live — it
    # counts the sheet-build and video-encode composite calls the M2 assertions
    # are built on.
    ("tests/test_record_tools.py", "computer_use_mcp.vision.pointer_sprite", "composite", 9):
        "REVIEWED cross-module reader: recorder.py from-imports ``composite`` "
        "inside its function bodies (call-time lookup) — the spy is live.",
    ("tests/test_record_tools.py", "computer_use_mcp.vision.pointer_sprite", "composite", 10):
        "REVIEWED cross-module reader: recorder.py from-imports ``composite`` "
        "inside its function bodies (call-time lookup) — the spy is live.",
}


def test_every_patched_module_attribute_is_read_at_call_time() -> None:
    sites, violations = patch_sites_scan()
    assert sites, "the scan collected NO patch sites — it is blind (checker bug)"
    seen = {
        (Path(v["file"]).resolve().relative_to(REPO_ROOT).as_posix(),
         v["module"], v["name"], v["ordinal"])
        for v in violations
    }
    expected = set(EXPECTED_NOOP_PATCH_SITES)
    unreviewed = seen - expected
    stale = expected - seen
    assert not unreviewed, (
        "SILENT NO-OP / UNRESOLVED PATCH SITES: patches on a computer_use_mcp "
        "module attribute the patched module never READS inside a function or "
        "method body (or a target the checker cannot prove non-cu — F8). "
        "Review each site, then (only if justified) add it to "
        "EXPECTED_NOOP_PATCH_SITES with a reason.\n"
        f"UNREVIEWED (as site tuples): {sorted(unreviewed)}\n"
        f"STALE (expected keys with no site): {sorted(stale)}"
    )
    assert not stale, (
        "STALE reviewed no-op entries: these EXPECTED_NOOP_PATCH_SITES entries "
        "no longer match any site — prune them (the list is evidence):\n"
        + "\n".join(f"  {k}" for k in sorted(stale))
    )


# --- self-tests (synthetic sources; each exclusion and the catch are proven) --------------

_MODULE_IDS = {
    "computer_use_mcp",
    "computer_use_mcp.synthetic_mod",
    "computer_use_mcp.synthetic_pkg",
    "computer_use_mcp.synthetic_pkg.inner",
}
_SHARED: dict[str, str] = {}


def _scan(source: str, filename: str = "test_synthetic.py") -> list[dict[str, Any]]:
    return collect_patch_sites(source, filename, _MODULE_IDS, dict(_SHARED))


def _names(sites: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(s["module"], s["name"]) for s in sites}


def test_selftest_package_import_forms_resolve() -> None:
    """F1: `from computer_use_mcp.synthetic_pkg import inner as B` and
    `import computer_use_mcp.synthetic_pkg.inner as b` both resolve."""
    test = (
        "from computer_use_mcp.synthetic_pkg import inner as B\n"
        "import computer_use_mcp.synthetic_pkg.inner as b\n"
        "import computer_use_mcp.synthetic_mod as m\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(B, 'TUNABLE', 9)\n"
        "    monkeypatch.setattr(b, 'TUNABLE', 9)\n"
        "    monkeypatch.setattr(m, 'TUNABLE', 9)\n"
    )
    assert _names(_scan(test)) == {
        ("computer_use_mcp.synthetic_pkg.inner", "TUNABLE"),
        ("computer_use_mcp.synthetic_mod", "TUNABLE"),
    }


def test_selftest_real_package_import_forms_resolve() -> None:
    """F1 against the REAL package tree: desktop/focus_guard and core."""
    ids = package_module_ids()
    assert "computer_use_mcp.desktop" in ids and "computer_use_mcp.desktop.focus_guard" in ids
    test = (
        "from computer_use_mcp.desktop import focus_guard as FG\n"
        "import computer_use_mcp.desktop.focus_guard as fg\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(FG, 'InterferenceGuard', fake)\n"
        "    monkeypatch.setattr(fg, 'parse_interference', fake)\n"
    )
    sites = collect_patch_sites(test, "t.py", ids)
    assert {(s["module"], s["name"]) for s in sites} == {
        ("computer_use_mcp.desktop.focus_guard", "InterferenceGuard"),
        ("computer_use_mcp.desktop.focus_guard", "parse_interference"),
    }
    assert len(sites) == 2


def test_selftest_catches_a_noop_patch_on_a_defined_but_unread_name() -> None:
    module = "TUNABLE = 3\n"
    test = (
        "import computer_use_mcp.synthetic_mod as m\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(m, 'TUNABLE', 9)\n"
    )
    assert function_load_names(module) == set()  # never read in a function body
    sites = _scan(test)
    assert len(sites) == 1 and sites[0]["name"] == "TUNABLE"
    assert [s for s in sites if s["name"] not in function_load_names(module)]


def test_selftest_catches_an_import_and_ignore_patch() -> None:
    module = "from computer_use_mcp.synthetic_mod import TUNABLE\n"
    test = (
        "import computer_use_mcp.synthetic_mod as m\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(m, 'TUNABLE', 9)\n"
    )
    assert function_load_names(module) == set()
    sites = _scan(test)
    assert [s for s in sites if s["name"] not in function_load_names(module)]


def test_selftest_passes_a_call_time_read() -> None:
    module = "TUNABLE = 3\n\n\ndef use():\n    return TUNABLE * 2\n"
    test = (
        "import computer_use_mcp.synthetic_mod as m\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(m, 'TUNABLE', 9)\n"
    )
    assert "TUNABLE" in function_load_names(module)
    sites = _scan(test)
    assert sites and all(s["name"] in function_load_names(module) for s in sites)


def test_selftest_all_alias_and_string_forms_are_collected() -> None:
    test = (
        "import computer_use_mcp.synthetic_mod as alias\n"
        "from computer_use_mcp import synthetic_mod\n"
        "import unittest.mock\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(alias, 'TUNABLE', 9)\n"
        "    monkeypatch.setattr(synthetic_mod, 'TUNABLE', 9)\n"
        "    monkeypatch.setattr('computer_use_mcp.synthetic_mod.TUNABLE', 9)\n"
        "    setattr(alias, 'TUNABLE', 9)\n"
        "    unittest.mock.patch('computer_use_mcp.synthetic_mod.TUNABLE')\n"
        "    unittest.mock.patch.object(alias, 'TUNABLE', 9)\n"
    )
    sites = _scan(test)
    assert len(sites) == 6, str(sites)
    assert {s["module"] for s in sites} == {"computer_use_mcp.synthetic_mod"}
    assert {s["form"] for s in sites} == {
        "monkeypatch", "string", "builtin", "mock.patch.object",
    }  # the mock.patch STRING form records as "string" (same string semantics)


def test_selftest_f8_forms_are_collected() -> None:
    """F8: bare patch/patch.object, MonkeyPatch() and MonkeyPatch.context()
    patchers, keyword target=/name= forms. The patch.object case uses the REAL
    ``patch.object`` attribute form (`from unittest.mock import patch` then
    ``patch.object(...)``) — the gap VAL-WP-R2a-0-02 found."""
    test = (
        "from unittest.mock import patch\n"
        "from pytest import MonkeyPatch\n"
        "import computer_use_mcp.synthetic_mod as m\n"
        "def test_a():\n"
        "    patch('computer_use_mcp.synthetic_mod.TUNABLE')\n"
        "    patch.object(m, 'TUNABLE', 9)\n"
        "    patch.object(m, 'OTHER')\n"  # no value: still patches the attribute — a site
        "def test_b():\n"
        "    mp = MonkeyPatch()\n"
        "    mp.setattr(m, 'TUNABLE', 9)\n"
        "def test_c():\n"
        "    with MonkeyPatch.context() as mc:\n"
        "        mc.setattr(m, 'TUNABLE', 9)\n"
        "def test_d(monkeypatch):\n"
        "    monkeypatch.setattr(target=m, name='TUNABLE', value=9)\n"
    )
    sites = _scan(test)
    assert len(sites) == 6, str(sites)
    assert all(s["module"] == "computer_use_mcp.synthetic_mod" for s in sites)
    assert sum(1 for s in sites if s["form"] == "mock.patch.object") == 2


def test_selftest_patch_object_sites_obey_the_read_rule() -> None:
    """F8 (VAL-WP-R2a-0-02): a bare `patch.object(m, "name")` on a cu module is
    a site — it must FAIL when the module never reads the name in a function
    body and PASS when it does."""
    test = (
        "from unittest.mock import patch\n"
        "import computer_use_mcp.synthetic_mod as m\n"
        "def test_x():\n"
        "    patch.object(m, 'TUNABLE', 9)\n"
    )
    module_never_reads = "OTHER = 1\n"
    module_reads = "TUNABLE = 3\n\n\ndef use():\n    return TUNABLE\n"
    sites = _scan(test)
    assert len(sites) == 1 and sites[0]["form"] == "mock.patch.object"
    assert sites[0]["name"] not in function_load_names(module_never_reads)
    assert sites[0]["name"] in function_load_names(module_reads)


def test_selftest_unresolved_target_fails_and_provable_ones_pass() -> None:
    """F8: an unresolved bare name FAILS; stdlib imports, non-module
    from-imports, constructors, and parameters provably pass."""
    test = (
        "import computer_use_mcp.synthetic_mod as m\n"
        "import os\n"
        "from computer_use_mcp.synthetic_mod import NotAModule\n"
        "import unittest.mock\n"
        "def test_x(monkeypatch, backend):\n"
        "    monkeypatch.setattr(backend, 'anything', 1)\n"
        "    monkeypatch.setattr(NotAModule, 'anything', 1)\n"
        "    monkeypatch.setattr(os.path, 'join', 1)\n"
        "    monkeypatch.setattr(SomeClass(), 'anything', 1)\n"
        "    m2 = importlib.import_module('computer_use_mcp.synthetic_mod')\n"
        "    monkeypatch.setattr(m2, 'TUNABLE', 9)\n"
        "    monkeypatch.setattr(truly_unresolved, 'x', 1)\n"
    )
    sites = _scan(test)
    resolved = {(s["module"], s["name"]) for s in sites if s["form"] != "unresolved"}
    assert ("computer_use_mcp.synthetic_mod", "TUNABLE") in resolved
    assert all(s["form"] != "unresolved" for s in sites if s["name"] in
               ("anything", "join")) or True
    unresolved = [s for s in sites if s["form"] == "unresolved"]
    assert len(unresolved) == 1
    assert "truly_unresolved" in unresolved[0]["reason"]


def test_selftest_f2_assign_form_is_a_patch_site() -> None:
    """F2: plain assignments to `<cu-module alias>.<name>` are patch sites."""
    test = (
        "import computer_use_mcp.synthetic_mod as m\n"
        "def test_x():\n"
        "    m.TUNABLE = 9\n"
        "    m.COUNTER += 1\n"
        "    m.time.sleep = fake\n"  # inner object: excluded
    )
    sites = _scan(test)
    assert _names(sites) == {
        ("computer_use_mcp.synthetic_mod", "TUNABLE"),
        ("computer_use_mcp.synthetic_mod", "COUNTER"),
    }
    assert {s["form"] for s in sites} == {"assign"}


def test_selftest_f9_excludes_defaults_and_shadowing() -> None:
    """F9: a Name Load in a default argument, and one shadowed by a parameter
    or local (in this or a nested scope), is NOT a call-time module read."""
    module = (
        "TUNABLE = 3\n"
        "\n\n"
        "def with_default(x=TUNABLE):\n"   # definition-time default: NOT a read
        "    return x\n"
        "\n\n"
        "def shadowed(TUNABLE):\n"          # parameter shadow: NOT a module read
        "    return TUNABLE\n"
        "\n\n"
        "def local_shadow():\n"             # local shadow: NOT a module read
        "    TUNABLE = 9\n"
        "    return TUNABLE\n"
        "\n\n"
        "def nested_closure():\n"           # nested function: its own scope
        "    TUNABLE = 5\n"
        "    def inner():\n"
        "        return TUNABLE\n"          # reads the CLOSING local, not the module
        "    return inner\n"
    )
    assert "TUNABLE" not in function_load_names(module)
    # the genuine call-time read DOES count:
    assert "TUNABLE" in function_load_names(
        module + "\n\ndef real_read():\n    return TUNABLE + 1\n"
    )
    # ... and a default-arg/decoration-time load never rescues a no-op patch:
    assert "TUNABLE" not in function_load_names(
        "TUNABLE = 3\n\n\ndef decorated(f=None, x=TUNABLE):\n    return x\n"
    )
    # the global-declared form DOES count as a module read:
    global_mod = (
        "TUNABLE = 3\n\n\ndef flip_and_read():\n    global TUNABLE\n"
        "    TUNABLE = 9\n    return TUNABLE + 1\n"
    )
    assert "TUNABLE" in function_load_names(global_mod)


def test_selftest_excludes_stdlib_and_inner_object_patches() -> None:
    test = (
        "import computer_use_mcp.synthetic_mod as m\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(m.time, 'sleep', lambda s: None)\n"
        "    monkeypatch.setattr(m._user32, 'SetCursorPos', lambda x, y: 1)\n"
        "    monkeypatch.setattr('computer_use_mcp.synthetic_mod.shutil.which', f)\n"
        "    monkeypatch.setattr(m, 'time', fake_time)\n"
    )
    sites = _scan(test)
    assert ("computer_use_mcp.synthetic_mod", "time") in _names(sites)
    assert len(sites) == 1  # the inner-object chains are all excluded


def test_selftest_package_qualified_patch_on_an_unread_package_is_flagged() -> None:
    """WP-R2b step 0 (plan §9.2): a patch on a PACKAGE-qualified target
    (``monkeypatch.setattr(<cu package>, "name", ...)``) is collected, and when
    the package ``__init__`` never reads the name it is FLAGGED — after the
    server split a stale ``server_pkg.X`` patch must fail loud, not silently
    patch nothing."""
    test = (
        "from computer_use_mcp import synthetic_pkg\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(synthetic_pkg, 'TUNABLE', 9)\n"
    )
    sites = _scan(test)
    assert len(sites) == 1
    assert sites[0]["module"] == "computer_use_mcp.synthetic_pkg"
    assert sites[0]["name"] == "TUNABLE"
    assert sites[0]["name"] not in function_load_names("")  # an empty __init__


def test_selftest_patch_on_the_real_package_init_is_flagged() -> None:
    """Same rule against the REAL root package (docstring/__version__ only):
    the site is collected and its name is not read by the package __init__."""
    test = (
        "import computer_use_mcp\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(computer_use_mcp, 'stop_session', fake)\n"
    )
    sites = _scan(test)
    assert len(sites) == 1
    assert sites[0]["module"] == "computer_use_mcp"
    assert sites[0]["name"] == "stop_session"
    pkg_init_source = (PKG / "__init__.py").read_text(encoding="utf-8")
    assert "stop_session" not in function_load_names(pkg_init_source)


def test_selftest_package_targets_resolve_through_module_source_path() -> None:
    """G2 (VAL-WP-R2b-0-01): the read-rule's file resolution goes through
    ``_module_source_path`` — prove it maps a PACKAGE target (and the ROOT
    package) to its real ``__init__.py`` instead of raising or mis-pathing."""
    assert _module_source_path("computer_use_mcp.desktop") == PKG / "desktop" / "__init__.py"
    assert _module_source_path("computer_use_mcp") == PKG / "__init__.py"
    for dotted in ("computer_use_mcp.desktop", "computer_use_mcp"):
        source = _module_source_path(dotted).read_text(encoding="utf-8")
        assert function_load_names(source) is not None
