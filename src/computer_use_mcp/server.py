"""MCP server wiring: 6 tools, bounded session registry, kill path, audit, limits.

Compatibility contract (master-mission section 6, binding):

- The 6 tool names, stdio transport, and parameter positions are preserved; signatures
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
  ``screenshot_after_base64`` from the response; omitted/None keeps the legacy payload.
  ``follow_ups`` (additive, max 5) queues actions that each pass the FULL independent
  pipeline; the queue stops at the first failure — zero bypass.
- ``run_goal`` keeps exactly ``approval_budget = 1`` per call when
  ``approve_next_action=True``; bounded recovery retries of the same approved action
  instance do not re-consume budget; a new distinct action after exhaustion is denied
  fail-closed with ``requires_approval`` in the response.
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

Test/extension seam (for E6/E7): the module-level ``_backend_factory`` and
``_provider_factory`` callables are invoked once per ``start_session``; tests monkeypatch
them to inject ``FakeComputerBackend``/scripted providers, and read session wiring via
``_get_bundle(session_id)`` (agent, state, backend, enforcer, auditor, metrics).
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import io
import json
import logging
import math
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from dataclasses import fields as dataclass_fields
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
from PIL import Image
from pydantic import BeforeValidator

from .agent import ComputerUseAgent
from .audit import AuditLogger, Metrics
from .backend import ComputerBackend, LocalComputerBackend
from .checkpoint_manager import (
    CheckpointError,
    CheckpointManager,
    CheckpointTrigger,
    CheckpointValidationError,
)
from .context_manager import ContextManager
from .interference import parse_interference
from .limits import LimitEnforcer, LimitExceeded, Limits
from .long_running import (
    LongRunningError,
    LongRunningRuntime,
    PlannerUnavailableError,
    RuntimeBusyError,
    SubtaskNotRunnableError,
)
from .models import (
    MAX_FOLLOW_UPS,
    ActionSpec,
    ExecutionResult,
    GroundedAction,
    SessionState,
    SubtaskStatus,
)
from .observation import observation_text_summary
from .plan_validator import PlanRejectedError
from .provider import OpenAICompatibleVisionProvider
from .redaction import redact_text
from .resume_manager import ResumeBundle, ResumeManager, ResumeRefusalError
from .safety import SafetyPolicy
from .state import SessionContext, SessionLimitExceeded, SessionRegistry, TaskStopped
from .subtask_manager import (
    DependencyCycleError,
    InvalidSubtaskError,
    InvalidTransitionError,
    SelfDependencyError,
    SubtaskAlreadyExistsError,
    SubtaskError,
    SubtaskLimitExceeded,
    SubtaskNotReadyError,
    UnknownDependencyError,
    UnknownSubtaskError,
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
    and surface as a fail-closed error at decide-time instead of breaking start_session;
    the agent converts any provider failure into an audited, bounded recovery path.
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

    async def decide(self, goal: str, observation: Any, history: list[str]) -> Any:
        provider = self._resolve()
        result = provider.decide(goal, observation, history)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def decide_full(self, goal: str, observation: Any, history: list[str]) -> Any:
        provider = self._resolve()
        delegate = getattr(provider, "decide_full", None)
        if callable(delegate):
            result = delegate(goal, observation, history)
            if inspect.isawaitable(result):
                result = await result
            return result
        decision = await self.decide(goal, observation, history)
        return SimpleNamespace(
            decision=decision,
            expected_effect=None,
            verification_hint=None,
            suspicious_content=None,
            redactions_applied=[],
        )

    def judge_change(self, before_b64: str, after_b64: str, expected_effect: str) -> Any:
        provider = self._resolve()
        delegate = getattr(provider, "judge_change", None)
        if not callable(delegate):
            raise TypeError(
                "Provider does not expose judge_change; model-based verification is unavailable."
            )
        return delegate(before_b64, after_b64, expected_effect)

    async def plan_subtasks(self, goal: str, **kwargs: Any) -> Any:
        """Delegate the long-running planner call (lazy; typed failure without a key)."""
        provider = self._resolve()
        delegate = getattr(provider, "plan_subtasks", None)
        if not callable(delegate):
            raise TypeError("Provider does not expose plan_subtasks; planning is unavailable.")
        result = delegate(goal, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def summarize_context(self, request: Any) -> Any:
        """Delegate the bounded context summarizer (ContextManager falls back on failure)."""
        provider = self._resolve()
        delegate = getattr(provider, "summarize_context", None)
        if not callable(delegate):
            raise TypeError("Provider does not expose summarize_context.")
        result = delegate(request)
        if inspect.isawaitable(result):
            result = await result
        return result


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
    session_id: str, bundle: _SessionBundle, *, source: str, audit_stop_events: bool = True
) -> None:
    """Shared kill-path cleanup (F7): one stopping flavor, one hygiene outcome.

    Arms the token (idempotent), mirrors the flag onto the legacy session state, audits
    the stop, removes the bundle from the store AND the registry, and records the
    bounded stopped-session snapshot. ``audit_stop_events=False`` is used when the stop
    was already audited by another path (in-run emergency_stop, detection event).

    B4 (T8): teardown is ATOMIC under the lock and the stopped-session snapshot is
    remembered FIRST, with every fallible piece guarded — a failure inside the audit
    or the metrics snapshot can never again leave the registry/bundle store popped
    WITHOUT the stopped memory (the exact state that surfaces to clients as a
    bogus ``unknown_session`` for a session that was already stopped).
    """
    bundle.context.stop.stop()
    bundle.state.stopped = True
    bundle.state.pending_approval_token = None
    if audit_stop_events:
        for event_type in ("stop", "emergency_stop"):
            try:
                bundle.auditor.emit(
                    event_type,
                    session_id,
                    task_id=bundle.context.task.task_id,
                    result="stopped",
                    metadata={"source": source},
                )
            except Exception:
                logger.debug("stop audit failed", exc_info=True)
    with _lock:
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
        _remember_stopped(session_id, snapshot)
        _bundles.pop(session_id, None)
        _registry.remove(session_id)


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
        payload["message"] = (
            "Session is stopped; refusing to proceed (fail-closed stopped-session policy)."
        )
    return payload


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
    """F2 defense-in-depth for PERF-004 C7 queue entries: redact text at the sink."""
    redacted = dict(entry)
    if redacted.get("message"):
        redacted["message"] = redact_text(str(redacted["message"]))[0]
    reasons = redacted.get("reasons")
    if isinstance(reasons, list):
        redacted["reasons"] = [redact_text(str(reason))[0] for reason in reasons]
    verification_note = redacted.get("verification_note")
    if verification_note:
        redacted["verification_note"] = redact_text(str(verification_note))[0]
    return redacted


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


def _bound_outbound_image(data_b64: str) -> tuple[str, str]:
    """Return ``(base64, mimeType)`` for the OUTBOUND copy, under the size budget.

    H7: the outbound PNG travels as-is while it fits the budget; an oversized PNG is
    re-encoded as JPEG (quality 85, no alpha channel) and, if still over budget,
    progressively downscaled by 0.85 steps until it fits. INTERNAL pipeline bytes are
    never touched: captures, verification pixel-diff, and checkpoints keep PNG exactly
    as today. Degradation (PIL failure) returns the original — an oversized real image
    beats none.
    """
    try:
        decoded = base64.b64decode(data_b64, validate=True)
    except Exception:  # noqa: BLE001 - unusable environment is fail-closed data
        return data_b64, "image/png"
    if len(decoded) <= _result_image_max_bytes():
        return data_b64, "image/png"
    try:
        image = Image.open(io.BytesIO(decoded))
        image.load()
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
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
        return base64.b64encode(last_bytes).decode("ascii"), "image/jpeg"
    except Exception:  # noqa: BLE001 - degradation: deliver the original image
        logger.debug("outbound image re-encode failed; delivering original PNG", exc_info=True)
        return data_b64, "image/png"


def _execute_response_blocks(response: dict[str, object]) -> list[Any]:
    """REM-A H3/H5: executed ``computer_execute`` results as MCP content blocks.

    Returns one TextContent (the result JSON with the image blob stripped and the
    outbound format noted) plus one ImageContent carrying the post-action screenshot
    as a real image block (bounded by :func:`_bound_outbound_image`) — parity with
    ``computer_observe``. When the caller opted out
    (``include_screenshot_after=False``, honored BEFORE this point) the response
    carries no blob and this yields the text block only — never an empty image.
    """
    payload = dict(response)
    data_b64 = payload.pop("screenshot_after_base64", None)
    blocks: list[Any] = []
    if data_b64 is not None:
        bounded_b64, mime = _bound_outbound_image(str(data_b64))
        payload["image_format"] = mime
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
        kwargs[str(key)] = float(value)
    return Limits(**kwargs).validate()  # type: ignore[arg-type]


# --- REM-F weak-model tolerant argument coercion (ORVEX-CORTEX-055, boundary only) --------------
# The live Kimi Code / GLM-5V run proved the pipeline works but the DRIVING MODEL
# could not pass the strict pydantic schema four times (float coordinates, a
# follow_ups array shape, an allowed_processes array) and gave up on clicking.
# These helpers add BOUNDARY tolerance on exactly that class — the advertised tool
# schemas stay self-describing ("integer" / "array"), the coercion runs
# PRE-validation, and every downstream internal model (GroundedAction, Limits,
# ActionSpec bounds) stays strict and unchanged. Non-numeric garbage still fails
# with a clean pydantic-style typed error — this is tolerance, not semantics change.


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
_PLAIN_NULLABLE_INTEGER = _PlainJsonSchema({"type": ["integer", "null"]})
_PLAIN_NULLABLE_STRING = _PlainJsonSchema({"type": ["string", "null"]})
_PLAIN_NULLABLE_BOOLEAN = _PlainJsonSchema({"type": ["boolean", "null"]})
_PLAIN_NULLABLE_STRING_ARRAY = _PlainJsonSchema(
    {"type": ["array", "null"], "items": {"type": "string"}}
)
_PLAIN_NULLABLE_OBJECT_ARRAY = _PlainJsonSchema(
    {"type": ["array", "null"], "items": {"type": "object", "additionalProperties": True}}
)
_PLAIN_NULLABLE_STRING_MAP = _PlainJsonSchema(
    {"type": ["object", "null"], "additionalProperties": True}
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
NullableInt = Annotated[TolerantInt | None, _PLAIN_NULLABLE_INTEGER]
NullableStr = Annotated[str | None, _PLAIN_NULLABLE_STRING]
NullableBool = Annotated[bool | None, _PLAIN_NULLABLE_BOOLEAN]
NullableStrList = Annotated[TolerantStrList | None, _PLAIN_NULLABLE_STRING_ARRAY]
NullableFollowUps = Annotated[TolerantFollowUps | None, _PLAIN_NULLABLE_OBJECT_ARRAY]
NullableStrMap = Annotated[dict[str, Any] | None, _PLAIN_NULLABLE_STRING_MAP]
NullableNumberMap = Annotated[dict[str, float] | None, _PLAIN_NULLABLE_NUMBER_MAP]
NullableKeys = Annotated[TolerantKeys | None, _PLAIN_NULLABLE_STRING_ARRAY]


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
# click, double_click, drag, type, keypress, scroll, wait, done, move, hotkey,
# focus_window. There is deliberately NO 'key' and NO 'triple_click' action.

#: The precise valid action list, taught in tool descriptions and error messages.
ACTION_VOCABULARY = (
    "Valid actions (exact names): "
    "click (x,y required), double_click (x,y), drag (x,y start + x2,y2 end, both required), "
    "move (x,y), type (text required), keypress (keys=[\"<one key name>\"]), "
    "hotkey (keys=[2-12 key names], e.g. [\"ctrl\",\"a\"]), scroll (delta -20..20), "
    "wait (delta 0..20 seconds), focus_window (target = window title), "
    "ensure_app (target = \"process\" or \"process|doc-token\"; attaches to an EXISTING "
    "instance — REATTACHED/AMBIGUOUS_INSTANCE/NO_INSTANCE — and may launch the target "
    "server-side when no instance matches, the target is allowlisted, and the "
    "launch=\"server\" policy holds (the default; CORTEX_ATTACH_OR_LAUNCH=driver "
    "restores never-launch)), done."
)

#: Key-name rule taught alongside the vocabulary (Session 1 failure class: text sent
#: as hotkey keys).
KEY_NAME_RULE = (
    "keys must be KEY NAMES (e.g. \"ctrl\", \"a\", \"enter\", \"esc\") — never text, "
    "words, or sentences; to type text use action=\"type\" with text=\"...\". "
    "Single-key presses belong on keypress (a hotkey needs 2-12 keys)."
)


def _teaching_invalid_action(exc: Exception, action: str | None) -> dict[str, object]:
    """Fail-closed ``invalid_action`` response that TEACHES the valid vocabulary.

    Session 1 lost full agent turns to schema rejections (``key``, 1-key ``hotkey``
    with a word payload, ``triple_click``). The rejection now always carries the exact
    valid values and, when recognizable, the closest valid shape for what was tried.
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
    elif tried in {"keypress", "hotkey"}:
        hints.append(
            "Closest valid shape for a hotkey chord: {\"action\": \"hotkey\", \"keys\": [\"ctrl\", \"a\"]} "
            "(2-12 key names). Closest valid shape for one key: {\"action\": \"keypress\", \"keys\": [\"a\"]}."
        )
    message = f"{exc} {ACTION_VOCABULARY}"
    return {"ok": False, "error": "invalid_action", "message": message, "reasons": hints}


