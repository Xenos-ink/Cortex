"""WP6 (ORVEX-2026-108) — THE BLOCKING COMPLETION PATH: segments, the status
consumer, event-loop safety, and the WP-S idle integration.

The user's requirement (goal.md Addendum 2 follow-up, verbatim semantics):

* an agent that must wait before deciding can call ``computer_wait(duration)``,
  be blocked until the duration ends, and be TOLD when it is over — dev's
  fire-and-forget wait (S-02) had no completion signal at all;
* the waiting time counts as TOOL-RUNNING time, never idle time: a running wait
  holds the WP-S in-flight mark (``SessionContext.in_flight_call``), so the idle
  reaper can never reap a mid-wait session and the idle count starts only when
  the wait ENDS;
* the blocking never trips the MCP client's 60 s tool timeout: every call blocks
  at most one segment (``CORTEX_WAIT_SEGMENT_SECONDS``, default 45, band 1..55,
  fallback-not-clamp);
* the blocking never blocks the SERVER: the tool functions are async and await
  the worker's done event inside the executor, so any other tool on any session
  is served while a wait call is blocked, and cancel/stop wake a blocked caller
  in well under 1 s;
* a client-side cancellation of the blocking CALL only abandons the call — the
  wait keeps running and stays queryable.

Also pinned here, per the VAL-WP-S carry-over (same code): the entry sweep reaps
an expired session B from a tool call on session A; a raising tool body and an
``asyncio.CancelledError`` leave ``in_flight_count() == 0``; an unknown-id call
creates no bookkeeping; one failing reap never aborts the sweep; the reaper uses
the atomic claim fence so it can close OUTSIDE the server lock without racing a
session that just became busy.

All timing uses patched clocks, patched segment lengths (0.2-5 s), and real
event waits bounded well under a second — no long real sleeps.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import pytest
from test_controller_integration import FAST_LIMITS, make_session

import computer_use_mcp.server.lifecycle as srv_server_lifecycle
import computer_use_mcp.server.wait_machine as srv_server_wait_machine
import computer_use_mcp.server.wait as srv_server_wait
import computer_use_mcp.server.state as srv_server_state
import time as srv_time
import computer_use_mcp.server.observe as srv_server_observe
from computer_use_mcp.core.state import SessionRegistry
from reply_contract import validate_reply

TEST_SEGMENT_SECONDS = 0.2


@pytest.fixture
def wait_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh registries + short blocking segment, as in the w1 suite."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(srv_server_lifecycle, "_registry", SessionRegistry(max_sessions=16))
    monkeypatch.setattr(srv_server_wait_machine, "_WAITS_IN_FLIGHT", {})
    monkeypatch.setattr(srv_server_wait, "_WAITS_IN_FLIGHT", srv_server_wait_machine._WAITS_IN_FLIGHT)
    monkeypatch.setattr(srv_server_wait_machine, "_WAIT_TOMBSTONES", {})
    monkeypatch.setattr(srv_server_wait, "_WAIT_TOMBSTONES", srv_server_wait_machine._WAIT_TOMBSTONES)
    monkeypatch.setattr(srv_server_lifecycle, "_bundles", {})
    monkeypatch.setattr(srv_server_wait_machine, "_bundles", srv_server_lifecycle._bundles)
    monkeypatch.setattr(srv_server_wait, "_bundles", srv_server_lifecycle._bundles)
    monkeypatch.setattr(srv_server_lifecycle, "_stopped_sessions", {})
    monkeypatch.setattr(srv_server_state, "_stopped_sessions", srv_server_lifecycle._stopped_sessions)
    monkeypatch.setattr(srv_server_wait, "_wait_segment_seconds", lambda: TEST_SEGMENT_SECONDS)
    yield
    for registry in list(srv_server_wait_machine._WAITS_IN_FLIGHT.values()):
        handles = list(registry.values())
        for handle in handles:
            handle.cancel.set()
        srv_server_wait_machine._join_wait_threads(handles)


def _open_session(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> tuple[str, Any]:
    limits = dict(FAST_LIMITS)
    limits.update(kwargs.pop("limits", {}))
    session_id, bundle, _backend, _provider = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=limits, **kwargs
    )
    return session_id, bundle


def _unwrap(result: Any) -> dict[str, Any]:
    if isinstance(result, list):
        return json.loads(result[0].text)
    return result


def _await_retired_real(session_id: str, wait_id: str, timeout: float = 5.0) -> bool:
    """Bounded REAL-time poll for worker retirement (safe under a patched clock)."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        registry = srv_server_wait_machine._WAITS_IN_FLIGHT.get(session_id, {})
        if not registry or wait_id not in registry:
            return True
        time.sleep(0.01)
    return False


