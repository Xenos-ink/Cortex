"""Session lifecycle (LAYER 3): the ONE kill-path funnel, the idle reaper, the scoping decorator, and the start/stop lifecycle tools."""

from __future__ import annotations

import asyncio
import functools
import inspect
import math
import os
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from collections.abc import Callable

from ..core.limits import ACTIONS_BAND, STEPS_BAND, LimitEnforcer, Limits
from ..core.models import SessionState
from ..core.state import SessionLimitExceeded
from ..desktop.client_budget import client_timeout_seconds
from ..desktop.interference import parse_interference
from ..pipeline.agent import ComputerUseAgent
from ..planning.checkpoint_manager import (
    CheckpointError,
    CheckpointTrigger,
    CheckpointValidationError,
)
from ..planning.context_manager import ContextManager
from ..planning.long_running import LongRunningRuntime
from ..planning.resume_manager import ResumeBundle, ResumeRefusalError
from ..security.audit import AuditLogger, Metrics
from ..security.safety import SafetyPolicy
from ..vision.evidence import EVIDENCE_STORE_KEY, dispose_store, new_store
from .instance import logger, mcp
from .plan_surface import _audit_plan_terminal, _plan_guard, _plan_terminal_audit_lock
from .pointer_reply import resolve_show_pointer
from .schemas import NullableBool, NullableNumberMap, NullableStr, NullableStrList, NullableStrMap, _coerce_tolerant_str_list

if TYPE_CHECKING:
    from .wait_machine import _WaitHandle
from .state import (
    IMAGE_DELIVERY_TEXT_NOTE,
    _LazyProvider,
    _SessionBundle,
    _StoppedSession,
    _UnknownSession,
    _audit_root,
    _backend_factory,
    _bundles,
    _checkpoint_manager,
    _error_response,
    _lock,
    _normalize_image_delivery_param,
    _parse_limits,
    _provider_factory,
    _registry,
    _remember_stopped,
    _resolve_image_delivery,
    _resume_manager,
    _stopped_sessions,
)
from .wait_machine import _cancel_all_waits, _join_wait_threads



def _get_bundle(session_id: str) -> _SessionBundle:
    """Return the session bundle or raise :class:`_UnknownSession` (fail-closed)."""
    bundle = _bundles.get(session_id)
    if bundle is None:
        raise _UnknownSession(session_id)
    return bundle


def _get_live_bundle(session_id: str) -> _SessionBundle:
    """Return a NON-stopped session bundle (D1/F7 fail-closed discipline).

    Stopped sessions are removed from the registry and the bundle store; later tool
    calls receive a structured ``session_stopped`` error — consistent with the
    stopped-session policy: a stopped session performs no further work of any kind.

    F7: this ALSO covers the internal kill path — if a session's StopToken was armed
    by an internal safety path (not via ``stop_session``), the bundle is routed through
    the SAME cleanup here, so bundle hygiene is identical for both stop flavors and
    every tool refuses with ``session_stopped``.
    """
    bundle = _bundles.get(session_id)
    if bundle is None:
        if session_id in _stopped_sessions:
            raise _StoppedSession(session_id)
        raise _UnknownSession(session_id)
    if bundle.context.stop.stopped:
        try:
            bundle.auditor.emit(
                "stop",
                session_id,
                task_id=bundle.context.task.task_id,
                result="stopped",
                metadata={"source": "internal_kill_path", "detected": True},
            )
        except Exception:
            logger.debug("internal-stop audit failed", exc_info=True)
        _close_stopped_bundle(session_id, bundle, source="internal_kill_path", audit_stop_events=False)
        raise _StoppedSession(session_id)
    return bundle


