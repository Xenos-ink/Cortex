"""Health monitor tests (spec sections 13 + 17 "Health").

Covers: boundary-evaluated periodic scheduling (due only when
``health_check_interval`` elapsed, on an injected clock — no threads, no sleeps),
unexpected foreground and unexpected application detection, hung-application
indicators, safe pause/recovery routing data (verdicts + recommendations are DATA the
caller acts on), probe-failure fail-closed behavior, bounded result structures, secret
redaction, and proof that the monitor never executes actions (its public API exposes no
executor and probes are the only callables it ever invokes).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from computer_use_mcp.health import (
    DETAIL_MAX_CHARS,
    DETAILS_CAP,
    HUNG_INDICATOR_MAX_CHARS,
    HUNG_INDICATORS_CAP,
    ExpectedEnvironment,
    HealthCheckResult,
    HealthMonitor,
    HealthProbes,
    HealthRecommendation,
    HealthVerdict,
)
from computer_use_mcp.limits import Limits


class FakeClock:
    """Deterministic monotonic-style clock; tests advance it explicitly (no sleeps)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class ProbeCalls:
    """Per-probe invocation counters (proves the monitor touches nothing else)."""

    observation: int = 0
    app: int = 0
    window: int = 0
    hung: int = 0
    expectation: int = 0
    extra_callables: list[str] = field(default_factory=list)


EXPECTED = ExpectedEnvironment(active_app="notepad.exe", active_window="Untitled - Notepad")


def make_probes(
    *,
    observation: bool | Exception = True,
    app: str | None | Exception = "notepad.exe",
    window: str | None | Exception = "Untitled - Notepad",
    hung: Sequence[str] | Exception = (),
    expectation: ExpectedEnvironment | None | Exception = EXPECTED,
) -> tuple[HealthProbes, ProbeCalls]:
    """Build injected probes with fixed readings and invocation counters."""
    calls = ProbeCalls()

    def wrap(counter_attr: str, reading: object) -> object:
        def probe() -> object:
            setattr(calls, counter_attr, getattr(calls, counter_attr) + 1)
            if isinstance(reading, Exception):
                raise reading
            return reading

        return probe

    probes = HealthProbes(
        observation=wrap("observation", observation),  # type: ignore[arg-type]
        app=wrap("app", app),  # type: ignore[arg-type]
        window=wrap("window", window),  # type: ignore[arg-type]
        hung=wrap("hung", hung),  # type: ignore[arg-type]
        expectation=wrap("expectation", expectation),  # type: ignore[arg-type]
    )
    return probes, calls


def make_monitor(
    probes: HealthProbes | None = None,
    *,
    interval: float = 600.0,
    clock: FakeClock | None = None,
) -> tuple[HealthMonitor, FakeClock]:
    clock = clock if clock is not None else FakeClock()
    monitor = HealthMonitor(
        Limits(health_check_interval=interval), probes=probes, clock=clock
    )
    return monitor, clock


# --- limits wiring ------------------------------------------------------------------------------------


def test_health_interval_defaults_and_clamps() -> None:
    assert Limits().health_check_interval == 600.0  # default: every 10 minutes
    high = Limits(health_check_interval=99_999.0).validate()
    low = Limits(health_check_interval=0.0).validate()
    assert high.health_check_interval == 3600.0
    assert low.health_check_interval == 60.0
    monitor, _ = make_monitor(make_probes()[0], interval=99_999.0)
    assert monitor.interval_seconds == 3600.0  # monitor uses the CLAMPED limits


# --- periodic due-evaluation (boundary-evaluated, no scheduler) ---------------------------------------


def test_first_check_is_due_then_interval_scheduling_applies() -> None:
    probes, calls = make_probes()
    monitor, clock = make_monitor(probes, interval=600.0)
    assert monitor.is_due()  # fresh monitor: baseline check due at first boundary

    first = monitor.maybe_check()
    assert first is not None
    assert first.checked_at_monotonic == clock.now
    assert calls.observation == 1 and calls.app == 1 and calls.expectation == 1

    clock.advance(599)
    assert monitor.is_due() is False
    assert monitor.maybe_check() is None  # not due -> no evaluation at all

    clock.advance(1)  # exactly 600s since the last check -> due (inclusive boundary)
    second = monitor.maybe_check()
    assert second is not None
    assert second.checked_at_monotonic == clock.now
    assert calls.observation == 2


def test_not_due_check_touches_no_probes() -> None:
    probes, calls = make_probes()
    monitor, clock = make_monitor(probes, interval=600.0)
    monitor.maybe_check()
    baseline = ProbeCalls(**calls.__dict__)
    clock.advance(10.0)
    assert monitor.maybe_check() is None
    assert calls.__dict__ == baseline.__dict__  # zero probe invocations while not due