# --- (i) completes inside one segment ---------------------------------------------------------


async def test_a_wait_that_fits_inside_one_segment_answers_completed_directly(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline user case, short form: the call blocks, then says ``completed``."""
    monkeypatch.setattr(srv_server_wait, "_wait_segment_seconds", lambda: 2.0)
    session_id, _bundle = _open_session(monkeypatch)

    started = time.perf_counter()
    reply = await srv_server_wait.computer_wait(session_id, duration_seconds=0.3)
    took = time.perf_counter() - started

    assert reply["ok"] is True and reply["result"] == "marker"
    assert reply["status"] == "completed"
    assert reply["elapsed_seconds"] == pytest.approx(0.3, abs=0.15)
    assert reply["remaining_seconds"] == pytest.approx(0.0, abs=0.15)
    assert 0.3 <= took < 1.5, f"expected the blocking segment, took {took:.3f}s"
    validate_reply(reply, "wait")
    assert srv_server_wait_machine._WAITS_IN_FLIGHT == {}
    ring = srv_server_wait_machine._WAIT_TOMBSTONES[session_id]
    assert next(iter(ring.values())).outcome == "completed"


# --- (i) spans several segments: running -> status -> completed -------------------------------


async def test_a_long_wait_spans_segments_running_then_status_then_completed(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE WAIT LOOP, end to end: each call blocks at most one (short, patched)
    segment; the running replies name the status tool; the loop ends completed."""
    monkeypatch.setattr(srv_server_wait, "_wait_segment_seconds", lambda: TEST_SEGMENT_SECONDS)
    session_id, _bundle = _open_session(monkeypatch)

    first = await srv_server_wait.computer_wait(session_id, duration_seconds=0.7)
    assert first["status"] == "running"
    assert first["elapsed_seconds"] == pytest.approx(TEST_SEGMENT_SECONDS, abs=0.15)
    assert first["remaining_seconds"] == pytest.approx(0.5, abs=0.15)
    assert "computer_wait_status" in first["message"]
    validate_reply(first, "wait")

    wait_id = first["wait_id"]
    blocking_status_calls = 0
    reply = first
    for _ in range(6):
        began = time.perf_counter()
        reply = await srv_server_wait.computer_wait_status(session_id, wait_id)
        took = time.perf_counter() - began
        # No single call may block longer than the segment cap (+ scheduling slack).
        assert took < TEST_SEGMENT_SECONDS + 0.5, f"status call blocked {took:.3f}s"
        blocking_status_calls += 1
        if reply["status"] != "running":
            break

    assert blocking_status_calls >= 2, "the wait should have spanned several segments"
    assert reply["status"] == "completed"
    validate_reply(reply, "wait_status")
    assert reply["remaining_seconds"] == pytest.approx(0.0, abs=0.15)
    assert srv_server_wait_machine._WAITS_IN_FLIGHT == {}


# --- (i) cancel wakes a blocked call <= 1 s ---------------------------------------------------


async def test_cancel_wakes_a_blocked_wait_call_within_one_second(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A LONG segment (5 s) with a cancel arriving 0.3 s in: the blocked
    ``computer_wait`` call must wake and answer ``cancelled`` in under 1 s."""
    monkeypatch.setattr(srv_server_wait, "_wait_segment_seconds", lambda: 5.0)
    session_id, _bundle = _open_session(monkeypatch)

    def _cancel_soon() -> None:
        time.sleep(0.3)
        registry = srv_server_wait_machine._WAITS_IN_FLIGHT.get(session_id, {})
        wait_id = next(iter(registry))
        asyncio.run(srv_server_wait.computer_cancel_wait(session_id, wait_id))

    thread = threading.Thread(target=_cancel_soon, daemon=True)
    thread.start()
    began = time.perf_counter()
    reply = await srv_server_wait.computer_wait(session_id, duration_seconds=120.0)
    took = time.perf_counter() - began
    thread.join(2.0)

    assert reply["status"] == "cancelled"
    assert reply["elapsed_seconds"] == pytest.approx(0.3, abs=0.2)
    assert took < 1.0, f"a blocked wait call took {took:.3f}s to wake on cancel"
    validate_reply(reply, "wait")


async def test_cancel_wakes_a_blocked_status_call_within_one_second(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wake contract for a blocked ``computer_wait_status`` call too."""
    monkeypatch.setattr(srv_server_wait, "_wait_segment_seconds", lambda: TEST_SEGMENT_SECONDS)
    session_id, _bundle = _open_session(monkeypatch)
    started = await srv_server_wait.computer_wait(session_id, duration_seconds=120.0)
    assert started["status"] == "running"
    wait_id = started["wait_id"]
    monkeypatch.setattr(srv_server_wait, "_wait_segment_seconds", lambda: 5.0)

    def _cancel_soon() -> None:
        time.sleep(0.3)
        asyncio.run(srv_server_wait.computer_cancel_wait(session_id, wait_id))

    thread = threading.Thread(target=_cancel_soon, daemon=True)
    thread.start()
    began = time.perf_counter()
    reply = await srv_server_wait.computer_wait_status(session_id, wait_id)
    took = time.perf_counter() - began
    thread.join(2.0)

    assert reply["status"] == "cancelled"
    assert took < 1.0, f"a blocked status call took {took:.3f}s to wake on cancel"
    validate_reply(reply, "wait_status")


async def test_stop_session_wakes_a_blocked_wait_call_and_tears_the_wait_down(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same wake contract for the stop path: the blocked call answers
    ``session_stopped`` in under 1 s and the wait is torn down."""
    monkeypatch.setattr(srv_server_wait, "_wait_segment_seconds", lambda: 5.0)
    session_id, _bundle = _open_session(monkeypatch)
    captured: dict[str, Any] = {}

    def _stop_soon() -> None:
        time.sleep(0.3)
        registry = srv_server_wait_machine._WAITS_IN_FLIGHT.get(session_id, {})
        captured["handle"] = next(iter(registry.values()))
        asyncio.run(srv_server_lifecycle.stop_session(session_id))

    thread = threading.Thread(target=_stop_soon, daemon=True)
    thread.start()
    began = time.perf_counter()
    reply = await srv_server_wait.computer_wait(session_id, duration_seconds=120.0)
    took = time.perf_counter() - began
    thread.join(2.0)

    assert reply["ok"] is False and reply["error"] == "session_stopped"
    assert took < 1.0, f"a blocked wait call took {took:.3f}s to wake on stop"
    assert captured["handle"].cancel.is_set()
    assert _await_retired_real(session_id, captured["handle"].wait_id)
    assert srv_server_wait_machine._WAITS_IN_FLIGHT == {}


async def test_a_client_side_cancellation_only_abandons_the_call_the_wait_keeps_running(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Documented semantics: cancelling the blocking MCP call stops only that
    call's blocking — the wait itself keeps running and stays queryable."""
    monkeypatch.setattr(srv_server_wait, "_wait_segment_seconds", lambda: 5.0)
    session_id, _bundle = _open_session(monkeypatch)

    task = asyncio.ensure_future(srv_server_wait.computer_wait(session_id, duration_seconds=120.0))
    await asyncio.sleep(0.3)  # the call is now inside its blocking segment
    assert not task.done()
    wait_id = next(iter(srv_server_wait_machine._WAITS_IN_FLIGHT[session_id]))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The wait itself survived the call's cancellation.
    assert wait_id in srv_server_wait_machine._WAITS_IN_FLIGHT.get(session_id, {})
    peek = await srv_server_wait.computer_wait_status(session_id, wait_id, block=False)
    assert peek["status"] == "running"

    cancelled = await srv_server_wait.computer_cancel_wait(session_id, wait_id)
    assert cancelled["status"] == "cancelled"


# --- (i) a blocked wait call never blocks other tool calls (loop concurrency) ------------------


async def test_a_blocked_wait_call_does_not_block_other_tool_calls_on_any_session(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Event-loop safety, behavioral form: while computer_wait blocks in a segment,
    observe on ANOTHER session and cancel_wait on the SAME session are served
    promptly — the loop was never blocked."""
    monkeypatch.setattr(srv_server_wait, "_wait_segment_seconds", lambda: 3.0)
    session_a, _a = _open_session(monkeypatch)
    session_b, _b = _open_session(monkeypatch)

    wait_task = asyncio.ensure_future(srv_server_wait.computer_wait(session_a, duration_seconds=120.0))
    await asyncio.sleep(0.3)  # let the call enter its blocking segment
    assert not wait_task.done()
    wait_id = next(iter(srv_server_wait_machine._WAITS_IN_FLIGHT[session_a]))

    began = time.perf_counter()
    observe = _unwrap(srv_server_observe.computer_observe(session_b))
    observe_seconds = time.perf_counter() - began
    assert observe["ok"] is True, observe
    assert observe_seconds < 0.5, f"observe stalled {observe_seconds:.3f}s behind a blocked wait"

    began = time.perf_counter()
    cancelled = await srv_server_wait.computer_cancel_wait(session_a, wait_id)
    cancel_seconds = time.perf_counter() - began
    assert cancelled["status"] == "cancelled"
    assert cancel_seconds < 1.0

    reply = await wait_task
    assert reply["status"] == "cancelled"


# --- (i) non-blocking peek ---------------------------------------------------------------------


async def test_block_false_peeks_without_blocking(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle = _open_session(monkeypatch)
    started = await srv_server_wait.computer_wait(session_id, duration_seconds=120.0)
    assert started["status"] == "running"
    wait_id = started["wait_id"]
    # Widen the segment AFTER the start call so the peek below genuinely could
    # block a long time if block=False were broken.
    monkeypatch.setattr(srv_server_wait, "_wait_segment_seconds", lambda: 5.0)

    began = time.perf_counter()
    peek = await srv_server_wait.computer_wait_status(session_id, wait_id, block=False)
    took = time.perf_counter() - began

    assert peek["status"] == "running"
    assert peek["block"] is False
    assert took < 0.2, f"block=False peek blocked for {took:.3f}s"
    validate_reply(peek, "wait_status")

    # A peek at a TERMINAL wait answers from the tombstone ring without blocking.
    await srv_server_wait.computer_cancel_wait(session_id, wait_id)
    final = await srv_server_wait.computer_wait_status(session_id, wait_id, block=False)
    assert final["status"] == "cancelled"
    assert final.get("already_cancelled") is True or "finished_at" in final


# --- (i) the segment knob: band + fallback-not-clamp -------------------------------------------


def test_wait_segment_knob_band_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default 45; band 1..55; ANYTHING else (unparsable, non-finite, out of band)
    FALLS BACK to the default — never clamped to the nearest edge."""
    assert srv_server_wait_machine.WAIT_SEGMENT_DEFAULT_SECONDS == 45.0
    cases: list[tuple[str | None, float]] = [
        (None, 45.0),
        ("", 45.0),
        ("30", 30.0),
        ("1", 1.0),
        ("55", 55.0),
        ("0.5", 45.0),   # below the band -> default, NOT clamped to 1
        ("56", 45.0),    # above the band -> default, NOT clamped to 55
        ("abc", 45.0),
        (str(float("nan")), 45.0),
        (str(float("inf")), 45.0),
        ("-10", 45.0),
    ]
    for raw, expected in cases:
        if raw is None:
            monkeypatch.delenv(srv_server_wait_machine.WAIT_SEGMENT_ENV, raising=False)
        else:
            monkeypatch.setenv(srv_server_wait_machine.WAIT_SEGMENT_ENV, raw)
        assert srv_server_wait._wait_segment_seconds() == expected, raw


# --- (i) idle integration: a running wait is never reaped; idle counts from its end -------------


class FakeClock:
    """Controllable ``time.monotonic`` stand-in (no real sleeping)."""

    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def idle_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(srv_time, "monotonic", fake)
    return fake


@pytest.fixture
def idle_wait_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, idle_clock: FakeClock
) -> Any:
    """Wait registries + short segment + 60 s idle timeout + patched monotonic."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv(srv_server_lifecycle.SESSION_IDLE_TIMEOUT_ENV, "60")
    monkeypatch.setattr(srv_server_lifecycle, "_registry", SessionRegistry(max_sessions=16))
    monkeypatch.setattr(srv_server_wait_machine, "_WAITS_IN_FLIGHT", {})
    monkeypatch.setattr(srv_server_wait, "_WAITS_IN_FLIGHT", srv_server_wait_machine._WAITS_IN_FLIGHT)
    monkeypatch.setattr(srv_server_wait_machine, "_WAIT_TOMBSTONES", {})
    monkeypatch.setattr(srv_server_wait, "_WAIT_TOMBSTONES", srv_server_wait_machine._WAIT_TOMBSTONES)
    monkeypatch.setattr(srv_server_lifecycle, "_bundles", {})
    monkeypatch.setattr(srv_server_wait_machine, "_bundles", srv_server_lifecycle._bundles)
    monkeypatch.setattr(srv_server_wait, "_bundles", srv_server_lifecycle._bundles)
    monkeypatch.setattr(srv_server_lifecycle, "_stopped_sessions", {})
    monkeypatch.setattr(srv_server_state, "_stopped_sessions", srv_server_lifecycle._stopped_sessions)
    monkeypatch.setattr(srv_server_wait, "_wait_segment_seconds", lambda: TEST_SEGMENT_SECONDS)
    return None


async def test_the_idle_reaper_never_reaps_a_running_wait_and_counts_from_its_end(
    idle_wait_server: Any, monkeypatch: pytest.MonkeyPatch, idle_clock: FakeClock
) -> None:
    """Addendum 2 follow-up: waiting time is TOOL-RUNNING time. The worker holds
    the WP-S in-flight mark for the whole wait; the idle count starts when the
    wait ENDS (the worker's exit touch re-bases the clock)."""
    session_id, bundle = _open_session(monkeypatch)
    started = await srv_server_wait.computer_wait(session_id, duration_seconds=600.0)
    assert started["status"] == "running"
    wait_id = started["wait_id"]

    idle_clock.advance(120)  # far past the 60 s timeout, mid-wait
    assert srv_server_lifecycle._sweep_idle_sessions() == []  # the in-flight mark protects the session
    assert session_id in srv_server_lifecycle._bundles
    assert bundle.context.in_flight_count() == 1  # the worker's mark (WP-S API)

    await srv_server_wait.computer_cancel_wait(session_id, wait_id)
    assert _await_retired_real(session_id, wait_id)
    # Balanced on the cancel exit path; the idle clock was re-based at the wait's end.
    assert bundle.context.in_flight_count() == 0
    assert srv_server_lifecycle._sweep_idle_sessions() == []

    idle_clock.advance(60)  # a full idle window AFTER the wait ended
    assert srv_server_lifecycle._sweep_idle_sessions() == [session_id]


async def test_a_running_wait_keeps_the_session_active_through_the_wp_s_api(
    idle_wait_server: Any, monkeypatch: pytest.MonkeyPatch, idle_clock: FakeClock
) -> None:
    """Same protection for a NATURALLY expiring wait (the exit path, not cancel):
    the idle clock is re-based to the wait's END, not to the session's creation."""
    session_id, bundle = _open_session(monkeypatch)
    started = await srv_server_wait.computer_wait(session_id, duration_seconds=0.1)
    # The monotonic clock is patched, so the worker only sees its deadline after
    # the test advances fake time; the first (0.2 s) segment answers running.
    assert started["status"] == "running"
    idle_clock.advance(0.2)  # the worker's deadline passes in fake time

    deadline = time.perf_counter() + 2.0
    while bundle.context.in_flight_count() == 1 and time.perf_counter() < deadline:
        time.sleep(0.01)
    assert bundle.context.in_flight_count() == 0  # balanced on the expiry path

    idle_clock.advance(59)  # one second short of the timeout after the wait ended
    assert srv_server_lifecycle._sweep_idle_sessions() == []
    idle_clock.advance(1)  # exactly the timeout after the wait ENDED: reaped now
    assert srv_server_lifecycle._sweep_idle_sessions() == [session_id]


# --- WP-S carry-over (1): an expired session B is reaped by a tool call on session A ------------


async def test_entry_sweep_from_session_a_reaps_expired_session_b(
    idle_wait_server: Any, monkeypatch: pytest.MonkeyPatch, idle_clock: FakeClock
) -> None:
    """Proves the tool-entry sweep: a call on A reaps the forgotten B (A excluded)."""
    session_a, _bundle_a = _open_session(monkeypatch)
    session_b, _bundle_b = _open_session(monkeypatch)

    idle_clock.advance(60)  # both sessions are now past the timeout
    observe = _unwrap(srv_server_observe.computer_observe(session_a))
    assert observe["ok"] is True

    assert session_a in srv_server_lifecycle._bundles  # the addressed session is never self-reaped
    assert session_b not in srv_server_lifecycle._bundles  # ...but the sweep reaped B on entry
    assert srv_server_lifecycle._stopped_sessions[session_b]["stop_reason"] == "idle_timeout"


async def test_a_running_wait_on_a_survives_a_sweep_triggered_by_b(
    idle_wait_server: Any, monkeypatch: pytest.MonkeyPatch, idle_clock: FakeClock
) -> None:
    """The in-flight mark survives an entry sweep triggered from another session:
    A is past the idle timeout at sweep time, but its running wait protects it."""
    session_a, bundle_a = _open_session(monkeypatch)
    session_b, _bundle_b = _open_session(monkeypatch)
    started = await srv_server_wait.computer_wait(session_a, duration_seconds=600.0)
    assert started["status"] == "running"

    idle_clock.advance(120)  # BOTH sessions are past the timeout now
    observe = _unwrap(srv_server_observe.computer_observe(session_b))
    assert observe["ok"] is True

    # B is the addressed session (excluded from its own entry sweep, touched by
    # the call); A was a sweep candidate and survived ONLY because of its wait.
    assert session_b in srv_server_lifecycle._bundles
    assert session_a in srv_server_lifecycle._bundles
    assert bundle_a.context.in_flight_count() == 1
    await srv_server_wait.computer_cancel_wait(session_a, started["wait_id"])
    assert _await_retired_real(session_a, started["wait_id"])


# --- WP-S carry-over (1): raising / cancelled tool bodies leave the counter exact ---------------


async def test_a_raising_tool_body_leaves_in_flight_count_zero(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, bundle = _open_session(monkeypatch)

    @srv_server_lifecycle._session_scoped
    async def boom(session_id: str) -> Any:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await boom(session_id)

    assert bundle.context.in_flight_count() == 0


async def test_a_cancelled_tool_body_leaves_in_flight_count_zero(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, bundle = _open_session(monkeypatch)

    @srv_server_lifecycle._session_scoped
    async def cancelled_body(session_id: str) -> Any:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await cancelled_body(session_id)

    assert bundle.context.in_flight_count() == 0


async def test_an_unknown_session_call_creates_no_bookkeeping(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = len(srv_server_lifecycle._registry)
    reply = await srv_server_wait.computer_wait("sess-never-existed", duration_seconds=5.0)
    status = await srv_server_wait.computer_wait_status("sess-never-existed", "wt_" + "0" * 16)
    cancel = await srv_server_wait.computer_cancel_wait("sess-never-existed", "wt_" + "0" * 16)

    assert reply["error"] == "unknown_session"
    assert status["error"] == "unknown_session"
    assert cancel["error"] == "unknown_session"
    assert srv_server_wait_machine._WAITS_IN_FLIGHT == {}
    assert srv_server_wait_machine._WAIT_TOMBSTONES == {}
    assert len(srv_server_lifecycle._registry) == before


# --- WP-S carry-over (2)+(4): the sweep closes outside the lock and survives a failing reap ------


async def test_one_failing_reap_never_aborts_the_sweep(
    idle_wait_server: Any, monkeypatch: pytest.MonkeyPatch, idle_clock: FakeClock
) -> None:
    """Carry-over (4): a reap that raises is logged and skipped; the other expired
    sessions are still reaped and the sweep returns normally."""
    session_a, _a = _open_session(monkeypatch)
    session_b, _b = _open_session(monkeypatch)
    idle_clock.advance(60)

    real_close = srv_server_lifecycle._close_stopped_bundle
    calls: list[str] = []

    def flaky_close(session_id: str, bundle: Any, **kwargs: Any) -> Any:
        calls.append(session_id)
        if session_id == session_a:
            raise RuntimeError("injected reap fault")
        return real_close(session_id, bundle, **kwargs)

    monkeypatch.setattr(srv_server_lifecycle, "_close_stopped_bundle", flaky_close)
    reaped = srv_server_lifecycle._sweep_idle_sessions()

    assert session_b in reaped and session_a not in reaped
    assert session_a in srv_server_lifecycle._bundles  # left live, unclaimed, retryable
    assert session_b not in srv_server_lifecycle._bundles
    assert not bundle_a_claimed(session_a)


def bundle_a_claimed(session_id: str) -> bool:
    bundle = srv_server_lifecycle._bundles.get(session_id)
    return bool(bundle is not None and bundle.context.reap_claimed)


async def test_the_reap_claim_fences_a_racing_tool_call(
    idle_wait_server: Any, monkeypatch: pytest.MonkeyPatch, idle_clock: FakeClock
) -> None:
    """Carry-over (2)+(3): once a reaper CLAIMS an idle-expired session, a tool
    call arriving on it refuses with session_stopped instead of marking busy —
    the close outside the lock can never reap a session that just became busy."""
    session_id, bundle = _open_session(monkeypatch)
    idle_clock.advance(60)
    assert bundle.context.claim_for_reap(now=idle_clock.now, timeout=60.0) is not None
    assert bundle.context.reap_claimed is True

    refusal = _unwrap(srv_server_observe.computer_observe(session_id))
    assert refusal["ok"] is False
    assert refusal["error"] == "session_stopped"
    assert bundle.context.in_flight_count() == 0  # the fenced call marked nothing

    # A failed close releases the claim so a later sweep can retry the session.
    bundle.context.release_reap_claim()
    assert bundle.context.reap_claimed is False
