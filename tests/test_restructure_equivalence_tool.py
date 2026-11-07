"""WP-R3 unit tests for tools/restructure/equivalence.py pure parts.

Covers the comparators (inventory-diff classification against the reviewed
allow-list), the rename mapping (node ids, name-status parsing), the test-diff
residue classifier, the bootstrap statistics and the perf verdict rule, and the
statement-allow-list derivation over a synthetic mini-package.

No subprocesses, no desktop contact, no product imports: the tool module is
loaded from its path exactly like tests/test_restructure_baseline_tool.py does.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools" / "restructure"
EQUIVALENCE_PY = TOOLS_DIR / "equivalence.py"
BASELINE_PY = TOOLS_DIR / "baseline.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def eq():
    return _load("restructure_equivalence_tool", EQUIVALENCE_PY)


@pytest.fixture(scope="module")
def baseline_tool():
    return _load("restructure_baseline_tool_for_eq", BASELINE_PY)


# --- name-status parsing + node-id rename mapping --------------------------

def test_parse_name_status_renames_added_modified(eq) -> None:
    text = (
        "R100\ttests/old.py\ttests/new.py\n"
        "A\ttests/test_guard.py\n"
        "M\ttests/test_keep.py\n"
        "M\tbenchmarks/runner.py\n"
    )
    renames, added, modified = eq.parse_name_status(text)
    assert renames == {"tests/old.py": "tests/new.py"}
    assert added == {"tests/test_guard.py"}
    assert modified == {"tests/test_keep.py", "benchmarks/runner.py"}


def test_map_node_ids_renames_prefixes_and_reports_removed_added(eq) -> None:
    before = [
        "tests/old.py::test_a",
        "tests/old.py::test_b[param]",
        "tests/keep.py::test_c",
    ]
    after = [
        "tests/new.py::test_a",
        "tests/new.py::test_b[param]",
        "tests/keep.py::test_c",
        "tests/test_new_guard.py::test_d",
    ]
    out = eq.map_node_ids(before, after, {"tests/old.py": "tests/new.py"})
    assert out["removed"] == []
    assert out["added"] == ["tests/test_new_guard.py::test_d"]


def test_map_node_ids_removed_id_outside_renames_is_reported(eq) -> None:
    before = ["tests/gone.py::test_x", "tests/old.py::test_a"]
    after = ["tests/new.py::test_a"]
    out = eq.map_node_ids(before, after, {"tests/old.py": "tests/new.py"})
    assert out["removed"] == ["tests/gone.py::test_x"]


# --- inventory classification against the reviewed allow-list ---------------

def test_classify_accepts_the_reviewed_deviation_set(eq) -> None:
    report = {
        "missing": [{"module": "computer_use_mcp.server", "kind": "assign",
                     "name": "logger", "hash": "h1"}],
        "added": [
            {"module": "computer_use_mcp.server", "kind": "def",
             "name": "main", "hash": "h2"},
            {"module": "computer_use_mcp.server.instance", "kind": "assign",
             "name": "logger", "hash": "h3"},
        ],
        "symbol_duplicated": [
            {"module": "computer_use_mcp.server", "kind": "def",
             "name": "main", "hash": "h2", "extra_count": 1},
        ],
        "symbol_map_mismatch": [{"from_module": "computer_use_mcp.server",
                                 "name": "logger", "kind": "assign"}],
        "changed": [], "moved_mismatch": [], "symbol_map_unknown_keys": [],
        "statement_order_violations": [],
        "statement_missing": [{"module": "computer_use_mcp.backend", "kind": "If",
                               "name": "stmt@fc4bb3c1", "hash": "h4",
                               "ordinal": 12, "expected_module": "win32"}],
        "statement_added": [{"module": "computer_use_mcp.desktop.win32", "kind": "If",
                             "name": "stmt@36c01571", "hash": "h5", "extra_count": 1}],
    }
    cls = eq.classify_inventory_report(
        report,
        expected_stmt_missing=[{"module": "computer_use_mcp.backend", "kind": "If",
                                "name": "stmt@fc4bb3c1", "hash": "h4"}],
        expected_stmt_added=[{"module": "computer_use_mcp.desktop.win32", "kind": "If",
                              "name": "stmt@36c01571", "hash": "h5"}],
    )
    assert cls["unexpected_total"] == 0
    assert cls["changed"] == []


def test_classify_flags_an_unlisted_change(eq) -> None:
    report = {
        "missing": [], "added": [], "symbol_duplicated": [],
        "symbol_map_mismatch": [], "statement_missing": [], "statement_added": [],
        "changed": [{"before": {"module": "computer_use_mcp.core.models",
                                "kind": "def", "name": "f"},
                     "after": {"module": "computer_use_mcp.core.models",
                               "kind": "def", "name": "f"}}],
        "moved_mismatch": [], "symbol_map_unknown_keys": [],
        "statement_order_violations": [],
    }
    cls = eq.classify_inventory_report(report, [], [])
    assert cls["unexpected_total"] == 1


def test_classify_flags_an_unexpected_added_symbol(eq) -> None:
    report = {
        "missing": [], "added": [{"module": "computer_use_mcp.some_mod",
                                  "kind": "def", "name": "extra", "hash": "h"}],
        "symbol_duplicated": [], "symbol_map_mismatch": [],
        "statement_missing": [], "statement_added": [], "changed": [],
        "moved_mismatch": [], "symbol_map_unknown_keys": [],
        "statement_order_violations": [],
    }
    cls = eq.classify_inventory_report(report, [], [])
    assert cls["added_unexpected"] == report["added"]
    assert cls["unexpected_total"] == 1


# --- symbol-map merging ------------------------------------------------------

def test_merge_symbol_maps_precedence_and_defaults(eq) -> None:
    move_table = {"computer_use_mcp.models": "computer_use_mcp.core.models"}
    maps = [
        {"computer_use_mcp.backend:Foo": "computer_use_mcp.desktop.launch"},
        {"computer_use_mcp.backend": "computer_use_mcp.desktop.backend",
         "computer_use_mcp.server:bar": "computer_use_mcp.server.wait"},
    ]
    merged = eq.merge_symbol_maps(move_table, maps)
    assert merged["computer_use_mcp.models"] == "computer_use_mcp.core.models"
    assert merged["computer_use_mcp.backend:Foo"] == "computer_use_mcp.desktop.launch"
    assert merged["computer_use_mcp.backend"] == "computer_use_mcp.desktop.backend"
    assert merged["computer_use_mcp.server"] == eq.MODULE_DEFAULTS["computer_use_mcp.server"]
    assert merged["computer_use_mcp.server:bar"] == "computer_use_mcp.server.wait"


# --- test-diff residue classifier -------------------------------------------

def test_diff_residue_drops_import_retargets_and_fixture_pairs(eq) -> None:
    diff = """diff --git a/tests/test_x.py b/tests/test_x.py