# --- long-running session wiring (master-mission 003, additive) -----------------------------


def _build_runtime(
    bundle: _SessionBundle,
    *,
    goal: str = "",
    resume_bundle: ResumeBundle | None = None,
    resumed: bool = False,
) -> LongRunningRuntime:
    """Construct the per-session orchestration runtime over the EXISTING bundle pieces."""
    provider = bundle.agent.provider
    if resume_bundle is not None:
        # Restore the checkpoint's context into a FRESH ContextManager bound to the
        # (lazy) provider summarizer — snapshot/restore is lossless per A3's contract.
        context = ContextManager(
            goal=resume_bundle.goal,
            summarizer=(
                (lambda request: provider.summarize_context(request))  # type: ignore[union-attr]
                if provider is not None and hasattr(provider, "summarize_context")
                else None
            ),
            summarize_every=resume_bundle.limits.context_summarize_every,
        )
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
            resumed=resumed,
        )
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


def _get_or_create_runtime(bundle: _SessionBundle) -> LongRunningRuntime:
    """Return the session's runtime, creating it lazily on first subtask-tool use."""
    runtime = bundle.extra.get("long_running")
    if isinstance(runtime, LongRunningRuntime):
        return runtime
    runtime = _build_runtime(bundle, goal=str(bundle.context.task.goal or ""))
    bundle.extra["long_running"] = runtime
    return runtime


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


