"""Batch-4 engine-node regressions (WP4b port of the dev tree's
test_batch4_local_remediation.py — ONLY the nodes that need no plans, no waits, no
leases, and no evidence machinery; those are WP7/WP8 surface tests and the plan
surface does not exist in the stable tree yet).

Ported here:
- B4-2: a ``model_construct``-bypassed GroundedAction is rejected by the backend
  DEFENSE gate BEFORE any engine call (real backend and fake parity);
- B4-5: every numeric env helper falls back to its default on non-finite input, and
  the motion import keys survive an overflowing env without an import crash.

Deferred (owning WP recorded in wp4b-notes.md):
- B4-1 (plan numeric wire strictness), B4-2 unknown-plan-keys/unknown-action-name,
  B4-8 (limit cutoff / cancellation prefix truth), B4-9 (plan trace lifecycle) —
  the waypoint-plan boundary is WP7/WP8.
- B4-5 ``test_b4_5_no_new_cap_is_added_to_finite_signed_values`` is superseded by the
  stable WP2 band decision: ``MOVE_BEND_PX`` is deliberately band-bounded
  (``_env_bounded_int(..., -5000, 5000)``), pinned in test_batch5_motion_nodes.py —
  the dev copy's uncapped ``_env_int`` reading is not this tree's contract.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from recording_engine import RecordingEngine

import computer_use_mcp.backend as backend_module
from computer_use_mcp.backend import LocalComputerBackend
from computer_use_mcp.models import ActionType, GroundedAction, Point

WINDOWS_ONLY = pytest.mark.skipif(not backend_module.IS_WINDOWS, reason="requires Windows")


# --- B4-2: model_construct misuse stops BEFORE any engine call -------------------------------


@pytest.mark.parametrize(
    ("field", "value", "action", "message"),
    [
        ("hold_ms", "250", ActionType.CLICK, "hold_ms"),
        ("hold_ms", 5_001, ActionType.CLICK, "hold_ms"),
        ("hold_ms", 1, ActionType.DOUBLE_CLICK, "hold_ms"),
        ("duration_ms", 200, ActionType.CLICK, "duration_ms"),
        ("duration_ms", 5_001, ActionType.MOVE, "duration_ms"),
        ("delta", True, ActionType.SCROLL, "delta"),
        ("plan_point_settle", "yes", ActionType.CLICK, "plan_point_settle"),
    ],
)
@WINDOWS_ONLY
def test_b4_2_model_construct_misuse_stops_before_engine_calls(
    real_backend: LocalComputerBackend,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
    action: ActionType,
    message: str,
) -> None:
    engine = RecordingEngine()
    monkeypatch.setattr(real_backend, "_engine", engine)
    monkeypatch.setattr(real_backend, "_active_context", None)
    kwargs: dict[str, Any] = {
        "action": action,
        "point": Point(x=30, y=45),
        "confidence": 1.0,
        field: value,
    }
    bypassed = GroundedAction.model_construct(**kwargs)
    with pytest.raises(ValueError, match=message):
        real_backend.execute(bypassed)
    assert engine.calls == []
    assert engine.down is False


def test_b4_2_fake_backend_applies_model_construct_defense_before_recording() -> None:
    backend = backend_module.FakeComputerBackend()
    bypassed = GroundedAction.model_construct(
        action=ActionType.CLICK,
        point=Point(x=1, y=2),
        hold_ms="250",
        confidence=1.0,
    )
    with pytest.raises(ValueError, match="hold_ms"):
        backend.execute(bypassed)
    assert backend.executed == []


# --- B4-5: env helpers never crash on non-finite input ---------------------------------------


@pytest.mark.parametrize("raw", ["1e400", "9" * 400, "NaN", "Infinity", "-Infinity"])
def test_b4_5_every_numeric_env_helper_falls_back_on_nonfinite(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("CORTEX_BATCH4_NUMBER", raw)
    assert backend_module._env_nonnegative_float("CORTEX_BATCH4_NUMBER", 5.0) == 5.0
    assert backend_module._env_positive_int("CORTEX_BATCH4_NUMBER", 7) == 7
    assert backend_module._env_int_in_range("CORTEX_BATCH4_NUMBER", 7, 1, 9) == 7
    assert backend_module._env_nonnegative_int("CORTEX_BATCH4_NUMBER", 7) == 7
    assert backend_module._env_int("CORTEX_BATCH4_NUMBER", 7) == 7


def _run_env_probe(
    value: str, expression: str, *, key: str = "CORTEX_MOVE_BEND_PX"
) -> subprocess.CompletedProcess[str]:
    from types import SimpleNamespace

    from fresh_backend_import import fresh_backend

    try:
        import traceback

        module = fresh_backend({key: value})
        stdout = str(eval(expression, {"b": module}))  # noqa: S307 - test-authored expression
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    except Exception as exc:  # surfaced exactly like the former child's non-zero exit
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
        )


@pytest.mark.parametrize("value", ["1e400", "9" * 400, "NaN", "Infinity", "-Infinity", "abc"])
def test_b4_5_motion_env_overflow_and_nonfinite_fall_back_without_import_crash(
    value: str,
) -> None:
    completed = _run_env_probe(value, "b.MOVE_BEND_PX")
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "0"


@pytest.mark.parametrize(
    ("key", "expression", "expected"),
    [
        ("CORTEX_MOVE_BEND_PX", "b.MOVE_BEND_PX", "0"),
        # SPEC 5.2 (restated): the in-plan transition's overflow fallback is its
        # default, 0; the settle's is its default, 0.0.
        ("CORTEX_PLAN_MOVE_DURATION_MS", "b.PLAN_MOVE_DURATION_MS", "0"),
        ("CORTEX_MOVE_DURATION_MS", "b.MOVE_DURATION_MS", "0"),
        ("CORTEX_PLAN_SAMPLE_INTERVAL_MS", "b.PLAN_SAMPLE_INTERVAL_MS", "12"),
        ("CORTEX_MOVE_SETTLE_MS", "b.MOVE_SETTLE_MS", "0.0"),
    ],
)
def test_b4_5_each_motion_import_key_survives_overflow(
    key: str, expression: str, expected: str
) -> None:
    """An infinite/overflowing env value falls back to the DEFAULT and never raises."""
    completed = _run_env_probe("1e400", expression, key=key)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == expected


def test_b4_5_settle_infinity_falls_back_before_dispatch_tail() -> None:
    completed = _run_env_probe(
        "1e400",
        "(b.MOVE_SETTLE_MS, b._MOVE_SETTLE_SECONDS)",
        key="CORTEX_MOVE_SETTLE_MS",
    )
    assert completed.returncode == 0, completed.stderr
    # RESTATED by ORVEX-2026-102 §5.1 (SPEC 5.1, conflict C3): an infinite/overflowing
    # settle env value still falls back to the DEFAULT — and the default is now 0.0 ms,
    # not 50.0.  The rule the test exists for ("never raises, resolves to the default")
    # is unchanged; only the default it resolves to moved.
    assert completed.stdout.strip() == "(0.0, 0.0)"