--- a/tests/test_x.py
+++ b/tests/test_x.py
@@ -1,6 +1,6 @@
-import computer_use_mcp.models
-from computer_use_mcp.backend import FakeComputerBackend
+import computer_use_mcp.core.models
+from computer_use_mcp.desktop.fake_backend import FakeComputerBackend
 def fixture():
-    return server
+    return None
     yield
-    value = compute(1)
+    value = compute(2)
"""
    residue = eq.diff_residue(diff)
    assert set(residue) == {"tests/test_x.py"}
    # residue lines are stored in their normalized (stripped) form, sorted
    assert residue["tests/test_x.py"]["unexpected"] == \
        ["+ value = compute(2)", "- value = compute(1)"]


def test_diff_residue_ignores_added_files(eq) -> None:
    diff = """diff --git a/tests/test_new_guard.py b/tests/test_new_guard.py
--- /dev/null
+++ b/tests/test_new_guard.py
@@ -0,0 +1,2 @@
+def test_fresh():
+    assert True
"""
    assert eq.diff_residue(diff, added_files={"tests/test_new_guard.py"}) == {}


def test_diff_residue_handles_yield_server_fixture_pair(eq) -> None:
    diff = """diff --git a/tests/test_y.py b/tests/test_y.py
--- a/tests/test_y.py
+++ b/tests/test_y.py
@@ -1,3 +1,3 @@
 def fresh():
-    yield server
+    yield
"""
    assert eq.diff_residue(diff) == {}


def test_normalize_modpath_keeps_the_tail_component(eq) -> None:
    assert eq.normalize_modpath(
        'patch("computer_use_mcp.desktop.backend._DxgiDuplicator")'
    ) == eq.normalize_modpath('patch("computer_use_mcp.backend._DxgiDuplicator")')


# --- bootstrap statistics + perf verdict rule -------------------------------

def test_bootstrap_ci_of_identical_series_is_one(eq) -> None:
    before = [10.0] * 40
    after = [10.0] * 40
    lo, hi = eq.bootstrap_ratio_ci(before, after, resamples=300)
    assert lo <= 1.0 <= hi
    assert hi <= 1.02


def test_bootstrap_ci_detects_a_true_regression(eq) -> None:
    before = [10.0, 10.1, 9.9, 10.0, 10.05, 10.0] * 10
    after = [13.0, 13.1, 12.9, 13.0, 13.05, 13.0] * 10
    lo, hi = eq.bootstrap_ratio_ci(before, after, resamples=500)
    assert lo > 1.02
    assert eq.perf_verdict(lo, hi, 1.3) == "REGRESSION"


def test_perf_verdict_rules(eq) -> None:
    assert eq.perf_verdict(0.98, 1.01, 1.0) == "PASS"
    assert eq.perf_verdict(0.98, 1.05, 1.01) == "NOISE"
    assert eq.perf_verdict(1.03, 1.06, 1.04) == "REGRESSION"


def test_bootstrap_is_seeded_and_deterministic(eq) -> None:
    before = [5.0, 6.0, 7.0, 8.0] * 10
    after = [5.1, 6.1, 7.1, 8.1] * 10
    assert eq.bootstrap_ratio_ci(before, after) == eq.bootstrap_ratio_ci(before, after)


# --- statement allow-list derivation over a synthetic package ---------------

def test_derive_expected_statement_deltas(tmp_path, eq, baseline_tool) -> None:
    src = tmp_path / "src" / "computer_use_mcp"
    (src / "backend").mkdir(parents=True)
    (src / "win32.py").write_text(
        "import ctypes\n"
        "IS_WINDOWS = True\n"
        "if IS_WINDOWS:\n"
        "    _USER32 = ctypes.windll.user32\n"
        "else:\n"
        "    _USER32 = None\n",
        encoding="utf-8",
    )
    (src / "capture.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from computer_use_mcp.backend.win32 import _USER32\n",
        encoding="utf-8",
    )
    before_tree = tmp_path / "before"
    b_src = before_tree / "src" / "computer_use_mcp"
    b_src.mkdir(parents=True)
    (b_src / "backend.py").write_text(
        "import ctypes\n"
        "IS_WINDOWS = True\n"
        "if IS_WINDOWS:\n"
        "    _USER32 = ctypes.windll.user32\n",
        encoding="utf-8",
    )
    derived = eq.derive_expected_statement_deltas(before_tree, tmp_path)
    assert derived["type_checking_block_count"] == 1
    assert len(derived["before_is_windows_blocks"]) == 1
    assert derived["before_is_windows_blocks"][0]["module"] == "computer_use_mcp.backend"
    assert len(derived["expected_stmt_missing"]) == 1
    assert len(derived["after_is_windows_blocks"]) == 1
    assert derived["after_is_windows_blocks"][0]["module"] == "computer_use_mcp.win32"
    # the two If hashes differ exactly because of the added else: branch
    assert derived["expected_stmt_missing"][0]["hash"] != \
        derived["after_is_windows_blocks"][0]["hash"]
    # the TYPE_CHECKING If is part of the expected statement additions
    added_hashes = {e["hash"] for e in derived["expected_stmt_added"]}
    assert derived["after_is_windows_blocks"][0]["hash"] in added_hashes
    assert len(derived["expected_stmt_added"]) == 2


def test_derivation_uses_baseline_normalizer(tmp_path, eq, baseline_tool) -> None:
    """The derived hash equals what baseline.normalized_hash produces."""
    src = tmp_path / "src" / "computer_use_mcp"
    src.mkdir(parents=True)
    (src / "m.py").write_text("from typing import TYPE_CHECKING\n"
                              "if TYPE_CHECKING:\n    X = 1\n", encoding="utf-8")
    derived = eq.derive_expected_statement_deltas(tmp_path, tmp_path)
    tree = ast.parse("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    X = 1\n")
    digest = baseline_tool.normalized_hash(
        tree.body[1], baseline_tool.collect_module_ids(), {})
    assert derived["before_is_windows_blocks"] == []
    assert derived["type_checking_block_count"] == 1
    assert len(derived["expected_stmt_added"]) == 1
    assert derived["expected_stmt_added"][0]["hash"] == digest


# --- misc pure helpers -------------------------------------------------------

def test_module_file_map_of_a_tree(tmp_path, eq) -> None:
    pkg = tmp_path / "src" / "computer_use_mcp"
    (pkg / "core").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core" / "limits.py").write_text("X = 1\n", encoding="utf-8")
    mapping = eq.module_file_map(tmp_path)
    assert mapping == {
        "computer_use_mcp": "computer_use_mcp/__init__.py",
        "computer_use_mcp.core": "computer_use_mcp/core/__init__.py",
        "computer_use_mcp.core.limits": "computer_use_mcp/core/limits.py",
    }


def test_entry_matching_is_subset_based(eq) -> None:
    actual = {"module": "m", "kind": "If", "name": "stmt@ab", "hash": "h",
              "ordinal": 3, "expected_module": "win32", "extra_count": 1}
    assert eq._entry_matches(actual, {"module": "m", "hash": "h"})
    assert not eq._entry_matches(actual, {"module": "m", "hash": "other"})


def test_fixture_normalization_table(eq) -> None:
    assert eq.normalize_diff_line("-", "return server") == "return None"
    assert eq.normalize_diff_line("+", "return None") == "return None"
    assert eq.normalize_diff_line("-", "yield server") == "yield"


def test_drop_imports_handles_parenthesised_from_imports(eq) -> None:
    lines = [
        "from computer_use_mcp.core.limits import (",
        "    Limits,",
        ")",
        "x = Limits()",
    ]
    assert eq._drop_imports(lines) == ["x = Limits()"]


def test_expected_constants_are_the_reviewed_ones(eq) -> None:
    assert eq.EXPECTED_TOOLS_SHA == (
        "eda8b72457deff6d21660e640638009b8ea42eea91d345ff59a07e6deac6c0c5")
    assert eq.BEFORE_GIT == "bf21ae0" and eq.AFTER_GIT == "9227c4b"
    assert eq.ALLOWED_MISSING == [{"module": "computer_use_mcp.server",
                                   "kind": "assign", "name": "logger"}]
    assert len(eq.ALLOWED_ADDED) == 2
    assert eq.MODULE_DEFAULTS["computer_use_mcp.server"] == \
        "computer_use_mcp.server.plan_surface"


def test_raw_counts_keeps_distinct_string_code_objects_separate(eq) -> None:
    """F1b: two distinct code objects sharing filename/firstlineno/name (the
    dataclass <string>:2(__init__) case) must be counted SEPARATELY from RAW
    profiler entries — pstats's label dict would collapse them last-wins."""
    from types import SimpleNamespace
    code_a = SimpleNamespace(co_filename="<string>", co_firstlineno=2,
                             co_name="__init__", co_qualname="A.__init__")
    code_b = SimpleNamespace(co_filename="<string>", co_firstlineno=2,
                             co_name="__init__", co_qualname="B.__init__")
    entries = [SimpleNamespace(code=code_a, callcount=900),
               SimpleNamespace(code=code_b, callcount=300),
               SimpleNamespace(code=code_a, callcount=7)]
    counts = eq.raw_counts_from_entries(entries, "")
    assert counts["<string>:2(A.__init__)"] == 907
    assert counts["<string>:2(B.__init__)"] == 300
    # qualname mismatch keeps them distinct; equal labels would sum
    assert len(counts) == 2