_SUBTASK_ERROR_CODES: tuple[tuple[type[Exception], str], ...] = (
    (SubtaskLimitExceeded, "subtask_limit_exceeded"),
    (UnknownSubtaskError, "unknown_subtask"),
    (SubtaskAlreadyExistsError, "subtask_already_exists"),
    (InvalidSubtaskError, "invalid_subtask"),
    (UnknownDependencyError, "unknown_dependency"),
    (SelfDependencyError, "self_dependency"),
    (DependencyCycleError, "dependency_cycle"),
    (InvalidTransitionError, "invalid_transition"),
)


def _subtask_error_response(exc: Exception) -> dict[str, object]:
    """Structured, typed error payloads for subtask/orchestration failures."""
    if isinstance(exc, SubtaskNotReadyError):
        return {
            "ok": False,
            "error": "subtask_not_ready",
            "message": str(exc),
            "subtask_id": exc.subtask_id,
            "unmet_dependencies": list(exc.unmet),
        }
    for exc_type, code in _SUBTASK_ERROR_CODES:
        if isinstance(exc, exc_type):
            payload: dict[str, object] = {"ok": False, "error": code, "message": str(exc)}
            subtask_id = getattr(exc, "subtask_id", None)
            if subtask_id:
                payload["subtask_id"] = subtask_id
            return payload
    long_running_codes: tuple[tuple[type[Exception], str], ...] = (
        (RuntimeBusyError, "runtime_busy"),
        (PlannerUnavailableError, "planner_unavailable"),
        (SubtaskNotRunnableError, "subtask_not_runnable"),
    )
    for exc_type, code in long_running_codes:
        if isinstance(exc, exc_type):
            return {"ok": False, "error": code, "message": str(exc)}
    return {"ok": False, "error": type(exc).__name__, "message": str(exc)}


