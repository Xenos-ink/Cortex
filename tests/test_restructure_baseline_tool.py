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


# --- WP-R2a: top-level statement inventory + --symbol-map + order check -----

_STATEMENT_SOURCE = (
    '"""Module docstring."""\n'
    "import os\n"
    "SIMPLE = 1\n"
    "if os.name == 'nt':\n"
    "    MODE = 'win'\n"
    "_TABLE = {}\n"
    "_TABLE.update({'a': 1})\n"
    "A, B = 1, 2\n"
    "__all__ = ['SIMPLE']\n"
    "try:\n"
    "    import json\n"
    "except ImportError:\n"
    "    json = None\n"
    "COUNTER = 0\n"
    "COUNTER += 1\n"
    "\n\n"
    "def helper():\n"
    "    return SIMPLE\n"
)


def _entries_for(tool, source: str) -> list[dict]:
    tree = ast.parse(source)
    ids = tool.collect_module_ids()
    aliases = tool._alias_map(tree, ids)
    return tool.statement_entries(tree, ids, aliases)


def test_inventory_records_every_non_simple_top_level_statement(tool) -> None:
    entries = _entries_for(tool, _STATEMENT_SOURCE)
    # body ordinals: 0 docstring, 1 import, 2 SIMPLE, 3 if, 4 _TABLE(=),
    # 5 Expr(update), 6 A,B tuple, 7 __all__, 8 try, 9 COUNTER, 10 AugAssign,
    # 12 def — recorded: 3, 5, 6, 7, 8, 10
    assert sorted(e["ordinal"] for e in entries) == [3, 5, 6, 7, 8, 10]
    by_ordinal = {e["ordinal"]: e for e in entries}
    assert by_ordinal[3]["kind"] == "If"
    assert by_ordinal[5]["kind"] == "Expr"
    assert by_ordinal[6]["kind"] == "Assign"
    assert by_ordinal[7]["kind"] == "Assign"  # the __all__ assign
    assert by_ordinal[8]["kind"] == "Try"
    assert by_ordinal[10]["kind"] == "AugAssign"
    # F4: every statement has the stable ADDRESSABLE name stmt@<hash8>
    for e in entries:
        assert e["name"] == f"stmt@{e['hash'][:8]}"
        assert len(e["hash"]) == 64
    assert len({e["name"] for e in entries}) == len(entries)  # addressable
    # simple assigns / def / import / docstring are NOT statement entries...
    assert not any(e["ordinal"] in (0, 1, 2, 4, 9, 12) for e in entries)


def test_statement_hash_tracks_body_and_ignores_position(tool) -> None:
    src_a = "X = 0\nif X:\n    Y = 1\n"
    src_b = "Z = 9\n\n\nX = 0\nif X:\n    Y = 1\n"  # shifted down, body identical
    src_c = "X = 0\nif X:\n    Y = 2\n"  # body edited
    (a,), (b,), (c,) = _entries_for(tool, src_a), _entries_for(tool, src_b), _entries_for(tool, src_c)
    assert a["hash"] == b["hash"] and a["ordinal"] != b["ordinal"]
    assert a["hash"] != c["hash"]


def _write_inventory_with_statements(
    tmp_path: Path, fname: str, symbols: list[dict], statements: list[dict]
) -> str:
    path = tmp_path / fname
    path.write_text(
        json.dumps({"symbols": symbols, "statements": statements}), encoding="utf-8"
    )
    return str(path)


def _stmt(module: str, kind: str, name: str, h: str, ordinal: int) -> dict:
    return {"module": module, "kind": kind, "name": name, "hash": h, "ordinal": ordinal}


def test_diff_symbol_map_verifies_moved_symbols(tool, tmp_path) -> None:
    before = _write_inventory(tmp_path, "before.json", [_sym("pkg.old", "f")])
    after = _write_inventory(tmp_path, "after.json", [_sym("pkg.new", "f")])
    table = tmp_path / "map.json"
    table.write_text(json.dumps({"pkg.old:f": "pkg.new"}), encoding="utf-8")
    rc = tool.main(["diff", before, after, "--out", str(tmp_path / "r.json"),
                    "--symbol-map", str(table)])
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert rc == 0
    assert report["symbol_map_verified"] == 1
    assert report["symbol_map_mismatch_count"] == 0


def test_diff_symbol_map_flags_a_wrong_landing(tool, tmp_path) -> None:
    before = _write_inventory(tmp_path, "before.json", [_sym("pkg.old", "f")])
    after = _write_inventory(tmp_path, "after.json", [_sym("pkg.wrong", "f")])
    table = tmp_path / "map.json"
    table.write_text(json.dumps({"pkg.old:f": "pkg.right"}), encoding="utf-8")
    rc = tool.main(["diff", before, after, "--out", str(tmp_path / "r.json"),
                    "--symbol-map", str(table)])
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert rc == 1
    assert report["symbol_map_mismatch_count"] == 1
    assert report["symbol_map_mismatch"][0]["expected_module"] == "pkg.right"