def _close_stopped_bundle(
    session_id: str,
    bundle: _SessionBundle,
    *,
    source: str,
    audit_stop_events: bool = True,
    audit_metadata: dict[str, Any] | None = None,
    snapshot_extra: dict[str, Any] | None = None,
) -> list["_WaitHandle"]:
    """Shared kill-path cleanup (F7): one stopping flavor, one hygiene outcome.

    Arms the token (idempotent), mirrors the flag onto the legacy session state, audits
    the stop, removes the bundle from the store AND the registry, and records the
    bounded stopped-session snapshot. ``audit_stop_events=False`` is used when the stop
    was already audited by another path (in-run emergency_stop, detection event).

    WP-S (idle reaper): ``audit_metadata`` is merged into BOTH stop events' metadata
    (the idle reaper adds ``idle_seconds``); ``snapshot_extra`` is merged into the
    remembered stopped-session snapshot (the reaper adds ``stop_reason`` +
    ``idle_seconds`` so a later call's ``session_stopped`` message can say the session
    was stopped for inactivity after N seconds). Both default to None — existing
    callers (stop_session, internal kill path) are byte-identical.

    B4 (T8): teardown is ATOMIC under the lock and the stopped-session snapshot is
    remembered FIRST, with every fallible piece guarded — a failure inside the audit
    or the metrics snapshot can never again leave the registry/bundle store popped
    WITHOUT the stopped memory (the exact state that surfaces to clients as a
    bogus ``unknown_session`` for a session that was already stopped).

    WP6: every live background wait of the session is cancelled HERE, inside the
    locked teardown and BEFORE the bundle is popped (the dev ``_cancel_all_waits``
    anchor), so a wait can never outlive the session that owns it; the session's
    tombstone ring is dropped with it (a stopped session performs no further work).
    The cancelled handles are RETURNED — the caller MUST join them OUTSIDE the
    server lock (K-5: the workers' own ``finally`` needs that lock; joining under
    it deadlocks). ``_join_wait_threads`` is the bounded join helper.
    """
    bundle.context.stop.stop()
    bundle.state.stopped = True
    bundle.state.pending_approval_token = None
    if audit_stop_events:
        metadata: dict[str, Any] = {"source": source}
        if audit_metadata:
            metadata.update(audit_metadata)
        for event_type in ("stop", "emergency_stop"):
            try:
                bundle.auditor.emit(
                    event_type,
                    session_id,
                    task_id=bundle.context.task.task_id,
                    result="stopped",
                    metadata=metadata,
                )
            except Exception:
                logger.debug("stop audit failed", exc_info=True)
    with _lock:
        # WP6: cancel-and-detach the session's waits while the teardown is atomic
        # (cancel flags set + registries cleaned under ``_lock``, before the pop).
        cancelled_wait_handles = _cancel_all_waits(session_id)
        # WP7b: release the plan lease HERE — inside the locked teardown, before
        # the bundle is popped. This is the ONE session-end funnel (stop_session,
        # the WP-S idle reaper and the internal kill path all call this), so an
        # active lease can never outlive its session; the guard's release is
        # idempotent (exactly once, no double-release error).
        active_plan_lease = _plan_guard.active_lease(bundle)
        _plan_guard.release(session_id, bundle)
        # WP5: dispose the evidence store HERE, inside the locked teardown and BEFORE
        # the bundle is popped — this is the ONE session-end path (stop_session, the
        # WP-S idle reaper and the internal kill path all funnel through
        # ``_close_stopped_bundle``), so disposal happens exactly once.  Disposal both
        # empties the store object and REMOVES the key from ``extra``, so a suspended
        # tool call resuming after the pop cannot find a store — the resurrection race
        # dies here.  A second call finds no key and releases nothing (exactly-once).
        released_frames = dispose_store(bundle.extra)
        # WP13 DELTA-3 m3 (VAL-WP13-01): stop the backend's UIA COM owner thread
        # (releases the CUIAutomation pointer ON the owner thread +
        # CoUninitialize) — no leaked thread or COM object per session. The
        # sentinel is fire-and-forget (never blocks the teardown); a backend
        # without the hook (test doubles) is skipped.
        backend = getattr(bundle, "backend", None)
        backend_close = getattr(backend, "close", None)
        if callable(backend_close):
            try:
                backend_close()
            except Exception:
                logger.debug("backend close (UIA owner stop) failed", exc_info=True)
        snapshot: dict[str, Any] = {}
        try:
            snapshot = {
                "task_id": bundle.context.task.task_id,
                "step_count": bundle.state.step_count,
                "metrics": bundle.metrics.snapshot(),
            }
        except Exception:
            logger.debug("stopped-session snapshot failed", exc_info=True)
            snapshot = {"task_id": bundle.context.task.task_id}
        if snapshot_extra:
            snapshot.update(snapshot_extra)
        _remember_stopped(session_id, snapshot)
        _bundles.pop(session_id, None)
        _registry.remove(session_id)
    if released_frames:
        logger.debug(
            "evidence store disposed on stop; released %d frame(s)", released_frames
        )
    if active_plan_lease is not None:
        # WP8: a killed session (stop_session / idle reap / internal kill path)
        # ends any active plan as cancelled — audited OUTSIDE the lock, after the
        # reply-shaped teardown decided the stop, with the kill source named.
        # F3/F4: idempotent per lease; the reason is the reply's own
        # failure_reason ("cancelled"), the kill source rides the metadata.
        # DELTA 4 (VAL-WP8-04): when a step is IN FLIGHT the funnel does NOT
        # emit — it records the kill on the lease (under the SAME lock the step's
        # finally uses) and returns; the step's single try/finally closes the
        # lifecycle on every exit, with the step's plan_point already emitted and
        # the final prefix counts.
        # DELTA 5 (Commander review): the defer DECISION is made INSIDE the lock
        # — a check-then-act across it could race the step's finally (funnel
        # reads True, finally clears + sees no kill, funnel then sets
        # kill_deferred on a finished step = zero terminals). The emit itself
        # stays outside the lock (it is fallible and never holds it).
        with _plan_terminal_audit_lock:
            defer = active_plan_lease.step_in_flight
            if defer:
                active_plan_lease.kill_deferred = True
        if not defer:
            _audit_plan_terminal(
                bundle,
                active_plan_lease,
                reply_result="stopped",
                reason="cancelled",
                failure_reason="cancelled",
                source=source,
            )
    return cancelled_wait_handles




# --- WP-S idle-session reaper (Addendum 2 + follow-up, ORVEX-2026-108) ------------------------
# A session the agent started and forgot (or whose stop failed) used to sit in the
# registry forever: SessionRegistry is fail-closed (never evicts), so forgotten
# sessions piled up until start_session was refused permanently. The reaper stops an
# idle session through the SAME kill path as stop_session (``_close_stopped_bundle`` —
# audited, bundle + registry removed, stopped snapshot remembered with
# ``stop_reason``), sweeping lazily at the entry of every tool call and inside
# start_session BEFORE the limit check, so a full registry of forgotten sessions
# frees itself. NOTHING that is actually running is ever reaped: a session with an
# in-flight tool call (or a WP6 blocking wait via ``SessionContext.in_flight_call``)
# is skipped, and its idle clock is re-based to the END of the last call.

