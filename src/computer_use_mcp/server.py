"""MCP server wiring: 5 deterministic tools, bounded session registry, kill path, audit, limits.

Compatibility contract (master-mission section 6, binding, as amended 2026-09 by user
order — the internal-LLM loop tool family is REMOVED PERMANENTLY):

- The exposed tool surface is exactly FIVE deterministic tools: ``start_session``,
  ``stop_session``, ``computer_observe``, ``computer_screenshot``,
  ``computer_execute``. The internal-LLM loop tool family is deleted — the
  host model drives the tools directly; no model-decides loop remains in Cortex.
  [AMENDMENT A1 (AL-002, owner addendum 2026-09-18): a SIXTH additive tool
  ``computer_zoom`` joins the observation family — observation-only (no action
  surface), fresh capture per call, fail-closed region validation; it changes no
  pre-existing tool's default behavior.]
- Tool names, stdio transport, and parameter positions are preserved; signatures
  gain TRAILING OPTIONAL params only (``start_session(..., allowed_processes=None,
  limits=None)``, ``computer_execute(..., expected_effect=None, include_screenshot_after=None,
  follow_ups=None)``).
- Existing return shapes keep their top-level keys; new fields are additive only.
- SANCTIONED DEFAULT CHANGE (PERF-004 C5, documented intentional policy change):
  ``start_session`` now defaults to ``dry_run=False`` (Session 1 forensics: the old
  ``dry_run=True`` default silently produced no-op sessions that cost a full agent
  turn). ``require_approval`` still defaults to True. Every dry-run result message
  starts with the unmistakable banner ``DRY-RUN (no input dispatched):``.
- ``computer_execute`` keeps the hardcoded ``confidence=1.0`` — redefined as the
  client-asserted MODEL confidence for a direct caller action; grounding confidence,
  staleness, risk classification, approval, and verification apply independently.
  ``include_screenshot_after=False`` (additive) omits the heavy
  ``screenshot_after_base64`` from the response; omitted or None ships the DEFAULT
  image as a HALF-RESOLUTION JPEG (0.5x q60 — full resolution via an
  explicit ``include_screenshot_after=true``, ``computer_observe``, or
  ``CORTEX_ACTION_IMAGE_FULL=1``). ``follow_ups`` (additive, max 5) queues actions
  that each pass the FULL independent pipeline; the queue stops only on genuinely
  blocking conditions (rejection, safety, approval, attached-window staleness —
  ordinary pixel changes from earlier items do not stop it; dispatch
  error) — an executed item's uncertain OR failed verification verdict rides its
  per-item entry while the batch continues (``CORTEX_QUEUE_STRICT_VERIFY=1``
  restores stop-on-failed, ``CORTEX_QUEUE_STRICT_DIGEST=1`` the whole-screen stop).
- ``stop_session`` keeps its signature/return shape; it now arms the thread-safe
  StopToken kill path and audits stop + emergency_stop.
- ``start_session`` succeeds with no API key (provider construction is lazy at the first
  model call — compat decision 5; dry-run usable).
- REM-F weak-model boundary tolerance (ORVEX-CORTEX-055, live-test hardening):
  ``x``/``y``/``x2``/``y2`` accept integral floats and numeric strings and ROUND
  non-integral values; ``follow_ups`` accepts a JSON-encoded string / single dict /
  JSON-string entries; ``allowed_processes``/``allowed_windows`` accept
  comma/space-separated or bare strings. All via Annotated BeforeValidator
  coercions PRE-validation — the advertised schemas stay "integer"/"array", the
  downstream internal models stay strict, and garbage still fails typed.
  ``limits`` is deliberately NOT made tolerant (fail-closed numeric contract).

- Image delivery (ORVEX-CORTEX-056-LIVEFIX, D1, trailing optional):
  ``start_session(..., image_delivery=None)`` — "image" (default) keeps real image
  blocks; "text" suppresses every OUTBOUND image block so non-vision models stay
  alive (one ImageContent in their history kills the provider request with a 400).
  The start_session docstring teaches judging by WHAT THE MODEL RECEIVES (not
  self-identity): text inputs → pass "text" (required); unsure → "text" (a vision
  model in text mode only loses pixels, a text-only model in image mode dies).
  Precedence: param > env ``CORTEX_IMAGE_DELIVERY`` (fail-safe: only exact
  "text"/"image" honored, garbage/unset → "image") > default "image"; an invalid
  param value fails closed with ``invalid_image_delivery`` BEFORE any session is
  created; ``include_screenshot_after`` can only remove bytes, never re-add them.

Test/extension seam (for E6/E7): the module-level ``_backend_factory`` and
``_provider_factory`` callables are invoked once per ``start_session``; tests monkeypatch
them to inject ``FakeComputerBackend``/scripted providers, and read session wiring via
``_get_bundle(session_id)`` (agent, state, backend, enforcer, auditor, metrics).
"""

from __future__ import annotations

import asyncio
import base64
import functools
import inspect
import io
import json
import logging
import math
import os
import tempfile
import threading
import time
import uuid
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from dataclasses import fields as dataclass_fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
from PIL import Image
from pydantic import BeforeValidator

from .agent import ComputerUseAgent, execute_result_token, rejection_reasons
from .audit import AuditLogger, Metrics
from .backend import ComputerBackend, LocalComputerBackend
from .checkpoint_manager import (
    CheckpointError,
    CheckpointManager,
    CheckpointTrigger,
    CheckpointValidationError,
)
from .context_manager import ContextManager
from .evidence import (
    EVIDENCE_REFUSE_DISPOSED,
    EVIDENCE_STORE_KEY,
    FETCH_FRESHNESS_NOTE,
    RETIRE_AGE_CAP,
    RETIRE_BYTE_CAP,
    RETIRE_COUNT_CAP,
    EvidenceLookup,
    EvidenceStore,
    dispose_store,
    evidence_store,
    new_store,
    register_frame,
)
from .interference import parse_interference
from .limits import LimitEnforcer, LimitExceeded, Limits
from .long_running import LongRunningRuntime
from .models import (
    MAX_FOLLOW_UPS,
    ActionSpec,
    CoordinateSpace,
    GroundedAction,
    SessionState,
    ok_from_result,
)
from .observation import observation_text_summary
from . import plan as plan_layer
from .plan import (
    PLAN_EVIDENCE_KEY,
    PLAN_FAILURE_REASONS,
    PlanLease,
    PlanLeaseGuard,
    plan_failure_reason,
)
from .provider import OpenAICompatibleVisionProvider
from .redaction import redact_text
from .resume_manager import ResumeBundle, ResumeManager, ResumeRefusalError
from .safety import SafetyPolicy
from .state import SessionContext, SessionLimitExceeded, SessionRegistry, TaskStopped
from .visual_views import (
    DEFAULT_GRID_DENSITY,
    InvalidVisualViewError,
    VisualViewError,
    ViewDeriveError,
    crop_observation_frame,
    encode_png_b64,
    observation_for_payload,
    outbound_view_scale,
    render_crop_grid,
    resolve_density,
    resolve_view,
    spatial_text_of,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)
mcp = FastMCP("Cortex")


# --- module wiring (test seam: monkeypatch the factories) ---------------------------------

def _default_max_sessions() -> int:
    try:
        return Limits().max_sessions
    except Exception:  # noqa: BLE001 - defensive default
        return 4


def _audit_root() -> Path:
    """Root directory for per-session audit logs (env override honored)."""
    override = os.getenv("COMPUTER_USE_MCP_LOG_DIR")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "cortex" / "logs"


_backend_factory: Callable[[], ComputerBackend] = LocalComputerBackend
_provider_factory: Callable[[], Any] = OpenAICompatibleVisionProvider
_registry = SessionRegistry(max_sessions=_default_max_sessions())
_bundles: dict[str, _SessionBundle] = {}
# D1: stopped sessions are remembered (bounded) so later tool calls fail closed with a
# precise ``session_stopped`` error instead of a live bundle leaking in memory forever.
_stopped_sessions: dict[str, str] = {}
_STOPPED_SESSION_MEMORY = 1024
_lock = threading.RLock()
# Long-running session persistence (master-mission 003): one shared checkpoint store
# (env-var override honored) and the resume manager built on top of it.
_checkpoint_manager = CheckpointManager()
_resume_manager = ResumeManager(_checkpoint_manager)


class _UnknownSession(KeyError):
    """Raised when a tool is called with a session id that does not exist."""


class _StoppedSession(LookupError):
    """Raised when a tool targets a session that has been stopped (D1, fail-closed)."""


def _remember_stopped(session_id: str, snapshot: dict[str, Any]) -> None:
    """Record a stopped session's id + minimal snapshot (bounded; oldest evicted first).

    The snapshot lets stopped-session tool responses keep their standard shapes
    (task_id / step_count / metrics) even though the live bundle is gone (D1).
    """
    _stopped_sessions[session_id] = snapshot
    while len(_stopped_sessions) > _STOPPED_SESSION_MEMORY:
        _stopped_sessions.pop(next(iter(_stopped_sessions)))


class _LazyProvider:
    """Defers provider construction to the first model call (compat decision 5).

    Construction failures (legacy providers raising without an API key) are remembered
    and surface as a fail-closed error at judge-time (the only live model surface,
    ``judge_change``) instead of breaking start_session.
    """

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._provider: Any | None = None
        self._unavailable: str | None = None

    def _resolve(self) -> Any:
        if self._provider is None and self._unavailable is None:
            try:
                self._provider = self._factory()
            except Exception as exc:  # noqa: BLE001 - unavailability is a planned state
                self._unavailable = f"{type(exc).__name__}: {exc}"
                logger.warning("Vision provider unavailable (fail-closed): %s", self._unavailable)
        if self._provider is None:
            raise RuntimeError(f"Vision provider unavailable (fail-closed): {self._unavailable}")
        return self._provider

    def judge_change(self, before_b64: str, after_b64: str, expected_effect: str) -> Any:
        provider = self._resolve()
        delegate = getattr(provider, "judge_change", None)
        if not callable(delegate):
            raise TypeError(
                "Provider does not expose judge_change; model-based verification is unavailable."
            )
        return delegate(before_b64, after_b64, expected_effect)


@dataclass
class _SessionBundle:
    """Everything isolated per session: state, backend, agent, limits, audit, metrics."""

    context: SessionContext
    state: SessionState
    backend: ComputerBackend
    agent: ComputerUseAgent
    enforcer: LimitEnforcer
    auditor: AuditLogger
    metrics: Metrics
    limits: Limits
    extra: dict[str, Any] = field(default_factory=dict)


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
        # F3/F4: idempotent per lease (an in-step TaskStopped racing this funnel
        # emits first and wins); the reason is the reply's own failure_reason
        # ("cancelled"), the kill source rides the metadata.
        _audit_plan_terminal(
            bundle,
            active_plan_lease,
            reply_result="stopped",
            reason="cancelled",
            failure_reason="cancelled",
            source=source,
        )
    return cancelled_wait_handles


def _error_response(exc: Exception) -> dict[str, object]:
    """Structured, traceback-free error payload for typed failures."""
    if isinstance(exc, TaskStopped):
        code = "task_stopped"
    elif isinstance(exc, LimitExceeded):
        code = "limit_exceeded"
    elif isinstance(exc, SessionLimitExceeded):
        code = "session_limit_exceeded"
    elif isinstance(exc, _StoppedSession):
        code = "session_stopped"
    elif isinstance(exc, _UnknownSession):
        code = "unknown_session"
    else:
        code = type(exc).__name__
    payload: dict[str, object] = {"ok": False, "error": code, "message": str(exc)}
    if isinstance(exc, LimitExceeded):
        payload["limit"] = exc.limit_name
    if isinstance(exc, (_UnknownSession, _StoppedSession)):
        payload["session_id"] = str(exc.args[0]) if exc.args else ""
    if isinstance(exc, _StoppedSession):
        payload["message"] = _stopped_session_message(str(exc.args[0]) if exc.args else "")
    return payload


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


def _stopped_session_message(session_id: str) -> str:
    """The ``session_stopped`` message, naming the inactivity stop when the reaper did it."""
    base = "Session is stopped; refusing to proceed (fail-closed stopped-session policy)."
    snapshot = _stopped_sessions.get(session_id)
    if isinstance(snapshot, dict) and snapshot.get("stop_reason") == "idle_timeout":
        return (
            f"{base} This session was stopped automatically for inactivity "
            f"(idle_timeout) after {snapshot.get('idle_seconds')} seconds without "
            "any tool call on it."
        )
    return base


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


def _redact_result_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """F2: the MCP response path is redacted too (defense in depth).

    The audit sink and the provider payload are already redaction-enforced; until now
    the tool RESPONSE was the one unredacted surface (action text/reason, result
    messages, verification notes/evidence echo provider-proposed strings verbatim).
    The calling client supplied the goal, so this is same-party data — but the
    response path must not be the surface that bypasses redaction.
    """
    if payload.get("message"):
        payload["message"] = redact_text(str(payload["message"]))[0]
    action = payload.get("action")
    if isinstance(action, dict):
        if action.get("text"):
            action["text"] = redact_text(str(action["text"]))[0]
        if action.get("reason"):
            action["reason"] = redact_text(str(action["reason"]))[0]
    verification = payload.get("verification")
    if isinstance(verification, dict):
        if verification.get("note"):
            verification["note"] = redact_text(str(verification["note"]))[0]
        evidence = verification.get("evidence")
        if isinstance(evidence, list):
            verification["evidence"] = [redact_text(str(item))[0] for item in evidence]
    return payload


def _redact_queue_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """F2 defense-in-depth for PERF-004 C7 queue entries: redact text at the sink.

    Delegates to the plan layer's ``redact_queue_entry`` so the flat-queue and plan
    sinks cannot drift.
    """
    return plan_layer.redact_queue_entry(entry)


# --- REM-A outbound image budget + content-block parity (master-mission Phase 2) ---------------

#: Default budget for ONE outbound image block, in KB (H7). The host inline limit is
#: ~200KB; the default keeps the encoded image comfortably under it.
RESULT_IMAGE_MAX_KB_DEFAULT = 180

#: Env knob name: ``CORTEX_RESULT_IMAGE_MAX_KB`` bounds the OUTBOUND image only —
#: observe and execute paths share the same budget; internal captures,
#: verification pixel-diff, and checkpoints keep PNG exactly as today.
RESULT_IMAGE_MAX_KB_ENV = "CORTEX_RESULT_IMAGE_MAX_KB"

#: Cap on ``ocr_text``/``ui_elements`` entries serialized into the observe metadata
#: text (H8): the internal Observation model keeps the full lists.
OBSERVE_METADATA_ELEMENT_CAP = 20


def _result_image_max_bytes() -> int:
    """Resolve the outbound image budget in BYTES; invalid values fall back to default."""
    raw = os.environ.get(RESULT_IMAGE_MAX_KB_ENV, "").strip()
    try:
        kb = float(raw) if raw else RESULT_IMAGE_MAX_KB_DEFAULT
    except (TypeError, ValueError):
        kb = RESULT_IMAGE_MAX_KB_DEFAULT
    if kb <= 0 or kb != kb:  # non-positive or NaN -> fail-safe default
        kb = RESULT_IMAGE_MAX_KB_DEFAULT
    kb = min(kb, 100_000.0)
    return int(kb * 1024)


# --- D1 image delivery (ORVEX-CORTEX-056-LIVEFIX) --------------------------------------------
# A NON-VISION model receiving ONE ImageContent block anywhere in the session
# history gets the ENTIRE provider request rejected with a 400 ('content' must
# be a string) — the turn dies and every later turn replays the poisoned image.
# The delivery mode must therefore be decided BEFORE the first image is emitted
# (start_session is the earliest and natural point) and may not be re-enabled
# per-call afterwards (``include_screenshot_after`` can only REMOVE bytes, never
# add them back). Precedence (F-2 confirmed no client→server model-identity
# signal exists on kimi 0.42.0): start_session param > env > default "image".

#: Env knob name: ``CORTEX_IMAGE_DELIVERY`` — "text" makes ALL sessions text-mode
#: by default (deterministic fallback for users whose default model is
#: non-vision); "image" restores pixel delivery. Fail-safe: ONLY exact (trimmed,
#: case-insensitive) "text"/"image" are honored; unset/empty/garbage → "image"
#: (today's status quo — a typo can never arm the killer accidentally).
IMAGE_DELIVERY_ENV = "CORTEX_IMAGE_DELIVERY"

#: The bounded constant note shipped in every text-mode response (D1).
IMAGE_DELIVERY_TEXT_NOTE = "image_delivery=text: screenshot suppressed (non-vision-safe); metadata only"


def _normalize_image_delivery_param(value: Any) -> str | None:
    """REM-F tolerant normalization of the ``image_delivery`` param.

    None → None (caller resolves via env/default); a string is trimmed +
    casefolded and accepted when it equals "image" or "text". Anything else
    raises a teaching ValueError — a typo silently meaning "image" would re-arm
    the D1 killer, so garbage is REJECTED fail-closed, never defaulted.
    """
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in ("image", "text"):
            return normalized
    raise ValueError(
        f"image_delivery must be \"image\" or \"text\" (got {value!r}). "
        "Pass \"text\" if you cannot view images (non-vision model); "
        "\"image\" if you can."
    )


def _resolve_image_delivery(explicit: str | None) -> str:
    """Resolve the effective delivery mode: param > env > default "image".

    The env layer mirrors ``CORTEX_ATTACH_OR_LAUNCH`` parsing (fail-safe): only
    exact trimmed/casefolded "text"/"image" are honored; unset/empty/garbage
    falls back to "image" — the env knob can save text sessions but can never
    silently blind vision sessions. Single seam: a FUTURE host/protocol
    model-identity signal (none exists today — F-2 verdict) slots in between
    the param and env layers with a one-branch change.
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get(IMAGE_DELIVERY_ENV, "").strip().casefold()
    if raw in ("text", "image"):
        return raw
    return "image"


def _session_image_delivery(bundle: "_SessionBundle") -> str:
    """The session's effective image delivery mode (default "image")."""
    mode = bundle.extra.get("image_delivery")
    return "text" if mode == "text" else "image"


def _outbound_image_scale(
    served_width: int, served_height: int, source_width: int, source_height: int
) -> float | None:
    """The linear scale an OUTBOUND copy was served at (§1.1 D-3, measured).

    Mirrors the AL-002 reference implementation
    (:func:`computer_use_mcp.visual_views.outbound_view_scale`) — the same ``min`` of
    the two axis ratios and the same 4-decimal rounding. §1.1 (MINOR-b, Commander
    decision): ``None`` means NO downscale was applied (raw pass-through, bytes served
    as captured); a float is returned only for a real downscale (always < 1). No pixel
    decode: the caller passes the dimensions the ladder already had in hand.
    """
    if source_width <= 0 or source_height <= 0:
        return None
    ratio = round(min(served_width / source_width, served_height / source_height), 4)
    return None if ratio >= 1.0 else ratio


#: The ONLY outbound-truth scale key on every image-bearing reply (ORVEX-2026-108
#: §1.1 D-3 — one key, one meaning; the legacy ``image_scale`` key is deleted). It
#: carries the scale the image ladder actually applied to the delivered copy, so a
#: caller can map a coordinate read off the served picture back to the capture.
#: Deliberately NOT ``visual_view_scale``: that key belongs to AL-002 and means
#: "the scale of the DERIVED VIEW the host explicitly asked for" (``None`` for a
#: pass-through raw view) — reusing it here would change an existing key's meaning.
IMAGE_OUTBOUND_SCALE_KEY = "image_outbound_scale"


def _bound_outbound_image(data_b64: str, frame: Any = None) -> tuple[str, str, float | None]:
    """Return ``(base64, mimeType, appliedScale)`` for the OUTBOUND copy, under the
    size budget (§1.1 D-3: the scale is REPORTED, not discarded).

    H7: the outbound PNG travels as-is while it fits the budget; an oversized PNG is
    re-encoded as JPEG (quality 85, no alpha channel) and, if still over budget,
    progressively downscaled by 0.85 steps until it fits. INTERNAL pipeline bytes are
    never touched: captures, verification pixel-diff, and checkpoints keep PNG exactly
    as today. Degradation (PIL failure) returns the original — an oversized real image
    beats none.

    The ladder's arithmetic and the bytes it produces are UNCHANGED by the third return
    value: the scale is derived from the dimensions the loop already resizes through
    (no new maths, no second encode, no pixel decode). Third value semantics
    (§1.1 MINOR-b + N3): ``float | None`` — ``None`` when the bytes traveled untouched
    (raw pass-through, no downscale applied), a float < 1 only when the ladder actually
    downscaled. No path returns ``1.0``.

    R-5 (W4): ``frame`` may carry the observation's capture-time RGB frame (the
    backend's private stash). When the PNG is over budget the JPEG ladder then starts
    from that frame instead of re-decoding the PNG that was just encoded; the ladder's
    outputs are byte-identical (same source pixels, same qualities/scales), and a
    PNG-under-budget payload is returned untouched on both paths. The ``frame``
    argument never changes the INTERNAL bytes — only the outbound copy's encode cost.
    """
    try:
        decoded = base64.b64decode(data_b64, validate=True)
    except Exception:  # noqa: BLE001 - unusable environment is fail-closed data
        return data_b64, "image/png", None
    if len(decoded) <= _result_image_max_bytes():
        # §1.1 MINOR-b: the bytes travel untouched — a raw PASS-THROUGH, so the
        # honest report is None (no downscale applied), never 1.0.
        return data_b64, "image/png", None
    try:
        if (
            frame is not None
            and isinstance(frame, Image.Image)
            and frame.mode in ("RGB", "L")
        ):
            image = frame.copy()  # independent object: the ladder mutates via resize
        else:
            image = Image.open(io.BytesIO(decoded))
            image.load()
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
        source_size = (image.width, image.height)
        # Ladder: JPEG q85, then progressive 0.85 downscale; after exhausting the
        # scale steps, quality descent (85 -> 70 -> 55 -> 40 -> 25) on the smallest
        # frame. The ladder terminates well before pixel collapse; the floor path
        # only exists so a pathological image still yields SOMETHING legible.
        qualities = (85, 70, 55, 40, 25)
        last_bytes = b""
        for step in range(17):  # 12 scale steps (0.85^12 ≈ 0.14x area) + quality descent
            quality = qualities[0] if step < 12 else qualities[min(step - 11, len(qualities) - 1)]
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality)
            last_bytes = buffer.getvalue()
            if len(last_bytes) <= _result_image_max_bytes():
                break
            if step < 12:
                image = image.resize(  # progressive 0.85 downscale until it fits
                    (max(1, int(image.width * 0.85)), max(1, int(image.height * 0.85))),
                    Image.LANCZOS,
                )
        applied_scale = _outbound_image_scale(image.width, image.height, *source_size)
        return base64.b64encode(last_bytes).decode("ascii"), "image/jpeg", applied_scale
    except Exception:  # noqa: BLE001 - degradation: deliver the original image
        logger.debug("outbound image re-encode failed; delivering original PNG", exc_info=True)
        return data_b64, "image/png", None


# --- default action-response image weight ---------------------------------------------------

#: Env knob name: ``CORTEX_ACTION_IMAGE_FULL`` — set to exactly ``1`` to restore the
#: FULL-resolution default action-response image (the pre-0.5.7 behavior). Read lazily
#: (test-toggleable like ``CORTEX_DIFF_FAST``). Scoped to the executed
#: ``computer_execute`` response DEFAULT image only: ``computer_observe`` /
#: ``computer_screenshot`` keep their full-resolution budget path, an explicit
#: ``include_screenshot_after=true`` always ships full resolution, and D1 text-mode
#: semantics are untouched (no images either way).
ACTION_IMAGE_FULL_ENV = "CORTEX_ACTION_IMAGE_FULL"