def test_normalize_code_file_strips_tree_and_layers(eq) -> None:
    assert eq.normalize_code_file(
        "D:/Cortex/Cortex/src/computer_use_mcp/pipeline/verification.py",
        "D:/Cortex/Cortex/src/computer_use_mcp/") == "computer_use_mcp/verification.py"
    assert eq.normalize_code_file(
        "C:\\w\\src\\computer_use_mcp\\core\\state.py",
        "C:/w/src/computer_use_mcp/") == "computer_use_mcp/state.py"
    assert eq.normalize_code_file("<string>", "") == "<string>"


def test_diag_round_contamination_guard(eq) -> None:
    assert not eq.diag_round_contaminated(20.0, 40.0)
    assert eq.diag_round_contaminated(250.0, 40.0)
    assert eq.diag_round_contaminated(20.0, 2500.0)


def test_tree_globals_rebinds_run_cwd_default(eq) -> None:
    """Regression guard: baseline.run's cwd DEFAULT is bound at import time;
    _tree_globals must rebind it or checks using run()'s default (nodeids,
    probe) silently run BOTH sides against the AFTER tree."""
    saved_defaults = eq.baseline.run.__defaults__
    saved_pkg = eq.baseline.PKG
    fake = Path("Z:/not-a-real-tree")
    with eq._tree_globals(fake):
        assert eq.baseline.run.__defaults__ == (fake,)
        assert eq.baseline.PKG == fake / "src" / "computer_use_mcp"
    assert eq.baseline.run.__defaults__ == saved_defaults
    assert eq.baseline.PKG == saved_pkg


def test_perf_json_contract_shapes(eq) -> None:
    assert eq.CEILING == 1.02
    assert eq.BOOTSTRAP_RESAMPLES >= 2000
    assert eq.IMPORT_N >= 40
    assert eq.PROFILE_ROUNDS >= 15


# --- VAL-WP-R3 delta: m1/m2/m6/B1/m1-preflight/n1 guards ---------------------

