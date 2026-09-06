"""Long-Running Runtime: the orchestration layer ABOVE the existing closed-loop executor.

Layering (master-mission 003 section 5): this module sits at the TOP of the stack. It
imports the Wave-1/2 surfaces (``subtask_manager``, ``plan_validator``, ``limits``,
``context_manager``, ``checkpoint_manager``, ``approval``, ``health``), ``models``,
``audit``, and ``redaction`` — and is imported only by ``server``. It contains **NO new
execution loop**: every subtask is executed by exactly ONE call to
``ComputerUseAgent.run`` (the single closed-loop executor); the runtime decides WHEN,
never HOW, actions happen. There are no threads and no schedulers: everything is
evaluated deterministically at orchestration boundaries, inside MCP tool calls.

Doctrines implemented here (SubtasksProtocol sections 3/4/7/8/10/11/12/13, conflicts
C2-C9 of the master mission):

- **Sequential execution through the existing executor** (C2/C3): a ready subtask is
  started via :class:`~computer_use_mcp.subtask_manager.SubtaskManager` (ALL dependencies
  completed), then run with ONE ``agent.run(subtask.description, state, approval)``
  call. Nothing else ever executes actions.
- **Per-subtask limits + shared session budget** (spec section 3): each subtask gets a
  FRESH per-subtask :class:`~computer_use_mcp.limits.LimitEnforcer` scope (installed via
  the minimal ``ComputerUseAgent.set_enforcer`` seam — no loop change); its consumption
  is recorded on BOTH the subtask's own accounting and the SHARED
  :class:`~computer_use_mcp.limits.SessionBudgetTracker`. A subtask can never reset any
  session counter: the tracker only grows within a session's life.
- **Bounded results** (spec section 4/C5): results are recorded per subtask via
  ``SubtaskManager.record_result`` (heavy screenshots stripped, cap 20); orchestration
  response lists are capped at :data:`MAX_ORCHESTRATION_RESULTS`.
- **Bounded replan** (spec section 15, ``RecoveryStrategy.REPLAN_REMAINING``): on
  subtask failure the manager blocks transitive dependents automatically; the runtime
  may ask the planner for replacement work (at most :data:`MAX_REPLAN_ATTEMPTS` times,
  never depending on dead ids). A terminal-failed prerequisite can never complete, so
  blocked dependents of a failed subtask are permanently unrunnable — requeue cannot
  revive them; when nothing executable remains and dead work exists the session ends
  with :attr:`~computer_use_mcp.models.TerminationReason.UNRECOVERABLE`.
- **Approval epochs** (spec section 11/C9): every approval flows through
  :meth:`~computer_use_mcp.approval.ApprovalEpochManager.authorize_action`. Epoch expiry
  (30 min / N interactive actions) or material change stops execution FAIL-CLOSED until
  a fresh epoch is granted. ``require_approval=false`` is a full-scope epoch that STILL
  expires. Interactive actions are recorded via ``record_interactive_action``.
- **Prolonged unattended execution** (spec section 12): the raise-only
  :func:`~computer_use_mcp.approval.effective_protection` modifier is consulted at every
  boundary; while it is active a full-scope session may not START new subtasks without a
  fresh approval epoch. It can only raise protection, never lower it.
- **Health checks** (spec section 13/C6): :class:`~computer_use_mcp.health.HealthMonitor`
  probes bound to the real session backend, evaluated via ``maybe_check`` at boundaries
  only. UNSAFE stops execution (epoch invalidated — the environment changed);
  DEGRADED gets one bounded re-evaluation and pauses otherwise. The monitor never
  executes actions.
- **Checkpoints** (spec section 7): written at every lifecycle trigger (subtask
  completed/failed/transition), on the periodic 50-steps/30-minutes cadence, and before
  session end; atomic, redacted, fail-closed via
  :class:`~computer_use_mcp.checkpoint_manager.CheckpointManager`.
- **Deterministic progress** (spec section 9): the progress percentage is computed from
  manager state (completed/total) — never invented.

All mutable state is guarded by ``threading.RLock`` (state) and a dedicated
non-reentrant execution lock (one subtask execution at a time per session); clocks are
injectable for deterministic tests (no sleeps).
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .approval import ApprovalEpochManager, InvalidationReason, effective_protection
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
from .context_manager import ContextManager, ContextSummarizer
from .health import ExpectedEnvironment, HealthMonitor, HealthProbes, HealthVerdict
from .limits import LimitEnforcer, Limits, SessionBudgetExceeded, SessionBudgetTracker
from .models import (
    MAX_SUBTASKS,
    ExecutionResult,
    FailureClass,
    GroundedAction,
    Subtask,
    SubtaskPlanEntry,
    TerminationReason,
)
from .plan_validator import PlanRejectedError, PlanValidator
from .redaction import redact_text
from .subtask_manager import (
    SubtaskAlreadyExistsError,
    SubtaskLimitExceeded,
    SubtaskManager,
    SubtaskStatus,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ORCHESTRATION_RESULTS",
    "MAX_REPLAN_ATTEMPTS",
    "LongRunningError",
    "LongRunningRuntime",
    "OrchestrationOutcome",
    "PlannerUnavailableError",
    "RuntimeBusyError",
    "SubtaskNotRunnableError",
    "SubtaskRunOutcome",
]

#: Bounded replanning: the planner may be consulted at most this many times per session
#: to re-plan REMAINING work after a subtask failure (spec section 15: bounded replan).
MAX_REPLAN_ATTEMPTS = 3

#: Hard cap on the flattened ExecutionResult list carried by one orchestration outcome
#: (bounded responses, spec section 19). Per-subtask retention keeps the full bounded
#: record (cap 20 each); the outcome list keeps only the most recent slice.
MAX_ORCHESTRATION_RESULTS = 200

#: Bounded metadata strings (audit/progress surfaces never carry unbounded text).
_DETAIL_MAX_CHARS = 500
_DESCRIPTION_MAX_CHARS = 200
_GOAL_MAX_CHARS = 2_000


class LongRunningError(RuntimeError):
    """Base class for typed long-running orchestration failures (fail closed)."""


class PlannerUnavailableError(LongRunningError):
    """The LLM planner is unavailable or failed (no key, transport, parse failure).

    Typed fail-closed degradation: the session stays fully usable for MANUAL subtask
    creation (``create_subtask``) — only the automatic planning path is refused.
    """

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail[:_DETAIL_MAX_CHARS]


class RuntimeBusyError(LongRunningError):
    """Another subtask execution is already in progress for this session."""


class SubtaskNotRunnableError(LongRunningError):
    """The referenced subtask cannot be executed in its current state."""


def _redacted(text: Any, limit: int) -> str:
    """Redact + bound a controller-supplied string for audit/progress surfaces."""
    return redact_text(str(text))[0][:limit]


class _ApprovalBudget:
    """The per-call interactive-action approval budget (run_goal semantics: at most 1)."""

    def __init__(self, total: int) -> None:
        self.remaining = max(0, int(total))

    def available(self) -> bool:
        return self.remaining > 0

    def consume(self) -> None:
        if self.remaining > 0:
            self.remaining -= 1


@dataclass
class SubtaskRunOutcome:
    """Bounded outcome of executing ONE subtask through the existing executor."""

    subtask_id: str
    status: str
    termination_reason: str | None = None
    results: list[ExecutionResult] = field(default_factory=list)
    requires_approval: bool = False
    stopped: bool = False
    actions_used: int = 0
    model_calls_used: int = 0
    steps_used: int = 0
    detail: str = ""


@dataclass
class OrchestrationOutcome:
    """Bounded outcome of an orchestration run (one subtask, or the whole plan)."""

    ok: bool
    termination_reason: str | None = None
    requires_approval: bool = False
    stopped: bool = False
    results: list[ExecutionResult] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)
    replan_attempts: int = 0
    health_verdict: str | None = None
    approval_budget_remaining: int = 0
    detail: str = ""


def _creation_order(entries: Sequence[SubtaskPlanEntry]) -> list[SubtaskPlanEntry]:
    """Deterministic topological order (dependencies first; validator order tie-breaks).

    ``SubtaskManager.create`` validates dependencies against ALREADY-created subtasks, so
    a plan entry may only be created after its in-plan dependencies. The plan validator
    guarantees acyclicity; this walk is a defensive no-op for cycles.
    """
    by_id = {entry.subtask_id: entry for entry in entries}
    ordered: list[SubtaskPlanEntry] = []
    state: dict[str, int] = {}

    def visit(entry: SubtaskPlanEntry) -> None:
        marker = state.get(entry.subtask_id, 0)
        if marker == 2:
            return
        if marker == 1:  # defensive: the validator already rejects cycles
            return
        state[entry.subtask_id] = 1
        for dep in entry.depends_on:
            dep_entry = by_id.get(dep)
            if dep_entry is not None:
                visit(dep_entry)
        state[entry.subtask_id] = 2
        ordered.append(entry)

    for entry in entries:
        visit(entry)
    return ordered


class _BackendProbeState:
    """Caches ONE backend observation per evaluation + the recorded expected environment.

    Probes must be cheap and side-effect free for the executor; the cache is refreshed
    lazily (first probe of an evaluation triggers the capture, later probes reuse it).
    A capture failure is fail-closed data (``None``), never an exception.
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

    def observation_ok(self) -> bool:
        observation = self.current()
        return observation is not None and bool(getattr(observation, "image_base64", None))

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

    def hung_indicators(self) -> Sequence[str]:
        indicators: list[str] = []
        blocked = getattr(self._backend, "_input_blocked", None)
        if blocked is None:
            getter = getattr(self._backend, "is_input_blocked", None)
            blocked = getter() if callable(getter) else False
        if blocked:
            indicators.append("input_blocked")
        return indicators