#: Half-resolution scale and JPEG quality for the DEFAULT executed-response image
#: (action-image): 0.5 -> 960x540 at 1920x1080, quality ~60 — target <= ~60KB typical so a
#: driving model's per-action context stops growing by 150-230KB PNG per action.
_ACTION_IMAGE_SCALE = 0.5
_ACTION_IMAGE_JPEG_QUALITY = 60


def _action_image_full() -> bool:
    """Resolve the action-image full-res escape hatch (only the exact string ``1`` enables)."""
    return os.environ.get(ACTION_IMAGE_FULL_ENV, "").strip() == "1"


def _half_res_action_image(data_b64: str, frame: Any = None) -> tuple[str, str, float | None] | None:
    """Half-resolution JPEG encode of the executed response's default image.

    Downscale 0.5 (LANCZOS) then JPEG quality 60 — 960x540 at 1920x1080. Prefers the
    capture-time RGB ``frame`` (R-5 W4 stash) over re-decoding the PNG. The REM-A H7
    outbound budget still governs: an over-budget half-res result runs through
    :func:`_bound_outbound_image` (which keeps it JPEG and bounded). Returns ``None``
    on any encode failure so the caller falls back to the full-resolution budget
    ladder — a degraded real image beats none. The third value is the MEASURED applied
    scale (§1.1 D-3): the half-res ratio while the half-res encode fits the budget,
    the ladder's measured ratio when the budget ladder fires on top of it.
    """
    try:
        if frame is not None and isinstance(frame, Image.Image) and frame.mode in ("RGB", "L"):
            image = frame.copy()  # independent object: resize must not mutate the stash
        else:
            decoded = base64.b64decode(data_b64, validate=True)
            image = Image.open(io.BytesIO(decoded))
            image.load()
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        source_size = (image.width, image.height)
        image = image.resize(
            (
                max(1, int(image.width * _ACTION_IMAGE_SCALE)),
                max(1, int(image.height * _ACTION_IMAGE_SCALE)),
            ),
            Image.LANCZOS,
        )
        buffer = io.BytesIO()
        # optimize=True: optimal Huffman tables — free ~5-8% on real frames at the
        # same quality level (context bytes ARE the product here).
        image.save(buffer, format="JPEG", quality=_ACTION_IMAGE_JPEG_QUALITY, optimize=True)
        raw = buffer.getvalue()
        encoded = base64.b64encode(raw).decode("ascii")
        if len(raw) <= _result_image_max_bytes():
            scale = _outbound_image_scale(image.width, image.height, *source_size)
            return encoded, "image/jpeg", scale
        # H7 budget still governs the outbound copy even at half resolution; the
        # applied scale composes: half-res step x the budget ladder's own scale,
        # measured against the capture the encode started from. MINOR-N3
        # (VAL-WP1-02): None-safe composition — a None factor means "no downscale on
        # that step" (factor 1.0); the product reports None when NO downscale was
        # applied overall (e.g. a 1x1 source where the half-res step is a no-op).
        bounded_b64, mime, ladder_scale = _bound_outbound_image(encoded)
        half_scale = _outbound_image_scale(image.width, image.height, *source_size)
        composed = (half_scale or 1.0) * (ladder_scale or 1.0)
        return bounded_b64, mime, None if composed >= 1.0 else round(composed, 4)
    except Exception:  # noqa: BLE001 - degradation: fall back to the budget ladder
        logger.debug("half-res action-image encode failed; using the budget ladder", exc_info=True)
        return None


def _execute_response_blocks(
    response: dict[str, object],
    frame: Any = None,
    *,
    full_resolution: bool = False,
    view_id: str | None = None,
    view_payload: tuple[str, Any] | None = None,
) -> list[Any]:
    """REM-A H3/H5: executed ``computer_execute`` results as MCP content blocks.

    Returns one TextContent (the result JSON with the image blob stripped and the
    outbound format noted) plus one ImageContent carrying the post-action screenshot
    as a real image block — parity with ``computer_observe``. When the caller opted
    out (``include_screenshot_after=False``, honored BEFORE this point) the response
    carries no blob and this yields the text block only — never an empty image.
    ``frame`` (R-5 W4, optional) is the capture-time RGB frame for the outbound JPEG
    ladder (skips re-decoding the PNG; byte-identical outputs).

    ``full_resolution=False`` (the DEFAULT — ``include_screenshot_after``
    omitted) ships the image as a HALF-RESOLUTION JPEG (0.5x, q60; target <= ~60KB
    typical) so driver context stops growing by a 150-230KB PNG per action.
    ``full_resolution=True`` (an explicit ``include_screenshot_after=true`` — the
    documented full-image opt-in — or ``CORTEX_ACTION_IMAGE_FULL=1``) keeps the
    full-resolution ``_bound_outbound_image`` budget path unchanged.

    AL-002 (additive, W2): when the caller requested a ``visual_view``, ``view_id``
    names the served view and ``view_payload`` is the pre-derived
    ``(encoded_view_b64, derived_frame)`` pair (derived ONCE by the caller — I-4: the
    derivation consumed the stash or decoded the payload exactly once). The derived
    bytes ride the UNCHANGED stock bounding branch below, and the additive
    ``visual_view``/``visual_view_scale`` keys report what was served
    (``visual_view_scale=None`` for the pass-through raw view — raw adds no view
    transform; a header-parse ratio for derived views, 1.0 = undegraded). With
    ``view_id`` set and ``view_payload=None`` (raw) the stock bytes travel untouched.
    ``view_id=None`` (the default path) is byte-identical to the pre-AL-002 builder.
    """
    payload = dict(response)
    data_b64 = payload.pop("screenshot_after_base64", None)
    blocks: list[Any] = []
    if data_b64 is not None:
        if view_payload is not None:
            # AL-002: the requested view was derived ONCE by the caller; the derived
            # bytes + frame ride the UNCHANGED stock bounding branch (I-11: the stock
            # ladder's degradation still applies and is reported via visual_view_scale).
            data_b64, frame = view_payload
        if full_resolution:
            bounded_b64, mime, outbound_scale = _bound_outbound_image(str(data_b64), frame=frame)
        else:
            halved = _half_res_action_image(str(data_b64), frame=frame)
            if halved is None:
                bounded_b64, mime, outbound_scale = _bound_outbound_image(str(data_b64), frame=frame)
            else:
                bounded_b64, mime, outbound_scale = halved
        payload["image_format"] = mime
        # §1.1 D-3 (ORVEX-2026-108): image_outbound_scale is the ONLY scale key —
        # the MEASURED scale the image ladder applied to the delivered copy (1.0
        # when nothing fired). The legacy ``image_scale`` constant-marker key is
        # deleted: one key, one meaning, and it can no longer lie when the budget
        # ladder fires on top of the half-res encode.
        payload[IMAGE_OUTBOUND_SCALE_KEY] = outbound_scale
        if view_id is not None:
            # AL-002 additive truth markers: which view was served, and its outbound
            # scale (None for raw pass-through; header-parsed ratio for derived views).
            payload["visual_view"] = view_id
            payload["visual_view_scale"] = (
                None
                if view_payload is None
                else outbound_view_scale(frame, bounded_b64)
            )
        blocks.append(ImageContent(type="image", data=bounded_b64, mimeType=mime))
    return [
        TextContent(type="text", text=json.dumps(payload, ensure_ascii=False)),
        *blocks,
    ]


def _bound_observe_lists(dump: dict[str, Any]) -> dict[str, Any]:
    """H8: cap ``ocr_text``/``ui_elements`` in the SERIALIZED observe metadata only.

    The internal :class:`~computer_use_mcp.models.Observation` keeps its full lists;
    only the host-facing metadata text is capped, with an additive
    ``<field>_omitted_count`` marker so nothing is silently lost.
    """
    for field in ("ocr_text", "ui_elements"):
        value = dump.get(field)
        if isinstance(value, list) and len(value) > OBSERVE_METADATA_ELEMENT_CAP:
            omitted = len(value) - OBSERVE_METADATA_ELEMENT_CAP
            dump[field] = value[:OBSERVE_METADATA_ELEMENT_CAP]
            dump[f"{field}_omitted_count"] = omitted
    return dump


# --- AL-002 adaptive visual representations (additive; MISSION-CRF-AVR-008 W2) ---------------
# A per-call, fail-closed view selector on the observe/execute paths: an ABSENT request
# ships byte-identical results with NO new keys and no new work beyond the None-check
# (H19/I-1); an EXPLICIT request (raw OR grid) is a view selection by the model — the
# spatial text layer accompanies EVERY selected representation (owner directive Phase 5,
# Commander correction 2026-09-17). Unknown names are typed ``invalid_visual_view``
# errors; impossible derivations are typed ``view_derive_failed`` errors — never a silent
# raw substitution (I-7). The providers/registry/grid renderer/OCR-once cache live in
# ``visual_views.py`` (models+stdlib+PIL only, never agent/server/verification).


def _resolve_view_param(visual_view: str | None) -> Any:
    """Resolve an explicit ``visual_view`` request (fail-closed, AL-002 I-7).

    ``None`` (the default path) returns None WITHOUT touching the registry — the
    None-check is the ONLY added work on the default path (H19). Invalid names raise
    :class:`InvalidVisualViewError`; callers translate that into the typed
    ``invalid_visual_view`` error dict naming the available views.
    """
    if visual_view is None:
        return None
    return resolve_view(visual_view)


def _spatial_text_for(
    observation: Any, bundle: "_SessionBundle", *, pixel_evidence: bool = False
) -> Any:
    """Build/reuse the observation's spatial-text block with instrumentation (AL-002).

    Counter ``spatial_text_compute`` + latency ``spatial_text_ms`` ride the
    create-on-first-use metrics seams (audit.py:200-208); both fire exactly once per
    actual computation (the OCR-once cache, I-2). Cap = ``OBSERVE_METADATA_ELEMENT_CAP``
    — the SAME serialized-metadata cap the observe path already applies (I-9), passed
    through so the lockstep with ``visual_views.SPATIAL_TEXT_CAP`` is structural.
    ``pixel_evidence`` (A2, owner decision D2) rides the single build: OFF (default)
    omits the per-region field and skips all sigma computation; ON computes the frozen
    A1.1 formula inside the SAME build (the flag never changes the computation count).
    """
    def _on_compute(elapsed_ms: float) -> None:
        bundle.metrics.incr("spatial_text_compute")
        bundle.metrics.record_latency("spatial_text_ms", elapsed_ms)

    try:
        return spatial_text_of(
            observation,
            on_compute=_on_compute,
            cap=OBSERVE_METADATA_ELEMENT_CAP,
            pixel_evidence=pixel_evidence,
        )
    except Exception as exc:  # noqa: BLE001 - attach failure is typed, never stock-breaking
        raise ViewDeriveError(f"spatial-text attach failed: {type(exc).__name__}: {exc}") from exc


def _view_derive_failed(message: str) -> dict[str, object]:
    """The typed ``view_derive_failed`` boundary error (AL-002; no silent raw fallback)."""
    return {"ok": False, "error": "view_derive_failed", "message": message}


def _invalid_visual_view(exc: InvalidVisualViewError) -> dict[str, object]:
    """The typed ``invalid_visual_view`` boundary error (mirrors ``invalid_image_delivery``)."""
    return {"ok": False, "error": "invalid_visual_view", "message": str(exc)}


def _invalid_region(message: str) -> dict[str, object]:
    """The typed ``invalid_region`` boundary error (A1.3 item 2; never a clipped guess).

    Every ``computer_zoom`` region violation class — malformed shape, non-int entries,
    non-positive width/height, out-of-bounds, UNVERIFIABLE coordinate space — returns
    this shape naming the bounds. NO clipping, NO silent full-frame fallback.
    """
    return {"ok": False, "error": "invalid_region", "message": message}


def _invalid_pixel_evidence(value: Any) -> dict[str, object]:
    """The typed ``invalid_pixel_evidence`` boundary error (A2; the same fail-closed
    family as ``invalid_visual_view`` / ``invalid_region``).

    ``pixel_evidence`` is a STRICT boolean (A2, owner decision D2): absent/None and
    False mean OFF, True computes the frozen A1.1 scores; any other value is rejected
    here rather than coerced — direct callers bypass MCP validation, so the tool
    re-checks at the boundary (defense in depth, the ``via`` precedent).
    """
    return {
        "ok": False,
        "error": "invalid_pixel_evidence",
        "message": (
            "pixel_evidence must be a boolean (true = compute the per-region A1.1 "
            "pixel-evidence scores for this request; false/absent = the field is "
            f"omitted entirely); got {value!r} ({type(value).__name__})."
        ),
    }


def _zoom_region_shape_error(region: Any) -> str | None:
    """Pre-capture shape validation for a zoom region (A1.3 item 2, fail-closed economy).

    Checks everything that needs NO observation (4-sequence of ints, width > 0,
    height > 0) so a malformed request wastes no capture; the bounds and the
    coordinate-space doctrine are checked against the FRESH observation by
    :func:`_zoom_region_bounds_error` after the capture. Returns the error message, or
    ``None`` when the shape is acceptable.
    """
    if region is None or isinstance(region, (str, bytes, dict)) or not isinstance(
        region, (list, tuple)
    ):
        return (
            "region must be a [left, top, width, height] sequence of 4 ints in canonical "
            f"screenshot space; got {region!r}."
        )
    if len(region) != 4:
        return (
            "region must be a [left, top, width, height] sequence of exactly 4 ints; "
            f"got {len(region)} entries: {region!r}."
        )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in region):
        return (
            "region entries must all be ints in canonical screenshot space; got "
            f"{region!r}. Zoom never coerces a guess."
        )
    left, top, width, height = region
    if width <= 0 or height <= 0:
        return (
            f"region width and height must be > 0; got {region!r}."
        )
    return None


def _zoom_region_bounds_error(region: Any, observation: Any) -> str | None:
    """Post-capture bounds/space validation for a zoom region (A1.3 item 2).

    The region must be FULLY inside the fresh observation's frame
    (``0 <= left``, ``0 <= top``, ``left+width <= observation.width``,
    ``top+height <= observation.height``) and the observation's coordinate space must
    NOT be ``UNVERIFIABLE`` (the same refusal doctrine as coordinate actions). Returns
    the error message, or ``None`` when the region is acceptable.
    """
    left, top, width, height = region
    if (
        left < 0
        or top < 0
        or left + width > observation.width
        or top + height > observation.height
    ):
        return (
            f"region {list(region)} is not fully inside the observation frame "
            f"{observation.width}x{observation.height} (bounds: 0 <= left, 0 <= top, "
            f"left+width <= {observation.width}, top+height <= {observation.height}); "
            "zoom never clips and never falls back to a full frame."
        )
    if observation.coordinate_space is CoordinateSpace.UNVERIFIABLE:
        return (
            "the observation's coordinate space is unverifiable; zoom coordinates cannot "
            "be trusted (the same refusal doctrine as coordinate actions)."
        )
    return None


def _zoom_spatial_block(
    block: dict[str, Any] | None, left: int, top: int, width: int, height: int
) -> dict[str, Any] | None:
    """Filter the OCR-once spatial-text block to the zoom crop (A1.3 item 5).

    Regions whose bboxes INTERSECT the crop are included with their canonical
    screenshot-space coordinates UNCHANGED (never re-based to crop-local — one
    coordinate space everywhere, I-6), each with its ``pixel_evidence`` computed on the
    FULL frame by the cached block build (A1.1). The block gains ``crop_region`` next to
    the usual provenance (``crop_origin`` stays the MONITOR origin — screen =
    ``crop_origin + screenshot``; screenshot = ``crop_region[:2] + crop-local``).
    ``region_count``/``omitted_count`` semantics are preserved: ``omitted_count``
    reports every known-but-unserved region (the cap-dropped AND the non-intersecting)
    — nothing silently lost (I-9). The cached full block is never mutated (I-2/I-5).
    """
    if block is None:
        return None
    regions = block.get("regions") or []
    kept = [
        dict(entry)
        for entry in regions
        if entry["x"] < left + width
        and entry["y"] < top + height
        and entry["x"] + entry["width"] > left
        and entry["y"] + entry["height"] > top
    ]
    zoom_block = dict(block)  # the cached OCR-once block stays untouched (I-2)
    zoom_block["crop_region"] = [left, top, width, height]
    zoom_block["region_count"] = len(kept)
    zoom_block["omitted_count"] = int(block.get("omitted_count", 0)) + (
        len(regions) - len(kept)
    )
    zoom_block["regions"] = kept
    return zoom_block


def _parse_limits(limits: dict[str, Any] | None) -> Limits:
    """Validate a client-supplied limits dict via ``Limits.validate`` (fail-closed)."""
    if not limits:
        return Limits()
    if not isinstance(limits, dict):
        raise TypeError("limits must be a mapping of Limits field names to numbers.")
    known = {item.name for item in dataclass_fields(Limits)}
    unknown = sorted(str(key) for key in limits if key not in known)
    if unknown:
        raise ValueError(f"Unknown limits fields: {unknown}. Valid fields: {sorted(known)}.")
    kwargs: dict[str, float] = {}
    for key, value in limits.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError(f"Limit {key!r} must be a number, got {type(value).__name__}.")
        # F3 fix 3.4 (B·N5), ported with WP6: ``math.isfinite`` guard. ``Limits._clamp``
        # cannot detect a non-finite value — ``min(hi, nan)`` is ``hi`` — so e.g.
        # ``max_wait_seconds=nan`` would silently resolve to the CEILING (10800), the
        # most PERMISSIVE value the field allows. A non-finite value is a CLIENT ERROR
        # here, so the fail-closed answer is a typed rejection, never a substitution.
        if not math.isfinite(float(value)):
            raise ValueError(
                f"Limit {key!r} must be a finite number; got {value!r} "
                "(nan/inf are not accepted — omit the field for its default)."
            )
        kwargs[str(key)] = float(value)
    return Limits(**kwargs).validate()  # type: ignore[arg-type]


# --- REM-F weak-model tolerant argument coercion (ORVEX-CORTEX-055, boundary only) --------------
# The live Kimi Code / GLM-5V run proved the pipeline works but the DRIVING MODEL
# could not pass the strict pydantic schema four times (float coordinates, a
# follow_ups array shape, an allowed_processes array) and gave up on clicking.
# These helpers add BOUNDARY tolerance on exactly that class — the coercion runs
# PRE-validation, and every downstream internal model (GroundedAction, Limits,
# ActionSpec bounds) stays strict and unchanged. Non-numeric garbage still fails
# with a clean pydantic-style typed error — this is tolerance, not semantics
# change. D5 (ORVEX-CORTEX-056-LIVEFIX): the ADVERTISED schemas for the four
# live-friction classes (x/y/x2/y2, allowed_processes/allowed_windows,
# follow_ups, keys) are now WIDENED flat type-arrays that also admit the string
# (and, for follow_ups, object) shapes the client-side validator used to reject
# before these coercions could ever run; everything else keeps the plain
# "integer"/"array" advertisement.


#: Names of the action-spec fields that carry integer coordinates (used to coerce
#: follow_ups entries with the same policy as the tool parameters).
_SPEC_COORDINATE_FIELDS = ("x", "y", "x2", "y2")


def _coerce_tolerant_int(value: Any, *, field: str = "value") -> int:
    """Boundary coercion for one integer tool argument (weak-model tolerance).

    Accepts ints as-is; integral floats (1343.0) and numeric strings ("1343") pass
    through; NON-INTEGRAL floats/strings ROUND to the nearest int — a vision model
    aiming at pixel 1343.7 must not hard-fail at the schema boundary. Booleans,
    NaN/infinity, non-numeric strings, and non-number types raise a clean
    pydantic-style ValueError instead of crashing.
    """
    if isinstance(value, bool):
        # ValueError (not TypeError): pydantic only converts ValueError/AssertionError
        # inside BeforeValidator into a typed ValidationError — a raw TypeError would
        # escape the validation boundary as a crash (verified against pydantic 2.13).
        raise ValueError(  # noqa: TRY004 - deliberate: see the comment above
            f"{field}: boolean is not a valid integer coordinate."
        )
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field}: non-finite number is not a valid integer coordinate.")
        return round(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError:
            raise ValueError(
                f"{field}: cannot interpret {value!r} as an integer coordinate."
            ) from None
        if not math.isfinite(number):
            raise ValueError(f"{field}: non-finite number is not a valid integer coordinate.")
        return round(number)
    raise ValueError(
        f"{field}: cannot interpret a {type(value).__name__} as an integer coordinate."
    )


#: ``x``/``y``/``x2``/``y2`` tool type — schema still says "integer" (Annotated
#: int), coercion runs before validation. Covers int, integral float, fractional
#: float (rounded), and numeric string inputs.
TolerantInt = Annotated[int, BeforeValidator(_coerce_tolerant_int)]


def _coerce_tolerant_str_list(value: Any) -> list[str] | None:
    """Boundary coercion for allowlist parameters (``allowed_processes``/windows).

    Accepts list[str] as today, PLUS weak-model serializations: a comma- or
    space-separated STRING ("mspaint.exe, notepad.exe" -> two entries) and a single
    bare string ("mspaint.exe" -> one entry). Empty/whitespace-only string yields
    [] (same as an omitted value). Anything else raises a typed error.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        return [item for item in stripped.replace(",", " ").split() if item]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    # ValueError (not TypeError): pydantic converts only ValueError/AssertionError
    # inside BeforeValidator into a typed ValidationError — a raw TypeError would
    # escape the validation boundary as a crash.
    raise ValueError(
        f"must be a list of strings (or a comma/space-separated string), "
        f"got {type(value).__name__}."
    )


#: Allowlist parameter type — schema still says array-of-string.
TolerantStrList = Annotated[list[str], BeforeValidator(_coerce_tolerant_str_list)]


def _coerce_tolerant_follow_ups(value: Any) -> list[dict[str, Any]] | None:
    """Boundary coercion for the ``follow_ups`` queue (weak-model tolerance).

    Accepts list[dict] as today, PLUS: a JSON-encoded STRING containing the list
    (parsed), a single dict (wrapped into [dict]), and per-entry JSON strings (each
    parsed). Entries keep ``dict[str, Any]`` (unknown keys already tolerated);
    non-JSON strings and non-object entries raise a typed error — the queue is
    never silently truncated or guessed at.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"follow_ups string is not valid JSON: {exc}") from None
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, (list, tuple)):
        coerced: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"follow_ups entry is not valid JSON: {exc}") from None
            if not isinstance(item, dict):
                # ValueError (not TypeError): pydantic converts only ValueError/
                # AssertionError inside BeforeValidator into a typed ValidationError.
                raise ValueError(  # noqa: TRY004 - deliberate, see comment above
                    "each follow_ups entry must be an action-spec object."
                ) from None
            coerced.append(item)
        return coerced
    raise ValueError("follow_ups must be a list of action-spec objects.") from None


#: Follow-up queue type — schema still says array-of-object (dict[str, Any]).
TolerantFollowUps = Annotated[list[dict[str, Any]], BeforeValidator(_coerce_tolerant_follow_ups)]