def _run_goal_stopped_response(session_id: str, approve_next_action: bool) -> dict[str, object]:
    """The standard run_goal stopped-session response shape (E6 contract, D1/D2)."""
    memory = _stopped_sessions.get(session_id, {})
    stopped_result = ExecutionResult(
        ok=False,
        action=GroundedAction(action="done"),
        message="Session is stopped; run_goal refused (fail-closed stopped-session policy).",
    )
    return {
        "ok": False,
        "approval_budget_remaining": 1 if approve_next_action else 0,
        "results": [stopped_result.model_dump()],
        "session_id": session_id,
        "task_id": str(memory.get("task_id", "")),
        "termination_reason": "stopped_by_user",
        "stopped": True,
        "requires_approval": False,
        "step_count": int(memory.get("step_count", 0)),
        "metrics": memory.get("metrics") or {"counters": {}, "latencies": {}},
    }


async def _run_goal_auto_subtasks(
    session_id: str, goal: str, approve_next_action: bool
) -> dict[str, object]:
    """Long-Running/Multi-Subtask mode of ``run_goal`` (spec section 10, additive).

    Plan (provider -> deterministic validation) -> sequential subtask execution through
    the EXISTING executor, all within this call's budget semantics; the response keeps
    the run_goal shape with additive subtask fields. Existing callers (default off) are
    byte-identical.
    """
    try:
        bundle = _get_live_bundle(session_id)
    except _StoppedSession:
        return _run_goal_stopped_response(session_id, approve_next_action)
    except _UnknownSession as exc:
        return _error_response(exc)
    runtime = _get_or_create_runtime(bundle)
    planned = 0
    try:
        runtime.set_goal(goal)
        entries = await runtime.plan_from_llm(goal)
        planned = len(entries)
    except (PlannerUnavailableError, PlanRejectedError) as exc:
        code = "planner_unavailable" if isinstance(exc, PlannerUnavailableError) else "plan_rejected"
        payload: dict[str, object] = {
            "ok": False,
            "error": code,
            "message": str(exc)[:2000],
            "session_id": session_id,
            "planned_subtasks": 0,
        }
        if isinstance(exc, PlanRejectedError):
            payload["codes"] = list(exc.codes)[:10]
        return payload
    try:
        outcome = await runtime.run_pending_subtasks(approve_next_action=approve_next_action)
    except RuntimeBusyError as exc:
        return {"ok": False, "error": "runtime_busy", "message": str(exc), "session_id": session_id}
    except LimitExceeded as exc:
        return {
            "ok": False,
            "error": "limit_exceeded",
            "limit": exc.limit_name,
            "message": str(exc),
            "session_id": session_id,
        }
    task = bundle.agent.task
    results_payload: list[dict[str, object]] = []
    for item in outcome.results:
        results_payload.append(_redact_result_payload(item.model_dump()))
    if task.status.value == "stopped" and session_id in _bundles:
        # F7: an internally-armed kill path that terminated this run gets the SAME
        # bundle hygiene as run_goal (the stop was already audited in-run).
        _close_stopped_bundle(
            session_id, bundle, source="run_goal_kill_path", audit_stop_events=False
        )
    return {
        "ok": bool(outcome.ok),
        "approval_budget_remaining": outcome.approval_budget_remaining,
        "results": results_payload,
        "session_id": session_id,
        "task_id": task.task_id,
        "termination_reason": outcome.termination_reason,
        "stopped": bool(
            outcome.stopped or bundle.state.stopped or task.status.value == "stopped"
        ),
        "requires_approval": bool(outcome.requires_approval or bundle.agent.approval_denied),
        "step_count": bundle.state.step_count,
        "metrics": bundle.metrics.snapshot(),
        # Additive long-running fields (existing keys above unchanged).
        "planned_subtasks": planned,
        "executed_subtasks": list(outcome.executed),
        "replan_attempts": outcome.replan_attempts,
        "detail": outcome.detail[:500],
        "progress": runtime.progress(),
    }


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
    string, coerced to the documented list at the tool boundary only — the
    advertised schema stays array-of-string and ``limits`` stays STRICT
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
    callers that omit the parameter are byte-identical.
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
    # REM-F: normalize the tolerant allowlist shapes for DIRECT callers too (the
    # boundary coercion already produced lists for MCP callers — idempotent here).
    try:
        allowed_windows = _coerce_tolerant_str_list(allowed_windows)
        allowed_processes = _coerce_tolerant_str_list(allowed_processes)
    except ValueError as exc:
        return {"ok": False, "error": "invalid_limits", "message": str(exc)}
    try:
        context = _registry.create()
    except SessionLimitExceeded as exc:
        return {
            "ok": False,
            "error": "session_limit_exceeded",
            "message": str(exc),
            "max_sessions": exc.max_sessions,
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
            runtime = _build_runtime(
                bundle, goal="", resume_bundle=resume_bundle, resumed=True
            )
        except Exception as exc:  # noqa: BLE001 - typed refusal, never a partial restore
            with _lock:
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
        }
    )
    payload.update(resume_extra)
    return payload


