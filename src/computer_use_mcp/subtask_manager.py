"""Deterministic subtask domain state: entities, transitions, and the dependency graph.

Layering (master-mission 003 section 5): this module imports only ``models`` and the pure
graph helpers of ``plan_validator``; it never imports orchestration, MCP, or backend
surfaces. It contains NO execution — the single closed-loop executor stays
``ComputerUseAgent.run``; the orchestrator (later wave) drives subtasks through it.

Doctrine (SubtasksProtocol sections 1/3/4/16):
- All mutable state is guarded by a ``threading.RLock`` and every state transition follows
  the fixed table :data:`TRANSITIONS`, so identical operation sequences always produce
  identical states (deterministic transitions).
- A subtask may START only when ALL of its dependencies are ``completed``; a failed or
  blocked dependency blocks its (transitive) dependents.
- Everything is bounded: at most ``max_subtasks`` subtasks (hard ceiling
  :data:`~computer_use_mcp.models.MAX_SUBTASKS` = 50), at most
  :data:`~computer_use_mcp.models.SUBTASK_RESULTS_CAP` execution results per subtask with
  heavy payloads stripped before storage, and read APIs return bounded snapshots.
- ``subtask_id`` values are stable and serializable: :meth:`SubtaskManager.snapshot` and
  :meth:`SubtaskManager.restore` round-trip them losslessly for checkpoint/resume.
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from .models import (
    MAX_SUBTASKS,
    SUBTASK_RESULTS_CAP,
    ExecutionResult,
    FailureClass,
    Subtask,
    SubtaskFailureInfo,
    SubtaskStatus,
)
from .plan_validator import contains_control_characters, find_cycle

__all__ = [
    "SUBTASK_RESULTS_CAP",
    "TRANSITIONS",
    "DependencyCycleError",
    "InvalidSubtaskError",
    "InvalidTransitionError",
    "SelfDependencyError",
    "SubtaskAlreadyExistsError",
    "SubtaskError",
    "SubtaskLimitExceeded",
    "SubtaskManager",
    "SubtaskNotReadyError",
    "UnknownDependencyError",
    "UnknownSubtaskError",
]

_ID_MAX_LENGTH = 128
_DESCRIPTION_MAX_LENGTH = 2_000
#: Bounded projection lengths for ``list()`` summaries (MCP-facing output stays small).
_SUMMARY_FIELD_MAX_LENGTH = 300


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


#: Fixed, documented transition table (deterministic; terminal states move nowhere).
#: ``pending -> failed`` is legal: the orchestrator may fail a never-started subtask when
#: it can no longer be executed safely (e.g. replan exhaustion) — still fail-closed since
#: ``failed`` is terminal.
TRANSITIONS: dict[SubtaskStatus, frozenset[SubtaskStatus]] = {
    SubtaskStatus.PENDING: frozenset(
        {SubtaskStatus.RUNNING, SubtaskStatus.BLOCKED, SubtaskStatus.PAUSED, SubtaskStatus.FAILED}
    ),
    SubtaskStatus.RUNNING: frozenset(
        {SubtaskStatus.COMPLETED, SubtaskStatus.FAILED, SubtaskStatus.PAUSED}
    ),
    SubtaskStatus.PAUSED: frozenset(
        {
            SubtaskStatus.PENDING,
            SubtaskStatus.RUNNING,
            SubtaskStatus.BLOCKED,
            SubtaskStatus.FAILED,
        }
    ),
    SubtaskStatus.BLOCKED: frozenset({SubtaskStatus.PENDING, SubtaskStatus.PAUSED}),
    SubtaskStatus.COMPLETED: frozenset(),
    SubtaskStatus.FAILED: frozenset(),
}


class SubtaskError(RuntimeError):
    """Base class for all subtask-domain errors (typed, testable, fail-closed)."""


class SubtaskLimitExceeded(SubtaskError):
    """Raised when creating a subtask would exceed the manager's subtask ceiling."""

    def __init__(self, message: str, max_subtasks: int) -> None:
        super().__init__(message)
        self.max_subtasks = max_subtasks