def test_diff_symbol_map_accepts_the_plan_json_wrapper_shape(tool, tmp_path) -> None:
    before = _write_inventory(tmp_path, "before.json", [_sym("pkg.old", "f")])
    after = _write_inventory(tmp_path, "after.json", [_sym("pkg.new", "f")])
    table = tmp_path / "plan.json"
    table.write_text(
        json.dumps({"symbol_map": {"pkg.old:f": "pkg.new"}, "other": "ignored"}),
        encoding="utf-8",
    )
    rc = tool.main(["diff", before, after, "--out", str(tmp_path / "r.json"),
                    "--symbol-map", str(table)])
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert rc == 0 and report["symbol_map_verified"] == 1


def test_diff_symbol_map_verifies_moved_statements(tool, tmp_path) -> None:
    before_stmts = [_stmt("pkg.old", "If", "", "hIf", 3)]
    after_stmts = [_stmt("pkg.new", "If", "", "hIf", 2)]
    before = _write_inventory_with_statements(tmp_path, "before.json", [], before_stmts)
    after = _write_inventory_with_statements(tmp_path, "after.json", [], after_stmts)
    table = tmp_path / "map.json"
    table.write_text(json.dumps({"pkg.old": "pkg.new"}), encoding="utf-8")
    rc = tool.main(["diff", before, after, "--out", str(tmp_path / "r.json"),
                    "--symbol-map", str(table)])
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert rc == 0
    assert report["statement_missing"] == []
    assert report["statement_moved_per_map"] == 1


def test_diff_reports_a_dropped_statement(tool, tmp_path) -> None:
    """F15: a TRUE drop — one of two BEFORE statements is gone in AFTER."""
    before = _write_inventory_with_statements(
        tmp_path, "before.json", [],
        [_stmt("pkg.m", "If", "stmt@aaaa1111", "hIf", 3),
         _stmt("pkg.m", "Try", "stmt@bbbb2222", "hTry", 8)])
    after = _write_inventory_with_statements(
        tmp_path, "after.json", [], [_stmt("pkg.m", "If", "stmt@aaaa1111", "hIf", 3)])
    rc = tool.main(["diff", before, after, "--out", str(tmp_path / "r.json")])
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert rc == 1
    assert report["statement_missing_count"] == 1
    assert report["statement_missing"][0]["hash"] == "hTry"


def test_diff_reports_a_duplicated_statement(tool, tmp_path) -> None:
    """F3: a copy-instead-of-move (the same Expr twice in AFTER) fails."""
    before = _write_inventory_with_statements(
        tmp_path, "before.json", [],
        [_stmt("pkg.m", "Expr", "stmt@cccc3333", "hUp", 5)])
    after = _write_inventory_with_statements(
        tmp_path, "after.json", [],
        [_stmt("pkg.m", "Expr", "stmt@cccc3333", "hUp", 5),
         _stmt("pkg.m", "Expr", "stmt@cccc3333", "hUp", 6)])
    rc = tool.main(["diff", before, after, "--out", str(tmp_path / "r.json")])
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert rc == 1
    assert report["statement_added_count"] == 1
    assert report["statement_added"][0]["extra_count"] == 1


def test_diff_reports_a_duplicated_symbol(tool, tmp_path) -> None:
    """F3: the same (module, name, kind, hash) twice in AFTER fails."""
    before = _write_inventory(tmp_path, "before.json", [_sym("pkg.m", "f", h="hA")])
    after = _write_inventory(
        tmp_path, "after.json", [_sym("pkg.m", "f", h="hA"), _sym("pkg.m", "f", h="hA")])
    rc = tool.main(["diff", before, after, "--out", str(tmp_path / "r.json")])
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert rc == 1
    assert report["symbol_duplicated_count"] == 1


def test_diff_name_addressed_map_routes_if_and_expr_differently(tool, tmp_path) -> None:
    """F4: `mod:stmt@<hash8>` keys send the If block and the Expr calls to
    DIFFERENT destination modules — the step-1 backend split shape."""
    before_stmts = [
        _stmt("pkg.backend", "If", "stmt@aaaacc11", "hIf", 116),
        _stmt("pkg.backend", "Expr", "stmt@bbbbdd22", "hUp", 151),
        _stmt("pkg.backend", "Expr", "stmt@bbbbdd22", "hUp", 152),
    ]
    after_stmts = [
        _stmt("pkg.win32", "If", "stmt@aaaacc11", "hIf", 2),
        _stmt("pkg.engine", "Expr", "stmt@bbbbdd22", "hUp", 30),
        _stmt("pkg.engine", "Expr", "stmt@bbbbdd22", "hUp", 31),
    ]
    before = _write_inventory_with_statements(
        tmp_path, "before.json", [], before_stmts)
    after = _write_inventory_with_statements(
        tmp_path, "after.json", [], after_stmts)
    table = tmp_path / "map.json"
    table.write_text(json.dumps({
        "pkg.backend:stmt@aaaacc11": "pkg.win32",
        "pkg.backend:stmt@bbbbdd22": "pkg.engine",
    }), encoding="utf-8")
    rc = tool.main(["diff", before, after, "--out", str(tmp_path / "r.json"),
                    "--symbol-map", str(table)])
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert rc == 0
    assert report["statement_missing"] == []
    assert report["statement_moved_per_map"] == 3


