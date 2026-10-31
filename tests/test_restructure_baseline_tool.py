"""WP-R0 normalizer tests for tools/restructure/baseline.py.

Four required properties of the normalized AST hash:
  1. the same function in two modules hashes identically (module-qualified
     references are normalized away);
  2. a changed body changes the hash;
  3. an import-line-only change keeps the hash;
  4. a changed docstring changes the hash.

No subprocesses, no desktop contact: the tool module is loaded from its path
and exercised through its public helpers.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools" / "restructure"
BASELINE_PY = TOOLS_DIR / "baseline.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("restructure_baseline_tool", BASELINE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _load_tool()


def _hash_source(tool, source: str, module_ids: set[str] | None = None) -> str:
    tree = ast.parse(source)
    ids = module_ids if module_ids is not None else tool.collect_module_ids()
    aliases = tool._alias_map(tree, ids)
    for name, kind, node in tool.top_level_symbols(tree):
        if kind == "def" and name == "helper":
            return tool.normalized_hash(node, ids, aliases)
    raise AssertionError("no 'helper' def in test source")


def test_same_function_in_two_modules_same_hash(tool) -> None:
    """Same function body referencing two src modules via different paths."""
    src_a = (
        "import computer_use_mcp.limits\n"
        "from computer_use_mcp import observation\n"
        "from computer_use_mcp.models import Observation\n"
        "def helper():\n"
        "    lim = computer_use_mcp.limits.Limits()\n"
        "    eng = observation.ObservationEngine(None)\n"
        "    return lim, eng, Observation\n"
    )
    # "another module": aliased differently, but the same resolved references
    src_b = (
        "import computer_use_mcp.limits as limmod\n"
        "from computer_use_mcp import observation as obsmod\n"
        "from computer_use_mcp.models import Observation\n"
        "def helper():\n"
        "    lim = limmod.Limits()\n"
        "    eng = obsmod.ObservationEngine(None)\n"
        "    return lim, eng, Observation\n"
    )
    assert _hash_source(tool, src_a) == _hash_source(tool, src_b)


def test_changed_body_changes_hash(tool) -> None:
    src_a = (
        "import computer_use_mcp.limits\n"
        "def helper():\n"
        "    return computer_use_mcp.limits.Limits()\n"
    )
    src_b = (
        "import computer_use_mcp.limits\n"
        "def helper():\n"
        "    return computer_use_mcp.limits.Limits(min_screenshot_interval_ms=1)\n"
    )
    assert _hash_source(tool, src_a) != _hash_source(tool, src_b)


def test_import_line_only_change_same_hash(tool) -> None:
    body = "def helper():\n    return Limits()\n"
    src_a = "import computer_use_mcp.limits\n" + body
    src_b = (
        "import os\n"
        "import computer_use_mcp.limits as lm\n"
        "from computer_use_mcp.state import TaskState\n"
        + body
    )
    assert _hash_source(tool, src_a) == _hash_source(tool, src_b)


def test_changed_docstring_changes_hash(tool) -> None:
    src_a = 'def helper():\n    """Original docstring."""\n    return 1\n'
    src_b = 'def helper():\n    """Rewritten docstring."""\n    return 1\n'
    assert _hash_source(tool, src_a) != _hash_source(tool, src_b)


def test_docstring_is_kept_not_stripped(tool) -> None:
    """The docstring is part of the hash (unlike line/col info)."""
    src_a = 'def helper():\n    """Kept."""\n    return 1\n'
    src_b = "def helper():\n    return 1\n"
    assert _hash_source(tool, src_a) != _hash_source(tool, src_b)


def test_lineno_change_same_hash(tool) -> None:
    src_a = "X = 0\n\n\ndef helper():\n    return 1\n"
    src_b = "\n\n\n\nX = 0\n\n\n\n\ndef helper():\n    return 1\n"
    assert _hash_source(tool, src_a) == _hash_source(tool, src_b)
