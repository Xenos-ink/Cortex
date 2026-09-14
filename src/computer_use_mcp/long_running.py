"""Long-Running Runtime: per-session checkpoint owner (removed-loop survivor).

Layering (master-mission 003 section 5): this module sits near the TOP of the stack and
is imported only by ``server``. The removed-loop family it used to drive — sequential
subtask execution through ``ComputerUseAgent.run``, LLM planning (``plan_from_llm``),
approval epochs, health monitors, and the orchestration run gates — was REMOVED with the
run_goal loop. What survives here is the machinery the LIVE checkpoint/resume path still
consumes:

- the per-session component holders (subtask graph, shared session budget, bounded
  context) the resume manager rebuilds from a checkpoint,
- :meth:`LongRunningRuntime.capture_checkpoint_payload` / :meth:`LongRunningRuntime.checkpoint`
  (``stop_session`` writes the BEFORE_SESSION_END checkpoint through them), and
- the deterministic periodic cadence (:meth:`LongRunningRuntime.checkpoint_due`) used by
  :meth:`LongRunningRuntime._maybe_checkpoint`.

The runtime contains NO execution loop and never touches the backend for input; the
probe-state helper below exists only to record the environment identity that
checkpoints capture.

All mutable state is guarded by ``threading.RLock``; clocks are injectable for
deterministic tests (no sleeps).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .audit import AuditLogger, Metrics
from .checkpoint_manager import (
    CHECKPOINT_EVERY_SECONDS,
    CHECKPOINT_EVERY_STEPS,
    CheckpointError,
    CheckpointManager,
    CheckpointTrigger,
    EnvironmentExpectations,
    SessionSnapshot,
    TerminationState,
    should_checkpoint,
)
from .context_manager import ContextManager
from .limits import Limits, SessionBudgetTracker
from .models import MAX_SUBTASKS, TerminationReason
from .redaction import redact_text
from .subtask_manager import SubtaskManager

logger = logging.getLogger(__name__)

__all__ = [
    "LongRunningRuntime",
]

_BUDGET_REDACTED_STATE_MAX_CHARS = 300


def _redacted(text: Any, limit: int) -> str:
    """Redact + bound a controller-supplied string for audit/progress surfaces."""
    return redact_text(str(text))[0][:limit]


class _BackendProbeState:
    """Caches ONE backend observation per evaluation + the recorded expected environment.

    Probes must be cheap and side-effect free; the cache is refreshed lazily (the first
    probe of a capture triggers the observation, later probes reuse it). A capture
    failure is fail-closed data (``None``), never an exception.
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        self._observation: Any | None = None
        self.expected_app: str | None = None
        self.expected_window: str | None = None

    def refresh(self) -> Any:
        try:
            self._observation = self._backend.observe()
        except Exception:  # noqa: BLE001 - probe failure is fail-closed data
            self._observation = None
        return self._observation

    def current(self) -> Any | None:
        if self._observation is None:
            self.refresh()
        return self._observation

    def app(self) -> str | None:
        observation = self.current()
        if observation is None:
            return None
        info = getattr(observation, "active_window_info", None)
        if info is not None and getattr(info, "process_name", None):
            return str(info.process_name)
        return str(getattr(observation, "active_window", None) or "") or None

    def window(self) -> str | None:
        observation = self.current()
        if observation is None:
            return None
        info = getattr(observation, "active_window_info", None)
        title = getattr(info, "title", None) if info is not None else None
        title = title or getattr(observation, "active_window", None)
        return str(title) if title else None