#: Env knob: idle timeout in seconds. Default 900 (15 minutes, per the user's
#: follow-up). Band 60..86400; ``0`` = disabled. Out-of-band or unparsable values
#: FALL BACK to the default (fallback-not-clamp, the WP2 knob style) — a typo can
#: never silently arm an aggressive reaper.
SESSION_IDLE_TIMEOUT_ENV = "CORTEX_SESSION_IDLE_TIMEOUT_SECONDS"
SESSION_IDLE_TIMEOUT_DEFAULT_SECONDS = 900.0
SESSION_IDLE_TIMEOUT_MIN_SECONDS = 60.0
SESSION_IDLE_TIMEOUT_MAX_SECONDS = 86400.0


def _session_idle_timeout() -> float:
    """Resolve the idle timeout in seconds: env > default, fallback-not-clamp.

    Unset/empty → default; unparsable, non-finite, or out-of-band (excluding the
    explicit ``0`` = disabled) → default, never clamped into the band.
    """
    raw = os.environ.get(SESSION_IDLE_TIMEOUT_ENV, "").strip()
    if not raw:
        return SESSION_IDLE_TIMEOUT_DEFAULT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return SESSION_IDLE_TIMEOUT_DEFAULT_SECONDS
    if not math.isfinite(value):
        return SESSION_IDLE_TIMEOUT_DEFAULT_SECONDS
    if value == 0.0:
        return 0.0  # explicitly disabled
    if value < SESSION_IDLE_TIMEOUT_MIN_SECONDS or value > SESSION_IDLE_TIMEOUT_MAX_SECONDS:
        return SESSION_IDLE_TIMEOUT_DEFAULT_SECONDS
    return value


def _sweep_idle_sessions(*, exclude: str | None = None) -> list[str]:
    """Stop every session idle past the timeout with nothing in flight; return the ids.

    O(live sessions) ≤ max_sessions (≤ 64), so sweeping lazily at every tool entry is
    cheap and no background thread is needed. Reaping goes through
    ``_close_stopped_bundle`` (the stop_session kill path — audited ``stop`` +
    ``emergency_stop`` with ``source="idle_timeout"`` and ``idle_seconds`` in the
    metadata, stopped snapshot remembered with ``stop_reason="idle_timeout"``).
    ``exclude`` keeps the session the CURRENT call addresses from being judged before
    its own activity is recorded (it is also touched first by :func:`_session_enter`).

    WP-S carry-over (2)+(3)+(4): eligibility is decided ATOMICALLY per session
    (``SessionContext.claim_for_reap`` — in-flight + last activity read together, and
    the claim fences tool entry), the audited/metrics close runs OUTSIDE the server
    lock so one slow audit sink can never stall unrelated tool calls, and each
    individual reap is guarded — one failing reap never aborts the sweep or an
    unrelated session's tool call.
    """
    timeout = _session_idle_timeout()
    if timeout <= 0:
        return []
    reaped: list[str] = []
    with _lock:
        now = time.monotonic()
        candidates: list[tuple[str, _SessionBundle, float]] = []
        for session_id, bundle in list(_bundles.items()):
            if session_id == exclude:
                continue
            try:
                claimed_idle = bundle.context.claim_for_reap(now=now, timeout=timeout)
            except Exception:  # noqa: BLE001 - one broken context never aborts the sweep
                logger.warning("idle-reap eligibility check failed for %s", session_id, exc_info=True)
                continue
            if claimed_idle is not None:
                candidates.append((session_id, bundle, claimed_idle))
    for session_id, bundle, idle in candidates:
        try:
            with _lock:
                current = _bundles.get(session_id)
                if current is not bundle:
                    bundle.context.release_reap_claim()
                    continue  # already stopped/replaced by someone else
            _close_stopped_bundle(
                session_id,
                bundle,
                source="idle_timeout",
                audit_metadata={"idle_seconds": round(idle, 3)},
                snapshot_extra={"stop_reason": "idle_timeout", "idle_seconds": round(idle, 3)},
            )
            reaped.append(session_id)
        except Exception:  # noqa: BLE001 - one failing reap never aborts the sweep
            logger.warning("idle reap of %s failed; session left live", session_id, exc_info=True)
            try:
                bundle.context.release_reap_claim()
            except Exception:  # noqa: BLE001 - defensive
                logger.debug("reap-claim release failed for %s", session_id, exc_info=True)
    if reaped:
        logger.info(
            "Idle-session reaper stopped %d forgotten session(s): %s", len(reaped), reaped
        )
    return reaped


def _live_sessions_payload() -> tuple[list[dict[str, Any]], float]:
    """Snapshot of the live sessions for the ``session_limit_exceeded`` reply.

    Returns ``(entries, effective_idle_timeout)`` with entries sorted MOST-IDLE first
    so an agent that lost its session ids can see exactly what to ``stop_session``.
    """
    timeout = _session_idle_timeout()
    with _lock:
        now = time.monotonic()
        entries = [
            {
                "session_id": session_id,
                "idle_seconds": round(bundle.context.idle_seconds(now), 3),
                "in_flight": bundle.context.in_flight_count(),
                "age_seconds": round(max(0.0, now - bundle.context.created_monotonic), 3),
            }
            for session_id, bundle in _bundles.items()
        ]
    entries.sort(key=lambda entry: (entry["idle_seconds"], entry["age_seconds"]), reverse=True)
    return entries, timeout


