"""WP12a pace CALIBRATION (LIVE_DESKTOP-gated, diagnostic-marked).

The WP12a default pace is 0.02 s per character (the Windows 11 packaged Notepad
corruption mitigation, user ruling 2026-11-02). This test gives the Commander the
data to tell the user whether 20 ms can be LOWERED later: it types the marker text
through the PRODUCT at intervals 0.005 / 0.01 / 0.015 / 0.02 s, three trials each,
every trial into its OWN fresh marker Notepad (D6: ``launch_marked_notepad`` +
tabs-aware ``release_notepad``, marker-tab-active guard before typing, token
allowlist per session).

The 0.02 s trials deliberately run on the PRODUCT DEFAULT (no env override) so the
gate proves the shipped default itself; the other intervals set
``CORTEX_TYPE_INTERVAL`` around their session construction (restored in finally).

Every trial is recorded (interval, trial, elapsed, read-back, integrity message,
exact flag). The gate fails ONLY at the end, and ONLY if the DEFAULT interval had
any non-exact trial — lower-interval results are pure calibration data for the user
decision, never a failure.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import helpers_runtime as rt
import helpers_win32 as w32
import pytest

from computer_use_mcp.desktop.backend import DEFAULT_TYPE_INTERVAL_SECONDS

import computer_use_mcp.server.execute as srv_server_execute
import computer_use_mcp.server.lifecycle as srv_server_lifecycle

pytestmark = pytest.mark.e2e

LIVE_DESKTOP = pytest.mark.skipif(
    os.environ.get("CUMCP_RUN_E2E") != "1",
    reason="calibration drives REAL keyboard input on the LIVE desktop; CUMCP_RUN_E2E=1 only",
)

TEXT = "e2e-typed-7391 quick brown fox"
INTERVALS = (0.005, 0.01, 0.015, 0.02)
TRIALS = 3
DEFAULT_INTERVAL = DEFAULT_TYPE_INTERVAL_SECONDS  # the shipped default, from code
PACE_ENV = "CORTEX_TYPE_INTERVAL"


def _calibrate_trial(
    interval: float,
    trial: int,
    e2e_scratch: Path,
    make_session: Any,
    evidence: Any,
) -> dict[str, Any]:
    """One typing trial at ``interval``; returns the recorded entry."""
    token = w32.unique_window_token(f"cal-{int(interval * 1000):03d}-{trial:02d}")
    file_path = e2e_scratch / f"{token}.txt"
    file_path.write_text("", encoding="utf-8")
    paced_env_set = interval != DEFAULT_INTERVAL  # the default leg proves the default
    session_id: str | None = None
    entry: dict[str, Any] = {"interval": interval, "trial": trial}
    prior_env = os.environ.get(PACE_ENV)  # WP12b VAL NIT: RESTORE, never just pop
    try:
        if paced_env_set:
            os.environ[PACE_ENV] = str(interval)
        session_id, _bundle = make_session(
            provider=rt.E2EScriptedProvider([rt.done("unused on the direct path")]),
            dry_run=False,
            require_approval=False,
            allowed_windows=[token],
        )
        proc, hwnd = w32.launch_marked_notepad(
            w32.Deadline(float(os.environ.get("CUMCP_E2E_TEST_TIMEOUT", "240"))),
            file_path,
            timeout_s=30.0,
        )
        try:
            assert w32.focus_window(hwnd), f"could not focus the marker window ({entry})"
            assert token.casefold() in w32.window_text(hwnd).casefold(), w32.window_text(hwnd)
            started = time.perf_counter()
            response = rt.execute_payload(
                asyncio.run(
                    srv_server_execute.computer_execute(
                        session_id, "type", text=TEXT, expected_effect=TEXT
                    )
                )
            )
            elapsed = time.perf_counter() - started
            entry.update(
                {
                    "elapsed_s": round(elapsed, 4),
                    "ok": response.get("ok"),
                    "message": response.get("message"),
                    "read_back": w32.read_edit_text(hwnd),
                    "expected": TEXT,
                    "via_default": not paced_env_set,
                }
            )
            entry["exact"] = entry["read_back"] == TEXT
        finally:
            w32.release_notepad(proc, hwnd, token)
    except Exception as exc:  # noqa: BLE001 - calibration records failures, never hides
        entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["exact"] = False
    finally:
        if paced_env_set:
            if prior_env is None:
                os.environ.pop(PACE_ENV, None)  # was unset: removal IS the restore
            else:
                os.environ[PACE_ENV] = prior_env  # restore the operator's value
        with suppress(Exception):
            if session_id is not None:
                asyncio.run(srv_server_lifecycle.stop_session(session_id))
    if evidence is not None:
        evidence.record(f"trial_i{int(interval * 1000):03d}_t{trial:02d}", **entry)
    return entry


@LIVE_DESKTOP
def test_pace_calibration_matrix(
    e2e_scratch: Path, make_session: Any, evidence: Any
) -> None:
    """Calibration matrix: 4 intervals x 3 trials, everything recorded; the gate
    fails only if the SHIPPED DEFAULT interval (0.02 s) ever missed."""
    results: list[dict[str, Any]] = []
    for interval in INTERVALS:
        for trial in range(1, TRIALS + 1):
            results.append(
                _calibrate_trial(interval, trial, e2e_scratch, make_session, evidence)
            )
    if evidence is not None:
        evidence.add_extra("calibration_matrix", results)
        evidence.add_extra(
            "summary",
            {
                f"{interval}s": {
                    "exact": sum(
                        1 for entry in results if entry["interval"] == interval and entry["exact"]
                    ),
                    "trials": TRIALS,
                }
                for interval in INTERVALS
            },
        )
    default_misses = [
        entry
        for entry in results
        if entry["interval"] == DEFAULT_INTERVAL and not entry.get("exact")
    ]
    assert not default_misses, (
        "PACE CALIBRATION: the SHIPPED DEFAULT interval (0.02 s/char) had "
        f"{len(default_misses)} non-exact trial(s): {default_misses!r} — the default "
        "must type exactly on this box; lower intervals are calibration data only."
    )
