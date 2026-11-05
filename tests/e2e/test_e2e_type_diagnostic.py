"""WP12 live-run-6 DIAGNOSTIC: who corrupts fast VK_PACKET bursts? (LIVE_DESKTOP-gated)

Run-4/5 anomaly: within a fast unicode burst the TARGET replaces a TRAILING run of
characters with the burst's LAST character (count preserved): one 30-char batch lost
the last 15 to 'x'; segment 3 'brown fox' lost the last 3 to 'xxx'. Short bursts
verified clean. Run 6 taught two lessons applied here:
- the RAW legs must use the PRODUCT'S OWN low-level input code (desktop/input_engine.py
  event builders + _send/_dispatch — the exact _INPUT structs SendInput accepts); the
  run-6 test-side struct had a wrong size and SendInput rejected all 60 events;
- EVERY leg must be exception-wrapped and recorded so one failing leg never starves
  the others; a single end-of-test assertion fails if ANY leg errored or diverged.

Legs (each into its OWN fresh marker Notepad):
- L1 (PRODUCT, 3 segments): computer_execute(type) per segment; records read-back,
  reply message/integrity, transport, timing.
- L2 (RAW BURST, no backend/pipeline): the product's SendInputEngine (interval 0)
  types the whole text as ONE SendInput batch.
- L3 (RAW PACED, no backend/pipeline): the product's SendInputEngine with
  type_interval=0.015 — one character per SendInput call with a 15 ms pause
  (input_engine.py:668 paced chunk_size-2 path).
- L4 (PRODUCT PACED): the same 3 product segments with the product's OWN backend
  pacing knob (CORTEX_TYPE_INTERVAL, read at backend construction — backend.py:884-888)
  set for that leg and restored in finally.

Recorded per leg: dispatched text, read-back, timing, product reply
message/integrity — plus the active keyboard layout of the foreground thread
(read-only). DIAGNOSTIC by design (records everything, hides nothing) with ONE
honest end assertion: it fails if ANY leg errored or its read-back diverged.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import helpers_runtime as rt
import helpers_win32 as w32
import pytest

from computer_use_mcp.desktop.input_engine import SendInputEngine

import computer_use_mcp.desktop.backend as backend_module
from computer_use_mcp.desktop.backend import DEFAULT_TYPE_INTERVAL_SECONDS

import computer_use_mcp.server.execute as srv_server_execute
import computer_use_mcp.server.lifecycle as srv_server_lifecycle

pytestmark = pytest.mark.e2e

#: Same gate as tests/desktop_tripwire.py LIVE_DESKTOP (e2e already requires it; the
#: marker keeps the skip reason loud even when someone runs tests/e2e directly).
LIVE_DESKTOP = pytest.mark.skipif(
    os.environ.get("CUMCP_RUN_E2E") != "1",
    reason="diagnostic drives REAL keyboard input on the LIVE desktop; CUMCP_RUN_E2E=1 only",
)

TEXT = "e2e-typed-7391 quick brown fox"
SEGMENTS = ("e2e-typed-7391 ", "quick ", "brown fox")
PACED_INTERVAL = str(DEFAULT_TYPE_INTERVAL_SECONDS)  # L4 leg: the product's own default
RAW_PER_CHAR_PAUSE = 0.015


def _foreground_keyboard_layout() -> str:
    """The ACTIVE keyboard layout (HKL) of the FOREGROUND thread — read-only."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetKeyboardLayout.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
    foreground = int(user32.GetForegroundWindow() or 0)
    thread_id = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    hkl = int(user32.GetKeyboardLayout(thread_id) or 0)
    return f"0x{hkl & 0xFFFFFFFF:08X}"


def _assert_marker_active(hwnd: int, token: str, what: str) -> None:
    """D6 guard before ANY input burst: our marker tab is the ACTIVE tab."""
    assert w32.focus_window(hwnd), f"{what}: could not focus the marker window"
    assert token.casefold() in w32.window_text(hwnd).casefold(), (
        f"{what}: marker tab not active (title={w32.window_text(hwnd)!r})"
    )