# --- REM-G plain advertised schemas (ORVEX-CORTEX-055, live-test hardening 2) --------------------
# TWO further Kimi Code / GLM-5V live runs (REM-F runtime coercion already in
# place) still failed on EVERY parameter whose ADVERTISED wire schema uses
# pydantic's Optional anyOf union form — a plain integer x=679, a plain array
# keys=["enter"], a valid follow_ups array, allowed_processes=["mspaint.exe"]
# all came back "/x must be integer; /x must be null; /x must match a schema in
# anyOf" from the CLIENT-side validator, while plain-string params passed. The
# client chokes on the anyOf UNION FORM itself, not the types.
#
# Fix (advertised shape only — runtime validation is EXACTLY as today): a small
# ``_PlainJsonSchema`` marker attached via ``Annotated[T | None, marker]``
# flattens each Optional parameter's advertised JSON Schema from anyOf[X, null]
# to the maximally-compatible type-array form ({"type": ["integer", "null"]},
# {"type": ["array", "null"], "items": ...}). The marker is the LAST Annotated
# metadata item so pydantic honors it for the WHOLE union (an inner placement
# would only cover the non-null arm). "default": null semantics survive —
# the marker's schema merges with the Field default (pinned by tests). The
# REM-F BeforeValidator coercions are COMPOSED, not replaced: the union still
# validates X through the tolerant arm and null through the None arm.


class _PlainJsonSchema:
    """Schema marker: advertise a fixed plain shape for the WHOLE Optional union.

    Implements pydantic's ``__get_pydantic_json_schema__`` protocol. Used only
    at the MCP tool boundary to flatten ``anyOf[X, null]`` wire schemas into the
    maximally-compatible type-array form; the union's RUNTIME validation (REM-F
    tolerant arm + null arm) is untouched.
    """

    def __init__(self, shape: dict[str, Any]) -> None:
        self._shape = shape

    def __get_pydantic_json_schema__(  # noqa: D105 - pydantic protocol, not docstring-able
        self,
        schema: Any,
        handler: Any,
    ) -> dict[str, Any]:
        return dict(self._shape)


#: The flattened advertised shapes (the only wire-schema change REM-G makes).
#: D5 (ORVEX-CORTEX-056-LIVEFIX) WIDENED four of them to flat type-arrays that
#: ALSO admit "string" (and "object" for follow_ups): the live client-side ajv
#: validator rejected numeric-string coordinates, string allowlists, string/dict
#: follow_ups, and bare-string keys BEFORE the bytes ever reached Cortex, so the
#: REM-F server-side coercions for those shapes were unreachable from the host.
#: The runtime coercions already exist and already reject garbage fail-closed —
#: this is SCHEMA-WIDENING ONLY (still no anyOf; REM-G pin A holds).
_PLAIN_NULLABLE_INTEGER = _PlainJsonSchema({"type": ["integer", "null"]})
_PLAIN_NULLABLE_STRING = _PlainJsonSchema({"type": ["string", "null"]})
_PLAIN_NULLABLE_BOOLEAN = _PlainJsonSchema({"type": ["boolean", "null"]})
_PLAIN_NULLABLE_STRING_ARRAY = _PlainJsonSchema(
    {"type": ["array", "null"], "items": {"type": "string"}}
)
_PLAIN_NULLABLE_OBJECT_ARRAY = _PlainJsonSchema(
    {"type": ["array", "null"], "items": {"type": "object", "additionalProperties": True}}
)
#: D5 widened shapes (WO-1..WO-4): coordinate scalars admit numeric strings;
#: allowlist arrays admit comma/space-separated strings; the follow_ups queue
#: admits JSON-encoded strings and a single wrapped dict; keys admits one bare
#: key-name string or a JSON-array string. Garbage still fails typed server-side.
_PLAIN_WIDENED_NULLABLE_COORDINATE = _PlainJsonSchema(
    {"type": ["integer", "string", "null"]}
)
_PLAIN_WIDENED_NULLABLE_STRING_ARRAY = _PlainJsonSchema(
    {"type": ["array", "string", "null"], "items": {"type": "string"}}
)
_PLAIN_WIDENED_NULLABLE_OBJECT_ARRAY = _PlainJsonSchema(
    {
        "type": ["array", "object", "string", "null"],
        "items": {"type": "object", "additionalProperties": True},
    }
)
_PLAIN_NULLABLE_STRING_MAP = _PlainJsonSchema(
    {"type": ["object", "null"], "additionalProperties": True}
)
# A1.3: the ``computer_zoom`` region — an advertised 4-integer array in canonical
# screenshot space. The runtime validation is STRICTER than the wire schema (typed
# ``invalid_region`` for every violation class, A1.3 item 2 — never a clipped guess).
_PLAIN_NULLABLE_REGION = _PlainJsonSchema(
    {"type": ["array", "null"], "items": {"type": "integer"}, "minItems": 4, "maxItems": 4}
)
_PLAIN_NULLABLE_NUMBER_MAP = _PlainJsonSchema(
    {"type": ["object", "null"], "additionalProperties": {"type": "number"}}
)


def _coerce_tolerant_keys(value: Any) -> list[str] | None:
    """Boundary coercion for the ``keys`` parameter (REM-G weak-model tolerance).

    Accepts list[str] as today, PLUS weak-model serializations: a bare STRING
    "enter" -> ["enter"] (a single key name) and a JSON-encoded array string
    '["ctrl","a"]' -> parsed (same pattern as the REM-F follow_ups tolerance).
    Non-JSON bracket strings and non-list/non-string values raise a typed error.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"keys string is not a valid JSON array: {exc}") from None
            if not isinstance(parsed, list):
                raise ValueError("keys JSON string must encode an ARRAY of key names.")
            value = parsed
        elif not stripped:
            return []
        else:
            return [stripped]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    # ValueError (not TypeError): pydantic converts only ValueError/AssertionError
    # inside BeforeValidator into a typed ValidationError — a raw TypeError would
    # escape the validation boundary as a crash.
    raise ValueError(
        f"keys must be an array of key names (or one key name as a string), "
        f"got {type(value).__name__}."
    ) from None


#: ``keys`` tool type — runtime list[str] with REM-G string tolerance; the
#: advertised schema is flattened to array-of-string|null (see _PlainJsonSchema).
TolerantKeys = Annotated[list[str], BeforeValidator(_coerce_tolerant_keys)]


#: Optional tool parameter types with FLATTENED advertised schemas (REM-G). Each
#: composes the EXISTING REM-F tolerant runtime type with the plain-union marker:
#: runtime validation is byte-identical, only the wire schema loses anyOf.
#: D5 (WO-1..WO-4) swaps in the WIDENED markers on exactly the four friction
#: classes observed live (coordinates, allowlists, follow_ups, keys) so the
#: client-side validator stops rejecting shapes the server already tolerates.
#: NullableInt keeps the strict integer-only ADVERTISED shape for any non-tool
#: consumer; the four D5 friction classes use the widened aliases below.
NullableInt = Annotated[TolerantInt | None, _PLAIN_NULLABLE_INTEGER]
NullableStr = Annotated[str | None, _PLAIN_NULLABLE_STRING]
NullableBool = Annotated[bool | None, _PLAIN_NULLABLE_BOOLEAN]
NullableCoordinate = Annotated[TolerantInt | None, _PLAIN_WIDENED_NULLABLE_COORDINATE]
NullableStrList = Annotated[TolerantStrList | None, _PLAIN_WIDENED_NULLABLE_STRING_ARRAY]
NullableFollowUps = Annotated[TolerantFollowUps | None, _PLAIN_WIDENED_NULLABLE_OBJECT_ARRAY]
# The S-10 typed advertised schema from plan.py is attached here. The ADVERTISED
# shape is the object-only PLAN_PARAM_PLAIN_SCHEMA (no
# anyOf/$ref, no "string" arm — the REM-G plain rule and S-10 hold verbatim); the
# JSON-string tolerance is RUNTIME-only, behind the boundary, via the same
# parse-only coercion the follow_ups queue uses (the strict fail-closed validation
# itself stays in computer_execute's handler so a malformed plan is the teaching
# ``invalid_action`` reply, never an argument error).
def _coerce_plan_param(value: Any) -> Any:
    """Boundary tolerance for the ``plan`` parameter (the follow_ups pattern).

    Accepts the plan object as-is and a JSON-encoded STRING containing it; anything
    else raises a typed error before the tool runs. This helper PARSES only — the
    strict :func:`computer_use_mcp.plan.coerce_tolerant_plan` pipeline (raw
    boundary -> strict model -> model defense -> SPEC 5.2 explicitness) applies in
    the handler regardless of the wire shape, so tolerance is never a bypass.
    """
    if value is None or isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"plan string is not valid JSON: {exc}") from None
        if not isinstance(parsed, dict):
            raise ValueError("plan string must encode a JSON object")
        return parsed
    raise ValueError(
        f"plan must be an object: {{\"points\": [{{\"x\", \"y\", \"actions\"}}]}}; "
        f"got {type(value).__name__}."
    ) from None


NullablePlan = Annotated[
    dict[str, Any] | None,
    BeforeValidator(_coerce_plan_param),
    _PlainJsonSchema(plan_layer.PLAN_PARAM_PLAIN_SCHEMA),
]
NullableStrMap = Annotated[dict[str, Any] | None, _PLAIN_NULLABLE_STRING_MAP]
NullableNumberMap = Annotated[dict[str, float] | None, _PLAIN_NULLABLE_NUMBER_MAP]
NullableKeys = Annotated[TolerantKeys | None, _PLAIN_WIDENED_NULLABLE_STRING_ARRAY]
# A1.3: the zoom region [left, top, width, height] (canonical screenshot space).
NullableRegion = Annotated[list[Any] | None, _PLAIN_NULLABLE_REGION]


def _coerce_follow_up_entry(item: dict[str, Any]) -> dict[str, Any]:
    """Apply the same coordinate coercion INSIDE one follow_ups entry (REM-F).

    The boundary validates the queue as list[dict[str, Any]]; the strict
    :class:`~computer_use_mcp.models.ActionSpec` (unchanged) then rejects a float
    ``x`` the same way the tool parameter used to. This helper reuses the parameter
    coercion on the coordinate fields only — the strict internal model still
    enforces every bound and rejects garbage identically.
    """
    if not isinstance(item, dict):
        return item  # the strict model produces the typed failure
    coerced = dict(item)
    for name in _SPEC_COORDINATE_FIELDS:
        if name in coerced and coerced[name] is not None:
            try:
                coerced[name] = _coerce_tolerant_int(coerced[name], field=name)
            except ValueError:
                return item  # let the strict model emit its own typed error
    return coerced


# --- teach-in-text action vocabulary (PERF-004 C6) ------------------------------------------
# The EXACT ActionType vocabulary from models.py (verbatim enum values):
# click, double_click, right_click, drag, type, keypress, scroll, wait, done, move,
# hotkey, focus_window. There is deliberately NO 'key' and NO 'triple_click' action.

#: The precise valid action list, taught in tool descriptions and error messages.
ACTION_VOCABULARY = (
    "Valid actions (exact names): "
    "click (x,y required), double_click (x,y), right_click (x,y; opens a context menu), "
    "drag (x,y start + x2,y2 end, both required), "
    "move (x,y), type (text required), keypress (keys=[\"<one key name>\"]), "
    "hotkey (keys=[2-12 key names], e.g. [\"ctrl\",\"a\"]), scroll (delta -20..20), "
    "wait (delta seconds; this flat action is capped at 10 s and reports "
    "waited_seconds when the cap bites — for a LONG wait use the computer_wait tool, "
    "not a large delta), focus_window (target = window title), "
    "ensure_app (target = \"process\" or \"process|doc-token\"; attaches to an EXISTING "
    "instance — REATTACHED/AMBIGUOUS_INSTANCE/NO_INSTANCE — and may launch the target "
    "server-side when no instance matches, the target is allowlisted, and the "
    "launch=\"server\" policy holds (the default; CORTEX_ATTACH_OR_LAUNCH=driver "
    "restores never-launch)), done. "
    # WP2 (ORVEX-2026-107): the vocabulary must not disagree with the three production
    # guards, which all read "hold_ms is only valid on click/right_click/keypress/
    # hotkey actions" (models.py, backend.py, validator.py). Kept to the enforced
    # rule: the legal set, the press/hold/release reading, the reverse-order hotkey
    # release, ``0`` as an explicit zero hold, and omitted-is-legacy.
    "hold_ms (0..5000 ms, optional, carried on an individual action — a follow_ups "
    "entry) is valid on click/right_click/keypress/hotkey ONLY and is rejected on "
    "every other action, including double_click, move, scroll and type: the input is "
    "pressed, stays down for the duration, then released — on a hotkey every named "
    "key goes down SIMULTANEOUSLY and comes up in REVERSE order — never a chord "
    "followed by a wait — and hold_ms=0 is that same path with a zero-duration hold, "
    "while omitting it keeps the ordinary immediate dispatch."
)

#: Key-name rule taught alongside the vocabulary (Session 1 failure class: text sent
#: as hotkey keys).
KEY_NAME_RULE = (
    "keys must be KEY NAMES (e.g. \"ctrl\", \"a\", \"enter\", \"esc\") — never text, "
    "words, or sentences; to type text use action=\"type\" with text=\"...\". "
    "Single-key presses belong on keypress (a hotkey needs 2-12 keys)."
)


def _grounded_shape_hint(text: str) -> str | None:
    """R-18: the closest valid shape for an ActionSpec-valid but GroundedAction-invalid
    queue item.

    A follow_ups entry can pass the ActionSpec schema and still fail GroundedAction
    construction inside the queue (a target-less ``focus_window``, a half-specified
    ``drag`` with one endpoint missing, a 1-key ``hotkey``, a point-less ``move``, a
    target-less ``ensure_app``). The validator messages are matched by their stable
    prefixes so the typed rejection can teach the exact valid shape instead of
    surfacing a bare pydantic error.
    """
    if "drag actions require both point" in text:
        return (
            "A drag needs BOTH endpoints: {\"action\": \"drag\", \"x\": <start x>, \"y\": <start y>, "
            "\"x2\": <end x>, \"y2\": <end y>} — x,y alone (or x2,y2 alone) is rejected."
        )
    if "hotkey actions require 2 to 12" in text:
        return (
            "A hotkey chord needs 2-12 key names: {\"action\": \"hotkey\", \"keys\": [\"ctrl\", \"a\"]}. "
            "For a single key use {\"action\": \"keypress\", \"keys\": [\"<key name>\"]}."
        )
    if "focus_window actions require a non-empty target" in text:
        return (
            "focus_window requires a non-empty target window title: "
            "{\"action\": \"focus_window\", \"target\": \"<window title>\"}."
        )
    if "move actions require a point" in text:
        return "{\"action\": \"move\", \"x\": <x>, \"y\": <y>} — move needs both coordinates."
    if "ensure_app actions require a non-empty target" in text:
        return (
            "ensure_app requires a non-empty target: {\"action\": \"ensure_app\", "
            "\"target\": \"<process>\"} or \"<process>|<doc-token>\"."
        )
    if "via is only valid on type actions" in text:
        return (
            "via selects the type-action transport only: "
            "{\"action\": \"type\", \"text\": \"...\", \"via\": \"clipboard\"} or "
            "via=\"sendinput\" (default). Remove via from every other action."
        )
    if "via must be" in text:
        return (
            "via accepts exactly \"sendinput\" (default per-character typing) or "
            "\"clipboard\" (set CF_UNICODETEXT then ctrl+v paste, for OpenGL/console "
            "apps that drop per-character input): {\"action\": \"type\", \"text\": \"...\", "
            "\"via\": \"clipboard\"}."
        )
    return None


def _teaching_invalid_action(exc: Exception, action: str | None) -> dict[str, object]:
    """Fail-closed ``invalid_action`` response that TEACHES the valid vocabulary.

    Session 1 lost full agent turns to schema rejections (``key``, 1-key ``hotkey``
    with a word payload, ``triple_click``). The rejection now always carries the exact
    valid values and, when recognizable, the closest valid shape for what was tried.
    R-18: the same teaching path covers follow_ups items that pass the ActionSpec
    schema but fail GroundedAction construction (half-specified ``drag``, 1-key
    ``hotkey``, target-less ``focus_window``/``ensure_app``, point-less ``move``).
    """
    hints = [KEY_NAME_RULE]
    tried = (action or "").strip().casefold()
    if tried == "key":
        hints.append(
            "You sent action=\"key\", which does not exist. For a single key press use "
            "action=\"keypress\" with keys=[\"<key name>\"] (e.g. {\"action\": \"keypress\", "
            "\"keys\": [\"ctrl\"]}); for a chord use action=\"hotkey\" with 2-12 key names."
        )
    elif tried == "triple_click":
        hints.append(
            "You sent action=\"triple_click\", which does not exist. Use action=\"double_click\", "
            "or queue repeated clicks via follow_ups."
        )
    elif tried in {"context_menu", "contextmenu", "rightclick", "right-click", "rclick"}:
        hints.append(
            f"You sent action=\"{tried}\", which does not exist. The context-menu action is "
            "action=\"right_click\" with x,y (e.g. {\"action\": \"right_click\", \"x\": 640, \"y\": 360})."
        )
    elif tried in {"keypress", "hotkey"}:
        hints.append(
            "Closest valid shape for a hotkey chord: {\"action\": \"hotkey\", \"keys\": [\"ctrl\", \"a\"]} "
            "(2-12 key names). Closest valid shape for one key: {\"action\": \"keypress\", \"keys\": [\"a\"]}."
        )
    shape_hint = _grounded_shape_hint(str(exc))
    if shape_hint is not None:
        hints.append(shape_hint)
    message = f"{exc} {ACTION_VOCABULARY}"
    # §1.1 rejection envelope (ORVEX-2026-108): a malformed action is a REJECTION —
    # {ok False, result "rejected", message, reasons}, never an ``error`` key; the
    # typed ``invalid_action`` code names itself as the first reason.
    return {
        "ok": False,
        "result": "rejected",
        "message": message,
        "reasons": ["invalid_action", *hints],
    }


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
        _registry.remove(session_id)
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
    payload.update(
        {
            "allowed_processes": list(allowed_processes or []),
            "limits": str(limits_obj),
            "task_id": context.task.task_id,
            # D1: the mode is visible in the transcript right after creation.
            "image_delivery": image_mode,
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


@mcp.tool()
@_session_scoped
def computer_observe(
    session_id: str,
    visual_view: NullableStr = None,
    visual_view_density: NullableStr = None,
    pixel_evidence: NullableBool = None,
) -> Any:
    """Return the current observation state used for grounding: screenshot, dimensions, window, and cursor.

    On success this returns MCP content blocks: one TextContent carrying the
    observation metadata (dimensions, active window, digest, coordinate scale,
    plus an additive ``text_summary`` one-liner: window title/process, cursor,
    focused-control hint when the backend supplies ui_elements, and
    changed/unchanged versus the previous observation of this session — never the
    raw base64) and one ImageContent carrying the screenshot itself, so
    vision-capable clients receive it as a real image rather than as text.
    In a text-mode session (``start_session(image_delivery="text")`` /
    ``CORTEX_IMAGE_DELIVERY=text``) this returns ONE text block only (metadata +
    ``image_delivery`` keys) — never an image block — so non-vision models stay
    alive; the delivery mode is fixed at start_session (pass image_delivery="text"
    there if your inputs are text-only). Error paths still return the structured
    error dict.

    Visual view (AL-002, trailing optional): ``visual_view`` selects the
    representation of THIS observation — "raw" (the untouched screenshot) or "grid"
    (a coordinate-grid annotation aid; labels show canonical screenshot-space
    coordinates, ``displayed + crop_origin = screen`` is a reader-side fact). An
    ABSENT request ships byte-identical results with no new keys and no new work
    (the default never changes). An EXPLICIT request — raw OR grid — also attaches
    the ``spatial_text`` metadata block (the OCR-once UIA-derived text regions with
    provenance: observation_id, coordinate space/scale, crop_origin; ``null`` plus
    ``spatial_text_available: false`` when the read degraded, a truthful
    ``region_count: 0`` when it read nothing). The server never auto-selects a view;
    unknown values are rejected fail-closed (``invalid_visual_view``) and impossible
    derivations with ``view_derive_failed`` — never a silent raw substitution.

    ``visual_view_density`` (A1.2, trailing optional): the grid density for a
    ``visual_view="grid"`` request — closed enum ``coarse|standard|fine`` (default
    ``coarse``). Unknown values are rejected with the same typed
    ``invalid_visual_view``-class error naming the enum. Raw/absent view requests
    IGNORE the parameter silently (density is meaningless for a pass-through), and
    cost is REPORTED (visual_view_scale + instrumentation), never gated.

    ``pixel_evidence`` (A2, owner decision D2, trailing optional): OPT-IN measured
    evidence. ``true`` = every served region in the ``spatial_text`` block carries its
    A1.1 ``pixel_evidence`` score (frozen formula, σ 0.0/32.535) and the block records
    ``"pixel_evidence_mode": "on"``. ``false``/absent (the default) = the field is
    OMITTED entirely (absent, never null), NO sigma computation runs, and the block
    records ``"pixel_evidence_mode": "off"``. Non-boolean values are rejected
    fail-closed (``invalid_pixel_evidence``) before any capture. The OCR-once cache
    semantics are unchanged (H21'): the flag shapes WHAT the single per-observation
    build computes, never the computation count.
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    # WP7b (observe/zoom-during-lease decision, dev parity): an INDEPENDENT
    # observation while a plan lease is waiting would desynchronize the lease's
    # grounding premise — refused with the typed ``plan_lease`` rejection before
    # any capture. computer_fetch_image stays allowed (S-04: it serves evidence
    # the session already holds and captures nothing).
    lease_rejection = _active_plan_tool_rejection(bundle)
    if lease_rejection is not None:
        return lease_rejection
    # D1: text mode suppresses the OUTBOUND image block only — a non-vision model
    # receiving one ImageContent anywhere in its history gets the whole provider
    # request rejected with a 400 and the session dies permanently.
    image_mode = _session_image_delivery(bundle)
    # AL-002: resolve an explicit view request BEFORE any capture (fail-closed — a bad
    # name wastes no capture); the absent request adds only this None-check (H19).
    try:
        view_provider = _resolve_view_param(visual_view)
    except InvalidVisualViewError as exc:
        return _invalid_visual_view(exc)
    # A1.2: density resolves ONLY for a derived-view (grid) request — raw/absent
    # requests ignore it silently (A1.2), so the default path never resolves it (H19).
    density: str | None = None
    if view_provider is not None and not view_provider.passthrough:
        try:
            density = resolve_density(visual_view_density)
        except InvalidVisualViewError as exc:
            return _invalid_visual_view(exc)
    # A2 (owner decision D2): pixel_evidence is a STRICT boolean — absent/None and
    # False mean OFF; any non-bool value is rejected fail-closed BEFORE any capture
    # (the same typed-error family as the other boundary params; direct callers bypass
    # MCP validation, so the boundary re-check is defense in depth).
    if pixel_evidence is not None and not isinstance(pixel_evidence, bool):
        return _invalid_pixel_evidence(pixel_evidence)
    evidence_requested = pixel_evidence is True
    try:
        observation, digest = bundle.agent.observation.capture_with_digest()
    except Exception as exc:  # noqa: BLE001 - structured error, no traceback
        return _error_response(exc)
    info = observation.active_window_info
    # PERF-004 C8: additive structured summary line (previous digest tracked per
    # session; the FIRST observation reports "first observation" — never invented).
    previous_digest = bundle.extra.get("last_observation_digest")
    text_summary = observation_text_summary(observation, previous_digest=previous_digest)
    bundle.extra["last_observation_digest"] = digest
    try:
        bundle.auditor.emit(
            "observation",
            session_id,
            task_id=bundle.context.task.task_id,
            observation_id=observation.observation_id,
            active_app=info.process_name if info is not None else observation.active_window,
            result="ok",
            metadata={"source": "computer_observe"},
        )
    except Exception:
        logger.debug("observation audit failed", exc_info=True)
    bundle.metrics.incr("screenshot_count")
    observation_dump = observation.model_dump(mode="json", exclude={"image_base64"})
    # REM-E (live-test gap): the OUTBOUND ImageContent rides the SAME budget as
    # the execute path (`_bound_outbound_image`, CORTEX_RESULT_IMAGE_MAX_KB —
    # observe and execute). The live probe shipped a 1.37 MB PNG block and blew
    # the provider request; the INTERNAL Observation stays PNG untouched below,
    # so pixel-diff, staleness digests, and checkpoints are unaffected.
    if image_mode == "text":
        # D1 text mode: ONE text block only (metadata + mode keys), never an
        # ImageContent. Internal capture is UNCHANGED — digest, staleness,
        # pixel-diff, text_summary, metrics, and audit all keep working above.
        metadata = {
            # §1.1: ok + message on every reply (text mode: metadata-only, no image).
            "ok": True,
            "message": "Observation served (text mode: no image block).",
            "observation": _bound_observe_lists(observation_dump),
            "digest": digest,
            "observation_id": observation.observation_id,
            "active_app": info.process_name if info is not None else observation.active_window,
            "image_format": "none",  # truthful: no image block was emitted
            "image_delivery": "text",
            # §1.1 D-3: no image was served, so no scale was applied — honest None.
            IMAGE_OUTBOUND_SCALE_KEY: None,
            "image_delivery_note": IMAGE_DELIVERY_TEXT_NOTE,
            # PERF-004 C8 (additive): bounded one-line grounding text for weak models.
            "text_summary": text_summary,
        }
        if view_provider is not None:
            # AL-002 (explicit request, text mode): the view keys ride the single text
            # block; image_format stays "none" and no pixels are derived (there is no
            # outbound image — visual_view_scale stays None, honest by construction).
            # W5.1 (R-10): the attach is guarded here EXACTLY like the image branch —
            # an attach failure degrades to the typed ``view_derive_failed`` error per
            # the termination table ("any exception inside derive/attach -> typed
            # error"), never an untyped crash of the text-mode observe.
            metadata["visual_view"] = view_provider.id
            metadata["visual_view_scale"] = None
            try:
                metadata["spatial_text"] = _spatial_text_for(
                    observation, bundle, pixel_evidence=evidence_requested
                )
            except VisualViewError as exc:
                return _view_derive_failed(str(exc))
            if metadata["spatial_text"] is None:
                metadata["spatial_text_available"] = False  # degraded UIA read (honest)
        return [TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False))]
    # AL-002: ABSENT request or an explicit RAW request → today's bytes EXACTLY
    # (the pass-through branch is the stock call, untouched — I-1 by construction);
    # an explicit derived-view request (grid) → render + one PNG encode, then the
    # UNCHANGED stock ladder.
    view_scale: float | None = None
    if view_provider is None or view_provider.passthrough:
        # WP5 (VAL-WP5 F1): the RAW capture is what this reply carries — exactly the
        # bytes the evidence store keeps (pre-ladder, so a fetch re-runs the ladder).
        carried_b64 = observation.image_base64
        outbound_b64, outbound_mime, image_scale = _bound_outbound_image(
            # R-5 (W4): the capture-time frame (backend stash) skips the PNG re-decode on
            # the JPEG ladder; absent (fakes/legacy) the path decodes exactly as before.
            observation.image_base64, frame=getattr(observation, "_frame", None)
        )
    else:
        try:
            started = time.perf_counter()
            # A1.2: the resolved density rides the derive call (raw ignores it).
            derived = view_provider.derive(observation, density=density)  # stash-copy or ONE payload decode (I-4)
            encoded_b64 = encode_png_b64(derived.image)
            bundle.metrics.record_latency(
                "view_derive_ms", (time.perf_counter() - started) * 1000.0
            )
            outbound_b64, outbound_mime, image_scale = _bound_outbound_image(encoded_b64, frame=derived.image)
            view_scale = outbound_view_scale(derived.image, outbound_b64)
        except VisualViewError as exc:  # fail-closed: typed error, never a raw substitution
            return _view_derive_failed(str(exc))
        except Exception as exc:  # noqa: BLE001 - fail-closed: typed error, no traceback
            return _view_derive_failed(f"{type(exc).__name__}: {exc}")
        # WP5 (VAL-WP5 F1): the DERIVED view (grid labels and all) is what this reply
        # carries — the store keeps exactly these bytes, never the raw capture.
        carried_b64 = encoded_b64
    metadata = {
        # §1.1 (ORVEX-2026-108): ok + message on EVERY reply — here ok means "served
        # as requested" (a read-only surface carries no result token, §1.3).
        "ok": True,
        "message": "Observation served.",
        "observation": _bound_observe_lists(observation_dump),
        "digest": digest,
        "observation_id": observation.observation_id,
        "active_app": info.process_name if info is not None else observation.active_window,
        "image_format": outbound_mime,
        # §1.1 D-3 (ORVEX-2026-108): the ONLY scale key on an image-bearing reply —
        # the measured applied downscale (1.0 when the copy kept its dimensions).
        IMAGE_OUTBOUND_SCALE_KEY: image_scale,
        # PERF-004 C8 (additive): bounded one-line grounding text for weak models.
        "text_summary": text_summary,
    }
    if view_provider is not None:
        # AL-002 (explicit request): the spatial text layer accompanies EVERY selected
        # representation (raw or grid — owner directive Phase 5, Commander correction
        # 2026-09-17); the additive keys ship only on explicit requests (absent request
        # = byte-identical metadata, H19).
        try:
            spatial_block = _spatial_text_for(
                observation, bundle, pixel_evidence=evidence_requested
            )
        except VisualViewError as exc:
            return _view_derive_failed(str(exc))
        metadata["visual_view"] = view_provider.id
        metadata["visual_view_scale"] = view_scale
        metadata["spatial_text"] = spatial_block
        if spatial_block is None:
            metadata["spatial_text_available"] = False  # degraded UIA read (honest)
    # WP5: THIS reply is committed to carrying the image — the hook runs HERE, after
    # every late failure return above (a ``_view_derive_failed`` from the spatial-text
    # block stores nothing — VAL-WP5 F2), so the frame retained is EXACTLY the bytes
    # the reply carried (raw capture or derived view, pre-ladder — VAL-WP5 F1), and
    # the reply gains the additive image_id keys naming it.
    metadata.update(
        _record_observation_evidence(
            bundle,
            observation,
            source="computer_observe",
            digest=digest,
            payload=carried_b64,
            visual_view=view_provider.id if view_provider is not None else None,
        )
    )
    return [
        TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)),
        ImageContent(type="image", data=outbound_b64, mimeType=outbound_mime),
    ]