@mcp.tool()
def stop_session(session_id: str) -> dict[str, object]:
    """Arm the kill path: no further input is performed after this returns.

    Shape-compatible with the legacy tool; it now arms the thread-safe StopToken checked
    before every physical input, at every loop checkpoint, and inside waits, and it
    audits stop + emergency_stop events. The session bundle is closed and removed from
    the registry (D1): subsequent tool calls fail closed with ``session_stopped``.
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
    _close_stopped_bundle(session_id, bundle, source="stop_session")
    return {
        "ok": True,
        "session_id": session_id,
        "message": "Session stopped before the next action.",
        "task_id": bundle.context.task.task_id,
        "termination_reason": bundle.context.task.termination_reason.value
        if bundle.context.task.termination_reason
        else None,
    }


@mcp.tool()
def computer_observe(session_id: str) -> Any:
    """Return the current observation state used for grounding: screenshot, dimensions, window, and cursor.

    On success this returns MCP content blocks: one TextContent carrying the
    observation metadata (dimensions, active window, digest, coordinate scale,
    plus an additive ``text_summary`` one-liner: window title/process, cursor,
    focused-control hint when the backend supplies ui_elements, and
    changed/unchanged versus the previous observation of this session — never the
    raw base64) and one ImageContent carrying the screenshot itself, so
    vision-capable clients receive it as a real image rather than as text.
    Error paths still return the structured error dict.
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
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
    outbound_b64, outbound_mime = _bound_outbound_image(observation.image_base64)
    metadata = {
        "observation": _bound_observe_lists(observation_dump),
        "digest": digest,
        "observation_id": observation.observation_id,
        "active_app": info.process_name if info is not None else observation.active_window,
        "image_format": outbound_mime,
        # PERF-004 C8 (additive): bounded one-line grounding text for weak models.
        "text_summary": text_summary,
    }
    return [
        TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)),
        ImageContent(type="image", data=outbound_b64, mimeType=outbound_mime),
    ]


@mcp.tool()
def computer_screenshot(session_id: str) -> Any:
    """Compatibility alias for the raw screenshot observation."""
    return computer_observe(session_id)


