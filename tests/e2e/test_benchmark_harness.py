"""Benchmark harness validation — runs the runner in FAKE mode (no GUI, no env flag).

This file lives in tests/e2e/ but is deliberately NOT marked ``e2e``: it exercises only
fakes (FakeWorldBackend), so it runs in the standard suite and proves the benchmark
harness end-to-end: every task file parses and validates, the fake-mode direct pipeline
(``start_session`` -> ``computer_execute`` per scripted step; the runner plays the host —
RETARGETED, loop removal) runs through the real server tool surface (real runtime
code, simulated desktop), and the results JSON has the documented shape. It validates
the HARNESS — it is not a benchmark score and does not touch real applications.
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
        # WP4a DELTA 1 amendment (the ONE line that legitimately had to change, see
        # wp4a-notes.md DELTA 1): t03 recovers a verified step (its type_text verifies
        # through the pixel fallback), but EVERY t04 step states an effect that no
        # deterministic tier can corroborate in the fake world (no ui_elements, no
        # display surface, no focus/bounds delta) — those steps are honestly
        # ``uncertain`` (dispatched, unproven; recorded below) and the TASK's truth is
        # its calc_display_equals predicate, asserted above. Requiring a verified STEP
        # in t04 would re-demand the pixel laundering E-03 removed.
        if task_id != "t04-calculator-decimal-entry":
            assert item["verification_outcomes"]["verified"] > 0, (task_id, item)
        assert item["latencies_ms"], task_id

    # t04's honest per-step record (WP4a DELTA 1): every stated-effect click abstains
    # (uncertain, dispatched, zero verified) and the task still completes on its
    # predicate — unproven is not failed, and unproven is not laundered either.
    t04 = by_task["t04-calculator-decimal-entry"]
    assert t04["verification_outcomes"]["uncertain"] == 4, t04
    assert t04["verification_outcomes"]["verified"] == 0, t04
    assert t04["verification_outcomes"]["failed"] == 0, t04
    assert t04["termination_reason"] == "completed", t04
    assert t04["uncertain_steps"] == 4, t04  # F3 (VAL-WP4a DELTA 2)

    # F3 (VAL-WP4a DELTA 2): the summary row counts unproven steps (t03's two
    # fault-window clicks + t04's four display clicks) and each task row carries its
    # own count — unproven is recorded, never laundered into completed.
    assert payload["summary"]["uncertain_steps_total"] == 6, payload["summary"]

    # The moved-window fault (RETARGETED, loop removal): the loop's automatic
    # recovery/re-decide died with the loop, so t03's fault-window click no longer
    # carries a deterministic verdict (the stated effect names no before/after
    # identity delta — the window moved BEFORE the premise capture), the step is
    # honestly ``uncertain``, and the host-driver script now CONTINUES past it
    # (WP4a DELTA 1: unproven is not failed, §1.2) to the re-grounded completion; the
    # final re-grounded click and the typed marker complete the task predicate. The
    # recovery-event counters stay 0 on the five-tool surface (no in-run recovery
    # exists) — the fault coverage is the honest uncertain record plus the completed
    # predicate after re-ground.
    recovery = by_task["t03-notepad-moved-window-recovery"]
    assert recovery["recovery_events"] == 0, recovery  # no in-run recovery exists anymore
    assert recovery["status"] == "completed", recovery
    assert recovery["uncertain_steps"] == 2, recovery  # F3 (VAL-WP4a DELTA 2)

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


def test_a_not_determined_predicate_after_uncertain_steps_is_not_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3 (VAL-WP4a DELTA 2): a task whose steps were honestly uncertain may read
    ``completed`` only on a POSITIVELY satisfied predicate. A ``not_determined``
    predicate (monkeypatched here — every REAL fake-world kind decides definitely;
    ``runtime_blocked`` is reserved for the expected-safety-block vocabulary) after
    an uncertain step is a FAILED run — the old rule accepted ``not_determined``
    after uncertain steps as "completed", claiming a success the evidence does not
    hold. The uncertain step is still counted, per task and in the summary row."""
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "t90-uncertain-not-determined.yaml").write_text(
        json.dumps(
            {
                "id": "t90-uncertain-not-determined",
                "category": "verification",
                "goal": "probe: an unproven step with an undecidable predicate",
                "fake_runnable": True,
                "setup": {"app": "notepad"},
                "provider": {
                    "steps": [
                        {
                            "type": "click_window_center",
                            "window": "target",
                            "expected_effect": "a change no deterministic tier can see",
                        },
                        {"type": "done", "summary": "probe done"},
                    ]
                },
                "verification": {"kind": "window_text_contains", "value": "typed marker"},
                "max_actions": 5,
                "safety_notes": "offline fake probe; clicks land in the harness window only.",
            }
        ),
        encoding="utf-8",
    )

    from benchmarks.runner import FakeWorld

    def _undecidable(self: Any, task: Any, scratch_dir: Any) -> dict[str, Any]:
        return {"kind": "probe", "outcome": "not_determined", "evidence": {}}

    monkeypatch.setattr(FakeWorld, "final_predicate", _undecidable)
    runner = BenchmarkRunner("fake", tasks, tmp_path / "out", "harness-f3")
    payload = runner.run()
    item = payload["tasks"][0]
    assert item["status"] == "failed", item
    assert item["termination_reason"] == "completed", item  # the script ran to its marker
    assert item["final_predicate"]["outcome"] == "not_determined", item
    assert item["verification_outcomes"]["uncertain"] == 1, item
    assert item["verification_outcomes"]["verified"] == 0, item
    assert item["uncertain_steps"] == 1, item
    assert payload["summary"]["uncertain_steps_total"] == 1, payload["summary"]


