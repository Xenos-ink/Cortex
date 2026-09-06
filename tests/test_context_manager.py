"""Context management tests (spec sections 6 + 17 "Context").

Covers: summarization trigger at ``context_summarize_every``, the bounded 5-10 entry
recent window, retention of the required critical information, plan_notes truncation
(safe on character boundaries), the deterministic fallback when the summarizer is
absent/failing/malformed, redaction of user-content-bearing text, and the guarantee that
the built request payload exposes ONLY summary + bounded recent window (never the full
history, never an unbounded size).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from computer_use_mcp.context_manager import (
    ContextManager,
    ContextSummary,
    SummarizationRequest,
    safe_truncate,
)
from computer_use_mcp.redaction import contains_secret

HUGE = 50_000


def make_context(**kwargs: Any) -> ContextManager:
    return ContextManager(summarize_every=3, **kwargs)


def trailing_regional_indicator_run(text: str) -> int:
    run = 0
    for char in reversed(text):
        if "\U0001f1e6" <= char <= "\U0001f1ff":
            run += 1
        else:
            break
    return run


async def test_summarization_triggers_at_threshold() -> None:
    context = make_context()
    assert context.record_step() is False
    assert context.record_step() is False
    assert context.record_step() is True  # threshold crossed at step 3
    assert context.should_summarize() is True
    summary = await context.summarize()
    assert summary.summarized_step == 3
    assert context.should_summarize() is False  # consumed by the summarization
    assert context.record_step() is False
    assert context.record_step() is False
    assert context.record_step() is True  # next cycle: step 6


async def test_next_trigger_counts_steps_since_last_summary() -> None:
    context = make_context()
    for _ in range(5):
        context.record_step()
    summary = await context.summarize()  # manual (early) compression at step 5
    assert summary.summarized_step == 5
    assert context.should_summarize() is False
    for _ in range(2):
        assert context.record_step() is False  # due again 3 steps after the summary
    assert context.record_step() is True


def test_recent_history_window_is_bounded_5_to_10() -> None:
    context = ContextManager(recent_history_cap=5)
    for index in range(40):
        context.append_history(f"entry-{index}")
    assert len(context.recent_history) == 5
    assert context.recent_history[-1] == "entry-39"  # newest retained
    assert "entry-0" not in context.recent_history  # oldest dropped, never stored
    # Configurable bounds are clamped into the 5..10 contract.
    wide = ContextManager(recent_history_cap=50)
    for index in range(12):
        wide.append_history(f"wide-{index}")
    assert len(wide.recent_history) == 10  # never more than 10
    narrow = ContextManager(recent_history_cap=2)
    for index in range(8):
        narrow.append_history(f"narrow-{index}")
    assert len(narrow.recent_history) == 5  # never fewer than 5 once filled


def test_history_entries_and_duplicates_are_bounded() -> None:
    context = make_context(entry_max_chars=100)
    assert context.append_history("x" * HUGE) is True
    assert len(context.recent_history[-1]) <= 100  # entry safely truncated
    assert context.append_history(context.recent_history[-1]) is False  # duplicate dropped
    assert len(context.recent_history) == 1


async def test_fallback_summary_retains_required_information() -> None:
    context = make_context(goal="produce the monthly report")
    context.set_current_task("subtask-2: format the workbook")
    context.set_app_window_state("excel.exe focused: Q3.xlsx")
    context.record_accomplishment("loaded the source workbook")
    context.record_completed_subtask("subtask-1: import raw data")
    context.record_error("row 42 failed to parse")
    context.record_success("sheet cleaned")
    context.record_recovery_attempt("retried import after dialog dismiss")
    context.record_unresolved_problem("chart template missing")
    context.record_decision_needed("awaiting choice of output directory")
    summary = await context.summarize()  # no summarizer bound -> deterministic fallback

    assert summary.current_goal == "produce the monthly report"
    assert summary.current_task == "subtask-2: format the workbook"
    assert summary.app_window_state == "excel.exe focused: Q3.xlsx"
    assert summary.accomplished == ["loaded the source workbook"]
    assert summary.completed_subtasks == ["subtask-1: import raw data"]
    assert summary.important_errors == ["row 42 failed to parse"]
    assert summary.important_successes == ["sheet cleaned"]
    assert summary.recovery_attempts == ["retried import after dialog dismiss"]
    assert summary.unresolved_problems == ["chart template missing"]
    assert summary.decisions_needed == ["awaiting choice of output directory"]


async def test_fallback_summary_folds_recent_window_tail() -> None:
    context = make_context()
    for index in range(6):
        context.append_history(f"event {index}")
    summary = await context.summarize()
    assert "event 5" in summary.notes  # bounded recent data feeds the fallback summary
    assert "event 1" not in summary.notes  # redundant detail dropped, window still holds it


async def test_summarizer_success_merges_structured_state() -> None:
    async def summarizer(request: SummarizationRequest) -> str:
        return f"compressed: {request.goal} over {request.total_steps} steps"

    context = make_context(goal="ship the release", summarizer=summarizer)
    context.record_completed_subtask("subtask-1 done")
    for _ in range(3):
        context.record_step()
    summary = await context.summarize()
    assert summary.notes.startswith("compressed: ship the release")
    assert summary.completed_subtasks == ["subtask-1 done"]  # tracked info never lost


async def test_summarizer_receives_bounded_request_only() -> None:
    captured: list[SummarizationRequest] = []

    def summarizer(request: SummarizationRequest) -> str:
        captured.append(request)
        return "ok"

    context = make_context(summarizer=summarizer, recent_history_cap=5, plan_notes_limit=10)
    for index in range(30):
        context.append_history(f"entry-{index}")
        context.add_plan_note(f"note-{index}")
    for _ in range(3):
        context.record_step()
    await context.summarize()

    request = captured[0]
    assert len(request.recent_history) == 5  # bounded window, never the full history
    assert len(request.plan_notes) == 10
    assert request.steps_since_summary == 3
    assert request.previous_summary is None

    context.append_history("after first summary")
    for _ in range(3):
        context.record_step()
    await context.summarize()
    assert captured[1].previous_summary is not None  # chained compression
    assert captured[1].recent_history[0] == "entry-26"  # window moved on


async def test_summarizer_failure_and_garbage_fall_back_fail_safe() -> None:
    def exploding(request: SummarizationRequest) -> str:
        raise RuntimeError("provider down")

    context = make_context(summarizer=exploding)
    context.record_completed_subtask("subtask-1 done")
    for _ in range(3):
        context.record_step()
    summary = await context.summarize()  # never raises into the caller
    assert summary.completed_subtasks == ["subtask-1 done"]  # deterministic fallback used

    garbage = make_context(summarizer=lambda request: 42)  # type: ignore[arg-type,return-value]
    garbage.record_completed_subtask("garbage test")
    for _ in range(3):
        garbage.record_step()
    assert isinstance(await garbage.summarize(), ContextSummary)

    invalid = make_context(summarizer=lambda request: {"completed_subtasks": ["ok"], 1: 2})
    invalid.record_completed_subtask("invalid test")
    for _ in range(3):
        invalid.record_step()
    assert isinstance(await invalid.summarize(), ContextSummary)


def test_plan_notes_limit_and_safe_truncation() -> None:
    context = make_context(plan_notes_limit=10, note_max_chars=200)
    for index in range(25):
        context.add_plan_note(f"note-{index} " + "y" * HUGE)
    assert len(context.plan_notes) == 10
    assert context.plan_notes[-1].startswith("note-24")  # newest retained
    assert all(len(note) <= 200 for note in context.plan_notes)


def test_truncation_is_safe_on_character_boundaries() -> None:
    # Regional-indicator (flag) pairs are never split: trailing run stays even.
    flag_note = "flag " + "\U0001f1fa\U0001f1f8" * 50
    for limit in (6, 7, 12, 13, 51):
        truncated = safe_truncate(flag_note, limit)
        assert trailing_regional_indicator_run(truncated) % 2 == 0
    # No dangling ZWJ joiner inside an emoji sequence.
    family = "family x\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert not safe_truncate(family, 10).endswith("\u200d")
    # No dangling combining mark.
    assert safe_truncate("cafe\u0301" + "z" * 20, 5) == "cafe"
    assert safe_truncate("short", 100) == "short"


async def test_payload_contains_summary_and_recent_window_only() -> None:
    context = make_context(recent_history_cap=5, plan_notes_limit=10)
    for index in range(40):
        context.append_history(f"entry-{index} " + "z" * 900)
        context.add_plan_note(f"note-{index} " + "q" * 400)
    for _ in range(3):
        context.record_step()
    await context.summarize()

    payload = context.build_request_payload()
    assert set(payload) == {"summary", "recent_history", "plan_notes"}
    assert len(payload["recent_history"]) == 5
    assert len(payload["plan_notes"]) == 10
    assert payload["summary"]["summarized_step"] == 3
    encoded = json.dumps(payload)
    assert "entry-0 " not in encoded and "note-0 " not in encoded  # full history never sent
    assert len(encoded) < 60_000  # bounded composition: summary + window + capped notes
    assert "entry-39 " in encoded and "note-39 " in encoded  # recent window present


async def test_payload_text_is_redacted() -> None:
    context = make_context()
    context.append_history("configured api_key=supersecretvalue123 in settings")
    context.add_plan_note("password=hunter2hunter2 for the staging box")
    payload = context.build_request_payload()
    encoded = json.dumps(payload)
    assert contains_secret(encoded) is False
    assert "supersecretvalue123" not in encoded
    assert "[REDACTED:" in encoded


async def test_snapshot_restore_roundtrip_preserves_context() -> None:
    context = make_context(goal="resume me")
    context.set_current_task("subtask-3")
    context.record_completed_subtask("subtask-1 done")
    context.append_history("recent event")
    context.add_plan_note("plan note")
    for _ in range(3):
        context.record_step()
    await context.summarize()  # stores the compressed summary asserted via the payload

    restored = make_context()  # same cadence config as the original
    restored.restore(context.snapshot())
    assert restored.recent_history == context.recent_history
    assert restored.plan_notes == context.plan_notes
    assert restored.steps == context.steps
    assert (
        restored.build_request_payload()["summary"] == context.build_request_payload()["summary"]
    )
    # Summarization cadence continues exactly where the checkpoint left off.
    assert restored.should_summarize() is False
    assert restored.record_step() is False
    assert restored.record_step() is False
    assert restored.record_step() is True


def test_restore_rejects_malformed_snapshots_fail_closed() -> None:
    context = make_context()
    good = context.snapshot()
    for malformed in (
        "not-a-mapping",
        {**good, "snapshot_version": 99},
        {key: value for key, value in good.items() if key != "recent_history"},
        {**good, "steps": -1},
        {**good, "summary": 17},
    ):
        with pytest.raises((TypeError, ValueError)):
            context.restore(malformed)  # type: ignore[arg-type]
