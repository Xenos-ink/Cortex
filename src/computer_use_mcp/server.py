"""MCP server wiring: 6 tools, bounded session registry, kill path, audit, limits.

Compatibility contract (master-mission section 6, binding):

- The 6 tool names, stdio transport, and parameter positions are preserved; signatures
  gain TRAILING OPTIONAL params only (``start_session(..., allowed_processes=None,
  limits=None)``, ``computer_execute(..., expected_effect=None)``).
- Existing return shapes keep their top-level keys; new fields are additive only.
- ``computer_execute`` keeps the hardcoded ``confidence=1.0`` — redefined as the
  client-asserted MODEL confidence for a direct caller action; grounding confidence,
  staleness, risk classification, approval, and verification apply independently.
- ``run_goal`` keeps exactly ``approval_budget = 1`` per call when
  ``approve_next_action=True``; bounded recovery retries of the same approved action
  instance do not re-consume budget; a new distinct action after exhaustion is denied
  fail-closed with ``requires_approval`` in the response.
- ``stop_session`` keeps its signature/return shape; it now arms the thread-safe
  StopToken kill path and audits stop + emergency_stop.
- ``start_session`` succeeds with no API key (provider construction is lazy at the first
  model call — compat decision 5; dry-run usable).

Test/extension seam (for E6/E7): the module-level ``_backend_factory`` and
``_provider_factory`` callables are invoked once per ``start_session``; tests monkeypatch
them to inject ``FakeComputerBackend``/scripted providers, and read session wiring via
``_get_bundle(session_id)`` (agent, state, backend, enforcer, auditor, metrics).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from dataclasses import fields as dataclass_fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mcp.server.fastmcp import FastMCP

from .agent import ComputerUseAgent
from .audit import AuditLogger, Metrics
from .backend import ComputerBackend, LocalComputerBackend
from .checkpoint_manager import (
    CheckpointError,
    CheckpointManager,
    CheckpointTrigger,
    CheckpointValidationError,
)
from .context_manager import ContextManager
from .limits import LimitEnforcer, LimitExceeded, Limits
from .long_running import (
    LongRunningError,
    LongRunningRuntime,
    PlannerUnavailableError,
    RuntimeBusyError,
    SubtaskNotRunnableError,
)
from .models import ExecutionResult, GroundedAction, SessionState, SubtaskStatus
from .plan_validator import PlanRejectedError
from .provider import OpenAICompatibleVisionProvider
from .redaction import redact_text
from .resume_manager import ResumeBundle, ResumeManager, ResumeRefusalError
from .safety import SafetyPolicy
from .state import SessionContext, SessionLimitExceeded, SessionRegistry, TaskStopped
from .subtask_manager import (
    DependencyCycleError,
    InvalidSubtaskError,
    InvalidTransitionError,
    SelfDependencyError,
    SubtaskAlreadyExistsError,
    SubtaskError,
    SubtaskLimitExceeded,
    SubtaskNotReadyError,
    UnknownDependencyError,
    UnknownSubtaskError,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)
mcp = FastMCP("Cortex")


# --- module wiring (test seam: monkeypatch the factories) ---------------------------------

def _default_max_sessions() -> int:
    try:
        return Limits().max_sessions
    except Exception:  # noqa: BLE001 - defensive default
        return 4


def _audit_root() -> Path:
    """Root directory for per-session audit logs (env override honored)."""
    override = os.getenv("COMPUTER_USE_MCP_LOG_DIR")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "cortex" / "logs"


_backend_factory: Callable[[], ComputerBackend] = LocalComputerBackend
_provider_factory: Callable[[], Any] = OpenAICompatibleVisionProvider
_registry = SessionRegistry(max_sessions=_default_max_sessions())
_bundles: dict[str, _SessionBundle] = {}
# D1: stopped sessions are remembered (bounded) so later tool calls fail closed with a
# precise ``session_stopped`` error instead of a live bundle leaking in memory forever.
_stopped_sessions: dict[str, str] = {}
_STOPPED_SESSION_MEMORY = 1024
_lock = threading.RLock()
# Long-running session persistence (master-mission 003): one shared checkpoint store
# (env-var override honored) and the resume manager built on top of it.
_checkpoint_manager = CheckpointManager()
_resume_manager = ResumeManager(_checkpoint_manager)


class _UnknownSession(KeyError):
    """Raised when a tool is called with a session id that does not exist."""


class _StoppedSession(LookupError):
    """Raised when a tool targets a session that has been stopped (D1, fail-closed)."""


def _remember_stopped(session_id: str, snapshot: dict[str, Any]) -> None:
    """Record a stopped session's id + minimal snapshot (bounded; oldest evicted first).

    The snapshot lets stopped-session tool responses keep their standard shapes
    (task_id / step_count / metrics) even though the live bundle is gone (D1).
    """
    _stopped_sessions[session_id] = snapshot
    while len(_stopped_sessions) > _STOPPED_SESSION_MEMORY:
        _stopped_sessions.pop(next(iter(_stopped_sessions)))


class _LazyProvider:
    """Defers provider construction to the first model call (compat decision 5).

    Construction failures (legacy providers raising without an API key) are remembered
    and surface as a fail-closed error at decide-time instead of breaking start_session;
    the agent converts any provider failure into an audited, bounded recovery path.
    """

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._provider: Any | None = None
        self._unavailable: str | None = None

    def _resolve(self) -> Any:
        if self._provider is None and self._unavailable is None:
            try:
                self._provider = self._factory()
            except Exception as exc:  # noqa: BLE001 - unavailability is a planned state
                self._unavailable = f"{type(exc).__name__}: {exc}"
                logger.warning("Vision provider unavailable (fail-closed): %s", self._unavailable)
        if self._provider is None:
            raise RuntimeError(f"Vision provider unavailable (fail-closed): {self._unavailable}")
        return self._provider

    async def decide(self, goal: str, observation: Any, history: list[str]) -> Any:
        provider = self._resolve()
        result = provider.decide(goal, observation, history)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def decide_full(self, goal: str, observation: Any, history: list[str]) -> Any:
        provider = self._resolve()
        delegate = getattr(provider, "decide_full", None)
        if callable(delegate):
            result = delegate(goal, observation, history)
            if inspect.isawaitable(result):
                result = await result
            return result
        decision = await self.decide(goal, observation, history)
        return SimpleNamespace(
            decision=decision,
            expected_effect=None,
            verification_hint=None,
            suspicious_content=None,
            redactions_applied=[],
        )

    def judge_change(self, before_b64: str, after_b64: str, expected_effect: str) -> Any:
        provider = self._resolve()
        delegate = getattr(provider, "judge_change", None)
        if not callable(delegate):
            raise TypeError(
                "Provider does not expose judge_change; model-based verification is unavailable."
            )
        return delegate(before_b64, after_b64, expected_effect)

    async def plan_subtasks(self, goal: str, **kwargs: Any) -> Any:
        """Delegate the long-running planner call (lazy; typed failure without a key)."""
        provider = self._resolve()
        delegate = getattr(provider, "plan_subtasks", None)
        if not callable(delegate):
            raise TypeError("Provider does not expose plan_subtasks; planning is unavailable.")
        result = delegate(goal, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def summarize_context(self, request: Any) -> Any:
        """Delegate the bounded context summarizer (ContextManager falls back on failure)."""
        provider = self._resolve()
        delegate = getattr(provider, "summarize_context", None)
        if not callable(delegate):
            raise TypeError("Provider does not expose summarize_context.")
        result = delegate(request)
        if inspect.isawaitable(result):
            result = await result
        return result


@dataclass
class _SessionBundle:
    """Everything isolated per session: state, backend, agent, limits, audit, metrics."""

    context: SessionContext
    state: SessionState
    backend: ComputerBackend
    agent: ComputerUseAgent
    enforcer: LimitEnforcer
    auditor: AuditLogger
    metrics: Metrics
    limits: Limits
    extra: dict[str, Any] = field(default_factory=dict)


def _get_bundle(session_id: str) -> _SessionBundle:
    """Return the session bundle or raise :class:`_UnknownSession` (fail-closed)."""
    bundle = _bundles.get(session_id)
    if bundle is None:
        raise _UnknownSession(session_id)
    return bundle


def _get_live_bundle(session_id: str) -> _SessionBundle:
    """Return a NON-stopped session bundle (D1/F7 fail-closed discipline).

    Stopped sessions are removed from the registry and the bundle store; later tool
    calls receive a structured ``session_stopped`` error — consistent with the
    stopped-session policy: a stopped session performs no further work of any kind.

    F7: this ALSO covers the internal kill path — if a session's StopToken was armed
    by an internal safety path (not via ``stop_session``), the bundle is routed through
    the SAME cleanup here, so bundle hygiene is identical for both stop flavors and
    every tool refuses with ``session_stopped``.
    """
    bundle = _bundles.get(session_id)
    if bundle is None:
        if session_id in _stopped_sessions:
            raise _StoppedSession(session_id)
        raise _UnknownSession(session_id)
    if bundle.context.stop.stopped:
        try:
            bundle.auditor.emit(
                "stop",
                session_id,
                task_id=bundle.context.task.task_id,
                result="stopped",
                metadata={"source": "internal_kill_path", "detected": True},
            )
        except Exception:
            logger.debug("internal-stop audit failed", exc_info=True)
        _close_stopped_bundle(session_id, bundle, source="internal_kill_path", audit_stop_events=False)
        raise _StoppedSession(session_id)
    return bundle


def _close_stopped_bundle(
    session_id: str, bundle: _SessionBundle, *, source: str, audit_stop_events: bool = True
) -> None:
    """Shared kill-path cleanup (F7): one stopping flavor, one hygiene outcome.

    Arms the token (idempotent), mirrors the flag onto the legacy session state, audits
    the stop, removes the bundle from the store AND the registry, and records the
    bounded stopped-session snapshot. ``audit_stop_events=False`` is used when the stop
    was already audited by another path (in-run emergency_stop, detection event).
    """
    bundle.context.stop.stop()
    bundle.state.stopped = True
    bundle.state.pending_approval_token = None
    if audit_stop_events:
        for event_type in ("stop", "emergency_stop"):
            try:
                bundle.auditor.emit(
                    event_type,
                    session_id,
                    task_id=bundle.context.task.task_id,
                    result="stopped",
                    metadata={"source": source},
                )
            except Exception:
                logger.debug("stop audit failed", exc_info=True)
    with _lock:
        _bundles.pop(session_id, None)
        _registry.remove(session_id)
        _remember_stopped(
            session_id,
            {
                "task_id": bundle.context.task.task_id,
                "step_count": bundle.state.step_count,
                "metrics": bundle.metrics.snapshot(),
            },
        )


def _error_response(exc: Exception) -> dict[str, object]:
    """Structured, traceback-free error payload for typed failures."""
    if isinstance(exc, TaskStopped):
        code = "task_stopped"
    elif isinstance(exc, LimitExceeded):
        code = "limit_exceeded"
    elif isinstance(exc, SessionLimitExceeded):
        code = "session_limit_exceeded"
    elif isinstance(exc, _StoppedSession):
        code = "session_stopped"
    elif isinstance(exc, _UnknownSession):
        code = "unknown_session"
    else:
        code = type(exc).__name__
    payload: dict[str, object] = {"ok": False, "error": code, "message": str(exc)}
    if isinstance(exc, LimitExceeded):
        payload["limit"] = exc.limit_name
    if isinstance(exc, (_UnknownSession, _StoppedSession)):
        payload["session_id"] = str(exc.args[0]) if exc.args else ""
    if isinstance(exc, _StoppedSession):
        payload["message"] = (
            "Session is stopped; refusing to proceed (fail-closed stopped-session policy)."
        )
    return payload


def _redact_result_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """F2: the MCP response path is redacted too (defense in depth).

    The audit sink and the provider payload are already redaction-enforced; until now
    the tool RESPONSE was the one unredacted surface (action text/reason, result
    messages, verification notes/evidence echo provider-proposed strings verbatim).
    The calling client supplied the goal, so this is same-party data — but the
    response path must not be the surface that bypasses redaction.
    """
    if payload.get("message"):
        payload["message"] = redact_text(str(payload["message"]))[0]
    action = payload.get("action")
    if isinstance(action, dict):
        if action.get("text"):
            action["text"] = redact_text(str(action["text"]))[0]
        if action.get("reason"):
            action["reason"] = redact_text(str(action["reason"]))[0]
    verification = payload.get("verification")
    if isinstance(verification, dict):
        if verification.get("note"):
            verification["note"] = redact_text(str(verification["note"]))[0]
        evidence = verification.get("evidence")
        if isinstance(evidence, list):
            verification["evidence"] = [redact_text(str(item))[0] for item in evidence]
    return payload


def _parse_limits(limits: dict[str, Any] | None) -> Limits:
    """Validate a client-supplied limits dict via ``Limits.validate`` (fail-closed)."""
    if not limits:
        return Limits()
    if not isinstance(limits, dict):
        raise TypeError("limits must be a mapping of Limits field names to numbers.")
    known = {item.name for item in dataclass_fields(Limits)}
    unknown = sorted(str(key) for key in limits if key not in known)
    if unknown:
        raise ValueError(f"Unknown limits fields: {unknown}. Valid fields: {sorted(known)}.")
    kwargs: dict[str, float] = {}
    for key, value in limits.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError(f"Limit {key!r} must be a number, got {type(value).__name__}.")
        kwargs[str(key)] = float(value)
    return Limits(**kwargs).validate()  # type: ignore[arg-type]


# --- long-running session wiring (master-mission 003, additive) -----------------------------


def _build_runtime(
    bundle: _SessionBundle,
    *,
    goal: str = "",
    resume_bundle: ResumeBundle | None = None,
    resumed: bool = False,
) -> LongRunningRuntime:
    """Construct the per-session orchestration runtime over the EXISTING bundle pieces."""
    provider = bundle.agent.provider
    if resume_bundle is not None:
        # Restore the checkpoint's context into a FRESH ContextManager bound to the
        # (lazy) provider summarizer — snapshot/restore is lossless per A3's contract.
        context = ContextManager(
            goal=resume_bundle.goal,
            summarizer=(
                (lambda request: provider.summarize_context(request))  # type: ignore[union-attr]
                if provider is not None and hasattr(provider, "summarize_context")
                else None
            ),
            summarize_every=resume_bundle.limits.context_summarize_every,
        )
        context.restore(resume_bundle.context.snapshot())
        return LongRunningRuntime(
            session_id=bundle.context.session_id,
            goal=resume_bundle.goal,
            agent=bundle.agent,
            state=bundle.state,
            limits=resume_bundle.limits,
            backend=bundle.backend,
            auditor=bundle.auditor,
            metrics=bundle.metrics,
            checkpoint_manager=_checkpoint_manager,
            provider=provider,
            allowed_processes=list(bundle.extra.get("allowed_processes") or []),
            allowed_windows=list(bundle.state.allowed_windows or []),
            subtasks=resume_bundle.subtasks,
            budget=resume_bundle.budget,
            context=context,
            continuation_of=resume_bundle.continuation_identity,
            expected_environment=resume_bundle.environment,
            resumed=resumed,
        )
    return LongRunningRuntime(
        session_id=bundle.context.session_id,
        goal=goal,
        agent=bundle.agent,
        state=bundle.state,
        limits=bundle.limits,
        backend=bundle.backend,
        auditor=bundle.auditor,
        metrics=bundle.metrics,
        checkpoint_manager=_checkpoint_manager,
        provider=provider,
        allowed_processes=list(bundle.extra.get("allowed_processes") or []),
        allowed_windows=list(bundle.state.allowed_windows or []),
    )


def _get_or_create_runtime(bundle: _SessionBundle) -> LongRunningRuntime:
    """Return the session's runtime, creating it lazily on first subtask-tool use."""
    runtime = bundle.extra.get("long_running")
    if isinstance(runtime, LongRunningRuntime):
        return runtime
    runtime = _build_runtime(bundle, goal=str(bundle.context.task.goal or ""))
    bundle.extra["long_running"] = runtime
    return runtime


