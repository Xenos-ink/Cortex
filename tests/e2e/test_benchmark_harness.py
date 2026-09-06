"""Benchmark harness validation — runs the runner in FAKE mode (no GUI, no env flag).

This file lives in tests/e2e/ but is deliberately NOT marked ``e2e``: it exercises only
fakes (FakeWorldBackend), so it runs in the standard suite and proves the benchmark
harness end-to-end: every task file parses and validates, the fake-mode closed loop runs
through the real server tool surface (real runtime code, simulated desktop), and the
results JSON has the documented shape. It validates the HARNESS — it is not a benchmark
score and does not touch real applications.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.runner import DISCLAIMER, BenchmarkRunner, load_tasks

REPO_ROOT = Path(__file__).resolve().parents[2]
TASKS_DIR = REPO_ROOT / "benchmarks" / "tasks"


def test_task_files_load_and_validate() -> None:
    tasks = load_tasks(TASKS_DIR)
    assert len(tasks) >= 6, f"expected at least 6 tasks, found {len(tasks)}"
    categories = {task.category for task in tasks}
    assert categories == {
        "long_horizon_state_tracking",
        "hidden_state",
        "cross_source_reasoning",
        "visual_spatial_precision",
        "verification",
        "safety_compliance",
    }
    ids = [task.id for task in tasks]
    assert len(ids) == len(set(ids)), "task ids must be unique"
    for task in tasks:
        assert task.max_actions > 0
        assert task.safety_notes
        assert task.goal


def test_runner_fake_mode_full_harness(tmp_path: Path) -> None:
    runner = BenchmarkRunner("fake", TASKS_DIR, tmp_path, "harness-test")
    payload = runner.run()
    assert payload["disclaimer"] == DISCLAIMER
    assert payload["mode"] == "fake"
    by_task = {item["task"]: item for item in payload["tasks"]}
    assert len(by_task) >= 6

    # Browser tasks need a real browser: honest requires_env, nothing else may fail.
    for task_id, item in by_task.items():
        status = item["status"]
        assert status in {"completed", "safety_blocked_as_expected", "requires_env"}, (task_id, item)
        if status == "requires_env":
            assert task_id.startswith(("t06", "t07")), task_id

    # Closed-loop tasks: completion + task-level predicates verified on the fake world.
    for task_id in ("t01-notepad-type-verify", "t02-notepad-two-round-edit",
                    "t03-notepad-moved-window-recovery", "t04-calculator-decimal-entry",
                    "t05-calculator-keyboard-compute", "t08-notepad-save-cross-source"):
        item = by_task[task_id]
        assert item["status"] == "completed", (task_id, item)
        assert item["final_predicate"]["outcome"] == "verified", (task_id, item)
        assert item["verification_outcomes"]["verified"] > 0, (task_id, item)
        assert item["latencies_ms"], task_id

    # The moved-window fault must have produced bounded recovery events.
    recovery = by_task["t03-notepad-moved-window-recovery"]
    assert recovery["recovery_events"] >= 1, recovery
    assert set(recovery["recovery_classes"]) & {"wrong_window", "moved_ui", "stale_coordinates"}

    # The safety task must have been blocked with zero executions and no false blocks.
    safety = by_task["t09-notepad-block-destructive-text"]
    assert safety["status"] == "safety_blocked_as_expected", safety
    assert safety["termination_reason"] == "blocked_safety", safety
    assert safety["executed_actions"] == 0, safety
    assert safety["safety_violations"] == 0
    assert payload["summary"]["false_safety_blocks_total"] == 0, payload["summary"]
    assert payload["summary"]["safety_violations_total"] == 0

    # Latency summaries are collected from the real session metrics.
    for item in by_task.values():
        if item["status"] == "requires_env":
            continue
        assert "observation_ms" in item["latencies_ms"], item["task"]

    # The runner's own JSON serialization is exactly what gets written to results/.
    encoded = json.dumps(payload, indent=2, default=str)
    assert json.loads(encoded)["summary"]["disclaimer"] == DISCLAIMER


def test_runner_rejects_invalid_task(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(json.dumps({"id": "x", "category": "not-a-category"}), encoding="utf-8")
    with pytest.raises(ValueError, match="not-a-category"):
        load_tasks(tmp_path)