def test_preflight_rejects_same_dir_and_bad_heads(eq) -> None:
    """m1: fail fast on mis-bound trees (tested via a stubbed git runner)."""
    calls = {}

    def stub_git(args, cwd):
        calls.setdefault(tuple(args[:2]), []).append(str(cwd))
        args_str = " ".join(args)
        if "rev-parse" in args_str:
            head = eq.BEFORE_GIT if "wt-before" in str(cwd) else "deadbeef"
            return SimpleNamespace(stdout=head + "\n", returncode=0)
        if "merge-base" in args_str:
            return SimpleNamespace(stdout="", returncode=1)
        if "--ignored" in args_str:
            return SimpleNamespace(stdout="", returncode=0)
        # AFTER porcelain: exactly the WP-R3 files untracked
        lines = "".join(f"?? {f}\n" for f in eq.WP_R3_FILES)
        return SimpleNamespace(stdout=lines, returncode=0)

    problems = eq.preflight(Path("C:/x/wt-before"), Path("D:/x/after"),
                            git_fn=stub_git)
    assert any("AFTER HEAD" in p for p in problems)
    assert not any("same directory" in p for p in problems)


def test_preflight_rejects_identical_dirs(eq) -> None:
    problems = eq.preflight(Path("C:/x/same"), Path("C:/x/same"),
                            git_fn=lambda a, c: SimpleNamespace(stdout="", returncode=0))
    assert any("same directory" in p for p in problems)


def test_patch_extra_requires_reviewed_target(eq) -> None:
    """M2: an added monkeypatch.setattr on an unreviewed target is residue."""
    assert eq._is_patch_extra(
        'monkeypatch.setattr(srv_server_lifecycle, "_bundles", {})')
    assert not eq._is_patch_extra(
        'monkeypatch.setattr(srv_server_lifecycle, "_some_other_attr", {})')


def test_pinned_statement_hashes_are_the_reviewed_ones(eq) -> None:
    assert eq.PINNED_BEFORE_WIN32_IF == (
        "computer_use_mcp.backend",
        "fc4bb3c1001f4bb690b8e082bfb4c0a7bea859d79ef32fa5c8e6c4e8321d6497")
    assert eq.PINNED_AFTER_WIN32_IF == (
        "computer_use_mcp.desktop.win32",
        "36c01571c09949424102a6ca36ced82ceb79a590d4da6b8f3ed0b263b8c4f3f5")
    assert eq.PINNED_TYPE_CHECKING_HASH == (
        "ba9db845e3758de48e312988ae15286ecd3387a2c8fefe3f894abbba4bd0c6fc")
    assert eq.EXPECTED_TYPE_CHECKING_BLOCKS == 10


def test_normalize_code_file_is_segment_anchored(eq) -> None:
    """n1: the layer strip must not substring-match (server.py is NOT a
    server/ layer segment)."""
    assert eq.normalize_code_file(
        "x/src/computer_use_mcp/server/observe.py",
        "x/src/computer_use_mcp/") == "computer_use_mcp/observe.py"
    assert eq.normalize_code_file(
        "x/src/computer_use_mcp/server.py",
        "x/src/computer_use_mcp/") == "computer_use_mcp/server.py"


def test_mirror_reads_no_input(eq) -> None:
    """B1 guard: the perf mirror is screen-READ only. Statically fail if it
    calls any input/execute/clipboard/launch/focus entry point (helper shared
    with the type* prefix coverage)."""
    import ast as _ast
    tree = _ast.parse(
        (Path(__file__).resolve().parents[1] / "tools" / "restructure"
         / "r5_profile_mirror.py").read_text(encoding="utf-8"))
    assert eq.mirror_forbidden_calls(tree) == [], "mirror must stay read-only"


def test_mirror_guard_catches_type_text(eq) -> None:
    """B1/M6: `backend.type_text(...)` (and any type* call) IS caught."""
    import ast as _ast
    tree = _ast.parse("backend.type_text(session, 'x')\n"
                      "backend.execute(action)\n"
                      "read_clipboard()\n"
                      "backend.observe()\n")
    hits = eq.mirror_forbidden_calls(tree)
    assert "type_text" in hits and "execute" in hits and "read_clipboard" in hits
    assert "observe" not in hits


def test_warm_child_env_drops_dontwritebytecode(eq) -> None:
    """M1: the warm env drops PYTHONDONTWRITEBYTECODE only; the per-side
    PYTHONPYCACHEPREFIX stays so the .pyc assertion has a prefix to check."""
    env = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": "cache/before",
           "PYTHONPATH": "src"}
    out = eq.warm_child_env(env)
    assert "PYTHONDONTWRITEBYTECODE" not in out
    assert out["PYTHONPYCACHEPREFIX"] == "cache/before"
    assert out["PYTHONPATH"] == "src"


def test_pyc_written_assertion(eq, tmp_path) -> None:
    """M1: the .pyc assertion fails when no .pyc exists under the prefix."""
    assert not eq._pyc_written(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "agent.cpython-311.pyc").write_bytes(b"x")
    assert eq._pyc_written(tmp_path)


def test_residue_pin_mismatch_fails(eq) -> None:
    """M2: a changed digest or a vanished pinned file fails the check."""
    problems = eq.residue_pin_problems(
        {"tests/test_a.py": "digest1"}, {"tests/test_a.py": "other"},
        allowed=frozenset({"tests/test_a.py"}))
    assert any("PIN mismatch" in p for p in problems)
    problems = eq.residue_pin_problems(
        {}, {"tests/test_a.py": "digest1"},
        allowed=frozenset({"tests/test_a.py"}))
    assert any("no longer has any residue" in p for p in problems)
    assert eq.residue_pin_problems(
        {"tests/test_a.py": "digest1"}, {"tests/test_a.py": "digest1"},
        allowed=frozenset({"tests/test_a.py"})) == []


def test_missing_wheel_module_fails_launch(eq) -> None:
    """M3: a lost module produces a launch problem, not just a record."""
    problems = eq.missing_module_problems(
        [{"tree": "before", "module": "computer_use_mcp.models",
          "expected": "computer_use_mcp/core/models.py"}])
    assert problems and "missing 1 module" in problems[0]
    assert eq.missing_module_problems([]) == []


def test_rerun_done_per_metric_group(eq) -> None:
    """M5: the 2xN rerun is tracked per metric group from rerun-* phases only."""
    phases = {"import-cold": 40, "rerun-profile": 15}
    assert eq.rerun_done_for_metric("mirror.mss_grab.p50", phases)
    assert eq.rerun_done_for_metric("harness.execution_ms.p50", phases)
    assert not eq.rerun_done_for_metric("import_cold_ms", phases)
    assert not eq.rerun_done_for_metric("import_warm_ms", phases)
    phases2 = {"rerun-import-cold": 40, "rerun-import-warm": 40}
    assert eq.rerun_done_for_metric("import_cold_ms", phases2)
    assert eq.rerun_done_for_metric("import_warm_ms", phases2)
    assert not eq.rerun_done_for_metric("mirror.mss_grab.p50", phases2)


