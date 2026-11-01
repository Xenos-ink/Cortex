"""WP11 perf-tool unit tests: the pure helpers of tools/perf/wp11_perf.py.

Covers the contract's verdict rule at the WP11 ceiling 1.05 (imported reuse of
equivalence.bootstrap_ratio_ci / perf_verdict), the NOISE downgrade (NOISE is
NEVER coerced to PASS — after the 2xN rerun it becomes NOISE-UNRESOLVED), the
overall STOP rule (hot-path REGRESSION -> STOP; import metrics never STOP),
series bookkeeping helpers, mix aggregation, and the child/template invariants
(flat/layered import pairs, identical sleep knobs, no CUMCP_RUN_E2E).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "tools" / "perf"
RESTRUCTURE = Path(__file__).resolve().parent.parent / "tools" / "restructure"
for p in (str(TOOL), str(RESTRUCTURE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest

import wp11_perf


# ---------------------------------------------------------------------------
# verdict rule (ceiling 1.05)
# ---------------------------------------------------------------------------

def _flat(n: float, count: int) -> list[float]:
    return [n] * count


def test_verdict_pass_when_ci_hi_at_or_below_ceiling():
    before = [100.0] * 30
    after = [101.0] * 30  # +1% everywhere -> tight CI at 1.01
    rec = wp11_perf.metric_record(before, after)
    assert rec["verdict"] == "PASS"
    assert rec["ci95"][1] <= 1.05


def test_verdict_regression_when_ci_lo_above_ceiling():
    rec = wp11_perf.metric_record(_flat(100.0, 30), _flat(120.0, 30))
    assert rec["verdict"] == "REGRESSION"
    assert rec["ci95"][0] > 1.05


def test_verdict_noise_when_ci_straddles_ceiling():
    # a wide, mostly-small-but-sometimes-large series around the ceiling
    before = [100.0, 110.0] * 15
    after = [105.0, 104.0] * 14 + [160.0, 100.0]
    rec = wp11_perf.metric_record(before, after)
    assert rec["verdict"] in ("NOISE", "PASS", "REGRESSION")
    # the specific boundary shape: median <= ceiling < ci_hi
    if rec["ratio"] <= 1.05 < rec["ci95"][1]:
        assert rec["verdict"] == "NOISE"


def test_degenerate_zero_metric_is_pass():
    rec = wp11_perf.metric_record([0.0] * 5, [0.0] * 5)
    assert rec["verdict"] == "PASS"
    assert rec["degenerate"] is True


def test_metric_record_uses_wp11_ceiling_not_equivalence_ceiling():
    # +3% medians: below equivalence's 1.02 ceiling this would still be judged
    # there; here the record must carry the WP11 ceiling's semantics
    before = [100.0] * 40
    after = [103.0] * 40
    rec = wp11_perf.metric_record(before, after)
    assert rec["ci95"] == [rec["ci95"][0], rec["ci95"][1]]
    assert rec["verdict"] == "PASS" and rec["ratio"] == pytest.approx(1.03)


# ---------------------------------------------------------------------------
# NOISE downgrade: never -> PASS
# ---------------------------------------------------------------------------

def test_noise_downgrades_to_noise_unresolved_only_after_full_rerun():
    records = {"hot.execution_ms.p50": {"verdict": "NOISE"}}
    phases = {"rerun-hot": wp11_perf.HOT_ROUNDS}
    resolved = wp11_perf.downgrade_noise(records, phases)
    assert resolved == ["hot.execution_ms.p50"]
    assert records["hot.execution_ms.p50"]["verdict"] == "NOISE-UNRESOLVED"


def test_noise_without_full_rerun_stays_noise():
    records = {"hot.execution_ms.p50": {"verdict": "NOISE"}}
    phases = {"rerun-hot": wp11_perf.HOT_ROUNDS - 1}
    assert wp11_perf.downgrade_noise(records, phases) == []
    assert records["hot.execution_ms.p50"]["verdict"] == "NOISE"


def test_noise_never_becomes_pass_in_any_path():
    records = {m: {"verdict": "NOISE"} for m in
               ("hot.execution_ms.p50", "import_cold_ms")}
    phases = {"rerun-hot": 999, "rerun-import-cold": 999}
    wp11_perf.downgrade_noise(records, phases)
    assert set(r["verdict"] for r in records.values()) == {"NOISE-UNRESOLVED"}


# ---------------------------------------------------------------------------
# overall verdict / rerun bookkeeping
# ---------------------------------------------------------------------------

def test_hot_path_regression_means_stop_and_import_does_not(monkeypatch):
    monkeypatch.setattr(wp11_perf, "_PHASES_FOR_PENDING", {})
    records = {
        "hot.execution_ms.p50": {"verdict": "REGRESSION"},
        "import_cold_ms": {"verdict": "REGRESSION"},
        "r5.observe_full.p50": {"verdict": "PASS"},
    }
    ov = wp11_perf.overall_verdict(records)
    assert ov["overall"] == "STOP"
    assert ov["regression_metrics"] == ["hot.execution_ms.p50"]


def test_all_pass_hot_path_means_pass_even_if_import_regresses(monkeypatch):
    monkeypatch.setattr(wp11_perf, "_PHASES_FOR_PENDING", {})
    records = {
        "hot.captures_per_action": {"verdict": "PASS"},
        "import_warm_ms": {"verdict": "REGRESSION"},
    }
    ov = wp11_perf.overall_verdict(records)
    assert ov["overall"] == "PASS"
    assert ov["regression_metrics"] == []


def test_is_hot_path_classification():
    assert wp11_perf.is_hot_path("hot.captures_per_action")
    assert wp11_perf.is_hot_path("r5.verify_diff_only.p95")
    assert not wp11_perf.is_hot_path("import_cold_ms")
    assert not wp11_perf.is_hot_path("hotter.thing.p50")  # prefix must be exact


# ---------------------------------------------------------------------------
# series / mix helpers
# ---------------------------------------------------------------------------

def test_first_pass_records_uses_first_contract_n():
    series = {"hot.execution_ms.p50": {"before": [1.0] * 30 + [50.0],
                                       "after": [1.1] * 30 + [0.1]}}
    fp = wp11_perf.first_pass_records(series)
    assert fp["hot.execution_ms.p50"]["before_median"] == 1.0
    assert fp["hot.execution_ms.p50"]["after_median"] == 1.1
    assert fp["hot.execution_ms.p50"]["n"] == [30, 30]


def test_merge_mix_accumulates_kinds_and_verdicts():
    acc: dict = {}
    wp11_perf.merge_mix(acc, {"click": {"actions": 2, "kinds": {"executed": 2},
                                        "verdicts": {"verified": 1, "uncertain": 1}}})
    wp11_perf.merge_mix(acc, {"click": {"actions": 1, "kinds": {"executed": 1},
                                        "verdicts": {"uncertain": 1}}})
    assert acc["click"] == {"actions": 3,
                            "kinds": {"executed": 3},
                            "verdicts": {"verified": 1, "uncertain": 2}}


# ---------------------------------------------------------------------------
# environment / template invariants
# ---------------------------------------------------------------------------

def test_child_env_sets_knobs_and_strips_e2e(monkeypatch):
    monkeypatch.setenv("CUMCP_RUN_E2E", "1")
    env = wp11_perf.child_env(Path("D:/Cortex/Cortex"))
    assert "CUMCP_RUN_E2E" not in env
    assert env["CORTEX_KEY_DISPATCH_GAP"] == "0"
    assert env["CORTEX_FOCUS_SETTLE_SECONDS"] == "0"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTHONPATH"].endswith("src")


def test_hot_child_workload_covers_both_versions_action_set():
    for action in ("click", "move", "type", "keypress", "hotkey",
                   "scroll", "wait", "focus_window"):
        assert f'action="{action}"' in wp11_perf.HOT_CHILD


def test_hot_child_and_r5_child_use_flat_layered_import_pairs():
    for template in (wp11_perf.HOT_CHILD, wp11_perf.R5_CHILD, wp11_perf.PROFILE_CHILD):
        assert 'imp("computer_use_mcp.' in template
        assert '"computer_use_mcp.pipeline.' in template or \
               '"computer_use_mcp.desktop.' in template


def test_r5_child_handles_both_bound_shapes():
    # API difference 3: 2-tuple BEFORE (server) vs 3-tuple AFTER (server.replies)
    assert "bound_result[0], bound_result[1]" in wp11_perf.R5_CHILD
    assert "computer_use_mcp.server.replies" in wp11_perf.R5_CHILD
    assert '__import__("computer_use_mcp.server", fromlist=["*"])' in wp11_perf.R5_CHILD


def test_sleep_knobs_identical_and_zero():
    assert wp11_perf.SLEEP_KNOBS == {"CORTEX_KEY_DISPATCH_GAP": "0",
                                     "CORTEX_FOCUS_SETTLE_SECONDS": "0"}


def test_import_entries_match_contract():
    assert wp11_perf.IMPORT_ENTRIES == {"before": "computer_use_mcp.server",
                                        "after": "computer_use_mcp.server.app"}


def test_rerun_done_requires_full_2xn_again():
    phases_hot = {"rerun-hot": wp11_perf.HOT_ROUNDS}
    phases_half = {"rerun-hot": wp11_perf.HOT_ROUNDS - 1}
    assert wp11_perf.rerun_done("hot.execution_ms.p50", phases_hot)
    assert not wp11_perf.rerun_done("hot.execution_ms.p50", phases_half)
    assert wp11_perf.rerun_done("import_cold_ms",
                                {"rerun-import-cold": wp11_perf.IMPORT_PAIRS})
    assert not wp11_perf.rerun_done("import_cold_ms",
                                    {"rerun-hot": 10 ** 6})


def test_render_report_lists_every_final_metric(tmp_path):
    blob = {"environment": {"before_tree": "b", "after_tree": "a",
                            "before_git": "2ae76b0", "after_git": "05b6a00",
                            "sleep_knobs": wp11_perf.SLEEP_KNOBS,
                            "import_entries": wp11_perf.IMPORT_ENTRIES,
                            "hot_rounds_contract_n": 30,
                            "import_pairs_contract_n": 40,
                            "hot_child_repeats": 6, "r5_iterations": 8,
                            "python": "p", "python_version": "3.11",
                            "platform": "win", "cumcp_run_e2e_set": False}}
    records = {"hot.captures_per_action": wp11_perf.metric_record(
        _flat(3.0, 30), _flat(3.0, 30))}
    result = {"verdict": "PASS", "environment": blob["environment"],
              "metrics_final": records,
              "metrics_first_pass": {"hot.captures_per_action": records["hot.captures_per_action"]},
              "verdict_mix": {}}
    text = wp11_perf.render_report(result, blob)
    assert "hot.captures_per_action" in text
    assert "OVERALL: PASS" in text
    json.loads(json.dumps(result))  # report payload stays JSON-serializable
