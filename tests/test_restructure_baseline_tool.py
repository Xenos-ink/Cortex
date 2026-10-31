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
import json
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
        "import computer_use_mcp.core.limits\n"
        "from computer_use_mcp.vision import observation\n"
        "from computer_use_mcp.core.models import Observation\n"
        "def helper():\n"
        "    lim = computer_use_mcp.core.limits.Limits()\n"
        "    eng = observation.ObservationEngine(None)\n"
        "    return lim, eng, Observation\n"
    )
    # "another module": aliased differently, but the same resolved references
    src_b = (
        "import computer_use_mcp.core.limits as limmod\n"
        "from computer_use_mcp.vision import observation as obsmod\n"
        "from computer_use_mcp.core.models import Observation\n"
        "def helper():\n"
        "    lim = limmod.Limits()\n"
        "    eng = obsmod.ObservationEngine(None)\n"
        "    return lim, eng, Observation\n"
    )
    assert _hash_source(tool, src_a) == _hash_source(tool, src_b)


def test_changed_body_changes_hash(tool) -> None:
    src_a = (
        "import computer_use_mcp.core.limits\n"
        "def helper():\n"
        "    return computer_use_mcp.core.limits.Limits()\n"
    )
    src_b = (
        "import computer_use_mcp.core.limits\n"
        "def helper():\n"
        "    return computer_use_mcp.core.limits.Limits(min_screenshot_interval_ms=1)\n"
    )
    assert _hash_source(tool, src_a) != _hash_source(tool, src_b)


def test_import_line_only_change_same_hash(tool) -> None:
    body = "def helper():\n    return Limits()\n"
    src_a = "import computer_use_mcp.core.limits\n" + body
    src_b = (
        "import os\n"
        "import computer_use_mcp.core.limits as lm\n"
        "from computer_use_mcp.core.state import TaskState\n"
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


# --- inventory diff + move-table verification (WP-R1 DELTA 1 / F2) ----------


def _sym(module: str, name: str, kind: str = "def", h: str = "h1") -> dict:
    return {"module": module, "name": name, "kind": kind, "hash": h}


def _write_inventory(tmp_path: Path, fname: str, symbols: list[dict]) -> str:
    path = tmp_path / fname
    path.write_text(json.dumps({"symbols": symbols}), encoding="utf-8")
    return str(path)


def _run_diff(tool, before: str, after: str, out: str, move_table: str | None = None) -> dict:
    argv = ["diff", before, after, "--out", out]
    if move_table:
        argv += ["--move-table", move_table]
    tool.main(argv)
    return json.loads(Path(out).read_text(encoding="utf-8"))


def test_diff_verifies_moved_symbols_against_the_move_table(tool, tmp_path) -> None:
    before = _write_inventory(tmp_path, "before.json", [_sym("pkg.old", "f")])
    after = _write_inventory(tmp_path, "after.json", [_sym("pkg.new", "f")])
    table = tmp_path / "table.json"
    table.write_text(json.dumps({"pkg.old": "pkg.new"}), encoding="utf-8")
    report = _run_diff(tool, before, after, str(tmp_path / "report.json"), str(table))
    assert report["missing"] == []
    assert report["moved_count"] == 1
    assert report["moved_mismatch_count"] == 0
    assert report["moved_verified_by_table"] == 1
    assert report["moved"][0]["to_module"] == "pkg.new"


def test_diff_reports_a_swapped_pair_against_the_move_table(tool, tmp_path) -> None:
    """Two symbols whose actual landing modules are swapped vs the table must
    be reported as MOVE-MISMATCH (exit 1) — identical-hash pairing may not be
    matched first-in-first-out across unrelated modules."""
    before = _write_inventory(
        tmp_path, "before.json",
        [_sym("pkg.m1", "f", h="hA"), _sym("pkg.m2", "g", h="hB")],
    )
    # swapped landings: f goes where the table sends g, and vice versa
    after = _write_inventory(
        tmp_path, "after.json",
        [_sym("pkg.n2", "f", h="hA"), _sym("pkg.n1", "g", h="hB")],
    )
    table = tmp_path / "table.json"
    table.write_text(json.dumps({"pkg.m1": "pkg.n1", "pkg.m2": "pkg.n2"}), encoding="utf-8")
    report = _run_diff(tool, before, after, str(tmp_path / "report.json"), str(table))
    assert report["moved_mismatch_count"] == 2
    expected = {("pkg.m1", "pkg.n1"), ("pkg.m2", "pkg.n2")}
    got = {(p["from_module"], p["expected_module"]) for p in report["moved_mismatch"]}
    assert got == expected
    assert all(p["to_module"] != p["expected_module"] for p in report["moved_mismatch"])


def test_diff_without_table_still_reports_moves(tool, tmp_path) -> None:
    before = _write_inventory(tmp_path, "before.json", [_sym("a.x", "f")])
    after = _write_inventory(tmp_path, "after.json", [_sym("b.y", "f")])
    report = _run_diff(tool, before, after, str(tmp_path / "report.json"))
    assert report["moved_count"] == 1
    assert report["moved_mismatch_count"] == 0
    assert report["missing"] == []