@mcp.tool()
@_session_scoped
def computer_screenshot(session_id: str) -> Any:
    """Compatibility alias for the raw screenshot observation."""
    return computer_observe(session_id)


@mcp.tool()
@_session_scoped
def computer_zoom(
    session_id: str,
    region: NullableRegion = None,
    visual_view: NullableStr = "raw",
    visual_view_density: NullableStr = None,
    pixel_evidence: NullableBool = None,
) -> Any:
    """Capture a FRESH observation cropped to a region: the zoom view (AL-002 A1.3).

    session_id is REQUIRED on every call: copy it from start_session's result and
    reuse it for the whole session. ``region`` is ``[left, top, width, height]`` in
    canonical SCREENSHOT space (the same space coordinates are grounded in — mss
    convention, matching MonitorInfo.bounds).

    Behavior (spec A1.3 items 1-7):
    1. FRESH capture through the existing lifecycle (the same path computer_observe
       uses): a new ``observation_id`` every call, no frame caching across requests;
       ``screenshot_count`` increments and an ``observation`` audit event with
       ``metadata={"source": "computer_zoom"}`` is emitted.
    2. FAIL-CLOSED region validation: the region must be a 4-int sequence, width > 0,
       height > 0, fully inside the captured frame, and the observation's coordinate
       space must NOT be unverifiable. Any violation returns the typed
       ``invalid_region`` error naming the bounds — NEVER a clipped guess, NEVER a
       silent full-frame fallback.
    3. The served image is the NATIVE-RESOLUTION crop (no downscale, no upscale — the
       crop itself is the zoom); the outbound budget ladder still governs the served
       bytes, and ``visual_view_scale`` reports any ladder-applied scale.
    4. ``visual_view="grid"`` renders the coordinate grid ON THE CROP at the requested
       density; labels show CANONICAL SCREENSHOT-space VALUES — a label at crop-local
       ``(x, y)`` displays ``(left + x, top + y)`` (the crop origin is baked into the
       label VALUES, unlike the full-frame grid view).
    5. The ``spatial_text`` block is ALWAYS attached (observe-family rule): UIA text
       regions whose bboxes intersect the crop, canonical coordinates UNCHANGED; the
       block carries ``crop_region`` + ``crop_origin`` provenance so a reader can
       convert (screenshot = crop_region[:2] + crop-local). The result metadata also
       carries top-level ``crop_region``. The per-region ``pixel_evidence`` score is
       OPT-IN (A2): pass ``pixel_evidence=true`` to compute it (frozen A1.1 formula,
       ``"pixel_evidence_mode": "on"``); the default OFF omits the field entirely and
       records ``"pixel_evidence_mode": "off"`` (no sigma computation, no decode).
    6. OBSERVATION-ONLY: no action surface, no queue interaction; coordinates read
       from a zoom are used through the stock ground -> validate -> safety -> execute
       pipeline exactly like any other observation.
    7. Result shape mirrors computer_observe's: one TextContent (metadata) + one
       bounded ImageContent; text-mode sessions get the single-text-block treatment
       (``image_format: "none"``), never an image block.

    ``visual_view`` accepts "raw" (default) or "grid"; ``visual_view_density``
    (trailing optional, A1.2) selects the grid density — closed enum
    ``coarse|standard|fine``, default ``coarse``; raw requests ignore it silently.
    Unknown view/density values are rejected fail-closed
    (``invalid_visual_view``-class) BEFORE any capture. ``pixel_evidence`` (A2) is a
    STRICT boolean: non-boolean values are rejected fail-closed
    (``invalid_pixel_evidence``) BEFORE any capture.
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    # WP7b: zoom is refused during a plan lease exactly like observe (dev parity;
    # same rationale — an independent capture would desynchronize the lease).
    lease_rejection = _active_plan_tool_rejection(bundle)
    if lease_rejection is not None:
        return lease_rejection
    image_mode = _session_image_delivery(bundle)
    # Fail-closed view/density resolution BEFORE any capture (a bad name wastes no
    # capture; the observe-path precedent). Raw/absent views ignore density silently.
    try:
        view_provider = _resolve_view_param(visual_view)
    except InvalidVisualViewError as exc:
        return _invalid_visual_view(exc)
    density: str | None = None
    if view_provider is None or view_provider.passthrough:
        view_id = "raw"
    else:
        view_id = view_provider.id
        try:
            density = resolve_density(visual_view_density)
        except InvalidVisualViewError as exc:
            return _invalid_visual_view(exc)
    # A2 (owner decision D2): pixel_evidence is a STRICT boolean — rejected fail-closed
    # BEFORE any capture (the same typed-error family; the boundary re-check covers
    # direct callers, the ``via`` precedent).
    if pixel_evidence is not None and not isinstance(pixel_evidence, bool):
        return _invalid_pixel_evidence(pixel_evidence)
    evidence_requested = pixel_evidence is True
    # A1.3 item 2: pre-capture region shape validation (fail-closed economy — a
    # malformed region wastes no capture); bounds/space need the fresh observation.
    shape_error = _zoom_region_shape_error(region)
    if shape_error is not None:
        return _invalid_region(shape_error)
    try:
        observation, digest = bundle.agent.observation.capture_with_digest()
    except Exception as exc:  # noqa: BLE001 - structured error, no traceback
        return _error_response(exc)
    info = observation.active_window_info
    # PERF-004 C8: the same additive summary/digest tracking as computer_observe (a
    # zoom IS an observation request; the next observe's changed/unchanged line
    # compares against the freshest capture of the session). Recorded BEFORE the
    # region-bounds verdict: the capture genuinely happened, so the audit event and
    # the ``screenshot_count`` increment are owed regardless of the outcome
    # (A1.3 item 7: "screenshot_count increments (a capture happened)").
    previous_digest = bundle.extra.get("last_observation_digest")
    text_summary = observation_text_summary(observation, previous_digest=previous_digest)
    bundle.extra["last_observation_digest"] = digest
    try:
        bundle.auditor.emit(
            "observation",
            session_id,
            task_id=bundle.context.task.task_id,
            observation_id=observation.observation_id,
            active_app=info.process_name if info is not None else observation.active_window,
            result="ok",
            metadata={"source": "computer_zoom"},
        )
    except Exception:
        logger.debug("observation audit failed", exc_info=True)
    bundle.metrics.incr("screenshot_count")
    # A1.3 item 2 (continued): the bounds + coordinate-space verdict against the FRESH
    # observation. A refusal here still leaves the capture honestly recorded above.
    bounds_error = _zoom_region_bounds_error(region, observation)
    if bounds_error is not None:
        return _invalid_region(bounds_error)
    left, top, width, height = (int(value) for value in region)
    observation_dump = observation.model_dump(mode="json", exclude={"image_base64"})
    # A1.3 item 5: the spatial text layer is ALWAYS attached on the observe family.
    # The OCR-once full block is built/reused here (A2: per-region pixel_evidence only
    # when the caller opted in), then filtered to the crop for THIS result (the cache
    # stays full — I-2).
    try:
        full_block = _spatial_text_for(
            observation, bundle, pixel_evidence=evidence_requested
        )
    except VisualViewError as exc:
        return _view_derive_failed(str(exc))
    zoom_block = _zoom_spatial_block(full_block, left, top, width, height)
    # D1 text mode: ONE text block only (metadata + mode keys), never an ImageContent.
    # No pixels are derived for the outbound image (there is none), so
    # visual_view_scale stays None — honest by construction (the observe precedent).
    if image_mode == "text":
        metadata = {
            # §1.1: ok + message on every reply (text mode: metadata-only, no image).
            "ok": True,
            "message": "Zoom served (text mode: no image block).",
            "observation": _bound_observe_lists(observation_dump),
            "digest": digest,
            "observation_id": observation.observation_id,
            "active_app": info.process_name if info is not None else observation.active_window,
            "image_format": "none",  # truthful: no image block was emitted
            "image_delivery": "text",
            # §1.1 D-3: no image was served, so no scale was applied — honest None.
            IMAGE_OUTBOUND_SCALE_KEY: None,
            "image_delivery_note": IMAGE_DELIVERY_TEXT_NOTE,
            "text_summary": text_summary,
            "crop_region": [left, top, width, height],
            "visual_view": view_id,
            "visual_view_scale": None,
            "spatial_text": zoom_block,
        }
        if zoom_block is None:
            metadata["spatial_text_available"] = False  # degraded UIA read (honest)
        return [TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False))]
    # A1.3 item 3: native-resolution crop (stash or ONE recorded payload decode);
    # item 4: grid views render ON the crop with canonical-value labels. The UNCHANGED
    # stock ladder then governs the served bytes exactly as for full-frame views.
    try:
        started = time.perf_counter()
        crop, _crop_provenance = crop_observation_frame(observation, left, top, width, height)
        served = crop
        if view_provider is not None and not view_provider.passthrough:
            served = render_crop_grid(
                crop, origin_x=left, origin_y=top, density=density or DEFAULT_GRID_DENSITY
            )
        encoded_b64 = encode_png_b64(served)
        bundle.metrics.record_latency(
            "view_derive_ms", (time.perf_counter() - started) * 1000.0
        )
        outbound_b64, outbound_mime, image_scale = _bound_outbound_image(encoded_b64, frame=served)
        view_scale = outbound_view_scale(served, outbound_b64)
    except VisualViewError as exc:  # fail-closed: typed error, never a raw substitution
        return _view_derive_failed(str(exc))
    except Exception as exc:  # noqa: BLE001 - fail-closed: typed error, no traceback
        return _view_derive_failed(f"{type(exc).__name__}: {exc}")
    metadata = {
        # §1.1 (ORVEX-2026-108): ok + message on EVERY reply (read-only surface: no
        # result token, §1.3).
        "ok": True,
        "message": "Zoom served.",
        "observation": _bound_observe_lists(observation_dump),
        "digest": digest,
        "observation_id": observation.observation_id,
        "active_app": info.process_name if info is not None else observation.active_window,
        "image_format": outbound_mime,
        # §1.1 D-3 (ORVEX-2026-108): the ONLY scale key — the measured applied
        # downscale the outbound budget ladder ran on the served crop.
        IMAGE_OUTBOUND_SCALE_KEY: image_scale,
        "text_summary": text_summary,
        "crop_region": [left, top, width, height],
        "visual_view": view_id,
        "visual_view_scale": view_scale,
        "spatial_text": zoom_block,
    }
    # WP5: the CROP this reply carries is what is retained (pre-ladder, so a later
    # fetch re-runs the outbound ladder honestly), and the crop region rides along so
    # the frame's own coordinates are never mistaken for full-screen coordinates.
    # The text-mode branch above stores nothing — that reply carries no image.
    metadata.update(
        _record_observation_evidence(
            bundle,
            observation,
            source="computer_zoom",
            digest=digest,
            payload=encoded_b64,
            crop_region=(left, top, width, height),
            visual_view=view_id,
        )
    )
    if zoom_block is None:
        metadata["spatial_text_available"] = False  # degraded UIA read (honest)
    return [
        TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)),
        ImageContent(type="image", data=outbound_b64, mimeType=outbound_mime),
    ]


# --- WP7b: the waypoint plan surface (ORVEX-2026-101 Batch 2 / master-mission §1.2-§1.3) ------
# ONE normalization path, NO second executor: a plan normalizes to plain action-queue
# items (``[move, actions...]`` per point) and each STEP runs through the EXISTING
# queue via ``agent.run_single(..., plan_policies=..., plan_context=...)``, so every
# item inherits the full independent pipeline (ground -> validate -> safety ->
# approval -> execute -> verify) — "wait and re-observe" per point is inherited,
# never bypassed (RFC §4.1, decision D1). The data layer lives in plan.py; this
# section is ONLY the wire: the lease gate, one-step-per-call execution, the §1.3
# step/terminal replies, and the lease lifecycle.
#
# S-01: the dev copy's LEGACY single-call plan path (a whole plan executed in one
# call through the queue) is deliberately NOT ported — a plan is always stepwise
# here; a host that wants one-shot behavior passes a one-point plan.

#: The module-level lease guard (replaces the dev ``_PLANS_IN_FLIGHT`` set + the
#: ``bundle.extra`` bookkeeping helpers): atomic install, idempotent release — the
#: ONE exit for every terminal/session-end path.
_plan_guard = PlanLeaseGuard()


def _plan_counts(lease: PlanLease) -> dict[str, int]:
    """The ONE bounded count pair for every plan audit event (F4/VAL-WP8-01).

    Every emitter reads its counts here — no hand-summed copies, so the audit
    record's counts can never drift from the prefix the reply was built from.
    """
    return {
        "executed_count": sum(
            entry.get("kind") in {"executed", "dispatched_unverified"} for entry in lease.prefix
        ),
        "resolved_count": len(lease.prefix),
    }


def _audit_plan_event(
    bundle: Any,
    event_type: str,
    *,
    lease: PlanLease,
    result: str | None = None,
    **metadata: Any,
) -> None:
    """Emit one bounded plan lifecycle audit event (WP8) through the agent's sink.

    The redaction rules are the existing ones — the audit sink applies
    ``redact_text`` to every string field and metadata value, exactly as for every
    other event type; no plan-specific redaction exists.  Audit failures never
    break the plan path (the agent's ``_audit`` swallows them).  Every caller emits
    AFTER the reply's verdict fields are decided and reads ``result`` /
    ``failure_reason`` off the reply itself, so the audit record and the reply
    agree (E-05 rule).  Known sink convention (F4, documented in wp8-notes): the
    shared ``agent._audit`` drops ``None`` metadata values, so an explicit
    ``failure_reason: null`` (a verified step) is recorded as the key's ABSENCE —
    tests and consumers treat absent as null for plan events.
    """
    audit = getattr(getattr(bundle, "agent", None), "_audit", None)
    if audit is None:
        return
    audit(event_type, result=result, metadata={"plan_id": lease.context["plan_id"], **metadata})


def _audit_plan_terminal(
    bundle: Any,
    lease: PlanLease,
    *,
    reply_result: str,
    reason: str | None,
    failure_reason: str | None = None,
    **extra: Any,
) -> None:
    """Emit THE terminal lifecycle event of a plan — exactly once per lease.

    F3 (VAL-WP8-01): the terminal emission is IDEMPOTENT per lease — the first
    terminal path to reach it (an in-step TaskStopped handler or the session-end
    funnel racing it) wins, so a session killed mid-step can never produce a
    second terminal event for the same plan.

    F4 (VAL-WP8-01): ``reply_result`` is the RESULT TOKEN the terminal reply
    carries (verified / unknown / failed / stopped) — the event's ``result``
    equals the reply's, and ``plan_complete`` is emitted iff the plan verified.
    ``failure_reason`` is the reply's OWN failure_reason (null where the reply
    has none — absent ≡ null at the sink); ``reason`` is the lifecycle cause,
    consistent across paths: the reply's failure_reason where one exists
    (cancelled / invalid_action / execution_error / verification_*),
    ``completed`` for a verified plan, or the lifecycle cause no reply names
    (stop / replaced / exception).
    """
    if lease.terminal_audited:
        return
    lease.terminal_audited = True
    _audit_plan_event(
        bundle,
        "plan_complete" if reply_result == "verified" else "plan_stopped",
        lease=lease,
        result=reply_result,
        reason=reason or "",
        failure_reason=failure_reason,
        **_plan_counts(lease),
        **extra,
    )


#: Bounded retention for the lease's cross-call interference-event carry (D-3): the
#: guard produces at most a handful of events per step, but an unbounded lease is a
#: leak, so the retained list is deduped (order-preserving) and capped.
_PLAN_INTERFERENCE_EVENTS_MAX = 64

#: Bounded capture metadata per plan reply (the dev ``_plan_evidence`` bound).
_PLAN_EVIDENCE_CAPTURES_MAX = 32


def _plan_evidence(bundle: Any, lease: PlanLease) -> dict[str, Any]:
    """Return bounded backend/capture evidence WITHOUT fabricating observations.

    evidence-by-backend seam — WIRED (S-03 / decision D-2): the value is built
    from ``EvidenceStore.captures()`` (filled by the reply hooks, so it is real on
    BOTH backends) plus the lease's retained repositions — never from the fake
    backend's ``executed``/``observed`` attributes the dev copy read (those do not
    exist on ``LocalComputerBackend``; the dev block was fake-only). Served under
    :data:`PLAN_EVIDENCE_KEY` (``evidence_by_backend``) so ``evidence`` keeps meaning
    exactly one thing everywhere: a LIST of evidence lines.
    """
    store = evidence_store(bundle.extra)
    captures = store.captures() if store is not None else []
    return {
        "captures": captures[-_PLAN_EVIDENCE_CAPTURES_MAX:],
        "repositions": list(lease.repositions),
    }


def _active_plan_tool_rejection(bundle: Any) -> dict[str, Any] | None:
    """Reject an independent tool while a plan lease is waiting (typed ``plan_lease``).

    Read-only surfaces that CONSUME the lease's own state stay allowed:
    ``computer_wait_status`` (read-only, master-mission WP6 decision) and
    ``computer_fetch_image`` (S-04: a fetch is a READ-ONLY retrieval of evidence the
    session already holds). Refused: flat computer_execute (its own gate inside
    computer_execute), computer_wait, computer_cancel_wait, and the independent
    observation tools computer_observe / computer_zoom / computer_screenshot (dev
    parity: a mid-plan independent observation would desynchronize the lease's
    grounding premise; recorded as the WP7b observe/zoom decision).
    """
    lease = _plan_guard.active_lease(bundle)
    if lease is None:
        return None
    return {
        "ok": False,
        "error": "plan_lease",
        "blocked": True,
        "message": "A stepwise plan lease is active; finish or stop the plan (continue/stop/replace) first.",
        "executed_prefix": [_redact_queue_entry(entry) for entry in lease.prefix],
        "remaining": plan_layer.plan_remaining(lease, "plan_lease_active"),
        PLAN_EVIDENCE_KEY: _plan_evidence(bundle, lease),
        "next_step": plan_layer.plan_next_step(lease),
    }


def _plan_lease_refusal(bundle: Any, lease: PlanLease, message: str) -> dict[str, Any]:
    """The typed zero-effect ``plan_lease`` refusal for a misdirected execute call.

    DELTA 1 #6 (§1.1): an execute-surface refusal is a REJECTED reply — it carries
    the closed ``result:"rejected"`` token and a ``reasons`` list naming the typed
    code, beside the kept ``error``/``blocked``/handshake keys (the validate_reply
    ``plan_refusal`` surface)."""
    return {
        "ok": False,
        "result": "rejected",
        "reasons": ["plan_lease"],
        "error": "plan_lease",
        "blocked": True,
        "message": message,
        "executed_prefix": [_redact_queue_entry(entry) for entry in lease.prefix],
        "remaining": plan_layer.plan_remaining(lease, "plan_lease_active"),
        PLAN_EVIDENCE_KEY: _plan_evidence(bundle, lease),
        "next_step": plan_layer.plan_next_step(lease),
    }


def _cutoff_budget_fields(
    bundle: Any,
    plan_context: dict[str, Any],
    *,
    limit_name: str | None,
    executed_count: int,
) -> dict[str, Any]:
    """Build the four truthful budget fields required at every cut."""
    required = int(plan_context.get("normalized_item_count") or 0)
    available = 0
    if limit_name == "max_actions":
        available = max(
            0,
            int(getattr(bundle.enforcer.limits, "max_actions", 0))
            - int(getattr(bundle.enforcer, "_actions", 0)),
        )
    elif limit_name == "max_steps":
        available = max(0, int(bundle.state.max_steps) - int(bundle.state.step_count))
    else:
        # Non-action gates still expose a bounded statement; zero is truthful when
        # the available budget is not a simple session counter.
        available = 0
    return {
        "limit_name": limit_name,
        "required": required,
        "available": available,
        "executed_count": int(executed_count),
    }


def _plan_limit_cutoff_from_outcome(
    outcome: Any,
    bundle: Any | None = None,
    plan_context: dict[str, Any] | None = None,
    prefix_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Identify the established max_steps cut without introducing preflight policy."""
    if outcome.follow_ups_stopped_reason != "safety_denied":
        return None
    entries = list(prefix_entries if prefix_entries is not None else (outcome.follow_up_results or []))
    if not entries:
        return None
    final = list(outcome.follow_up_results or [entries[-1]])[-1]
    if (
        final.get("kind") == "safety_denied"
        and final.get("message") == "Maximum session steps reached."
    ):
        executed_count = sum(
            entry.get("kind") in {"executed", "dispatched_unverified"}
            for entry in entries
        )
        if bundle is not None and plan_context is not None:
            return {
                **_cutoff_budget_fields(
                    bundle, plan_context, limit_name="max_steps", executed_count=executed_count
                ),
                "resolved_count": len(entries),
            }
        return {
            "limit_name": "max_steps",
            "required": 0,
            "available": 0,
            "executed_count": executed_count,
            "resolved_count": len(entries),
        }
    return None


