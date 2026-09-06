"""Boundary-evaluated health checks for long-running sessions (spec section 13).

Layering rule (master-mission section 5): this module imports only ``models``,
``limits``, and ``redaction`` — never orchestration, provider, or MCP modules.

Doctrines implemented here (conflict C6):

- There is NO background thread and NO scheduler. :class:`HealthMonitor` is a pure,
  deterministic evaluator the orchestrator calls at execution boundaries: ``maybe_check``
  evaluates only when a check is DUE (at least ``Limits.health_check_interval`` seconds
  since the last check, default 10 minutes) and otherwise returns ``None`` without
  touching any probe. The monitor is not a new execution loop and never runs one.
- The world is only ever read through INJECTED PROBES (observation, active app, active
  window, hung indicators, environment expectation), so tests fake everything and A5
  binds the real backend probes. A monitor constructed without probes gets fail-closed
  defaults that can never report ``healthy``.
- ``evaluate`` compares the CURRENT probe readings against the session's recorded
  expected environment (app/window) and reports: observation validity, active app/window,
  expected-environment match, probe-supplied hung indicators, and unexpected foreground
  changes. A probe failure is fail-closed (worst-case reading for that dimension), never
  an exception escaping to the caller.
- Results are ROUTING DATA, never execution: ``healthy`` -> the caller continues;
  ``degraded`` -> the caller decides per policy (re-observe, recovery, pause);
  ``unsafe`` -> the caller MUST pause/stop or run recovery. The monitor holds no action
  executor and performs no actions itself; probe-supplied strings are redacted so no
  secret can enter the result.

Verdict matrix (deterministic): an unexpected APPLICATION (foreground app differs from
the recorded expectation, or its identity became unavailable) is ``unsafe`` — input could
land in the wrong application. A window-title drift, a stale/invalid observation, hung
indicators, or an unverifiable expectation are ``degraded``. Everything verified is
``healthy``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from .limits import Limits
from .redaction import redact_text

__all__ = [
    "ExpectedEnvironment",
    "HealthCheckResult",
    "HealthMonitor",
    "HealthProbes",
    "HealthRecommendation",
    "HealthVerdict",
]

#: Bounded result structures (no unbounded growth; spec section 19).
DETAILS_CAP = 16
DETAIL_MAX_CHARS = 300
HUNG_INDICATORS_CAP = 16
HUNG_INDICATOR_MAX_CHARS = 200
_IDENTITY_MAX_CHARS = 200
_PROBE_ERROR_MAX_CHARS = 120


class HealthVerdict(StrEnum):
    """Deterministic verdict: healthy -> continue, degraded -> caller decides, unsafe -> stop."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNSAFE = "unsafe"


class HealthRecommendation(StrEnum):
    """Advisory routing data (the CALLER decides and acts; the monitor never does)."""

    CONTINUE = "continue"
    PAUSE = "pause"
    RECOVER = "recover"
    STOP = "stop"


class ExpectedEnvironment(BaseModel):
    """The session's recorded expected environment (app/window) supplied by a probe.

    ``None`` fields mean "not recorded" and are never compared (a missing expectation
    makes the environment unverifiable, which is degraded — not a mismatch).
    """

    active_app: str | None = None
    active_window: str | None = None


class HealthCheckResult(BaseModel):
    """One health-check evaluation: routing data only, never an executed action.

    All probe-supplied strings are redacted and bounded; ``details`` carries stable
    machine-readable reasons (``observation_unavailable``, ``expected_application_mismatch``,
    ``probe_error:<name>:...``) capped at :data:`DETAILS_CAP` entries.
    """

    observation_ok: bool
    active_app: str | None = None
    active_window: str | None = None
    expected_environment_ok: bool
    hung_indicators: list[str] = Field(default_factory=list)
    unexpected_foreground: bool = False
    verdict: HealthVerdict
    recommendation: HealthRecommendation
    details: list[str] = Field(default_factory=list)
    checked_at_monotonic: float

    @model_validator(mode="after")
    def _bound_lists(self) -> HealthCheckResult:
        """Re-clamp bounded lists so no construction path can grow them unbounded."""
        self.hung_indicators = [
            indicator[:HUNG_INDICATOR_MAX_CHARS]
            for indicator in self.hung_indicators[:HUNG_INDICATORS_CAP]
        ]
        self.details = [detail[:DETAIL_MAX_CHARS] for detail in self.details[:DETAILS_CAP]]
        return self


ObservationProbe = Callable[[], bool]
AppProbe = Callable[[], str | None]
WindowProbe = Callable[[], str | None]
HungIndicatorProbe = Callable[[], Sequence[str]]
ExpectationProbe = Callable[[], ExpectedEnvironment | None]


@dataclass(frozen=True)
class HealthProbes:
    """The five injected world-reading probes; the monitor holds no other I/O.

    A probe returns plain data: observation freshness (bool), active app/window identity
    (``str | None``; ``None`` = identity unavailable), probe-supplied hung indicators
    (empty when none), and the session's recorded :class:`ExpectedEnvironment`.
    """

    observation: ObservationProbe
    app: AppProbe
    window: WindowProbe
    hung: HungIndicatorProbe
    expectation: ExpectationProbe

    @classmethod
    def fail_closed(cls) -> HealthProbes:
        """Probes that can never confirm a healthy environment (used when none are bound).

        Observation unconfirmed, identities unavailable, no expectation verifiable: a
        monitor built with these always reports at least ``degraded`` — it can never be
        lied into ``healthy`` by a missing probe binding.
        """
        return cls(
            observation=lambda: False,
            app=lambda: None,
            window=lambda: None,
            hung=lambda: (),
            expectation=lambda: None,
        )


_RECOMMENDATION: dict[HealthVerdict, HealthRecommendation] = {
    HealthVerdict.HEALTHY: HealthRecommendation.CONTINUE,
    HealthVerdict.DEGRADED: HealthRecommendation.RECOVER,
    HealthVerdict.UNSAFE: HealthRecommendation.PAUSE,
}


def _redacted(value: str | None, limit: int) -> str | None:
    """Redact and bound a probe-supplied identity/indicator string."""
    if value is None:
        return None
    cleaned = redact_text(str(value))[0].strip()
    return cleaned[:limit] or None