def test_diff_order_check_covers_a_new_destination_module(tool, tmp_path) -> None:
    """F5: a swap of two statements inside a NEW module (absent from BEFORE)
    is caught via the symbol-map destination sequence."""
    before_stmts = [
        _stmt("pkg.backend", "Expr", "stmt@bbbbdd22", "hUp", 151),
        _stmt("pkg.backend", "Expr", "stmt@bbbbdd22", "hUp", 152),
        _stmt("pkg.backend", "Expr", "stmt@eeee4444", "hUp2", 153),
    ]
    # after the map moves them to pkg.engine (NEW), the first two are SWAPPED:
    after_stmts = [
        _stmt("pkg.engine", "Expr", "stmt@eeee4444", "hUp2", 30),
        _stmt("pkg.engine", "Expr", "stmt@bbbbdd22", "hUp", 31),
        _stmt("pkg.engine", "Expr", "stmt@bbbbdd22", "hUp", 32),
    ]
    before = _write_inventory_with_statements(
        tmp_path, "before.json", [], before_stmts)
    after = _write_inventory_with_statements(
        tmp_path, "after.json", [], after_stmts)
    table = tmp_path / "map.json"
    table.write_text(json.dumps({"pkg.backend": "pkg.engine"}), encoding="utf-8")
    rc = tool.main(["diff", before, after, "--out", str(tmp_path / "r.json"),
                    "--symbol-map", str(table)])
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert rc == 1
    assert report["statement_order_violation_count"] == 1
    assert report["statement_order_violations"][0]["module"] == "pkg.engine"


def test_diff_fails_on_symbol_map_keys_that_match_nothing(tool, tmp_path) -> None:
    """F14: an unknown/typo'd --symbol-map key fails instead of being ignored."""
    before = _write_inventory(tmp_path, "before.json", [_sym("pkg.old", "f")])
    after = _write_inventory(tmp_path, "after.json", [_sym("pkg.new", "f")])
    table = tmp_path / "map.json"
    table.write_text(json.dumps({
        "pkg.old:f": "pkg.new",           # valid
        "pkg.old:typo_name": "pkg.new",   # symbol-level key: no such symbol
        "pkg.typo_module": "pkg.new",     # module-level key: no such module
    }), encoding="utf-8")
    rc = tool.main(["diff", before, after, "--out", str(tmp_path / "r.json"),
                    "--symbol-map", str(table)])
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert rc == 1
    assert report["symbol_map_unknown_key_count"] == 2
    assert set(report["symbol_map_unknown_keys"]) == {
        "pkg.old:typo_name", "pkg.typo_module"
    }


def test_diff_flags_a_reorder_of_statements_that_stay_together(tool, tmp_path) -> None:
    before = _write_inventory_with_statements(
        tmp_path, "before.json", [],
        [_stmt("pkg.m", "If", "", "hIf", 3), _stmt("pkg.m", "Try", "", "hTry", 8)])
    after = _write_inventory_with_statements(
        tmp_path, "after.json", [],
        [_stmt("pkg.m", "Try", "", "hTry", 3), _stmt("pkg.m", "If", "", "hIf", 8)])
    rc = tool.main(["diff", before, after, "--out", str(tmp_path / "r.json")])
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert rc == 1
    assert report["statement_order_violation_count"] == 1
    assert report["statement_order_violations"][0]["module"] == "pkg.m"


def test_diff_order_check_passes_when_relative_order_is_kept(tool, tmp_path) -> None:
    before = _write_inventory_with_statements(
        tmp_path, "before.json", [],
        [_stmt("pkg.m", "If", "", "hIf", 3), _stmt("pkg.m", "Try", "", "hTry", 8)])
    after = _write_inventory_with_statements(
        tmp_path, "after.json", [],
        [_stmt("pkg.m", "If", "", "hIf", 2), _stmt("pkg.m", "Try", "", "hTry", 9)])
    rc = tool.main(["diff", before, after, "--out", str(tmp_path / "r.json")])
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert rc == 0
    assert report["statement_order_violations"] == []


def test_diff_blobs_without_statements_skip_statement_checks(tool, tmp_path) -> None:
    """Backward compat: the WP-R0/R1 inventory files carry symbols only."""
    before = _write_inventory(tmp_path, "before.json", [_sym("a.x", "f")])
    after = _write_inventory(tmp_path, "after.json", [_sym("a.x", "f")])
    rc = tool.main(["diff", before, after, "--out", str(tmp_path / "r.json")])
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert rc == 0
    assert report["statement_missing_count"] == 0
    assert report["statement_order_violation_count"] == 0
