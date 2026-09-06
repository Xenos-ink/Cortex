"""Bounded conversation/context state for long-running sessions (spec section 6).

Layering rule (master-mission section 5): this module imports only ``limits`` and
``redaction`` — never orchestration, provider, or MCP modules. The orchestrator feeds
secret-free, redacted history entries in via :meth:`ContextManager.append_history` and
builds every model request payload via :meth:`ContextManager.build_request_payload`,
which exposes ONLY the compressed summary + the bounded recent window — never the full
history. Every structure is bounded: recent history is a ``deque(maxlen=5..10)``; plan
notes are a capped ``deque`` with safe per-note truncation; summary lists and strings
are clamped. When the step count crosses ``context_summarize_every`` a summarization is
due; the injectable summarizer callable (async-friendly; the orchestrator binds the LLM
provider) produces the compressed summary, and on absence/failure a deterministic
bounded fallback derived from tracked state is used instead — summarization never raises
into the caller.
"""

from __future__ import annotations

import inspect
import threading
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from .limits import Limits
from .redaction import redact_text

RECENT_HISTORY_MIN = 5
RECENT_HISTORY_MAX = 10
PLAN_NOTES_LIMIT_DEFAULT = 50
CATEGORY_CAP = 20
SUMMARY_ITEM_MAX_CHARS = 300
SUMMARY_TEXT_MAX_CHARS = 500
SUMMARY_NOTES_MAX_CHARS = 4000
ENTRY_MAX_CHARS = 1000
PLAN_NOTE_MAX_CHARS = 500
FALLBACK_NOTES_ENTRIES = 3
_SNAPSHOT_VERSION = 1

_CATEGORY_NAMES = (
    "accomplished",
    "completed_subtasks",
    "important_errors",
    "important_successes",
    "recovery_attempts",
    "unresolved_problems",
    "decisions_needed",
)
_SCALAR_NAMES = ("current_goal", "current_task", "app_window_state")

# Trailing code points that must not dangle after a truncation.
_COMBINING_CATEGORIES = {"Mn", "Mc", "Me", "Cs"}
_ATTACH_CHARS = {"\u200d", "\ufe0e", "\ufe0f"}  # ZWJ, variation selectors
_REGIONAL_INDICATOR_LO = "\U0001f1e6"
_REGIONAL_INDICATOR_HI = "\U0001f1ff"


def _is_regional_indicator(char: str) -> bool:
    return _REGIONAL_INDICATOR_LO <= char <= _REGIONAL_INDICATOR_HI