def test_evaluate_reanchors_the_schedule() -> None:
    monitor, clock = make_monitor(make_probes()[0], interval=600.0)
    monitor.evaluate()  # unconditional evaluation (orchestrator escape hatch)
    clock.advance(599.0)
    assert monitor.is_due() is False
    monitor.evaluate()  # re-anchors even though it was not due
    clock.advance(599.0)
    assert monitor.is_due() is False
    clock.advance(1.0)
    assert monitor.is_due() is True


# --- detection matrix ----------------------------------------------------------------------------------


def test_healthy_when_environment_matches_expectation() -> None:
    monitor, _ = make_monitor(make_probes()[0])
    result = monitor.evaluate()
    assert result.verdict is HealthVerdict.HEALTHY
    assert result.recommendation is HealthRecommendation.CONTINUE
    assert result.observation_ok is True
    assert result.active_app == "notepad.exe"
    assert result.active_window == "Untitled - Notepad"
    assert result.expected_environment_ok is True
    assert result.unexpected_foreground is False
    assert result.hung_indicators == []


def test_unexpected_application_is_unsafe() -> None:
    probes, _ = make_probes(app="calc.exe")
    monitor, _ = make_monitor(probes)
    result = monitor.evaluate()
    assert result.verdict is HealthVerdict.UNSAFE
    assert result.recommendation is HealthRecommendation.PAUSE
    assert result.unexpected_foreground is True
    assert result.expected_environment_ok is False
    assert "expected_application_mismatch" in result.details


def test_unexpected_window_drift_is_degraded() -> None:
    # Same expected application, different window title: foreground changed but input
    # still lands in the expected app -> degraded (caller decides per policy).
    probes, _ = make_probes(window="other.txt - Notepad")
    monitor, _ = make_monitor(probes)
    result = monitor.evaluate()
    assert result.verdict is HealthVerdict.DEGRADED
    assert result.recommendation is HealthRecommendation.RECOVER
    assert result.unexpected_foreground is True
    assert result.expected_environment_ok is False
    assert "expected_window_mismatch" in result.details


def test_app_identity_unavailable_is_fail_closed_unsafe() -> None:
    # Expected app is known but current identity is unavailable: cannot confirm the
    # foreground -> treated as an unexpected application (fail closed).
    probes, _ = make_probes(app=None)
    monitor, _ = make_monitor(probes)
    result = monitor.evaluate()
    assert result.verdict is HealthVerdict.UNSAFE
    assert result.unexpected_foreground is True
    assert "active_app_unavailable_vs_expected" in result.details


def test_hung_application_is_reported_degraded() -> None:
    probes, _ = make_probes(hung=("window_not_responding", "input_queue_stuck"))
    monitor, _ = make_monitor(probes)
    result = monitor.evaluate()
    assert result.verdict is HealthVerdict.DEGRADED
    assert result.recommendation is HealthRecommendation.RECOVER
    assert result.hung_indicators == ["window_not_responding", "input_queue_stuck"]
    assert "hung_application" in result.details
    assert result.expected_environment_ok is True  # environment matches; app is hung
    assert result.unexpected_foreground is False


def test_stale_observation_is_degraded() -> None:
    probes, _ = make_probes(observation=False)
    monitor, _ = make_monitor(probes)
    result = monitor.evaluate()
    assert result.verdict is HealthVerdict.DEGRADED
    assert result.observation_ok is False
    assert "observation_unavailable" in result.details
    assert result.expected_environment_ok is True


def test_missing_expectation_is_degraded_not_mismatch() -> None:
    # Without a recorded expectation the environment is unverifiable (degraded), but no
    # mismatch can be claimed -> unexpected_foreground stays False.
    probes, _ = make_probes(expectation=None)
    monitor, _ = make_monitor(probes)
    result = monitor.evaluate()
    assert result.verdict is HealthVerdict.DEGRADED
    assert result.expected_environment_ok is False
    assert result.unexpected_foreground is False
    assert "expected_environment_unavailable" in result.details


def test_partial_expectation_only_compares_recorded_fields() -> None:
    probes, _ = make_probes(
        expectation=ExpectedEnvironment(active_app="notepad.exe", active_window=None),
    )
    monitor, _ = make_monitor(probes)
    result = monitor.evaluate()
    assert result.verdict is HealthVerdict.HEALTHY  # window not recorded -> not compared
    assert result.expected_environment_ok is True


# --- fail-closed construction and probe failures --------------------------------------------------------


def test_monitor_without_probes_can_never_report_healthy() -> None:
    monitor, _ = make_monitor(None)  # fail_closed default probes
    result = monitor.evaluate()
    assert result.verdict is HealthVerdict.DEGRADED
    assert result.verdict is not HealthVerdict.HEALTHY
    assert result.observation_ok is False
    assert result.expected_environment_ok is False


