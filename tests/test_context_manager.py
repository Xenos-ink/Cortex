"""Context management tests (spec section 6, post loop-removal).

The summarizer plumbing (``record_step`` trigger, ``summarize``, injectable summarizer
callables, ``SummarizationRequest``) was REMOVED with the run_goal loop family; the
summary is now the deterministic bounded projection of tracked state. These tests cover
what survives: the bounded 5-10 entry recent window, retention of the required critical
information, plan_notes truncation (safe on character boundaries), redaction of
user-content-bearing text, the guarantee that the built request payload exposes ONLY
summary + bounded recent window, and the checkpoint/resume snapshot round-trip.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from computer_use_mcp.context_manager import ContextManager, safe_truncate
from computer_use_mcp.redaction import contains_secret

HUGE = 50_000


def make_context(**kwargs: Any) -> ContextManager:
    return ContextManager(**kwargs)


def trailing_regional_indicator_run(text: str) -> int:
    run = 0
    for char in reversed(text):
        if "\U0001f1e6" <= char <= "\U0001f1ff":
            run += 1
        else:
            break
    return run


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


def test_tracked_state_is_retained_and_projected_into_the_summary() -> None:
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
    # No stored summary -> build_request_payload projects the deterministic bounded state.
    summary = context.build_request_payload()["summary"]

    assert summary["current_goal"] == "produce the monthly report"
    assert summary["current_task"] == "subtask-2: format the workbook"
    assert summary["app_window_state"] == "excel.exe focused: Q3.xlsx"
    assert summary["accomplished"] == ["loaded the source workbook"]
    assert summary["completed_subtasks"] == ["subtask-1: import raw data"]
    assert summary["important_errors"] == ["row 42 failed to parse"]
    assert summary["important_successes"] == ["sheet cleaned"]
    assert summary["recovery_attempts"] == ["retried import after dialog dismiss"]
    assert summary["unresolved_problems"] == ["chart template missing"]
    assert summary["decisions_needed"] == ["awaiting choice of output directory"]


def test_fallback_summary_folds_recent_window_tail() -> None:
    context = make_context()
    for index in range(6):
        context.append_history(f"event {index}")
    summary = context.build_request_payload()["summary"]
    assert "event 5" in summary["notes"]  # bounded recent data feeds the fallback summary
    assert "event 1" not in summary["notes"]  # redundant detail dropped, window still holds it


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


def test_payload_contains_summary_and_recent_window_only() -> None:
    context = make_context(recent_history_cap=5, plan_notes_limit=10)
    for index in range(40):
        context.append_history(f"entry-{index} " + "z" * 900)
        context.add_plan_note(f"note-{index} " + "q" * 400)

    payload = context.build_request_payload()
    assert set(payload) == {"summary", "recent_history", "plan_notes"}
    assert len(payload["recent_history"]) == 5
    assert len(payload["plan_notes"]) == 10
    assert payload["summary"]["summarized_step"] == 0  # nothing summarized: deterministic state
    encoded = json.dumps(payload)
    assert "entry-0 " not in encoded and "note-0 " not in encoded  # full history never sent
    assert len(encoded) < 60_000  # bounded composition: summary + window + capped notes
    assert "entry-39 " in encoded and "note-39 " in encoded  # recent window present


def test_payload_text_is_redacted() -> None:
    context = make_context()
    context.append_history("configured api_key=supersecretvalue123 in settings")
    context.add_plan_note("password=hunter2hunter2 for the staging box")
    payload = context.build_request_payload()
    encoded = json.dumps(payload)
    assert contains_secret(encoded) is False
    assert "supersecretvalue123" not in encoded
    assert "[REDACTED:" in encoded


def test_snapshot_restore_roundtrip_preserves_context() -> None:
    context = make_context(goal="resume me")
    context.set_current_task("subtask-3")
    context.record_completed_subtask("subtask-1 done")
    context.append_history("recent event")
    context.add_plan_note("plan note")

    restored = make_context()
    restored.restore(context.snapshot())
    assert restored.recent_history == context.recent_history
    assert restored.plan_notes == context.plan_notes
    assert restored.steps == context.steps
    assert (
        restored.build_request_payload()["summary"] == context.build_request_payload()["summary"]
    )


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
