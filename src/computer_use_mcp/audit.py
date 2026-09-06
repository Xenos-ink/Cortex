"""Structured audit logging (JSONL) and the metrics registry.

Redaction is ENFORCED at write time: every string field and every metadata value passes
through :func:`computer_use_mcp.redaction.redact_text` before touching disk (datetime and
numeric values are passed through untouched; non-primitive metadata values are JSON-serialized
then redacted). Additionally, string values stored under obviously sensitive metadata keys
(password/token/key/secret/credential/auth/cookie) are redacted wholesale. The in-memory
:class:`AuditEvent` is never mutated; the JSONL file is guaranteed secret-free.

Default sink location is under the system temp directory (``<temp>/computer-use-mcp/logs``)
so no repository files are polluted; callers (and tests) pass an explicit ``log_dir``.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .redaction import redact_text

AuditEventType = Literal[
    "observation",
    "model_decision",
    "grounding",
    "validation",
    "safety",
    "approval",
    "execution",
    "verification",
    "recovery",
    "failure",
    "stop",
    "emergency_stop",
    "limit_exceeded",
    "session_start",
    "session_stop",
]

_REDACTED_STR_FIELDS = (
    "session_id",
    "task_id",
    "observation_id",
    "action_id",
    "active_app",
    "risk",
    "result",
)

# Metadata keys whose very name implies secret content: string values under these keys are
# redacted wholesale (conservative, fail closed). Word boundaries avoid "author" matching.
_SENSITIVE_METADATA_KEY = re.compile(
    r"\b(?:password|passwd|pwd|secret|token|api_key|apikey|access_key|credential|credentials"
    r"|authorization|authentication|auth_token|authtoken|cookie|session_id)\b",
    re.IGNORECASE,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AuditEvent(BaseModel):
    """One structured audit event (Goal.md section 17 field set)."""

    timestamp: datetime = Field(default_factory=_utc_now)
    session_id: str
    task_id: str | None = None
    observation_id: str | None = None
    action_id: str | None = None
    event_type: AuditEventType
    active_app: str | None = None
    risk: str | None = None
    result: str | None = None
    duration_ms: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditLogger:
    """Thread-safe, per-session JSONL audit sink with redaction enforced on write."""

    def __init__(self, log_dir: str | Path | None = None) -> None:
        self.log_dir = Path(log_dir) if log_dir is not None else _default_log_dir()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, event: AuditEvent) -> None:
        """Serialize (with redaction) and append one event to the session's JSONL file."""
        line = json.dumps(self._redacted(event).model_dump(mode="json"), ensure_ascii=False, default=str)
        path = self._path_for(event.session_id)
        with self._lock, path.open("a", encoding="utf-8") as sink:
            sink.write(line + "\n")

    def emit(self, event_type: AuditEventType, session_id: str, **fields: Any) -> AuditEvent:
        """Construct, persist, and return an :class:`AuditEvent` in one call."""
        event = AuditEvent(event_type=event_type, session_id=session_id, **fields)
        self.log(event)
        return event

    def path_for(self, session_id: str) -> Path:
        """Public accessor for the JSONL file backing ``session_id``."""
        return self._path_for(session_id)

    def _path_for(self, session_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)[:100] or "unknown"
        return self.log_dir / f"audit_{safe}.jsonl"

    def _redacted(self, event: AuditEvent) -> AuditEvent:
        data = event.model_dump()
        for key in _REDACTED_STR_FIELDS:
            value = data[key]
            if isinstance(value, str) and value:
                data[key] = redact_text(value)[0]
        data["metadata"] = {str(k): self._redact_value(k, v) for k, v in data["metadata"].items()}
        return AuditEvent.model_validate(data)

    def _redact_value(self, key: str, value: Any) -> Any:
        if isinstance(value, str):
            redacted = redact_text(value)[0]
            if _SENSITIVE_METADATA_KEY.search(key) and value:
                return f"[REDACTED:{key}]"
            return redacted
        if value is None or isinstance(value, bool | int | float):
            return value
        return redact_text(json.dumps(value, default=str, ensure_ascii=False))[0]


def _default_log_dir() -> Path:
    return Path(tempfile.gettempdir()) / "computer-use-mcp" / "logs"


class Metrics:
    """Thread-safe counters + latency tracking (Goal.md section 18 observability set).

    Latencies are kept per metric in bounded deques (most recent 1024 samples);
    ``snapshot()`` computes count/avg/p50/p95/max on demand.
    """

    COUNTER_NAMES: tuple[str, ...] = (
        "task_started",
        "task_completed",
        "task_failed",
        "action_total",
        "action_success",
        "action_failure",
        "verification_verified",
        "verification_failed",
        "verification_uncertain",
        "grounding_failure",
        "safety_block",
        "approval_requested",
        "approval_granted",
        "approval_denied",
        "retry_total",
        "recovery_total",
        "recovery_success",
        "model_calls",
        "screenshot_count",
    )
    LATENCY_NAMES: tuple[str, ...] = (
        "observation_ms",
        "model_ms",
        "execution_ms",
        "verification_ms",
        "task_ms",
    )
    _LATENCY_CAP = 1024

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = dict.fromkeys(self.COUNTER_NAMES, 0)
        self._latencies: dict[str, deque[float]] = {
            name: deque(maxlen=self._LATENCY_CAP) for name in self.LATENCY_NAMES
        }

    def incr(self, counter: str, amount: int = 1) -> None:
        """Increment a named counter (unknown counters are created on first use)."""
        with self._lock:
            self._counters[counter] = self._counters.get(counter, 0) + amount

    def record_latency(self, name: str, value_ms: float) -> None:
        """Record one latency sample (milliseconds) for a named metric."""
        with self._lock:
            self._latencies.setdefault(name, deque(maxlen=self._LATENCY_CAP)).append(float(value_ms))

    def snapshot(self) -> dict[str, Any]:
        """Return a point-in-time copy of all counters and latency summaries."""
        with self._lock:
            counters = dict(self._counters)
            latencies = {name: self._summary(values) for name, values in self._latencies.items()}
        return {"counters": counters, "latencies": latencies}

    @staticmethod
    def _summary(values: deque[float]) -> dict[str, float | int | None]:
        if not values:
            return {"count": 0, "avg_ms": None, "p50_ms": None, "p95_ms": None, "max_ms": None}
        ordered = sorted(values)
        return {
            "count": len(ordered),
            "avg_ms": sum(ordered) / len(ordered),
            "p50_ms": _percentile(ordered, 0.50),
            "p95_ms": _percentile(ordered, 0.95),
            "max_ms": ordered[-1],
        }


def _percentile(ordered: list[float], fraction: float) -> float:
    """Linear-interpolated percentile over an ascending list (len >= 1)."""
    if len(ordered) == 1:
        return ordered[0]
    rank = fraction * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight
