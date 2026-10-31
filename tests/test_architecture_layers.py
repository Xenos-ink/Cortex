"""WP-R1 permanent architecture guard: the layered package structure.

Layer order — a module may import only its own layer or LOWER layers; an
import of a HIGHER layer fails:

    core (0) < security (1) < desktop (2) < vision (3) < pipeline (4)
             < planning (5) < server (6) < cli (7)

server.py counts as server and cli.py as cli until
WP-R2 moves them.  Every import of every module under
``src/computer_use_mcp`` (module-level AND function-level, absolute AND
relative) is resolved to a layer; an import of a HIGHER layer fails.
Subpackage ``__init__.py`` files must stay empty apart from an optional
docstring — no re-exports, no logic.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "src" / "computer_use_mcp"

LAYER_ORDER = ("core", "security", "desktop", "vision", "pipeline",
               "planning", "server", "cli")
_LAYER_INDEX = {name: idx for idx, name in enumerate(LAYER_ORDER)}
# root-level modules until WP-R2 relocates them (backend now lives in desktop/)
_ROOT_MODULE_LAYER = {"server": "server", "cli": "cli"}
SUBPACKAGES = ("core", "security", "desktop", "vision", "pipeline", "planning")


def layer_of(dotted: str) -> str | None:
    """Layer of a computer_use_mcp-internal dotted module path (None: external)."""
    parts = dotted.split(".")
    if parts[0] != "computer_use_mcp":
        return None
    if len(parts) == 1:
        return None  # the package itself — no layer
    first = parts[1]
    if first in _LAYER_INDEX:
        return first
    return _ROOT_MODULE_LAYER.get(first)


def resolve_target(owner: str, node: ast.Import | ast.ImportFrom) -> list[str]:
    """Dotted computer_use_mcp module paths an import statement may bind."""
    targets: list[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.startswith("computer_use_mcp"):
                targets.append(alias.name)
        return targets
    level, module = node.level, node.module
    if level == 0:
        if module and module.startswith("computer_use_mcp"):
            base = module
        else:
            return []
    else:
        package = owner.split(".")[:-1]
        for _ in range(level - 1):
            package = package[:-1]
        base = ".".join(package + ([module] if module else []))
    if not base.startswith("computer_use_mcp"):
        return []
    targets.append(base)
    for alias in node.names:
        targets.append(f"{base}.{alias.name}")
    return targets


def _string_import_targets(tree: ast.Module) -> list[str]:
    """computer_use_mcp dotted paths passed to importlib.import_module/__import__.

    Covers the direct forms AND the alias forms (VAL-WP-R1-02 nit):
    ``import importlib`` / ``import importlib as il`` /
    ``from importlib import import_module`` /
    ``from importlib import import_module as im``.
    """
    importlib_aliases: set[str] = {"importlib"}
    import_module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or "importlib")
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    import_module_names.add(alias.asname or "import_module")
    targets: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_import_module = (
            isinstance(func, ast.Attribute)
            and func.attr == "import_module"
            and isinstance(func.value, ast.Name)
            and func.value.id in importlib_aliases
        )
        is_bound_import_module = (
            isinstance(func, ast.Name) and func.id in import_module_names
        )
        is_dunder_import = isinstance(func, ast.Name) and func.id == "__import__"
        if not (is_import_module or is_bound_import_module or is_dunder_import):
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            name = node.args[0].value
            if name.startswith("computer_use_mcp."):
                targets.append(name)
    return targets


def layer_violations(owner: str, source: str) -> list[str]:
    """All upward-import descriptions for one module's source text."""
    tree = ast.parse(source, filename=owner)
    own = layer_of(owner)
    if own is None:
        return []
    own_idx = _LAYER_INDEX[own]
    found: list[str] = []
    check_nodes: list[ast.AST] = list(ast.walk(tree))
    for node in check_nodes:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for target in resolve_target(owner, node):
            target_layer = layer_of(target)
            if target_layer is None:
                continue
            if _LAYER_INDEX[target_layer] > own_idx:
                found.append(
                    f"{owner}: {own} -> {target_layer} "
                    f"(line {getattr(node, 'lineno', '?')}, '{target}')"
                )
    for name in _string_import_targets(tree):
        target_layer = layer_of(name)
        if target_layer is not None and _LAYER_INDEX[target_layer] > own_idx:
            found.append(f"{owner}: {own} -> {target_layer} (string import '{name}')")
    return found


def init_violations(source: str) -> list[str]:
    """Descriptions of any non-docstring statement in an __init__.py source."""
    tree = ast.parse(source)
    if len(tree.body) == 0:
        return []
    if (len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)):
        return []
    return [ast.dump(node, include_attributes=False) for node in tree.body[1 if _is_docstring(tree.body[0]) else 0:]]


def _is_docstring(node: ast.stmt) -> bool:
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def all_package_modules() -> list[tuple[str, str]]:
    """(dotted module name, source text) for every .py under the package."""
    out: list[tuple[str, str]] = []
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG).with_suffix("").as_posix()
        dotted = "computer_use_mcp" + ("" if rel == "__init__" else "." + rel.replace("/", "."))
        out.append((dotted, path.read_text(encoding="utf-8")))
    return out


# --- the guards -----------------------------------------------------------------------


def test_no_module_imports_a_higher_layer() -> None:
    violations: list[str] = []
    for dotted, source in all_package_modules():
        violations.extend(layer_violations(dotted, source))
    assert violations == [], "upward imports found:\n" + "\n".join(violations)


@pytest.mark.parametrize("subpackage", SUBPACKAGES)
def test_subpackage_init_is_empty_or_docstring_only(subpackage: str) -> None:
    source = (PKG / subpackage / "__init__.py").read_text(encoding="utf-8")
    assert init_violations(source) == [], (
        f"{subpackage}/__init__.py must stay empty (optional docstring only); "
        "re-exports belong in the modules themselves"
    )


def test_every_module_and_subpackage_has_a_layer_mapping() -> None:
    """No .py under the package and no subpackage may go silently unchecked
    (WP-R2 will add new modules — each must land in a mapped layer)."""
    unmapped: list[str] = []
    for dotted, _source in all_package_modules():
        if dotted != "computer_use_mcp" and layer_of(dotted) is None:
            unmapped.append(dotted)
    for path in sorted(PKG.iterdir()):
        if path.is_dir() and (path / "__init__.py").exists():
            sub = f"computer_use_mcp.{path.name}"
            if layer_of(sub) is None:
                unmapped.append(sub + " (subpackage)")
    assert unmapped == [], "modules with no layer mapping:\n" + "\n".join(unmapped)


MOVED_MODULE_FILES = (
    "core/models.py", "core/state.py", "core/limits.py", "core/textnorm.py",
    "security/safety.py", "security/redaction.py", "security/audit.py",
    "desktop/backend.py", "desktop/capture.py", "desktop/clipboard.py",
    "desktop/coordinates.py", "desktop/errors.py", "desktop/fake_backend.py",
    "desktop/input_engine.py", "desktop/launch.py", "desktop/motion.py",
    "desktop/text_readers.py", "desktop/windows.py", "desktop/win32.py",
    "desktop/focus_guard.py", "desktop/interference.py",
    "vision/observation.py", "vision/visual_views.py",
    "vision/text_substrates.py", "vision/evidence.py",
    "pipeline/agent.py", "pipeline/grounding.py", "pipeline/validator.py",
    "pipeline/verification.py", "pipeline/provider.py",
    "planning/plan.py", "planning/plan_validator.py",
    "planning/subtask_manager.py", "planning/checkpoint_manager.py",
    "planning/resume_manager.py", "planning/context_manager.py",
    "planning/long_running.py",
)
ROOT_MODULE_FILES = ("__init__.py", "server.py", "cli.py")


def all_package_module_relpaths() -> set[str]:
    """The RECURSIVE package module-file listing (package-relative posix paths).

    F1 (VAL-WP-R1-02): the SHARED scan listing — every source-tree scan in the
    suite must derive its file set from this helper (or prove equality with
    it), so a root-only ``glob("*.py")`` regression cannot hide moved modules.
    """
    return {path.relative_to(PKG).as_posix() for path in PKG.rglob("*.py")}


def test_recursive_walk_sees_every_module_file() -> None:
    """F1 regression guard (VAL-WP-R1-01): source-tree scans must be
    RECURSIVE — none of the 26 moved modules lives at the package root, so a
    root-level ``glob("*.py")`` (as test_w1_cancellable_wait.py used) is blind
    to them. Any scan of the source tree must see all 29 module files."""
    seen = all_package_module_relpaths()
    expected = set(MOVED_MODULE_FILES) | set(ROOT_MODULE_FILES)
    missing = sorted(expected - seen)
    assert missing == [], f"recursive walk missed module files: {missing}"
    assert "pipeline/agent.py" in seen, "the shared listing itself went blind"
    root_only = {path.name for path in PKG.glob("*.py")}
    assert not (root_only & {Path(f).name for f in MOVED_MODULE_FILES}), (
        "moved modules reappeared at the package root — recheck the F1 scan pins"
    )


def test_checker_catches_an_upward_import() -> None:
    # a synthetic core module importing pipeline must be flagged...
    bad = layer_violations(
        "computer_use_mcp.core.synthetic",
        "from ..pipeline.agent import ComputerUseAgent\n",
    )
    assert bad and all("core -> pipeline" in v for v in bad)
    assert any("computer_use_mcp.pipeline.agent" in v for v in bad)
    # ...including the function-level lazy form and the absolute form
    assert layer_violations(
        "computer_use_mcp.core.synthetic",
        "def f():\n    import computer_use_mcp.pipeline.validator\n",
    )
    # ...and the string-import forms (F4), including the alias forms
    # (VAL-WP-R1-02 nit): `import importlib as il` and
    # `from importlib import import_module [as im]`
    assert layer_violations(
        "computer_use_mcp.core.synthetic",
        "import importlib\n\n\ndef f():\n"
        "    importlib.import_module('computer_use_mcp.pipeline.validator')\n",
    )
    assert layer_violations(
        "computer_use_mcp.core.synthetic",
        "import importlib as il\n\n\ndef f():\n"
        "    il.import_module('computer_use_mcp.pipeline.validator')\n",
    )
    assert layer_violations(
        "computer_use_mcp.core.synthetic",
        "from importlib import import_module\n\n\ndef f():\n"
        "    import_module('computer_use_mcp.pipeline.validator')\n",
    )
    assert layer_violations(
        "computer_use_mcp.core.synthetic",
        "from importlib import import_module as im\n\n\ndef f():\n"
        "    im('computer_use_mcp.pipeline.validator')\n",
    )
    # a non-importlib `il.import_module` stays unflagged (no false positive)
    assert layer_violations(
        "computer_use_mcp.core.synthetic",
        "class il:\n"
        "    @staticmethod\n"
        "    def import_module(name):\n"
        "        return None\n\n\n"
        "def f():\n"
        "    il.import_module('computer_use_mcp.pipeline.validator')\n",
    ) == []
    assert layer_violations(
        "computer_use_mcp.security.synthetic",
        "from ..vision.observation import ObservationEngine\n"
        "__import__('computer_use_mcp.vision.evidence')\n",
    )
    # downward / same-layer imports stay clean (string imports included)
    assert layer_violations(
        "computer_use_mcp.server.synthetic",
        "from ..core.models import Observation\nfrom ..security.safety import SafetyPolicy\n"
        "importlib.import_module('computer_use_mcp.core.state')\n",
    ) == []
    assert layer_violations(
        "computer_use_mcp.pipeline.synthetic",
        "from ..pipeline.grounding import GroundingRouter\n",
    ) == []


def test_checker_catches_a_nonempty_subpackage_init() -> None:
    assert init_violations("from .models import Observation\n") != []
    assert init_violations("__version__ = '1'\n") != []
    assert init_violations('"""docstring only."""\n') == []
    assert init_violations("") == []


def test_every_layer_is_present_in_the_package() -> None:
    for subpackage in SUBPACKAGES:
        assert (PKG / subpackage).is_dir(), subpackage
    for root_mod, layer in _ROOT_MODULE_LAYER.items():
        assert (PKG / f"{root_mod}.py").is_file(), f"{root_mod} pinned as {layer}"
