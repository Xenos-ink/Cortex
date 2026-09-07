"""Central resource limits and fail-closed enforcement.

Every limit class from master-mission section 6 lives here as a ``Limits`` field with a
safe default. ``Limits.validate()`` clamps user-provided values into safe bounds, and
``LimitEnforcer`` exposes ``check_*`` methods that raise :class:`LimitExceeded` (fail
closed) plus ``record_*`` mutators the controller calls as work happens. Elapsed time is
measured with ``time.monotonic`` so wall-clock changes cannot fool the task-duration gate.

Long-running sessions add session-level fields to ``Limits`` plus
:class:`SessionBudgetTracker`, a restorable tracker of the SHARED session budget
(duration/actions/model calls/steps/subtasks) that persists across subtasks and resume
(counters are restored from checkpoints, never reset).

Approval/health policy fields (master-mission 003, consumed by ``approval.py`` and
``health.py``): ``approval_epoch_seconds`` (60..86400, default 30 min — wall-clock half
of the approval-epoch expiry), ``approval_epoch_actions`` (1..1000, default 50 —
interactive-action half, whichever comes first ends the epoch) and
``health_check_interval`` (60..3600, default 10 min — boundary-evaluated health checks).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace


class LimitExceeded(RuntimeError):
    """Raised when a resource limit is exceeded; carries the ``Limits`` field name."""

    def __init__(self, limit_name: str, message: str) -> None:
        super().__init__(message)
        self.limit_name = limit_name


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class Limits:
    """Hard execution limits (defaults per master-mission section 6).

    Clamping bounds (``validate``): ``max_task_seconds`` 1..3600, ``max_actions`` 1..500,
    ``max_retries_per_action`` 0..5 (aligned with ``SessionState``), ``max_recovery_per_action``
    0..10, ``max_recovery_per_task`` 0..50, ``max_model_calls`` 1..1000,
    ``min_screenshot_interval_ms`` 0..60000, ``max_context_items`` 1..1000,
    ``max_sessions`` 1..64.

    Session-level long-running limits (additive; enforced by :class:`SessionBudgetTracker`,
    spanning every subtask/run of a session): ``max_session_seconds`` 1..86400 (default 4h,
    max configurable 24h), ``max_session_actions`` 1..2000, ``max_session_model_calls`` 1..500,
    ``max_session_steps`` 1..500, ``max_subtasks`` 1..50,
    ``context_summarize_every`` 1..500 (steps between context compressions).

    Approval/health policy limits (additive; consumed by ``approval.py``/``health.py``):
    ``approval_epoch_seconds`` 60..86400 (default 1800 = 30 minutes), ``approval_epoch_actions``
    1..1000 (default 50 interactive actions) — whichever is reached first ends the approval
    epoch — and ``health_check_interval`` 60..3600 (default 600 = 10 minutes).
    """

    max_task_seconds: float = 900.0
    max_actions: int = 100
    max_retries_per_action: int = 5
    max_recovery_per_action: int = 2
    max_recovery_per_task: int = 6
    max_model_calls: int = 60
    min_screenshot_interval_ms: int = 250
    max_context_items: int = 50
    max_sessions: int = 4
    max_session_seconds: float = 14400.0
    max_session_actions: int = 2000
    max_session_model_calls: int = 500
    max_session_steps: int = 500
    max_subtasks: int = 50
    context_summarize_every: int = 25
    approval_epoch_seconds: float = 1800.0
    approval_epoch_actions: int = 50
    health_check_interval: float = 600.0

    def validate(self) -> Limits:
        """Return a clamped copy of these limits; the original is left untouched."""
        return replace(
            self,
            max_task_seconds=_clamp(self.max_task_seconds, 1.0, 3600.0),
            max_actions=int(_clamp(self.max_actions, 1, 500)),
            max_retries_per_action=int(_clamp(self.max_retries_per_action, 0, 5)),
            max_recovery_per_action=int(_clamp(self.max_recovery_per_action, 0, 10)),
            max_recovery_per_task=int(_clamp(self.max_recovery_per_task, 0, 50)),
            max_model_calls=int(_clamp(self.max_model_calls, 1, 1000)),
            min_screenshot_interval_ms=int(_clamp(self.min_screenshot_interval_ms, 0, 60_000)),
            max_context_items=int(_clamp(self.max_context_items, 1, 1000)),
            max_sessions=int(_clamp(self.max_sessions, 1, 64)),
            max_session_seconds=_clamp(self.max_session_seconds, 1.0, 86400.0),
            max_session_actions=int(_clamp(self.max_session_actions, 1, 2000)),
            max_session_model_calls=int(_clamp(self.max_session_model_calls, 1, 500)),
            max_session_steps=int(_clamp(self.max_session_steps, 1, 500)),
            max_subtasks=int(_clamp(self.max_subtasks, 1, 50)),
            context_summarize_every=int(_clamp(self.context_summarize_every, 1, 500)),
            approval_epoch_seconds=_clamp(self.approval_epoch_seconds, 60.0, 86400.0),
            approval_epoch_actions=int(_clamp(self.approval_epoch_actions, 1, 1000)),
            health_check_interval=_clamp(self.health_check_interval, 60.0, 3600.0),
        )


class LimitEnforcer:
    """Tracks counters against validated :class:`Limits`; every check raises when tripped.

    Per-action counters (retries, per-action recovery) are reset via :meth:`begin_action`
    when the controller moves to a new action; task-level counters never reset.
    """

    def __init__(self, limits: Limits | None = None) -> None:
        self.limits = (limits if limits is not None else Limits()).validate()
        self._lock = threading.Lock()
        self._started_monotonic = time.monotonic()
        self._actions = 0
        self._model_calls = 0
        self._retries_current_action = 0
        self._recovery_current_action = 0
        self._recovery_task = 0
        self._screenshots = 0
        self._burst_screenshots = 0
        self._last_screenshot_monotonic: float | None = None

    # --- task duration -----------------------------------------------------
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started_monotonic

    def check_task_duration(self) -> None:
        elapsed = self.elapsed_seconds()
        if elapsed > self.limits.max_task_seconds:
            raise LimitExceeded(
                "max_task_seconds",
                f"Task duration {elapsed:.1f}s exceeded limit {self.limits.max_task_seconds:.1f}s.",
            )

    # --- actions -----------------------------------------------------------
    def check_action(self) -> None:
        if self._actions >= self.limits.max_actions:
            raise LimitExceeded(
                "max_actions",
                f"Action limit {self.limits.max_actions} reached; refusing further actions.",
            )

    def record_action(self) -> None:
        with self._lock:
            self._actions += 1

    def begin_action(self) -> None:
        """Reset per-action scopes (retries, per-action recovery) for a new action."""
        with self._lock:
            self._retries_current_action = 0
            self._recovery_current_action = 0

    # --- retries per action --------------------------------------------------
    def check_retry(self) -> None:
        if self._retries_current_action >= self.limits.max_retries_per_action:
            raise LimitExceeded(
                "max_retries_per_action",
                f"Retry limit {self.limits.max_retries_per_action} for the current action reached.",
            )

    def record_retry(self) -> None:
        with self._lock:
            self._retries_current_action += 1

    # --- recovery ------------------------------------------------------------
    def check_recovery(self) -> None:
        if self._recovery_current_action >= self.limits.max_recovery_per_action:
            raise LimitExceeded(
                "max_recovery_per_action",
                f"Per-action recovery limit {self.limits.max_recovery_per_action} reached.",
            )
        if self._recovery_task >= self.limits.max_recovery_per_task:
            raise LimitExceeded(
                "max_recovery_per_task",
                f"Task recovery limit {self.limits.max_recovery_per_task} reached.",
            )

    def record_recovery(self) -> None:
        with self._lock:
            self._recovery_current_action += 1
            self._recovery_task += 1

    # --- model calls -----------------------------------------------------------
    def check_model_call(self) -> None:
        if self._model_calls >= self.limits.max_model_calls:
            raise LimitExceeded(
                "max_model_calls",
                f"Model call limit {self.limits.max_model_calls} reached.",
            )

    def record_model_call(self) -> None:
        with self._lock:
            self._model_calls += 1

    # --- context size ----------------------------------------------------------
    def check_context_items(self, count: int) -> None:
        if count > self.limits.max_context_items:
            raise LimitExceeded(
                "max_context_items",
                f"Context items {count} exceed limit {self.limits.max_context_items}.",
            )

    # --- concurrent sessions -----------------------------------------------------
    def check_session_count(self, current_sessions: int) -> None:
        """Check a would-be session count against ``max_sessions`` (call before adding)."""
        if current_sessions >= self.limits.max_sessions:
            raise LimitExceeded(
                "max_sessions",
                f"Concurrent session limit {self.limits.max_sessions} reached.",
            )

    # --- screenshot rate gate ------------------------------------------------------
    # Refined semantics (PERF-004 C2, no field removed): ``min_screenshot_interval_ms``
    # protects FRESH observations — the loop_top capture on a new step, host-driven
    # observe captures, and recovery re-observes. Intra-step verification capture pairs
    # (validate re-capture, post-action capture, P0-H revalidate) are BURST-EXEMPT via
    # :meth:`record_burst_screenshot`: they skip the interval wait but still record into
    # the counters and refresh the pacing timestamp, so session-wide capture volume
    # stays enforced and bounded (per-step captures are structurally capped: at most one
    # staleness re-capture + one post-action capture per action).
    def can_screenshot(self) -> bool:
        with self._lock:
            if self._last_screenshot_monotonic is None:
                return True
            elapsed_ms = (time.monotonic() - self._last_screenshot_monotonic) * 1000.0
            return elapsed_ms >= self.limits.min_screenshot_interval_ms

    def record_screenshot(self) -> None:
        with self._lock:
            self._last_screenshot_monotonic = time.monotonic()
            self._screenshots += 1

    def record_burst_screenshot(self) -> None:
        """Record a burst-exempt intra-step capture (counted, pacing timestamp refreshed).

        Deliberately does NOT consult the interval gate — the caller asserts the
        intra-step exemption policy (agent.py ``_INTRA_STEP_EXEMPT_PHASES``). The count
        and the pacing timestamp are still updated so the session-wide protection stays
        truthful: the next gated capture measures from the latest capture of any kind.
        """
        with self._lock:
            self._last_screenshot_monotonic = time.monotonic()
            self._screenshots += 1
            self._burst_screenshots += 1

    def check_screenshot(self) -> None:
        """Raise when called sooner than ``min_screenshot_interval_ms`` after the last one."""
        if not self.can_screenshot():
            raise LimitExceeded(
                "min_screenshot_interval_ms",
                f"Screenshot interval below {self.limits.min_screenshot_interval_ms}ms.",
            )

    # --- introspection ---------------------------------------------------------
    def snapshot(self) -> dict[str, float | int | None]:
        """Return current counters for audit/telemetry purposes."""
        with self._lock:
            return {
                "elapsed_seconds": self.elapsed_seconds(),
                "actions": self._actions,
                "model_calls": self._model_calls,
                "retries_current_action": self._retries_current_action,
                "recovery_current_action": self._recovery_current_action,
                "recovery_task": self._recovery_task,
                "screenshots": self._screenshots,
                "burst_screenshots": self._burst_screenshots,
            }


class SessionBudgetExceeded(LimitExceeded):
    """Typed fail-closed signal that a session-level long-running budget is exhausted.

    Subclasses :class:`LimitExceeded` so existing fail-closed handling (typed
    ``limit_exceeded`` error codes, audited terminations) applies unchanged.
    """


_BUDGET_SNAPSHOT_VERSION = 1
_BUDGET_SNAPSHOT_KEYS = ("elapsed_seconds", "actions", "model_calls", "steps", "subtasks")


def _snapshot_number(snapshot: dict[str, object], key: str) -> float:
    value = snapshot[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"Session budget snapshot field {key!r} must be a number.")
    if value < 0:
        raise ValueError(f"Session budget snapshot field {key!r} must be non-negative.")
    return float(value)


class SessionBudgetTracker:
    """Restorable session-level counter tracker for long-running sessions.

    Complements (never replaces) :class:`LimitEnforcer`: the enforcer still gates each
    per-run task, while this tracker counts consumption of the SHARED session budget
    across every subtask and run of a session. Counters only ever grow within a session
    life; :meth:`restore` sets counters to checkpoint values (never zeroes them) and
    re-bases the monotonic elapsed-time anchor so a resumed session continues the
    original budget instead of getting a fresh one. All state is RLock-protected.
    """

    def __init__(
        self, limits: Limits | None = None, *, start_monotonic: float | None = None
    ) -> None:
        self.limits = (limits if limits is not None else Limits()).validate()
        self._lock = threading.RLock()
        self._started_monotonic = (
            time.monotonic() if start_monotonic is None else float(start_monotonic)
        )
        self._actions = 0
        self._model_calls = 0
        self._steps = 0
        self._subtasks = 0

    # --- session duration -----------------------------------------------------
    def elapsed_seconds(self) -> float:
        """Elapsed session seconds derived from the monotonic anchor (restorable)."""
        return time.monotonic() - self._started_monotonic

    def check_duration(self) -> None:
        elapsed = self.elapsed_seconds()
        if elapsed > self.limits.max_session_seconds:
            raise SessionBudgetExceeded(
                "max_session_seconds",
                f"Session duration {elapsed:.1f}s exceeded limit "
                f"{self.limits.max_session_seconds:.1f}s.",
            )

    # --- session counters -----------------------------------------------------
    def check_actions(self) -> None:
        if self._actions >= self.limits.max_session_actions:
            raise SessionBudgetExceeded(
                "max_session_actions",
                f"Session action limit {self.limits.max_session_actions} reached; "
                "refusing further actions.",
            )

    def check_model_calls(self) -> None:
        if self._model_calls >= self.limits.max_session_model_calls:
            raise SessionBudgetExceeded(
                "max_session_model_calls",
                f"Session model call limit {self.limits.max_session_model_calls} reached.",
            )

    def check_steps(self) -> None:
        if self._steps >= self.limits.max_session_steps:
            raise SessionBudgetExceeded(
                "max_session_steps",
                f"Session step limit {self.limits.max_session_steps} reached.",
            )

    def check_subtasks(self) -> None:
        if self._subtasks >= self.limits.max_subtasks:
            raise SessionBudgetExceeded(
                "max_subtasks",
                f"Session subtask limit {self.limits.max_subtasks} reached.",
            )

    def check_all(self) -> None:
        """Raise :class:`SessionBudgetExceeded` on the first exhausted budget dimension."""
        self.check_duration()
        self.check_actions()
        self.check_model_calls()
        self.check_steps()
        self.check_subtasks()

    # --- mutators (call after the corresponding check passes) -------------------
    def record_action(self) -> None:
        with self._lock:
            self._actions += 1

    def record_model_call(self) -> None:
        with self._lock:
            self._model_calls += 1

    def record_step(self) -> None:
        with self._lock:
            self._steps += 1

    def record_subtask(self) -> None:
        with self._lock:
            self._subtasks += 1

    # --- persistence ------------------------------------------------------------
    def snapshot(self) -> dict[str, float | int]:
        """Serializable counter snapshot for checkpoints; ``restore``-compatible."""
        with self._lock:
            return {
                "snapshot_version": _BUDGET_SNAPSHOT_VERSION,
                "elapsed_seconds": self.elapsed_seconds(),
                "actions": self._actions,
                "model_calls": self._model_calls,
                "steps": self._steps,
                "subtasks": self._subtasks,
            }

    def restore(self, snapshot: dict[str, object]) -> None:
        """Set counters to checkpoint values; never zeroes or refills the budget.

        Counters are set to the snapshot values; a counter that is already higher is
        never lowered (monotonic guarantee). The elapsed-time anchor is re-based so
        elapsed time continues from the snapshot value (wall-clock changes cannot
        refill the duration budget). Malformed snapshots fail closed (ValueError or
        TypeError, never a partial restore).
        """
        if not isinstance(snapshot, dict):
            raise TypeError("Session budget snapshot must be a mapping.")
        version = snapshot.get("snapshot_version", _BUDGET_SNAPSHOT_VERSION)
        if isinstance(version, bool) or version != _BUDGET_SNAPSHOT_VERSION:
            raise ValueError(f"Unsupported session budget snapshot version: {version!r}.")
        missing = [key for key in _BUDGET_SNAPSHOT_KEYS if key not in snapshot]
        if missing:
            raise ValueError(f"Session budget snapshot missing fields: {sorted(missing)}.")
        elapsed = _snapshot_number(snapshot, "elapsed_seconds")
        values = {key: int(_snapshot_number(snapshot, key)) for key in _BUDGET_SNAPSHOT_KEYS[1:]}
        with self._lock:
            if elapsed > self.elapsed_seconds():
                self._started_monotonic = time.monotonic() - elapsed
            for key, value in values.items():
                attr = f"_{key}"
                setattr(self, attr, max(getattr(self, attr), value))