def test_perf_verdict_pass_and_regression_clauses_and_straddle(eq) -> None:
    """NIT 11: asserts the PASS clause (CI hi <= ceiling), the REGRESSION
    clause (CI lo > ceiling) and that a straddling CI is NOISE regardless of
    the median (the contract has no fourth verdict)."""
    assert eq.perf_verdict(0.99, 1.01, 1.05) == "PASS"
    assert eq.perf_verdict(0.99, 1.05, 0.99) == "NOISE"
    assert eq.perf_verdict(0.99, 1.05, 1.05) == "NOISE"
    assert eq.perf_verdict(1.03, 1.06, 1.04) == "REGRESSION"


def test_ms_to_us(eq) -> None:
    """M6 unit fix: D4 harness p50 in ms converts to ~21 us, not ~0."""
    assert eq.ms_to_us(0.0205) == 20.5


def test_normalize_d5_func(eq) -> None:
    """m3: 0x addresses and r3-diag child paths normalize to stable names."""
    a = eq.normalize_d5_func(
        "<function socket.close at 0x0000020A80828180>")
    b = eq.normalize_d5_func(
        "<function socket.close at 0x0000016B8C6880E0>")
    assert a == b
    c = eq.normalize_d5_func(
        "C:/Users/x/AppData/Local/Temp/r3-diag-abc12345/_diag_child.py(observe)")
    assert c.startswith("(diag_child)/") and "observe" in c