class UnknownSubtaskError(SubtaskError):
    """Raised when an operation references a subtask id the manager does not hold."""

    def __init__(self, message: str, subtask_id: str) -> None:
        super().__init__(message)
        self.subtask_id = subtask_id


class SubtaskAlreadyExistsError(SubtaskError):
    """Raised when a subtask id collides with an existing one (create/restore)."""

    def __init__(self, message: str, subtask_id: str) -> None:
        super().__init__(message)
        self.subtask_id = subtask_id


class InvalidSubtaskError(SubtaskError):
    """Raised for malformed subtask input (types, emptiness, bounds, control characters)."""


class UnknownDependencyError(SubtaskError):
    """Raised when a dependency references a subtask that does not exist."""

    def __init__(self, message: str, subtask_id: str, dependency_id: str) -> None:
        super().__init__(message)
        self.subtask_id = subtask_id
        self.dependency_id = dependency_id


class SelfDependencyError(SubtaskError):
    """Raised when a subtask would depend on itself."""


class DependencyCycleError(SubtaskError):
    """Raised when the dependency graph would contain a cycle."""


class InvalidTransitionError(SubtaskError):
    """Raised when a status change is not permitted by the fixed transition table."""

    def __init__(self, message: str, subtask_id: str, current: SubtaskStatus, target: SubtaskStatus) -> None:
        super().__init__(message)
        self.subtask_id = subtask_id
        self.current = current
        self.target = target


class SubtaskNotReadyError(SubtaskError):
    """Raised when starting a subtask whose dependencies are not all completed."""

    def __init__(self, message: str, subtask_id: str, unmet: list[str]) -> None:
        super().__init__(message)
        self.subtask_id = subtask_id
        self.unmet = list(unmet)


def _valid_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= _ID_MAX_LENGTH
        and not contains_control_characters(value)
    )