def _current_environment_from_backend(backend: Any) -> dict[str, Any]:
    """Fresh CURRENT environment reading for resume verification (fail-closed on absence).

    A failed observation returns empty identity fields — the resume verification treats
    missing identity as a mismatch, never as a pass (stale-state enforcement, spec 14).
    """
    try:
        observation = backend.observe()
    except Exception:  # noqa: BLE001 - unusable environment is fail-closed data
        return {}
    info = getattr(observation, "active_window_info", None)
    process = None
    title = None
    if info is not None:
        process = str(info.process_name) if getattr(info, "process_name", None) else None
        title = str(info.title) if getattr(info, "title", None) else None
    if title is None and getattr(observation, "active_window", None):
        title = str(observation.active_window)
    return {"active_process_name": process, "active_window_title": title}


def _resume_error_response(exc: Exception, path: str) -> dict[str, object]:
    """Typed fail-closed resume refusal responses (never a partial restore)."""
    if isinstance(exc, ResumeRefusalError):
        code = "resume_refused"
    elif isinstance(exc, CheckpointValidationError):
        code = "invalid_checkpoint"
    elif isinstance(exc, CheckpointError):
        code = "checkpoint_error"
    else:
        code = type(exc).__name__
    payload: dict[str, object] = {
        "ok": False,
        "error": code,
        "message": str(exc)[:2000],
        "checkpoint": str(path),
    }
    checks = getattr(exc, "checks", None)
    if checks is not None and hasattr(checks, "summary"):
        payload["checks"] = checks.summary()
    return payload


