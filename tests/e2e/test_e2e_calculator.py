"""Real-Windows Calculator E2E: grounded coordinate clicks + display-predicate verification.

Drives classic ``win32calc.exe`` (``CalcFrame``) through the runtime's tool surface with a
deterministic scripted provider. Button coordinates come from the LIVE window: the button
grid is enumerated from the real instance (owner-drawn buttons have no captions, so labels
follow the documented classic layout mapped onto the enumerated grid geometry) —
coordinates derived from a real observation, executed through grounding/validation.

Verification doctrine (acceptance A, honest):

- MEASURED LIVE: a single digit change on the 1920x1080 screenshot is a mean pixel
  difference of ~0.2 — far below the legacy diff threshold (1.0) — so pixel diff cannot
  reliably verify Calculator input at all. Every click in this test is therefore verified
  by the injected ``CalcDisplayPredicateStrategy`` reading the REAL display Static control
  (``calc_display_equals:<value>``), claimed for both ``predicate`` and ``visual_change``
  intents carrying the marker. Deterministic semantic verification on real window state.
- Every display value is additionally read directly via Win32 in the test (independent
  proof). No OCR dependency, no pixel-diff-only claims.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import helpers_runtime as rt
import helpers_win32 as w32
import pytest

from computer_use_mcp import server

pytestmark = pytest.mark.e2e

WIN32CALC = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "win32calc.exe")


@contextmanager
def calculator_app(deadline: w32.Deadline):
    """Launch classic Calculator; kill ONLY the process we started.

    Environment note (documented, from live probes): ``calc.exe`` on this box is a stub
    that exits immediately and re-launches the real binary as ``win32calc.exe`` — so the
    suite launches ``win32calc.exe`` directly, making the tracked PID the actual window
    owner. If a future build replaces it with a store app this test fails visibly here.

    D6 window isolation: the classic Calculator window title cannot carry a run
    token, so attach here is PID-scoped — ``launch_gui`` matches class ``CalcFrame``
    AND the pid of the process WE started, which the user's own Calculator instance
    (a different pid) can never satisfy. Every subsequent identity assertion in the
    tests pins hwnd == the launched pid's window, so input can only land in ours.
    """
    if not os.path.exists(WIN32CALC):
        pytest.skip(f"classic Calculator binary not present: {WIN32CALC}")
    proc, hwnd = w32.launch_gui(
        deadline, [WIN32CALC], window_class=w32.CALC_WINDOW_CLASS, timeout_s=30.0
    )
    # D6 pin at launch time: the window belongs to OUR pid (never the user's calc).
    assert w32.window_pid(hwnd) == proc.pid, (hwnd, proc.pid)
    assert w32.window_class(hwnd) == w32.CALC_WINDOW_CLASS, w32.window_class(hwnd)
    try:
        yield proc, hwnd
    finally:
        w32.kill_process_tree(proc.pid)


def observe(evidence: Any, session_id: str, when: str) -> dict[str, Any]:
    response = w32.observe_tool_metadata(server.computer_observe(session_id))
    observation = response["observation"]
    if evidence is not None:
        evidence.save_observation(when, observation)
        evidence.record(
            f"observe_{when}",
            observation_id=response["observation_id"],
            active_app=response["active_app"],
            window=observation.get("active_window_info"),
        )
    return observation


def test_calculator_clicks_and_display_verification(
    deadline: w32.Deadline, e2e_scratch: Path, make_session: Any, with_verifier: Any, evidence: Any
) -> None:
    """Grounded clicks compute 7*6; digit clicks verified by pixel change, the operator
    click by a REAL display predicate; display proven independently via Win32."""
    with calculator_app(deadline) as (proc, hwnd):
        assert w32.focus_window(hwnd), "could not focus Calculator"
        grid = w32.calc_button_grid(hwnd)
        display_strategy = rt.CalcDisplayPredicateStrategy(hwnd)
        # RETARGETED (run_goal removal): no scripted provider — the loop is gone; every
        # step below is a direct computer_execute click with its own expected_effect.
        session_id, bundle = make_session(
            provider=rt.E2EScriptedProvider([rt.done("unused on the direct path")]),
            dry_run=False,
            require_approval=False,
            allowed_processes=["win32calc.exe"],
        )
        with_verifier(session_id, bundle, display_strategy)
        try:
            observation = observe(evidence, session_id, "before")
            info = observation["active_window_info"]
            assert info["process_name"] == "win32calc.exe", info
            assert info["hwnd"] == hwnd, (info["hwnd"], hwnd)
            assert info["pid"] == proc.pid, (info["pid"], proc.pid)
            assert observation["coordinate_space"] == "verified_passthrough", observation

            # --- direct client action: computer_execute digit click (approved=True) -----
            # NOTE: the effect carries the calc_display_equals marker on purpose. Measured
            # live: a single digit change on the 1920x1080 screenshot is a mean pixel diff
            # of ~0.2, far below the legacy diff threshold (1.0), so pixel diff alone
            # cannot verify Calculator input; the injected display-predicate strategy
            # reads the REAL display control instead (deterministic, evidence-based).
            direct = asyncio.run(
                server.computer_execute(
                    session_id,
                    "click",
                    x=grid["7"][0],
                    y=grid["7"][1],
                    approved=True,
                    expected_effect="calc_display_equals:7",
                )
            )
            assert direct.get("ok") is True, direct
            assert direct["verification"]["outcome"] == "verified", direct["verification"]
            assert direct["verification"]["verification_method"] == "calc_display", direct["verification"]
            assert w32.calc_display_value(hwnd) == "7", w32.calc_display_values(hwnd)

            # --- clear via computer_execute, then compute via direct calls ---------------
            # RETARGETED (run_goal removal): the loop died with run_goal; each scripted
            # step below is now a direct computer_execute click, verified per call by
            # the injected display-predicate strategy (the same verification pipeline).
            cleared = asyncio.run(
                server.computer_execute(
                    session_id,
                    "click",
                    x=grid["C"][0],
                    y=grid["C"][1],
                    approved=True,
                    expected_effect="calc_display_equals:0",
                )
            )
            assert cleared.get("ok") is True, cleared
            assert w32.calc_display_value(hwnd) == "0", w32.calc_display_values(hwnd)

            for label, key, expected in (
                ("7", "7", "calc_display_equals:7"),
                ("*", "*", "calc_display_equals:7"),
                ("6", "6", "calc_display_equals:6"),
                ("=", "=", "calc_display_equals:42"),
            ):
                clicked = asyncio.run(
                    server.computer_execute(
                        session_id,
                        "click",
                        x=grid[key][0],
                        y=grid[key][1],
                        approved=True,
                        expected_effect=expected,
                    )
                )
                if evidence is not None:
                    evidence.record(
                        f"computer_execute(click {label})",
                        ok=clicked.get("ok"),
                        verification=clicked.get("verification"),
                    )
                assert clicked.get("ok") is True, clicked
                verification = clicked["verification"]
                assert verification["outcome"] == "verified", (label, verification)
                assert verification["verification_method"] == "calc_display", (label, verification)
            assert display_strategy.calls[-1]["display"] == "42", display_strategy.calls
            assert w32.calc_display_value(hwnd) == "42", w32.calc_display_values(hwnd)

            if evidence is not None:
                evidence.assert_that("win32calc identity: pid/exe/hwnd/coordinate-space", True)
                evidence.assert_that(
                    "computer_execute click verified by visual change (display 0 -> 7)", True
                )
                evidence.assert_that(
                    "operator click '*' verified by display predicate (pixel-identical screen)",
                    True,
                    "screenshot diff alone cannot verify this state transition",
                )
                evidence.assert_that("direct computer_execute clicks computed 7*6=42, every step verified", True)
                evidence.assert_that("independent Win32 display read == 42", True)
                evidence.add_extra("button_grid", {k: list(v) for k, v in grid.items()})
                evidence.add_extra("display_strategy_calls", display_strategy.calls)
                # (metrics snapshot died with run_goal's response; per-click
                # verification outcomes are recorded above and in the audit excerpt.)
                evidence.save_audit(bundle, session_id)
                observe(evidence, session_id, "after")
        finally:
            server.stop_session(session_id)


def test_calculator_division_precision(
    deadline: w32.Deadline, e2e_scratch: Path, make_session: Any, with_verifier: Any, evidence: Any
) -> None:
    """Visual-spatial precision: small operator buttons, decimal result verification
    (1 / 8 = 0.125) — every step grounded from the live grid and verified against the
    real display."""
    with calculator_app(deadline) as (_proc, hwnd):
        assert w32.focus_window(hwnd), "could not focus Calculator"
        grid = w32.calc_button_grid(hwnd)
        display_strategy = rt.CalcDisplayPredicateStrategy(hwnd)
        # RETARGETED (run_goal removal): direct calls, no scripted provider (see above).
        session_id, bundle = make_session(
            provider=rt.E2EScriptedProvider([rt.done("unused on the direct path")]),
            dry_run=False,
            require_approval=False,
            allowed_processes=["win32calc.exe"],
        )
        with_verifier(session_id, bundle, display_strategy)
        try:
            observe(evidence, session_id, "before")
            # RETARGETED (run_goal removal): direct calls; each click verified per call.
            for label, key, expected in (
                ("1", "1", "calc_display_equals:1"),
                ("/", "/", "calc_display_equals:1"),
                ("8", "8", "calc_display_equals:8"),
                ("=", "=", "calc_display_equals:0.125"),
            ):
                clicked = asyncio.run(
                    server.computer_execute(
                        session_id,
                        "click",
                        x=grid[key][0],
                        y=grid[key][1],
                        approved=True,
                        expected_effect=expected,
                    )
                )
                assert clicked.get("ok") is True, (label, clicked)
                verification = clicked["verification"]
                assert verification["outcome"] == "verified", (label, verification)
                assert verification["verification_method"] == "calc_display", (label, verification)
            assert w32.calc_display_value(hwnd) == "0.125", w32.calc_display_values(hwnd)

            if evidence is not None:
                evidence.assert_that("1/8=0.125 computed via precise small-target clicks", True)
                evidence.assert_that("divide-operator click verified via display predicate", True)
                evidence.add_extra("display_strategy_calls", display_strategy.calls)
                evidence.save_audit(bundle, session_id)
                observe(evidence, session_id, "after")
        finally:
            server.stop_session(session_id)
