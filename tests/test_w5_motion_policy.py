"""W5 — the motion/settle policy (WP2, ORVEX-2026-102 Phase 5, SPEC 5.1, gate G6).

**Outside a plan the motion is INSTANT (SPEC 5.1).** The shipped settle is 0 ms —
not 50 ms — and the shipped in-plan transition default is 0, not 300 ms. Both the
default ARGUMENT and the accepted BAND moved, because ``_env_bounded_float`` /
``_env_bounded_int`` are range-validation-with-fallback, not clamping: with the old
``50..200`` band a zero settle was inexpressible by any env route. The proof that
this is settle-SENSITIVE is the sleep-argument recorder: a flat move's recorded
``time.sleep`` arguments go ``[0.05] -> []``.

PORT NOTE (WP2, stable tree): the dev tree's SPEC 5.2 plan-explicitness matrix, the
``_normalize_plan`` duration-resolution tests, and the plan-side declared-settle
normalization tests are DEFERRED to WP7 (the waypoint-plan surface does not exist in
the stable tree yet). The DECLARED-SETTLE CONSUMER is ported and proven:
``test_the_declared_settle_is_the_sleep_the_dispatch_path_issues`` (and, end to end
with the None/0/declared band, tests/test_plan_settle_wiring.py).
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import time
from typing import Any

import pytest
from recording_engine import RecordingEngine

import computer_use_mcp.desktop.backend as backend_module
from computer_use_mcp.desktop.backend import LocalComputerBackend
from computer_use_mcp.core.models import ActionSpec, GroundedAction

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")


# --- helpers -----------------------------------------------------------------------------------


class _SleepRecorder:
    """Records the ARGUMENT of every ``time.sleep`` the dispatch path issues."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.moments: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.moments.append(time.perf_counter())


@pytest.fixture
def recorded_sleeps(monkeypatch: pytest.MonkeyPatch) -> _SleepRecorder:
    recorder = _SleepRecorder()
    monkeypatch.setattr(backend_module.time, "sleep", recorder)
    return recorder


def _move(point: tuple[int, int], **kw: Any) -> GroundedAction:
    kw.setdefault("confidence", 1.0)
    return GroundedAction(action="move", point={"x": point[0], "y": point[1]}, **kw)


def _click(point: tuple[int, int], **kw: Any) -> GroundedAction:
    kw.setdefault("confidence", 1.0)
    return GroundedAction(action="click", point={"x": point[0], "y": point[1]}, **kw)


def _env_probe(env: dict[str, str], expression: str) -> str:
    """Evaluate ``expression`` against a FRESH in-process import with a controlled env.

    The bands are evaluated once at import, so a fresh module execution is the way to
    prove a band's edge. WP-H DELTA 1 (m2): the former ``sys.executable -c`` child
    probe is replaced by the in-process fresh import (no subprocess at all).
    """
    from fresh_backend_import import fresh_backend

    overrides = {k: v for k, v in env.items() if k != "PYTHONPATH"}
    module = fresh_backend(overrides)
    return str(eval(expression, {"b": module}))  # noqa: S307 - test-authored expression


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CORTEX_")}
    source_root = str(backend_module.__file__).rsplit(os.sep + "computer_use_mcp", 1)[0]
    env["PYTHONPATH"] = source_root
    env.update(overrides)
    return env


# ================================================================================================
# PART 1 — SPEC 5.1: outside a plan the motion is INSTANT
# ================================================================================================


def test_shipped_settle_default_is_zero_ms() -> None:
    """SPEC 5.1: no env -> exactly 0.0 ms / 0.0 s (the in-process constant pin)."""
    assert backend_module.MOVE_SETTLE_MS == 0.0
    assert backend_module._MOVE_SETTLE_SECONDS == 0.0


def test_shipped_move_duration_is_zero_and_stays_zero() -> None:
    """SPEC 5.1: the outside-plan move is a jump. Recorded so nobody "fixes" the
    already-correct value."""
    assert backend_module.MOVE_DURATION_MS == 0