_SUBTASK_ERROR_CODES: tuple[tuple[type[Exception], str], ...] = (
    (SubtaskLimitExceeded, "subtask_limit_exceeded"),
    (UnknownSubtaskError, "unknown_subtask"),
    (SubtaskAlreadyExistsError, "subtask_already_exists"),
    (InvalidSubtaskError, "invalid_subtask"),
    (UnknownDependencyError, "unknown_dependency"),
    (SelfDependencyError, "self_dependency"),
    (DependencyCycleError, "dependency_cycle"),
    (InvalidTransitionError, "invalid_transition"),
)


def _subtask_error_response(exc: Exception) -> dict[str, object]:
    """Structured, typed error payloads for subtask/orchestration failures."""
    if isinstance(exc, SubtaskNotReadyError):
        return {
            "ok": False,
            "error": "subtask_not_ready",
            "message": str(exc),
            "subtask_id": exc.subtask_id,
            "unmet_dependencies": list(exc.unmet),
        }
    for exc_type, code in _SUBTASK_ERROR_CODES:
        if isinstance(exc, exc_type):
            payload: dict[str, object] = {"ok": False, "error": code, "message": str(exc)}
            subtask_id = getattr(exc, "subtask_id", None)
            if subtask_id:
                payload["subtask_id"] = subtask_id
            return payload
    long_running_codes: tuple[tuple[type[Exception], str], ...] = (
        (RuntimeBusyError, "runtime_busy"),
        (PlannerUnavailableError, "planner_unavailable"),
        (SubtaskNotRunnableError, "subtask_not_runnable"),
    )
    for exc_type, code in long_running_codes:
        if isinstance(exc, exc_type):
            return {"ok": False, "error": code, "message": str(exc)}
    return {"ok": False, "error": type(exc).__name__, "message": str(exc)}