class LongRunningRuntime:
    """Per-session orchestration state + the sequential subtask execution driver.

    Components (all Wave-1/2 surfaces): :class:`SubtaskManager` (dependency graph),
    :class:`SessionBudgetTracker` (shared, restorable session budget),
    :class:`ContextManager` (bounded context + provider-backed summarizer),
    :class:`ApprovalEpochManager` (fail-closed approval epochs),
    :class:`HealthMonitor` (boundary health checks), :class:`CheckpointManager`
    (durable checkpoints). The runtime is constructed by the server per session and
    persists in the server's session bundle between MCP calls (conflict C7).
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
        health_monitor: HealthMonitor | None = None,
        provider: Any | None = None,
        summarizer: ContextSummarizer | None = None,
        allowed_processes: list[str] | None = None,
        allowed_windows: list[str] | None = None,
        clock: Callable[[], float] | None = None,
        subtasks: SubtaskManager | None = None,
        budget: SessionBudgetTracker | None = None,
        context: ContextManager | None = None,
        continuation_of: str | None = None,
        expected_environment: EnvironmentExpectations | None = None,
        resumed: bool = False,
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
        # One subtask execution at a time per session (non-reentrant on purpose: MCP tool
        # calls must never interleave orchestration work on the same session).
        self._execution_lock = threading.Lock()
        self._execution_active = False
        self._terminated_reason: TerminationReason | None = None
        self._current_subtask_id: str | None = None
        self._replan_attempts = 0
        self._superseded_failed: set[str] = set()
        self._approval_block: str | None = None
        self._health_hold: str | None = None
        self._last_human_interaction_monotonic: float | None = (
            self._clock() if resumed else None
        )
        # Checkpoint anchors (periodic cadence: steps since / seconds since last write).
        self._checkpoint_steps_anchor = 0
        self._checkpoint_monotonic = self._clock()
        self._last_checkpoint_trigger: str | None = None
        self._last_checkpoint_at: str | None = None

        self._require_approval = bool(getattr(state, "require_approval", True))
        self._epochs = ApprovalEpochManager(
            self._limits,
            full_scope=not self._require_approval,
            clock=self._clock,
        )
        # The budget tracker's duration anchor is the REAL monotonic clock (its
        # elapsed_seconds() has no clock injection); checkpoint cadence, approval-epoch
        # expiry, and the unattended modifier all run on the injectable runtime clock.
        self._budget = budget if budget is not None else SessionBudgetTracker(self._limits)
        self._session_start_clock = self._clock()
        self._subtasks = subtasks if subtasks is not None else SubtaskManager(
            max_subtasks=max(1, min(int(self._limits.max_subtasks), MAX_SUBTASKS))
        )
        if summarizer is None and provider is not None and hasattr(provider, "summarize_context"):
            summarizer = self._provider_summarizer
        self._context = context if context is not None else ContextManager(
            goal=goal,
            summarizer=summarizer,
            summarize_every=self._limits.context_summarize_every,
        )
        self._continuation_of = continuation_of
        self._allowed_processes = list(allowed_processes or [])
        self._allowed_windows = list(allowed_windows or [])
        # Health probes bound to the REAL session backend/validator surface (spec: the
        # orchestrator binds real probes; tests may inject a full monitor instead).
        self._probe_state = _BackendProbeState(backend) if backend is not None else None
        if expected_environment is not None:
            # A resumed session starts with the CHECKPOINT's recorded expectations.
            self._probe_state.expected_app = expected_environment.active_process_name
            self._probe_state.expected_window = expected_environment.active_window_title
        if health_monitor is not None:
            self._health = health_monitor
        else:
            self._health = HealthMonitor(
                self._limits,
                probes=self._build_probes(),
                clock=self._clock,
            )
        if goal:
            self._context.set_goal(goal)

    # ------------------------------------------------------------------ wiring helpers

    def _provider_summarizer(self, request: Any) -> Any:
        """Adapt the (lazy) provider onto A2's ``ContextSummarizer`` callable interface."""
        provider = self._provider
        if provider is None:
            return None
        outcome = provider.summarize_context(request)
        if inspect.isawaitable(outcome):
            return outcome  # ContextManager awaits awaitable summarizer results
        return outcome

    def _build_probes(self) -> HealthProbes:
        if self._probe_state is None:
            return HealthProbes.fail_closed()
        probe_state = self._probe_state

        def _expectation() -> ExpectedEnvironment | None:
            # First evaluation records the session-start environment as the expectation
            # (the runtime never touches the backend at construction time).
            with self._lock:
                if probe_state.expected_app is None and probe_state.expected_window is None:
                    probe_state.expected_app = probe_state.app()
                    probe_state.expected_window = probe_state.window()
                return ExpectedEnvironment(
                    active_app=probe_state.expected_app,
                    active_window=probe_state.expected_window,
                )

        return HealthProbes(
            observation=probe_state.observation_ok,
            app=probe_state.app,
            window=probe_state.window,
            hung=probe_state.hung_indicators,
            expectation=_expectation,
        )

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
    def epochs(self) -> ApprovalEpochManager:
        return self._epochs

    @property
    def health(self) -> HealthMonitor:
        return self._health

    @property
    def checkpoints(self) -> CheckpointManager:
        return self._checkpoints

    @property
    def goal(self) -> str:
        return self._context._goal  # single-owner read of the bounded goal

    @property
    def continuation_of(self) -> str | None:
        return self._continuation_of

    def _session_status(self) -> str:
        if self._terminated_reason is not None:
            return self._terminated_reason.value
        if self._execution_active:
            return "running"
        return "idle"

    def checkpoint_status(self) -> dict[str, Any]:
        """Bounded checkpoint status (has_checkpoint / last trigger / last timestamp)."""
        return {
            "has_checkpoint": self._checkpoints.has_checkpoint(self._session_id),
            "last_trigger": self._last_checkpoint_trigger,
            "last_checkpoint_at": self._last_checkpoint_at,
        }

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

    def counts(self) -> dict[str, int]:
        return self._subtasks.counts()

    def list_subtasks(self, status: str | None = None) -> list[dict[str, Any]]:
        """Bounded structured subtask summaries (no result payloads, no history)."""
        return self._subtasks.list(status)

    def progress(self) -> dict[str, Any]:
        """Deterministic progress report (computed from state; never invented)."""
        counts = self._subtasks.counts()
        total = len(self._subtasks)
        completed = counts.get("completed", 0)
        percent = round(100.0 * completed / total, 2) if total > 0 else 0.0
        budget_snapshot = self._budget.snapshot()
        return {
            "session_id": self._session_id,
            "continuation_of": self._continuation_of,
            "status": self._session_status(),
            "termination_reason": (
                self._terminated_reason.value if self._terminated_reason is not None else None
            ),
            "goal": _redacted(self.goal, _GOAL_MAX_CHARS),
            "total_subtasks": total,
            "completed_subtasks": completed,
            "progress_percent": percent,
            "counts": counts,
            "current_subtask_id": self._current_subtask_id,
            "elapsed_seconds": round(float(budget_snapshot["elapsed_seconds"]), 3),
            "resource_counters": {
                "actions": int(budget_snapshot["actions"]),
                "model_calls": int(budget_snapshot["model_calls"]),
                "steps": int(budget_snapshot["steps"]),
                "subtasks": int(budget_snapshot["subtasks"]),
            },
            "resource_limits": {
                "max_session_seconds": self._limits.max_session_seconds,
                "max_session_actions": self._limits.max_session_actions,
                "max_session_model_calls": self._limits.max_session_model_calls,
                "max_session_steps": self._limits.max_session_steps,
                "max_subtasks": self._limits.max_subtasks,
            },
            "approval_epoch": self._epochs.snapshot(),
            "checkpoint": self.checkpoint_status(),
            "replan_attempts_used": self._replan_attempts,
            "replan_attempts_max": MAX_REPLAN_ATTEMPTS,
        }

    # ------------------------------------------------------------------ goal + planning

    def set_goal(self, goal: str) -> None:
        """Update the session goal; a MATERIAL goal change invalidates the live epoch."""
        previous = self.goal
        self._context.set_goal(goal)
        if previous and previous != self.goal:
            self._epochs.invalidate(InvalidationReason.GOAL_CHANGED, detail="session goal changed")
            self._audit("approval_epoch", result="invalidated", reason="goal_changed")

    async def plan_from_llm(self, goal: str | None = None) -> list[SubtaskPlanEntry]:
        """Request a plan from the provider, validate it deterministically, populate.

        The LLM plan is an UNTRUSTED PROPOSAL (spec section 2): it is validated by
        :class:`PlanValidator` BEFORE anything is created, and creation goes through
        :meth:`SubtaskManager.create` (cap, dependency, cycle rules re-checked there).
        Any provider failure (missing key, transport, parse) raises the typed
        :class:`PlannerUnavailableError` — fail-closed degradation; the session stays
        usable for manual ``create_subtask``.
        """
        planner = self._provider
        if planner is None or not callable(getattr(planner, "plan_subtasks", None)):
            raise PlannerUnavailableError(
                "no planner is bound to this session; create subtasks manually with create_subtask"
            )
        effective_goal = goal or self.goal or ""
        if not effective_goal.strip():
            raise PlannerUnavailableError("no goal was supplied for planning")
        remaining = self._subtasks.max_subtasks - len(self._subtasks)
        if remaining <= 0:
            raise SubtaskLimitExceeded(
                f"Subtask limit reached ({self._subtasks.max_subtasks}); planning refused.",
                self._subtasks.max_subtasks,
            )
        try:
            raw = planner.plan_subtasks(effective_goal)
            if inspect.isawaitable(raw):
                raw = await raw
        except Exception as exc:  # any planner failure is typed degradation
            raise PlannerUnavailableError(
                "planner unavailable (fail-closed); the session stays usable for manual "
                "create_subtask",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        entries = PlanValidator(max_subtasks=remaining).validate_or_raise(raw)
        existing = set(self._subtasks.ids())
        for entry in entries:
            if entry.subtask_id in existing:
                raise SubtaskAlreadyExistsError(
                    f"Plan reuses existing subtask id {entry.subtask_id!r}; refusing the "
                    "whole plan (fail-closed).",
                    entry.subtask_id,
                )
        created = self._create_entries(entries)
        self._context.add_plan_note(f"LLM plan accepted: {created} subtask(s) for the session goal")
        return entries

    def _create_entries(
        self, entries: Sequence[SubtaskPlanEntry], *, skip_existing: bool = False
    ) -> int:
        """Create validated plan entries in dependency order (deterministic, audited)."""
        existing = set(self._subtasks.ids())
        created = 0
        for entry in _creation_order(entries):
            if skip_existing and entry.subtask_id in existing:
                continue
            subtask = self._subtasks.create(
                entry.description, entry.depends_on, entry.subtask_id
            )
            created += 1
            self._audit(
                "subtask_created",
                result=subtask.subtask_id,
                metadata={
                    "source": "planner",
                    "depends_on": list(subtask.depends_on),
                    "description": _redacted(subtask.description, _DESCRIPTION_MAX_CHARS),
                },
            )
        return created

    def create_subtask(
        self,
        description: str,
        depends_on: Sequence[str] | None = None,
        subtask_id: str | None = None,
    ) -> Subtask:
        """Manual subtask creation (spec section 9); same domain rules as planned ones."""
        subtask = self._subtasks.create(description, list(depends_on or []), subtask_id)
        self._audit(
            "subtask_created",
            result=subtask.subtask_id,
            metadata={
                "source": "manual",
                "depends_on": list(subtask.depends_on),
                "description": _redacted(subtask.description, _DESCRIPTION_MAX_CHARS),
            },
        )
        return subtask

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
        """Lifecycle triggers checkpoint immediately; otherwise the periodic cadence gates."""
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

    # ------------------------------------------------------------------ budget/approval gates

    def _stopped(self) -> bool:
        return bool(getattr(self._state, "stopped", False)) or bool(
            getattr(getattr(self._agent, "stop_token", None), "stopped", False)
        )

    def _effective_protection(self) -> Any:
        return effective_protection(
            session_elapsed_seconds=max(0.0, self._clock() - self._session_start_clock),
            last_human_interaction_monotonic=self._last_human_interaction_monotonic,
            now_monotonic=self._clock(),
        )

    def _refresh_human_approval(self) -> None:
        """A fresh EXPLICIT human approval (an MCP call with ``approve_next_action=True``).

        Issues a NEW approval epoch (the only renewal path — nothing auto-renews) and
        restarts the unattended clock: the call itself is the human interaction. This is
        the fail-closed renewal route for a dead epoch or an active unattended hold.
        """
        now = self._clock()
        self._epochs.grant(now=now)
        self._last_human_interaction_monotonic = now
        self._approval_block = None
        self._audit("approval_epoch", result="refreshed", metadata={"source": "explicit_call_approval"})

    def _make_approval_callback(self, budget: _ApprovalBudget) -> Callable[[Any, str], bool]:
        """The agent's per-action approval callback, gated by the approval epoch (C9)."""

        def _approve(action: Any, reason: str) -> bool:
            decision = self._epochs.authorize_action(approved=budget.available())
            if decision.requires_fresh_approval:
                self._approval_block = decision.reason
                self._audit(
                    "approval_epoch",
                    result="refused_fresh_approval_required",
                    metadata={"epoch_id": decision.epoch_id, "reason": decision.reason[:200]},
                )
                return False
            if not decision.granted:
                return False  # per-action approval required and no human budget left
            budget.consume()
            if not self._epochs.full_scope:
                # A real per-action human approval just happened: the unattended clock
                # restarts (raise-only policy can only tighten, never loosen).
                self._last_human_interaction_monotonic = self._clock()
            self._epochs.record_interactive_action()
            self._audit(
                "approval_epoch",
                result="granted",
                metadata={"epoch_id": decision.epoch_id, "reason": _redacted(reason, 200)},
            )
            return True

        return _approve

    def _epoch_gate(self) -> tuple[bool, str]:
        """Boundary approval-epoch gate; returns (blocked, reason)."""
        decision = self._epochs.authorize_action()
        if decision.requires_fresh_approval:
            self._audit(
                "approval_epoch",
                result="expired_stop",
                metadata={"epoch_id": decision.epoch_id, "reason": decision.reason[:200]},
            )
            return True, decision.reason
        protection = self._effective_protection()
        if protection.modifier_active and self._epochs.full_scope:
            # Raise-only runtime policy (spec section 12): after an hour unattended, a
            # full-scope standing grant may not start NEW subtasks — fresh approval is
            # required. Non-full-scope sessions already gate every interactive action.
            reason = (
                "prolonged unattended execution: non-routine work requires a fresh "
                "approval epoch (raise-only runtime policy)"
            )
            self._audit("approval_epoch", result="unattended_hold", metadata={"reason": reason})
            return True, reason
        return False, ""

    # ------------------------------------------------------------------ execution

    def _swap_enforcer(self, enforcer: LimitEnforcer) -> None:
        """Install the per-subtask limit scope on the executor (minimal seam)."""
        setter = getattr(self._agent, "set_enforcer", None)
        if callable(setter):
            setter(enforcer)
            return
        if hasattr(self._agent, "enforcer"):
            self._agent.enforcer = enforcer  # fake/test agents without the seam

    def _fold_history(self) -> None:
        """Fold the executor's cross-run history into the bounded context window (§6/C10).

        The agent's ``history`` list would otherwise grow across subtasks until the
        fail-closed ``max_context_items`` gate kills the session; folding moves it into
        the ContextManager's bounded window (recent cap + summary) and clears it, so the
        model receives only the current run's bounded window plus the summarized state.
        """
        history = getattr(self._agent, "history", None)
        if isinstance(history, list):
            for entry in history:
                self._context.append_history(str(entry))
            history.clear()

    def _remaining_session_steps(self) -> int:
        used = int(self._budget.snapshot()["steps"])
        return max(0, int(self._limits.max_session_steps) - used)

    def _capped_run_state(self) -> Any:
        """A run-state whose step loop can never exceed the REMAINING session budget.

        The executor bounds one run by ``state.max_steps`` loop iterations and by the
        safety gate ``state.step_count >= state.max_steps``. Capping ``max_steps`` to
        ``step_count + remaining_session_budget`` bounds the subtask's step consumption
        by the shared session budget (a subtask can never overspend the session).
        """
        state = self._state
        max_steps = int(getattr(state, "max_steps", 30) or 30)
        step_count = int(getattr(state, "step_count", 0) or 0)
        loop_room = max(0, max_steps - step_count)
        remaining = self._remaining_session_steps()
        if remaining < loop_room:
            capped = step_count + max(1, remaining)
            return state.model_copy(update={"max_steps": capped})
        return state

    def _record_budget_consumption(
        self, enforcer: LimitEnforcer, steps_delta: int
    ) -> tuple[int, int, int]:
        """Mirror one subtask's consumption onto the SHARED session tracker (§3).

        The tracker only grows: recording deltas after each run means a subtask can
        never reset main-session counters.
        """
        snapshot = enforcer.snapshot()
        actions = max(0, int(snapshot["actions"]))
        model_calls = max(0, int(snapshot["model_calls"]))
        steps = max(0, int(steps_delta))
        for _ in range(actions):
            self._budget.record_action()
        for _ in range(model_calls):
            self._budget.record_model_call()
        for _ in range(steps):
            self._budget.record_step()
        return actions, model_calls, steps

    async def _run_one_subtask(
        self, subtask_id: str, *, approval: _ApprovalBudget, already_running: bool = False
    ) -> SubtaskRunOutcome:
        """Execute ONE subtask through the EXISTING closed-loop executor (one agent.run)."""
        subtask = self._subtasks.require(subtask_id)
        self._budget.check_all()  # shared session budget gates every subtask start
        if not already_running:
            self._subtasks.start(subtask_id)  # raises SubtaskNotReadyError when gated
            self._budget.record_subtask()
        self._current_subtask_id = subtask_id
        self._context.set_current_task(subtask.description)
        self._audit(
            "subtask_started",
            result=subtask_id,
            metadata={
                "depends_on": list(subtask.depends_on),
                "resumed": already_running,
            },
        )
        self._maybe_checkpoint(
            CheckpointTrigger.SUBTASK_TRANSITION, force=True, current_subtask_id=subtask_id
        )
        # Per-subtask limit scope (fresh enforcer; the recovery controller follows it).
        sub_enforcer = LimitEnforcer(self._limits)
        self._swap_enforcer(sub_enforcer)
        self._fold_history()
        task = getattr(self._agent, "task", None)
        steps_before = int(getattr(task, "step_count", 0) or 0)
        run_state = self._capped_run_state()
        results: list[ExecutionResult] = []
        try:
            results = await self._agent.run(
                subtask.description, run_state, approval=self._make_approval_callback(approval)
            )
        except Exception as exc:  # noqa: BLE001 - defensive: run() is fail-closed internally
            results = [
                ExecutionResult(
                    ok=False,
                    action=GroundedAction(action="done"),
                    message=f"Subtask execution failed (fail-closed): {type(exc).__name__}: {exc}",
                )
            ]
        finally:
            # The run mutated the (possibly copied) run state; fold step_count back.
            mutated_steps = int(getattr(run_state, "step_count", 0) or 0)
            original_steps = int(getattr(self._state, "step_count", 0) or 0)
            if mutated_steps > original_steps:
                self._state.step_count = mutated_steps
        task_termination = getattr(task, "termination_reason", None)
        termination_value = task_termination.value if task_termination is not None else None
        actions, model_calls, steps = self._record_budget_consumption(
            sub_enforcer, int(getattr(task, "step_count", 0) or 0) - steps_before
        )
        # Context + bounded result recording (only while the subtask is running/paused).
        results = list(results)[-MAX_ORCHESTRATION_RESULTS:]
        for result in results:
            self._subtasks.record_result(subtask_id, result)
            self._context.append_history(
                f"subtask={subtask_id}; ok={result.ok}; {_redacted(result.message, 150)}"
            )
        if task_termination is TerminationReason.COMPLETED:
            self._subtasks.complete(subtask_id)
            self._context.record_completed_subtask(
                f"{subtask_id}: {_redacted(subtask.description, 150)}"
            )
            self._audit("subtask_completed", result=subtask_id)
            status = "completed"
        elif task_termination is TerminationReason.APPROVAL_EXHAUSTED:
            # Fail-closed pause: the subtask keeps its progress and waits for a fresh
            # approval epoch; no replan, no failure marking.
            self._subtasks.pause(subtask_id)
            self._audit("subtask_paused", result=subtask_id, reason="approval_exhausted")
            status = "paused"
        else:
            error_text = _redacted(results[-1].message if results else termination_value or "", 500)
            self._subtasks.fail(
                subtask_id,
                failure_class=FailureClass.SUBTASK_FAILED,
                error=error_text,
                last_known_state=termination_value or "",
            )
            self._context.record_error(f"subtask {subtask_id} failed: {error_text[:200]}")
            self._audit(
                "subtask_failed",
                result=subtask_id,
                metadata={"termination_reason": termination_value or ""},
            )
            status = "failed"
        self._context.set_app_window_state(
            _redacted(
                f"{self._probe_state.app() if self._probe_state else ''}|"
                f"{self._probe_state.window() if self._probe_state else ''}",
                300,
            )
        )
        # Context summarization when due (bounded payload; never raises). All steps are
        # recorded; at most ONE summarization per boundary.
        summarize_due = False
        for _ in range(max(0, steps)):
            if self._context.record_step():
                summarize_due = True
        if summarize_due:
            await self._context.summarize()
        # Lifecycle checkpoint (subtask completed/failed) — forced per spec section 7.
        trigger = (
            CheckpointTrigger.SUBTASK_COMPLETED
            if status == "completed"
            else CheckpointTrigger.SUBTASK_FAILED
            if status == "failed"
            else CheckpointTrigger.SUBTASK_TRANSITION
        )
        self._maybe_checkpoint(trigger, force=True, current_subtask_id=subtask_id)
        return SubtaskRunOutcome(
            subtask_id=subtask_id,
            status=status,
            termination_reason=termination_value,
            results=results,
            requires_approval=status == "paused",
            stopped=self._stopped(),
            actions_used=actions,
            model_calls_used=model_calls,
            steps_used=steps,
        )

    async def run_single_subtask(
        self, subtask_id: str, *, approve_next_action: bool = False
    ) -> OrchestrationOutcome:
        """Execute exactly ONE ready subtask (MCP ``run_subtask``; state persists server-side).

        One bounded subtask per call — no MCP request stays open for hours (conflict C7).
        """
        if not self._execution_lock.acquire(blocking=False):
            raise RuntimeBusyError(
                "another subtask execution is already in progress for this session"
            )
        self._execution_active = True
        try:
            if approve_next_action:
                # An explicit per-call human approval: fresh epoch + unattended clock
                # restart (the only renewal path; nothing auto-renews).
                self._refresh_human_approval()
            self._budget.check_all()
            blocked, reason = self._epoch_gate()
            if blocked:
                return OrchestrationOutcome(
                    ok=False, requires_approval=True, detail=reason
                )
            health_block = self._boundary_health()
            if health_block is not None:
                return OrchestrationOutcome(
                    ok=False, health_verdict=health_block[0], detail=health_block[1]
                )
            subtask = self._subtasks.require(subtask_id)
            already_running = False
            if subtask.status is SubtaskStatus.PAUSED:
                self._subtasks.resume(subtask_id)  # paused -> running (had started)
                already_running = True
            elif subtask.status is not SubtaskStatus.PENDING:
                raise SubtaskNotRunnableError(
                    f"Subtask {subtask_id!r} is {subtask.status.value}; only pending or "
                    "paused subtasks can be run."
                )
            approval = _ApprovalBudget(1 if approve_next_action else 0)
            outcome = await self._run_one_subtask(
                subtask_id, approval=approval, already_running=already_running
            )
            return OrchestrationOutcome(
                ok=outcome.status == "completed",
                termination_reason=outcome.termination_reason,
                requires_approval=outcome.requires_approval,
                stopped=outcome.stopped,
                results=outcome.results,
                executed=[subtask_id],
                replan_attempts=self._replan_attempts,
                approval_budget_remaining=approval.remaining,
                detail=outcome.detail,
            )
        finally:
            self._execution_active = False
            self._execution_lock.release()

    async def run_pending_subtasks(
        self, *, approve_next_action: bool = False, session_level: bool = True
    ) -> OrchestrationOutcome:
        """Sequentially execute ready subtasks until the plan completes or gates stop it.

        Loop invariant: every iteration re-checks stop/budget/epoch/unattended/health at
        the boundary, then picks the deterministic head of ``SubtaskManager.ready_set()``
        (creation order) and runs it through the existing executor. Bounded replan runs
        when only dead (dependent-of-failed) work remains. This is orchestration-only:
        NO second executor loop exists here.
        """
        if not self._execution_lock.acquire(blocking=False):
            raise RuntimeBusyError(
                "another subtask execution is already in progress for this session"
            )
        self._execution_active = True
        results: list[ExecutionResult] = []
        executed: list[str] = []
        termination: TerminationReason | None = None
        requires_approval = False
        detail = ""
        health_verdict: str | None = None
        approval = _ApprovalBudget(1 if approve_next_action else 0)
        try:
            if approve_next_action:
                # An explicit per-call human approval: fresh epoch + unattended clock
                # restart (the only renewal path; nothing auto-renews).
                self._refresh_human_approval()
            while True:
                if self._stopped():
                    termination = TerminationReason.STOPPED_BY_USER
                    detail = "session stop requested; orchestration halted"
                    break
                try:
                    self._budget.check_all()
                except SessionBudgetExceeded as exc:
                    termination = TerminationReason.LIMIT_EXCEEDED
                    detail = f"session budget exhausted: {exc}"
                    self._audit(
                        "limit_exceeded",
                        result="failed",
                        metadata={"limit": exc.limit_name, "detail": str(exc)[:200]},
                    )
                    break
                blocked, reason = self._epoch_gate()
                if blocked:
                    requires_approval = True
                    termination = TerminationReason.APPROVAL_EXHAUSTED
                    detail = reason
                    break
                health_block = self._boundary_health()
                if health_block is not None:
                    health_verdict, detail = health_block
                    termination = TerminationReason.BLOCKED_SAFETY
                    break
                ready = self._subtasks.ready_set()
                if not ready:
                    counts = self._subtasks.counts()
                    total = len(self._subtasks)
                    if counts.get("paused"):
                        requires_approval = True
                        detail = "a paused subtask awaits a fresh approval epoch"
                        break
                    if counts.get("pending") or counts.get("blocked"):
                        # Only DEAD work (transitive dependents of failed subtasks) may
                        # remain here; anything else could never become ready.
                        dead = self._dead_ids()
                        statuses = {
                            sid: self._subtasks.require(sid).status
                            for sid in self._subtasks.ids()
                        }
                        remaining = [
                            sid
                            for sid, status in statuses.items()
                            if status in {SubtaskStatus.PENDING, SubtaskStatus.BLOCKED}
                        ]
                        if [sid for sid in remaining if sid not in dead]:
                            termination = TerminationReason.UNRECOVERABLE
                            detail = "remaining work can never become ready (fail-closed)"
                            break
                        unresolved_failures = [
                            sid
                            for sid, status in statuses.items()
                            if status is SubtaskStatus.FAILED
                            and sid not in self._superseded_failed
                        ]
                        # Consult the bounded replan only while an unresolved failed
                        # branch exists (or nothing has succeeded yet): an already
                        # superseded dead branch cannot be revived by another attempt.
                        replanned = False
                        if unresolved_failures or counts.get("completed", 0) == 0:
                            replanned = await self._bounded_replan()
                        if replanned:
                            continue
                        if not unresolved_failures and counts.get("completed", 0) > 0:
                            # Every failed branch was replaced by (completed) bounded
                            # replan work: the goal was achieved through the replan.
                            termination = TerminationReason.COMPLETED
                            detail = "goal completed through bounded replan"
                        else:
                            termination = TerminationReason.UNRECOVERABLE
                            detail = (
                                "remaining plan cannot be completed safely: the only "
                                "remaining work depends on a failed subtask (dead "
                                "dependency branch) and bounded replanning could not "
                                "replace it"
                            )
                        break
                    if total > 0 and counts.get("completed", 0) == total:
                        termination = TerminationReason.COMPLETED
                        break
                    termination = (
                        TerminationReason.UNRECOVERABLE
                        if counts.get("failed")
                        else TerminationReason.COMPLETED
                    )
                    detail = "no executable subtasks remain"
                    break
                outcome = await self._run_one_subtask(ready[0], approval=approval)
                executed.append(outcome.subtask_id)
                results.extend(outcome.results)
                results = results[-MAX_ORCHESTRATION_RESULTS:]
                if outcome.stopped:
                    termination = TerminationReason.STOPPED_BY_USER
                    detail = "session stopped during subtask execution"
                    break
                if outcome.requires_approval:
                    requires_approval = True
                    detail = "subtask paused awaiting a fresh approval epoch"
                    break
                if self.checkpoint_due():
                    self._maybe_checkpoint(
                        CheckpointTrigger.PERIODIC,
                        force=True,
                        current_subtask_id=outcome.subtask_id,
                    )
        finally:
            self._execution_active = False
            self._execution_lock.release()
        if session_level:
            # Session-level runs end the (long-running) session execution: record the
            # termination and write the BEFORE_SESSION_END checkpoint (best effort).
            if termination is not None:
                self._terminated_reason = termination
            self._maybe_checkpoint(
                CheckpointTrigger.BEFORE_SESSION_END,
                force=True,
                current_subtask_id=self._current_subtask_id,
            )
        return OrchestrationOutcome(
            ok=(termination is TerminationReason.COMPLETED),
            termination_reason=termination.value if termination is not None else None,
            requires_approval=requires_approval,
            stopped=termination is TerminationReason.STOPPED_BY_USER,
            results=results,
            executed=executed,
            replan_attempts=self._replan_attempts,
            health_verdict=health_verdict,
            approval_budget_remaining=approval.remaining,
            detail=detail,
        )

    # ------------------------------------------------------------------ health boundary

    def _boundary_health(self) -> tuple[str, str] | None:
        """Boundary health evaluation; returns (verdict, reason) to stop, or None.

        - ``None``: not due, or healthy — continue.
        - UNSAFE: the environment changed materially — invalidate the approval epoch
          (fail-closed) and stop; the caller must not execute further actions.
        - DEGRADED: one bounded re-evaluation; still not healthy -> stop (pause).
        The monitor never executes actions and is not a second loop (conflict C6).
        """
        check = self._health.maybe_check()
        if check is None:
            return None
        self._audit(
            "health_check",
            result=check.verdict.value,
            metadata={
                "recommendation": check.recommendation.value,
                "details": "; ".join(check.details[:4]),
            },
        )
        if check.verdict is HealthVerdict.UNSAFE:
            reason_name = (
                InvalidationReason.APPLICATION_CHANGED
                if "expected_application_mismatch" in check.details
                else InvalidationReason.ENVIRONMENT_CHANGED
            )
            self._epochs.invalidate(
                reason_name, detail="; ".join(check.details[:3])[:_DETAIL_MAX_CHARS]
            )
            self._audit(
                "approval_epoch", result="invalidated", reason=reason_name.value
            )
            return (
                check.verdict.value,
                f"health check unsafe: {'; '.join(check.details[:4])}",
            )
        if check.verdict is HealthVerdict.DEGRADED:
            recheck = self._health.evaluate()  # one bounded re-evaluation
            self._audit("health_check", result=recheck.verdict.value, metadata={"phase": "recheck"})
            if recheck.verdict is not HealthVerdict.HEALTHY:
                self._health_hold = "; ".join(recheck.details[:4])
                return (
                    recheck.verdict.value,
                    f"health degraded after re-check; pausing: {self._health_hold}",
                )
        return None

    # ------------------------------------------------------------------ bounded replan

    def _dead_ids(self) -> set[str]:
        """Terminal-failed subtask ids plus everything that transitively depends on them.

        A terminal-failed prerequisite can never complete, so its (transitive)
        dependents can never become ready — requeueing them would be pointless churn.
        """
        dead: set[str] = set()
        for subtask_id in self._subtasks.ids():
            subtask = self._subtasks.require(subtask_id)
            if subtask.status is SubtaskStatus.FAILED:
                dead.add(subtask_id)
                dead.update(self._subtasks.dependents(subtask_id, transitive=True))
        return dead

    def _remaining_goal_text(self) -> str:
        failed = [
            self._subtasks.require(sid).description
            for sid in self._subtasks.ids()
            if self._subtasks.require(sid).status is SubtaskStatus.FAILED
        ]
        suffix = ""
        if failed:
            suffix = f" (replan needed; failed so far: {'; '.join(failed)[:400]})"
        return f"{(self.goal or 'session goal')[:_GOAL_MAX_CHARS]}{suffix}"

    async def _bounded_replan(self) -> bool:
        """Bounded replan of REMAINING work (``RecoveryStrategy.REPLAN_REMAINING``).

        Asks the planner for replacement work at most :data:`MAX_REPLAN_ATTEMPTS` times;
        replacement entries never depend on dead ids and are validated by the
        deterministic plan validator before creation. Returns True when executable work
        was added.
        """
        if self._replan_attempts >= MAX_REPLAN_ATTEMPTS:
            return False
        self._replan_attempts += 1
        self._audit(
            "replan",
            result="attempt",
            metadata={"attempt": self._replan_attempts, "max": MAX_REPLAN_ATTEMPTS},
        )
        planner = self._provider
        if planner is None or not callable(getattr(planner, "plan_subtasks", None)):
            self._audit("replan", result="unavailable")
            return False
        try:
            raw = planner.plan_subtasks(self._remaining_goal_text())
            if inspect.isawaitable(raw):
                raw = await raw
        except Exception as exc:  # noqa: BLE001 - planner failure ends the replan attempt
            self._audit(
                "replan", result="planner_failed", metadata={"detail": str(exc)[:200]}
            )
            return False
        capacity = max(1, self._subtasks.max_subtasks - len(self._subtasks))
        try:
            entries = PlanValidator(max_subtasks=capacity).validate_or_raise(raw)
        except PlanRejectedError as exc:
            self._audit(
                "replan", result="rejected", metadata={"codes": "; ".join(exc.codes[:6])}
            )
            return False
        dead = self._dead_ids()
        created = self._create_entries(
            [entry for entry in entries if not dead.intersection(entry.depends_on)],
            skip_existing=True,
        )
        if created > 0:
            # The replan addressed the dead branch: currently-failed subtasks are
            # considered superseded by the replacement work.
            for sid in self._subtasks.ids():
                if self._subtasks.require(sid).status is SubtaskStatus.FAILED:
                    self._superseded_failed.add(sid)
        self._audit("replan", result="created" if created else "no_new_work")
        return created > 0

    # ------------------------------------------------------------------ snapshot (tests)

    def runtime_snapshot(self) -> dict[str, Any]:
        """Bounded introspection snapshot (tests/telemetry; not a checkpoint source)."""
        return {
            "session_id": self._session_id,
            "continuation_of": self._continuation_of,
            "budget": self._budget.snapshot(),
            "counts": self._subtasks.counts(),
            "epoch": self._epochs.snapshot(),
            "replan_attempts": self._replan_attempts,
            "terminated_reason": (
                self._terminated_reason.value if self._terminated_reason is not None else None
            ),
        }