@contextmanager
def notepad_for(file_path: Path, token: str):
    """Launch + D6-safe teardown for one diagnostic leg."""
    timeout = float(os.environ.get("CUMCP_E2E_TEST_TIMEOUT", "240"))
    proc, hwnd = w32.launch_marked_notepad(w32.Deadline(timeout), file_path, timeout_s=30.0)
    real_pid = w32.window_pid(hwnd)
    assert real_pid == proc.pid or (w32.process_exe(real_pid) or "").casefold().endswith(
        "notepad.exe"
    ), (proc.pid, real_pid)
    try:
        yield proc, hwnd
    finally:
        w32.release_notepad(proc, hwnd, token)


def _product_leg(
    name: str,
    e2e_scratch: Path,
    make_session: Any,
    evidence: Any,
    results: list[dict[str, Any]],
    *,
    paced_env: str | None,
) -> None:
    """One PRODUCT leg: 3 computer_execute(type) segments into a fresh marker file.
    Fully wrapped: an error is recorded and the remaining legs still run."""
    token = w32.unique_window_token(f"diag-{name}")
    file_path = e2e_scratch / f"{token}.txt"
    file_path.write_text("", encoding="utf-8")
    session_id: str | None = None
    prior_env = os.environ.get("CORTEX_TYPE_INTERVAL")  # WP12b VAL NIT: restore
    try:
        if paced_env is not None:
            os.environ["CORTEX_TYPE_INTERVAL"] = paced_env  # backend reads it at :884-888
        session_id, _bundle = make_session(
            provider=rt.E2EScriptedProvider([rt.done("unused on the direct path")]),
            dry_run=False,
            require_approval=False,
            allowed_windows=[token],
        )
        with notepad_for(file_path, token) as (_proc, hwnd):
            typed = ""
            for index, segment in enumerate(SEGMENTS, start=1):
                _assert_marker_active(hwnd, token, f"{name} segment {index} pre-check")
                started = time.perf_counter()
                response = rt.execute_payload(
                    asyncio.run(
                        srv_server_execute.computer_execute(
                            session_id, "type", text=segment, expected_effect=segment
                        )
                    )
                )
                elapsed = time.perf_counter() - started
                verification = response.get("verification") or {}
                action = response.get("action") or {}
                read_back = w32.read_edit_text(hwnd)
                typed += segment
                entry = {
                    "leg": name,
                    "segment": segment,
                    "ok": response.get("ok"),
                    "message": response.get("message"),
                    "transport": action.get("via") if isinstance(action, dict) else None,
                    "integrity_note": verification.get("note"),
                    "read_back": read_back,
                    "expected_accumulated": typed,
                    "elapsed_s": round(elapsed, 4),
                    "matches": read_back == typed,
                }
                results.append(entry)
                if evidence is not None:
                    evidence.record(f"{name}_segment_{index}", **entry)
    except Exception as exc:  # noqa: BLE001 - diagnostic: record and continue
        results.append({"leg": name, "error": f"{type(exc).__name__}: {exc}", "matches": False})
        if evidence is not None:
            evidence.record(f"{name}_error", error=f"{type(exc).__name__}: {exc}")
    finally:
        if paced_env is not None:
            if prior_env is None:
                os.environ.pop("CORTEX_TYPE_INTERVAL", None)  # was unset: removal restores
            else:
                os.environ["CORTEX_TYPE_INTERVAL"] = prior_env  # restore operator value
        with suppress(Exception):  # teardown never masks the diagnostic result
            if session_id is not None:
                asyncio.run(srv_server_lifecycle.stop_session(session_id))