class LongRunningRuntime:
    """Per-session checkpoint owner over the surviving component holders.

    Components: :class:`SubtaskManager` (dependency graph),
    :class:`SessionBudgetTracker` (shared, restorable session budget),
    :class:`ContextManager` (bounded context), :class:`CheckpointManager` (durable
    checkpoints). The runtime is constructed by the server per session (only when a
    checkpoint is resumed; tests may arm it through the module seam) and persists in
    the server's session bundle between MCP calls (conflict C7).
    """

    def __init__(
        self,
        *,
        session_id: str,
        goal: str = "",
        agent: Any,
        state: Any,
        limits: Limits,
        backend: Any | None = None,
        auditor: AuditLogger | None = None,
        metrics: Metrics | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        provider: Any | None = None,
        allowed_processes: list[str] | None = None,
        allowed_windows: list[str] | None = None,
        clock: Callable[[], float] | None = None,
        subtasks: SubtaskManager | None = None,
        budget: SessionBudgetTracker | None = None,
        context: ContextManager | None = None,
        continuation_of: str | None = None,
        expected_environment: EnvironmentExpectations | None = None,
    ) -> None:
        self._session_id = str(session_id)
        self._clock = clock if clock is not None else time.monotonic
        self._agent = agent
        self._state = state
        self._limits = limits.validate()
        self._backend = backend
        self._auditor = auditor
        self._metrics = metrics
        self._provider = provider
        self._checkpoints = checkpoint_manager if checkpoint_manager is not None else CheckpointManager()
        self._lock = threading.RLock()
        self._terminated_reason: TerminationReason | None = None
        self._current_subtask_id: str | None = None
        # Checkpoint anchors (periodic cadence: steps since / seconds since last write).
        self._checkpoint_steps_anchor = 0
        self._checkpoint_monotonic = self._clock()
        self._last_checkpoint_trigger: str | None = None
        self._last_checkpoint_at: str | None = None

        # The budget tracker's duration anchor is the REAL monotonic clock (its
        # elapsed_seconds() has no clock injection); checkpoint cadence runs on the
        # injectable runtime clock.
        self._budget = budget if budget is not None else SessionBudgetTracker(self._limits)
        self._subtasks = subtasks if subtasks is not None else SubtaskManager(
            max_subtasks=max(1, min(int(self._limits.max_subtasks), MAX_SUBTASKS))
        )
        self._context = context if context is not None else ContextManager(goal=goal)
        self._continuation_of = continuation_of
        self._allowed_processes = list(allowed_processes or [])
        self._allowed_windows = list(allowed_windows or [])
        # Environment probes bound to the REAL session backend surface (tests may inject
        # a backend instead); a resumed session starts with the CHECKPOINT's recorded
        # expectations.
        self._probe_state = _BackendProbeState(backend) if backend is not None else None
        if expected_environment is not None:
            self._probe_state.expected_app = expected_environment.active_process_name
            self._probe_state.expected_window = expected_environment.active_window_title
        if goal:
            self._context.set_goal(goal)

    # ------------------------------------------------------------------ wiring helpers

    def _audit(self, event_type: str, *, result: str | None = None, **metadata: Any) -> None:
        if self._auditor is None:
            return
        merged = {key: value for key, value in metadata.items() if value not in (None, "")}
        try:
            self._auditor.emit(
                str(event_type),
                self._session_id,
                result=result,
                metadata=merged,
            )
        except Exception:  # audit failures never break orchestration
            logger.debug("audit write failed", exc_info=True)

    # ------------------------------------------------------------------ introspection

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def limits(self) -> Limits:
        return self._limits

    @property
    def subtasks(self) -> SubtaskManager:
        return self._subtasks

    @property
    def budget(self) -> SessionBudgetTracker:
        return self._budget

    @property
    def context(self) -> ContextManager:
        return self._context

    @property
    def checkpoints(self) -> CheckpointManager:
        return self._checkpoints

    @property
    def goal(self) -> str:
        return self._context._goal  # single-owner read of the bounded goal

    @property
    def continuation_of(self) -> str | None:
        return self._continuation_of

    def checkpoint_due(self) -> bool:
        """Pure periodic cadence: 50 steps OR 30 minutes since the last checkpoint."""
        with self._lock:
            steps_since = max(0, int(self._budget.snapshot()["steps"]) - self._checkpoint_steps_anchor)
            seconds_since = max(0.0, self._clock() - self._checkpoint_monotonic)
        return should_checkpoint(
            steps_since,
            seconds_since,
            every_steps=CHECKPOINT_EVERY_STEPS,
            every_seconds=CHECKPOINT_EVERY_SECONDS,
        )

    # ------------------------------------------------------------------ checkpoints

    def capture_checkpoint_payload(
        self,
        *,
        trigger: CheckpointTrigger = CheckpointTrigger.MANUAL,
        current_subtask_id: str | None = None,
    ) -> Any:
        """Build a validated checkpoint payload from the live component snapshots."""
        environment = EnvironmentExpectations(
            active_process_name=self._probe_state.app() if self._probe_state else None,
            active_window_title=self._probe_state.window() if self._probe_state else None,
            app_window_state=_redacted(
                f"{self._probe_state.app() if self._probe_state else ''}|"
                f"{self._probe_state.window() if self._probe_state else ''}",
                300,
            ),
            allowed_processes=list(self._allowed_processes)[:64],
            allowed_windows=list(self._allowed_windows)[:64],
        )
        session = SessionSnapshot(
            status=str(getattr(getattr(self._agent, "task", None), "status", "idle")),
            dry_run=bool(getattr(self._state, "dry_run", True)),
            require_approval=bool(getattr(self._state, "require_approval", True)),
            max_steps=int(getattr(self._state, "max_steps", 30)),
            max_retries_per_action=int(getattr(self._state, "max_retries_per_action", 1)),
            min_confidence=float(getattr(self._state, "min_confidence", 0.70)),
            stopped=bool(getattr(self._state, "stopped", False)),
        )
        termination = TerminationState(
            terminated=self._terminated_reason is not None,
            reason=self._terminated_reason.value if self._terminated_reason is not None else None,
        )
        goal = self.goal.strip() or "(no goal)"
        return self._checkpoints.capture_payload(
            session_id=self._session_id,
            goal=goal[:2_000],
            subtasks_snapshot=self._subtasks.snapshot(),
            budget_snapshot=self._budget.snapshot(),
            limits=self._limits,
            context_snapshot=self._context.snapshot(),
            session=session,
            environment=environment,
            current_subtask_id=current_subtask_id,
            termination=termination,
            trigger=trigger,
            continuation_of=self._continuation_of,
        )

    def checkpoint(
        self,
        trigger: CheckpointTrigger | str = CheckpointTrigger.MANUAL,
        *,
        current_subtask_id: str | None = None,
    ) -> Path:
        """Write a checkpoint NOW (raises typed :class:`CheckpointError` on failure)."""
        payload = self.capture_checkpoint_payload(
            trigger=trigger, current_subtask_id=current_subtask_id
        )
        path = self._checkpoints.write(payload)
        with self._lock:
            self._checkpoint_steps_anchor = int(self._budget.snapshot()["steps"])
            self._checkpoint_monotonic = self._clock()
            self._last_checkpoint_trigger = str(
                trigger.value if isinstance(trigger, CheckpointTrigger) else trigger
            )
            self._last_checkpoint_at = payload.created_at.isoformat()
        self._audit(
            "checkpoint",
            result=self._last_checkpoint_trigger,
            metadata={"path": str(path), "trigger": self._last_checkpoint_trigger},
        )
        return path

    def _maybe_checkpoint(
        self,
        trigger: CheckpointTrigger,
        *,
        force: bool,
        current_subtask_id: str | None = None,
    ) -> Path | None:
        """Lifecycle triggers checkpoint immediately; otherwise the periodic cadence gates.

        The removed loop family called this at subtask boundaries; the surviving caller
        is session teardown (BEFORE_SESSION_END, forced) via ``stop_session``.
        """
        if not force and not self.checkpoint_due():
            return None
        try:
            return self.checkpoint(
                trigger, current_subtask_id=current_subtask_id or self._current_subtask_id
            )
        except CheckpointError as exc:
            # Durability failure: the previous checkpoint on disk stays intact; the
            # session continues (a checkpoint is not a safety gate) but is audited.
            self._audit("checkpoint", result="failed", metadata={"detail": str(exc)[:200]})
            return None