def safe_truncate(text: str, limit: int) -> str:
    """Truncate to ``limit`` code points on a safe character boundary.

    Never splits a surrogate pair, a combining-mark cluster, a ZWJ/variation-selector
    join, or a regional-indicator (flag) pair; dangling attach points are trimmed so the
    result stays well-formed text. Operates on code points (Python ``str``), so it cannot
    cut mid-code-point.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    while cut:
        last = cut[-1]
        if unicodedata.category(last) in _COMBINING_CATEGORIES or last in _ATTACH_CHARS:
            cut = cut[:-1]
            continue
        if _is_regional_indicator(last):
            run = 0
            for char in reversed(cut):
                if not _is_regional_indicator(char):
                    break
                run += 1
            if run % 2 == 1:
                cut = cut[:-1]  # never leave a lone half of a flag pair
                continue
        break
    return cut


class ContextSummary(BaseModel):
    """Compressed context retaining, at minimum, everything needed to continue.

    All fields are bounded (``ContextManager`` clamps lists to ``CATEGORY_CAP`` items of
    ``SUMMARY_ITEM_MAX_CHARS`` and scalars to ``SUMMARY_TEXT_MAX_CHARS``).
    """

    current_goal: str = ""
    accomplished: list[str] = Field(default_factory=list)
    completed_subtasks: list[str] = Field(default_factory=list)
    current_task: str = ""
    important_errors: list[str] = Field(default_factory=list)
    important_successes: list[str] = Field(default_factory=list)
    recovery_attempts: list[str] = Field(default_factory=list)
    unresolved_problems: list[str] = Field(default_factory=list)
    app_window_state: str = ""
    decisions_needed: list[str] = Field(default_factory=list)
    notes: str = ""
    summarized_step: int = 0


class SummarizationRequest(BaseModel):
    """Bounded input handed to the summarizer callable (never the full history)."""

    goal: str = ""
    previous_summary: ContextSummary | None = None
    tracked: ContextSummary = Field(default_factory=ContextSummary)
    recent_history: list[str] = Field(default_factory=list)
    plan_notes: list[str] = Field(default_factory=list)
    total_steps: int = 0
    steps_since_summary: int = 0
    max_summary_chars: int = SUMMARY_NOTES_MAX_CHARS


SummarizerResult = ContextSummary | dict[str, Any] | str
ContextSummarizer = Callable[
    [SummarizationRequest], SummarizerResult | Awaitable[SummarizerResult]
]


def _bounded_summary(summary: ContextSummary, notes_max_chars: int) -> ContextSummary:
    """Return a redacted, clamped copy of ``summary`` (defense in depth vs summarizers)."""
    data = summary.model_dump()
    for name in _CATEGORY_NAMES:
        bounded: list[str] = []
        for item in data.get(name) or []:
            text = safe_truncate(redact_text(str(item))[0], SUMMARY_ITEM_MAX_CHARS)
            if text and text not in bounded:
                bounded.append(text)
            if len(bounded) >= CATEGORY_CAP:
                break
        data[name] = bounded
    for name in _SCALAR_NAMES:
        data[name] = safe_truncate(
            redact_text(str(data.get(name) or ""))[0], SUMMARY_TEXT_MAX_CHARS
        )
    data["notes"] = safe_truncate(
        redact_text(str(data.get("notes") or ""))[0], notes_max_chars
    )
    return ContextSummary.model_validate(data)


class ContextManager:
    """Bounded context holder: compressed summary + rolling recent window (5-10 entries).

    Thread-safe (RLock). The orchestrator records progress with :meth:`record_step`;
    when the count crosses ``summarize_every`` (default: ``Limits.context_summarize_every``)
    a summarization is due and :meth:`summarize` must be awaited. :meth:`summarize` never
    raises: an absent, failing, or malformed summarizer result falls back to a
    deterministic summary built from the tracked bounded state.
    """

    def __init__(
        self,
        *,
        goal: str = "",
        summarizer: ContextSummarizer | None = None,
        summarize_every: int | None = None,
        recent_history_cap: int = RECENT_HISTORY_MAX,
        plan_notes_limit: int = PLAN_NOTES_LIMIT_DEFAULT,
        entry_max_chars: int = ENTRY_MAX_CHARS,
        note_max_chars: int = PLAN_NOTE_MAX_CHARS,
        summary_max_chars: int = SUMMARY_NOTES_MAX_CHARS,
    ) -> None:
        self._summarize_every = (
            int(summarize_every)
            if summarize_every is not None
            else Limits().validate().context_summarize_every
        )
        if self._summarize_every < 1:
            raise ValueError("summarize_every must be >= 1")
        self._recent_cap = max(RECENT_HISTORY_MIN, min(RECENT_HISTORY_MAX, int(recent_history_cap)))
        self._plan_notes_limit = max(1, min(500, int(plan_notes_limit)))
        self._entry_max_chars = max(1, int(entry_max_chars))
        self._note_max_chars = max(1, int(note_max_chars))
        self._summary_max_chars = max(1, int(summary_max_chars))
        self._summarizer = summarizer
        self._lock = threading.RLock()
        self._recent: deque[str] = deque(maxlen=self._recent_cap)
        self._plan_notes: deque[str] = deque(maxlen=self._plan_notes_limit)
        self._categories: dict[str, deque[str]] = {
            name: deque(maxlen=CATEGORY_CAP) for name in _CATEGORY_NAMES
        }
        self._goal = ""
        self._current_task = ""
        self._app_window_state = ""
        self._summary: ContextSummary | None = None
        self._steps = 0
        self._steps_at_last_summary = 0
        if goal:
            self.set_goal(goal)

    # --- ingest (all inputs redacted + safely truncated; structures bounded) ------

    def set_goal(self, goal: str) -> None:
        with self._lock:
            self._goal = safe_truncate(redact_text(str(goal))[0], SUMMARY_TEXT_MAX_CHARS)

    def set_current_task(self, task: str) -> None:
        with self._lock:
            self._current_task = safe_truncate(
                redact_text(str(task))[0], SUMMARY_TEXT_MAX_CHARS
            )

    def set_app_window_state(self, state: str) -> None:
        with self._lock:
            self._app_window_state = safe_truncate(
                redact_text(str(state))[0], SUMMARY_TEXT_MAX_CHARS
            )

    def _record(self, category: str, text: str) -> None:
        redacted = safe_truncate(redact_text(str(text))[0], SUMMARY_ITEM_MAX_CHARS)
        if not redacted:
            return
        with self._lock:
            bucket = self._categories[category]
            if redacted in bucket:  # drop redundant detail
                return
            bucket.append(redacted)

    def record_accomplishment(self, text: str) -> None:
        self._record("accomplished", text)

    def record_completed_subtask(self, text: str) -> None:
        self._record("completed_subtasks", text)

    def record_error(self, text: str) -> None:
        self._record("important_errors", text)

    def record_success(self, text: str) -> None:
        self._record("important_successes", text)

    def record_recovery_attempt(self, text: str) -> None:
        self._record("recovery_attempts", text)

    def record_unresolved_problem(self, text: str) -> None:
        self._record("unresolved_problems", text)

    def record_decision_needed(self, text: str) -> None:
        self._record("decisions_needed", text)

    def append_history(self, entry: str) -> bool:
        """Append one entry to the bounded recent window; False when dropped as a
        duplicate of the immediately preceding entry (repetitive detail)."""
        redacted = safe_truncate(redact_text(str(entry))[0], self._entry_max_chars)
        if not redacted:
            return False
        with self._lock:
            if self._recent and self._recent[-1] == redacted:
                return False
            self._recent.append(redacted)
            return True

    def add_plan_note(self, note: str) -> None:
        """Append a plan note (redacted, safely truncated; deque capped at the limit)."""
        redacted = safe_truncate(redact_text(str(note))[0], self._note_max_chars)
        if not redacted:
            return
        with self._lock:
            self._plan_notes.append(redacted)

    # --- summarization trigger -----------------------------------------------------

    def record_step(self) -> bool:
        """Count one step; return True when a summarization is now due."""
        with self._lock:
            self._steps += 1
            return self._steps - self._steps_at_last_summary >= self._summarize_every

    def should_summarize(self) -> bool:
        with self._lock:
            return self._steps - self._steps_at_last_summary >= self._summarize_every

    # --- summarization ----------------------------------------------------------------

    def _tracked_summary(self) -> ContextSummary:
        """Deterministic summary from tracked bounded state (no summarizer needed)."""
        with self._lock:
            return ContextSummary(
                current_goal=self._goal,
                current_task=self._current_task,
                app_window_state=self._app_window_state,
                **{name: list(self._categories[name]) for name in _CATEGORY_NAMES},
            )

    def _fallback_summary(self) -> ContextSummary:
        """Bounded deterministic fallback: tracked state + compact recent-window tail."""
        with self._lock:
            tail = list(self._recent)[-FALLBACK_NOTES_ENTRIES:]
            fallback = self._tracked_summary()
            fallback.notes = safe_truncate(" | ".join(tail), self._summary_max_chars)
            return _bounded_summary(fallback, self._summary_max_chars)

    def _coerce_summary(self, result: object) -> ContextSummary | None:
        """Normalize a summarizer result; None -> caller uses the bounded fallback."""
        if result is None:
            return None
        try:
            if isinstance(result, ContextSummary):
                base = result
            elif isinstance(result, dict):
                base = ContextSummary.model_validate(result)
            elif isinstance(result, str):
                base = self._tracked_summary()
                base.notes = safe_truncate(redact_text(result)[0], self._summary_max_chars)
                return _bounded_summary(base, self._summary_max_chars)
            else:
                return None
        except Exception:  # noqa: BLE001 - malformed summaries must never propagate
            return None
        # Merge deterministic tracked state so required info is never lost to a partial
        # summarizer result; tracked scalars win (they are controller-owned facts).
        tracked = self._tracked_summary()
        merged = ContextSummary(
            current_goal=tracked.current_goal or base.current_goal,
            current_task=tracked.current_task or base.current_task,
            app_window_state=tracked.app_window_state or base.app_window_state,
            notes=base.notes,
        )
        for name in _CATEGORY_NAMES:
            items = list(getattr(tracked, name))
            items += [item for item in getattr(base, name) if item not in items]
            setattr(merged, name, items)
        return _bounded_summary(merged, self._summary_max_chars)

    def _build_request(self) -> SummarizationRequest:
        with self._lock:
            return SummarizationRequest(
                goal=self._goal,
                previous_summary=self._summary,
                tracked=self._tracked_summary(),
                recent_history=list(self._recent),
                plan_notes=list(self._plan_notes),
                total_steps=self._steps,
                steps_since_summary=self._steps - self._steps_at_last_summary,
                max_summary_chars=self._summary_max_chars,
            )

    async def summarize(self) -> ContextSummary:
        """Summarize now; never raises (fallback on summarizer absence/failure)."""
        request = self._build_request()
        result: object = None
        try:
            if self._summarizer is not None:
                outcome = self._summarizer(request)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                result = outcome
        except Exception:  # noqa: BLE001 - summarization failure must be non-fatal
            result = None
        summary = self._coerce_summary(result)
        if summary is None:
            summary = self._fallback_summary()
        with self._lock:
            summary.summarized_step = self._steps
            self._summary = summary
            self._steps_at_last_summary = self._steps
            return summary

    # --- request payload (summary + recent window ONLY; never the full history) ----

    def build_request_payload(self) -> dict[str, Any]:
        with self._lock:
            return {
                "summary": (self._summary or self._fallback_summary()).model_dump(),
                "recent_history": list(self._recent),
                "plan_notes": list(self._plan_notes),
            }

    @property
    def recent_history(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._recent)

    @property
    def plan_notes(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._plan_notes)

    @property
    def steps(self) -> int:
        with self._lock:
            return self._steps

    # --- persistence (checkpoint/resume; counters and windows restored, never reset) --

    def snapshot(self) -> dict[str, Any]:
        """Serializable bounded snapshot of the context state."""
        with self._lock:
            return {
                "snapshot_version": _SNAPSHOT_VERSION,
                "goal": self._goal,
                "current_task": self._current_task,
                "app_window_state": self._app_window_state,
                "summary": None if self._summary is None else self._summary.model_dump(),
                "recent_history": list(self._recent),
                "plan_notes": list(self._plan_notes),
                "categories": {name: list(deq) for name, deq in self._categories.items()},
                "steps": self._steps,
                "steps_at_last_summary": self._steps_at_last_summary,
            }

    def restore(self, snapshot: dict[str, Any]) -> None:
        """Restore from a :meth:`snapshot`; fail-closed on malformed input
        (ValueError/TypeError, never a partial restore)."""
        if not isinstance(snapshot, dict):
            raise TypeError("Context snapshot must be a mapping.")
        version = snapshot.get("snapshot_version", _SNAPSHOT_VERSION)
        if version != _SNAPSHOT_VERSION:
            raise ValueError(f"Unsupported context snapshot version: {version!r}.")
        for key in ("goal", "current_task", "app_window_state", "recent_history",
                    "plan_notes", "categories", "steps", "steps_at_last_summary"):
            if key not in snapshot:
                raise ValueError(f"Context snapshot missing field {key!r}.")
        for key in ("steps", "steps_at_last_summary"):
            value = snapshot[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Context snapshot field {key!r} must be a non-negative int.")
        summary_data = snapshot["summary"]
        if summary_data is not None and not isinstance(summary_data, dict):
            raise TypeError("Context snapshot 'summary' must be a mapping or null.")
        try:
            summary = (
                None
                if summary_data is None
                else _bounded_summary(ContextSummary.model_validate(summary_data),
                                      self._summary_max_chars)
            )
        except Exception as exc:  # fail closed on malformed summaries (re-raised below)
            raise ValueError(f"Invalid context snapshot summary: {exc}") from exc
        recent = [str(item) for item in snapshot["recent_history"]]
        notes = [str(item) for item in snapshot["plan_notes"]]
        categories_data = snapshot["categories"]
        if not isinstance(categories_data, dict):
            raise TypeError("Context snapshot 'categories' must be a mapping.")
        with self._lock:
            self._goal = safe_truncate(redact_text(str(snapshot["goal"]))[0],
                                       SUMMARY_TEXT_MAX_CHARS)
            self._current_task = safe_truncate(
                redact_text(str(snapshot["current_task"]))[0], SUMMARY_TEXT_MAX_CHARS
            )
            self._app_window_state = safe_truncate(
                redact_text(str(snapshot["app_window_state"]))[0], SUMMARY_TEXT_MAX_CHARS
            )
            self._recent.clear()
            for entry in recent[-self._recent_cap:]:
                self._recent.append(
                    safe_truncate(redact_text(entry)[0], self._entry_max_chars)
                )
            self._plan_notes.clear()
            for note in notes[-self._plan_notes_limit:]:
                self._plan_notes.append(
                    safe_truncate(redact_text(note)[0], self._note_max_chars)
                )
            for name in _CATEGORY_NAMES:
                bucket = self._categories[name]
                bucket.clear()
                for item in list(categories_data.get(name) or [])[-CATEGORY_CAP:]:
                    text = safe_truncate(redact_text(str(item))[0], SUMMARY_ITEM_MAX_CHARS)
                    if text:
                        bucket.append(text)
            self._summary = summary
            self._steps = int(snapshot["steps"])
            self._steps_at_last_summary = int(snapshot["steps_at_last_summary"])
