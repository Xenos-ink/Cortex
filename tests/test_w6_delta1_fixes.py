"""WP6 DELTA 1 (VAL-WP6-01 fixes) — pool-saturation safety, retire-before-signal
boundary, and the minor/nit repairs.

* F1 (MAJOR): the blocking segments and the cancel join NEVER use the loop's
  default executor. Each blocked call awaits a per-call ``asyncio.Event`` that the
  worker — and the cancel/stop paths — signal through ``loop.call_soon_threadsafe``.
  Pinned here with MORE simultaneously blocked calls than the default pool size
  (40 statuses + 4 blocked waits), cancel waking its target < 1 s among them, and
  a client-side cancellation that leaves no thread held.
* F2: the worker retires the terminal state BEFORE any wake (inner finally), so a
  caller woken at the exact segment/wait boundary always reads the tombstone —
  tested at the boundary for computer_wait, computer_wait_status and the
  cancel-at-expiry race.
* F4: stop_session is now ASYNC (sync FastMCP tools run inline on the loop); its
  bounded join runs off the loop via ``asyncio.to_thread`` (starvation-safe: the
  F1 design never occupies the default executor for waits). Schema + reply pinned.
* F5: SURFACE_RESULT_TOKENS["wait"] admits "failed"; the computer_wait
  wait_failed reply is validated through the contract.
* F6: a failed ``thread.start()`` pops the registration (no phantom cap usage).
* F7: the registration re-checks session liveness under the registry lock.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from typing import Any

import pytest
from test_controller_integration import FAST_LIMITS, make_session

import computer_use_mcp.server.wait as srv_server_wait
import computer_use_mcp.server.lifecycle as srv_server_lifecycle
import computer_use_mcp.server.wait_machine as srv_server_wait_machine
import computer_use_mcp.server.state as srv_server_state
import computer_use_mcp.server.app as srv_server_app
from computer_use_mcp.core.state import SessionRegistry
from reply_contract import validate_reply

TEST_SEGMENT_SECONDS = 0.2


@pytest.fixture
def wait_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh registries + short blocking segment (same as the w6 suite)."""
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


# --- F1 (a)+(b): more blocked calls than the default pool; cancel still wakes < 1 s --------------