class SubtaskManager:
    """Thread-safe, bounded owner of one session's subtask entities and dependency graph.

    Args:
        max_subtasks: Subtask ceiling; hard domain maximum is
            :data:`~computer_use_mcp.models.MAX_SUBTASKS` (50) per SubtasksProtocol.
    """

    def __init__(self, max_subtasks: int = MAX_SUBTASKS) -> None:
        if not isinstance(max_subtasks, int) or isinstance(max_subtasks, bool):
            raise TypeError("max_subtasks must be an int")
        if not 1 <= max_subtasks <= MAX_SUBTASKS:
            raise ValueError(f"max_subtasks must be within 1..{MAX_SUBTASKS}")
        self._max_subtasks = max_subtasks
        self._subtasks: dict[str, Subtask] = {}
        self._lock = threading.RLock()

    # -- introspection ---------------------------------------------------------------------

    @property
    def max_subtasks(self) -> int:
        return self._max_subtasks

    def __len__(self) -> int:
        with self._lock:
            return len(self._subtasks)

    def ids(self) -> list[str]:
        """All subtask ids in deterministic creation order."""
        with self._lock:
            return list(self._subtasks)

    def get(self, subtask_id: str) -> Subtask | None:
        """Return a detached snapshot of the subtask, or ``None`` when unknown."""
        with self._lock:
            subtask = self._subtasks.get(subtask_id)
            return None if subtask is None else subtask.model_copy(deep=True)

    def require(self, subtask_id: str) -> Subtask:
        """Return a detached snapshot; raise :class:`UnknownSubtaskError` when unknown."""
        with self._lock:
            subtask = self._subtasks.get(subtask_id)
            if subtask is None:
                raise UnknownSubtaskError(f"Unknown subtask: {subtask_id!r}.", subtask_id)
            return subtask.model_copy(deep=True)

    def list(self, status: SubtaskStatus | str | None = None) -> list[dict[str, Any]]:
        """Bounded structured summaries (creation order; no result payloads, no history).

        Optional ``status`` filters the projection. Each summary carries bounded scalar
        fields plus result/recovery counts — never result payloads — so the output size is
        bounded by ``max_subtasks`` regardless of retained results.
        """
        with self._lock:
            wanted = self._normalize_status_filter(status)
            summaries: list[dict[str, Any]] = []
            for subtask in self._subtasks.values():
                if wanted is not None and subtask.status is not wanted:
                    continue
                summaries.append(
                    {
                        "subtask_id": subtask.subtask_id,
                        "description": subtask.description,
                        "status": subtask.status.value,
                        "depends_on": list(subtask.depends_on),
                        "created_at": subtask.created_at.isoformat(),
                        "started_at": None if subtask.started_at is None else subtask.started_at.isoformat(),
                        "completed_at": None if subtask.completed_at is None else subtask.completed_at.isoformat(),
                        "result_count": len(subtask.results),
                        "recovery_attempts": subtask.recovery_attempts,
                        "failure": self._failure_summary(subtask.failure),
                    }
                )
            return summaries

    def counts(self) -> dict[str, int]:
        """Per-status subtask counts (every status key present; deterministic order)."""
        with self._lock:
            return {
                status.value: sum(1 for s in self._subtasks.values() if s.status is status)
                for status in SubtaskStatus
            }

    # -- creation --------------------------------------------------------------------------

    def create(
        self,
        description: str,
        depends_on: Iterable[str] | None = None,
        subtask_id: str | None = None,
    ) -> Subtask:
        """Create a ``pending`` subtask; refuse the ceiling, bad input, and bad graphs.

        Deterministic validation order: ceiling -> description -> id -> dependency shape ->
        self-dependency -> unknown dependency -> duplicate dependency -> cycle.
        """
        with self._lock:
            if len(self._subtasks) >= self._max_subtasks:
                raise SubtaskLimitExceeded(
                    f"Subtask limit reached ({self._max_subtasks}); refusing to create more.",
                    self._max_subtasks,
                )
            if (
                not isinstance(description, str)
                or not description.strip()
                or len(description) > _DESCRIPTION_MAX_LENGTH
                or contains_control_characters(description)
            ):
                raise InvalidSubtaskError(
                    "description must be a non-empty string of at most "
                    f"{_DESCRIPTION_MAX_LENGTH} characters without control characters"
                )
            if subtask_id is not None:
                if not _valid_id(subtask_id):
                    raise InvalidSubtaskError(
                        "subtask_id must be a non-empty string of at most "
                        f"{_ID_MAX_LENGTH} characters without control characters"
                    )
                if subtask_id in self._subtasks:
                    raise SubtaskAlreadyExistsError(
                        f"Subtask already exists: {subtask_id!r}.", subtask_id
                    )
            dep_ids = self._validated_dependencies(subtask_id if subtask_id is not None else _new_id(), depends_on)
            subtask = Subtask(
                subtask_id=subtask_id if subtask_id is not None else _new_id(),
                description=description,
                depends_on=dep_ids,
            )
            self._subtasks[subtask.subtask_id] = subtask
            return subtask.model_copy(deep=True)

    # -- deterministic transitions ---------------------------------------------------------

    def start(self, subtask_id: str) -> Subtask:
        """``pending -> running``; requires ALL dependencies ``completed``."""
        with self._lock:
            subtask = self._require_locked(subtask_id)
            unmet = [
                dep
                for dep in subtask.depends_on
                if self._subtasks[dep].status is not SubtaskStatus.COMPLETED
            ]
            if unmet:
                raise SubtaskNotReadyError(
                    f"Subtask {subtask_id!r} has dependencies that are not completed.",
                    subtask_id,
                    unmet,
                )
            self._apply_transition(subtask, SubtaskStatus.RUNNING)
            if subtask.started_at is None:
                subtask.started_at = _utc_now()
            return subtask.model_copy(deep=True)

    def complete(self, subtask_id: str) -> Subtask:
        """``running -> completed`` (terminal)."""
        with self._lock:
            subtask = self._require_locked(subtask_id)
            self._apply_transition(subtask, SubtaskStatus.COMPLETED)
            subtask.completed_at = _utc_now()
            return subtask.model_copy(deep=True)

    def fail(
        self,
        subtask_id: str,
        failure_class: FailureClass = FailureClass.SUBTASK_FAILED,
        error: str = "",
        last_known_state: str = "",
        recovery_attempts: int | None = None,
    ) -> Subtask:
        """``running|paused -> failed`` (terminal); blocks all transitive dependents."""
        with self._lock:
            subtask = self._require_locked(subtask_id)
            if not isinstance(error, str) or not isinstance(last_known_state, str):
                raise InvalidSubtaskError("error and last_known_state must be strings")
            self._apply_transition(subtask, SubtaskStatus.FAILED)
            subtask.completed_at = _utc_now()
            subtask.failure = SubtaskFailureInfo(
                failure_class=failure_class,
                error=error[:2000],
                recovery_attempts=subtask.recovery_attempts if recovery_attempts is None else max(0, recovery_attempts),
                last_known_state=last_known_state[:1000],
                recorded_at=_utc_now(),
            )
            self._block_dependents_of({subtask_id})
            return subtask.model_copy(deep=True)

    def pause(self, subtask_id: str) -> Subtask:
        """``pending|running -> paused`` (session lifecycle resting state)."""
        with self._lock:
            subtask = self._require_locked(subtask_id)
            self._apply_transition(subtask, SubtaskStatus.PAUSED)
            return subtask.model_copy(deep=True)

    def resume(self, subtask_id: str) -> Subtask:
        """``paused -> running`` when it had started, else ``paused -> pending``.

        Only paused subtasks can be resumed (strict, deterministic precondition).
        """
        with self._lock:
            subtask = self._require_locked(subtask_id)
            if subtask.status is not SubtaskStatus.PAUSED:
                raise InvalidTransitionError(
                    f"Only paused subtasks can be resumed; subtask {subtask_id!r} is "
                    f"{subtask.status.value}.",
                    subtask_id,
                    subtask.status,
                    SubtaskStatus.PAUSED,
                )
            target = SubtaskStatus.RUNNING if subtask.started_at is not None else SubtaskStatus.PENDING
            self._apply_transition(subtask, target)
            return subtask.model_copy(deep=True)

    def requeue(self, subtask_id: str) -> Subtask:
        """``blocked -> pending`` for bounded-replan rewiring by the orchestrator."""
        with self._lock:
            subtask = self._require_locked(subtask_id)
            self._apply_transition(subtask, SubtaskStatus.PENDING)
            return subtask.model_copy(deep=True)

    # -- bounded results and recovery bookkeeping ------------------------------------------

    def record_result(self, subtask_id: str, result: ExecutionResult) -> None:
        """Append a trimmed copy of ``result`` to the subtask's bounded retention.

        Conflict C5: heavy payloads (``screenshot_after_base64``) are stripped BEFORE
        storage; the deque cap evicts the oldest records. Only allowed while the subtask
        is running or paused.
        """
        with self._lock:
            subtask = self._require_locked(subtask_id)
            if not isinstance(result, ExecutionResult):
                raise InvalidSubtaskError("result must be an ExecutionResult")
            if subtask.status not in {SubtaskStatus.RUNNING, SubtaskStatus.PAUSED}:
                raise InvalidTransitionError(
                    f"Results can only be recorded while subtask {subtask_id!r} is running or paused.",
                    subtask_id,
                    subtask.status,
                    subtask.status,
                )
            trimmed = ExecutionResult.model_validate(
                {**result.model_dump(), "screenshot_after_base64": None}
            )
            subtask.results.append(trimmed)

    def record_recovery_attempt(self, subtask_id: str) -> int:
        """Count one bounded in-subtask recovery attempt; returns the new total."""
        with self._lock:
            subtask = self._require_locked(subtask_id)
            if subtask.status not in {SubtaskStatus.RUNNING, SubtaskStatus.PAUSED}:
                raise InvalidTransitionError(
                    f"Recovery attempts can only be recorded while subtask {subtask_id!r} is running or paused.",
                    subtask_id,
                    subtask.status,
                    subtask.status,
                )
            subtask.recovery_attempts += 1
            return subtask.recovery_attempts

    # -- dependency graph queries ----------------------------------------------------------

    def ready_set(self) -> list[str]:
        """Subtask ids that are ``pending`` with ALL dependencies ``completed``.

        A subtask is ready iff every dependency is completed (SubtasksProtocol section 3);
        returned in deterministic creation order.
        """
        with self._lock:
            ready: list[str] = []
            for subtask in self._subtasks.values():
                if subtask.status is not SubtaskStatus.PENDING:
                    continue
                if all(
                    self._subtasks[dep].status is SubtaskStatus.COMPLETED
                    for dep in subtask.depends_on
                ):
                    ready.append(subtask.subtask_id)
            return ready

    def dependents(self, subtask_id: str, transitive: bool = False) -> list[str]:
        """Ids that directly (or transitively) depend on ``subtask_id``, creation order."""
        with self._lock:
            self._require_locked(subtask_id)
            visited = self._dependents_of({subtask_id}) if transitive else self._direct_dependents(subtask_id)
            return [sid for sid in self._subtasks if sid in visited]

    # -- checkpoint round-trip (stable, serializable, restorable ids) ----------------------

    def snapshot(self) -> dict[str, Any]:
        """Full bounded state as serializable data (creation order; restore is lossless).

        ``results`` is emitted as a plain list so the snapshot is directly JSON-serializable
        (a raw ``deque`` would stringify as ``"deque([...])"`` under ``json.dumps``).
        """
        with self._lock:
            dumps: list[dict[str, Any]] = []
            for subtask in self._subtasks.values():
                dump = subtask.model_dump()
                dump["results"] = list(dump["results"])
                dumps.append(dump)
            return {"subtasks": dumps}

    def restore(self, data: Any) -> None:
        """Rebuild manager state from :meth:`snapshot` output; fail-closed on any defect.

        Accepts the snapshot mapping or a bare list of subtask dumps. Refuses (typed
        errors, manager left EMPTY and unchanged on failure): malformed shapes, invalid
        entities, more than ``max_subtasks`` entries, duplicate ids, unknown/self
        dependencies, and dependency cycles.
        """
        with self._lock:
            if self._subtasks:
                raise InvalidSubtaskError("cannot restore into a non-empty manager")
            if isinstance(data, dict):
                if set(data) != {"subtasks"} or not isinstance(data["subtasks"], list):
                    raise InvalidSubtaskError("snapshot must be a mapping with a 'subtasks' list")
                items = data["subtasks"]
            elif isinstance(data, (list, tuple)):
                items = list(data)
            else:
                raise InvalidSubtaskError(
                    f"snapshot must be a mapping or a list; got {type(data).__name__}"
                )
            if len(items) > self._max_subtasks:
                raise SubtaskLimitExceeded(
                    f"Snapshot holds {len(items)} subtasks; maximum is {self._max_subtasks}.",
                    self._max_subtasks,
                )
            restored: dict[str, Subtask] = {}
            for index, item in enumerate(items):
                try:
                    subtask = item if isinstance(item, Subtask) else Subtask.model_validate(item)
                except ValidationError as exc:
                    raise InvalidSubtaskError(f"invalid subtask at index {index}: {exc}") from exc
                if subtask.subtask_id in restored:
                    raise SubtaskAlreadyExistsError(
                        f"Snapshot holds duplicate subtask id {subtask.subtask_id!r}.",
                        subtask.subtask_id,
                    )
                restored[subtask.subtask_id] = subtask
            for subtask in restored.values():
                for dep in subtask.depends_on:
                    if dep == subtask.subtask_id:
                        raise SelfDependencyError(f"Subtask {subtask.subtask_id!r} depends on itself.")
                    if dep not in restored:
                        raise UnknownDependencyError(
                            f"Subtask {subtask.subtask_id!r} depends on unknown subtask {dep!r}.",
                            subtask.subtask_id,
                            dep,
                        )
            cycle = find_cycle({sid: set(s.depends_on) for sid, s in restored.items()})
            if cycle is not None:
                raise DependencyCycleError("Snapshot dependency cycle: " + " -> ".join(cycle))
            self._subtasks = restored

    # -- internals (caller must hold the lock) ---------------------------------------------

    def _require_locked(self, subtask_id: str) -> Subtask:
        subtask = self._subtasks.get(subtask_id)
        if subtask is None:
            raise UnknownSubtaskError(f"Unknown subtask: {subtask_id!r}.", subtask_id)
        return subtask

    @staticmethod
    def _apply_transition(subtask: Subtask, target: SubtaskStatus) -> None:
        allowed = TRANSITIONS[subtask.status]
        if target not in allowed:
            raise InvalidTransitionError(
                f"Transition {subtask.status.value} -> {target.value} is not permitted "
                f"for subtask {subtask.subtask_id!r}.",
                subtask.subtask_id,
                subtask.status,
                target,
            )
        subtask.status = target

    def _validated_dependencies(self, subtask_id: str, depends_on: Iterable[str] | None) -> list[str]:
        if depends_on is None:
            return []
        if isinstance(depends_on, (str, bytes)) or not isinstance(depends_on, Iterable):
            raise InvalidSubtaskError("depends_on must be an iterable of subtask ids")
        dep_ids: list[str] = []
        for dep in depends_on:
            if not _valid_id(dep):
                raise InvalidSubtaskError(
                    "each dependency must be a non-empty string of at most "
                    f"{_ID_MAX_LENGTH} characters without control characters"
                )
            if dep in dep_ids:
                raise InvalidSubtaskError(f"Duplicate dependency {dep!r}.")
            if dep == subtask_id:
                raise SelfDependencyError(f"Subtask {subtask_id!r} cannot depend on itself.")
            if dep not in self._subtasks:
                raise UnknownDependencyError(
                    f"Dependency {dep!r} does not exist yet.", subtask_id, dep
                )
            dep_ids.append(dep)
        graph = {sid: set(s.depends_on) for sid, s in self._subtasks.items()}
        graph[subtask_id] = set(dep_ids)
        cycle = find_cycle(graph)
        if cycle is not None:  # defensive: existing-only deps cannot close a cycle today
            raise DependencyCycleError("Dependency cycle: " + " -> ".join(cycle))
        return dep_ids

    def _direct_dependents(self, subtask_id: str) -> set[str]:
        return {sid for sid, s in self._subtasks.items() if subtask_id in s.depends_on}

    def _dependents_of(self, roots: set[str]) -> set[str]:
        """Transitive dependents of ``roots`` (BFS over reverse edges)."""
        visited: set[str] = set()
        queue: deque[str] = deque(roots)
        while queue:
            current = queue.popleft()
            for dep_id in self._direct_dependents(current):
                if dep_id not in visited and dep_id not in roots:
                    visited.add(dep_id)
                    queue.append(dep_id)
        return visited

    def _block_dependents_of(self, roots: set[str]) -> list[str]:
        """Transitively move ``pending|paused`` dependents of ``roots`` to ``blocked``."""
        blocked: list[str] = []
        for dep_id in self._dependents_of(roots):
            subtask = self._subtasks[dep_id]
            if subtask.status in {SubtaskStatus.PENDING, SubtaskStatus.PAUSED}:
                subtask.status = SubtaskStatus.BLOCKED
                blocked.append(dep_id)
        return blocked

    @staticmethod
    def _normalize_status_filter(status: SubtaskStatus | str | None) -> SubtaskStatus | None:
        if status is None:
            return None
        if isinstance(status, SubtaskStatus):
            return status
        try:
            return SubtaskStatus(status)
        except ValueError as exc:
            raise InvalidSubtaskError(f"Unknown status filter: {status!r}") from exc

    @staticmethod
    def _failure_summary(failure: SubtaskFailureInfo | None) -> dict[str, Any] | None:
        if failure is None:
            return None
        return {
            "failure_class": None if failure.failure_class is None else failure.failure_class.value,
            "error": failure.error[:_SUMMARY_FIELD_MAX_LENGTH],
            "recovery_attempts": failure.recovery_attempts,
            "last_known_state": failure.last_known_state[:_SUMMARY_FIELD_MAX_LENGTH],
            "recorded_at": failure.recorded_at.isoformat(),
        }

    def __iter__(self) -> Iterator[Subtask]:
        with self._lock:
            return iter([s.model_copy(deep=True) for s in self._subtasks.values()])
