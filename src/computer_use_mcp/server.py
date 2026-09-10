"""MCP server wiring: 5 deterministic tools, bounded session registry, kill path, audit, limits.

Compatibility contract (master-mission section 6, binding, as amended 2026-09 by user
order — the run_goal family is REMOVED PERMANENTLY):

- The exposed tool surface is exactly FIVE deterministic tools: ``start_session``,
  ``stop_session``, ``computer_observe``, ``computer_screenshot``,
  ``computer_execute``. The internal-LLM-loop family (``run_goal``, ``run_subtask``,
  ``create_subtask``, ``list_subtasks``, ``get_session_progress``) is deleted — the
  host model drives the tools directly; no model-decides loop remains in Cortex.
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
  ``screenshot_after_base64`` from the response; omitted or None keeps the legacy payload.
  ``follow_ups`` (additive, max 5) queues actions that each pass the FULL independent
  pipeline; the queue stops at the first failure — zero bypass.
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
from .long_running import LongRunningRuntime
from .models import (
    MAX_FOLLOW_UPS,
    ActionSpec,
    GroundedAction,
    SessionState,
)
from .observation import observation_text_summary
from .provider import OpenAICompatibleVisionProvider
from .redaction import redact_text
from .resume_manager import ResumeBundle, ResumeManager, ResumeRefusalError
from .safety import SafetyPolicy
from .state import SessionContext, SessionLimitExceeded, SessionRegistry, TaskStopped

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


def _bound_outbound_image(data_b64: str, frame: Any = None) -> tuple[str, str]:
    """Return ``(base64, mimeType)`` for the OUTBOUND copy, under the size budget.

    H7: the outbound PNG travels as-is while it fits the budget; an oversized PNG is
    re-encoded as JPEG (quality 85, no alpha channel) and, if still over budget,
    progressively downscaled by 0.85 steps until it fits. INTERNAL pipeline bytes are
    never touched: captures, verification pixel-diff, and checkpoints keep PNG exactly
    as today. Degradation (PIL failure) returns the original — an oversized real image
    beats none.

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
        return data_b64, "image/png"
    if len(decoded) <= _result_image_max_bytes():
        return data_b64, "image/png"
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


def _execute_response_blocks(response: dict[str, object], frame: Any = None) -> list[Any]:
    """REM-A H3/H5: executed ``computer_execute`` results as MCP content blocks.

    Returns one TextContent (the result JSON with the image blob stripped and the
    outbound format noted) plus one ImageContent carrying the post-action screenshot
    as a real image block (bounded by :func:`_bound_outbound_image`) — parity with
    ``computer_observe``. When the caller opted out
    (``include_screenshot_after=False``, honored BEFORE this point) the response
    carries no blob and this yields the text block only — never an empty image.
    ``frame`` (R-5 W4, optional) is the capture-time RGB frame for the outbound
    JPEG ladder (skips re-decoding the PNG; byte-identical outputs).
    """
    payload = dict(response)
    data_b64 = payload.pop("screenshot_after_base64", None)
    blocks: list[Any] = []
    if data_b64 is not None:
        bounded_b64, mime = _bound_outbound_image(str(data_b64), frame=frame)
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
NullableStrMap = Annotated[dict[str, Any] | None, _PLAIN_NULLABLE_STRING_MAP]
NullableNumberMap = Annotated[dict[str, float] | None, _PLAIN_NULLABLE_NUMBER_MAP]
NullableKeys = Annotated[TolerantKeys | None, _PLAIN_WIDENED_NULLABLE_STRING_ARRAY]


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
            # D1: the mode is visible in the transcript right after creation.
            "image_delivery": image_mode,
        }
    )
    if image_mode == "text":
        payload["image_delivery_note"] = IMAGE_DELIVERY_TEXT_NOTE
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
    In a text-mode session (``start_session(image_delivery="text")`` /
    ``CORTEX_IMAGE_DELIVERY=text``) this returns ONE text block only (metadata +
    ``image_delivery`` keys) — never an image block — so non-vision models stay
    alive; the delivery mode is fixed at start_session (pass image_delivery="text"
    there if your inputs are text-only). Error paths still return the structured
    error dict.
    """
    try:
        bundle = _get_live_bundle(session_id)
    except (_StoppedSession, _UnknownSession) as exc:
        return _error_response(exc)
    # D1: text mode suppresses the OUTBOUND image block only — a non-vision model
    # receiving one ImageContent anywhere in its history gets the whole provider
    # request rejected with a 400 and the session dies permanently.
    image_mode = _session_image_delivery(bundle)
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
            "observation": _bound_observe_lists(observation_dump),
            "digest": digest,
            "observation_id": observation.observation_id,
            "active_app": info.process_name if info is not None else observation.active_window,
            "image_format": "none",  # truthful: no image block was emitted
            "image_delivery": "text",
            "image_delivery_note": IMAGE_DELIVERY_TEXT_NOTE,
            # PERF-004 C8 (additive): bounded one-line grounding text for weak models.
            "text_summary": text_summary,
        }
        return [TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False))]
    outbound_b64, outbound_mime = _bound_outbound_image(
        # R-5 (W4): the capture-time frame (backend stash) skips the PNG re-decode on
        # the JPEG ladder; absent (fakes/legacy) the path decodes exactly as before.
        observation.image_base64, frame=getattr(observation, "_frame", None)
    )
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
    include_screenshot_after: NullableBool = None,
    follow_ups: NullableFollowUps = None,
) -> Any:
    """Validate and execute one grounded action; approval applies only to this action call.

    session_id is REQUIRED on every call: copy it from start_session's result and
    reuse it for the whole session.

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
    The D5-widened advertised schema admits integer | string | null; non-numeric
    garbage ("left") still fails with a clean typed error. ``follow_ups`` also
    accepts a JSON-encoded string containing the list, a single dict (wrapped into a
    one-entry list), and per-entry JSON strings — same fail-closed queue semantics
    afterwards.

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
    In a text-mode session (``start_session(image_delivery="text")``) the executed
    response is the slim dict (no image block, no screenshot bytes, mode keys added)
    and ``include_screenshot_after`` cannot re-enable images in text mode.

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
    # D1: text mode dominates every executed response shape — include_screenshot_after
    # can only REMOVE bytes, never re-enable an image (a non-vision model passing
    # True must not be able to kill its own session).
    image_mode = _session_image_delivery(bundle)
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
    # D1 text mode: the slim dict shape (the opt-out precedent, mode keys added) —
    # include_screenshot_after can never promote text mode back to image mode.
    if outcome.kind == "executed" and "screenshot_after_base64" in response:
        if image_mode == "text":
            response.pop("screenshot_after_base64", None)
            response["image_delivery"] = "text"
            response["image_delivery_note"] = IMAGE_DELIVERY_TEXT_NOTE
            return response
        # R-5 (W4): the capture-time frame rides along (never serialized; absent for
        # legacy results) so the outbound JPEG ladder skips re-decoding the PNG.
        frame = getattr(result, "_frame", None) if result is not None else None
        return _execute_response_blocks(response, frame=frame)
    return response
def main() -> None:
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