class HealthMonitor:
    """Pure, deterministic, boundary-evaluated health check (no threads, no actions).

    State is RLock-protected and limited to the last-check anchor and the last result.
    The clock is injectable; probes are injected and immutable for the monitor's life.
    """

    def __init__(
        self,
        limits: Limits | None = None,
        *,
        probes: HealthProbes | None = None,
        clock: Callable[[], float] | None = None,
        start_monotonic: float | None = None,
    ) -> None:
        self.limits = (limits if limits is not None else Limits()).validate()
        self._probes = probes if probes is not None else HealthProbes.fail_closed()
        self._clock = clock if clock is not None else time.monotonic
        self._lock = threading.RLock()
        self._last_check_monotonic: float | None = (
            None if start_monotonic is None else float(start_monotonic)
        )
        self._last_result: HealthCheckResult | None = None

    # --- scheduling ------------------------------------------------------------
    @property
    def interval_seconds(self) -> float:
        return self.limits.health_check_interval

    @property
    def last_check_monotonic(self) -> float | None:
        return self._last_check_monotonic

    @property
    def last_result(self) -> HealthCheckResult | None:
        with self._lock:
            return None if self._last_result is None else self._last_result.model_copy(deep=True)

    def _resolve_now(self, now: float | None) -> float:
        return self._clock() if now is None else float(now)

    def is_due(self, now: float | None = None) -> bool:
        """True when a check is due (never checked, or interval elapsed since the last)."""
        with self._lock:
            if self._last_check_monotonic is None:
                return True
            resolved = self._resolve_now(now)
            return (resolved - self._last_check_monotonic) >= self.limits.health_check_interval

    def maybe_check(self, now: float | None = None) -> HealthCheckResult | None:
        """Evaluate only when a check is due; ``None`` means "not due, no probes touched".

        The first call on a fresh monitor is due (establishes a baseline at the first
        orchestration boundary); afterwards a check is due every
        ``Limits.health_check_interval`` seconds.
        """
        with self._lock:
            if not self.is_due(now):
                return None
            return self.evaluate(now)

    # --- evaluation ------------------------------------------------------------
    def evaluate(self, now: float | None = None) -> HealthCheckResult:
        """Unconditional evaluation (also re-anchors the schedule). Pure and fail-closed."""
        with self._lock:
            resolved = self._resolve_now(now)
            self._last_check_monotonic = resolved
            result = self._evaluate_probes(resolved)
            self._last_result = result.model_copy(deep=True)
            return result

    def _evaluate_probes(self, resolved: float) -> HealthCheckResult:
        details: list[str] = []

        def call(name: str, probe: Callable[[], object], default: object) -> object:
            try:
                return probe()
            except Exception as exc:  # noqa: BLE001 - probe failure is fail-closed, never fatal
                details.append(f"probe_error:{name}:{str(exc)[:_PROBE_ERROR_MAX_CHARS]}")
                return default

        observation_value = call("observation", self._probes.observation, False)
        app_value = call("app", self._probes.app, None)
        window_value = call("window", self._probes.window, None)
        hung_value = call("hung", self._probes.hung, ())
        expectation_value = call("expectation", self._probes.expectation, None)

        observation_ok = bool(observation_value)
        current_app = _redacted(app_value if isinstance(app_value, str) else None, _IDENTITY_MAX_CHARS)
        current_window = _redacted(
            window_value if isinstance(window_value, str) else None, _IDENTITY_MAX_CHARS
        )
        hung = [
            indicator
            for indicator in (
                _redacted(item, HUNG_INDICATOR_MAX_CHARS) or ""
                for item in (hung_value if isinstance(hung_value, (list, tuple)) else ())
            )
            if indicator
        ][:HUNG_INDICATORS_CAP]

        expectation: ExpectedEnvironment | None = None
        if isinstance(expectation_value, ExpectedEnvironment):
            # The expectation is normalized through the SAME redaction/bounding as the
            # current readings so comparison is apples-to-apples and no secret-bearing
            # identity can influence (or leak into) a result.
            expectation = ExpectedEnvironment(
                active_app=_redacted(expectation_value.active_app, _IDENTITY_MAX_CHARS),
                active_window=_redacted(expectation_value.active_window, _IDENTITY_MAX_CHARS),
            )

        unexpected_application = False
        unexpected_window = False
        expectation_ok = False
        if expectation is not None:
            if expectation.active_app is None:
                app_matches = True
            elif current_app is None:
                app_matches = False
                details.append("active_app_unavailable_vs_expected")
            else:
                app_matches = current_app == expectation.active_app
            if expectation.active_window is None:
                window_matches = True
            elif current_window is None:
                window_matches = False
                details.append("active_window_unavailable_vs_expected")
            else:
                window_matches = current_window == expectation.active_window
            unexpected_application = not app_matches
            unexpected_window = not window_matches
            expectation_ok = app_matches and window_matches
            if unexpected_application:
                details.append("expected_application_mismatch")
            elif unexpected_window:
                details.append("expected_window_mismatch")
        else:
            details.append("expected_environment_unavailable")

        if not observation_ok:
            details.append("observation_unavailable")
        if current_app is None:
            details.append("active_app_unavailable")
        if hung:
            details.append("hung_application")

        unexpected_foreground = unexpected_application or unexpected_window
        if unexpected_application:
            verdict = HealthVerdict.UNSAFE
        elif unexpected_window or not observation_ok or bool(hung) or expectation is None:
            verdict = HealthVerdict.DEGRADED
        else:
            verdict = HealthVerdict.HEALTHY

        return HealthCheckResult(
            observation_ok=observation_ok,
            active_app=current_app,
            active_window=current_window,
            expected_environment_ok=expectation_ok,
            hung_indicators=hung,
            unexpected_foreground=unexpected_foreground,
            verdict=verdict,
            recommendation=_RECOMMENDATION[verdict],
            details=details,
            checked_at_monotonic=resolved,
        )