def _session_enter(session_id: str) -> tuple[_SessionBundle | None, dict[str, object] | None]:
    """Enter bookkeeping for EVERY session-scoped tool call (WP-S, one helper).

    Touch the session's activity (a call is activity even when it is later rejected
    or refused), mark it in-flight, then return the captured context for the matching
    :func:`_session_exit`. All bookkeeping is under the server lock.

    WP-S carry-over (2)+(3): if a reaper currently HOLDS a claim on this session
    (it was selected as idle-expired and is about to be closed), the call refuses
    with ``session_stopped`` WITHOUT marking busy — the session really did time out,
    and the refusal keeps the in-flight count at zero so the close outside the lock
    can never race a session that just became busy.

    Returns ``(captured_bundle, refusal)``; ``refusal`` is a ready-made
    ``session_stopped`` error payload the decorator returns directly. A unknown
    session returns ``(None, None)`` — the tool body owns the ``unknown_session``
    answer, and no bookkeeping is created for it.
    """
    with _lock:
        entered = _bundles.get(session_id)
        if entered is not None:
            entered.context.touch_activity()
            if entered.context.reap_claimed:
                return None, _error_response(_StoppedSession(session_id))
            entered.context.mark_busy()
    return entered, None


def _session_exit(session_id: str, entered: _SessionBundle | None) -> None:
    """Exit bookkeeping matching :func:`_session_enter` (WP-S, one helper).

    Touch activity again (idle time is measured from the END of the last call) and
    release the in-flight mark on the context CAPTURED AT ENTRY (carry-over 5: the
    decrement always pairs with the increment, even if the bundle was reaped and a
    new context was created under the same id mid-call — that new context is touched
    so it starts its idle window fresh).
    """
    with _lock:
        if entered is not None:
            entered.context.touch_activity()
            entered.context.mark_idle()
        current = _bundles.get(session_id)
        if current is not None and current is not entered:
            current.context.touch_activity()


