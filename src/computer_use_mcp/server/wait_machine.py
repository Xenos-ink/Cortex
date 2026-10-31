"""The background-wait machinery (LAYER 2): registries, handles, worker, cancel/join, wake signalling - no tool registrations here (the three tools live in wait.py, LAYER 4)."""

from __future__ import annotations

import asyncio
import math
import os
import threading
import time
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .instance import logger
from .state import _bundles, _lock



# --- WP6 (ORVEX-2026-108, goal Addendum 2 follow-up): the cancellable background wait ------
# The user's requirement: an agent that must wait before deciding calls
# ``computer_wait(duration)`` and BLOCKS — in segments that never trip the MCP client's
# 60 s tool timeout — until it is TOLD the wait is over; it can cancel early; the
# waiting time is TOOL-RUNNING time, never idle time (a running wait holds the WP-S
# in-flight mark, so the session is never idle-reaped mid-wait and the idle count
# starts only when the wait ends); and ``stop_session`` tears every wait down.
#
# Architecture: ONE bounded daemon worker thread per wait (the worker sleeps in
# ``WAIT_CANCEL_SLICE_SECONDS`` slices on its OWN thread — never on the event loop).
# The tool functions are ASYNC and block ON the event loop via a per-call
# asyncio.Event that the worker (and the cancel/stop paths) signal through
# ``loop.call_soon_threadsafe`` (DELTA 1/F1: the executor is NEVER used — any
# number of concurrently blocked wait calls costs the loop zero threads, so the
# default pool can never be starved and no call can exceed its segment cap).
# The event loop stays free, so ``stop_session``, ``computer_cancel_wait`` and
# every other tool on any session are served while a wait call is blocked. Cancel/
# stop set the worker's cancel event AND signal the loop events directly; the
# worker retires within one slice (≤ 0.1 s) and — F2 ordering — retires its
# terminal state BEFORE signalling, so a woken caller always reads the tombstone.
# A client-side cancellation of the blocking MCP call only abandons that call's
# await — the wait itself keeps running and stays queryable.
#
# Registry/tombstone design is the dev (ORVEX-2026-102) port: an entry in
# ``_WAITS_IN_FLIGHT`` exists IFF a worker is alive; terminal outcomes live in the
# bounded ``_WAIT_TOMBSTONES`` ring; every terminal path funnels through
# ``_retire_wait``. S-15 is fixed here: the concurrent-wait caps are checked BEFORE
# any ``setdefault``, so a refused wait leaves no empty per-session dict behind.

#: Env knob: how long ONE blocking segment may last, in seconds. Default 45; band
#: 1..55 — every value keeps a call comfortably under the MCP client's 60 s tool
#: timeout. Out-of-band / unparsable / non-finite values FALL BACK to the default
#: (fallback-not-clamp, the WP-S knob style) — a typo can never silently arm a
#: call-blocking 55+ s segment.
WAIT_SEGMENT_ENV = "CORTEX_WAIT_SEGMENT_SECONDS"
WAIT_SEGMENT_DEFAULT_SECONDS = 45.0
WAIT_SEGMENT_MIN_SECONDS = 1.0
WAIT_SEGMENT_MAX_SECONDS = 55.0


def _wait_segment_seconds() -> float:
    """Resolve the blocking-segment length in seconds: env > default, fallback-not-clamp."""
    raw = os.environ.get(WAIT_SEGMENT_ENV, "").strip()
    if not raw:
        return WAIT_SEGMENT_DEFAULT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return WAIT_SEGMENT_DEFAULT_SECONDS
    if not math.isfinite(value):
        return WAIT_SEGMENT_DEFAULT_SECONDS
    if value < WAIT_SEGMENT_MIN_SECONDS or value > WAIT_SEGMENT_MAX_SECONDS:
        return WAIT_SEGMENT_DEFAULT_SECONDS
    return value


#: Concurrent waits allowed on ONE session (K-18: a named constant, not derived from
#: ``max_sessions`` — that is the concurrent-SESSION cap; deriving a per-session wait
#: cap from it was a category error).
MAX_WAITS_PER_SESSION = 4
#: Concurrent waits allowed process-wide. At the default ``max_sessions=4`` the
#: product-wide cap (4 x 4 = 16) is the one that binds.
MAX_WAITS_GLOBAL = 16
#: Retained terminal outcomes per session (K-6): the ring is what makes the
#: ``completed`` and ``already_cancelled`` rows answerable after the live entry is gone.
WAIT_TOMBSTONES_PER_SESSION = 8
#: The hard ceiling one request may name. The EFFECTIVE cap is
#: ``min(bundle.limits.max_wait_seconds, WAIT_HARD_MAX_SECONDS)`` and the refusal
#: message names THAT value, not this constant (K-19).
WAIT_HARD_MAX_SECONDS = 10800.0
#: Cancellation granularity: the worker re-checks its deadline and its cancel event
#: every ``WAIT_CANCEL_SLICE_SECONDS``, so cancellation latency is ≤ 100 ms and a
#: blocked caller wakes in well under the 1 s contract.
WAIT_CANCEL_SLICE_SECONDS = 0.1
#: Bounded join budget for the teardown path (S-16): a worker is woken by its own
#: cancel event within ``WAIT_CANCEL_SLICE_SECONDS`` and retires in one ``finally``,
#: so this is ~50x the observed cost; it exists so a wedged worker can never hang
#: ``stop_session`` forever.
WAIT_JOIN_TIMEOUT_SECONDS = 5.0

#: session_id -> wait_id -> live handle. An entry exists IFF a worker is alive (K-6:
#: the terminal outcome lives in ``_WAIT_TOMBSTONES``).
_WAITS_IN_FLIGHT: dict[str, dict[str, "_WaitHandle"]] = {}
#: session_id -> ordered wait_id -> terminal outcome, bounded at
#: ``WAIT_TOMBSTONES_PER_SESSION`` with the OLDEST evicted first.
_WAIT_TOMBSTONES: dict[str, dict[str, "_WaitTombstone"]] = {}


@dataclass
class _WaitHandle:
    """One in-flight background wait.

    Deliberately holds NO strong reference to the session bundle — only a WEAK
    reference to its ``SessionContext`` (for the WP-S in-flight mark) — so a handle
    that lives up to 3 h can never keep a stopped session's backend/agent/audit
    memory alive. It holds its own identity, its own timing, and its own events.
    Terminal state is NOT a field here — it lives in the bounded
    ``_WAIT_TOMBSTONES`` ring, which is what makes a cancel arriving after the
    worker exited answerable at all (K-6).
    """

    wait_id: str
    session_id: str
    requested_seconds: float
    label: str | None
    started_at: str
    started_monotonic: float
    deadline_monotonic: float
    context_ref: "weakref.ref[Any] | None" = None
    # F1 (VAL-WP6-01): the event loop the ``computer_wait`` that created this handle
    # ran on (diagnostics only since N1 — wakes no longer route through it), plus the
    # per-call ``(own loop, asyncio.Event)`` pairs the worker signals through
    # ``call_soon_threadsafe`` on EACH waiter's own loop (N1, VAL-WP6-02). Blocking
    # NEVER occupies an executor thread — any number of concurrently blocked wait
    # calls costs the loop zero threads, so the default pool can never be starved.
    loop: Any | None = None
    waiters: "set[Any]" = field(default_factory=set)
    waiters_lock: threading.Lock = field(default_factory=threading.Lock)
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


@dataclass(frozen=True)
class _WaitTombstone:
    """A bounded record of one finished wait, so a late cancel stays honest."""

    outcome: str  # completed | cancelled | wait_failed
    elapsed_seconds: float
    remaining_seconds: float
    at: str
    message: str | None = None


def _wait_now() -> float:
    """The monotonic clock every wait decision reads (injectable in tests).

    Every deadline, elapsed and remaining value on this path is derived from this
    one function — a worker can never mix a wall clock into a duration decision.
    """
    return time.monotonic()