def _run_goal_stopped_response(session_id: str, approve_next_action: bool) -> dict[str, object]:
    """The standard run_goal stopped-session response shape (E6 contract, D1/D2)."""
    memory = _stopped_sessions.get(session_id, {})
    stopped_result = ExecutionResult(
        ok=False,
        action=GroundedAction(action="done"),
        message="Session is stopped; run_goal refused (fail-closed stopped-session policy).",
    )
    return {
        "ok": False,
        "approval_budget_remaining": 1 if approve_next_action else 0,
        "results": [stopped_result.model_dump()],
        "session_id": session_id,
        "task_id": str(memory.get("task_id", "")),
        "termination_reason": "stopped_by_user",
        "stopped": True,
        "requires_approval": False,
        "step_count": int(memory.get("step_count", 0)),
        "metrics": memory.get("metrics") or {"counters": {}, "latencies": {}},
    }


async def _run_goal_auto_subtasks(
    session_id: str, goal: str, approve_next_action: bool
) -> dict[str, object]:
    """Long-Running/Multi-Subtask mode of ``run_goal`` (spec section 10, additive).

    Plan (provider -> deterministic validation) -> sequential subtask execution through
    the EXISTING executor, all within this call's budget semantics; the response keeps
    the run_goal shape with additive subtask fields. Existing callers (default off) are
    byte-identical.
    """
    try:
        bundle = _get_live_bundle(session_id)
    except _StoppedSession:
        return _run_goal_stopped_response(session_id, approve_next_action)
    except _UnknownSession as exc:
        return _error_response(exc)
    runtime = _get_or_create_runtime(bundle)
    planned = 0
    try:
        runtime.set_goal(goal)
        entries = await runtime.plan_from_llm(goal)
        planned = len(entries)
    except (PlannerUnavailableError, PlanRejectedError) as exc:
        code = "planner_unavailable" if isinstance(exc, PlannerUnavailableError) else "plan_rejected"
        payload: dict[str, object] = {
            "ok": False,
            "error": code,
            "message": str(exc)[:2000],
            "session_id": session_id,
            "planned_subtasks": 0,
        }
        if isinstance(exc, PlanRejectedError):
            payload["codes"] = list(exc.codes)[:10]
        return payload
    try:
        outcome = await runtime.run_pending_subtasks(approve_next_action=approve_next_action)
    except RuntimeBusyError as exc:
        return {"ok": False, "error": "runtime_busy", "message": str(exc), "session_id": session_id}
    except LimitExceeded as exc:
        return {
            "ok": False,
            "error": "limit_exceeded",
            "limit": exc.limit_name,
            "message": str(exc),
            "session_id": session_id,
        }
    task = bundle.agent.task
    results_payload: list[dict[str, object]] = []
    for item in outcome.results:
        results_payload.append(_redact_result_payload(item.model_dump()))
    if task.status.value == "stopped" and session_id in _bundles:
        # F7: an internally-armed kill path that terminated this run gets the SAME
        # bundle hygiene as run_goal (the stop was already audited in-run).
        _close_stopped_bundle(
            session_id, bundle, source="run_goal_kill_path", audit_stop_events=False
        )
    return {
        "ok": bool(outcome.ok),
        "approval_budget_remaining": outcome.approval_budget_remaining,
        "results": results_payload,
        "session_id": session_id,
        "task_id": task.task_id,
        "termination_reason": outcome.termination_reason,
        "stopped": bool(
            outcome.stopped or bundle.state.stopped or task.status.value == "stopped"
        ),
        "requires_approval": bool(outcome.requires_approval or bundle.agent.approval_denied),
        "step_count": bundle.state.step_count,
        "metrics": bundle.metrics.snapshot(),
        # Additive long-running fields (existing keys above unchanged).
        "planned_subtasks": planned,
        "executed_subtasks": list(outcome.executed),
        "replan_attempts": outcome.replan_attempts,
        "detail": outcome.detail[:500],
        "progress": runtime.progress(),
    }