def test_probe_exception_is_fail_closed_and_never_escapes() -> None:
    probes, _ = make_probes(
        observation=RuntimeError("capture blew up"),
        app=RuntimeError("enum failed"),
        window=RuntimeError("no window"),
        hung=RuntimeError("no indicators"),
        expectation=RuntimeError("state gone"),
    )
    monitor, _ = make_monitor(probes)
    result = monitor.evaluate()  # must not raise
    assert result.verdict is HealthVerdict.DEGRADED
    assert result.observation_ok is False
    probe_errors = [detail for detail in result.details if detail.startswith("probe_error:")]
    assert len(probe_errors) == 5
    assert any(detail.startswith("probe_error:observation:") for detail in probe_errors)
    assert any("capture blew up" in detail for detail in probe_errors)


def test_app_probe_failure_with_expectation_is_unsafe() -> None:
    probes, _ = make_probes(app=RuntimeError("enum failed"))
    monitor, _ = make_monitor(probes)
    result = monitor.evaluate()
    assert result.verdict is HealthVerdict.UNSAFE  # identity unavailable vs expected


def test_verdict_recommendation_routing_table() -> None:
    cases = [
        (make_probes()[0], HealthVerdict.HEALTHY, HealthRecommendation.CONTINUE),
        (make_probes(hung=("hung_window",))[0], HealthVerdict.DEGRADED, HealthRecommendation.RECOVER),
        (make_probes(app="calc.exe")[0], HealthVerdict.UNSAFE, HealthRecommendation.PAUSE),
    ]
    for probes, verdict, recommendation in cases:
        monitor, _ = make_monitor(probes)
        result = monitor.evaluate()
        assert result.verdict is verdict
        assert result.recommendation is recommendation


# --- bounded structures and redaction -------------------------------------------------------------------


def test_result_lists_are_bounded_by_the_model() -> None:
    result = HealthCheckResult(
        observation_ok=True,
        expected_environment_ok=True,
        verdict=HealthVerdict.HEALTHY,
        recommendation=HealthRecommendation.CONTINUE,
        hung_indicators=[x * (HUNG_INDICATOR_MAX_CHARS + 50) for x in "abcdefghijklmnopqrstuvwxyz0123"],
        details=[d * (DETAIL_MAX_CHARS + 50) for d in "abcdefghijklmnopqrstuvwxyz0123"],
        checked_at_monotonic=1.0,
    )
    assert len(result.hung_indicators) == HUNG_INDICATORS_CAP
    assert len(result.details) == DETAILS_CAP
    assert all(len(indicator) <= HUNG_INDICATOR_MAX_CHARS for indicator in result.hung_indicators)
    assert all(len(detail) <= DETAIL_MAX_CHARS for detail in result.details)


def test_secrets_are_redacted_from_probe_readings() -> None:
    probes, _ = make_probes(
        app="notepad.exe",
        window="Sign in - password=hunter2pass",
        hung=("api_key=supersecretvalue123",),
    )
    monitor, _ = make_monitor(probes)
    result = monitor.evaluate()
    dumped = result.model_dump_json()
    assert "hunter2pass" not in dumped
    assert "supersecretvalue123" not in dumped
    assert "[REDACTED:" in dumped
    assert result.active_window is not None and "REDACTED" in result.active_window


def test_last_result_is_none_then_a_defensive_copy() -> None:
    monitor, _ = make_monitor(make_probes()[0])
    assert monitor.last_result is None
    result = monitor.evaluate()
    assert monitor.last_result is not None
    monitor.last_result.hung_indicators.append("tampered")
    monitor.last_result.details.append("tampered")
    assert monitor.last_result.hung_indicators == []
    assert monitor.last_result.details == []
    assert result.verdict is HealthVerdict.HEALTHY


# --- the monitor never executes actions -----------------------------------------------------------------


def test_monitor_public_api_holds_no_executor() -> None:
    # Code-audit proof at runtime: the ONLY public callables on HealthMonitor are the
    # pure evaluators; there is no execute/perform/run entry point and no action type.
    public_callables = {
        name
        for name in dir(HealthMonitor)
        if not name.startswith("_") and callable(getattr(HealthMonitor, name))
    }
    assert public_callables == {"evaluate", "is_due", "maybe_check"}


def test_monitor_only_ever_invokes_the_injected_probes() -> None:
    # Runtime proof: one evaluation calls each probe exactly once, a not-due boundary
    # calls none, and nothing else callable is stored or invoked by the monitor.
    probes, calls = make_probes()
    monitor, clock = make_monitor(probes, interval=600.0)
    monitor.maybe_check()  # first boundary: the baseline check is due
    one = ProbeCalls(observation=1, app=1, window=1, hung=1, expectation=1)
    assert calls.__dict__ == one.__dict__
    clock.advance(10.0)
    assert monitor.maybe_check() is None
    assert calls.__dict__ == one.__dict__  # not due: zero invocations
    clock.advance(590.0)  # exactly 600s since the last check -> due again
    monitor.maybe_check()
    two = ProbeCalls(observation=2, app=2, window=2, hung=2, expectation=2)
    assert calls.__dict__ == two.__dict__
    # The only callables the monitor keeps are the five probes.
    assert vars(monitor)["_probes"] is probes