@mcp.tool()
async def computer_execute(
    session_id: str,
    action: str,
    x: NullableInt = None,
    y: NullableInt = None,
    text: NullableStr = None,
    keys: NullableKeys = None,
    delta: int = 0,
    approved: bool = False,
    expected_effect: NullableStr = None,
    x2: NullableInt = None,
    y2: NullableInt = None,
    target: NullableStr = None,
    include_screenshot_after: NullableBool = None,
    follow_ups: NullableFollowUps = None,
) -> Any:
    """Validate and execute one grounded action; approval applies only to this action call.

    ACTION VOCABULARY (exact names, from the models enum): click, double_click, drag,
    type, keypress, scroll, wait, done, move, hotkey, focus_window, ensure_app. There is
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

    REM-F weak-model tolerance (boundary only): integer coordinates also accept
    integral floats (1343.0) and numeric strings ("1343"); NON-INTEGRAL values ROUND
    to the nearest int (a vision model aiming at pixel 1343.7 must not hard-fail).
    The advertised schema still says "integer"; non-numeric garbage ("left") still
    fails with a clean typed error. ``follow_ups`` also accepts a JSON-encoded string
    containing the list, a single dict (wrapped into a one-entry list), and
    per-entry JSON strings — same fail-closed queue semantics afterwards.

    Host-payload opt-out (PERF-004, trailing optional): pass
    ``include_screenshot_after=false`` to OMIT the heavy image block
    (and any image bytes) from this response — recommended for actions whose outcome
    you check via the verification verdict + digest instead of the image (the full
    image stays available via computer_observe). Omitted or None keeps the default
    response. Result image budget (REM-A H7): the outbound image is kept under
    ``CORTEX_RESULT_IMAGE_MAX_KB`` (default 180 KB; oversized PNG re-encodes as JPEG
    outbound only — internal captures stay PNG). Executed responses return MCP
    content blocks (TextContent result JSON + ImageContent post-action screenshot,
    parity with computer_observe); error/rejection/approval shapes stay plain dicts.

    Queued actions (PERF-004, trailing optional): ``follow_ups`` is a list of at most 5
    action specs (same fields as this tool's action parameters, e.g.
    {"action": "click", "x": 10, "y": 20, "expected_effect": "..."}). Each follow-up
    passes the FULL independent pipeline (validate -> safety -> approval semantics ->
    execute -> verify) exactly like a single action — zero bypass; the queue stops at
    the first DEFINITIVE verification failure, safety rejection, approval requirement,
    or post-action digest surprise (the screen changed since the queued premise was
    captured). An UNCERTAIN verification (no expectation stated / a non-visual
    action pixels cannot judge) does NOT stop the queue; its per-item entry keeps
    the honest uncertain verdict. Batch small related groups and end the batch with
    an observation.
    Per-item results arrive in the additive ``follow_up_results`` field (bounded, no
    per-item screenshots) with ``follow_ups_stopped_reason`` (None = all verified).
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
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
        )
    except Exception as exc:  # noqa: BLE001 - structured error, no traceback
        return _teaching_invalid_action(exc, action)
    specs: list[ActionSpec] = []
    if follow_ups:
        for item in follow_ups:
            try:
                # REM-F: tolerate the same weak-model coordinate shapes inside
                # entries; every strict ActionSpec bound still applies afterwards.
                specs.append(ActionSpec.model_validate(_coerce_follow_up_entry(item)))
            except Exception as exc:  # noqa: BLE001 - malformed queue item: fail closed
                return _teaching_invalid_action(exc, str(item.get("action", "")))
    try:
        outcome = await bundle.agent.run_single(
            bundle.state,
            grounded,
            approved=approved,
            expected_effect=expected_effect,
            follow_ups=specs or None,
        )
    except TaskStopped as exc:
        return {"ok": False, "stopped": True, "message": str(exc)}
    except LimitExceeded as exc:
        return {"ok": False, "error": "limit_exceeded", "limit": exc.limit_name, "message": str(exc)}
    if outcome.kind == "rejected":
        response: dict[str, object] = {
            "ok": False,
            # T8: rejections carry their SPECIFIC message (e.g. "Focus interference: the
            # OS-focused window is not the session target.") instead of the generic
            # grounding text; the structured event payloads ride in ``reasons``.
            "message": outcome.message or "Grounding rejected.",
            "reasons": outcome.reasons,
        }
    elif outcome.kind == "safety_denied":
        response = {"ok": False, "message": outcome.message}
    elif outcome.kind == "approval_required":
        response = {"ok": False, "requires_approval": True, "message": outcome.message}
    elif outcome.kind == "digest_surprise":
        response = {"ok": False, "error": "digest_surprise", "message": outcome.message}
    elif outcome.kind == "error":
        response = {"ok": False, "error": "action_error", "message": outcome.message}
    else:
        result = outcome.result
        if result is None:  # defensive: executed outcomes always carry a result
            return {"ok": False, "error": "action_error", "message": "Execution produced no result."}
        payload = result.model_dump()
        # F2: the response path is redacted too (defense in depth).
        payload = _redact_result_payload(payload)
        payload.update(
            {
                "model_confidence": outcome.model_confidence,
                "grounding_confidence": outcome.grounding_confidence,
                "verification_confidence": outcome.verification_confidence,
            }
        )
        response = payload
    # PERF-004 C4: host-payload opt-out (additive, off by default) — omit the heavy
    # image from the response entirely when the caller asked for it.
    if include_screenshot_after is False:
        response.pop("screenshot_after_base64", None)  # also covers any nested path (H5)
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
    if outcome.kind == "executed" and "screenshot_after_base64" in response:
        return _execute_response_blocks(response)
    return response


@mcp.tool()
async def run_goal(
    session_id: str,
    goal: str,
    approve_next_action: bool = False,
    auto_subtasks: bool = False,
) -> dict[str, object]:
    """Run the loop; approve_next_action authorizes at most one interactive action in this call.

    Recovery retries of the same approved action instance do not re-consume the budget;
    a new distinct action after exhaustion is denied fail-closed with ``requires_approval``.

    Long-running additive (trailing optional): ``auto_subtasks=True`` switches to the
    Multi-Subtask mode — the goal is decomposed by the LLM planner (validated
    deterministically, fail-closed) and executed as sequential subtasks through the same
    closed-loop executor. Callers omitting it get byte-identical single-goal behavior.
    """
    if auto_subtasks:
        return await _run_goal_auto_subtasks(session_id, goal, approve_next_action)
    try:
        bundle = _get_live_bundle(session_id)
    except _StoppedSession:
        # D1 fail-closed: a stopped session runs NOTHING. The response keeps the standard
        # run_goal shape (E6 contract) with an explicit failed stopped-result entry, so
        # "ok" is never vacuously true over an empty list (D2).
        memory = _stopped_sessions.get(session_id, {})
        stopped_result = ExecutionResult(
            ok=False,
            action=GroundedAction(action="done"),
            message="Session is stopped; run_goal refused (fail-closed stopped-session policy).",
        )
        return {
            "ok": False,
            "approval_budget_remaining": 1 if approve_next_action else 0,
            "results": [stopped_result.model_dump()],
            "session_id": session_id,
            "task_id": str(memory.get("task_id", "")),
            "termination_reason": "stopped_by_user",
            "stopped": True,
            "requires_approval": False,
            "step_count": int(memory.get("step_count", 0)),
            "metrics": memory.get("metrics") or {"counters": {}, "latencies": {}},
        }
    except _UnknownSession as exc:
        return _error_response(exc)
    approval_budget = 1 if approve_next_action else 0

    def approve_one(_action: GroundedAction, _reason: str) -> bool:
        nonlocal approval_budget
        if approval_budget <= 0:
            return False
        approval_budget -= 1
        return True

    try:
        results = await bundle.agent.run(goal, bundle.state, approval=approve_one)
    except Exception as exc:  # noqa: BLE001 - structured error, no traceback
        return _error_response(exc)
    task = bundle.agent.task
    termination = task.termination_reason.value if task.termination_reason else None
    results_payload: list[dict[str, object]] = []
    for item in results:
        payload = item.model_dump()
        # D3: provider-flagged suspicious content travels with its action result.
        action_payload = payload.get("action") or {}
        suspicious = bundle.agent.suspicious_contents.get(str(action_payload.get("action_id", "")))
        if suspicious:
            payload["suspicious_content"] = suspicious
        # F3: honest completion marking — provider-declared done is model-asserted.
        verification_payload = payload.get("verification") or {}
        if isinstance(verification_payload, dict) and verification_payload.get(
            "verification_method"
        ) == "provider_done":
            payload["completion_evidence"] = "model_declared"
        # F2: the response path is redacted too (defense in depth).
        results_payload.append(_redact_result_payload(payload))
    if task.status.value == "stopped" and session_id in _bundles:
        # F7: an internally-armed kill path that terminated this run gets the SAME
        # bundle hygiene as stop_session (the stop was already audited in-run).
        _close_stopped_bundle(
            session_id, bundle, source="run_goal_kill_path", audit_stop_events=False
        )
    return {
        # D2: an empty result list is NOT a success (no vacuous all() over []).
        "ok": bool(results) and all(item.ok for item in results),
        "approval_budget_remaining": approval_budget,
        "results": results_payload,
        "session_id": session_id,
        "task_id": task.task_id,
        "termination_reason": termination,
        "stopped": bool(bundle.state.stopped or task.status.value == "stopped"),
        "requires_approval": bool(bundle.agent.approval_denied),
        "step_count": bundle.state.step_count,
        "metrics": bundle.metrics.snapshot(),
    }


# --- long-running session tools (SubtasksProtocol section 9; additive) ----------------------


@mcp.tool()
def create_subtask(
    session_id: str,
    description: str,
    depends_on: Annotated[list[str] | None, _PLAIN_NULLABLE_STRING_ARRAY] = None,
) -> dict[str, object]:
    """Create one manual subtask (spec section 9); fail-closed on invalid graphs.

    Validates the session, the description, and the dependency list (every dependency
    must already exist, no self-dependency, no cycles, hard cap 50 subtasks). Works
    without any planner/LLM key.
    """
    if depends_on is not None and (
        isinstance(depends_on, (str, bytes)) or not isinstance(depends_on, (list, tuple))
    ):
        # Tool-boundary type gate: a non-iterable (or string) depends_on must surface as
        # the typed fail-closed error code, never as an escaping TypeError from the MCP
        # tool (the runtime's list() coercion would raise one).
        return {
            "ok": False,
            "error": "invalid_subtask",
            "message": "depends_on must be a list of subtask ids (or null); "
            "refusing a non-iterable value at the tool boundary",
        }
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    runtime = _get_or_create_runtime(bundle)
    try:
        subtask = runtime.create_subtask(description, depends_on)
    except (SubtaskError, LongRunningError) as exc:
        return _subtask_error_response(exc)
    return {
        "ok": True,
        "session_id": session_id,
        "subtask": {
            "subtask_id": subtask.subtask_id,
            "description": subtask.description,
            "status": subtask.status.value,
            "depends_on": list(subtask.depends_on),
            "created_at": subtask.created_at.isoformat(),
        },
        "total_subtasks": len(runtime.subtasks),
    }


@mcp.tool()
def list_subtasks(session_id: str) -> dict[str, object]:
    """List all subtasks with statuses as a structured, bounded response.

    Each summary carries bounded scalar fields plus result/recovery COUNTS — never
    result payloads, never history (spec section 9/19).
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    runtime = _get_or_create_runtime(bundle)
    return {
        "ok": True,
        "session_id": session_id,
        "total": len(runtime.subtasks),
        "counts": runtime.counts(),
        "subtasks": runtime.list_subtasks(),
    }


