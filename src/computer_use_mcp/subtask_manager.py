"""Deterministic subtask domain state: entities, transitions, and the dependency graph.

Layering (master-mission 003 section 5): this module imports only ``models`` and the pure
graph helpers of ``plan_validator``; it never imports orchestration, MCP, or backend
surfaces. It contains NO execution: the removed loop family (and its single
``ComputerUseAgent.run`` executor) is gone; only the checkpoint/resume container remains.

Doctrine (SubtasksProtocol sections 1/3/4/16, post loop-removal):
- The MUTATION/transition APIs the removed loop family drove (create/start/complete/
  fail/pause/resume/requeue, result + recovery bookkeeping, the fixed TRANSITIONS
  table) were REMOVED with it. What survives is the checkpoint/resume surface: the
  constructor + ceiling, :meth:`SubtaskManager.snapshot`, the fail-closed
  :meth:`SubtaskManager.restore` (re-checking ids, dependencies, and cycles), and the
  pure READ projections over the restored graph (ids/get/require/list/counts/
  ready_set/dependents) that checkpoint-fidelity tests pin.
- ``subtask_id`` values are stable and serializable: :meth:`SubtaskManager.snapshot` and
  :meth:`SubtaskManager.restore` round-trip them losslessly for checkpoint/resume.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterator
from typing import Any

from pydantic import ValidationError

from .models import MAX_SUBTASKS, Subtask, SubtaskFailureInfo, SubtaskStatus
from .plan_validator import find_cycle

__all__ = [
    "DependencyCycleError",
    "InvalidSubtaskError",
    "SelfDependencyError",
    "SubtaskAlreadyExistsError",
    "SubtaskError",
    "SubtaskLimitExceeded",
    "SubtaskManager",
    "UnknownDependencyError",
    "UnknownSubtaskError",
]


_ID_MAX_LENGTH = 128
_DESCRIPTION_MAX_LENGTH = 2_000
#: Bounded projection lengths for ``list()`` summaries (MCP-facing output stays small).
_SUMMARY_FIELD_MAX_LENGTH = 300


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


class SubtaskManager:
    """Thread-safe, bounded holder of one session's subtask entities and dependency graph.

    Post loop-removal this is a checkpoint/resume container: subtasks enter ONLY via
    :meth:`restore` (fail-closed); the mutation APIs the removed orchestration drove no
    longer exist.

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

    # -- internals (caller must hold the lock) ---------------------------------------------

    def _require_locked(self, subtask_id: str) -> Subtask:
        subtask = self._subtasks.get(subtask_id)
        if subtask is None:
            raise UnknownSubtaskError(f"Unknown subtask: {subtask_id!r}.", subtask_id)
        return subtask

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
