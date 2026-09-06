"""Central resource limits and fail-closed enforcement.

Every limit class from master-mission section 6 lives here as a ``Limits`` field with a
safe default. ``Limits.validate()`` clamps user-provided values into safe bounds, and
``LimitEnforcer`` exposes ``check_*`` methods that raise :class:`LimitExceeded` (fail
closed) plus ``record_*`` mutators the controller calls as work happens. Elapsed time is
measured with ``time.monotonic`` so wall-clock changes cannot fool the task-duration gate.
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
            }