# --- tools ---------------------------------------------------------------------------------


@mcp.tool()
def start_session(
    dry_run: bool = True,
    require_approval: bool = True,
    max_steps: int = 30,
    max_retries_per_action: int = 1,
    min_confidence: float = 0.70,
    allowed_windows: list[str] | None = None,
    allowed_processes: list[str] | None = None,
    limits: dict[str, float] | None = None,
    resume_from_checkpoint: str | None = None,
) -> dict[str, object]:
    """Start a guarded session; dry-run and per-action approval are enabled by default.

    ``allowed_processes`` enforces a process allowlist (P0-G); ``limits`` carries any
    :class:`~computer_use_mcp.limits.Limits` field (validated + clamped, fail-closed on
    unknown names). No API key is required to start (the provider is lazy).

    Long-running additive (master-mission 003, trailing optional): pass
    ``resume_from_checkpoint`` (a checkpoint file path from a previous session) to
    RESUME it as a CONTINUATION — counters are restored (never reset/zeroed), subtasks,
    dependencies, and context are restored, the CURRENT environment is re-verified
    against the checkpoint's expectations (mismatch/invalid checkpoint -> fail-closed
    typed refusal), and approval is FRESH (never resurrected from data). Existing
    callers that omit the parameter are byte-identical.
    """
    try:
        # D9 precedence (documented): the legacy ``max_retries_per_action`` parameter and
        # the same key inside ``limits`` are mutually exclusive — supplying both is an
        # explicit conflict (ValueError). When only one is supplied, it applies.
        if limits and "max_retries_per_action" in limits and max_retries_per_action != 1:
            raise ValueError(
                "max_retries_per_action was supplied both as the start_session parameter "
                "and inside the limits dict; pass it in exactly one place."
            )
        limits_obj = _parse_limits(limits)
        if not limits:
            # Legacy parameter only: it applies (harmonized into Limits, one source).
            limits_obj = Limits(max_retries_per_action=max_retries_per_action).validate()
        elif "max_retries_per_action" not in limits:
            # Limits dict without the retry key + legacy parameter supplied: param wins.
            limits_obj = replace(
                limits_obj, max_retries_per_action=max_retries_per_action
            ).validate()
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": "invalid_limits", "message": str(exc)}
    try:
        context = _registry.create()
    except SessionLimitExceeded as exc:
        return {
            "ok": False,
            "error": "session_limit_exceeded",
            "message": str(exc),
            "max_sessions": exc.max_sessions,
        }
    session_id = context.session_id
    try:
        state = SessionState(
            session_id=session_id,
            dry_run=dry_run,
            require_approval=require_approval,
            max_steps=max_steps,
            max_retries_per_action=max_retries_per_action,
            min_confidence=min_confidence,
            allowed_windows=allowed_windows or [],
        )
        backend = _backend_factory()
        enforcer = LimitEnforcer(limits_obj)
        auditor = AuditLogger(_audit_root() / session_id)
        metrics = Metrics()
        agent = ComputerUseAgent(
            backend=backend,
            provider=_LazyProvider(_provider_factory),
            safety=SafetyPolicy(),
            session_id=session_id,
            task=context.task,
            stop=context.stop,
            limits=limits_obj,
            enforcer=enforcer,
            auditor=auditor,
            metrics=metrics,
            allowed_processes=allowed_processes or [],
        )
    except Exception as exc:  # noqa: BLE001 - never leak a traceback; release the slot
        _registry.remove(session_id)
        return _error_response(exc)
    with _lock:
        _bundles[session_id] = _SessionBundle(
            context=context,
            state=state,
            backend=backend,
            agent=agent,
            enforcer=enforcer,
            auditor=auditor,
            metrics=metrics,
            limits=limits_obj,
            extra={
                "allowed_processes": list(allowed_processes or []),
                "max_retries_per_action": max_retries_per_action,
            },
        )
    try:
        auditor.emit(
            "session_start",
            session_id,
            task_id=context.task.task_id,
            result="ok",
            metadata={
                "dry_run": dry_run,
                "require_approval": require_approval,
                "allowed_processes": list(allowed_processes or []),
                "limits": str(limits_obj),
            },
        )
    except Exception:
        logger.debug("session_start audit failed", exc_info=True)
    resume_extra: dict[str, object] = {}
    if resume_from_checkpoint:
        # Long-running resume (conflict C4): the live session gets a NEW session id and
        # carries the checkpoint's original id as continuation identity; counters are
        # restored (never reset), and the environment is re-verified BEFORE continuing.
        bundle = _bundles[session_id]
        resume_bundle: ResumeBundle | None = None
        try:
            current_environment = _current_environment_from_backend(backend)
            resume_bundle = _resume_manager.prepare(
                resume_from_checkpoint, current_environment=current_environment
            )
            runtime = _build_runtime(
                bundle, goal="", resume_bundle=resume_bundle, resumed=True
            )
        except Exception as exc:  # noqa: BLE001 - typed refusal, never a partial restore
            with _lock:
                _bundles.pop(session_id, None)
            _registry.remove(session_id)
            return _resume_error_response(exc, resume_from_checkpoint)
        assert resume_bundle is not None
        bundle.extra["long_running"] = runtime
        # Continuation doctrine: the checkpoint's OWN (re-clamped) limits stay in force;
        # the per-run enforcer/executor adopt them too — no budget is ever enlarged.
        bundle.limits = runtime.limits
        enforcer.limits = runtime.limits
        agent.limits = runtime.limits
        # Adopt the checkpointed session settings (the resumed session IS the old one).
        state.dry_run = resume_bundle.session.dry_run
        state.require_approval = resume_bundle.session.require_approval
        state.max_steps = resume_bundle.session.max_steps
        state.max_retries_per_action = resume_bundle.session.max_retries_per_action
        state.min_confidence = resume_bundle.session.min_confidence
        bundle.context.task.goal = runtime.goal
        limits_obj = runtime.limits
        resume_extra = {
            "resumed": True,
            "continuation_of": resume_bundle.continuation_identity,
            "resumed_from_checkpoint": str(resume_from_checkpoint),
        }
        try:
            auditor.emit(
                "resume",
                session_id,
                task_id=context.task.task_id,
                result="ok",
                metadata={
                    "continuation_of": resume_bundle.continuation_identity,
                    "checkpoint": str(resume_from_checkpoint),
                },
            )
        except Exception:
            logger.debug("resume audit failed", exc_info=True)
    payload = state.model_dump()
    payload.update(
        {
            "allowed_processes": list(allowed_processes or []),
            "limits": str(limits_obj),
            "task_id": context.task.task_id,
        }
    )
    payload.update(resume_extra)
    return payload