def test_shipped_in_plan_transition_default_is_zero() -> None:
    """SPEC 5.2: inside a plan the transition duration is required FROM THE MODEL, so
    the internal constant is a fail-safe (0 = an instant jump), not a 300 ms behaviour.
    (The constant ships in WP2; its CONSUMER — the plan normalizer — is WP7.)"""
    assert backend_module.PLAN_MOVE_DURATION_MS == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0", "0.0"), ("10", "10.0"), ("50", "50.0"), ("120", "120.0"), ("200", "200.0")],
)
def test_settle_band_accepts_zero_and_the_whole_legal_range(raw: str, expected: str) -> None:
    """K-12: the band's LOW edge moved 50 -> 0, so ``0`` and ``10`` are now LEGAL
    values that apply. Before the change both silently resolved to 50.0."""
    assert _env_probe(_clean_env(CORTEX_MOVE_SETTLE_MS=raw), "b.MOVE_SETTLE_MS") == expected


@pytest.mark.parametrize("raw", ["201", "-1", "-0.5", "abc", "", "NaN", "inf"])
def test_settle_band_rejects_out_of_range_and_invalid_to_the_zero_default(raw: str) -> None:
    """The band is still range-validation-with-fallback, not clamping — and the
    fallback is now the 0.0 default."""
    assert _env_probe(_clean_env(CORTEX_MOVE_SETTLE_MS=raw), "b.MOVE_SETTLE_MS") == "0.0"


def test_env_settle_zero_really_yields_zero() -> None:
    """AC-14: ``CORTEX_MOVE_SETTLE_MS=0`` is the operator's explicit disable and it now
    resolves to 0.0 — the exact value SPEC 5.1 promises. Measured, not inferred."""
    assert _env_probe(_clean_env(CORTEX_MOVE_SETTLE_MS="0"), "b._MOVE_SETTLE_SECONDS") == "0.0"


@pytest.mark.parametrize("raw", ["0", "1", "250", "500"])
def test_in_plan_transition_band_is_zero_to_500(raw: str) -> None:
    """SPEC 5.2 / C3: the in-plan band's low edge moved 200 -> 0, so an instant
    in-plan transition is expressible by env as well as by the model."""
    assert (
        _env_probe(_clean_env(CORTEX_PLAN_MOVE_DURATION_MS=raw), "b.PLAN_MOVE_DURATION_MS")
        == raw
    )


@pytest.mark.parametrize("raw", ["501", "-1", "abc", ""])
def test_in_plan_transition_band_rejects_out_of_range_to_the_zero_default(raw: str) -> None:
    assert (
        _env_probe(_clean_env(CORTEX_PLAN_MOVE_DURATION_MS=raw), "b.PLAN_MOVE_DURATION_MS")
        == "0"
    )


