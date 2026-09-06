"""Session and task state substrate: StopToken, TaskState, SessionContext, SessionRegistry.

Layering rule (master-mission section 5): this module imports only ``models``; it must
never import ``limits``/``audit``/backend modules. Histories are bounded via
``collections.deque(maxlen=...)`` so they cannot grow unbounded even if callers append
directly. ``TaskState.action_history`` stores secret-free summaries only (typed text is
intentionally excluded); full actions belong in the redaction-enforced audit sink.
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from .models import GroundedAction, TerminationReason


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


ACTION_HISTORY_CAP = 100
OBSERVATION_HISTORY_CAP = 50
PLAN_NOTES_CAP = 50


class TaskStopped(RuntimeError):
    """Raised when work is attempted after the task's stop token has fired."""


class StopToken:
    """Thread-safe cooperative stop signal wrapping ``threading.Event``.

    Reachable only from ``stop_session`` / internal safety paths — never from anything
    the model controls. ``stop()`` is idempotent and safe to call from any thread.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def stop(self) -> None:
        """Arm the stop signal (idempotent)."""
        self._event.set()

    @property
    def stopped(self) -> bool:
        return self._event.is_set()

    def ensure_live(self) -> None:
        """Raise :class:`TaskStopped` if the stop signal has fired."""
        if self.stopped:
            raise TaskStopped("Task stop requested; refusing to continue.")

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the stop signal; return True if it fired, False on timeout."""
        return self._event.wait(timeout)


class TaskStatus(StrEnum):
    """Lifecycle status of a running task within a session."""

    IDLE = "idle"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class ActionRecord(BaseModel):
    """Secret-free summary of one executed action (typed text intentionally excluded)."""

    action_id: str
    action_type: str
    point: tuple[int, int] | None = None
    timestamp: datetime = Field(default_factory=_utc_now)


class TaskState(BaseModel):
    """Bounded, controller-owned state for one task execution.

    ``recovery_attempts_action`` counts recovery attempts for the current action and must
    be reset via :meth:`reset_action_scope` when the controller moves to a new action;
    ``recovery_attempts_task`` accumulates for the whole task.
    """

    task_id: str = Field(default_factory=_new_id)
    goal: str = ""
    subgoal: str | None = None
    plan_notes: deque[str] = Field(default_factory=lambda: deque(maxlen=PLAN_NOTES_CAP))
    status: TaskStatus = TaskStatus.IDLE
    step_count: int = 0
    termination_reason: TerminationReason | None = None
    started_at: datetime = Field(default_factory=_utc_now)
    model_call_count: int = 0
    recovery_attempts_task: int = 0
    recovery_attempts_action: int = 0
    action_history: deque[ActionRecord] = Field(default_factory=lambda: deque(maxlen=ACTION_HISTORY_CAP))
    observation_history: deque[str] = Field(default_factory=lambda: deque(maxlen=OBSERVATION_HISTORY_CAP))

    def record_action(self, action: GroundedAction) -> ActionRecord:
        """Append a secret-free summary of ``action`` to the bounded history."""
        record = ActionRecord(
            action_id=action.action_id,
            action_type=action.action.value,
            point=None if action.point is None else (action.point.x, action.point.y),
        )
        self.action_history.append(record)
        return record

    def record_observation_id(self, observation_id: str) -> None:
        self.observation_history.append(observation_id)

    def add_plan_note(self, note: str) -> None:
        self.plan_notes.append(note)

    def reset_action_scope(self) -> None:
        """Reset the per-action recovery counter when starting a new action."""
        self.recovery_attempts_action = 0

    def terminate(self, reason: TerminationReason) -> None:
        """Set the termination reason and map it onto the matching terminal status."""
        self.termination_reason = reason
        if reason is TerminationReason.COMPLETED:
            self.status = TaskStatus.COMPLETED
        elif reason is TerminationReason.STOPPED_BY_USER:
            self.status = TaskStatus.STOPPED
        else:
            self.status = TaskStatus.FAILED


@dataclass
class SessionContext:
    """Everything isolated per session: task state, stop signal, and creation time."""

    session_id: str
    task: TaskState
    stop: StopToken
    created_at: datetime = field(default_factory=_utc_now)


class SessionLimitExceeded(RuntimeError):
    """Raised when a new session is refused because the registry is at capacity."""

    def __init__(self, message: str, max_sessions: int) -> None:
        super().__init__(message)
        self.max_sessions = max_sessions


class SessionRegistry:
    """Thread-safe, bounded registry of :class:`SessionContext` objects.

    Eviction policy is fail-closed: when the registry is full, ``create`` refuses the new
    session by raising :class:`SessionLimitExceeded` (it never evicts a live session).
    """

    def __init__(self, max_sessions: int = 4) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be >= 1")
        self._max_sessions = max_sessions
        self._sessions: dict[str, SessionContext] = {}
        self._lock = threading.RLock()

    @property
    def max_sessions(self) -> int:
        return self._max_sessions

    def create(self, session_id: str | None = None, goal: str = "") -> SessionContext:
        """Create and register a new session; refuse when at capacity."""
        with self._lock:
            if len(self._sessions) >= self._max_sessions:
                raise SessionLimitExceeded(
                    f"Session limit reached ({self._max_sessions}); refusing to create a new session.",
                    self._max_sessions,
                )
            sid = session_id or _new_id()
            if sid in self._sessions:
                raise ValueError(f"Session already exists: {sid}")
            context = SessionContext(session_id=sid, task=TaskState(goal=goal), stop=StopToken())
            self._sessions[sid] = context
            return context

    def get(self, session_id: str) -> SessionContext | None:
        with self._lock:
            return self._sessions.get(session_id)

    def remove(self, session_id: str) -> SessionContext | None:
        """Remove and return the session context, or None when unknown."""
        with self._lock:
            return self._sessions.pop(session_id, None)

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._sessions)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def __iter__(self) -> Iterator[SessionContext]:
        with self._lock:
            return iter(list(self._sessions.values()))
