"""Fail-closed resume: rebuild live long-running state from a validated checkpoint.

Layering (master-mission 003 section 5): this module imports ``checkpoint_manager``, the
Wave-1 persistence surfaces (``limits``, ``context_manager``, ``subtask_manager``) and
``models`` — never orchestration, MCP, or backend modules. It performs NO orchestration:
the orchestrator (later wave) calls :meth:`ResumeManager.prepare` (or re-verifies the
bundle later via :meth:`ResumeBundle.verify_environment`) and gates continuation on the
checklist result.

Doctrine (SubtasksProtocol sections 8/14/21):
- Resume is a CONTINUATION: the bundle carries the checkpoint's original session id as
  the continuation identity; the orchestrator mints the new live session id and records
  this identity on it (Commander decision, conflict C4).
- Counters are restored, never reset: :class:`SessionBudgetTracker.restore` SETS counters
  to the checkpoint values (never zeroes them) and re-bases the elapsed-time anchor.
- Limits are never enlarged: the bundle's limits are the checkpoint's own limits,
  re-clamped by the current mechanism (canonical equality is enforced at load).
- Safety re-verification is enforced, not assumed: the bundle carries the checkpoint's
  recorded environment expectations (foreground app/window + allowlists) and a pre-flight
  checklist. A missing or mismatched current environment yields a refusal (or a
  not-ok checklist) — never a silent continuation.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .checkpoint_manager import (
    CheckpointManager,
    CheckpointPayload,
    EnvironmentExpectations,
    SessionSnapshot,
    TerminationState,
)
from .context_manager import ContextManager
from .limits import Limits, SessionBudgetTracker
from .redaction import redact_text
from .subtask_manager import SubtaskManager

__all__ = [
    "CheckOutcome",
    "ResumeBundle",
    "ResumeManager",
    "ResumeRefusalError",
    "SafetyCheckResult",
    "matches_allowlist",
]

#: Static (non-environment) pre-flight check names produced by :meth:`ResumeManager.prepare`.
STATIC_CHECK_NAMES = (
    "checkpoint_valid",
    "limits_preserved",
    "subtasks_restored",
    "budget_restored",
    "context_restored",
)
_STATIC_CHECK_NAMES = frozenset(STATIC_CHECK_NAMES)


class CheckOutcome(BaseModel):
    """One named pre-flight/verification outcome (bounded, structured)."""

    name: str = Field(min_length=1, max_length=100)
    ok: bool
    detail: str = Field(default="", max_length=500)


class SafetyCheckResult(BaseModel):
    """Pre-flight checklist result: resume may continue ONLY when ``ok`` is True."""

    outcomes: list[CheckOutcome] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.outcomes)

    @property
    def failures(self) -> list[CheckOutcome]:
        return [item for item in self.outcomes if not item.ok]

    def summary(self) -> str:
        """Bounded human-readable summary (no secret-bearing content, ids only)."""
        if not self.outcomes:
            return "no checks recorded"
        return "; ".join(
            f"{item.name}:{'ok' if item.ok else 'FAILED'}" for item in self.outcomes
        )[:500]


class ResumeRefusalError(RuntimeError):
    """Typed refusal to resume; carries the failing checklist (never a partial restore)."""

    def __init__(self, message: str, checks: SafetyCheckResult) -> None:
        super().__init__(message)
        self.checks = checks


def matches_allowlist(value: str, patterns: list[str]) -> bool:
    """Conservative allowlist match: case-insensitive exact or trailing-``*`` prefix.

    This is the checkpoint/resume-side re-verification hook only; the live executor keeps
    enforcing its own (stricter) allowlist machinery unchanged.
    """
    folded = value.casefold()
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern:
            continue
        folded_pattern = pattern.casefold()
        if folded_pattern.endswith("*"):
            if folded.startswith(folded_pattern[:-1]):
                return True
        elif folded == folded_pattern:
            return True
    return False


def _as_environment(value: EnvironmentExpectations | dict[str, Any] | None) -> EnvironmentExpectations:
    if isinstance(value, EnvironmentExpectations):
        return value
    return EnvironmentExpectations.model_validate(value or {})


def _environment_outcomes(
    expected: EnvironmentExpectations, current: EnvironmentExpectations
) -> list[CheckOutcome]:
    """Fail-closed environment re-verification outcomes (stale-state enforcement).

    Missing current information is treated as a mismatch, never as a pass: the checkpoint
    recorded what the environment looked like, and resume must re-verify it (spec 14).
    """
    outcomes: list[CheckOutcome] = []
    expects_foreground = (
        expected.active_process_name is not None or expected.active_window_title is not None
    )
    current_known = (
        current.active_process_name is not None or current.active_window_title is not None
    )
    if not expects_foreground:
        outcomes.append(
            CheckOutcome(
                name="current_environment_provided",
                ok=True,
                detail="checkpoint recorded no foreground expectations",
            )
        )
    else:
        outcomes.append(
            CheckOutcome(
                name="current_environment_provided",
                ok=current_known,
                detail=(
                    "current environment provided"
                    if current_known
                    else "checkpoint recorded foreground expectations but no current "
                    "environment was supplied; refusing stale-state continuation"
                ),
            )
        )
    if expected.active_process_name is None:
        outcomes.append(
            CheckOutcome(name="active_process_matches", ok=True, detail="not recorded")
        )
    elif current.active_process_name is None:
        outcomes.append(
            CheckOutcome(
                name="active_process_matches",
                ok=False,
                detail="current foreground process unknown (fail closed)",
            )
        )
    else:
        matched = current.active_process_name.casefold() == expected.active_process_name.casefold()
        outcomes.append(
            CheckOutcome(
                name="active_process_matches",
                ok=matched,
                detail="foreground process unchanged" if matched else "foreground process changed",
            )
        )
    if expected.active_window_title is None:
        outcomes.append(
            CheckOutcome(name="active_window_matches", ok=True, detail="not recorded")
        )
    elif current.active_window_title is None:
        outcomes.append(
            CheckOutcome(
                name="active_window_matches",
                ok=False,
                detail="current foreground window unknown (fail closed)",
            )
        )
    else:
        matched = current.active_window_title.casefold() == expected.active_window_title.casefold()
        outcomes.append(
            CheckOutcome(
                name="active_window_matches",
                ok=matched,
                detail="foreground window unchanged" if matched else "foreground window changed",
            )
        )
    if not expected.allowed_processes:
        outcomes.append(
            CheckOutcome(name="process_allowlist_satisfied", ok=True, detail="allowlist not recorded")
        )
    elif current.active_process_name is None:
        outcomes.append(
            CheckOutcome(
                name="process_allowlist_satisfied",
                ok=False,
                detail="cannot verify process allowlist without the current foreground process",
            )
        )
    else:
        allowed = matches_allowlist(current.active_process_name, expected.allowed_processes)
        outcomes.append(
            CheckOutcome(
                name="process_allowlist_satisfied",
                ok=allowed,
                detail="current process is allowlisted" if allowed else "current process is not allowlisted",
            )
        )
    if not expected.allowed_windows:
        outcomes.append(
            CheckOutcome(name="window_allowlist_satisfied", ok=True, detail="allowlist not recorded")
        )
    elif current.active_window_title is None:
        outcomes.append(
            CheckOutcome(
                name="window_allowlist_satisfied",
                ok=False,
                detail="cannot verify window allowlist without the current foreground window",
            )
        )
    else:
        allowed = matches_allowlist(current.active_window_title, expected.allowed_windows)
        outcomes.append(
            CheckOutcome(
                name="window_allowlist_satisfied",
                ok=allowed,
                detail="current window is allowlisted" if allowed else "current window is not allowlisted",
            )
        )
    return outcomes


@dataclass
class ResumeBundle:
    """Everything the orchestrator needs to continue a session from a checkpoint.

    The restored components (``subtasks``, ``budget``, ``context``) hold EXACTLY the
    checkpointed state — counters equal checkpoint values, limits equal the checkpoint's
    own (clamped) limits. ``checks`` is the pre-flight checklist; continuation is allowed
    only when ``checks.ok`` is True after environment verification.
    """

    payload: CheckpointPayload
    session_id: str
    continuation_identity: str
    goal: str
    limits: Limits
    subtasks: SubtaskManager
    budget: SessionBudgetTracker
    context: ContextManager
    session: SessionSnapshot
    environment: EnvironmentExpectations
    termination: TerminationState
    recent_history: list[str]
    current_subtask_id: str | None
    checks: SafetyCheckResult

    def verify_environment(
        self, current: EnvironmentExpectations | dict[str, Any] | None
    ) -> SafetyCheckResult:
        """Re-verify the CURRENT environment against the checkpoint's expectations.

        Returns the full checklist (static outcomes + environment outcomes). Callers MUST
        refuse to continue when ``ok`` is False — a mismatch is a refusal result, never a
        continuation.
        """
        env_outcomes = _environment_outcomes(self.environment, _as_environment(current))
        static = [item for item in self.checks.outcomes if item.name in _STATIC_CHECK_NAMES]
        return SafetyCheckResult(outcomes=[*static, *env_outcomes])


class ResumeManager:
    """Loads a checkpoint via :class:`CheckpointManager` and rebuilds the restore bundle."""

    def __init__(self, checkpoint_manager: CheckpointManager | None = None) -> None:
        self._checkpoints = checkpoint_manager if checkpoint_manager is not None else CheckpointManager()
        self._lock = threading.RLock()

    @property
    def checkpoints(self) -> CheckpointManager:
        return self._checkpoints

    def prepare(
        self,
        path: str | Path,
        *,
        current_environment: EnvironmentExpectations | dict[str, Any] | None = None,
    ) -> ResumeBundle:
        """Load + validate + rebuild; fail-closed on any defect or env mismatch.

        With ``current_environment`` given, environment verification runs EAGERLY and a
        mismatch raises :class:`ResumeRefusalError`. Without it, the bundle is returned
        with the environment checks marked failed (pending) in ``checks`` — the caller
        MUST run :meth:`ResumeBundle.verify_environment` and refuse to continue unless
        ``ok``. Any load/restore defect raises a typed error; nothing is partially
        restored.
        """
        payload = self._checkpoints.load(path)
        limits = payload.limits_resolved
        outcomes: list[CheckOutcome] = [
            CheckOutcome(name="checkpoint_valid", ok=True, detail="schema, structure, integrity valid"),
            CheckOutcome(
                name="limits_preserved",
                ok=True,
                detail="checkpoint's own limits, re-clamped by the current mechanism",
            ),
        ]
        bundle = self._restore(payload, limits, outcomes)
        env_outcomes = _environment_outcomes(payload.environment, _as_environment(current_environment))
        bundle.checks = SafetyCheckResult(outcomes=[*outcomes, *env_outcomes])
        if current_environment is not None and not bundle.checks.ok:
            raise ResumeRefusalError(
                "resume refused: environment re-verification failed "
                f"({bundle.checks.summary()})",
                bundle.checks,
            )
        return bundle

    def _restore(
        self,
        payload: CheckpointPayload,
        limits: Limits,
        outcomes: list[CheckOutcome],
    ) -> ResumeBundle:
        """Rebuild subtasks/budget/context; any defect becomes a typed refusal."""
        with self._lock:
            try:
                subtasks = SubtaskManager(max_subtasks=limits.max_subtasks)
                subtasks.restore(payload.subtasks)
                outcomes.append(
                    CheckOutcome(
                        name="subtasks_restored",
                        ok=True,
                        detail=f"{len(subtasks)} subtasks with dependency graph",
                    )
                )
            except Exception as exc:
                raise self._refusal(outcomes, "subtasks_restored", exc) from exc
            try:
                budget = SessionBudgetTracker(limits=limits)
                budget.restore(payload.budget)
                snapshot = budget.snapshot()
                counters_equal = all(
                    snapshot[key] == payload.budget[key] for key in ("actions", "model_calls", "steps", "subtasks")
                ) and snapshot["elapsed_seconds"] >= float(payload.budget["elapsed_seconds"]) - 0.05
                if not counters_equal:
                    raise ValueError("restored counters do not equal the checkpoint values")
                outcomes.append(
                    CheckOutcome(
                        name="budget_restored",
                        ok=True,
                        detail="counters set to checkpoint values (never reset, never refilled)",
                    )
                )
            except Exception as exc:
                raise self._refusal(outcomes, "budget_restored", exc) from exc
            try:
                context = ContextManager(summarize_every=limits.context_summarize_every)
                context.restore(payload.context)
                outcomes.append(
                    CheckOutcome(
                        name="context_restored",
                        ok=True,
                        detail="summary, recent window, and counters restored",
                    )
                )
            except Exception as exc:
                raise self._refusal(outcomes, "context_restored", exc) from exc
            return ResumeBundle(
                payload=payload,
                session_id=payload.session_id,
                continuation_identity=payload.continuation_of or payload.session_id,
                goal=payload.goal,
                limits=limits,
                subtasks=subtasks,
                budget=budget,
                context=context,
                session=payload.session,
                environment=payload.environment,
                termination=payload.termination,
                recent_history=list(payload.recent_history),
                current_subtask_id=payload.current_subtask_id,
                checks=SafetyCheckResult(outcomes=list(outcomes)),
            )

    @staticmethod
    def _refusal(
        outcomes: list[CheckOutcome],
        failed_check: str,
        exc: Exception,
    ) -> ResumeRefusalError:
        detail = redact_text(str(exc))[0][:500]
        outcomes.append(CheckOutcome(name=failed_check, ok=False, detail=detail))
        return ResumeRefusalError(
            f"resume refused: {failed_check} failed ({detail})", SafetyCheckResult(outcomes=list(outcomes))
        )