def test_preflight_rejects_dirty_before_and_wrong_before_head(eq) -> None:
    """m1: the remaining preflight branches."""
    def stub(args, cwd):
        s = " ".join(args)
        if "rev-parse" in s:
            return SimpleNamespace(stdout="bf21ae0\n", returncode=0)
        if "--ignored" in s:
            return SimpleNamespace(stdout=" M some/file.py\n", returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    problems = eq.preflight(Path("C:/x/wt-before"), Path("D:/x/after"), git_fn=stub)
    assert any("not clean incl. ignored" in p for p in problems)

    def stub2(args, cwd):
        s = " ".join(args)
        if "rev-parse" in s:
            return SimpleNamespace(stdout="deadbf21\n", returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    problems = eq.preflight(Path("C:/x/wt-before"), Path("D:/x/after"), git_fn=stub2)
    assert any("BEFORE HEAD" in p for p in problems)


def test_preflight_accepts_after_git_as_ancestor_with_wp_only_diff(eq) -> None:
    """m1/R: after landing, HEAD > 9227c4b is fine when 9227c4b is an ancestor
    and the HEAD diff touches only WP-R3 files."""

    def stub(args, cwd):
        s = " ".join(args)
        if "rev-parse" in s:
            return SimpleNamespace(stdout="ff009227\n", returncode=0)
        if "merge-base" in s:
            return SimpleNamespace(stdout="", returncode=0)
        if "diff" in s and "name-only" in s:
            return SimpleNamespace(
                stdout="tools/restructure/equivalence.py\ntests/test_restructure_equivalence_tool.py\n",
                returncode=0)
        if "--ignored" in s:
            return SimpleNamespace(stdout="", returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    problems = eq.preflight(Path("C:/x/wt-before"), Path("D:/x/after"), git_fn=stub)
    assert not any("AFTER HEAD" in p for p in problems)


def test_preflight_rejects_ancestor_diff_beyond_wp_files(eq) -> None:
    def stub(args, cwd):
        s = " ".join(args)
        if "rev-parse" in s:
            return SimpleNamespace(stdout="ff009227\n", returncode=0)
        if "merge-base" in s:
            return SimpleNamespace(stdout="", returncode=0)
        if "diff" in s and "name-only" in s:
            return SimpleNamespace(stdout="src/computer_use_mcp/server/app.py\n",
                                   returncode=0)
        if "--ignored" in s:
            return SimpleNamespace(stdout="", returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    problems = eq.preflight(Path("C:/x/wt-before"), Path("D:/x/after"), git_fn=stub)
    assert any("non-WP-R3 changes" in p for p in problems)


def test_tracked_inputs_match_pinned_sha256(eq) -> None:
    """R: the tracked inputs match the sha256s PINNED IN CODE over
    line-ending-normalized bytes (no silent skip; CRLF-insensitive per the
    autocrlf finding)."""
    base = Path(__file__).resolve().parents[1]
    for rel, expected in eq.PINNED_INPUT_SHA256.items():
        f = base / "tools" / "restructure" / rel
        assert f.exists(), f"missing tracked input: {rel}"
        assert eq.normalized_text_sha256(f.read_bytes()) == expected, rel
    assert eq.verify_tracked_inputs() == []


def test_checks_bind_each_tree_via_tree_globals(eq) -> None:
    """m6: every check function must call _tree_globals (source scan), so a
    check can never silently run both sides against the same tree."""
    import inspect
    for name in ("check_inventory", "check_nodeids", "check_counts",
                 "check_tools", "check_probe", "check_launch"):
        src = inspect.getsource(getattr(eq, name))
        assert "_tree_globals" in src, f"{name} does not bind its trees"


def test_diag_rounds_call_the_contamination_guard(eq) -> None:
    """m6: the guard must be called at its call site, not merely defined."""
    import inspect
    src = inspect.getsource(eq._diag_stage_rounds)
    assert "diag_round_contaminated(" in src


def test_tree_globals_rebinds_repo_and_src(eq) -> None:
    fake = Path("Z:/not-a-real-tree")
    saved = (eq.baseline.REPO, eq.baseline.SRC, eq.baseline.PKG)
    with eq._tree_globals(fake):
        assert eq.baseline.REPO == fake
        assert eq.baseline.SRC == fake / "src"
        assert eq.baseline.PKG == fake / "src" / "computer_use_mcp"
    assert (eq.baseline.REPO, eq.baseline.SRC, eq.baseline.PKG) == saved


def test_classify_d5_rows_pairs_and_polls(eq) -> None:
    rows = [
        {"func": "computer_use_mcp/backend.py(observe)", "before": 900,
         "after": 0, "delta": -900},
        {"func": "computer_use_mcp/fake_backend.py(observe)", "before": 0,
         "after": 900, "delta": 900},
        {"func": "<built-in method time.monotonic>()", "before": 10,
         "after": 6, "delta": -4},
        {"func": "computer_use_mcp/backend.py(real_change)", "before": 10,
         "after": 50, "delta": 40},
    ]
    cls = eq.classify_d5_rows(rows)
    assert cls["split_move_pairs"] == 1
    assert cls["timing_lock_poll"] == 1
    assert len(cls["unclassified"]) == 1
    assert cls["unclassified"][0]["func"].endswith("real_change)")


def test_summary_never_defaults_to_pass_on_missing(eq, tmp_path) -> None:
    """M4: `--only report` on an empty dir must NOT print PASS."""
    summary = eq.write_summary_and_report(tmp_path, results={}, meta={
        "before": "b", "after": "a"})
    assert summary["overall"] == "INCOMPLETE"


def test_summary_stops_on_noise_unresolved(eq, tmp_path) -> None:
    results = {"inventory": {"verdict": "PASS", "evidence": []},
               "perf": {"verdict": "NOISE-UNRESOLVED", "evidence": [],
                        "noise_unresolved": [{"metric": "m",
                                              "ratio": 1.015,
                                              "ci95": [1.0, 1.048],
                                              "n": [40, 40]}]}}
    summary = eq.write_summary_and_report(tmp_path, results=results,
                                          meta={"before": "b", "after": "a"})
    assert summary["overall"] == "STOP"
    assert "noise_unresolved" in summary


def test_report_regeneration_preserves_diagnosis(eq, tmp_path) -> None:
    """n2: regeneration must keep an existing '## Diagnosis' section."""
    tmp_path.joinpath("report.md").write_text(
        "# WP-R3 equivalence report\n\n## Diagnosis\n\n- kept line\n",
        encoding="utf-8")
    eq.write_summary_and_report(tmp_path, results={}, meta={
        "before": "b", "after": "a"})
    text = tmp_path.joinpath("report.md").read_text(encoding="utf-8")
    assert "## Diagnosis" in text and "kept line" in text
    assert text.count("## Diagnosis") == 1


def test_tracked_inputs_exist() -> None:
    """R: the tracked input copies exist (identity vs the pinned sha256 is
    pinned by test_tracked_inputs_match_pinned_sha256; identity vs the .orvex
    sources is additionally compared when that tree is present)."""
    base = Path(__file__).resolve().parents[1]
    pairs = [
        ("tools/restructure/inputs/move_table.json",
         ".orvex/mission/artifacts/restructure/r1/move_table.json"),
        ("tools/restructure/inputs/r2a_symbol_map.json",
         ".orvex/mission/artifacts/restructure/r2a/symbol_map.json"),
        ("tools/restructure/inputs/r2b_symbol_map.json",
         ".orvex/mission/artifacts/restructure/r2b/symbol_map.json"),
    ]
    for tracked, source in pairs:
        t = base / tracked
        assert t.exists(), f"missing tracked input: {tracked}"
        s = base / source
        if s.exists():
            assert hashlib.sha256(t.read_bytes()).hexdigest() == \
                hashlib.sha256(s.read_bytes()).hexdigest(), tracked


# --- VAL-WP-R3-03 delta ------------------------------------------------------

def test_first_pass_records_uses_first_n_samples(eq) -> None:
    """Finding 1: the first pass derives from the FIRST N samples per series
    (40 import, 15 rounds), independent of compute order or later samples."""
    series = {
        "import_cold_ms": {"before": [900.0] * 40 + [1300.0] * 40,
                           "after": [910.0] * 40 + [1310.0] * 40},
        "mirror.mss_grab.p50": {"before": [5.0] * 15 + [9.0] * 15,
                                "after": [5.1] * 15 + [9.1] * 15},
    }
    fp = eq.first_pass_records(series)
    assert fp["import_cold_ms"]["n"] == [40, 40]
    assert fp["import_cold_ms"]["before_median"] == 900.0
    assert fp["mirror.mss_grab.p50"]["before_median"] == 5.0
    # and the FINAL computation over the full series differs from the first
    final = eq._metric_record(series["import_cold_ms"]["before"],
                              series["import_cold_ms"]["after"])
    assert final["before_median"] != fp["import_cold_ms"]["before_median"]


def test_new_generation_archives_and_never_pools(eq, tmp_path) -> None:
    """Finding 2: `new-generation` archives the old series (kept), resets the
    blob, and no gen-1 sample can appear in a gen-2 verdict."""
    old = {"generation": 1, "series": {"import_cold_ms": {
        "before": [111.0, 111.0], "after": [222.0, 222.0]}},
        "rounds_done": 0, "phases": {}}
    (tmp_path / "perf_series.json").write_text(json.dumps(old), encoding="utf-8")
    res = eq.check_perf(tmp_path, tmp_path, tmp_path, phase="new-generation")
    assert res["generation"] == 2
    archived = json.loads(
        (tmp_path / "perf_series.gen1.json").read_text(encoding="utf-8"))
    assert archived["series"]["import_cold_ms"]["before"] == [111.0, 111.0]
    fresh = json.loads(
        (tmp_path / "perf_series.json").read_text(encoding="utf-8"))
    assert fresh["generation"] == 2 and fresh["series"] == {}
    # a verdict computed now can only see the fresh (empty) series
    res2 = eq.check_perf(tmp_path, tmp_path, tmp_path)
    assert res2["verdict"] == "NOT_RUN"
    assert "import_cold_ms" not in json.dumps(
        (tmp_path / "perf.json").read_text(encoding="utf-8"))


def test_first_pass_freeze_is_deterministic(eq, tmp_path) -> None:
    """Finding 1: two computes over the same series yield the SAME
    metrics_first_pass, derived from the first N samples."""
    blob = {"generation": 3, "series": {"import_cold_ms": {
        "before": [900.0] * 40 + [1300.0] * 40,
        "after": [910.0] * 40 + [1310.0] * 40}},
        "rounds_done": 0, "phases": {}}
    (tmp_path / "perf_series.json").write_text(json.dumps(blob), encoding="utf-8")
    r1 = eq.check_perf(tmp_path, tmp_path, tmp_path)
    r2 = eq.check_perf(tmp_path, tmp_path, tmp_path)
    assert r1["metrics_first_pass"] == r2["metrics_first_pass"]
    assert r1["metrics_first_pass"]["import_cold_ms"]["before_median"] == 900.0
    assert r2["metrics_final"]["import_cold_ms"]["before_median"] == 1100.0


def test_overall_verdict_unknown_is_stop(eq) -> None:
    assert eq.overall_verdict(["PASS", "BLAH"]) == "STOP"


def test_overall_verdict_stop_outranks_incomplete(eq) -> None:
    assert eq.overall_verdict(["FAIL", "NOT_RUN"]) == "STOP"
    assert eq.overall_verdict(["REGRESSION", "INCOMPLETE"]) == "STOP"
    assert eq.overall_verdict(["PASS", "INCOMPLETE"]) == "INCOMPLETE"


def test_import_once_source_uses_warm_env_and_pyc_assertion(eq) -> None:
    """Finding 3 (call-site): _import_once must build its env through
    warm_child_env, and the warm phase must apply the .pyc assertion — a
    source scan fails if either call is removed."""
    import inspect
    assert "warm_child_env(" in inspect.getsource(eq._import_once)
    src = inspect.getsource(eq.check_perf)
    assert "_pyc_written(" in src


def test_preflight_rejects_non_wp_status_line(eq) -> None:
    """Finding 4: the working-tree status branch — a status line outside the
    WP files must be refused."""
    def stub(args, cwd):
        s = " ".join(args)
        if "rev-parse" in s:
            return SimpleNamespace(stdout="ff009227\n", returncode=0)
        if "merge-base" in s:
            return SimpleNamespace(stdout="", returncode=0)
        if "diff" in s and "name-only" in s:
            return SimpleNamespace(stdout="", returncode=0)
        if "--ignored" in s:
            return SimpleNamespace(stdout="", returncode=0)
        # AFTER working tree: one line outside the WP files
        return SimpleNamespace(stdout=" M src/computer_use_mcp/server/app.py\n",
                               returncode=0)

    problems = eq.preflight(Path("C:/x/wt-before"), Path("D:/x/after"), git_fn=stub)
    assert any("non-WP-R3 changes" in p for p in problems)


def test_clear_dir_removes_stale_pyc(eq, tmp_path) -> None:
    """Finding 5: the warm phase's clear_dir removes stale .pyc leftovers."""
    sub = tmp_path / "pycache-before"
    sub.mkdir()
    (sub / "old.cpython-311.pyc").write_bytes(b"x")
    eq.clear_dir(sub)
    assert sub.exists() and not any(sub.rglob("*.pyc"))


def test_d5_report_rows_use_normalized_names_and_labels(eq) -> None:
    """Finding 6: the printed D5 rows come from the classifier with
    normalized names and its OWN labels (source scan + behaviour)."""
    import inspect
    src = inspect.getsource(eq._diag_final)
    assert "split-move pair" in src and "nfunc" in src
    rows = [{"func": "<function socket.close at 0x1>", "before": 10,
             "after": 8, "delta": -2},
            {"func": "<function socket.close at 0x2>", "before": 8,
             "after": 10, "delta": 2}]
    cls = eq.classify_d5_rows(rows)
    assert cls["poll_rows"] and all("0xADDR" in r["nfunc"] for r in cls["poll_rows"])


def test_generation_history_includes_gen1_details(eq, tmp_path) -> None:
    """Finding 7: filename-fallback generation, gen-1 STOP ratio+CI, and the
    contamination note appear in the history rows."""
    gen1 = {"generation": None, "verdict": "REGRESSION",
            "regression_metrics": ["harness.execution_ms.p95"],
            "metrics_final": {"harness.execution_ms.p95": {
                "ratio": 1.124, "ci95": [1.027, 1.267]}}}
    (tmp_path / "perf.gen1.json").write_text(json.dumps(gen1), encoding="utf-8")
    # write_summary_and_report renders the history from the archive
    summary = eq.write_summary_and_report(tmp_path, results={}, meta={
        "before": "b", "after": "a"})
    assert summary["overall"] == "INCOMPLETE"
    text = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "generation 1" in text and "1.124" in text and "CONTAMINATED" in text


def test_import_time_section_computed(eq) -> None:
    """VAL-WP-R3-04: the section uses the Commander's wording with numbers
    filled from the artifacts, and the D7 line reports the NET product
    self-time, the control, and the noise floor — no sum_cum_us."""
    perf = {"metrics_final": {
        "import_cold_ms": {"ratio": 1.013, "ci95": [0.979, 1.041],
                           "n": [80, 80], "verdict": "NOISE-UNRESOLVED",
                           "after_median": 1003.8, "before_median": 990.5},
        "import_warm_ms": {"ratio": 1.019, "ci95": [0.996, 1.049],
                           "n": [80, 80], "verdict": "NOISE-UNRESOLVED",
                           "after_median": 900.7, "before_median": 883.9}}}
    d7 = {"rounds_per_side": 10, "importtime_total": {
        "before_median": 893521.5, "after_median": 925880.5, "ratio": 1.036,
        "n": [10, 10], "ci95": [0.896, 1.141]},
        "sigma_self": {"computer_use_mcp_before_us": 163602.5,
                       "computer_use_mcp_after_us": 205709.5,
                       "computer_use_mcp_delta_us": 42107.0,
                       "other_modules_delta_us": 8722.5},
        "noise_floor_median_abs_delta_us": 42.0, "same_name_module_count": 210,
        "only_in_after": {"count": 54},
        "only_in_before": {"count": 13}}
    d3 = {"per_side": {"before": {"cum_modules": 16},
                       "after": {"cum_modules": 32}}}
    text = "\n".join(eq.import_time_section(perf, d7, d3))
    assert "cold +1.3% CI [-2.1%, +4.1%]" in text
    assert "warm +1.9% CI [-0.4%, +4.9%]" in text
    assert "D7 total +3.6% CI" in text
    assert "cold, warm, D7 total lean slower" in text
    assert "product modules' own import time +42.1 ms " \
        "(163.6 -> 205.7 ms) vs other modules +8.7 ms" in text
    assert "very noisy (n 10 per side, CI width 24.5 percentage points)" in text
    assert "AFTER loads 16 more package modules" in text
    assert "not per action" in text
    assert "sum_cum_us" not in text


def test_import_time_section_sign_flips(eq) -> None:
    """Finding 1/2: the lean clause is COMPUTED — it flips when a ratio is
    below 1, and signed percentages print minus signs."""
    perf = {"metrics_final": {
        "import_cold_ms": {"ratio": 0.95, "ci95": [0.9, 1.0],
                           "n": [80, 80], "verdict": "PASS",
                           "after_median": 900.0, "before_median": 990.5},
        "import_warm_ms": {"ratio": 1.019, "ci95": [0.996, 1.049],
                           "n": [80, 80], "verdict": "NOISE-UNRESOLVED",
                           "after_median": 900.7, "before_median": 883.9}}}
    d7 = {"rounds_per_side": 10, "importtime_total": {
        "before_median": 893521.5, "after_median": 840000.0, "ratio": 0.9406,
        "n": [10, 10], "ci95": [0.732, 1.366]},
        "sigma_self": {"computer_use_mcp_before_us": 163602.5,
                       "computer_use_mcp_after_us": 205709.5,
                       "computer_use_mcp_delta_us": 42107.0,
                       "other_modules_delta_us": 8722.5},
        "noise_floor_median_abs_delta_us": 42.0, "same_name_module_count": 210,
        "only_in_after": {"count": 54}, "only_in_before": {"count": 13}}
    text = "\n".join(eq.import_time_section(perf, d7))
    assert "warm lean slower and cold, D7 total lean faster" in text
    assert "cold -5.0% CI [-10.0%, +0.0%]" in text
    assert "D7 total -5.9% CI [-26.8%, +36.6%]" in text


def test_rerun_size_required(eq) -> None:
    """Finding 10: a rerun phase counts only when it reached the 2xN size
    (import 40 samples, profile 15 rounds)."""
    phases = {"rerun-import-cold": 5, "rerun-profile": 5}
    assert not eq.rerun_done_for_metric("import_cold_ms", phases)
    assert not eq.rerun_done_for_metric("mirror.mss_grab.p50", phases)
    phases_full = {"rerun-import-cold": 40, "rerun-profile": 15}
    assert eq.rerun_done_for_metric("import_cold_ms", phases_full)
    assert eq.rerun_done_for_metric("mirror.mss_grab.p50", phases_full)


def test_parse_importtime_stderr(eq) -> None:
    """D7 parser: self/cumulative/module names and the entry total."""
    stderr = (
        "import time: 163 us | 163 us |   math\n"
        "import time: 1200 us | 5000 us | computer_use_mcp\n"
        "import time: 300 us | 20000 us | computer_use_mcp.server.app\n")
    parsed = eq.parse_importtime_stderr(stderr)
    assert parsed["total_us"] == 20000
    assert parsed["modules"]["math"] == {"self_us": 163, "cum_us": 163}
    assert parsed["modules"]["computer_use_mcp.server.app"]["cum_us"] == 20000


def test_clear_dir_called_at_warm_phase_start(eq) -> None:
    """VAL-WP-R3-04 finding 5 (call site): the warm phase must CLEAR the
    per-side pycache prefixes before priming — a source scan fails if the
    clear_dir call is removed from check_perf."""
    import inspect
    src = inspect.getsource(eq.check_perf)
    assert "clear_dir(cache_dir / f\"pycache-{side}\")" in src


def test_diag_d7_source_scans(eq) -> None:
    """VAL-WP-R3-05 finding 4: _diag_stage_d7 must interleave per round
    (side loop INSIDE the rounds loop), prime with a checked return code,
    assert .pyc presence, and report only_in_before."""
    import inspect
    src = inspect.getsource(eq._diag_stage_d7)
    rounds_pos = src.index("for _ in range(rounds)")
    side_pos = src.index("for side, _module, tree in trees", rounds_pos)
    assert side_pos > rounds_pos  # side loop nested inside the rounds loop
    assert "_pyc_written(" in src
    assert "prime.returncode" in src
    assert "only_in_before" in src


def test_d7_sigma_self_reports_residual_and_relabel(eq) -> None:
    """VAL-WP-R3-05 finding 3: the D7 result carries the per-side
    reconciliation residual (unexplained, noise-dominated) and the
    per-module relabeled noise floor."""
    perf = {"metrics_final": {
        "import_cold_ms": {"ratio": 1.013, "ci95": [0.979, 1.041],
                           "n": [80, 80], "verdict": "NOISE-UNRESOLVED",
                           "after_median": 1003.8, "before_median": 990.5},
        "import_warm_ms": {"ratio": 1.019, "ci95": [0.996, 1.049],
                           "n": [80, 80], "verdict": "NOISE-UNRESOLVED",
                           "after_median": 900.7, "before_median": 883.9}}}
    d7 = {"rounds_per_side": 10, "importtime_total": {
        "ratio": 1.036, "n": [10, 10], "ci95": [0.896, 1.141]},
        "sigma_self": {
            "computer_use_mcp_delta_us": 42107.0,
            "computer_use_mcp_before_us": 163602.5,
            "computer_use_mcp_after_us": 205709.5,
            "other_modules_delta_us": 8722.5,
            "reconciliation": {
                "residual_before_us": 254468.0,
                "residual_after_us": 131096.0}},
        "per_module_median_abs_delta_us": 155.0,
        "per_module_note": "per-module only", "same_name_module_count": 617,
        "only_in_after": {"count": 54}, "only_in_before": {"count": 27}}
    text = "\n".join(eq.import_time_section(perf, d7))
    assert "residual (measured total minus Sigma-self) is +254.5 ms BEFORE " \
        "and +131.1 ms AFTER - UNEXPLAINED" in text
    assert "noise-dominated" in text
    assert "per-module only, not comparable to sums" in text


def test_d5_make_agent_artifact_label(eq) -> None:
    """VAL-WP-R3-05 NIT: a +/-1 (diag_child) make_agent row is labeled a
    harmless per-child artifact in the report source."""
    import inspect
    src = inspect.getsource(eq._diag_final)
    assert "harmless per-child artifact" in src
    assert "(diag_child)" in src