@mcp.tool()
async def run_subtask(
    session_id: str,
    subtask_id: str,
    approve_next_action: bool = False,
) -> dict[str, object]:
    """Execute exactly ONE ready subtask through the existing closed-loop executor.

    One bounded subtask per MCP call; state and checkpoints persist server-side between
    calls, so no MCP connection needs to stay open for hours (spec section 9, conflict
    C7). The subtask can never bypass safety, approval, grounding, validation, the stop
    token, verification, recovery, or audit. ``approve_next_action`` authorizes at most
    one interactive action in this call (run_goal budget semantics).
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    runtime = _get_or_create_runtime(bundle)
    try:
        outcome = await runtime.run_single_subtask(
            subtask_id, approve_next_action=approve_next_action
        )
    except LimitExceeded as exc:
        return {
            "ok": False,
            "error": "limit_exceeded",
            "limit": exc.limit_name,
            "message": str(exc),
        }
    except (SubtaskError, LongRunningError) as exc:
        return _subtask_error_response(exc)
    task = bundle.agent.task
    results_payload: list[dict[str, object]] = []
    for item in outcome.results:
        results_payload.append(_redact_result_payload(item.model_dump()))
    if task.status.value == "stopped" and session_id in _bundles:
        # F7: an internally-armed kill path gets the SAME bundle hygiene (stop audited
        # in-run already).
        _close_stopped_bundle(
            session_id, bundle, source="run_subtask_kill_path", audit_stop_events=False
        )
    subtask_snapshot = runtime.subtasks.get(subtask_id)
    subtask_status = subtask_snapshot.status.value if subtask_snapshot is not None else "unknown"
    return {
        "ok": bool(outcome.ok),
        "session_id": session_id,
        "subtask_id": subtask_id,
        "status": subtask_status,
        "termination_reason": outcome.termination_reason,
        "requires_approval": bool(outcome.requires_approval),
        "stopped": bool(
            outcome.stopped or bundle.state.stopped or task.status.value == "stopped"
        ),
        "results": results_payload,
        "executed_subtasks": list(outcome.executed),
        "approval_budget_remaining": outcome.approval_budget_remaining,
        "detail": outcome.detail[:500],
        "progress": runtime.progress(),
        "metrics": bundle.metrics.snapshot(),
    }


@mcp.tool()
def get_session_progress(session_id: str) -> dict[str, object]:
    """Deterministic progress report: status, percent, counts, elapsed, counters, checkpoint.

    The progress percentage is computed from subtask manager state (completed/total) —
    never invented. The response is structured and bounded (no history dumps).
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    runtime = bundle.extra.get("long_running")
    if not isinstance(runtime, LongRunningRuntime):
        zero_counts = {status.value: 0 for status in SubtaskStatus}
        budget_like = bundle.metrics.snapshot()["counters"]
        return {
            "ok": True,
            "session_id": session_id,
            "status": bundle.context.task.status.value,
            "task_status": bundle.context.task.status.value,
            "goal": redact_text(str(bundle.context.task.goal or ""))[0][:2_000],
            "continuation_of": None,
            "total_subtasks": 0,
            "completed_subtasks": 0,
            "progress_percent": 0.0,
            "counts": zero_counts,
            "current_subtask_id": None,
            "elapsed_seconds": round(bundle.enforcer.elapsed_seconds(), 3),
            "resource_counters": {
                "actions": int(budget_like.get("action_total", 0)),
                "model_calls": int(budget_like.get("model_calls", 0)),
                "steps": int(bundle.state.step_count),
                "subtasks": 0,
            },
            "resource_limits": {
                "max_session_seconds": bundle.limits.max_session_seconds,
                "max_session_actions": bundle.limits.max_session_actions,
                "max_session_model_calls": bundle.limits.max_session_model_calls,
                "max_session_steps": bundle.limits.max_session_steps,
                "max_subtasks": bundle.limits.max_subtasks,
            },
            "checkpoint": {
                "has_checkpoint": _checkpoint_manager.has_checkpoint(session_id),
                "last_trigger": None,
                "last_checkpoint_at": None,
            },
            "replan_attempts_used": 0,
            "replan_attempts_max": 3,
        }
    payload = runtime.progress()
    payload["ok"] = True
    payload["task_status"] = bundle.context.task.status.value
    return payload


def main() -> None:
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