@mcp.tool()
def stop_session(session_id: str) -> dict[str, object]:
    """Arm the kill path: no further input is performed after this returns.

    Shape-compatible with the legacy tool; it now arms the thread-safe StopToken checked
    before every physical input, at every loop checkpoint, and inside waits, and it
    audits stop + emergency_stop events. The session bundle is closed and removed from
    the registry (D1): subsequent tool calls fail closed with ``session_stopped``.
    """
    bundle = _bundles.get(session_id)
    if bundle is None:
        if session_id in _stopped_sessions:
            # Idempotent re-stop: shape preserved, nothing left to stop.
            return {
                "ok": True,
                "session_id": session_id,
                "message": "Session already stopped.",
                "task_id": str(_stopped_sessions[session_id].get("task_id", "")),
                "termination_reason": None,
            }
        return _error_response(_UnknownSession(session_id))
    # Long-running lifecycle trigger (spec section 7): checkpoint BEFORE the session
    # ends. Best effort — a checkpoint write failure never blocks the kill path.
    runtime = bundle.extra.get("long_running")
    if isinstance(runtime, LongRunningRuntime):
        try:
            runtime.checkpoint(CheckpointTrigger.BEFORE_SESSION_END)
        except Exception:  # durability failure must not block stopping
            logger.debug("before_session_end checkpoint failed", exc_info=True)
    # D1/F7: shared kill-path cleanup — bundle store and registry stay consistent.
    _close_stopped_bundle(session_id, bundle, source="stop_session")
    return {
        "ok": True,
        "session_id": session_id,
        "message": "Session stopped before the next action.",
        "task_id": bundle.context.task.task_id,
        "termination_reason": bundle.context.task.termination_reason.value
        if bundle.context.task.termination_reason
        else None,
    }


@mcp.tool()
def computer_observe(session_id: str) -> dict[str, object]:
    """Return the current observation state used for grounding: screenshot, dimensions, window, and cursor."""
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    try:
        observation, digest = bundle.agent.observation.capture_with_digest()
    except Exception as exc:  # noqa: BLE001 - structured error, no traceback
        return _error_response(exc)
    info = observation.active_window_info
    try:
        bundle.auditor.emit(
            "observation",
            session_id,
            task_id=bundle.context.task.task_id,
            observation_id=observation.observation_id,
            active_app=info.process_name if info is not None else observation.active_window,
            result="ok",
            metadata={"source": "computer_observe"},
        )
    except Exception:
        logger.debug("observation audit failed", exc_info=True)
    bundle.metrics.incr("screenshot_count")
    return {
        "observation": observation.model_dump(),
        "digest": digest,
        "observation_id": observation.observation_id,
        "active_app": info.process_name if info is not None else observation.active_window,
    }


@mcp.tool()
def computer_screenshot(session_id: str) -> dict[str, object]:
    """Compatibility alias for the raw screenshot observation."""
    return computer_observe(session_id)


