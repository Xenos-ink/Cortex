"""Session state and session-scope config: registries, bundles, factories, managers, session errors, error envelope, image-delivery config, limits parsing (LAYER 1 - imports no server sibling above it)."""

from __future__ import annotations

import math
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

from ..core.limits import LimitEnforcer, LimitExceeded, Limits
from ..core.models import SessionState
from ..core.state import SessionContext, SessionLimitExceeded, SessionRegistry, TaskStopped
from ..desktop.backend import ComputerBackend, LocalComputerBackend
from ..pipeline.agent import ComputerUseAgent
from ..pipeline.provider import OpenAICompatibleVisionProvider
from ..planning.checkpoint_manager import CheckpointManager
from ..planning.resume_manager import ResumeManager
from ..security.audit import AuditLogger, Metrics
from .instance import logger



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