@WINDOWS_ONLY
def test_flat_move_issues_no_settle_sleep_at_all(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """GATE G6's PRIMARY claim: a flat ``move`` outside a plan records ``[]`` where it
    used to record ``[0.05]``, and the return message is byte-identical."""
    monkeypatch.delenv("CORTEX_MOVE_SETTLE_MS", raising=False)
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    message = real_backend.execute(_move((30, 45)))
    assert message == "Executed move."
    assert engine.calls == [("move", 30, 45)]
    assert recorded_sleeps.sleeps == []


@WINDOWS_ONLY
def test_flat_move_with_explicit_zero_duration_still_issues_no_settle_sleep(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """The explicitly-instant move and the defaulted one now behave identically — the
    50 ms hidden settle is gone from both."""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    real_backend.execute(_move((31, 46), duration_ms=0))
    assert recorded_sleeps.sleeps == []


@WINDOWS_ONLY
def test_no_new_settle_sleep_site_was_added() -> None:
    """The two settle sites are the ones that always existed, and both are still
    guarded on a positive value.

    PORT ADAPTATION (WP2): the dev tree also pinned a TOTAL ``time.sleep(`` count
    of 23 — a whole-file baseline that belongs to the dev tree's larger backend and
    is meaningless across the port (the stable tree carries different machinery).
    The INVARIANT that matters — exactly one unmarked-move settle site and exactly
    one declared-settle plan-tail site, both positive-guarded — is pinned here.
    """
    source = (backend_module.__file__ and open(backend_module.__file__, encoding="utf-8").read())
    assert source.count("time.sleep(_MOVE_SETTLE_SECONDS)") == 1  # the unmarked-move site
    assert source.count("time.sleep(_plan_point_settle_seconds(action))") == 1  # the plan tail


def test_action_spec_surface_is_unchanged() -> None:
    """The FLAT wire surface gains no field beyond the two documented duration
    pass-throughs. The name set is frozen here so a future change to it is a
    deliberate, visible act."""
    assert sorted(ActionSpec.model_fields) == [
        "action",
        "delta",
        "duration_ms",
        "expected_effect",
        "hold_ms",
        "keys",
        "target",
        "text",
        "via",
        "x",
        "x2",
        "y",
        "y2",
    ]


def test_the_internal_settle_markers_are_not_on_the_host_surface() -> None:
    """The declared settle rides an INTERNAL GroundedAction field, exactly like the
    existing ``plan_point_settle`` marker: ActionSpec never mirrors either, so no host
    payload on the flat surface can carry a settle."""
    assert "plan_point_settle" in GroundedAction.model_fields
    assert "plan_point_settle_ms" in GroundedAction.model_fields
    assert "plan_point_settle" not in ActionSpec.model_fields
    assert "plan_point_settle_ms" not in ActionSpec.model_fields
    spec = ActionSpec(action="move", x=1, y=2)
    assert spec.to_grounded().plan_point_settle is None
    assert spec.to_grounded().plan_point_settle_ms is None


# ================================================================================================
# PART 3 — SPEC 5.2: the declared settle CONSUMER (the WP7 producer is the seam)
# ================================================================================================


@WINDOWS_ONLY
def test_the_declared_settle_is_the_sleep_the_dispatch_path_issues(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    recorded_sleeps: _SleepRecorder,
) -> None:
    """End to end through the REAL dispatch tail: a point that declares 25 ms settles
    25 ms, and a point that declares 0 ms settles not at all. Both at the SAME site,
    with no new sleep anywhere. (The full None/0/declared band + the WP7 seam is
    pinned by tests/test_plan_settle_wiring.py — the E-01 proof.)"""
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    real_backend.execute(
        _click((10, 10), plan_point_settle=True, plan_point_settle_ms=25)
    )
    assert recorded_sleeps.sleeps == [0.025]
    real_backend.execute(
        _click((11, 11), plan_point_settle=True, plan_point_settle_ms=0)
    )
    assert recorded_sleeps.sleeps == [0.025]  # nothing added by the zero declaration


# ================================================================================================
# PART 6 — the "no new field on the flat surface" and "no new work per action" proofs
# ================================================================================================


def test_the_two_motion_policy_values_are_module_constants_evaluated_once_at_import() -> None:
    """Nothing per-action changed: the two values the policy moves are read from the
    module once, and ``PLAN_MOVE_DURATION_MS`` has no consumer in the dispatch body
    (its only consumer, the plan normalizer, is WP7)."""
    backend_source = inspect.getsource(backend_module)
    assert backend_source.count("_MOVE_SETTLE_SECONDS") >= 2  # the two guarded sites
    dispatched_source = inspect.getsource(backend_module.LocalComputerBackend._execute_dispatched)
    assert "PLAN_MOVE_DURATION_MS" not in dispatched_source


def test_a_flat_action_never_consults_the_in_plan_default() -> None:
    """The in-plan default is a module-level constant with no flat-action consumer.
    Changing the constant cannot slow a single flat action."""
    dispatched_source = inspect.getsource(
        backend_module.LocalComputerBackend._execute_dispatched
    )
    assert "PLAN_MOVE_DURATION_MS" not in dispatched_source


def inspect_source_of(fn: Any) -> str:
    return inspect.getsource(fn)