@mcp.tool()
async def computer_execute(
    session_id: str,
    action: str,
    x: int | None = None,
    y: int | None = None,
    text: str | None = None,
    keys: list[str] | None = None,
    delta: int = 0,
    approved: bool = False,
    expected_effect: str | None = None,
    x2: int | None = None,
    y2: int | None = None,
    target: str | None = None,
) -> dict[str, object]:
    """Validate and execute one grounded action; approval applies only to this action call.

    The hardcoded ``confidence=1.0`` is the client-asserted MODEL confidence; grounding,
    staleness, risk, approval, and verification run independently. ``expected_effect``
    opts into semantic verification (a stated effect must be observed or the result
    reports verification failed/uncertain — never silently successful). For
    ``action="drag"``, ``x``/``y`` are the drag start and the trailing ``x2``/``y2`` the
    drag end (both required, screenshot coordinates). ``action="move"`` requires
    ``x``/``y``; ``action="hotkey"`` requires ``keys`` (2-12 names);
    ``action="focus_window"`` requires ``target`` (a window title).
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    try:
        grounded = GroundedAction(
            action=action,  # type: ignore[arg-type]
            point=None if x is None or y is None else {"x": x, "y": y},
            to_point=None if x2 is None or y2 is None else {"x": x2, "y": y2},
            text=text,
            keys=keys or [],
            delta=delta,
            reason="Explicit MCP action",
            confidence=1.0,
            expected_effect=expected_effect,
            target=target,
        )
    except Exception as exc:  # noqa: BLE001 - unknown action type: fail closed, no crash
        return {"ok": False, "error": "invalid_action", "message": str(exc)}
    try:
        outcome = await bundle.agent.run_single(
            bundle.state, grounded, approved=approved, expected_effect=expected_effect
        )
    except TaskStopped as exc:
        return {"ok": False, "stopped": True, "message": str(exc)}
    except LimitExceeded as exc:
        return {"ok": False, "error": "limit_exceeded", "limit": exc.limit_name, "message": str(exc)}
    if outcome.kind == "rejected":
        return {"ok": False, "message": "Grounding rejected.", "reasons": outcome.reasons}
    if outcome.kind == "safety_denied":
        return {"ok": False, "message": outcome.message}
    if outcome.kind == "approval_required":
        return {"ok": False, "requires_approval": True, "message": outcome.message}
    if outcome.kind == "error":
        return {"ok": False, "error": "action_error", "message": outcome.message}
    result = outcome.result
    if result is None:  # defensive: executed outcomes always carry a result
        return {"ok": False, "error": "action_error", "message": "Execution produced no result."}
    payload = result.model_dump()
    # F2: the response path is redacted too (defense in depth).
    payload = _redact_result_payload(payload)
    payload.update(
        {
            "model_confidence": outcome.model_confidence,
            "grounding_confidence": outcome.grounding_confidence,
            "verification_confidence": outcome.verification_confidence,
        }
    )
    return payload


@mcp.tool()
async def run_goal(
    session_id: str,
    goal: str,
    approve_next_action: bool = False,
    auto_subtasks: bool = False,
) -> dict[str, object]:
    """Run the loop; approve_next_action authorizes at most one interactive action in this call.

    Recovery retries of the same approved action instance do not re-consume the budget;
    a new distinct action after exhaustion is denied fail-closed with ``requires_approval``.

    Long-running additive (trailing optional): ``auto_subtasks=True`` switches to the
    Multi-Subtask mode — the goal is decomposed by the LLM planner (validated
    deterministically, fail-closed) and executed as sequential subtasks through the same
    closed-loop executor. Callers omitting it get byte-identical single-goal behavior.
    """
    if auto_subtasks:
        return await _run_goal_auto_subtasks(session_id, goal, approve_next_action)
    try:
        bundle = _get_live_bundle(session_id)
    except _StoppedSession:
        # D1 fail-closed: a stopped session runs NOTHING. The response keeps the standard
        # run_goal shape (E6 contract) with an explicit failed stopped-result entry, so
        # "ok" is never vacuously true over an empty list (D2).
        memory = _stopped_sessions.get(session_id, {})
        stopped_result = ExecutionResult(
            ok=False,
            action=GroundedAction(action="done"),
            message="Session is stopped; run_goal refused (fail-closed stopped-session policy).",
        )
        return {
            "ok": False,
            "approval_budget_remaining": 1 if approve_next_action else 0,
            "results": [stopped_result.model_dump()],
            "session_id": session_id,
            "task_id": str(memory.get("task_id", "")),
            "termination_reason": "stopped_by_user",
            "stopped": True,
            "requires_approval": False,
            "step_count": int(memory.get("step_count", 0)),
            "metrics": memory.get("metrics") or {"counters": {}, "latencies": {}},
        }
    except _UnknownSession as exc:
        return _error_response(exc)
    approval_budget = 1 if approve_next_action else 0

    def approve_one(_action: GroundedAction, _reason: str) -> bool:
        nonlocal approval_budget
        if approval_budget <= 0:
            return False
        approval_budget -= 1
        return True

    try:
        results = await bundle.agent.run(goal, bundle.state, approval=approve_one)
    except Exception as exc:  # noqa: BLE001 - structured error, no traceback
        return _error_response(exc)
    task = bundle.agent.task
    termination = task.termination_reason.value if task.termination_reason else None
    results_payload: list[dict[str, object]] = []
    for item in results:
        payload = item.model_dump()
        # D3: provider-flagged suspicious content travels with its action result.
        action_payload = payload.get("action") or {}
        suspicious = bundle.agent.suspicious_contents.get(str(action_payload.get("action_id", "")))
        if suspicious:
            payload["suspicious_content"] = suspicious
        # F3: honest completion marking — provider-declared done is model-asserted.
        verification_payload = payload.get("verification") or {}
        if isinstance(verification_payload, dict) and verification_payload.get(
            "verification_method"
        ) == "provider_done":
            payload["completion_evidence"] = "model_declared"
        # F2: the response path is redacted too (defense in depth).
        results_payload.append(_redact_result_payload(payload))
    if task.status.value == "stopped" and session_id in _bundles:
        # F7: an internally-armed kill path that terminated this run gets the SAME
        # bundle hygiene as stop_session (the stop was already audited in-run).
        _close_stopped_bundle(
            session_id, bundle, source="run_goal_kill_path", audit_stop_events=False
        )
    return {
        # D2: an empty result list is NOT a success (no vacuous all() over []).
        "ok": bool(results) and all(item.ok for item in results),
        "approval_budget_remaining": approval_budget,
        "results": results_payload,
        "session_id": session_id,
        "task_id": task.task_id,
        "termination_reason": termination,
        "stopped": bool(bundle.state.stopped or task.status.value == "stopped"),
        "requires_approval": bool(bundle.agent.approval_denied),
        "step_count": bundle.state.step_count,
        "metrics": bundle.metrics.snapshot(),
    }


# --- long-running session tools (SubtasksProtocol section 9; additive) ----------------------


@mcp.tool()
def create_subtask(
    session_id: str,
    description: str,
    depends_on: list[str] | None = None,
) -> dict[str, object]:
    """Create one manual subtask (spec section 9); fail-closed on invalid graphs.

    Validates the session, the description, and the dependency list (every dependency
    must already exist, no self-dependency, no cycles, hard cap 50 subtasks). Works
    without any planner/LLM key.
    """
    if depends_on is not None and (
        isinstance(depends_on, (str, bytes)) or not isinstance(depends_on, (list, tuple))
    ):
        # Tool-boundary type gate: a non-iterable (or string) depends_on must surface as
        # the typed fail-closed error code, never as an escaping TypeError from the MCP
        # tool (the runtime's list() coercion would raise one).
        return {
            "ok": False,
            "error": "invalid_subtask",
            "message": "depends_on must be a list of subtask ids (or null); "
            "refusing a non-iterable value at the tool boundary",
        }
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    runtime = _get_or_create_runtime(bundle)
    try:
        subtask = runtime.create_subtask(description, depends_on)
    except (SubtaskError, LongRunningError) as exc:
        return _subtask_error_response(exc)
    return {
        "ok": True,
        "session_id": session_id,
        "subtask": {
            "subtask_id": subtask.subtask_id,
            "description": subtask.description,
            "status": subtask.status.value,
            "depends_on": list(subtask.depends_on),
            "created_at": subtask.created_at.isoformat(),
        },
        "total_subtasks": len(runtime.subtasks),
    }


@mcp.tool()
def list_subtasks(session_id: str) -> dict[str, object]:
    """List all subtasks with statuses as a structured, bounded response.

    Each summary carries bounded scalar fields plus result/recovery COUNTS — never
    result payloads, never history (spec section 9/19).
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    runtime = _get_or_create_runtime(bundle)
    return {
        "ok": True,
        "session_id": session_id,
        "total": len(runtime.subtasks),
        "counts": runtime.counts(),
        "subtasks": runtime.list_subtasks(),
    }