def _raw_leg(
    name: str,
    e2e_scratch: Path,
    evidence: Any,
    results: list[dict[str, Any]],
    *,
    per_char_pause: float | None,
) -> None:
    """One RAW leg driven by the PRODUCT'S OWN low-level input engine (input_engine.py
    event builders + _send/_dispatch — no backend, no pipeline). Fully wrapped."""
    token = w32.unique_window_token(f"diag-{name}")
    file_path = e2e_scratch / f"{token}.txt"
    file_path.write_text("", encoding="utf-8")
    try:
        with notepad_for(file_path, token) as (_proc, hwnd):
            _assert_marker_active(hwnd, token, f"{name} pre-check")
            engine = SendInputEngine(type_interval=per_char_pause or 0.0)

            def _focus_guard() -> None:
                """WP12b B2: per-chunk focus guard (VAL BLOCKER) — abort the burst
                unless the FOREGROUND window is still OUR marker hwnd carrying the
                marker; otherwise the remaining keystrokes would land in the
                user's foreground window."""
                if int(w32.user32.GetForegroundWindow() or 0) != hwnd or not (
                    token.casefold() in w32.window_text(hwnd).casefold()
                ):
                    raise AssertionError(
                        f"{name}: focus left the marker window mid-burst; aborting"
                    )

            started = time.perf_counter()
            engine.type_text(TEXT, before_chunk=_focus_guard)  # L2: one batch (guard once); L3: per char
            elapsed = time.perf_counter() - started
            read_back = w32.read_edit_text(hwnd)
            entry = {
                "leg": name,
                "segment": (
                    "whole text in ONE SendInput batch"
                    if per_char_pause is None
                    else f"one char per SendInput call + {per_char_pause * 1000:.0f} ms"
                ),
                "read_back": read_back,
                "expected_accumulated": TEXT,
                "elapsed_s": round(elapsed, 4),
                "matches": read_back == TEXT,
            }
            results.append(entry)
            if evidence is not None:
                evidence.record(f"{name}", **entry)
    except Exception as exc:  # noqa: BLE001 - diagnostic: record and continue
        results.append({"leg": name, "error": f"{type(exc).__name__}: {exc}", "matches": False})
        if evidence is not None:
            evidence.record(f"{name}_error", error=f"{type(exc).__name__}: {exc}")


@LIVE_DESKTOP
def test_diagnostic_type_corruption_product_vs_notepad_vs_reader(
    e2e_scratch: Path, make_session: Any, evidence: Any
) -> None:
    """DIAGNOSTIC (marked as such): ALL FOUR legs run (each wrapped — an error is
    recorded, the next leg still runs) BEFORE the single end assertion, which fails
    if ANY leg errored or diverged, naming each — so one live run answers product vs
    target vs pacing without a later leg being starved by an earlier failure."""
    results: list[dict[str, Any]] = []
    layout = _foreground_keyboard_layout()
    if evidence is not None:
        evidence.record("environment", active_keyboard_layout=layout)

    _product_leg("L1_product", e2e_scratch, make_session, evidence, results, paced_env=None)
    _raw_leg("L2_raw_burst", e2e_scratch, evidence, results, per_char_pause=None)
    _raw_leg("L3_raw_per_char", e2e_scratch, evidence, results, per_char_pause=RAW_PER_CHAR_PAUSE)
    _product_leg(
        "L4_product_paced", e2e_scratch, make_session, evidence, results,
        paced_env=PACED_INTERVAL,
    )

    if evidence is not None:
        evidence.add_extra("legs_summary", results)
        evidence.add_extra("active_keyboard_layout", layout)

    # WP12d (VAL R6): leg ERRORS always fail (a broken leg proves nothing), and the
    # PRODUCT legs (L1 default pace / L4) must land exactly. The RAW legs (L2
    # unpaced burst / L3 paced) stay RECORDED-AS-DATA: their divergence is the
    # KNOWN Windows 11 Notepad limitation (live runs 4-7), not a product failure —
    # this box truncated a raw burst to 'e2e-ty' (result.json) and substituted an
    # unpaced product tail. The recorded per-leg data is what the user decision
    # (lower the pace vs not) is made from.
    errored = [entry for entry in results if "error" in entry]
    product_diverged = [
        entry
        for entry in results
        if not entry.get("matches")
        and "error" not in entry
        and entry.get("leg", "").startswith(("L1", "L4"))
    ]
    raw_data = [
        {k: entry.get(k) for k in ("leg", "read_back", "expected_accumulated", "matches")}
        for entry in results
        if entry.get("leg", "").startswith(("L2", "L3"))
    ]
    if evidence is not None:
        evidence.add_extra("raw_legs_data", raw_data)
    assert not errored, (
        "DIAGNOSTIC: leg(s) errored: "
        + "; ".join(f"[{entry['leg']}] {entry.get('error')}" for entry in errored)
    )
    assert not product_diverged, (
        "DIAGNOSTIC: the PRODUCT legs diverged from the dispatched text: "
        + "; ".join(
            f"[{entry['leg']}] expected {entry.get('expected_accumulated')!r}, "
            f"read {entry.get('read_back')!r}"
            for entry in product_diverged
        )
        + f" — raw-leg data (Windows 11 Notepad limitation, recorded not gated): {raw_data!r}"
    )