def _build_plan_aggregate(
    plan_context: dict[str, Any],
    outcome: Any | None = None,
    *,
    follow_up_results: list[dict[str, Any]] | None = None,
    stopped_reason: str | None = None,
    interference_events: list[str] | None = None,
    cutoff: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the per-point aggregate from the known queue prefix (§4.4/B4-8).

    Normal calls pass the first ``SingleActionOutcome`` unchanged.  Limit/cancel
    paths pass the mutable prefix retained by the plan context, never a fabricated
    remainder.  An entry's ``ok`` stays the queue's own verdict; a point is
    ``completed`` only when EVERY item is present and ok, while a known but
    incomplete prefix is explicitly ``partial``.  Redaction remains at this sink.
    The per-action rows echo ``hold_ms`` (E2) exactly as the queue entries carry it.
    """
    if outcome is not None:
        raw_entries = list(outcome.follow_up_results or [])
        stopped_reason = outcome.follow_ups_stopped_reason
        interference_events = outcome.interference_events
    else:
        raw_entries = list(follow_up_results or [])
    entries = [
        _redact_queue_entry(entry) if entry is not None else None
        for entry in raw_entries
    ]
    points_out: list[dict[str, Any]] = []
    last_entry_index = max(
        (index for index, entry in enumerate(entries) if entry is not None),
        default=-1,
    )
    for point in plan_context["points"]:
        point_entries = [
            entry
            for entry in entries[point["first_item"] : point["last_item"] + 1]
            if entry is not None
        ]
        if not point_entries:
            status = "skipped"
        else:
            all_ok = all(bool(entry.get("ok")) for entry in point_entries)
            complete_count = point["last_item"] - point["first_item"] + 1
            point_complete = len(point_entries) == complete_count
            has_failed = any(
                entry.get("verification_outcome") == "failed" or entry.get("kind") != "executed"
                for entry in point_entries
            )
            has_uncertain = any(
                entry.get("verification_outcome") == "uncertain" for entry in point_entries
            )
            has_dispatched_unknown = any(
                entry.get("kind") == "dispatched_unverified" for entry in point_entries
            )
            stop_landed_here = (
                stopped_reason is not None
                and point["first_item"] <= last_entry_index <= point["last_item"]
            )
            if all_ok and point_complete:
                status = "completed"
            elif all_ok and not point_complete:
                status = "partial"
            elif has_dispatched_unknown:
                status = "uncertain_stopped"
            elif has_failed:
                status = "failed"
            elif has_uncertain:
                status = "uncertain_stopped" if stop_landed_here else "uncertain"
            else:
                status = "failed"
        transition_entry = point_entries[0] if point_entries else None
        actions_out = [
            {
                "action": entry.get("action_type"),
                "hold_ms": entry.get("hold_ms"),
                "outcome": entry.get("kind"),
                "verification": entry.get("verification_outcome"),
                "ok": bool(entry.get("ok")),
            }
            for entry in point_entries[1:]
        ]
        points_out.append(
            {
                "id": point["id"],
                "index": point["index"],
                "status": status,
                "timeout_ms": point["timeout_ms"],
                "transition": (
                    {
                        "action": transition_entry.get("action_type"),
                        "duration_ms": transition_entry.get("duration_ms"),
                        "outcome": transition_entry.get("kind"),
                        "verification": transition_entry.get("verification_outcome"),
                        "ok": bool(transition_entry.get("ok")),
                    }
                    if transition_entry is not None
                    else None
                ),
                "actions": actions_out,
            }
        )
    final_wait_outcome: dict[str, Any] | None = None
    final_wait_item = plan_context["final_wait_item"]
    if final_wait_item is not None and len(entries) > final_wait_item:
        entry = entries[final_wait_item]
        # Sparse prefix (F1 fix 2.1): every item the plan did not reach is padded
        # with ``None`` (``_plan_aggregate_for_state``), and the trailing wait is
        # ALWAYS the last item — so it is ``None`` in exactly the case this
        # aggregate exists for: a response issued before the wait ran (any halted
        # step, ``plan_action="stop"``, ``plan_action="continue"``, the terminal
        # reply).  Apply the same ``is not None`` guard the point loop above
        # already uses and NAME the wait instead of raising out of the tool.
        final_wait_outcome = (
            {"outcome": "not_executed", "ok": False}
            if entry is None
            else {"outcome": entry.get("kind"), "ok": bool(entry.get("ok"))}
        )
    aggregate = {
        "plan_id": plan_context["plan_id"],
        "status": (
            "completed"
            if stopped_reason is None
            else "cancelled"
            if stopped_reason == "stopped"
            else "failed"
        ),
        "stopped_reason": stopped_reason,
        "points": points_out,
        "interference_events": list(interference_events or []),
        "final_wait_ms": plan_context["final_wait_ms"],
        **({"final_wait_outcome": final_wait_outcome} if final_wait_outcome is not None else {}),
    }
    if cutoff is not None:
        aggregate["cutoff"] = dict(cutoff)
    return aggregate


def _plan_aggregate_for_state(
    lease: PlanLease,
    *,
    status: str,
    stopped_reason: str | None,
    current_events: list[str] | None = None,
) -> dict[str, Any]:
    """Build the additive aggregate from the exact retained prefix.

    ``current_events`` is the step being built RIGHT NOW.  The lease is appended to
    further down in ``_plan_response`` (the only place the outcome and the lease are
    both in scope), i.e. AFTER this aggregate is built, so without this the reply for
    the very step that observed a guard event would under-report its own — a
    pre-dispatch rejection has no later call to wait for, because the point does not
    advance.
    """
    aligned: list[dict[str, Any] | None] = [None] * len(lease.specs)
    for entry, source_index in zip(lease.prefix, lease.source_indices, strict=False):
        if 0 <= source_index < len(aligned):
            aligned[source_index] = entry
    aggregate = _build_plan_aggregate(
        lease.context,
        follow_up_results=aligned,  # type: ignore[arg-type]
        stopped_reason=stopped_reason,
        # ORVEX-2026-105 D-3: the retained lease list is the only thing that can
        # carry a previous step's guard events — the aggregate is rebuilt from the
        # lease on every reply.
        interference_events=[*lease.interference_events, *(current_events or [])],
    )
    aggregate["status"] = status
    return aggregate


def _step_interference_events(outcome: Any) -> list[str]:
    """The step's merged interference events (DELTA 1 #3): EVERY queue item's events,
    collected by the queue into ``follow_up_interference_events``; falls back to the
    first outcome's own list for non-queue outcomes."""
    merged = getattr(outcome, "follow_up_interference_events", None)
    if merged is None:
        return list(outcome.interference_events or [])
    return list(merged)


def _plan_retain_events(lease: PlanLease, events: list[str] | None) -> None:
    """Dedupe + bound the lease's cross-call interference-event carry (D-3/WP3).

    Appended at the one site where the step outcome and the lease are simultaneously
    in scope; a later call (continue/stop/terminal) runs in a fresh frame and would
    otherwise report ``interference_events: []`` for a guard event the host was
    already told about in an earlier reply. Dedup preserves first-seen order; the
    oldest entries beyond the cap are dropped (bounded lease).
    """
    if not events:
        return
    seen = set(lease.interference_events)
    for event in events:
        if event not in seen:
            lease.interference_events.append(event)
            seen.add(event)
    del lease.interference_events[:-_PLAN_INTERFERENCE_EVENTS_MAX]


def _plan_carry_events(response: dict[str, Any], lease: PlanLease) -> None:
    """Mirror the lease's retained events onto a reply that owns no outcome of its own.

    ORVEX-2026-105 D-3: ``_plan_response`` publishes the STEP's events on its own
    top-level key, but the terminal / exception / walk-completed replies are built in
    a LATER call whose frame never saw a ``SingleActionOutcome`` — without this the
    events would survive only inside the nested aggregate.  Conditional on a
    non-empty lease so a plan that observed nothing keeps its exact pre-existing key
    set.
    """
    if lease.interference_events:
        response["interference_events"] = list(lease.interference_events)


def _plan_step_result(step_entries: list[dict[str, Any]]) -> str:
    """The STEP reply's ``result`` token (§1.3 plan-step surface).

    The terminal aggregate vocabulary names an all-uncertain step ``unknown``; the
    §1.3 step surface names that same state ``uncertain`` — one classifier
    (:func:`plan.plan_result_from_entries`), one mapping at the surface. Step
    replies never say ``unknown``; only a terminal plan reply can.
    """
    aggregate = plan_layer.plan_result_from_entries(step_entries)
    return "uncertain" if aggregate == "unknown" else aggregate


def _plan_not_executed(lease: PlanLease, reason: str) -> list[dict[str, Any]]:
    """Decision 3: the explicit, bounded list of plan work that never executed.

    Same shape and same boundedness as ``remaining`` (decision 6 additive).  The
    only difference is the per-step ``reason``, which carries the typed failure
    classification on EVERY entry — including the partially attempted current step,
    whose stored remainder still carries the generic historical phrase.  A verified
    step is not a failure, so it reports ``awaiting_plan_action``.
    """
    return plan_layer.plan_not_executed(lease, reason)


def _plan_terminal_response(
    bundle: Any,
    lease: PlanLease,
    *,
    result: str,
    reason: str,
) -> dict[str, Any]:
    """Return a terminal stop/replace handoff with no backend or capture effect."""
    aggregate = _plan_aggregate_for_state(
        lease, status="stopped" if reason == "stop" else "failed", stopped_reason=reason
    )
    executed_count = sum(
        entry.get("kind") in {"executed", "dispatched_unverified"} for entry in lease.prefix
    )
    cutoff = {
        **_cutoff_budget_fields(
            bundle, lease.context, limit_name="stop", executed_count=executed_count
        )
    }
    # The host closed this plan on purpose: ``stopped`` never verifies (V/4244 ok
    # rule), so ``ok`` is False regardless of how good the prefix looked.
    outcome = plan_layer.plan_terminal_outcome(
        lease.prefix, stopped=True, expected_items=int(lease.context["normalized_item_count"])
    )
    assert outcome.result == "stopped" and outcome.ok is False
    response: dict[str, Any] = {
        "ok": False,
        "result": result,
        "message": (
            "Plan stopped by the host; the executed prefix is preserved."
            if reason == "stop"
            else reason
        ),
        "blocked": False,
        "plan_id": lease.context["plan_id"],
        "executed_prefix": [_redact_queue_entry(entry) for entry in lease.prefix],
        "follow_up_results": [_redact_queue_entry(entry) for entry in lease.prefix],
        "remaining": plan_layer.plan_remaining(lease, reason),
        PLAN_EVIDENCE_KEY: _plan_evidence(bundle, lease),
        "cutoff": cutoff,
        "plan": aggregate,
        "step_index": max(lease.cursor - 1, 0),
        "next_step": None,
        # Batch 6 (decision 3, additive): the host closed this plan on purpose, so
        # nothing FAILED; the work it chose not to run is named by ``not_executed``.
        "failure_reason": None,
        "not_executed": _plan_not_executed(lease, reason),
    }
    response["plan"]["cutoff"] = dict(response["cutoff"])
    _plan_carry_events(response, lease)
    # WP8: the host closed the plan on purpose — the lifecycle ends as ``stopped``
    # (after the reply fields above are final, E-05); the reason names the
    # lifecycle cause ("stop"), the reply's failure_reason stays null.
    _audit_plan_terminal(
        bundle, lease, reply_result=str(response["result"]), reason=reason
    )
    return response


def _plan_exception_response(
    bundle: Any,
    lease: PlanLease,
    *,
    step_index: int,
    result: str,
    reason: str,
    blocked: bool,
    stopped: bool = False,
    failure_reason: str | None = None,
    limit_name: str | None = None,
) -> dict[str, Any]:
    """Return a truthful failed/unknown step without claiming unobserved work.

    F6 (VAL-WP8-01): ``limit_name`` comes from ``exc.limit_name`` at the handler —
    EVERY limit trip (not only ``max_actions``/``max_steps``) gets the agreeing
    ``error:"limit_exceeded"`` + ``limit`` + ``cutoff`` fields next to
    ``failure_reason:"limit_exceeded"``.
    """
    if failure_reason is None:
        failure_reason = (
            "cancelled"
            if stopped
            else ("limit_exceeded" if limit_name is not None else "execution_error")
        )
    response: dict[str, Any] = {
        "ok": False,
        "result": result,
        "message": reason,
        "blocked": blocked,
        "plan_id": lease.context["plan_id"],
        "step_index": step_index,
        "step_number": step_index + 1,
        "executed_prefix": [_redact_queue_entry(entry) for entry in lease.prefix],
        "follow_up_results": [_redact_queue_entry(entry) for entry in lease.prefix],
        "remaining": plan_layer.plan_remaining(lease, reason),
        PLAN_EVIDENCE_KEY: _plan_evidence(bundle, lease),
        "next_step": plan_layer.plan_next_step(lease) if blocked else None,
        "plan": _plan_aggregate_for_state(
            lease,
            status="blocked" if blocked else ("cancelled" if stopped else "failed"),
            stopped_reason=reason,
        ),
        "failure_reason": failure_reason,
        "not_executed": _plan_not_executed(lease, failure_reason),
    }
    if limit_name is not None:
        response["error"] = "limit_exceeded"
        response["limit"] = limit_name
    else:
        # Same literals the sibling reply builders already use for the very same
        # conditions; ``action_error`` matches ``outcome.kind == "error"`` exactly.
        response["error"] = {
            "execution_error": "action_error",
            "invalid_action": "invalid_action",
            "cancelled": "cancelled",
        }.get(failure_reason, "action_error")
    if limit_name is not None or stopped:
        cutoff_name = limit_name or "cancel"
        response["cutoff"] = {
            **_cutoff_budget_fields(
                bundle,
                lease.context,
                limit_name=cutoff_name,
                # F4: one count helper — the same executed_count every audit
                # event reads.
                executed_count=_plan_counts(lease)["executed_count"],
            ),
            "resolved_count": len(lease.prefix),
        }
        response["plan"]["cutoff"] = dict(response["cutoff"])
    if stopped:
        response["stopped"] = True
    _plan_carry_events(response, lease)
    return response


def _plan_response(
    bundle: Any,
    lease: PlanLease,
    outcome: Any,
    *,
    step_index: int,
    step_entries: list[dict[str, Any]],
    include_screenshot_after: bool | None,
    image_mode: str,
    view_provider: Any | None,
    complete_walk: bool = False,
) -> Any:
    """Assemble the §1.3 plan-step reply around the per-item pipeline payload.

    One call == one waypoint step == one full pipeline pass (WP1 contract): the
    reply carries the step's ``result`` (the §1.3 plan-step token set), the typed
    ``failure_reason`` (ALWAYS, ``None`` on a verified step), ``not_executed``, the
    ``evidence_by_backend`` block (store captures + lease repositions, both
    backends), the executed prefix, the per-point aggregate (E2 ``hold_ms`` echo),
    and the step's interference events (D-3, retained on the lease).
    """
    if outcome.kind == "rejected":
        # §1.1: a gate refusal is a REJECTED reply — reasons, never an error key.
        response: dict[str, Any] = {
            "ok": False,
            "result": "rejected",
            "message": outcome.message or "Action rejected (no gate detail available).",
            "reasons": list(outcome.reasons),
        }
        if outcome.partial_delivery is not None:
            response["partial_delivery"] = dict(outcome.partial_delivery)
    elif outcome.kind == "safety_denied":
        response = {
            "ok": False,
            "result": "rejected",
            "message": outcome.message,
            "reasons": rejection_reasons(outcome),
        }
    elif outcome.kind == "approval_required":
        # §1.1 approval rule (O-03): ok=False, result="not_executed",
        # requires_approval=True — the SAME envelope as the flat approval gate.
        response = {
            "ok": False,
            "result": "not_executed",
            "requires_approval": True,
            "message": outcome.message,
        }
    elif outcome.kind == "digest_surprise":
        response = {
            "ok": False,
            "result": "rejected",
            "message": outcome.message,
            "reasons": rejection_reasons(outcome),
        }
    elif outcome.kind == "error":
        # §1.2: an operation error is ``failed``; the typed code stays.
        response = {"ok": False, "result": "failed", "error": "action_error", "message": outcome.message}
    else:
        result = outcome.result
        if result is None:
            response = {
                "ok": False,
                "result": "failed",
                "error": "action_error",
                "message": "Execution produced no result.",
            }
        else:
            response = _redact_result_payload(result.model_dump())
            response.update(
                {
                    "model_confidence": outcome.model_confidence,
                    "grounding_confidence": outcome.grounding_confidence,
                    "verification_confidence": outcome.verification_confidence,
                }
            )
    # The STEP verdict comes from the step's own entries through the ONE classifier
    # (never from the first item's token): rejected/approval outcomes name their own
    # §1.3 token directly.
    # DELTA 1 #5: a session killed mid-step (stop_session / idle reap / the internal
    # kill path) is a KILLED plan, not a finished one — the reply says ``stopped``
    # (ok false) even though the lease is already gone and the queue may have run.
    session_killed = bool(getattr(bundle.state, "stopped", False)) or bool(
        getattr(getattr(bundle.context, "stop", None), "stopped", False)
    )
    if outcome.kind == "approval_required":
        step_result = "not_executed"
    elif outcome.kind in {"rejected", "safety_denied", "digest_surprise"}:
        step_result = "rejected"
    elif outcome.kind == "error":
        step_result = "failed"
    elif session_killed:
        step_result = "stopped"
    else:
        step_result = _plan_step_result(step_entries)
        # §1.1 approval rule: when the step's queue was halted by an approval hold on
        # a LATER item (the first outcome is the executed transition move), the step
        # verdict is the approval token, not a failure — the point's action never
        # dispatched and approval is pending.
        if step_result == "failed" and any(
            entry.get("requires_approval") for entry in step_entries
        ):
            step_result = "not_executed"
    response["result"] = step_result
    response["ok"] = ok_from_result(step_result)
    if step_result == "not_executed":
        response["requires_approval"] = True
    response["follow_up_results"] = [_redact_queue_entry(entry) for entry in lease.prefix]
    response["follow_ups_stopped_reason"] = outcome.follow_ups_stopped_reason
    cutoff = _plan_limit_cutoff_from_outcome(
        outcome, bundle, lease.context, prefix_entries=lease.prefix
    )
    if cutoff is not None:
        response["cutoff"] = dict(cutoff)
        response["error"] = "limit_exceeded"
        response["limit"] = cutoff["limit_name"]
    response["executed_prefix"] = [_redact_queue_entry(entry) for entry in lease.prefix]
    response["step_index"] = step_index
    response["step_number"] = step_index + 1
    response[PLAN_EVIDENCE_KEY] = _plan_evidence(bundle, lease)
    response["next_step"] = plan_layer.plan_next_step(lease)
    response["remaining"] = plan_layer.plan_remaining(
        lease,
        "awaiting_plan_action" if response["result"] != "failed" else "previous_step_failed",
    )
    step_failure_reason = plan_failure_reason(
        outcome, step_entries, result=response["result"], error=response.get("error")
    )
    response["failure_reason"] = step_failure_reason
    response["not_executed"] = _plan_not_executed(
        lease, step_failure_reason or "awaiting_plan_action"
    )
    lease_active = _plan_guard.active_lease(bundle) is lease
    response["blocked"] = lease_active and not session_killed
    response["plan_id"] = lease.context["plan_id"]
    if lease.replaced_plan_id is not None:
        # WP7b M2 (VAL-WP7b-03): the replace disclosure rides the PAYLOAD (before the
        # content blocks are built), so an image-bearing replace reply — a
        # content-block LIST — keeps it too, exactly like a plain-dict reply.
        response["replaced_plan_id"] = lease.replaced_plan_id
        response["replaced_prefix"] = list(lease.replaced_prefix or [])
    response["message"] = response.get("message") or (
        f"Plan step {step_index + 1} executed; the lease awaits continue/stop/replace."
        if lease_active
        else f"Plan step {step_index + 1} executed."
    )
    if session_killed:
        # DELTA 1 #5: the session ended mid-step — the aggregate must not read as a
        # finished plan either.  F2 (VAL-WP8-01): the failure_reason is
        # UNCONDITIONALLY "cancelled" — the kill is the cause whatever the
        # classifier saw, and an entry-derived token must not survive it.
        response["message"] = (
            "The session was stopped while the plan step was in flight; the plan "
            "did not finish."
        )
        response["stopped"] = True
        response["failure_reason"] = "cancelled"
    response["plan"] = _plan_aggregate_for_state(
        lease,
        status=(
            "cancelled"
            if session_killed
            else "blocked"
            if lease_active
            else "completed"
        ),
        stopped_reason=(
            "stopped"
            if session_killed
            else "awaiting_plan_action"
            if lease_active
            else None
        ),
        # DELTA 1 #3: the step's events are the MERGED per-item list (every queue
        # item), not only the first outcome's.
        current_events=_step_interference_events(outcome),
    )
    if cutoff is not None:
        response["plan"]["cutoff"] = dict(cutoff)
        response["plan"]["stopped_reason"] = "limit_exceeded"
    # ORVEX-2026-105 D-3: publish the STEP's events on their own top-level key AND
    # retain them on the lease (deduped, bounded) — the only place the per-step
    # outcome and the lease are both in scope.
    _plan_retain_events(lease, _step_interference_events(outcome))
    if lease.interference_events:
        response["interference_events"] = list(lease.interference_events)
    if complete_walk:
        # DELTA 2 N1/N3: this step verified itself and completed the walk — the
        # verdict fields become the whole-plan aggregate's (released here, once);
        # everything else the step disclosed stays in the payload (the image tail
        # below still runs, so the walk-ending reply carries the frame + image_id).
        _apply_terminal_verdict_fields(bundle, lease, response)
    # WP8: one plan_point audit event per step reply, emitted AFTER the verdict
    # fields are final (E-05: the metadata is read off the reply itself, so the
    # audit record and the reply can never disagree).  A walk-ending step ALSO
    # closes the lifecycle — with the SAME result token and the SAME reason
    # formula the terminal-walk reply uses (F4).
    _audit_plan_event(
        bundle,
        "plan_point",
        lease=lease,
        result=str(response["result"]),
        step_index=response["step_index"],
        step_number=response["step_number"],
        failure_reason=response.get("failure_reason"),
        **_plan_counts(lease),
    )
    if complete_walk:
        _audit_plan_terminal(
            bundle,
            lease,
            reply_result=str(response["result"]),
            reason=(
                response.get("failure_reason")
                or ("completed" if response["result"] == "verified" else "")
            ),
            failure_reason=response.get("failure_reason"),
        )
    # v07-007 R-21: non-executed shapes embed model-proposed text in ``message`` —
    # the SAME redaction the flat path applies.
    if outcome.kind in {"rejected", "safety_denied", "approval_required", "digest_surprise", "error"}:
        response = _redact_result_payload(response)
    if include_screenshot_after is False:
        # PERF-004 C4 opt-out: no image, honest None scale.
        if response.pop("screenshot_after_base64", None) is not None:
            response[IMAGE_OUTBOUND_SCALE_KEY] = None
    if outcome.kind != "executed" or "screenshot_after_base64" not in response:
        return response
    if image_mode == "text":
        # D1: text mode dominates — the attachment and bytes are removed, never the
        # retained frame or its id (WP5 retention rule).
        response.pop("screenshot_after_base64", None)
        response["image_delivery"] = "text"
        response[IMAGE_OUTBOUND_SCALE_KEY] = None
        response["image_delivery_note"] = IMAGE_DELIVERY_TEXT_NOTE
        return response
    frame = getattr(outcome.result, "_frame", None)
    full_resolution = include_screenshot_after is True or _action_image_full()
    view_id: str | None = None
    view_payload: tuple[str, Any] | None = None
    if view_provider is not None:
        view_id = view_provider.id
        if not view_provider.passthrough:
            try:
                derived = view_provider.derive(
                    observation_for_payload(str(response["screenshot_after_base64"]), frame)
                )
                started = time.perf_counter()
                encoded_b64 = encode_png_b64(derived.image)
                bundle.metrics.record_latency(
                    "view_derive_ms", (time.perf_counter() - started) * 1000.0
                )
                view_payload = (encoded_b64, derived.image)
            except VisualViewError as exc:  # fail-closed: no image, no raw fallback
                response.pop("screenshot_after_base64", None)
                response["visual_view"] = view_id
                response["visual_view_error"] = f"view_derive_failed: {exc}"
                return response
            except Exception as exc:  # noqa: BLE001 - fail-closed, no traceback
                response.pop("screenshot_after_base64", None)
                response["visual_view"] = view_id
                response["visual_view_error"] = (
                    f"view_derive_failed: {type(exc).__name__}: {exc}"
                )
                return response
    # WP5 retention rule: THIS reply carries the post-action image, so the frame is
    # retained HERE — the bytes kept are EXACTLY the bytes the reply carries (the
    # derived view when one was served, otherwise the raw capture), both pre-ladder.
    carried_b64 = (
        str(view_payload[0]) if view_payload is not None else str(response["screenshot_after_base64"])
    )
    response.update(
        register_frame(
            bundle.extra,
            payload=carried_b64,
            source="computer_execute(plan_step)",
            attached=True,
            visual_view=view_id,
        )
    )
    # S-03: the evidence block is (re)built AFTER this reply's own frame was
    # retained, so ``captures`` includes the capture THIS reply carries — the same
    # truth the dev block reported from the backend's observation list.
    response[PLAN_EVIDENCE_KEY] = _plan_evidence(bundle, lease)
    return _execute_response_blocks(
        response,
        frame=frame,
        full_resolution=full_resolution,
        view_id=view_id,
        view_payload=view_payload,
    )


def _apply_terminal_verdict_fields(
    bundle: Any, lease: PlanLease, response: dict[str, Any]
) -> None:
    """Override a walk-ending STEP reply payload with the whole-plan aggregate
    verdict (in place, BEFORE the content blocks are built).

    DELTA 2 N1/N3: the reply keeps everything the completing step disclosed (image
    block + image_id, interference events, per-item fields like requires_approval /
    reasons / partial_delivery — ``_plan_response`` already retained the step's
    events on the lease and in the aggregate); ONLY the verdict fields are decided
    by ``plan_terminal_outcome`` over the whole retained prefix, so an earlier
    uncertain/failed point caps the reply at ``unknown``/``failed`` with
    ``ok:false`` even though the last step itself verified (the V/4244 fix). The
    lease is released exactly once here."""
    outcome = plan_layer.plan_terminal_outcome(
        lease.prefix,
        expected_items=int(lease.context["normalized_item_count"]),
    )
    result = outcome.result
    failure_reason = (
        None if result == "verified"
        else plan_failure_reason(None, lease.prefix, result=result)
    )
    _plan_guard.release(str(bundle.context.session_id), bundle)
    response["result"] = result
    response["ok"] = outcome.ok
    response["failure_reason"] = failure_reason
    response["message"] = (
        "Plan complete; every item verified."
        if result == "verified"
        else f"Plan walk finished; terminal verdict: {result}."
    )
    response["blocked"] = False
    response["next_step"] = None
    response["remaining"] = []
    response["not_executed"] = [] if result == "verified" else _plan_not_executed(lease, failure_reason)
    # WP7b nit (VAL-WP7b-03, decided): for a terminal ``unknown`` the nested
    # ``plan.status`` now reads ``"unknown"`` — the same token the top level uses —
    # instead of ``"blocked"``.  ``blocked`` claims the plan is parked awaiting the
    # host; a terminal walk is finished with nothing pending, and top-level
    # ``blocked`` is false here.  "unknown" (§1.2: at least one step uncertain, none
    # failed) is the honest aggregate status, and the nested status vocabulary gains
    # it as a documented terminal-only member.
    response["plan"]["status"] = (
        "completed" if result == "verified" else "failed" if result == "failed" else "unknown"
    )
    response["plan"]["stopped_reason"] = None if result == "verified" else failure_reason


def _plan_terminal_walk_reply(bundle: Any, lease: PlanLease) -> dict[str, Any]:
    """THE terminal reply of a finished plan walk (V/4244, VAL-WP7b-01 #1).

    Serves the ``continue`` path whose cursor is already past the last step (the
    parked-at-the-end shape of DELTA 2 N3: the last step did not verify itself, so
    it parked, and this is the host's explicit walk-end request). The final step's
    events and frame are already on the lease (retained by ``_plan_response`` when
    that step replied), so the aggregate over the retained prefix carries them.
    Releases the lease exactly once (the guard's release is idempotent)."""
    _plan_guard.release(str(bundle.context.session_id), bundle)
    # V/4244 (F2-0 honesty): the walk is over, but "the cursor passed the last
    # step" is not a success claim. The terminal verdict is read off the retained
    # prefix through plan.py's aggregate — ``ok`` is True ONLY for a fully-verified
    # plan (``ok = (result == "verified")`` by construction); unknown and stopped
    # are never success, and a partial prefix is capped at ``unknown`` by
    # ``expected_items``.
    outcome = plan_layer.plan_terminal_outcome(
        lease.prefix,
        expected_items=int(lease.context["normalized_item_count"]),
    )
    result = outcome.result
    # The typed classification rides the entries themselves (DELTA 1 #1 + F1,
    # VAL-WP8-01): with outcome=None the classifier reads the ENTRIES — an
    # approval-held prefix classifies ``approval_required``, a rejected follow-up
    # ``rejected``, a failed one ``verification_failed`` / ``execution_error``, an
    # uncertain one ``verification_uncertain`` — never a blanket token and never
    # ``unclassified`` for a prefix the entries can name.
    failure_reason = (
        None if result == "verified"
        else plan_failure_reason(None, lease.prefix, result=result)
    )
    # WP7b nit (VAL-WP7b-03, decided): the aggregate status of a terminal ``unknown``
    # is ``"unknown"`` — the same token the top level reports — never ``"blocked"``
    # (a finished walk parks nothing; top-level ``blocked`` is false here).
    aggregate_status = (
        "completed" if result == "verified" else "failed" if result == "failed" else "unknown"
    )
    aggregate_reason = None if result == "verified" else failure_reason
    completed: dict[str, Any] = {
        "ok": outcome.ok,
        "result": result,
        "message": (
            "Plan complete; every item verified."
            if result == "verified"
            else f"Plan walk finished; terminal verdict: {result}."
        ),
        "blocked": False,
        "plan_id": lease.context["plan_id"],
        "step_index": max(lease.cursor - 1, 0),
        "executed_prefix": [_redact_queue_entry(entry) for entry in lease.prefix],
        "follow_up_results": [_redact_queue_entry(entry) for entry in lease.prefix],
        "remaining": [],
        PLAN_EVIDENCE_KEY: _plan_evidence(bundle, lease),
        "next_step": None,
        "plan": _plan_aggregate_for_state(
            lease, status=aggregate_status, stopped_reason=aggregate_reason
        ),
        # A verified plan ran every declared item; a non-verified one names the work
        # that never produced evidence (a final step cut short by its own policy
        # stop, the final wait, ...).
        "failure_reason": failure_reason,
        "not_executed": [] if result == "verified" else _plan_not_executed(lease, failure_reason),
    }
    _plan_carry_events(completed, lease)
    # WP8: the terminal walk reply closes the lifecycle — result token identical
    # to the reply's, reason by the SAME formula the walk-ending step uses (F4).
    _audit_plan_terminal(
        bundle,
        lease,
        reply_result=result,
        reason=failure_reason or ("completed" if result == "verified" else ""),
        failure_reason=failure_reason,
    )
    return completed


async def _execute_plan_step(
    bundle: Any,
    lease: PlanLease,
    *,
    include_screenshot_after: bool | None,
    image_mode: str,
    view_provider: Any | None,
) -> Any:
    """Execute exactly one waypoint step and retain the lease unless terminal."""
    session_id = str(bundle.context.session_id)
    if lease.cursor >= len(lease.steps):
        return _plan_terminal_walk_reply(bundle, lease)
    step_index = lease.cursor
    step_specs = lease.steps[step_index]
    lease.remainder = []
    step_context, policies = plan_layer.plan_step_context(lease, step_index, step_specs)
    try:
        grounded = step_specs[0].to_grounded(reason_prefix="MCP plan action")
        for spec in step_specs:
            spec.to_grounded(reason_prefix="MCP plan action")
    except Exception as exc:  # noqa: BLE001 - structural rejection is typed
        _plan_guard.release(session_id, bundle)
        # WP8: the structural rejection ends the lease — audit the stop (E-05: the
        # reply below carries result "failed" / failure_reason "invalid_action";
        # F4: the counts are the real prefix, which may already hold executed
        # entries from earlier steps).
        _audit_plan_terminal(
            bundle, lease, reply_result="failed", reason="invalid_action",
            failure_reason="invalid_action",
        )
        return _plan_exception_response(
            bundle,
            lease,
            step_index=step_index,
            result="failed",
            reason=f"Plan step could not be converted: {exc}",
            blocked=False,
            failure_reason="invalid_action",
        )
    try:
        # O-03: approval was evaluated ONCE on the FIRST plan call and is inherited
        # by every step and by replace — ``continue`` never re-asks.
        outcome = await bundle.agent.run_single(
            bundle.state,
            grounded,
            approved=lease.approved,
            expected_effect=step_specs[0].expected_effect,
            follow_ups=step_specs[1:],
            follow_ups_cap=len(step_specs) - 1,
            plan_policies=policies,
            plan_context=step_context,
        )
    except TaskStopped as exc:
        step_entries = list(step_context.get("partial_results") or [])
        plan_layer.merge_plan_step_prefix(
            lease,
            step_entries,
            source_offset=int(lease.context["points"][step_index]["first_item"]),
        )
        plan_layer.set_plan_step_remainder(lease, step_index, step_specs, step_entries)
        # TaskStopped is the cancellation contract, including synthetic seams.
        if plan_layer.plan_step_attempted(step_entries):
            lease.cursor = plan_layer.plan_cursor_after(lease.cursor, step_index, step_entries)
        _plan_guard.release(session_id, bundle)
        # WP8: the session/stop-token was killed mid-step — the plan did NOT
        # finish; the lifecycle ends cancelled.  F3/F4: idempotent per lease (the
        # session-end funnel may have raced us here); the reason is the reply's
        # own failure_reason ("cancelled"), the result token is the reply's.
        _audit_plan_terminal(
            bundle, lease, reply_result="stopped", reason="cancelled",
            failure_reason="cancelled",
        )
        return _plan_exception_response(
            bundle,
            lease,
            step_index=step_index,
            # DELTA 1 #5: the session/stop-token was killed mid-step — the plan was
            # NOT finished, so the reply says ``stopped`` (ok false by the terminal
            # rule), never a step verdict that could read as an honest completion.
            result="stopped",
            reason=str(exc),
            blocked=False,
            stopped=True,
            failure_reason="cancelled",
        )
    except LimitExceeded as exc:
        step_entries = list(step_context.get("partial_results") or [])
        plan_layer.merge_plan_step_prefix(
            lease,
            step_entries,
            source_offset=int(lease.context["points"][step_index]["first_item"]),
        )
        plan_layer.set_plan_step_remainder(lease, step_index, step_specs, step_entries)
        if plan_layer.plan_step_attempted(step_entries):
            lease.cursor = plan_layer.plan_cursor_after(lease.cursor, step_index, step_entries)
        # WP8: the limit cut parks the lease (the plan continues on the host's
        # ``continue``) — this step still gets its own plan_point event.
        _audit_plan_event(
            bundle,
            "plan_point",
            lease=lease,
            result="failed",
            step_index=step_index,
            step_number=step_index + 1,
            failure_reason="limit_exceeded",
            **_plan_counts(lease),
        )
        return _plan_exception_response(
            bundle,
            lease,
            step_index=step_index,
            result="failed",
            reason=f"{exc.limit_name}: {exc}",
            blocked=True,
            failure_reason="limit_exceeded",
            # F6: EVERY limit carries its name — error / limit / cutoff agree with
            # failure_reason whatever limit tripped, not only the max_* ones.
            limit_name=exc.limit_name,
        )
    except BaseException:
        # Structural/unexpected failures release the lease in this path; the outer
        # server teardown cannot accidentally leave a caller stuck behind a dead plan.
        plan_layer.merge_plan_step_prefix(
            lease,
            list(step_context.get("partial_results") or []),
            source_offset=int(lease.context["points"][step_index]["first_item"]),
        )
        lease.cursor = step_index + 1
        _plan_guard.release(session_id, bundle)
        # F3/F4: idempotent per lease; "exception" is the lifecycle cause no step
        # reply names (the outer handler answers with a plain error envelope).
        _audit_plan_terminal(
            bundle, lease, reply_result="failed", reason="exception",
        )
        raise
    step_entries = list(outcome.follow_up_results or step_context.get("partial_results") or [])
    plan_layer.merge_plan_step_prefix(
        lease,
        step_entries,
        source_offset=int(lease.context["points"][step_index]["first_item"]),
    )
    plan_layer.set_plan_step_remainder(lease, step_index, step_specs, step_entries)
    # K-53: the cursor advances iff >= 1 item of the step dispatched input; a
    # pre-dispatch refusal (rejected/approval/safety) keeps the cursor so
    # ``continue`` re-executes the same step.
    lease.cursor = plan_layer.plan_cursor_after(lease.cursor, step_index, step_entries)
    if outcome.kind == "error":
        # A backend/agent error is a structural failure, not a repairable step
        # verdict: release the lease in the guaranteed path so no caller is
        # stranded behind it.
        _plan_guard.release(session_id, bundle)
        # WP8: the lifecycle ends failed with the SAME reason the reply reports.
        _audit_plan_terminal(
            bundle, lease, reply_result="failed", reason="execution_error",
            failure_reason="execution_error",
        )
        return _plan_exception_response(
            bundle,
            lease,
            step_index=step_index,
            result="failed",
            reason=outcome.message or "action_error",
            blocked=False,
            failure_reason="execution_error",
        )
    # DELTA 1 #1 + DELTA 2 N3 (§1.3 "retry = replace"): the walk ends in THIS call
    # only when the LAST STEP ITSELF verified — and then the reply is the terminal
    # reply whose verdict comes from the whole-plan aggregate over the retained
    # prefix (an earlier uncertain/failed point still caps the verdict at
    # ``unknown``/``failed`` with ``ok:false`` — the #1 fix stays). A last step
    # that did NOT itself verify (uncertain / failed / rejected / partial) PARKS
    # the lease: the host may ``replace`` to retry or ``continue`` to receive the
    # terminal reply (built by the same aggregate over the whole prefix).
    session_killed = bool(getattr(bundle.state, "stopped", False)) or bool(
        getattr(getattr(bundle.context, "stop", None), "stopped", False)
    )
    if (
        lease.cursor >= len(lease.steps)
        and not session_killed
        and outcome.kind == "executed"
        and _plan_step_result(step_entries) == "verified"
    ):
        # N1 (VAL-WP7b-02): the terminal reply is the STEP reply (image registered
        # and attached, per-item disclosure kept, the step's events retained on the
        # lease and in the aggregate) with the verdict overridden by the whole-plan
        # aggregate — the completing step loses nothing.
        return _plan_response(
            bundle,
            lease,
            outcome,
            step_index=step_index,
            step_entries=step_entries,
            include_screenshot_after=include_screenshot_after,
            image_mode=image_mode,
            view_provider=view_provider,
            complete_walk=True,
        )
    return _plan_response(
        bundle,
        lease,
        outcome,
        step_index=step_index,
        step_entries=step_entries,
        include_screenshot_after=include_screenshot_after,
        image_mode=image_mode,
        view_provider=view_provider,
    )


async def _handle_plan_action(
    bundle: Any,
    lease: PlanLease,
    *,
    plan_action: str | None,
    plan: Any,
    include_screenshot_after: bool | None,
    image_mode: str,
    view_provider: Any | None,
) -> Any:
    """Validate and service the additive continue/stop/replace handshake."""
    session_id = str(bundle.context.session_id)
    if not isinstance(plan_action, str) or plan_action not in {"continue", "stop", "replace"}:
        return {
            "ok": False,
            "result": "rejected",
            "reasons": ["invalid_plan_action"],
            "error": "invalid_plan_action",
            "blocked": True,
            "plan_id": lease.context["plan_id"],
            "message": "plan_action must be continue, stop, or replace while a plan lease is active.",
            "executed_prefix": [_redact_queue_entry(entry) for entry in lease.prefix],
            "remaining": plan_layer.plan_remaining(lease, "awaiting_plan_action"),
            PLAN_EVIDENCE_KEY: _plan_evidence(bundle, lease),
            "next_step": plan_layer.plan_next_step(lease),
        }
    if plan_action == "stop":
        _plan_guard.release(session_id, bundle)
        return _plan_terminal_response(bundle, lease, result="stopped", reason="stop")
    if plan_action == "replace":
        if plan is None:
            return {
                "ok": False,
                "result": "rejected",
                "reasons": ["invalid_plan_action"],
                "error": "invalid_plan_action",
                "blocked": True,
                "plan_id": lease.context["plan_id"],
                "message": "plan_action='replace' requires a complete replacement plan.",
                "executed_prefix": [_redact_queue_entry(entry) for entry in lease.prefix],
                "remaining": plan_layer.plan_remaining(lease, "awaiting_plan_action"),
                PLAN_EVIDENCE_KEY: _plan_evidence(bundle, lease),
                "next_step": plan_layer.plan_next_step(lease),
            }
        try:
            replacement = plan_layer.coerce_tolerant_plan(plan)
            replacement_specs, replacement_context = plan_layer.normalize_plan(replacement)
            for spec in replacement_specs:
                spec.to_grounded(reason_prefix="MCP plan action")
        except Exception as exc:  # noqa: BLE001 - malformed replacement has no effect
            return _teaching_invalid_action(exc, "done")
        old_id = lease.context["plan_id"]
        old_prefix = [_redact_queue_entry(entry) for entry in lease.prefix]
        _plan_guard.release(session_id, bundle)
        # WP8: the replaced plan's lifecycle ends as ``stopped`` (lifecycle reason
        # "replaced") BEFORE the replacement starts its own lifecycle.
        _audit_plan_terminal(bundle, lease, reply_result="stopped", reason="replaced")
        # O-03: approval was decided on the FIRST plan call; a replace INHERITS it.
        # The replace call's own ``approved`` parameter is deliberately NOT read —
        # an unapproved plan cannot be escalated into an approved one by replacing.
        new_lease = PlanLease(
            model=replacement,
            specs=replacement_specs,
            context=replacement_context,
            steps=plan_layer.plan_step_slices(replacement_specs, replacement_context),
            approved=lease.approved,
            replaced_plan_id=old_id,
            replaced_prefix=old_prefix,
        )
        _plan_guard.install(bundle, new_lease)
        # WP8: the replacement starts its own lifecycle (with the replace
        # disclosure, matching the reply the step call below returns).
        _audit_plan_event(
            bundle,
            "plan_start",
            lease=new_lease,
            result="started",
            point_count=replacement_context["point_count"],
            action_types=[spec.action.value for spec in replacement_specs],
            on_failure=replacement.on_failure,
            on_uncertain=replacement.on_uncertain,
            transition_duration_ms=replacement_specs[0].duration_ms,
            final_wait_ms=replacement_context["final_wait_ms"],
            replaced_plan_id=old_id,
        )
        response = await _execute_plan_step(
            bundle,
            new_lease,
            include_screenshot_after=include_screenshot_after,
            image_mode=image_mode,
            view_provider=view_provider,
        )
        # F11 (VAL-WP8-01): the replace disclosure rides ONLY the replace call's
        # own reply. The dict arm covers exception-shaped replies (the payload
        # builder attached the keys via the lease); afterwards BOTH leases'
        # disclosure fields are cleared so no later step of the replacement (or
        # of any further replace) re-carries the old plan's prefix.
        if isinstance(response, dict):
            response["replaced_plan_id"] = old_id
            response["replaced_prefix"] = old_prefix
        new_lease.replaced_plan_id = None
        new_lease.replaced_prefix = None
        return response
    return await _execute_plan_step(
        bundle,
        lease,
        include_screenshot_after=include_screenshot_after,
        image_mode=image_mode,
        view_provider=view_provider,
    )


@mcp.tool()
@_session_scoped
async def computer_execute(
    session_id: str,
    action: str,
    x: NullableCoordinate = None,
    y: NullableCoordinate = None,
    text: NullableStr = None,
    keys: NullableKeys = None,
    delta: int = 0,
    approved: bool = False,
    expected_effect: NullableStr = None,
    x2: NullableCoordinate = None,
    y2: NullableCoordinate = None,
    target: NullableStr = None,
    via: NullableStr = None,
    include_screenshot_after: NullableBool = None,
    follow_ups: NullableFollowUps = None,
    visual_view: NullableStr = None,
    plan: NullablePlan = None,
    plan_action: NullableStr = None,
) -> Any:
    """Validate and execute one grounded action; approval applies only to this action call.

    session_id is REQUIRED on every call: copy it from start_session's result and
    reuse it for the whole session.

    ACTION VOCABULARY (exact names, from the models enum): click, double_click,
    right_click, drag, type, keypress, scroll, wait, done, move, hotkey, focus_window,
    ensure_app. There is
    NO "key" action (single keys use "keypress": {"action": "keypress", "keys": ["ctrl"]})
    and NO "triple_click" (use "double_click" or repeated clicks). "keys" must be KEY
    NAMES ("ctrl", "a", "enter", "esc") — never text or sentences; to type text use
    {"action": "type", "text": "..."}. "ensure_app" takes target="process" or
    "process|doc-token" (e.g. "excel|book1") and attaches to an EXISTING instance
    (REATTACHED / AMBIGUOUS_INSTANCE / NO_INSTANCE payloads; with the default
    launch="server" policy a NO_INSTANCE for an allowlisted target launches the
    process server-side — launch="driver" or CORTEX_ATTACH_OR_LAUNCH=driver keeps
    the never-launch behavior).

    The hardcoded ``confidence=1.0`` is the client-asserted MODEL confidence; grounding,
    staleness, risk, approval, and verification run independently. ``expected_effect``
    opts into semantic verification (a stated effect must be observed or the result
    reports verification failed/uncertain — never silently successful). For
    ``action="drag"``, ``x``/``y`` are the drag start and the trailing ``x2``/``y2`` the
    drag end (both required, screenshot coordinates). ``action="move"`` requires
    ``x``/``y``; ``action="hotkey"`` requires ``keys`` (2-12 key names);
    ``action="focus_window"`` requires ``target`` (a window title).

    Type transport selector (v0.7.1, type actions ONLY): ``via`` chooses HOW text is
    delivered. ``via="sendinput"`` (default; also ``via=null``) is per-character
    ``KEYEVENTF_UNICODE`` injection. ``via="clipboard"`` saves the current clipboard,
    sets ``CF_UNICODETEXT`` with the typed text, dispatches the ctrl+v chord through the
    existing hotkey path, then best-effort restores the previous clipboard content
    (restore failure is typed-annotated, never fails the action). Use the clipboard
    transport for OpenGL/console apps that receive chords but drop per-character
    injection (field case: Blender's Python Console). GATE PARITY: the transport never
    bypasses anything — the SAME classification, redaction, and approval decision is
    computed for both transports from the identical text; they differ only after
    approval. When the target is unreadable (no accessibility tree), a blind type
    reports ``TYPE_UNCONFIRMED no-readable-target`` and recommends
    ``via="clipboard"`` plus visual verification with a stated ``expected_effect``.
    ``via`` on any non-type action, or an unknown value, is a typed ``invalid_action``
    rejection (fail closed).

    Held input duration (WP2, ORVEX-2026-107): ``hold_ms`` is an OPTIONAL integer,
    0..5000 MILLISECONDS, carried on an INDIVIDUAL ACTION — a ``follow_ups`` entry —
    and legal on ``click``,
    ``right_click``, ``keypress`` and ``hotkey`` ONLY. Every other action
    (``double_click``, ``move``, ``scroll``, ``type``, ...) is rejected fail-closed
    with the typed ``invalid_action`` whose message reads "hold_ms is only valid on
    click/right_click/keypress/hotkey actions". On a key action it presses the key,
    keeps it down for the duration, then releases it; on ``hotkey`` it holds EVERY
    named key down SIMULTANEOUSLY for the duration and releases them in REVERSE
    order — a ``shift`` a member requires is synthesized and released right after
    every key that required it, never before one, so a chord like ``["ctrl","A"]``
    releases shift before ctrl. It is NOT "chord, then wait" — that would hold
    nothing, because a chord has already released every key by the time it returns.
    ``hold_ms=0`` is an explicit zero-duration press/release through that SAME path,
    not a fast-path chord; OMITTING it (or ``null``) keeps the ordinary immediate
    dispatch unchanged — for a key action, the existing atomic chord. It is a
    duration on ONE input and never a motion path: ``duration_ms`` is a SEPARATE,
    ``move``-only field, and the two must not be conflated.

    REM-F weak-model tolerance (boundary only): integer coordinates also accept
    integral floats (1343.0) and numeric strings ("1343"); NON-INTEGRAL values ROUND
    to the nearest int (a vision model aiming at pixel 1343.7 must not hard-fail).
    The D5-widened advertised schema admits integer | string | null; non-numeric
    garbage ("left") still fails with a clean typed error. ``follow_ups`` also
    accepts a JSON-encoded string containing the list, a single dict (wrapped into a
    one-entry list), and per-entry JSON strings — same fail-closed queue semantics
    afterwards.

    Host-payload opt-out (PERF-004, trailing optional): pass
    ``include_screenshot_after=false`` to OMIT the heavy image block
    (and any image bytes) from this response — recommended for actions whose outcome
    you check via the verification verdict + digest instead of the image (the full
    image stays available via computer_observe). DEFAULT action responses carry a
    HALF-SIZE JPEG (0.5x scale, ~60KB typical at 1080p) to keep your context light;
    ask for ``include_screenshot_after=true`` when FULL resolution is required (the
    full image stays available via computer_observe/computer_screenshot either way),
    or set ``CORTEX_ACTION_IMAGE_FULL=1`` to restore full-res defaults. Result image
    budget (REM-A H7): every outbound image is kept under ``CORTEX_RESULT_IMAGE_MAX_KB``
    (default 180 KB; oversized PNG re-encodes as JPEG outbound only — internal
    captures stay PNG). Executed responses return MCP content blocks (TextContent
    result JSON + ImageContent post-action screenshot, parity with computer_observe);
    error/rejection/approval shapes stay plain dicts. In a text-mode session
    (``start_session(image_delivery="text")``) the executed response is the slim dict
    (no image block, no screenshot bytes, mode keys added) and
    ``include_screenshot_after`` cannot re-enable images in text mode.

    Queued actions (PERF-004, trailing optional): ``follow_ups`` is a list of at most 5
    action specs (same fields as this tool's action parameters, e.g.
    {"action": "click", "x": 10, "y": 20, "expected_effect": "..."}), PLUS the two
    duration fields that cannot be passed as tool parameters: ``hold_ms`` (held
    input, click/right_click/keypress/hotkey only) and ``duration_ms`` (timed
    ``move`` motion) — so a queued item behaves EXACTLY like the equivalent flat
    action. Each follow-up
    passes the FULL independent pipeline (validate -> safety -> approval semantics ->
    execute -> verify) exactly like a single action — zero bypass. The queue CONTINUES
    while the attached window stays the same (ordinary pixel changes from
    earlier items — drawing, typing, dialogs — do NOT stop it; each item re-grounds
    from the fresh post-action capture). It stops only on TRUE staleness — the
    attached window's identity (hwnd/title/bounds) changed or closed since the queued
    premise — plus safety rejection, approval requirement, validator/grounding
    rejection, dispatch error, or a named interference event. Practical pattern:
    commit your in-progress UI state BEFORE switching ribbon tools — e.g. while a
    Paint shape is in edit mode the ribbon swallows clicks (exactly 0 pixel change),
    so click once on the canvas to commit the shape, then use the next ribbon tool.
    ``CORTEX_QUEUE_STRICT_DIGEST=1`` restores the legacy whole-screen stop.
    An UNCERTAIN verification does NOT stop the queue, and neither does a "failed"
    verification of an EXECUTED item — the keystroke/click physically dispatched, so
    the honest failed verdict (ok=false) rides that item's entry in follow_up_results
    while the rest of the batch still runs. Batch small related groups and end the
    batch with an observation.
    Per-item results arrive in the additive ``follow_up_results`` field (bounded, no
    per-item screenshots) with ``follow_ups_stopped_reason`` (None = the batch ran to
    completion).
    An item that is ActionSpec-valid but GroundedAction-invalid (a target-less
    ``focus_window``, a half-specified ``drag`` with one endpoint missing, a 1-key
    ``hotkey``, a point-less ``move``, a target-less ``ensure_app``) is rejected at
    the boundary with the typed ``invalid_action`` error and teach-in-text hints
    listing the valid shapes — nothing dispatches and the queue never starts (R-18;
    never a raw pydantic ValidationError).

    Visual view (AL-002, trailing optional): ``visual_view`` selects the representation
    of the RETURNED post-action image — "raw" (untouched stock bytes + additive
    ``visual_view``/``visual_view_scale`` keys) or "grid" (a derived coordinate-grid
    annotation; derived ONCE per response, then the UNCHANGED stock half-res/ladder
    bounding). Absent request = byte-identical default behavior; unknown values are
    rejected fail-closed (``invalid_visual_view``) BEFORE the action runs — a bad view
    name never dispatches input. If the view cannot be derived from the post-action
    payload, the executed result is still returned honestly (an executed action's
    outcome is never hidden) but with NO image block and an additive
    ``visual_view_error: "view_derive_failed: ..."`` marker — never a silent raw
    substitution. In text mode (no image is served either way) the keys do not apply.

    Waypoint plan (ORVEX-2026-101 Batch 2, trailing optional ``plan``): a plan
    declares a sequence of POINTS + ACTIONS and Cortex executes it STEPWISE — you
    declare LOCATIONS AND ACTIONS ONLY; the mouse motion BETWEEN points is an
    internal detail (deterministic defaults; optionally overridable per point with
    ``duration_ms``; there are NO path/bend/easing parameters). Shape:
    {"points": [{"id": "open-menu", "x": 420, "y": 180, "actions": [{"action": "click"}],
                 "arrival_condition": "menu is open"},
                {"id": "save", "x": 850, "y": 720, "actions": [{"action": "click"}]}]}.
    Every point normalizes internally to a ``move`` transition + its actions, and
    EVERY item rides the FULL independent pipeline (ground -> validate -> safety ->
    approval -> execute -> verify) exactly like a single action — the "wait and
    re-observe" discipline is inherited per item, never bypassed. Point actions
    carry NO x/y (the point IS the location); x2/y2 belong to a queued ``drag``
    only. ``arrival_condition`` rides as ``expected_effect`` for the point's
    actions that lack an explicit one. A plan and the flat action/follow_ups
    surface are mutually exclusive (pass EITHER; a plan call leaves every flat
    action parameter at its default). An invalid or INCOMPLETE plan is a typed
    ``invalid_action`` rejection BEFORE any capture or motion. At most 10 points
    and 5 actions per point.

    MOTION AND TIMING — a plan is the EXPLICIT-TIMING mode (ORVEX-2026-102 §5.2).
    A bare action is the INSTANT mode: outside a plan the move duration and the
    settle are both 0 ms, so a single action is dispatched as fast as the machine
    allows and NOTHING is hidden. A SUBMITTED plan must therefore STATE all of the
    following, or it is rejected BEFORE any capture or motion, naming what is
    missing: ``points`` in order and ``actions`` at each point (an explicit empty
    ``"actions": []`` declares a crossing point); the movement timing
    (``transition_duration_ms`` on the plan, or ``duration_ms`` on every point);
    an explicit ``delta`` on every ``wait`` action inside a point (0..10 s;
    ``final_wait_ms`` never covers it); the settle wanted (``settle_ms``, 0 = no
    settle, 1..200); what is verified after each step (``arrival_condition``, or an
    action-level ``expected_effect``); the per-step cooperative timeout
    (``point_timeout_ms``, or ``timeout_ms`` per point); and BOTH ``on_failure``
    and ``on_uncertain`` ("stop"/"continue"). Optional plan-level keys:
    ``final_wait_ms`` (one trailing wait, capped 5000 ms) and inherited
    ``settle_ms``/policies/timeout/duration.

    Stepwise handshake (ORVEX-2026-101 decision 1, trailing optional
    ``plan_action``): the FIRST plan call executes ONE waypoint step, then returns
    ``blocked=true`` with an explicit JSON result/evidence/prefix/next_step/
    remaining block, and a plan LEASE stays active on the session. While the lease
    is active, flat actions, follow_ups, observation tools
    (computer_observe/zoom/screenshot), computer_wait and computer_cancel_wait are
    refused with the typed ``plan_lease`` error before any capture or input —
    computer_wait_status and computer_fetch_image stay allowed (read-only). A
    misdirected call costs nothing. ``plan_action="continue"`` runs the NEXT step
    under the SAME approval verdict (approval is decided once, on the FIRST plan
    call, and inherited by every step and by ``replace`` — ``continue`` never
    re-asks); ``plan_action="stop"`` releases the lease and returns the executed
    prefix with ``result:"stopped"``; ``plan_action="replace"`` validates a
    complete replacement plan (required), closes the old lease, and opens a new
    one inheriting the FIRST call's approval. A plan whose steps abstain reports
    ``result:"uncertain"`` per step and TERMINATES with ``result:"unknown"`` —
    ``ok`` is true ONLY when EVERY step verified (``ok = (result ==
    "verified")``); ``unknown`` and ``stopped`` terminal verdicts are never
    success.

    Plan replies (unified contract §1.3): every step and terminal reply carries
    ``result`` (step: verified/uncertain/failed/rejected/not_executed; terminal:
    verified/unknown/failed/stopped), ``failure_reason`` (ALWAYS; ``null`` when
    nothing failed; ONE token of the typed vocabulary — the full B6-B8
    classification tables land with the WP8 decomposition), ``not_executed`` (the
    bounded, explicit list of plan work that never ran, each entry carrying the
    reason), and ``evidence_by_backend`` (a DICT: ``captures`` from the session's
    evidence store — real on BOTH backends — plus ``repositions`` the plan's moves
    recorded). The bare ``evidence`` key stays a LIST of evidence lines everywhere
    else; the two are never mixed. The aggregate ``plan`` block reports per-point
    status and per-action rows (including each action's ``hold_ms``).

    The lease is released exactly once — on the terminal reply, a host
    ``stop``/``replace``, a structural step error or exception, or the session
    ending (stop_session, the idle timeout, an internal kill). An idle lease with
    no call in flight does NOT keep the session alive: after the idle timeout the
    session is reaped and the lease released with it.
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        # §1.1/§1.4 (ORVEX-2026-108), execute surface ONLY: a session-lifecycle
        # refusal of an ACTION call is a rejection — {ok False, result "rejected",
        # message, reasons}, the typed code named as the first reason, no ``error``
        # key. The read-only/lifecycle tools keep the typed ``_error_response``
        # shapes (§1.3).
        code = "session_stopped" if isinstance(exc, _StoppedSession) else "unknown_session"
        return {
            "ok": False,
            "result": "rejected",
            "message": (
                _stopped_session_message(str(exc.args[0]) if exc.args else "")
                if isinstance(exc, _StoppedSession)
                else str(exc)
            ),
            "reasons": [code],
            "session_id": str(exc.args[0]) if exc.args else "",
        }
    # D1: text mode dominates every executed response shape — include_screenshot_after
    # can only REMOVE bytes, never re-enable an image (a non-vision model passing
    # True must not be able to kill its own session).
    image_mode = _session_image_delivery(bundle)
    # ORVEX-2026-101 decision 1 (WP7b): the lease gate is the FIRST operation after
    # session lookup. A flat/follow-up call, a second plan, or a plan payload
    # without an explicit continuation decision is refused before visual-view
    # resolution, validation, capture, or backend dispatch: zero-effect handshake
    # rejection. A plan lease is released only by continue/stop/replace, the
    # terminal paths inside the step machinery, or session teardown — never leaked.
    active_lease = _plan_guard.active_lease(bundle)
    if active_lease is not None:
        if not (
            action == "done"
            and x is None
            and y is None
            and text is None
            and not keys
            and delta == 0
            and expected_effect is None
            and x2 is None
            and y2 is None
            and target is None
            and via is None
            and follow_ups is None
        ):
            return _plan_lease_refusal(
                bundle,
                active_lease,
                "A stepwise plan lease is active on this session; flat actions and follow-ups are refused with zero effect.",
            )
        if plan is not None and plan_action != "replace":
            return _plan_lease_refusal(
                bundle,
                active_lease,
                "A replacement plan requires plan_action='replace'; the active lease was not touched.",
            )
        if plan is None and plan_action is None:
            # DELTA 1 #13: a bare call (defaults, no plan_action) mid-lease is a
            # plan_lease refusal naming the handshake verb it must send — NOT the
            # invalid_plan_action reply, which is reserved for a GIVEN-but-invalid
            # plan_action.
            return _plan_lease_refusal(
                bundle,
                active_lease,
                "A stepwise plan lease is active on this session; send plan_action='continue', 'stop' or 'replace' (with a replacement plan only for 'replace').",
            )
        return await _handle_plan_action(
            bundle,
            active_lease,
            plan_action=plan_action,
            plan=plan,
            include_screenshot_after=include_screenshot_after,
            image_mode=image_mode,
            view_provider=None,
        )
    if plan_action is not None:
        return {
            "ok": False,
            "result": "rejected",
            "reasons": ["invalid_plan_action"],
            "error": "invalid_plan_action",
            "message": "plan_action is only valid while a stepwise plan lease is active.",
        }
    # AL-002: resolve an explicit view request BEFORE anything runs (fail-closed — a
    # bad view name never dispatches input); the absent request adds only this
    # None-check (H19).
    try:
        view_provider = _resolve_view_param(visual_view)
    except InvalidVisualViewError as exc:
        # §1.1/§1.4, execute surface ONLY: the malformed-parameter refusal of an
        # ACTION call is a rejection (the typed code names itself as the first
        # reason); observe/zoom keep the typed ``invalid_visual_view`` error (§1.3
        # read-only refusals).
        return {
            "ok": False,
            "result": "rejected",
            "message": str(exc),
            "reasons": [f"invalid_visual_view: {exc}"],
        }
    # ORVEX-2026-101 decision 1 (WP7b): a new plan is fully validated, normalized,
    # and pre-converted BEFORE the lease is installed — a malformed or incomplete
    # plan is a typed teaching rejection with ZERO effect (no capture, no motion,
    # no lease). Only then does the first point step execute. There is NO legacy
    # single-call plan path (S-01): a plan is always stepwise.
    if plan is not None:
        if (
            action != "done"
            or x is not None
            or y is not None
            or text is not None
            or keys
            or delta != 0
            or expected_effect is not None
            or x2 is not None
            or y2 is not None
            or target is not None
            or via is not None
            or follow_ups is not None
        ):
            return _teaching_invalid_action(
                ValueError(
                    "pass EITHER a plan (waypoints+actions) OR a flat action/follow_ups "
                    "call — not both; a plan call leaves every flat action parameter at "
                    "its default (action=\"done\", no x/y/text/keys/...)."
                ),
                action,
            )
        try:
            plan_model = plan_layer.coerce_tolerant_plan(plan)
            plan_specs, plan_context = plan_layer.normalize_plan(plan_model)
            for plan_spec in plan_specs:
                # R-18: every plan item must CONVERT to a GroundedAction before the
                # first motion — the plan never starts on an unconvertible item.
                plan_spec.to_grounded(reason_prefix="MCP plan action")
        except Exception as exc:  # noqa: BLE001 - malformed plan: fail closed, teach
            return _teaching_invalid_action(exc, action)
        lease = PlanLease(
            model=plan_model,
            specs=plan_specs,
            context=plan_context,
            steps=plan_layer.plan_step_slices(plan_specs, plan_context),
            # O-03: require_approval is evaluated ONCE, on this FIRST plan call,
            # for the whole plan — every step inherits this verdict and ``replace``
            # inherits it too; ``continue`` never re-asks.
            approved=bool(approved),
        )
        try:
            _plan_guard.install(bundle, lease)
        except ValueError as exc:
            # F15 (VAL-WP8-01): a plan-lease refusal is a REJECTED reply like the
            # other plan_lease refusals — the closed result token, ok False, and
            # the typed code as the first reason (assert_plan_refusal).
            return {
                "ok": False,
                "result": "rejected",
                "reasons": ["plan_lease"],
                "error": "plan_lease",
                "blocked": True,
                "message": str(exc),
            }
        # WP8: the plan lifecycle is audited from the FIRST call (E-05: the reply
        # this call returns is the step reply built below; the start event records
        # the declaration, the step reply's own plan_point records the verdict).
        _audit_plan_event(
            bundle,
            "plan_start",
            lease=lease,
            result="started",
            point_count=plan_context["point_count"],
            action_types=[spec.action.value for spec in plan_specs],
            on_failure=plan_model.on_failure,
            on_uncertain=plan_model.on_uncertain,
            transition_duration_ms=plan_specs[0].duration_ms,
            final_wait_ms=plan_context["final_wait_ms"],
        )
        return await _execute_plan_step(
            bundle,
            lease,
            include_screenshot_after=include_screenshot_after,
            image_mode=image_mode,
            view_provider=view_provider,
        )
    if follow_ups is not None:
        # REM-F: normalize the tolerant shapes for DIRECT callers too (the boundary
        # coercion already produced list[dict] for MCP callers — idempotent here:
        # a list[dict] passes through unchanged; JSON strings parse; a dict wraps).
        try:
            follow_ups = _coerce_tolerant_follow_ups(follow_ups)
        except ValueError as exc:
            return _teaching_invalid_action(exc, action)
        assert isinstance(follow_ups, list)
        if any(not isinstance(item, dict) for item in follow_ups):
            return _teaching_invalid_action(
                ValueError("follow_ups must be a list of action-spec objects."), action
            )
        if len(follow_ups) > MAX_FOLLOW_UPS:
            return _teaching_invalid_action(
                ValueError(
                    f"follow_ups supports at most {MAX_FOLLOW_UPS} entries "
                    f"(got {len(follow_ups)}); batch smaller groups."
                ),
                action,
            )
    try:
        grounded = GroundedAction(
            action=action,  # type: ignore[arg-type]
            point=None if x is None or y is None else {"x": x, "y": y},
            to_point=None if x2 is None or y2 is None else {"x": x2, "y": y2},
            text=text,
            keys=keys or [],
            delta=delta,
            reason="Explicit MCP action",
            confidence=1.0,
            expected_effect=expected_effect,
            target=target,
            via=via,
        )
    except Exception as exc:  # noqa: BLE001 - structured error, no traceback
        return _teaching_invalid_action(exc, action)
    specs: list[ActionSpec] = []
    if follow_ups:
        for item in follow_ups:
            try:
                # REM-F: tolerate the same weak-model coordinate shapes inside
                # entries; every strict ActionSpec bound still applies afterwards.
                spec = ActionSpec.model_validate(_coerce_follow_up_entry(item))
            except Exception as exc:  # noqa: BLE001 - malformed queue item: fail closed
                return _teaching_invalid_action(exc, str(item.get("action", "")))
            try:
                # R-18: an item can be ActionSpec-valid yet GroundedAction-invalid
                # (target-less focus_window, half-specified drag, 1-key hotkey).
                # Validate the conversion HERE so the host gets the typed
                # ``invalid_action`` rejection with teach-in-text hints instead of an
                # uncaught pydantic ValidationError from inside the queue; the queue
                # never starts (fail-closed, zero items dispatch — unchanged).
                spec.to_grounded()
            except Exception as exc:  # noqa: BLE001 - grounded-shape failure: teach
                return _teaching_invalid_action(exc, spec.action.value)
            specs.append(spec)
    try:
        outcome = await bundle.agent.run_single(
            bundle.state,
            grounded,
            approved=approved,
            expected_effect=expected_effect,
            follow_ups=specs or None,
        )
    except TaskStopped as exc:
        # §1.2: a host-requested stop is the ``stopped`` token — nothing verified,
        # ok=False by the shared ok-rule.
        stopped_token = "stopped"
        return {
            "ok": ok_from_result(stopped_token),
            "result": stopped_token,
            "stopped": True,
            "message": str(exc),
        }
    except LimitExceeded as exc:
        # §1.1/§1.4: a limit trip is a GATE REFUSAL — verified pre-dispatch (the
        # observation-phase rate ceiling and ``check_action`` both fire BEFORE the
        # triggering action dispatches any input), so this is a rejection: the typed
        # limit named as the first reason, the additive ``limit`` key kept, no
        # ``error`` key.
        return {
            "ok": False,
            "result": "rejected",
            "message": str(exc),
            "reasons": [f"limit_exceeded: {exc.limit_name}"],
            "limit": exc.limit_name,
        }
    # §1 (ORVEX-2026-108 unified reply contract): every reply carries a ``result``
    # token from the closed §1.2 set, and ``ok`` is derived from that token through
    # the ONE shared ok-rule (ok is True iff result ∈ {verified, marker}) — never a
    # per-shape rule.
    if outcome.kind == "rejected":
        response: dict[str, object] = {
            "ok": False,
            "result": "rejected",
            # T8: rejections carry their SPECIFIC message (e.g. "Focus interference: the
            # OS-focused window is not the session target.") instead of the generic
            # grounding text; the structured event payloads ride in ``reasons``.
            "message": outcome.message or "Action rejected (no gate detail available).",
            "reasons": outcome.reasons,
        }
        if outcome.partial_delivery is not None:
            # §1.1 K-05: mid-type focus drift — the reply names what already landed.
            response["partial_delivery"] = dict(outcome.partial_delivery)
    elif outcome.kind == "safety_denied":
        # §1.2: the safety gate refused — a rejection; the reasons come from the SAME
        # shared helper as every queue entry (typed code names itself first).
        response = {
            "ok": False,
            "result": "rejected",
            "message": outcome.message,
            "reasons": rejection_reasons(outcome),
        }
    elif outcome.kind == "approval_required":
        # §1.1 approval rule: ok=False, result="not_executed", requires_approval=True.
        response = {
            "ok": False,
            "result": "not_executed",
            "requires_approval": True,
            "message": outcome.message,
        }
    elif outcome.kind == "digest_surprise":
        # §1.2: the premise-validity gate refused before dispatch — a rejection; the
        # reasons come from the SAME shared helper as every queue entry (the ``error``
        # key is gone: rejected replies carry reasons, never an error key).
        response = {
            "ok": False,
            "result": "rejected",
            "message": outcome.message,
            "reasons": rejection_reasons(outcome),
        }
    elif outcome.kind == "error":
        # §1.2: an operation error is ``failed`` (ok=False); the typed code stays.
        response = {
            "ok": False,
            "result": "failed",
            "error": "action_error",
            "message": outcome.message,
        }
    else:
        result = outcome.result
        if result is None:  # defensive: executed outcomes always carry a result
            return {
                "ok": False,
                "result": "failed",
                "error": "action_error",
                "message": "Execution produced no result.",
            }
        payload = result.model_dump()
        # F2: the response path is redacted too (defense in depth).
        payload = _redact_result_payload(payload)
        payload["result"] = execute_result_token(outcome)
        payload["ok"] = ok_from_result(payload["result"])
        payload.update(
            {
                "model_confidence": outcome.model_confidence,
                "grounding_confidence": outcome.grounding_confidence,
                "verification_confidence": outcome.verification_confidence,
            }
        )
        response = payload
    # v07-007 R-21 (Commander decision D1): the non-executed response shapes embed
    # model-proposed text in ``message`` (e.g. approval messages carry _clip(action.text)),
    # so they get the SAME redaction the executed shape already applies — additive
    # redaction only, no shape/field changes.
    if outcome.kind in {"rejected", "safety_denied", "approval_required", "digest_surprise", "error"}:
        response = _redact_result_payload(response)
    # PERF-004 C4: host-payload opt-out (additive, off by default) — omit the heavy
    # image from the response entirely when the caller asked for it.
    if include_screenshot_after is False:
        # MINOR-c (§1.1 one key, one presence rule): an opt-out executed reply serves
        # no image, so the honest scale report is None (same as text mode).
        if response.pop("screenshot_after_base64", None) is not None:
            response[IMAGE_OUTBOUND_SCALE_KEY] = None  # also covers any nested path (H5)
    # PERF-004 C7: additive queue bookkeeping on every response shape.
    if outcome.follow_up_results is not None:
        response["follow_up_results"] = [
            _redact_queue_entry(entry) for entry in outcome.follow_up_results
        ]
        response["follow_ups_stopped_reason"] = outcome.follow_ups_stopped_reason
    # T8: additive Interference Guard events on every response shape (structured event
    # payloads the driver parses per DRIVER-PROTOCOL.md).
    if outcome.interference_events:
        response["interference_events"] = list(outcome.interference_events)
    # REM-A H3 (master-mission Phase 2): EXECUTED responses return MCP content blocks
    # in parity with computer_observe — a slim TextContent (result JSON without the
    # image blob; queue entries never carry image bytes) plus one ImageContent with
    # the post-action screenshot under the CORTEX_RESULT_IMAGE_MAX_KB budget (H7).
    # Error/rejection/approval shapes have no image key and stay plain dicts.
    # include_screenshot_after=False was applied above, so the opt-out never enters
    # this branch (H5: no ImageContent, and none nested in follow_up_results).
    # D1 text mode: the slim dict shape (the opt-out precedent, mode keys added) —
    # include_screenshot_after can never promote text mode back to image mode.
    if outcome.kind == "executed" and "screenshot_after_base64" in response:
        if image_mode == "text":
            response.pop("screenshot_after_base64", None)
            response["image_delivery"] = "text"
            # MINOR-c: no image served, no scale applied — honest None.
            response[IMAGE_OUTBOUND_SCALE_KEY] = None
            response["image_delivery_note"] = IMAGE_DELIVERY_TEXT_NOTE
            return response
        # R-5 (W4): the capture-time frame rides along (never serialized; absent for
        # legacy results) so the outbound JPEG ladder skips re-decoding the PNG.
        # an EXPLICIT include_screenshot_after=true is the documented
        # full-image opt-in and ships FULL resolution; the DEFAULT (omitted) ships
        # the half-res JPEG; CORTEX_ACTION_IMAGE_FULL=1 restores full-res defaults.
        frame = getattr(result, "_frame", None) if result is not None else None
        full_resolution = include_screenshot_after is True or _action_image_full()
        # AL-002 (additive): honor an explicit visual_view on the RETURNED image —
        # derived ONCE per response, then the UNCHANGED stock bounding branch. Raw is
        # the pass-through (stock bytes + additive keys). If the view cannot be
        # derived, the executed result is still returned honestly (an executed
        # action's outcome is never hidden behind an error dict) with NO image block
        # and the typed ``view_derive_failed`` marker — never a silent raw substitute.
        view_id: str | None = None
        view_payload: tuple[str, Any] | None = None
        if view_provider is not None:
            view_id = view_provider.id
            if not view_provider.passthrough:
                try:
                    derived = view_provider.derive(
                        observation_for_payload(str(response["screenshot_after_base64"]), frame)
                    )
                    started = time.perf_counter()
                    encoded_b64 = encode_png_b64(derived.image)
                    bundle.metrics.record_latency(
                        "view_derive_ms", (time.perf_counter() - started) * 1000.0
                    )
                    view_payload = (encoded_b64, derived.image)
                except VisualViewError as exc:  # fail-closed: no image, no raw fallback
                    response.pop("screenshot_after_base64", None)
                    response["visual_view"] = view_id
                    response["visual_view_error"] = f"view_derive_failed: {exc}"
                    return response
                except Exception as exc:  # noqa: BLE001 - fail-closed, no traceback
                    response.pop("screenshot_after_base64", None)
                    response["visual_view"] = view_id
                    response["visual_view_error"] = (
                        f"view_derive_failed: {type(exc).__name__}: {exc}"
                    )
                    return response
        # WP5: THIS reply is about to carry the post-action image, so the frame is
        # retained HERE — after every early return that ends in a reply WITHOUT the
        # image (rejection, approval, opt-out pop, text mode, view-derive failure) —
        # and the reply gains the additive image_id keys.  The bytes retained are
        # EXACTLY the bytes the reply carries (VAL-WP5 F1): the DERIVED view payload
        # when an explicit non-raw view was served, otherwise the raw post-action
        # capture — both pre-ladder, so a later fetch re-runs the ladder honestly.
        # ``register_frame`` reads the store key and never creates it, so a teardown
        # that already ran writes nothing instead of resurrecting a store.
        carried_b64 = (
            str(view_payload[0]) if view_payload is not None else str(response["screenshot_after_base64"])
        )
        response.update(
            register_frame(
                bundle.extra,
                payload=carried_b64,
                source="computer_execute",
                attached=True,
                visual_view=view_id,
            )
        )
        return _execute_response_blocks(
            response,
            frame=frame,
            full_resolution=full_resolution,
            view_id=view_id,
            view_payload=view_payload,
        )
    return response


# --- WP5: the evidence store's server seam ---------------------------------------------------
# Capture hooks + ``computer_fetch_image``.  The retention rule for the stable tree
# is ONE sentence: **a frame is retained iff THIS reply carries it.**  A reply that
# carries no image (text mode, an opt-out, a rejection, a failure return) stores
# nothing and carries no evidence keys — "no image in this reply" IS "nothing kept".
# Every hook runs at the point where the reply is already committed to carrying the
# image, after every early return that ends in an image-less reply.


def _record_observation_evidence(
    bundle: "_SessionBundle",
    observation: Any,
    *,
    source: str,
    digest: str,
    payload: str | None = None,
    crop_region: Any = None,
    visual_view: str | None = None,
) -> dict[str, Any]:
    """Retain the freshly captured frame an observe-family reply carries.

    ``payload`` overrides what is stored — and MUST be exactly the bytes the reply
    carries (VAL-WP5 F1): the zoom path stores the CROP it served, the observe path
    stores the carried derived view (grid) or the raw capture (raw/absent request).
    Both are the PRE-ladder bytes, never the post-ladder copy, so a later fetch can
    re-run the budget ladder and report an honest ``image_outbound_scale``.  The
    frame's width/height come from the Observation; the zoom's crop carries the
    crop region so its coordinates are never mistaken for full-screen ones, and
    ``visual_view`` records the derived view the frame shows (a fetched image is
    self-describing).
    """
    return register_frame(
        bundle.extra,
        payload=payload if payload is not None else observation.image_base64,
        source=source,
        attached=True,
        width=observation.width,
        height=observation.height,
        digest=digest,
        observation_id=observation.observation_id,
        coordinate_scale_x=observation.coordinate_scale_x,
        coordinate_scale_y=observation.coordinate_scale_y,
        coordinate_space=getattr(observation.coordinate_space, "value", None),
        crop_region=crop_region,
        visual_view=visual_view,
        redactions_applied=observation.redactions_applied,
    )


# evidence-by-backend seam — WIRED: ``_plan_evidence`` (in the plan surface
# section above) builds the plan-step/terminal ``evidence_by_backend`` block from
# ``EvidenceStore.captures()`` (the store read API, filled by the reply hooks — real
# on BOTH backends, no fake-only ``executed``/``observed`` reads, S-03) plus the
# lease's retained repositions, and publishes it on every plan step and terminal
# plan reply. The dev copy's fake-only block is gone by design (decision D-2).


#: Rejection wording for ``computer_fetch_image`` (unified contract §1.3: a fetch
#: refusal is a ``rejected`` reply whose ``reasons`` name the typed error).
_EVIDENCE_NOT_FOUND_MESSAGE = (
    "No stored image with this image_id exists on this session. Image ids are "
    "session-scoped, live in memory only, and a frame is dropped when it falls "
    "outside the session's retention bounds — so a wrong id, another session's id, "
    "and an id whose frame was already retired are indistinguishable by design."
)

_EVIDENCE_STORE_DISPOSED_MESSAGE = (
    "This session's evidence store is no longer available: the session is being "
    "torn down or was already stopped, so no stored frame can be served. Start a "
    "new session and capture again if you need the screen."
)

_EVIDENCE_FRAME_TOO_LARGE_MESSAGE = (
    "The stored frame exceeds this session's evidence byte ceiling, so it cannot "
    "be served. This is a fail-closed guard; capture a fresh observation instead."
)

#: Which retention policy retired a frame, in the words the model reads.
_EVIDENCE_RETIRE_POLICIES = {
    RETIRE_COUNT_CAP: "the frame count cap",
    RETIRE_BYTE_CAP: "the byte cap",
    RETIRE_AGE_CAP: "the age cap",
}


def _evidence_rejection(error: str, message: str, **extra: Any) -> dict[str, Any]:
    """The fetch rejection envelope (§1.1): ok:false, result:"rejected", a non-empty
    ``reasons`` list naming the typed error, and NO ``error`` key."""
    payload: dict[str, Any] = {
        "ok": False,
        "result": "rejected",
        "message": message,
        "reasons": [error],
    }
    payload.update(extra)
    return payload


async def _fetch_image_response(store: EvidenceStore, image_id: str) -> Any:
    """Resolve one id against THIS session's store and shape the reply.

    The outcomes are distinguishable answers: a frame that is still here is served
    (``verified`` + the image + ``image_outbound_scale``); a frame a retention policy
    retired answers ``image_expired`` naming WHICH policy; a store disposed under the
    lookup answers ``store_disposed`` ATOMICALLY (VAL-WP5 F3 — the disposal check
    shares the store lock, so a fetch racing the teardown can never answer
    ``image_not_found`` for a frame that existed a moment ago); and an id that never
    existed — a malformed id, and another session's id alike — answers
    ``image_not_found``, byte-identical in those forms, so a wrong id can never probe
    which ids exist in another session.
    """
    lookup: EvidenceLookup = store.lookup(image_id)
    if lookup.outcome == "not_found":
        # Byte-identical for a malformed id, an id that never existed, and another
        # session's id.  The message deliberately does NOT echo the value and does
        # NOT distinguish the three cases.
        return _evidence_rejection("image_not_found", _EVIDENCE_NOT_FOUND_MESSAGE)
    if lookup.outcome == "disposed":
        # Atomic with the frame read under the store lock (VAL-WP5 F3).
        return _evidence_rejection(
            EVIDENCE_REFUSE_DISPOSED, _EVIDENCE_STORE_DISPOSED_MESSAGE
        )
    if lookup.outcome == "expired":
        # Name WHICH policy retired it and the session's bounds, so the model can
        # tell "ask sooner" from "ask less".
        policy = _EVIDENCE_RETIRE_POLICIES.get(lookup.reason or "", "a retention policy")
        retention = store.retention()
        return _evidence_rejection(
            "image_expired",
            f"This frame is no longer available: it was retired by {policy}. This "
            f"session's retention bounds are {retention['max_frames']} frames, "
            f"{retention['max_bytes']} bytes, {retention['max_age_seconds']} s. "
            "Capture a new observation if you need to see the screen again.",
            retired_by=lookup.reason,
            evidence_retention=retention,
        )
    frame = lookup.frame
    assert frame is not None  # lookup() only returns "found" with a frame
    if frame.byte_size > store.max_bytes:
        # Fail-closed guard: a frame larger than the session's byte ceiling can never
        # have been stored through ``store()``; refuse rather than serve oversize.
        return _evidence_rejection(
            "frame_too_large",
            _EVIDENCE_FRAME_TOO_LARGE_MESSAGE,
            evidence_retention=store.retention(),
        )
    # The PRE-ladder original is re-bounded NOW, so the scale reported is the scale
    # of the bytes actually being returned and a frame stored under the budget is
    # served at its original resolution.  VAL-WP5 F5: the PIL re-encode ladder runs
    # OFF the event-loop thread via asyncio.to_thread — the loop stays responsive
    # while a large frame is encoded (pinned by a blocked-encode test).
    bounded_b64, mime_type, applied_scale = await asyncio.to_thread(
        _bound_outbound_image, frame.image_base64
    )
    metadata: dict[str, Any] = {
        "ok": True,
        "result": "verified",
        "message": (
            "Stored frame served (a stored capture is not necessarily current). "
            "A fetched frame may be served at a different resolution than the reply "
            "that published it — the reply that published the image_id may have "
            "carried a downscaled/JPEG copy, while this tool re-bounds the stored "
            "pre-ladder original and discloses its own image_outbound_scale."
        ),
        "session_id": frame.session_id,
        "image_id": frame.image_id,
        "image_format": mime_type,
        IMAGE_OUTBOUND_SCALE_KEY: applied_scale,
        "captured_at": frame.captured_at,
        "age_seconds": store.age_seconds(frame.image_id),
        "width": frame.width,
        "height": frame.height,
        "bytes": frame.byte_size,
        "source": frame.source,
        "observation_id": frame.observation_id,
        "digest": frame.digest,
        "coordinate_scale_x": frame.coordinate_scale_x,
        "coordinate_scale_y": frame.coordinate_scale_y,
        "coordinate_space": frame.coordinate_space,
        "crop_region": list(frame.crop_region) if frame.crop_region else None,
        # VAL-WP5 F1: the derived view this frame shows (grid etc.) — a fetched
        # image is self-describing; None for a raw capture.
        "visual_view": frame.visual_view,
        "redactions_applied": frame.redactions_applied,
        "evidence_retention": store.retention(),
        "freshness_note": FETCH_FRESHNESS_NOTE,
    }
    return [
        TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)),
        ImageContent(type="image", data=bounded_b64, mimeType=mime_type),
    ]


@mcp.tool()
@_session_scoped
async def computer_fetch_image(session_id: str, image_id: str) -> Any:
    """Re-serve a frame this session captured earlier, by id.

    Every capture THIS SESSION'S REPLIES actually carry (computer_observe,
    computer_screenshot, computer_zoom, computer_execute) is retained under a unique
    ``image_id`` published on that same reply — EXACTLY the bytes the reply carried
    (for a derived visual view, the derived image with its labels, not the raw
    capture).  This tool hands that exact frame back to you as a real image block,
    with its metadata.  It never auto-sends: nothing is fetched until you ask, by id.

    THE MANDATORY SCENARIO: capture -> read the ``image_id`` off the reply -> decide
    you must look at that frame again -> call this tool.  This is how you re-examine
    something you skimmed past, or re-check a step's evidence after doing something
    else.

    A FETCHED IMAGE IS NOT NECESSARILY CURRENT.  Read ``captured_at`` and
    ``age_seconds`` before you treat it as present truth — it is a picture of the
    screen at the moment it was captured, and anything you have done since then is
    not in it.  When you need to know what is on screen NOW, call
    ``computer_observe``; use this tool to look back, never to look forward.

    The verified reply carries ``image_id``, ``captured_at``, ``age_seconds``, the
    stored ``width``/``height``, ``image_format``, the ``image_outbound_scale`` of
    the copy you are being returned NOW, the source ``observation_id``/``digest``,
    the ``visual_view`` when the frame is a derived view, the ``crop_region`` when
    the frame came from ``computer_zoom``, and the session's retention bounds.

    RETENTION: 16 frames / 24 MB / 3600 s per session, in memory only.  The oldest
    frame is dropped when a cap is exceeded and every frame is dropped when the
    session ends (stop_session, the idle timeout, or the server restarting).

    REJECTED REPLIES (ok:false, result:"rejected", ``reasons`` names the error):
    ``image_not_found`` — the id does not exist on THIS session.  An id belonging to
    a different session and a malformed id return the BYTE-IDENTICAL reply, so a
    wrong id can never probe another session's images.  ``image_expired`` — the
    frame was retired by the count, byte or age cap; ``retired_by`` names which.
    ``store_disposed`` — the session's store is already gone (teardown).  A stopped
    session returns ``session_stopped`` and an unknown one ``unknown_session``.

    This tool can never return a ``RAW:`` placeholder: a text-delivery session
    (``start_session(image_delivery="text")``) has no image bytes at all, so no id
    is ever issued on one.
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    # S-04 (WP7b, wired): a fetch is a READ-ONLY retrieval of evidence this session
    # already holds — it stays ALLOWED while a plan lease is active. There is
    # deliberately NO lease refusal on this surface (the observe/zoom refusal does
    # not extend here); the mid-lease fetch behavior is pinned by
    # tests/test_wp7b_plan_surface.py::test_a_fetch_succeeds_mid_lease.
    store = evidence_store(bundle.extra)
    if store is None:
        # Fail-closed: a live session always has a store (written at bundle
        # construction), so reaching here means the bundle is mid-teardown — the
        # exactly-once disposal already popped the key.  The answer is the typed
        # disposed refusal, never a partial image and never a resurrection; a store
        # disposed BETWEEN this check and the lookup is caught atomically by the
        # lookup itself (VAL-WP5 F3 — same lock as the frame read).
        return _evidence_rejection(
            EVIDENCE_REFUSE_DISPOSED, _EVIDENCE_STORE_DISPOSED_MESSAGE
        )
    return await _fetch_image_response(store, image_id)


def main() -> None:
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
