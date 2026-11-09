"""The wait tools (LAYER 4): computer_wait, computer_wait_status, computer_cancel_wait and the segment-result builders (machinery in wait_machine.py, LAYER 2)."""

from __future__ import annotations

import asyncio
import math
import threading
import uuid
import weakref
from datetime import datetime, timezone
from typing import Any

from .instance import mcp
from .lifecycle import _get_live_bundle, _session_scoped
from .plan_surface import _active_plan_tool_rejection
from .schemas import NullableStr
from .state import _bundles, _error_response, _lock, _StoppedSession, _UnknownSession
from .wait_machine import (
    MAX_WAITS_GLOBAL,
    MAX_WAITS_PER_SESSION,
    WAIT_HARD_MAX_SECONDS,
    WAIT_JOIN_TIMEOUT_SECONDS,
    _WAITS_IN_FLIGHT,
    _WAIT_TOMBSTONES,
    _WaitHandle,
    _await_wait_wake,
    _effective_wait_cap,
    _live_wait_snapshot,
    _signal_loop_waiters,
    _tombstone_reply,
    _wait_iso_utc,
    _wait_marker_reply,
    _wait_now,
    _wait_rejection,
    _wait_segment_seconds,
    _wait_worker,
)



@mcp.tool()
@_session_scoped
async def computer_wait(
    session_id: str,
    duration_seconds: float,
    label: NullableStr = None,
) -> dict[str, object]:
    """Wait for a chosen duration and be TOLD when the wait is over.

    THE WAIT LOOP: call ``computer_wait(duration_seconds)`` — the call BLOCKS for up
    to one segment (``CORTEX_WAIT_SEGMENT_SECONDS``, default 45 s, band 1..55 —
    always far under the MCP client's 60 s tool timeout). When the call returns:

    * ``status`` is ``completed`` (with ``elapsed_seconds``) — the wait is over;
      ACT now.
    * ``status`` is ``running`` — the reply carries your ``wait_id`` plus
      ``elapsed_seconds`` and ``remaining_seconds``; call
      ``computer_wait_status(session_id, wait_id)`` to block for the NEXT segment.
      Repeat until it returns ``completed``, then act.

    ``duration_seconds`` is required, in SECONDS, and must be > 0 and finite. The
    hard ceiling is 10 800 s (3 h). A request above the EFFECTIVE cap
    ``min(limits.max_wait_seconds, 10800.0)`` is REFUSED with a rejection naming
    that cap and your requested value — it is never silently shortened, and nothing
    starts. Splitting one long wait into several waits is NOT a workaround: each
    call is bounded, each gets its own ``wait_id``, and the split is visible to you.

    WHAT A WAIT COSTS: **zero** ``max_actions`` slots. ``max_actions`` bounds INPUT
    volume and a wait dispatches no input, so ``computer_wait`` does not enter the
    action pipeline at all. The wait is bounded instead by ``max_wait_seconds`` per
    request and by the concurrent-wait caps (4 per session, 16 process-wide).

    WHAT ACTUALLY ENDS A SESSION (there is no hidden task clock): ``stop_session``,
    an internal safety kill, exhausting the ``max_actions`` budget, the host's
    ``max_steps``, or the process exiting. The advertised ``max_task_seconds`` (900)
    is a LIMIT YOU MAY SET, not a wall-clock ceiling that ends a session.

    SESSION LIFETIME: the waiting time is TOOL-RUNNING time, never idle time — a
    session with a running wait is never stopped for inactivity, and the idle count
    starts only when the wait completes, is cancelled, or the session is stopped.

    CANCELLATION: call ``computer_cancel_wait(wait_id)`` to end a wait early. If
    YOUR CLIENT cancels this blocking MCP call, only the call's blocking stops —
    the wait itself KEEPS RUNNING and stays queryable with ``computer_wait_status``;
    nothing is lost.

    This is a different product from the flat ``{"action": "wait", "delta": N}``
    queue item, which stays capped at 10 seconds for short in-queue pauses.

    Example: {"session_id": "S", "duration_seconds": 90, "label": "page load"} -> status "completed" (act now) or "running" (continue with computer_wait_status).
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    # While a waypoint-plan lease is active a wait
    # would interleave with the lease's step-by-step sequence and make the
    # handshake's own timing unaccounted for, so the call is refused with the typed
    # ``plan_lease`` rejection BEFORE anything starts (zero effect). The read-only
    # ``computer_wait_status`` stays allowed during a lease (master-mission WP6
    # decision).
    lease_rejection = _active_plan_tool_rejection(bundle)
    if lease_rejection is not None:
        return lease_rejection
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)):
        return _wait_rejection(
            "invalid_wait_duration",
            f"duration_seconds must be a number of seconds; got {duration_seconds!r} "
            f"({type(duration_seconds).__name__}).",
        )
    requested = float(duration_seconds)
    cap = _effective_wait_cap(bundle)
    if not math.isfinite(requested) or requested <= 0.0:
        return _wait_rejection(
            "invalid_wait_duration",
            "duration_seconds must be a finite number greater than 0 seconds "
            f"(the cap for this session is {cap} s); got {duration_seconds!r}.",
        )
    if requested > cap:
        return _wait_rejection(
            "wait_duration_exceeds_max",
            f"requested {requested} s exceeds the maximum wait of {cap} s "
            f"({cap * 1000:,.0f} ms) for this session; nothing was started. "
            "A wait is never silently shortened — request less, or split the work "
            "across sessions.",
        )
    wait_id = f"wt_{uuid.uuid4().hex[:16]}"
    started_at = datetime.now(timezone.utc)
    started_monotonic = _wait_now()
    handle = _WaitHandle(
        wait_id=wait_id,
        session_id=session_id,
        requested_seconds=requested,
        label=None if label is None else str(label),
        started_at=started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        started_monotonic=started_monotonic,
        deadline_monotonic=started_monotonic + requested,
        context_ref=weakref.ref(bundle.context),
        loop=asyncio.get_running_loop(),
    )
    thread = threading.Thread(
        target=_wait_worker,
        args=(handle,),
        name=f"cortex-wait-{wait_id}",
        daemon=True,
    )
    handle.thread = thread
    with _lock:
        # S-15 fix: the caps are checked BEFORE any setdefault, so a refused wait
        # leaves no empty per-session dict behind.
        registry = _WAITS_IN_FLIGHT.get(session_id, {})
        live = sum(len(entries) for entries in _WAITS_IN_FLIGHT.values())
        if len(registry) >= MAX_WAITS_PER_SESSION:
            return _wait_rejection(
                "too_many_waits",
                f"this session already has {len(registry)} waits in flight "
                f"(MAX_WAITS_PER_SESSION = {MAX_WAITS_PER_SESSION}); cancel one first.",
            )
        if live >= MAX_WAITS_GLOBAL:
            return _wait_rejection(
                "too_many_waits",
                f"{live} waits are in flight across all sessions "
                f"(MAX_WAITS_GLOBAL = {MAX_WAITS_GLOBAL}); cancel one first.",
            )
        # F7 (VAL-WP6-01 delta): re-check liveness AT registration, under the same
        # lock as the insert — a session stopped while this call was starting is
        # refused here instead of registering an orphaned wait.
        if session_id not in _bundles:
            return _error_response(_StoppedSession(session_id))
        # The entry is inserted BEFORE start() so a cancel can never race ahead of
        # the registry, and it is removed in exactly one place: the worker's finally.
        _WAITS_IN_FLIGHT.setdefault(session_id, {})[wait_id] = handle
    try:
        thread.start()
    except Exception as exc:  # noqa: BLE001 - F6 (VAL-WP6-01): no phantom registration
        with _lock:
            registry = _WAITS_IN_FLIGHT.get(session_id)
            if registry is not None:
                registry.pop(wait_id, None)
                if not registry:
                    _WAITS_IN_FLIGHT.pop(session_id, None)
        return {
            "ok": False,
            "result": "failed",
            "error": "wait_failed",
            "session_id": session_id,
            "wait_id": wait_id,
            "status": "failed",
            "message": (
                "The wait could not be started "
                f"({type(exc).__name__}: {exc}); nothing is registered, no wait "
                "slot is consumed, and no worker exists."
            ),
        }
    segment = _wait_segment_seconds()
    # THE BLOCKING SEGMENT: awaited ON the event loop via the loop-signal design —
    # the executor is never involved, so any number of concurrently blocked calls
    # stays safe, and cancel/stop wake this call through call_soon_threadsafe < 1 s.
    await _await_wait_wake(handle, segment)
    return _wait_segment_result_reply(session_id, wait_id, handle, segment, source="wait")


def _wait_segment_result_reply(
    session_id: str,
    wait_id: str,
    handle: _WaitHandle,
    segment: float,
    *,
    source: str,
) -> dict[str, object]:
    """Resolve the reply after a blocking segment (computer_wait / computer_wait_status).

    A client-side cancellation of the CALL never stops the WAIT: this resolver only
    ever READS state. Resolution order: session liveness (``session_stopped``), the
    tombstone (F2: a woken caller ALWAYS finds the terminal state here), the
    cancel-flag fallback (woken by cancel/stop before the worker reported
    retirement — answered from the handle rather than a lie), otherwise ``running``.
    """
    try:
        _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    with _lock:
        tombstone = _WAIT_TOMBSTONES.get(session_id, {}).get(wait_id)
    if tombstone is not None:
        return _tombstone_reply(session_id, wait_id, tombstone)
    elapsed, remaining = _live_wait_snapshot(handle)
    if handle.cancel.is_set():
        return _wait_marker_reply(
            session_id=session_id,
            wait_id=wait_id,
            status="cancelled",
            message=(
                "Cancellation was requested; the wait was signalled to end and the "
                "worker had not yet reported retirement. The wait is retired either "
                "way — call computer_wait_status to confirm."
            ),
            handle=handle,
            elapsed=elapsed,
            remaining=remaining,
            extra={"cancelled_at": _wait_iso_utc()},
        )
    if source == "wait":
        message = (
            "The wait did not finish inside this segment. It is still running "
            f"({round(elapsed, 3)} s elapsed, {round(remaining, 3)} s remaining of "
            f"{handle.requested_seconds} s). To be told when the wait is over, call "
            "computer_wait_status(session_id="
            f"\"{session_id}\", wait_id=\"{wait_id}\") — it blocks for the next "
            f"{round(segment, 3)} s segment and returns completed when the wait "
            "ends. Call computer_cancel_wait(wait_id) to end it early."
        )
    else:
        message = (
            "The wait is still running "
            f"({round(elapsed, 3)} s elapsed, {round(remaining, 3)} s remaining of "
            f"{handle.requested_seconds} s). Call computer_wait_status(session_id, "
            f"wait_id) again to wait the next {round(segment, 3)} s segment, or "
            "computer_cancel_wait(wait_id) to end it early."
        )
    return _wait_marker_reply(
        session_id=session_id,
        wait_id=wait_id,
        status="running",
        message=message,
        handle=handle,
        elapsed=elapsed,
        remaining=remaining,
        extra={"segment_seconds": round(segment, 3), "max_wait_seconds": _effective_wait_cap_for(session_id)},
    )


def _effective_wait_cap_for(session_id: str) -> float:
    """The effective cap echoed in replies (the session's configured limit)."""
    with _lock:
        bundle = _bundles.get(session_id)
    if bundle is None:
        return WAIT_HARD_MAX_SECONDS
    return _effective_wait_cap(bundle)


@mcp.tool()
@_session_scoped
async def computer_wait_status(
    session_id: str,
    wait_id: str,
    block: bool = True,
) -> dict[str, object]:
    """Wait for (or peek at) a running wait and report its outcome.

    The consumer half of the wait loop: call this with the ``wait_id`` from
    ``computer_wait`` whenever that call returned ``status`` ``running``. With the
    default ``block=true`` the call blocks for up to one more segment (the same
    ``CORTEX_WAIT_SEGMENT_SECONDS`` bound — always under the MCP client's 60 s tool
    timeout) and then answers with the wait's state:

    * ``status`` ``completed`` — the wait ran its full course; ACT now.
    * ``status`` ``running`` — still going; call this again to wait the next segment.
    * ``status`` ``cancelled`` — the wait was ended early (this carries
      ``elapsed_seconds``/``remaining_seconds`` so you know how far it got).

    Pass ``block=false`` for a non-blocking PEEK: the current state and timers with
    no waiting. A wait_id that finished earlier is answered from the bounded
    tombstone ring; an unknown or foreign-session wait_id is ``unknown_wait``
    (session-scoped ids: a wrong id can never probe another session's waits).

    If the worker behind the wait raised, the reply is ``failed`` with
    ``error`` ``wait_failed``. On a stopped session this is refused with
    ``session_stopped``. If YOUR CLIENT cancels this blocking call, the underlying
    wait is NOT affected — call again to re-attach.

    Example: {"session_id": "S", "wait_id": "W"} — or add "block": false to peek.
    """
    try:
        _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    if not isinstance(wait_id, str) or not wait_id:
        return _wait_rejection(
            "unknown_wait",
            "wait_id must be a non-empty string as returned by computer_wait.",
        )
    with _lock:
        handle = _WAITS_IN_FLIGHT.get(session_id, {}).get(wait_id)
        tombstone = _WAIT_TOMBSTONES.get(session_id, {}).get(wait_id)
    if handle is None:
        if tombstone is None:
            # Byte-identical for a never-existed id AND for another session's id:
            # the message deliberately does NOT echo the value (R2 / A-6).
            return _wait_rejection(
                "unknown_wait",
                "No wait with this wait_id exists on this session: wait ids are "
                "session-scoped, are not persisted across a server restart, and a "
                "finished wait is remembered only briefly.",
            )
        return _tombstone_reply(session_id, wait_id, tombstone)
    segment = _wait_segment_seconds()
    if block:
        await _await_wait_wake(handle, segment)
        return _wait_segment_result_reply(session_id, wait_id, handle, segment, source="status")
    elapsed, remaining = _live_wait_snapshot(handle)
    return _wait_marker_reply(
        session_id=session_id,
        wait_id=wait_id,
        status="running",
        message=(
            f"The wait is still running ({round(elapsed, 3)} s elapsed, "
            f"{round(remaining, 3)} s remaining of {handle.requested_seconds} s). "
            "Call computer_wait_status(session_id, wait_id) again to wait the next "
            f"{round(segment, 3)} s segment, or computer_cancel_wait(wait_id) to "
            "end it early."
        ),
        handle=handle,
        elapsed=elapsed,
        remaining=remaining,
        extra={"segment_seconds": round(segment, 3), "block": bool(block)},
    )


@mcp.tool()
@_session_scoped
async def computer_cancel_wait(session_id: str, wait_id: str) -> dict[str, object]:
    """End a wait EARLY and report how much of it actually elapsed.

    ``status`` is always one of the defined answers, never a guess:

    * ``cancelled`` — this call ended the wait. Carries ``elapsed_seconds`` and
      ``remaining_seconds`` so you know exactly how much time passed. A second
      cancel of the same id is idempotent: the same numbers plus
      ``already_cancelled: true``.
    * ``completed`` — the wait had already run its full course. That is an honest
      answer, not a failure; ``remaining_seconds`` is 0.

    An ``unknown`` ``wait_id`` is ``unknown_wait``. A ``wait_id`` belonging to a
    DIFFERENT session gets the byte-identical answer, so a wrong id can never probe
    another session's handles or stop someone else's wait.

    A wait whose worker raised answers ``failed`` with ``error`` ``wait_failed``
    and a short message (never a traceback). On a stopped session this is refused
    with ``session_stopped`` — a stopped session performs no further work of any
    kind.

    This call never blocks longer than one cancellation slice (~0.1 s): the wake is
    the worker's own done event, so a ``computer_wait``/``computer_wait_status``
    call blocked on this wait returns ``cancelled`` in under a second.

    Example: {"session_id": "S", "wait_id": "W"}
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    # WP7b: a cancel interleaved with a plan lease would kill a wait the lease's
    # sequence may be pacing around — refused with the typed ``plan_lease``
    # rejection while a lease is active (the wait itself is left running).
    lease_rejection = _active_plan_tool_rejection(bundle)
    if lease_rejection is not None:
        return lease_rejection
    if not isinstance(wait_id, str) or not wait_id:
        return _wait_rejection(
            "unknown_wait",
            "wait_id must be a non-empty string as returned by computer_wait.",
        )
    # Session-scoped lookup ONLY: a foreign id is indistinguishable from one that
    # never existed, and can never cancel another session's wait (R2 / A-6).
    with _lock:
        handle = _WAITS_IN_FLIGHT.get(session_id, {}).get(wait_id)
        tombstone = _WAIT_TOMBSTONES.get(session_id, {}).get(wait_id)
        if handle is not None:
            handle.cancel.set()
            # F1 (VAL-WP6-01 delta): wake any coroutine blocked on this wait
            # immediately — a loop-thread-safe event set, never an executor thread.
            _signal_loop_waiters(handle)
    if handle is None:
        if tombstone is None:
            return _wait_rejection(
                "unknown_wait",
                "No wait with this wait_id exists on this session: wait ids are "
                "session-scoped, are not persisted across a server restart, and a "
                "finished wait is remembered only briefly.",
            )
        return _tombstone_reply(
            session_id, wait_id, tombstone, already_cancelled=tombstone.outcome == "cancelled"
        )
    # No lock is held here and NO executor thread is occupied: this call waits on
    # the same loop-signal event the worker (and the direct signal above) set, so
    # the worker's retire — and every other blocked caller's wake — happens within
    # the 0.1 s cancellation slice, far under the 1 s contract. Bounded at the
    # S-16 join budget so a wedged worker degrades to the handle-based answer
    # below instead of hanging the call.
    await _await_wait_wake(handle, WAIT_JOIN_TIMEOUT_SECONDS)
    try:
        _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    with _lock:
        settled = _WAIT_TOMBSTONES.get(session_id, {}).get(wait_id)
    if settled is None:
        # The worker is wedged; answer from the handle rather than lie.
        elapsed, remaining = _live_wait_snapshot(handle)
        return _wait_marker_reply(
            session_id=session_id,
            wait_id=wait_id,
            status="cancelled",
            message=(
                "Cancellation requested; the wait was signalled to end and the "
                "worker had not yet reported retirement. The wait is retired "
                "either way — call computer_wait_status to confirm."
            ),
            handle=handle,
            elapsed=elapsed,
            remaining=remaining,
            extra={"cancelled_at": _wait_iso_utc()},
        )
    # Dev-identical semantics: THIS call ended the wait, so no already_cancelled
    # flag — that flag is reserved for a cancel that found the wait already over.
    return _tombstone_reply(session_id, wait_id, settled)