@mcp.tool()
async def run_subtask(
    session_id: str,
    subtask_id: str,
    approve_next_action: bool = False,
) -> dict[str, object]:
    """Execute exactly ONE ready subtask through the existing closed-loop executor.

    One bounded subtask per MCP call; state and checkpoints persist server-side between
    calls, so no MCP connection needs to stay open for hours (spec section 9, conflict
    C7). The subtask can never bypass safety, approval, grounding, validation, the stop
    token, verification, recovery, or audit. ``approve_next_action`` authorizes at most
    one interactive action in this call (run_goal budget semantics).
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    runtime = _get_or_create_runtime(bundle)
    try:
        outcome = await runtime.run_single_subtask(
            subtask_id, approve_next_action=approve_next_action
        )
    except LimitExceeded as exc:
        return {
            "ok": False,
            "error": "limit_exceeded",
            "limit": exc.limit_name,
            "message": str(exc),
        }
    except (SubtaskError, LongRunningError) as exc:
        return _subtask_error_response(exc)
    task = bundle.agent.task
    results_payload: list[dict[str, object]] = []
    for item in outcome.results:
        results_payload.append(_redact_result_payload(item.model_dump()))
    if task.status.value == "stopped" and session_id in _bundles:
        # F7: an internally-armed kill path gets the SAME bundle hygiene (stop audited
        # in-run already).
        _close_stopped_bundle(
            session_id, bundle, source="run_subtask_kill_path", audit_stop_events=False
        )
    subtask_snapshot = runtime.subtasks.get(subtask_id)
    subtask_status = subtask_snapshot.status.value if subtask_snapshot is not None else "unknown"
    return {
        "ok": bool(outcome.ok),
        "session_id": session_id,
        "subtask_id": subtask_id,
        "status": subtask_status,
        "termination_reason": outcome.termination_reason,
        "requires_approval": bool(outcome.requires_approval),
        "stopped": bool(
            outcome.stopped or bundle.state.stopped or task.status.value == "stopped"
        ),
        "results": results_payload,
        "executed_subtasks": list(outcome.executed),
        "approval_budget_remaining": outcome.approval_budget_remaining,
        "detail": outcome.detail[:500],
        "progress": runtime.progress(),
        "metrics": bundle.metrics.snapshot(),
    }


@mcp.tool()
def get_session_progress(session_id: str) -> dict[str, object]:
    """Deterministic progress report: status, percent, counts, elapsed, counters, checkpoint.

    The progress percentage is computed from subtask manager state (completed/total) —
    never invented. The response is structured and bounded (no history dumps).
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    runtime = bundle.extra.get("long_running")
    if not isinstance(runtime, LongRunningRuntime):
        zero_counts = {status.value: 0 for status in SubtaskStatus}
        budget_like = bundle.metrics.snapshot()["counters"]
        return {
            "ok": True,
            "session_id": session_id,
            "status": bundle.context.task.status.value,
            "task_status": bundle.context.task.status.value,
            "goal": redact_text(str(bundle.context.task.goal or ""))[0][:2_000],
            "continuation_of": None,
            "total_subtasks": 0,
            "completed_subtasks": 0,
            "progress_percent": 0.0,
            "counts": zero_counts,
            "current_subtask_id": None,
            "elapsed_seconds": round(bundle.enforcer.elapsed_seconds(), 3),
            "resource_counters": {
                "actions": int(budget_like.get("action_total", 0)),
                "model_calls": int(budget_like.get("model_calls", 0)),
                "steps": int(bundle.state.step_count),
                "subtasks": 0,
            },
            "resource_limits": {
                "max_session_seconds": bundle.limits.max_session_seconds,
                "max_session_actions": bundle.limits.max_session_actions,
                "max_session_model_calls": bundle.limits.max_session_model_calls,
                "max_session_steps": bundle.limits.max_session_steps,
                "max_subtasks": bundle.limits.max_subtasks,
            },
            "checkpoint": {
                "has_checkpoint": _checkpoint_manager.has_checkpoint(session_id),
                "last_trigger": None,
                "last_checkpoint_at": None,
            },
            "replan_attempts_used": 0,
            "replan_attempts_max": 3,
        }
    payload = runtime.progress()
    payload["ok"] = True
    payload["task_status"] = bundle.context.task.status.value
    return payload


def main() -> None:
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