async def test_forty_blocked_wait_calls_all_return_within_the_segment_cap(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 (a): 40 simultaneously blocked computer_wait_status calls (more than the
    loop's default executor size) plus blocked computer_wait calls on other
    sessions ALL return within segment + slack; F1 (b): with all of them blocked,
    computer_cancel_wait still wakes its target in well under 1 s. Under the old
    run_in_executor design this overruns the cap — the loop-signal design makes
    the blocked-call count irrelevant."""
    session_a, _a = _open_session(monkeypatch)
    seed = await srv_server_wait.computer_wait(session_a, duration_seconds=120.0)
    assert seed["status"] == "running"
    wait_id = seed["wait_id"]
    # Widen the segment for the BLOCKED phase: each status call below may legally
    # block up to 2.0 s — the cap under test.
    monkeypatch.setattr(srv_server_wait, "_wait_segment_seconds", lambda: 2.0)

    others = [_open_session(monkeypatch)[0] for _ in range(4)]
    blocked_wait_calls = [
        asyncio.ensure_future(srv_server_wait.computer_wait(s, duration_seconds=120.0)) for s in others
    ]
    blocked_status_calls = [
        asyncio.ensure_future(srv_server_wait.computer_wait_status(session_a, wait_id)) for _ in range(40)
    ]
    await asyncio.sleep(0.4)  # every call is now inside its blocking segment
    assert not any(task.done() for task in blocked_status_calls)

    began = time.perf_counter()
    cancelled = await srv_server_wait.computer_cancel_wait(session_a, wait_id)
    cancel_seconds = time.perf_counter() - began
    assert cancelled["status"] == "cancelled"
    assert cancel_seconds < 1.0, f"cancel took {cancel_seconds:.3f}s with 40 calls blocked"

    status_replies = await asyncio.wait_for(
        asyncio.gather(*blocked_status_calls), timeout=2.0 + 1.5
    )
    for reply in status_replies:
        assert reply["status"] == "cancelled", reply
    # The four blocked computer_wait calls exhaust their segment and answer running.
    wait_replies = await asyncio.wait_for(
        asyncio.gather(*blocked_wait_calls), timeout=2.0 + 1.5
    )
    for reply in wait_replies:
        assert reply["status"] == "running", reply

    await srv_server_lifecycle.stop_session(session_a)
    for s in others:
        await srv_server_lifecycle.stop_session(s)
    assert srv_server_wait_machine._WAITS_IN_FLIGHT == {}


async def test_a_client_side_cancellation_leaves_no_thread_and_the_wait_running(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 (c): cancelling the awaiting coroutine abandons ONLY the await — no
    executor thread stays blocked (the design uses none at all) and the wait
    keeps running and stays queryable."""
    session_id, _bundle = _open_session(monkeypatch)
    started = await srv_server_wait.computer_wait(session_id, duration_seconds=120.0)
    assert started["status"] == "running"
    wait_id = started["wait_id"]
    monkeypatch.setattr(srv_server_wait, "_wait_segment_seconds", lambda: 3.0)

    baseline_threads = threading.active_count()
    task = asyncio.ensure_future(srv_server_wait.computer_wait_status(session_id, wait_id))
    await asyncio.sleep(0.3)  # blocked inside its segment
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The wait itself is untouched, and NO thread is held by the abandoned call.
    peek = await srv_server_wait.computer_wait_status(session_id, wait_id, block=False)
    assert peek["status"] == "running"
    await asyncio.sleep(0.2)
    assert threading.active_count() <= baseline_threads, (
        "an abandoned blocking call must not hold a thread"
    )
    cancelled = await srv_server_wait.computer_cancel_wait(session_id, wait_id)
    assert cancelled["status"] == "cancelled"


# --- F2: the wait ends exactly as the segment ends — a woken caller sees the terminal state -----


async def test_a_wait_ending_exactly_at_the_segment_end_reports_completed(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2: duration == segment. Whichever wake wins, the reply is consistent: a
    completed reply ALWAYS finds its tombstone (retire happens before the wake
    signal), and a running reply is followed by completed on the next call."""
    monkeypatch.setattr(srv_server_wait, "_wait_segment_seconds", lambda: 0.3)
    session_id, _bundle = _open_session(monkeypatch)

    reply = await srv_server_wait.computer_wait(session_id, duration_seconds=0.3)
    if reply["status"] == "completed":
        assert reply["remaining_seconds"] == pytest.approx(0.0, abs=0.15)
        tombstone = srv_server_wait_machine._WAIT_TOMBSTONES[session_id][reply["wait_id"]]
        assert tombstone.outcome == "completed"
        validate_reply(reply, "wait")
    else:
        assert reply["status"] == "running"
        status = await srv_server_wait.computer_wait_status(session_id, reply["wait_id"])
        assert status["status"] == "completed"
        validate_reply(status, "wait_status")


async def test_status_woken_inside_the_final_segment_reports_completed(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2, status form: the remaining time is SHORTER than the segment, so the
    blocking status call must wake at the wait's end and answer completed —
    never running, and always with the tombstone already retired (F2)."""
    session_id, _bundle = _open_session(monkeypatch)
    started = await srv_server_wait.computer_wait(session_id, duration_seconds=0.5)
    assert started["status"] == "running"
    monkeypatch.setattr(srv_server_wait, "_wait_segment_seconds", lambda: 2.0)

    began = time.perf_counter()
    reply = await srv_server_wait.computer_wait_status(session_id, started["wait_id"])
    took = time.perf_counter() - began

    assert reply["status"] == "completed"
    assert reply["remaining_seconds"] == pytest.approx(0.0, abs=0.15)
    tombstone = srv_server_wait_machine._WAIT_TOMBSTONES[session_id][started["wait_id"]]
    assert tombstone.outcome == "completed"
    assert took < 1.5  # woke at the wait's end, not at the segment cap
    validate_reply(reply, "wait_status")


async def test_a_cancel_racing_the_segment_end_never_reports_running(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2, cancel form: a cancel arriving around the expiry boundary answers one
    of the two DEFINED terminal states with consistent halves — never running,
    and a terminal tombstone is always already retired when reported."""
    session_id, _bundle = _open_session(monkeypatch)

    for _ in range(8):
        started = await srv_server_wait.computer_wait(session_id, duration_seconds=0.2)
        wait_id = started["wait_id"]
        if started["status"] == "running":

            def _cancel_soon() -> None:
                time.sleep(0.12)
                asyncio.run(srv_server_wait.computer_cancel_wait(session_id, wait_id))

            thread = threading.Thread(target=_cancel_soon, daemon=True)
            thread.start()
            reply = await srv_server_wait.computer_wait_status(session_id, wait_id)
            thread.join(2.0)
            assert reply["status"] in {"cancelled", "completed"}, reply
            assert reply["elapsed_seconds"] + reply["remaining_seconds"] == pytest.approx(
                0.2, abs=0.3
            )
            if reply["status"] == "completed":
                tombstone = srv_server_wait_machine._WAIT_TOMBSTONES[session_id][wait_id]
                assert tombstone.outcome == "completed"
        else:
            tombstone = srv_server_wait_machine._WAIT_TOMBSTONES[session_id][wait_id]
            assert tombstone.outcome == "completed"


# --- F5: the wait surface itself answers wait_failed, contract-validated ------------------------


class _FaultClock:
    """Injectable wait clock that raises inside the worker from call N."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.calls = 0
        self.raise_from: int | None = None
        self.fired = False

    def __call__(self) -> float:
        self.calls += 1
        if self.raise_from is not None and not self.fired and self.calls >= self.raise_from:
            self.fired = True
            raise RuntimeError("injected wait-worker fault")
        return self.now


async def test_computer_wait_itself_reports_wait_failed_through_validate_reply(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F5: the worker raising inside the first segment surfaces on the
    ``computer_wait`` reply itself as result=failed / error=wait_failed, and the
    reply passes validate_reply on the 'wait' surface (which now admits failed)."""
    clock = _FaultClock()
    clock.raise_from = 3
    monkeypatch.setattr(srv_server_wait_machine, "_wait_now", clock)
    monkeypatch.setattr(srv_server_wait, "_wait_now", srv_server_wait_machine._wait_now)
    session_id, _bundle = _open_session(monkeypatch)

    reply = await srv_server_wait.computer_wait(session_id, duration_seconds=600.0)

    assert reply["ok"] is False
    assert reply["result"] == "failed"
    assert reply["error"] == "wait_failed"
    assert "injected wait-worker fault" in reply["message"]
    assert "Traceback" not in reply["message"]
    validate_reply(reply, "wait")
    assert srv_server_wait_machine._WAITS_IN_FLIGHT == {}


# --- F6: a failed thread.start pops the registration (no phantom cap usage) ---------------------


async def test_a_failed_thread_start_pops_the_registration_and_costs_no_slot(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6: if the worker thread cannot start, the registry entry is popped — no
    phantom wait consumes a cap slot and the tool answers failed/wait_failed."""

    def _no_threads(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no threads today")

    monkeypatch.setattr(threading.Thread, "start", _no_threads)
    session_id, _bundle = _open_session(monkeypatch)

    reply = await srv_server_wait.computer_wait(session_id, duration_seconds=60.0)

    assert reply["ok"] is False
    assert reply["result"] == "failed"
    assert reply["error"] == "wait_failed"
    assert "no threads today" in reply["message"]
    validate_reply(reply, "wait")
    assert srv_server_wait_machine._WAITS_IN_FLIGHT == {}  # no phantom entry, no empty dict, no slot used
    assert [t for t in threading.enumerate() if t.name.startswith("cortex-wait-")] == []


# --- F7: the registration re-checks session liveness --------------------------------------------


async def test_the_registration_rechecks_session_liveness(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7: a session that dies between the entry liveness check and the registry
    insert is refused at registration — no orphaned wait is ever registered."""
    session_id, _bundle = _open_session(monkeypatch)
    real_cap = srv_server_wait._effective_wait_cap

    def cap_and_stop(bundle: Any) -> float:
        # Simulate the session being stopped right before registration.
        srv_server_lifecycle._bundles.pop(session_id, None)
        srv_server_lifecycle._registry.remove(session_id)
        return real_cap(bundle)

    monkeypatch.setattr(srv_server_wait, "_effective_wait_cap", cap_and_stop)

    reply = await srv_server_wait.computer_wait(session_id, duration_seconds=60.0)

    assert reply["ok"] is False
    assert reply["error"] == "session_stopped"
    assert srv_server_wait_machine._WAITS_IN_FLIGHT == {}
    assert srv_server_wait_machine._WAIT_TOMBSTONES == {}


# --- F4: stop_session is async with an unchanged advertised schema and reply ---------------------


async def test_stop_session_is_async_with_unchanged_schema_and_reply(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4: the tool is now a coroutine (bounded join off the loop via to_thread),
    its advertised parameters are unchanged (session_id only, required, no anyOf),
    and the reply keeps the exact legacy key set, including the idempotent re-stop."""
    assert inspect.iscoroutinefunction(srv_server_lifecycle.stop_session)
    meta = srv_server_app.mcp._tool_manager.get_tool("stop_session").fn_metadata
    fields = meta.arg_model.model_fields
    assert set(fields) == {"session_id"}
    assert fields["session_id"].is_required()
    assert "anyOf" not in json.dumps(meta.arg_model.model_json_schema())

    session_id, bundle = _open_session(monkeypatch)
    reply = await srv_server_lifecycle.stop_session(session_id)
    assert set(reply) == {"ok", "session_id", "message", "task_id", "termination_reason"}
    assert reply["ok"] is True
    assert reply["task_id"] == bundle.context.task.task_id

    again = await srv_server_lifecycle.stop_session(session_id)
    assert again["ok"] is True
    assert again["message"] == "Session already stopped."


async def test_stop_session_with_a_live_wait_terminates_the_worker_thread(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4 companion: the async stop still cancels AND joins every wait of the
    session — no cortex-wait thread survives the stop."""
    session_id, _bundle = _open_session(monkeypatch)
    started = await srv_server_wait.computer_wait(session_id, duration_seconds=3000.0)
    handle = srv_server_wait_machine._WAITS_IN_FLIGHT[session_id][started["wait_id"]]
    thread = handle.thread
    assert thread is not None and thread.is_alive()

    reply = await srv_server_lifecycle.stop_session(session_id)

    assert reply["ok"] is True
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert handle.cancel.is_set()
    assert srv_server_wait_machine._WAITS_IN_FLIGHT == {}