def _wait_iso_utc() -> str:
    """ISO-8601 UTC wall-clock stamp, used only for the human-readable echoes."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wait_rejection(code: str, message: str) -> dict[str, object]:
    """The gate-refusal envelope for the wait family (unified contract §1.3).

    A refused wait (bad duration, over cap, too many waits, unknown id) is a
    ``rejected`` reply: ``{ok: False, result: "rejected", reasons: [<code>],
    message}`` — never an ``error`` key. The message text is byte-identical to dev
    wherever dev has the rejection.
    """
    return {
        "ok": False,
        "result": "rejected",
        "reasons": [code],
        "message": message,
    }


def _wait_verification() -> dict[str, Any]:
    """The marker verification block: a wait is a non-screen claim, verified by
    construction (same semantics as the flat ``done``/``wait`` markers)."""
    return {
        "outcome": "verified",
        "changed": False,
        "confidence": 1.0,
        "evidence": ["no_op_marker: computer_wait bookkeeping, no screen claim"],
        "verification_method": "no_op_marker",
        "note": (
            "A wait is an intentional non-screen claim: nothing was dispatched and "
            "no screen transition is asserted, so there is nothing to verify."
        ),
    }


def _wait_marker_reply(
    *,
    session_id: str,
    wait_id: str,
    status: str,
    message: str,
    handle: _WaitHandle | None = None,
    elapsed: float = 0.0,
    remaining: float = 0.0,
    extra: dict[str, Any] | None = None,
) -> dict[str, object]:
    """The unified §1.3 marker envelope for wait/cancel/status replies: ``ok`` true,
    ``result`` ``marker``, a ``status`` field (running/completed/cancelled)."""
    reply: dict[str, object] = {
        "ok": True,
        "result": "marker",
        "session_id": session_id,
        "wait_id": wait_id,
        "status": status,
        "elapsed_seconds": round(max(0.0, elapsed), 3),
        "remaining_seconds": round(max(0.0, remaining), 3),
        "message": message,
        "verification": _wait_verification(),
    }
    if handle is not None:
        reply["requested_seconds"] = handle.requested_seconds
        reply["label"] = handle.label
        reply["started_at"] = handle.started_at
    if extra:
        reply.update(extra)
    return reply


def _wait_failed_reply(
    session_id: str, wait_id: str, elapsed: float, remaining: float, detail: str | None
) -> dict[str, object]:
    """A wait whose worker raised: operation error → ``failed`` + ``wait_failed``."""
    reply: dict[str, object] = {
        "ok": False,
        "result": "failed",
        "error": "wait_failed",
        "session_id": session_id,
        "wait_id": wait_id,
        "status": "failed",
        "elapsed_seconds": round(max(0.0, elapsed), 3),
        "remaining_seconds": round(max(0.0, remaining), 3),
        "message": (
            "The wait ended with an internal error; the wait is retired either way "
            "(never a silent leak)."
            + (f" Detail: {detail}" if detail else "")
        ),
    }
    return reply


def _effective_wait_cap(bundle: Any) -> float:
    """The cap a request is measured against: the session's own limit, capped by
    the hard ceiling. The refusal message names THIS value, so a session
    configured to 60 s is told 60 and never 10 800 (K-19 / B-18)."""
    configured = float(getattr(bundle.limits, "max_wait_seconds", WAIT_HARD_MAX_SECONDS))
    return max(0.0, min(configured, WAIT_HARD_MAX_SECONDS))


def _retire_wait(handle: _WaitHandle, outcome: str, message: str | None) -> None:
    """THE single terminal path for a wait: tombstone it, then drop the entry.

    Every terminal condition — natural expiry, cancel, session stop, an exception
    inside the worker — funnels through here, so a wait can never leave a registry
    entry behind. The tombstone is written ONLY while the session is still live:
    ``_close_stopped_bundle`` clears the ring under ``_lock`` before the bundle is
    popped, and this runs under ``_lock`` too, so a worker that retires after a
    stop cannot resurrect the ring it was supposed to release.
    """
    with _lock:
        now = _wait_now()
        if handle.session_id in _bundles:
            ring = _WAIT_TOMBSTONES.setdefault(handle.session_id, {})
            ring[handle.wait_id] = _WaitTombstone(
                outcome=outcome,
                elapsed_seconds=max(0.0, now - handle.started_monotonic),
                remaining_seconds=max(0.0, handle.deadline_monotonic - now),
                at=_wait_iso_utc(),
                message=message,
            )
            while len(ring) > WAIT_TOMBSTONES_PER_SESSION:
                del ring[next(iter(ring))]
        registry = _WAITS_IN_FLIGHT.get(handle.session_id)
        if registry is not None:
            registry.pop(handle.wait_id, None)
            if not registry:
                _WAITS_IN_FLIGHT.pop(handle.session_id, None)


def _wait_worker(handle: _WaitHandle) -> None:
    """The wait thread: bounded SOLELY by its own deadline and its cancel event.

    Never by anything client-derived (A-9): a dropped connection must not leave a
    thread sleeping for three hours holding a slot. Never holds the module ``_lock``
    while sleeping either — it takes ``_lock`` only inside the terminal ``finally``,
    which is precisely why the canceller must NOT join while holding that same lock
    (K-5 / C7 / A-1).

    WP-S in-flight integration: the worker holds the session's in-flight mark (via
    the weak-referenced context) for the WHOLE wait — the session is never
    idle-reaped while a wait runs, and the worker's exit ``touch_activity`` re-bases
    the idle clock so idle counting starts when the wait ENDS. The mark is balanced
    on every exit path (expiry, cancel, stop, worker exception) because the
    touch/mark_idle pair sits in the same ``finally``.
    """
    context = handle.context_ref() if handle.context_ref is not None else None
    if context is not None:
        context.touch_activity()
        context.mark_busy()
    outcome = "completed"
    message: str | None = None
    try:
        while True:
            remaining = handle.deadline_monotonic - _wait_now()
            if remaining <= 0.0:
                break
            if handle.cancel.wait(min(WAIT_CANCEL_SLICE_SECONDS, remaining)):
                outcome = "cancelled"
                break
    except Exception as exc:  # noqa: BLE001 - a worker fault is a typed outcome
        outcome = "wait_failed"
        message = f"{type(exc).__name__}: {exc}"
        logger.warning("wait worker failed", exc_info=True)
    finally:
        try:
            # F2 (VAL-WP6-01): the terminal state is retired BEFORE anything wakes
            # a caller, so a woken computer_wait/computer_wait_status/cancel always
            # reads the tombstone — never a bogus `running` with remaining 0.
            _retire_wait(handle, outcome, message)
        finally:
            # DELTA 2: release the WP-S in-flight mark BEFORE waking anyone — when a
            # blocked caller returns, the idle clock is already re-based to the
            # wait's END and the counter is already balanced (a caller can never
            # observe a stale in-flight count for a wait it just saw finish).
            if context is not None:
                context.touch_activity()
                context.mark_idle()
            handle.done.set()
            _signal_loop_waiters(handle)


def _cancel_all_waits(session_id: str) -> list[_WaitHandle]:
    """Flag every live wait of a session for cancellation.  SET ONLY, NEVER JOINS.

    Called from ``_close_stopped_bundle`` INSIDE the locked teardown and BEFORE the
    bundle is popped, so a wait can never outlive the session that owns it. Joining
    a worker HERE would deadlock (K-5 / C7 / A-1): this holds ``_lock`` while the
    worker's own ``finally`` blocks on that same lock. The join belongs outside the
    lock (``_join_wait_threads``, called by the teardown's callers). The session's
    tombstone ring is dropped here too, because a stopped session performs no
    further work of any kind and a later cancel against it is refused with
    ``session_stopped`` before it ever reaches the ring.
    """
    with _lock:
        registry = _WAITS_IN_FLIGHT.pop(session_id, {})
        handles = list(registry.values())
        for handle in handles:
            handle.cancel.set()
            # F1: wake any coroutine blocked on this wait immediately — the wake is
            # a loop-thread-safe event set, never a join, and never a pool thread.
            _signal_loop_waiters(handle)
        _WAIT_TOMBSTONES.pop(session_id, None)
        return handles


def _join_wait_threads(
    handles: list[_WaitHandle], timeout: float = WAIT_JOIN_TIMEOUT_SECONDS
) -> None:
    """Join retired wait workers.  MUST be called OUTSIDE the module ``_lock``.

    The guard below is the mechanical form of the K-5 invariant: a future refactor
    that moves the join back inside the locked teardown fails loudly here instead
    of deadlocking the product.
    """
    if getattr(_lock, "_is_owned", lambda: False)():
        raise RuntimeError(
            "wait join attempted while holding the module lock (K-5 deadlock: the "
            "worker's own finally needs that lock)"
        )
    for handle in handles:
        thread = handle.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)


def _signal_loop_waiters(handle: _WaitHandle) -> None:
    """Wake every coroutine blocked on this wait — from ANY thread, no executor.

    F1 (VAL-WP6-01): the wake is delivered through ``loop.call_soon_threadsafe`` on
    the asyncio.Event each blocked call registered, so blocking NEVER occupies an
    executor thread and no number of concurrently blocked wait calls can starve
    the loop's default pool. Called by the worker AFTER the terminal state is
    retired (F2 ordering) and by the cancel/stop paths directly.

    N1 (VAL-WP6-02, fixed in WP4b): each waiter carries its OWN loop — captured at
    registration with ``asyncio.get_running_loop()`` — and its event is fired on
    THAT loop. The pre-fix code fired every waiter's event on the loop that CREATED
    the wait, so a ``computer_wait_status``/``computer_cancel_wait`` call running on
    a different loop than the originating ``computer_wait`` had its wake delivered
    to the wrong loop (a closed original loop swallowed it; the call then fell back
    to the full segment or the join budget).
    """
    with handle.waiters_lock:
        waiters = list(handle.waiters)
    for loop, event in waiters:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass  # that waiter's loop is already closed; its waiter is gone with it


def _fire_wait_event(loop: Any, event: Any) -> None:
    """Fire one loop-registered wait event from a foreign thread (best effort)."""
    try:
        loop.call_soon_threadsafe(event.set)
    except RuntimeError:
        pass  # the creating loop is already closed; its waiters are gone with it


async def _await_wait_wake(handle: _WaitHandle, timeout: float) -> bool:
    """Await one blocking segment (or the bounded cancel join) ON the event loop.

    The coroutine waits on an ``asyncio.Event`` that the worker — and, directly,
    the cancel/stop paths — signal via :func:`_signal_loop_waiters`. NO executor
    thread is ever involved (F1): the number of concurrently blocked wait calls
    costs the loop zero pooled threads.

    N1 (VAL-WP6-02, fixed in WP4b): the waiter registers ``(OWN loop, event)`` — the
    loop THIS call is running on, not the loop that created the wait — and the
    real-time segment Timer fires THAT pair. A status/cancel call on a different
    loop than the originating ``computer_wait`` is woken on its own loop.

    The timeout is enforced in REAL time by a one-shot daemon ``threading.Timer``
    that fires the SAME loop event at the deadline — deliberately not an asyncio
    timer: the segment cap (≤ 55 s) is a REAL-time contract and must hold even if
    the loop's clock is patched or stalled, and a one-shot timer thread that only
    ``set()``s an event cannot starve anything (it is never a pooled worker and
    exits immediately after firing).
    """
    event = asyncio.Event()
    own_loop = asyncio.get_running_loop()
    with handle.waiters_lock:
        handle.waiters.add((own_loop, event))
    timer: threading.Timer | None = None
    try:
        if not handle.done.is_set():
            timer = threading.Timer(timeout, _fire_wait_event, args=(own_loop, event))
            timer.daemon = True
            timer.start()
            await event.wait()
        # A wake that raced a completion signal still lands terminal-safe: the
        # worker retires (F2) BEFORE signalling, so a set ``done`` implies the
        # tombstone is already readable.
        return handle.done.is_set()
    finally:
        if timer is not None:
            timer.cancel()
        with handle.waiters_lock:
            handle.waiters.discard((own_loop, event))


def _live_wait_snapshot(handle: _WaitHandle) -> tuple[float, float]:
    """(elapsed, remaining) straight from the handle's monotonic anchors."""
    now = _wait_now()
    return (
        max(0.0, now - handle.started_monotonic),
        max(0.0, handle.deadline_monotonic - now),
    )


def _tombstone_reply(
    session_id: str,
    wait_id: str,
    tombstone: _WaitTombstone,
    *,
    already_cancelled: bool = False,
) -> dict[str, object]:
    """The reply for a wait that is already terminal (read from the tombstone ring)."""
    if tombstone.outcome == "wait_failed":
        return _wait_failed_reply(
            session_id,
            wait_id,
            tombstone.elapsed_seconds,
            tombstone.remaining_seconds,
            tombstone.message,
        )
    message = (
        f"Wait already finished ({tombstone.outcome}) at {tombstone.at}: elapsed "
        f"{round(tombstone.elapsed_seconds, 3)} s, remaining "
        f"{round(tombstone.remaining_seconds, 3)} s."
    )
    return _wait_marker_reply(
        session_id=session_id,
        wait_id=wait_id,
        status=tombstone.outcome,
        message=message,
        elapsed=tombstone.elapsed_seconds,
        remaining=tombstone.remaining_seconds,
        extra={
            "finished_at": tombstone.at,
            **({"already_cancelled": True} if already_cancelled else {}),
        },
    )