# --- WP6 DELTA 2: the runner must actually STOP the session it started -----------------------


def test_direct_call_driver_end_to_end_stops_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 2 (WP6) regression guard, strengthened (VAL-WP6-02 N2 carry-over, WP4b):
    ``stop_session`` is an ASYNC coroutine since WP6 DELTA 1/F4, and the RUNNER is the
    caller that must await it (``benchmarks/runner.py``: the sync→async bridge at the
    task teardown). This test drives the runner's OWN task path end to end on the fake
    backend and asserts the runner actually stopped the session it started — the test
    itself makes NO ``stop_session`` call of any kind. Reverting the runner's fix (a
    sync ``server.stop_session(...)`` call) fails this test twice over: the spy records
    no awaited stop, and the abandoned coroutine raises the never-awaited
    RuntimeWarning the suite-wide filter (tests/conftest.py) escalates to an error."""
    import shutil

    from benchmarks.runner import BenchmarkRunner

    import computer_use_mcp.server.lifecycle as srv_server_lifecycle
    import computer_use_mcp.server.observe as srv_server_observe

    tasks = tmp_path / "tasks"
    tasks.mkdir()
    shutil.copyfile(TASKS_DIR / "t01-notepad-type-verify.yaml", tasks / "t01-notepad-type-verify.yaml")

    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))

    installed: dict[str, Any] = {}
    original_isolate = BenchmarkRunner.__dict__["_isolate_server"].__func__

    def _spy_isolate(*args: Any, **kwargs: Any) -> Any:
        snapshot = original_isolate(*args, **kwargs)
        installed["registry"] = srv_server_lifecycle._registry
        installed["bundles"] = srv_server_lifecycle._bundles
        return snapshot

    monkeypatch.setattr(BenchmarkRunner, "_isolate_server", staticmethod(_spy_isolate))

    stop_calls: list[str] = []
    real_stop = srv_server_lifecycle.stop_session

    async def _spy_stop(session_id: str, *args: Any, **kwargs: Any) -> Any:
        stop_calls.append(str(session_id))
        return await real_stop(session_id, *args, **kwargs)

    monkeypatch.setattr(srv_server_lifecycle, "stop_session", _spy_stop)

    runner = BenchmarkRunner("fake", tasks, tmp_path / "out", "harness-stop")
    payload = runner.run()
    item = payload["tasks"][0]
    assert item["status"] == "completed", item  # the task itself ran to completion
    # The RUNNER stopped the session: exactly one awaited stop, for the session the
    # runner's own isolated wiring created.
    assert len(stop_calls) == 1, stop_calls
    session_id = stop_calls[0]
    assert installed["registry"].get(session_id) is None, "session leaked in the registry"
    observe = srv_server_observe.computer_observe(session_id)
    observe_payload = observe if isinstance(observe, dict) else json.loads(observe[0].text)
    assert observe_payload["ok"] is False
    assert observe_payload["error"] == "session_stopped"


def test_a_live_mode_run_halts_on_an_uncertain_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N2 (VAL-WP4a-02 carry-over, WP4b): the continue-past-uncertain rule is FAKE-MODE
    ONLY (``benchmarks/runner.py``, the ``resolver.mode == "fake"`` gate). A LIVE-mode
    (``env``) run must HALT on an unproven step — it must never keep dispatching real
    input after a step whose effect nothing could corroborate.

    Offline by construction: the "live" run is driven against a STUBBED world and a
    stubbed backend factory — the env-mode branches (``RealWorld``, the real backend
    constructor) are monkeypatched to the fake world, so no real application is
    touched and no real input is dispatched. The task is the t90 shape: one click with
    an effect no deterministic tier can see (honestly ``uncertain``), then a ``done``
    marker. Fake mode continues and reads ``completed`` (pinned above); live mode must
    halt instead."""
    import json as _json

    from benchmarks import runner as runner_module
    from benchmarks.runner import BenchmarkRunner, FakeWorld

    class _EnvWorld(FakeWorld):
        """The fake world standing in for ``RealWorld`` — env mode calls ``teardown``."""

        def teardown(self) -> None:
            pass

    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "t91-live-uncertain-halt.yaml").write_text(
        _json.dumps(
            {
                "id": "t91-live-uncertain-halt",
                "category": "verification",
                "goal": "probe: a live run halts on an unproven step",
                "fake_runnable": True,
                "setup": {"app": "notepad"},
                "provider": {
                    "steps": [
                        {
                            "type": "click_window_center",
                            "window": "target",
                            "expected_effect": "a change no deterministic tier can see",
                        },
                        {"type": "done", "summary": "probe done"},
                    ]
                },
                "verification": {"kind": "window_text_contains", "value": "typed marker"},
                "max_actions": 5,
                "safety_notes": "offline stub probe; no real input is ever dispatched.",
            }
        ),
        encoding="utf-8",
    )

    world = _EnvWorld()
    monkeypatch.setattr(runner_module, "RealWorld", _EnvWorld)
    monkeypatch.setattr(runner_module, "LocalComputerBackend", lambda: world.backend)

    runner = BenchmarkRunner("env", tasks, tmp_path / "out", "harness-n2")
    payload = runner.run()
    item = payload["tasks"][0]
    # THE HALT: the first step's honest ``uncertain`` ends a live run — the
    # fake-mode-only continue never fires here.
    assert item["termination_reason"] == "verification_uncertain", item
    assert item["status"] == "failed", item
    assert item["completed"] is False, item
    assert item["uncertain_steps"] == 1, item
    assert item["verification_outcomes"]["uncertain"] == 1, item
    assert item["verification_outcomes"]["verified"] == 0, item
    assert item["executed_actions"] == 1, item  # the marker never executed