def _session_scoped(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: wrap a session-scoped tool with WP-S session bookkeeping.

    Applied UNDER ``@mcp.tool()`` so the MCP-registered callable is the wrapper;
    ``functools.wraps`` preserves name/docstring/annotations, and ``inspect.signature``
    resolves through ``__wrapped__``, so the advertised tool schema is unchanged.
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(session_id: str, *args: Any, **kwargs: Any) -> Any:
            entered, refusal = _session_enter(session_id)
            if refusal is not None:
                return refusal
            try:
                _sweep_idle_sessions(exclude=session_id)
                return await fn(session_id, *args, **kwargs)
            finally:
                _session_exit(session_id, entered)

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(session_id: str, *args: Any, **kwargs: Any) -> Any:
        entered, refusal = _session_enter(session_id)
        if refusal is not None:
            return refusal
        try:
            _sweep_idle_sessions(exclude=session_id)
            return fn(session_id, *args, **kwargs)
        finally:
            _session_exit(session_id, entered)

    return wrapper


# --- long-running session wiring (master-mission 003, additive) -----------------------------


def _build_runtime(
    bundle: _SessionBundle,
    *,
    goal: str = "",
    resume_bundle: ResumeBundle | None = None,
) -> LongRunningRuntime:
    """Construct the per-session orchestration runtime over the EXISTING bundle pieces."""
    provider = bundle.agent.provider
    if resume_bundle is not None:
        # Restore the checkpoint's context into a FRESH ContextManager — snapshot/
        # restore is lossless per A3's contract. (The provider-bound summarizer plumbing
        # was removed with the run_goal loop family.)
        context = ContextManager(goal=resume_bundle.goal)
        context.restore(resume_bundle.context.snapshot())
        return LongRunningRuntime(
            session_id=bundle.context.session_id,
            goal=resume_bundle.goal,
            agent=bundle.agent,
            state=bundle.state,
            limits=resume_bundle.limits,
            backend=bundle.backend,
            auditor=bundle.auditor,
            metrics=bundle.metrics,
            checkpoint_manager=_checkpoint_manager,
            provider=provider,
            allowed_processes=list(bundle.extra.get("allowed_processes") or []),
            allowed_windows=list(bundle.state.allowed_windows or []),
            subtasks=resume_bundle.subtasks,
            budget=resume_bundle.budget,
            context=context,
            continuation_of=resume_bundle.continuation_identity,
            expected_environment=resume_bundle.environment,
        )
    # No-resume branch: production start_session only builds the runtime when RESUMING
    # (sole production call site above); this branch survives as the module seam used by
    # tests to arm a runtime on a plain session.
    return LongRunningRuntime(
        session_id=bundle.context.session_id,
        goal=goal,
        agent=bundle.agent,
        state=bundle.state,
        limits=bundle.limits,
        backend=bundle.backend,
        auditor=bundle.auditor,
        metrics=bundle.metrics,
        checkpoint_manager=_checkpoint_manager,
        provider=provider,
        allowed_processes=list(bundle.extra.get("allowed_processes") or []),
        allowed_windows=list(bundle.state.allowed_windows or []),
    )


def _current_environment_from_backend(backend: Any) -> dict[str, Any]:
    """Fresh CURRENT environment reading for resume verification (fail-closed on absence).

    A failed observation returns empty identity fields — the resume verification treats
    missing identity as a mismatch, never as a pass (stale-state enforcement, spec 14).
    """
    try:
        observation = backend.observe()
    except Exception:  # noqa: BLE001 - unusable environment is fail-closed data
        return {}
    info = getattr(observation, "active_window_info", None)
    process = None
    title = None
    if info is not None:
        process = str(info.process_name) if getattr(info, "process_name", None) else None
        title = str(info.title) if getattr(info, "title", None) else None
    if title is None and getattr(observation, "active_window", None):
        title = str(observation.active_window)
    return {"active_process_name": process, "active_window_title": title}


def _resume_error_response(exc: Exception, path: str) -> dict[str, object]:
    """Typed fail-closed resume refusal responses (never a partial restore)."""
    if isinstance(exc, ResumeRefusalError):
        code = "resume_refused"
    elif isinstance(exc, CheckpointValidationError):
        code = "invalid_checkpoint"
    elif isinstance(exc, CheckpointError):
        code = "checkpoint_error"
    else:
        code = type(exc).__name__
    payload: dict[str, object] = {
        "ok": False,
        "error": code,
        "message": str(exc)[:2000],
        "checkpoint": str(path),
    }
    checks = getattr(exc, "checks", None)
    if checks is not None and hasattr(checks, "summary"):
        payload["checks"] = checks.summary()
    return payload


# --- tools ---------------------------------------------------------------------------------


@mcp.tool()
def start_session(
    dry_run: bool = False,
    require_approval: bool = True,
    max_steps: int = 30,
    max_retries_per_action: int = 1,
    min_confidence: float = 0.70,
    allowed_windows: NullableStrList = None,
    allowed_processes: NullableStrList = None,
    limits: NullableNumberMap = None,
    resume_from_checkpoint: NullableStr = None,
    interference: NullableStrMap = None,
    image_delivery: NullableStr = None,
    show_pointer: NullableBool = None,
) -> dict[str, object]:
    """Start a guarded session; per-action approval is enabled by default.

    INTENTIONAL POLICY CHANGE (PERF-004 C5): ``dry_run`` now defaults to False. The
    Session 1 forensics showed the old ``dry_run=True`` default silently produced
    no-op sessions (a full agent turn wasted re-starting). Pass ``dry_run=True``
    explicitly for a no-input validation session; every dry-run result message starts
    with the unmistakable banner ``DRY-RUN (no input dispatched):`` so no client can
    misread a no-op as execution. ``require_approval`` still defaults to True.

    ``allowed_processes`` enforces a process allowlist (P0-G); ``limits`` carries any
    :class:`~computer_use_mcp.limits.Limits` field (validated + clamped, fail-closed on
    unknown names). No API key is required to start (the provider is lazy).

    REM-F weak-model tolerance: ``allowed_processes``/``allowed_windows`` also accept
    a comma/space-separated STRING ("mspaint.exe, notepad.exe") or a single bare
    string, coerced to the documented list at the tool boundary only — the advertised
    schema is the D5-widened flat type-array (array | string | null) so the client-side
    validator lets those shapes through, and ``limits`` stays STRICT
    (fail-closed numeric contract, untouched).

    Interference policy (T8, trailing optional): ``interference`` is a dict of policy
    sections for the Interference Guard (``focus_guard`` / ``attach_or_launch`` /
    ``dialog_sentinel`` / ``focus_continuity`` / ``hotkey_guard``). Every field is
    optional with A12's fail-safe defaults (protective); unknown sections/fields/values
    are REJECTED fail-closed (``invalid_interference``), exactly like ``limits``.
    Callers omitting the parameter get the same protective defaults.

    Long-running additive (master-mission 003, trailing optional): pass
    ``resume_from_checkpoint`` (a checkpoint file path from a previous session) to
    RESUME it as a CONTINUATION — counters are restored (never reset/zeroed), subtasks,
    dependencies, and context are restored, the CURRENT environment is re-verified
    against the checkpoint's expectations (mismatch/invalid checkpoint -> fail-closed
    typed refusal), and approval is FRESH (never resurrected from data). Existing
    callers that omit the parameter are byte-identical. The image delivery mode is
    deliberately NOT checkpointed: a resumed session is a FRESH session governed by
    THIS call's ``image_delivery`` param (or env/default).

    Image delivery (D1, trailing optional): ``image_delivery`` (optional, "image" or
    "text", default "image") controls how screenshots come back from
    computer_observe / computer_execute. JUDGE BY WHAT YOU RECEIVE, not by what
    you think you are: if your inputs arrive as text only (no image parts), pass
    image_delivery="text" — REQUIRED, not optional. Text mode never crashes: it
    returns full metadata (window, cursor, digest, OCR/UI elements, text summary)
    and never an image block; for a vision model it only costs the screenshot
    pixels. One image part in a text-only conversation KILLS the whole session
    PERMANENTLY (the provider rejects the request with a 400 and every later turn
    replays the poisoned image). UNSURE? Pass "text". Vision models that truly
    receive images pass "image" (or omit) for real image blocks.
    Precedence: param > env ``CORTEX_IMAGE_DELIVERY`` ("text"/"image") > default
    "image"; invalid values are rejected fail-closed (``invalid_image_delivery``).

    Pointer sprite (ORVEX-2026-109 WP-C3, trailing optional): ``show_pointer``
    (default ON; env ``CORTEX_SHOW_POINTER`` is the default source, param > env >
    default with a fallback-not-clamp boolean parse) draws the Cortex cursor
    sprite INTO every image-bearing reply (observe/zoom/execute/fetch) at the
    real pointer position — outbound decoration only: captures, digests,
    verification diffs and the stored evidence frames keep the clean pixels.
    Additive ``pointer`` / ``last_click`` keys ride image-bearing replies; pass
    ``show_pointer=False`` here (or ``show_pointer=false`` per computer_observe
    call) for the pre-WP-C byte-identical images.

    Idle-session reaper (WP-S, trailing behavior): sessions idle for more than 15
    minutes (env ``CORTEX_SESSION_IDLE_TIMEOUT_SECONDS``, band 60..86400, ``0`` =
    disabled) with no tool call in flight are STOPPED automatically through the same
    kill path as stop_session; any tool call on a session (including a rejected one)
    resets the count, and idle sessions are swept here BEFORE the session limit is
    checked — a full registry of forgotten sessions frees itself. When the limit is
    still reached, this reply lists the live sessions (``live_sessions``, most idle
    first, with ``idle_seconds``/``in_flight``/``age_seconds`` plus the effective
    ``idle_timeout_seconds``): call stop_session on one of them to free a slot.
    """
    try:
        # D9 precedence (documented): the legacy ``max_retries_per_action`` parameter and
        # the same key inside ``limits`` are mutually exclusive — supplying both is an
        # explicit conflict (ValueError). When only one is supplied, it applies.
        if limits and "max_retries_per_action" in limits and max_retries_per_action != 1:
            raise ValueError(
                "max_retries_per_action was supplied both as the start_session parameter "
                "and inside the limits dict; pass it in exactly one place."
            )
        limits_obj = _parse_limits(limits)
        if not limits:
            # Legacy parameter only: it applies (harmonized into Limits, one source).
            limits_obj = Limits(max_retries_per_action=max_retries_per_action).validate()
        elif "max_retries_per_action" not in limits:
            # Limits dict without the retry key + legacy parameter supplied: param wins.
            limits_obj = replace(
                limits_obj, max_retries_per_action=max_retries_per_action
            ).validate()
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": "invalid_limits", "message": str(exc)}
    try:
        interference_policy = parse_interference(interference)
    except (TypeError, ValueError) as exc:
        # Fail-closed policy parsing (mirrors ``invalid_limits``): a malformed policy
        # can never silently weaken the protective defaults.
        return {"ok": False, "error": "invalid_interference", "message": str(exc)}
    # D1: normalize + resolve the image delivery mode BEFORE any session exists so a
    # bad value leaks no registry slot; a typo silently meaning "image" would re-arm
    # the non-vision session killer, so garbage is REJECTED fail-closed (REM-F).
    try:
        image_mode = _resolve_image_delivery(_normalize_image_delivery_param(image_delivery))
    except ValueError as exc:
        return {"ok": False, "error": "invalid_image_delivery", "message": str(exc)}
    # DELTA 1 MINOR 5: ``show_pointer`` is a STRICT boolean or None — rejected
    # fail-closed before any session exists (the ``invalid_image_delivery``
    # precedent; the MCP boundary already rejects non-bools for MCP callers).
    if show_pointer is not None and not isinstance(show_pointer, bool):
        return {
            "ok": False,
            "error": "invalid_show_pointer",
            "message": (
                "show_pointer must be a boolean or null (null = the env/default); "
                f"got {show_pointer!r} ({type(show_pointer).__name__})."
            ),
        }
    # REM-F: normalize the tolerant allowlist shapes for DIRECT callers too (the
    # boundary coercion already produced lists for MCP callers — idempotent here).
    try:
        allowed_windows = _coerce_tolerant_str_list(allowed_windows)
        allowed_processes = _coerce_tolerant_str_list(allowed_processes)
    except ValueError as exc:
        return {"ok": False, "error": "invalid_limits", "message": str(exc)}
    try:
        # WP-S: sweep idle sessions BEFORE the limit check — a full registry of
        # forgotten sessions frees itself here instead of refusing forever.
        _sweep_idle_sessions()
        context = _registry.create()
    except SessionLimitExceeded as exc:
        # WP-S: the refusal lists the live sessions (most idle first) so an agent
        # that lost its session ids can stop_session one to free a slot. Additive
        # keys only — ok/error/max_sessions unchanged.
        live_sessions, idle_timeout = _live_sessions_payload()
        message = (
            f"{exc} Live sessions are listed in ``live_sessions`` (most idle first); "
            "call stop_session on one of them to free a slot."
        )
        if idle_timeout > 0:
            message += (
                f" Sessions idle past {idle_timeout:g}s with nothing in flight are "
                "stopped automatically."
            )
        return {
            "ok": False,
            "error": "session_limit_exceeded",
            "message": message,
            "max_sessions": exc.max_sessions,
            "live_sessions": live_sessions,
            "idle_timeout_seconds": idle_timeout,
        }
    session_id = context.session_id
    backend: Any = None  # N10 (VAL-WP13-03): bound BEFORE the try — the except
    # handler below runs even when SessionState(...) or _backend_factory()
    # raises, and must never read an unbound name.
    try:
        state = SessionState(
            session_id=session_id,
            dry_run=dry_run,
            require_approval=require_approval,
            max_steps=max_steps,
            max_retries_per_action=max_retries_per_action,
            min_confidence=min_confidence,
            allowed_windows=allowed_windows or [],
        )
        backend = _backend_factory()
        enforcer = LimitEnforcer(limits_obj)
        auditor = AuditLogger(_audit_root() / session_id)
        metrics = Metrics()
        agent = ComputerUseAgent(
            backend=backend,
            provider=_LazyProvider(_provider_factory),
            safety=SafetyPolicy(),
            session_id=session_id,
            task=context.task,
            stop=context.stop,
            limits=limits_obj,
            enforcer=enforcer,
            auditor=auditor,
            metrics=metrics,
            allowed_processes=allowed_processes or [],
            interference=interference_policy,
        )
        # R-6 encode-skip: in a text session NO image block is ever emitted (D1), so
        # the lossless PNG payload exists only to be hashed (digest) and compared
        # (staleness). Arming ``_raw_payload_keys`` makes the backend produce its
        # deterministic raw-pixel key instead — byte-for-byte identical semantics for
        # every consumer (digest, equality, verification fast path), minus the encode.
        # Guarded on the backend side by ``_raw_key_enabled`` (frame-reuse on AND
        # ``CORTEX_TEXT_PNG=0``); any other backend simply ignores the attribute.
        if image_mode == "text":
            try:
                backend._raw_payload_keys = True  # noqa: SLF001 - session-owned backend
            except Exception:
                logger.debug("backend rejected raw payload keys; text session keeps PNGs")
    except Exception as exc:  # noqa: BLE001 - structured error, no traceback
        # WP13 DELTA-4 N3 (VAL-WP13-02): the backend (and its UIA COM owner
        # thread) may already be built when this start failed — stop it exactly
        # like every other session-end path. N10 (VAL-WP13-03): ``backend`` is
        # None when SessionState(...) or _backend_factory() itself raised, and
        # ``_registry.remove`` runs WHATEVER close() does — an invalid
        # start_session must always return the typed error AND free its
        # registry slot (4 orphan slots would lock out every new session).
        backend_close = getattr(backend, "close", None)
        if callable(backend_close):
            try:
                backend_close()
            except Exception:
                logger.debug("backend close on failed start raised", exc_info=True)
        try:
            _registry.remove(session_id)
        except Exception:
            logger.debug("registry remove on failed start raised", exc_info=True)
        return _error_response(exc)
    with _lock:
        _bundles[session_id] = _SessionBundle(
            context=context,
            state=state,
            backend=backend,
            agent=agent,
            enforcer=enforcer,
            auditor=auditor,
            metrics=metrics,
            limits=limits_obj,
            extra={
                "allowed_processes": list(allowed_processes or []),
                "max_retries_per_action": max_retries_per_action,
                "interference_policy": interference_policy,
                "image_delivery": image_mode,
                # WP-C3 (ORVEX-2026-109): the session's pointer-sprite default —
                # param > env CORTEX_SHOW_POINTER > True (fallback-not-clamp parse).
                "show_pointer": resolve_show_pointer(show_pointer),
                # WP5: the evidence store is created HERE, once, next to the
                # image_delivery key — never lazily on first use.  ``extra`` is an
                # unsynchronised dict that is never copied, so a lazily-created key
                # could be re-created by a tool call resuming after a teardown already
                # popped the bundle from ``_bundles``.  A missing key therefore means
                # "this session is gone", and every evidence hook treats it as exactly
                # that: it writes nothing and creates nothing.
                EVIDENCE_STORE_KEY: new_store(session_id, image_delivery=image_mode),
            },
        )
    try:
        auditor.emit(
            "session_start",
            session_id,
            task_id=context.task.task_id,
            result="ok",
            metadata={
                "dry_run": dry_run,
                "require_approval": require_approval,
                "allowed_processes": list(allowed_processes or []),
                "limits": str(limits_obj),
                "interference": str(interference_policy),
                "image_delivery": image_mode,
            },
        )
    except Exception:
        logger.debug("session_start audit failed", exc_info=True)
    resume_extra: dict[str, object] = {}
    if resume_from_checkpoint:
        # Long-running resume (conflict C4): the live session gets a NEW session id and
        # carries the checkpoint's original id as continuation identity; counters are
        # restored (never reset), and the environment is re-verified BEFORE continuing.
        bundle = _bundles[session_id]
        resume_bundle: ResumeBundle | None = None
        try:
            current_environment = _current_environment_from_backend(backend)
            resume_bundle = _resume_manager.prepare(
                resume_from_checkpoint, current_environment=current_environment
            )
            runtime = _build_runtime(bundle, goal="", resume_bundle=resume_bundle)
        except Exception as exc:  # noqa: BLE001 - typed refusal, never a partial restore
            with _lock:
                # WP5: the bundle was already created (with its evidence store), so a
                # failed resume start is a session END — dispose the store exactly
                # once before the bundle is dropped, like every other end path.
                dispose_store(bundle.extra)
                # WP13 DELTA-4 N3 (VAL-WP13-02): stop the backend's UIA COM owner
                # thread too — never let close() raise into the refusal path.
                resume_backend_close = getattr(backend, "close", None)
                if callable(resume_backend_close):
                    try:
                        resume_backend_close()
                    except Exception:
                        logger.debug("backend close on failed resume raised", exc_info=True)
                # WP7b (defensive): a fresh session cannot carry a plan lease (no
                # plan call has run yet), but the release is idempotent and keeps
                # the lease lifecycle total across every bundle-drop path.
                _plan_guard.release(session_id, bundle)
                _bundles.pop(session_id, None)
            _registry.remove(session_id)
            return _resume_error_response(exc, resume_from_checkpoint)
        assert resume_bundle is not None
        bundle.extra["long_running"] = runtime
        # Continuation doctrine: the checkpoint's OWN (re-clamped) limits stay in force;
        # the per-run enforcer/executor adopt them too — no budget is ever enlarged.
        bundle.limits = runtime.limits
        enforcer.limits = runtime.limits
        agent.limits = runtime.limits
        # Adopt the checkpointed session settings (the resumed session IS the old one).
        state.dry_run = resume_bundle.session.dry_run
        state.require_approval = resume_bundle.session.require_approval
        state.max_steps = resume_bundle.session.max_steps
        state.max_retries_per_action = resume_bundle.session.max_retries_per_action
        state.min_confidence = resume_bundle.session.min_confidence
        bundle.context.task.goal = runtime.goal
        limits_obj = runtime.limits
        resume_extra = {
            "resumed": True,
            "continuation_of": resume_bundle.continuation_identity,
            "resumed_from_checkpoint": str(resume_from_checkpoint),
        }
        try:
            auditor.emit(
                "resume",
                session_id,
                task_id=context.task.task_id,
                result="ok",
                metadata={
                    "continuation_of": resume_bundle.continuation_identity,
                    "checkpoint": str(resume_from_checkpoint),
                },
            )
        except Exception:
            logger.debug("resume audit failed", exc_info=True)
    payload = state.model_dump()
    # DELTA 12 (Z3-a, VAL-WP13-08): the EFFECTIVE action cap is
    # min(max_steps, max_actions) — `step_count` counts every dispatched action
    # (follow_ups items and plan actions included) and safety refuses at
    # `step_count >= max_steps`.
    effective_cap = min(state.max_steps, limits_obj.max_actions)
    binding = "max_steps" if state.max_steps <= limits_obj.max_actions else "max_actions"
    caps_differ = state.max_steps != limits_obj.max_actions
    payload.update(
        {
            "allowed_processes": list(allowed_processes or []),
            "limits": str(limits_obj),
            "task_id": context.task.task_id,
            # D1: the mode is visible in the transcript right after creation.
            "image_delivery": image_mode,
            # WP13 DELTA 1 #7 (field A): the driver SEES the effective budget.
            # ``message`` is a contract field on EVERY reply (REPLY-CONTRACT.md
            # envelope); the key=value suffix form is the sanctioned channel —
            # no new top-level field. DELTA 11 (Z3, sess_138fbd60): the caps are
            # stated in the same line. DELTA 12 (Z3-a): the EFFECTIVE cap is
            # stated with the binding limit named, and the caps-differ WARNING
            # fires in either direction (the DEFAULT session is capped at 30,
            # not 100).
            "message": (
                f"Session started. client_timeout_s={client_timeout_seconds():g} "
                "(the host's per-tool-call timeout: the wait segment, the type "
                "deadline and the pacing budget derive from it). "
                f"Effective action cap = min(max_steps={state.max_steps}, "
                f"max_actions={limits_obj.max_actions}) = {effective_cap}"
                f" ({binding} binds) — every dispatched action counts (each"
                " follow_ups item and plan action included); raise max_steps via"
                f" the start_session parameter (band {STEPS_BAND[0]}..{STEPS_BAND[1]})"
                " and max_actions via"
                f' limits={{"max_actions": N}} (band {ACTIONS_BAND[0]}..{ACTIONS_BAND[1]}).'
                + (
                    f" WARNING: the caps differ — {binding} binds, so the"
                    " session is refused at action"
                    f" {effective_cap + 1}."
                    if caps_differ
                    else ""
                )
            ),
        }
    )
    if image_mode == "text":
        payload["image_delivery_note"] = IMAGE_DELIVERY_TEXT_NOTE
    payload.update(resume_extra)
    return payload


@mcp.tool()
@_session_scoped
async def stop_session(session_id: str) -> dict[str, object]:
    """Arm the kill path: no further input is performed after this returns.

    Shape-compatible with the legacy tool; it now arms the thread-safe StopToken checked
    before every physical input, at every loop checkpoint, and inside waits, and it
    audits stop + emergency_stop events. The session bundle is closed and removed from
    the registry (D1): subsequent tool calls fail closed with ``session_stopped``.
    (DELTA 1/F4: the tool is ASYNC — its bounded wait-worker join runs OFF the loop
    thread via ``asyncio.to_thread`` — with the same parameters and reply shape.)

    Idle-session reaper (WP-S): the same kill path stops a session automatically after
    15 minutes without any tool call on it (env
    ``CORTEX_SESSION_IDLE_TIMEOUT_SECONDS``, band 60..86400, ``0`` = disabled; ANY
    call — even a rejected one — resets the count; a call in flight is never
    interrupted and re-bases the idle clock to its END). An idle-reaped session is
    audited with ``source="idle_timeout"`` and later calls say it was stopped for
    inactivity after N seconds. Hitting the session limit? start_session's refusal
    lists the live sessions (most idle first) so you can stop_session one.
    """
    bundle = _bundles.get(session_id)
    if bundle is None:
        if session_id in _stopped_sessions:
            # Idempotent re-stop: shape preserved, nothing left to stop.
            return {
                "ok": True,
                "session_id": session_id,
                "message": "Session already stopped.",
                "task_id": str(_stopped_sessions[session_id].get("task_id", "")),
                "termination_reason": None,
            }
        return _error_response(_UnknownSession(session_id))
    # Long-running lifecycle trigger (spec section 7): checkpoint BEFORE the session
    # ends. Best effort — a checkpoint write failure never blocks the kill path.
    runtime = bundle.extra.get("long_running")
    if isinstance(runtime, LongRunningRuntime):
        try:
            runtime.checkpoint(CheckpointTrigger.BEFORE_SESSION_END)
        except Exception:  # durability failure must not block stopping
            logger.debug("before_session_end checkpoint failed", exc_info=True)
    # D1/F7: shared kill-path cleanup — bundle store and registry stay consistent.
    # WP6: the cancelled waits are joined so no worker thread survives the stop.
    # DELTA 1/F4: the tool is ASYNC and the bounded join (S-16, 5 s cap) runs OFF
    # the loop thread via asyncio.to_thread — starvation-safe because the F1
    # loop-signal design never uses the default executor for waits, so one short
    # bounded join thread can never delay a wait wake.
    cancelled_wait_handles = _close_stopped_bundle(session_id, bundle, source="stop_session")
    if cancelled_wait_handles:
        await asyncio.to_thread(_join_wait_threads, cancelled_wait_handles)
    return {
        "ok": True,
        "session_id": session_id,
        "message": "Session stopped before the next action.",
        "task_id": bundle.context.task.task_id,
        "termination_reason": bundle.context.task.termination_reason.value
        if bundle.context.task.termination_reason
        else None,
    }
