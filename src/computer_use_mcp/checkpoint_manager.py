"""Durable, atomic, redacted, versioned session checkpoints (SubtasksProtocol section 7).

Layering (master-mission 003 section 5): this module imports only domain/substrate
modules (``models``, ``state``, ``plan_validator`` helpers), the Wave-1 persistence
surfaces (``limits``), and ``redaction`` — never orchestration, approval/health, MCP, or
backend modules. It contains NO orchestration loop: the orchestrator (later wave) decides
WHEN to checkpoint via :func:`should_checkpoint` (periodic: 50 steps OR 30 minutes,
whichever first) and the lifecycle triggers in :data:`LIFECYCLE_TRIGGERS` (subtask
completed/failed, transition to a new subtask, before session end, before resume
transitions), then calls :meth:`CheckpointManager.write_checkpoint`.

Doctrine (SubtasksProtocol sections 7/8/19/21):
- **Atomic**: every checkpoint is serialized fully, written to a temp file in the
  destination directory, fsynced, and moved into place with ``os.replace``. A reader never
  observes a partial file, and a failed/interrupted write leaves the previous valid
  checkpoint untouched (the temp file is removed; the previous file is never modified).
- **Redacted**: every string value of the payload passes through
  :func:`computer_use_mcp.redaction.redact_text`; if any value still trips
  :func:`computer_use_mcp.redaction.contains_secret` the write is REFUSED (fail closed,
  :class:`CheckpointRedactionError`). The gate runs per string VALUE, not on the
  serialized JSON text: a JSON pairing such as ``"api_key": "[REDACTED:token_assignment]"``
  would falsely trip the value-oriented assignment patterns (the KEY supplies the
  secret-y name), while the ``[REDACTED:*]`` placeholder value itself is inert.
- **Versioned**: :data:`CHECKPOINT_SCHEMA_VERSION` gates loading; unknown/newer versions
  are rejected fail-closed — never loaded best-effort.
- **Fail-closed integrity**: structural, type, and self-consistency validation on load
  (subtask snapshot parseable, counters numeric, resource limits canonical — i.e. exactly
  the current clamping mechanism's output, current subtask exists in the graph, no heavy
  screenshot payloads persisted, bounded sizes). Corrupt files are REJECTED with a typed
  error; they are never deleted, repaired, or partially loaded.
- **Sealed (tamper-evident)**: every checkpoint carries an HMAC-SHA256 integrity seal
  (:class:`IntegritySeal`) over a canonical serialization of the tamper-sensitive state
  (budget counters, limits, subtask states, identity anchors). The key is a per-
  installation random secret stored OUTSIDE any session directory, directly under the
  checkpoint base dir (:data:`INTEGRITY_KEY_FILENAME`). Load verifies the seal BEFORE any
  parse-level acceptance and refuses a missing, malformed, or mismatching seal
  (``CheckpointValidationError``) — so out-of-band edits of the checkpoint file alone
  (e.g. zeroing budget counters to refill the session budget on resume, spec section 8)
  are fail-closed. THREAT MODEL (honest scope): this defends against tampering of the
  checkpoint FILE alone; an attacker who can also read/replace the integrity key file
  (same-user/full-disk access) can re-seal forged state and is OUT OF SCOPE — the OS
  user boundary, not this mechanism, is the control for that adversary. Sound
  deterministic cross-checks (budget counters vs the checkpoint's own ceilings) remain
  as defense in depth.
- **Bounded**: the serialized checkpoint is capped (:data:`MAX_CHECKPOINT_BYTES`) and
  every persisted collection is bounded by the capped components it snapshots (at most
  50 subtasks x 20 results each with screenshots stripped, recent history capped).
- **No secrets**: checkpoints store no credentials; identity fields carry session ids only.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .limits import Limits
from .models import MAX_SUBTASKS, SUBTASK_RESULTS_CAP, Subtask, TerminationReason
from .plan_validator import contains_control_characters, find_cycle
from .redaction import contains_secret, redact_text
from .state import TaskStatus

__all__ = [
    "CHECKPOINT_EVERY_SECONDS",
    "CHECKPOINT_EVERY_STEPS",
    "CHECKPOINT_FILENAME",
    "CHECKPOINT_SCHEMA_VERSION",
    "ENV_VAR_CHECKPOINT_DIR",
    "HISTORY_ENTRY_MAX_CHARS",
    "INTEGRITY_KEY_FILENAME",
    "LIFECYCLE_TRIGGERS",
    "MAX_CHECKPOINT_BYTES",
    "RECENT_HISTORY_CAP",
    "SEAL_ALGORITHM",
    "CheckpointError",
    "CheckpointManager",
    "CheckpointPayload",
    "CheckpointRedactionError",
    "CheckpointTrigger",
    "CheckpointValidationError",
    "CheckpointWriteError",
    "EnvironmentExpectations",
    "IntegritySeal",
    "SessionSnapshot",
    "TerminationState",
    "should_checkpoint",
]

#: Schema version of the checkpoint payload; bump on any breaking layout change.
CHECKPOINT_SCHEMA_VERSION = 1
#: Periodic cadence: checkpoint every 50 steps ... (SubtasksProtocol section 7).
CHECKPOINT_EVERY_STEPS = 50
#: ... OR every 30 minutes — whichever comes first (SubtasksProtocol section 7).
CHECKPOINT_EVERY_SECONDS = 1800.0
#: Canonical checkpoint file name inside each per-session directory. The payload carries
#: the schema version and UTC timestamp; the file itself is atomically replaced, so the
#: newest valid checkpoint is always exactly this path (bounded disk usage per session).
CHECKPOINT_FILENAME = "checkpoint.json"
_TEMP_PREFIX = ".checkpoint-"
#: Hard cap on the serialized checkpoint size (bounded growth, spec section 19). Generous
#: against the worst legal state (50 subtasks x 20 bounded results each) yet bounded.
MAX_CHECKPOINT_BYTES = 8 * 1024 * 1024
#: Hard cap on persisted recent-history entries (most recent kept).
RECENT_HISTORY_CAP = 50
#: Hard cap per persisted history entry (matches ContextManager's default entry bound).
HISTORY_ENTRY_MAX_CHARS = 1000
#: Base-dir override env var (naming follows ``COMPUTER_USE_MCP_LOG_DIR``).
ENV_VAR_CHECKPOINT_DIR = "COMPUTER_USE_MCP_CHECKPOINT_DIR"
#: Per-installation integrity key file, stored directly under the checkpoint base dir
#: (NEVER inside a session directory, NEVER inside a checkpoint payload, never logged).
#: Created on first write with restrictive semantics (0600 where the OS honors it); a
#: missing key at LOAD time is a fail-closed refusal, never a silent re-key.
INTEGRITY_KEY_FILENAME = ".integrity_key"
#: Seal algorithm identifier persisted inside every checkpoint payload.
SEAL_ALGORITHM = "HMAC-SHA256"
#: The tamper-sensitive payload fields covered by the integrity seal. ``budget`` is
#: covered EXCEPT its ``snapshot_version`` format tag (own restore-layer gate; keeping it
#: outside the seal preserves the layered typed-error contract for that field).
_SEALED_FIELDS = (
    "session_id",
    "continuation_of",
    "goal",
    "current_subtask_id",
    "budget",
    "limits",
    "subtasks",
)
_SEALED_BUDGET_EXCLUDED_KEYS = ("snapshot_version",)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_ID_MAX_LENGTH = 128
_GOAL_MAX_LENGTH = 2_000
_ALLOWLIST_ENTRY_MAX_LENGTH = 300
_ALLOWLIST_MAX_ENTRIES = 64

_BUDGET_KEYS = ("snapshot_version", "elapsed_seconds", "actions", "model_calls", "steps", "subtasks")
_BUDGET_COUNTER_KEYS = ("elapsed_seconds", "actions", "model_calls", "steps", "subtasks")
_LIMITS_FIELD_NAMES = tuple(field.name for field in dataclasses.fields(Limits))
_CONTEXT_REQUIRED_KEYS = (
    "snapshot_version",
    "goal",
    "current_task",
    "app_window_state",
    "summary",
    "recent_history",
    "plan_notes",
    "categories",
    "steps",
    "steps_at_last_summary",
)


class CheckpointTrigger(StrEnum):
    """Why a checkpoint was written (periodic cadence + spec section 7 lifecycle events)."""

    PERIODIC = "periodic"
    SUBTASK_COMPLETED = "subtask_completed"
    SUBTASK_FAILED = "subtask_failed"
    SUBTASK_TRANSITION = "subtask_transition"
    BEFORE_SESSION_END = "before_session_end"
    BEFORE_RESUME = "before_resume"
    MANUAL = "manual"


#: Lifecycle triggers the orchestrator MUST checkpoint on (SubtasksProtocol section 7),
#: in addition to the periodic 50-steps/30-minutes cadence.
LIFECYCLE_TRIGGERS: tuple[CheckpointTrigger, ...] = (
    CheckpointTrigger.SUBTASK_COMPLETED,
    CheckpointTrigger.SUBTASK_FAILED,
    CheckpointTrigger.SUBTASK_TRANSITION,
    CheckpointTrigger.BEFORE_SESSION_END,
    CheckpointTrigger.BEFORE_RESUME,
)

_TRIGGER_VALUES = frozenset(item.value for item in CheckpointTrigger)
_TASK_STATUS_VALUES = frozenset(item.value for item in TaskStatus)
_TERMINATION_VALUES = frozenset(item.value for item in TerminationReason)


class CheckpointError(RuntimeError):
    """Base class for typed, fail-closed checkpoint failures."""


class CheckpointValidationError(CheckpointError):
    """A checkpoint file could not be loaded/validated (corrupt, wrong version, invalid).

    The file on disk is NEVER deleted, repaired, or partially loaded.
    """


class CheckpointWriteError(CheckpointError):
    """Writing a checkpoint failed; the previous checkpoint on disk remains intact."""


class CheckpointRedactionError(CheckpointError):
    """Secret-like content survived redaction; the write is refused (fail closed)."""


def should_checkpoint(
    steps_since_last: int,
    seconds_since_last: float,
    *,
    every_steps: int = CHECKPOINT_EVERY_STEPS,
    every_seconds: float = CHECKPOINT_EVERY_SECONDS,
) -> bool:
    """Pure, deterministic periodic cadence: 50 steps OR 30 minutes, whichever first.

    The caller (orchestrator) tracks ``steps_since_last`` and ``seconds_since_last``
    (inject a monotonic clock in tests). Negative or non-numeric inputs are rejected
    (fail closed) rather than silently treated as "no checkpoint due".
    """
    if isinstance(steps_since_last, bool) or not isinstance(steps_since_last, int):
        raise TypeError("steps_since_last must be an int")
    if steps_since_last < 0:
        raise ValueError("steps_since_last must be non-negative")
    if isinstance(seconds_since_last, bool) or not isinstance(seconds_since_last, int | float):
        raise TypeError("seconds_since_last must be a number")
    if seconds_since_last < 0:
        raise ValueError("seconds_since_last must be non-negative")
    return steps_since_last >= every_steps or seconds_since_last >= every_seconds


def _valid_identity(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= _ID_MAX_LENGTH
        and not contains_control_characters(value)
    )


def _numeric(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and value >= 0


def _utc_now() -> datetime:
    return datetime.now(UTC)


# --- checkpoint payload models -------------------------------------------------------------


class TerminationState(BaseModel):
    """Termination state at checkpoint time (SubtasksProtocol section 7)."""

    model_config = ConfigDict(extra="forbid")

    terminated: bool = False
    reason: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _coherent(self) -> TerminationState:
        if self.terminated != (self.reason is not None):
            raise ValueError("termination state requires reason if and only if terminated")
        if self.reason is not None and self.reason not in _TERMINATION_VALUES:
            raise ValueError(f"unknown termination reason: {self.reason!r}")
        return self


class SessionSnapshot(BaseModel):
    """Persistable projection of the main session state (no session id — that is top-level).

    Counters do NOT live here: they belong to the budget snapshot so resume can restore
    them without any reset. ``allowed_*`` allowlists live in
    :class:`EnvironmentExpectations` (single source for the safety re-verification).
    """

    model_config = ConfigDict(extra="forbid")

    status: str = TaskStatus.IDLE.value
    dry_run: bool = True
    require_approval: bool = True
    max_steps: int = Field(default=30, ge=1, le=500)
    max_retries_per_action: int = Field(default=1, ge=0, le=5)
    min_confidence: float = Field(default=0.70, ge=0.0, le=1.0)
    stopped: bool = False

    @model_validator(mode="after")
    def _known_status(self) -> SessionSnapshot:
        if self.status not in _TASK_STATUS_VALUES:
            raise ValueError(f"unknown task status: {self.status!r}")
        return self


class EnvironmentExpectations(BaseModel):
    """Recorded safety environment at checkpoint time.

    The resume bundle carries this verbatim so the caller can re-verify the CURRENT
    environment (foreground process/window, allowlists) and refuse to continue on
    mismatch — stale-state validation is enforced, never assumed (spec sections 8/14).
    """

    model_config = ConfigDict(extra="forbid")

    active_process_name: str | None = Field(default=None, max_length=_ALLOWLIST_ENTRY_MAX_LENGTH)
    active_window_title: str | None = Field(default=None, max_length=_ALLOWLIST_ENTRY_MAX_LENGTH)
    app_window_state: str = Field(default="", max_length=_ALLOWLIST_ENTRY_MAX_LENGTH)
    allowed_processes: list[str] = Field(
        default_factory=list, max_length=_ALLOWLIST_MAX_ENTRIES
    )
    allowed_windows: list[str] = Field(default_factory=list, max_length=_ALLOWLIST_MAX_ENTRIES)


class IntegritySeal(BaseModel):
    """HMAC-SHA256 integrity seal over the tamper-sensitive checkpoint fields.

    ``digest`` is the lowercase hex HMAC (64 chars) of a canonical JSON serialization of
    :data:`_SEALED_FIELDS` (see :func:`_canonical_seal_input`), keyed by the per-
    installation secret (:data:`INTEGRITY_KEY_FILENAME` under the checkpoint base dir).
    The seal makes out-of-band edits of the checkpoint file detectable: any change to a
    covered field invalidates it, and load refuses such a file fail-closed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm: str
    digest: str

    @model_validator(mode="after")
    def _known_seal(self) -> IntegritySeal:
        if self.algorithm != SEAL_ALGORITHM:
            raise ValueError(f"unknown integrity seal algorithm: {self.algorithm!r}")
        if not _DIGEST_RE.fullmatch(self.digest):
            raise ValueError("integrity seal digest must be 64 lowercase hex characters")
        return self


class CheckpointPayload(BaseModel):
    """Versioned checkpoint payload (SubtasksProtocol section 7 field set).

    ``subtasks`` is the verbatim :meth:`SubtaskManager.snapshot` mapping — it IS the
    dependency graph/state (each entry carries ``depends_on`` and ``status``) plus the
    bounded necessary results (at most ``SUBTASK_RESULTS_CAP`` per subtask, heavy
    screenshot payloads stripped by the recorder). ``budget`` is the verbatim
    :meth:`SessionBudgetTracker.snapshot`; ``limits`` the exact :class:`Limits` in force;
    ``context`` the verbatim :meth:`ContextManager.snapshot` (summary + bounded recent
    window). All of them are re-validated fail-closed on load.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    created_at: datetime
    session_id: str = Field(min_length=1, max_length=_ID_MAX_LENGTH)
    #: Original session id when THIS session is itself a resume (continuation chain).
    continuation_of: str | None = Field(default=None, min_length=1, max_length=_ID_MAX_LENGTH)
    goal: str = Field(min_length=1, max_length=_GOAL_MAX_LENGTH)
    trigger: str = CheckpointTrigger.MANUAL.value
    session: SessionSnapshot = Field(default_factory=SessionSnapshot)
    environment: EnvironmentExpectations = Field(default_factory=EnvironmentExpectations)
    termination: TerminationState = Field(default_factory=TerminationState)
    current_subtask_id: str | None = Field(default=None, min_length=1, max_length=_ID_MAX_LENGTH)
    subtasks: dict[str, Any]
    budget: dict[str, Any]
    limits: dict[str, Any]
    context: dict[str, Any]
    recent_history: list[str] = Field(default_factory=list, max_length=RECENT_HISTORY_CAP)
    #: Tamper-evidence seal; ALWAYS present on load (manager.load refuses payloads
    #: without one). ``None`` only for freshly constructed payloads before serialization.
    integrity: IntegritySeal | None = None

    @property
    def limits_resolved(self) -> Limits:
        """The checkpoint's own limits, re-clamped by the current mechanism."""
        return Limits(**self.limits).validate()

    @property
    def dependency_edges(self) -> dict[str, list[str]]:
        """Dependency graph derived from the persisted subtask snapshots (stable order)."""
        return {
            dump["subtask_id"]: list(dump.get("depends_on") or [])
            for dump in self.subtasks["subtasks"]
        }

    @model_validator(mode="after")
    def _validate_integrity(self) -> CheckpointPayload:
        """Fail-closed self-consistency checks; any defect raises ``ValueError``/``TypeError``.

        On the load path :meth:`CheckpointManager.load` wraps these into
        :class:`CheckpointValidationError` — a corrupt checkpoint is never partially
        loaded.
        """
        if self.schema_version != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported checkpoint schema_version {self.schema_version!r}; "
                f"expected {CHECKPOINT_SCHEMA_VERSION}"
            )
        if not _valid_identity(self.session_id):
            raise ValueError("session_id must be a clean identity string (1..128 chars)")
        if self.continuation_of is not None and not _valid_identity(self.continuation_of):
            raise ValueError("continuation_of must be a clean identity string or null")
        if (
            not isinstance(self.goal, str)
            or not self.goal.strip()
            or contains_control_characters(self.goal)
        ):
            raise ValueError("goal must be a non-empty string without control characters")
        if self.trigger not in _TRIGGER_VALUES:
            raise ValueError(f"unknown checkpoint trigger: {self.trigger!r}")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        self._validate_subtasks()
        self._validate_budget()
        self._validate_limits()
        # Cross-check (needs canonical limits): the subtask count must respect the
        # checkpoint's own subtask ceiling — a state that violates its own limits is
        # corrupt and must be refused before anything is restored.
        if len(self.subtasks["subtasks"]) > self.limits_resolved.max_subtasks:
            raise ValueError(
                f"subtasks snapshot holds {len(self.subtasks['subtasks'])} entries but the "
                f"checkpointed limits allow at most {self.limits_resolved.max_subtasks}"
            )
        self._validate_context()
        self._validate_recent_history()
        self._validate_current_subtask()
        return self

    def _validate_subtasks(self) -> None:
        if set(self.subtasks) != {"subtasks"} or not isinstance(self.subtasks["subtasks"], list):
            raise ValueError("subtasks must be the SubtaskManager.snapshot mapping")
        entries = self.subtasks["subtasks"]
        if len(entries) > MAX_SUBTASKS:
            raise ValueError(f"subtasks snapshot holds {len(entries)} entries; max {MAX_SUBTASKS}")
        seen_subtasks: dict[str, Subtask] = {}
        for index, item in enumerate(entries):
            if not isinstance(item, dict):
                raise TypeError(f"subtask snapshot entry {index} is not a mapping")
            results = item.get("results") or []
            if len(results) > SUBTASK_RESULTS_CAP:
                raise ValueError(
                    f"subtask snapshot entry {index} holds {len(results)} results; "
                    f"max {SUBTASK_RESULTS_CAP}"
                )
            for position, result in enumerate(results):
                if (
                    not isinstance(result, dict)
                    or result.get("screenshot_after_base64") is not None
                ):
                    raise ValueError(
                        f"subtask snapshot entry {index} result {position} must carry no "
                        "screenshot payload (heavy fields are stripped before storage)"
                    )
            try:
                subtask = Subtask.model_validate(item)
            except ValidationError as exc:
                raise ValueError(f"subtask snapshot entry {index} is invalid: {exc}") from exc
            if subtask.subtask_id in seen_subtasks:
                raise ValueError(
                    f"subtask snapshot holds duplicate subtask id {subtask.subtask_id!r}"
                )
            seen_subtasks[subtask.subtask_id] = subtask
        # Dependency-graph self-consistency: no self-dependency, no dangling edge, no cycle
        # (SubtaskManager.restore re-checks all of this — defense in depth).
        for subtask in seen_subtasks.values():
            for dep in subtask.depends_on:
                if dep == subtask.subtask_id:
                    raise ValueError(f"subtask {subtask.subtask_id!r} depends on itself")
                if dep not in seen_subtasks:
                    raise ValueError(
                        f"subtask {subtask.subtask_id!r} depends on unknown subtask {dep!r}"
                    )
        cycle = find_cycle({sid: set(s.depends_on) for sid, s in seen_subtasks.items()})
        if cycle is not None:
            raise ValueError("subtask snapshot dependency cycle: " + " -> ".join(cycle))

    def _subtask_ids(self) -> list[str]:
        return [dump["subtask_id"] for dump in self.subtasks["subtasks"]]

    def _validate_budget(self) -> None:
        if set(self.budget) != set(_BUDGET_KEYS):
            raise ValueError(
                "budget must be the SessionBudgetTracker.snapshot mapping with keys "
                f"{sorted(_BUDGET_KEYS)}"
            )
        if isinstance(self.budget["snapshot_version"], bool) or not isinstance(
            self.budget["snapshot_version"], int
        ):
            raise TypeError("budget snapshot_version must be an int")
        for key in _BUDGET_COUNTER_KEYS:
            if not _numeric(self.budget[key]):
                raise ValueError(f"budget counter {key!r} must be a non-negative number")

    def _validate_limits(self) -> None:
        if set(self.limits) != set(_LIMITS_FIELD_NAMES):
            raise ValueError(
                "limits must carry exactly the current Limits fields: "
                f"{sorted(_LIMITS_FIELD_NAMES)}"
            )
        for key, value in self.limits.items():
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(f"limit {key!r} must be a number")
        try:
            canonical = Limits(**self.limits).validate()
        except TypeError as exc:
            raise ValueError(f"limits are not constructible: {exc}") from exc
        if dataclasses.asdict(canonical) != self.limits:
            raise ValueError(
                "limits are not canonical (must equal the clamping mechanism's output); "
                "refusing a checkpoint that could enlarge the budget on resume"
            )

    def _validate_context(self) -> None:
        missing = [key for key in _CONTEXT_REQUIRED_KEYS if key not in self.context]
        if missing:
            raise ValueError(f"context snapshot missing fields: {missing}")
        version = self.context["snapshot_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise TypeError("context snapshot_version must be an int")
        for key in ("steps", "steps_at_last_summary"):
            value = self.context[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"context {key!r} must be a non-negative int")
        if self.context["summary"] is not None and not isinstance(self.context["summary"], dict):
            raise ValueError("context summary must be a mapping or null")
        for key in ("recent_history", "plan_notes"):
            if not isinstance(self.context[key], list) or not all(
                isinstance(item, str) for item in self.context[key]
            ):
                raise ValueError(f"context {key!r} must be a list of strings")
        if not isinstance(self.context["categories"], dict):
            raise TypeError("context categories must be a mapping")

    def _validate_recent_history(self) -> None:
        for index, entry in enumerate(self.recent_history):
            if not isinstance(entry, str) or len(entry) > HISTORY_ENTRY_MAX_CHARS:
                raise ValueError(
                    f"recent_history entry {index} must be a string of at most "
                    f"{HISTORY_ENTRY_MAX_CHARS} characters"
                )

    def _validate_current_subtask(self) -> None:
        if self.current_subtask_id is None:
            return
        if self.current_subtask_id not in self._subtask_ids():
            raise ValueError(
                f"current_subtask_id {self.current_subtask_id!r} is not present in the "
                "subtask snapshot (self-consistency failure)"
            )


def _canonical_seal_input(data: dict[str, Any]) -> bytes:
    """Canonical bytes sealed by the integrity HMAC (deterministic across runs).

    Built from the RAW on-disk dict (post-redaction at write time, as-parsed at load
    time) so the writer's and reader's seal inputs are byte-identical for an untouched
    file. A covered field that is MISSING changes the canonical form, so field deletion
    is detected too. ``budget.snapshot_version`` is excluded (format tag with its own
    restore-layer gate — see :data:`_SEALED_BUDGET_EXCLUDED_KEYS`).
    """
    sealed: dict[str, Any] = {}
    for field in _SEALED_FIELDS:
        value = data.get(field)
        if field == "budget" and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k not in _SEALED_BUDGET_EXCLUDED_KEYS}
        sealed[field] = value
    return json.dumps(sealed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _compute_seal_digest(data: dict[str, Any], key: bytes) -> str:
    """HMAC-SHA256 hex digest over :func:`_canonical_seal_input` (never logged)."""
    return hmac.new(key, _canonical_seal_input(data), hashlib.sha256).hexdigest()


def _verify_budget_ceiling_cross_checks(payload: CheckpointPayload) -> None:
    """Deterministic, conservative cross-checks: counters vs the checkpoint's own ceilings.

    Defense in depth behind the integrity seal — these catch inflated-counter forgeries
    even for an attacker who could re-seal. Proof obligations (each bound is implied by
    the runtime's own fail-closed gates, so legitimate checkpoints always pass):

    - ``budget.subtasks <= limits.max_subtasks``: every subtask start is gated by
      ``SessionBudgetTracker.check_subtasks()`` (refuses when the counter is already at
      the ceiling) immediately before ``record_subtask()`` (``long_running``), so the
      counter can never exceed the checkpoint's own subtask ceiling.
    - ``budget.steps <= limits.max_session_steps``: each subtask run's step delta is
      bounded by ``_capped_run_state`` to the REMAINING session step budget, so
      cumulative steps can never exceed the checkpointed ``max_session_steps``.

    Deliberately ABSENT (would reject legitimate checkpoints): lower bounds of
    ``actions``/``steps``/``model_calls`` from subtask states. A provider "done"
    decision completes a subtask and records a result while consuming one model call
    but ZERO steps and ZERO actions, so e.g. ``steps >= completed_count`` is not
    implied by the payload. Zeroed-counter forgeries are caught by the integrity SEAL.
    """
    limits = payload.limits_resolved
    budget = payload.budget
    subtasks_used = float(budget["subtasks"])
    if subtasks_used > limits.max_subtasks:
        raise ValueError(
            f"budget counter 'subtasks' ({budget['subtasks']}) exceeds the checkpoint's "
            f"own max_subtasks ({limits.max_subtasks}); counters inconsistent with the "
            "persisted limits"
        )
    steps_used = float(budget["steps"])
    if steps_used > limits.max_session_steps:
        raise ValueError(
            f"budget counter 'steps' ({budget['steps']}) exceeds the checkpoint's own "
            f"max_session_steps ({limits.max_session_steps}); counters inconsistent with "
            "the persisted limits"
        )


def _redact_and_gate(node: Any, path: str = "$") -> tuple[Any, list[str]]:
    """Recursively redact every string value; return paths whose redaction still trips."""
    if isinstance(node, str):
        redacted, _count = redact_text(node)
        if contains_secret(redacted):
            return redacted, [path]
        return redacted, []
    if isinstance(node, dict):
        data: dict[str, Any] = {}
        offenders: list[str] = []
        for key, value in node.items():
            data[key], sub_offenders = _redact_and_gate(value, f"{path}.{key}")
            offenders.extend(sub_offenders)
        return data, offenders
    if isinstance(node, list | tuple):
        items: list[Any] = []
        offenders = []
        for index, item in enumerate(node):
            redacted_item, sub_offenders = _redact_and_gate(item, f"{path}[{index}]")
            items.append(redacted_item)
            offenders.extend(sub_offenders)
        return (tuple(items) if isinstance(node, tuple) else items), offenders
    return node, []


def _safe_error_text(exc: BaseException) -> str:
    """Redacted, bounded exception text — error paths must never leak secret content."""
    return redact_text(str(exc))[0][:2_000]


class CheckpointManager:
    """Owns one base directory of per-session checkpoint files (atomic, redacted).

    Layout: ``<base>/<sanitized session_id>/checkpoint.json``. ``base`` defaults to the
    ``COMPUTER_USE_MCP_CHECKPOINT_DIR`` env var, else ``<temp>/computer-use-mcp/checkpoints``
    (alongside the audit-log convention). Writes are serialized through an RLock; readers
    only ever see fully-written files (temp + ``os.replace``).
    """

    def __init__(self, base_dir: str | Path | None = None, *, max_checkpoint_bytes: int = MAX_CHECKPOINT_BYTES) -> None:
        if base_dir is None:
            override = os.getenv(ENV_VAR_CHECKPOINT_DIR)
            base_dir = (
                Path(override)
                if override
                else Path(tempfile.gettempdir()) / "computer-use-mcp" / "checkpoints"
            )
        self.base_dir = Path(base_dir)
        if isinstance(max_checkpoint_bytes, bool) or not isinstance(max_checkpoint_bytes, int):
            raise TypeError("max_checkpoint_bytes must be an int")
        if max_checkpoint_bytes < 1:
            raise ValueError("max_checkpoint_bytes must be positive")
        self._max_checkpoint_bytes = max_checkpoint_bytes
        self._lock = threading.RLock()

    # -- paths --------------------------------------------------------------------------------

    def session_dir(self, session_id: str) -> Path:
        """Per-session checkpoint directory (identity sanitized like the audit sink)."""
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id))[:100] or "unknown"
        return self.base_dir / safe

    def checkpoint_path(self, session_id: str) -> Path:
        """Canonical (newest valid) checkpoint file for ``session_id``."""
        return self.session_dir(session_id) / CHECKPOINT_FILENAME

    def has_checkpoint(self, session_id: str) -> bool:
        return self.checkpoint_path(session_id).exists()

    # -- integrity key (per installation; never logged, never stored in payloads) --------------

    @property
    def integrity_key_path(self) -> Path:
        """Location of the per-installation seal key (directly under the base dir)."""
        return self.base_dir / INTEGRITY_KEY_FILENAME

    def _ensure_integrity_key(self) -> bytes:
        """Read or create the per-installation seal key (write path).

        Creation is exclusive (``O_CREAT|O_EXCL``, mode 0600 where the OS honors it);
        a concurrent creator's file is simply read. Any filesystem failure raises
        :class:`CheckpointWriteError` (fail closed — nothing is sealed with a fallback
        or empty key). On Windows the POSIX mode is not enforced; protection relies on
        the OS user profile ACLs (documented threat model).
        """
        path = self.integrity_key_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            return path.read_bytes()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise CheckpointWriteError(
                f"checkpoint integrity key unreadable: {_safe_error_text(exc)}"
            ) from exc
        key = secrets.token_hex(32).encode("ascii")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(key)
            with contextlib.suppress(OSError):
                path.chmod(0o600)  # best-effort where the OS honors POSIX modes
            return key
        except FileExistsError:
            try:
                return path.read_bytes()
            except OSError as exc:
                raise CheckpointWriteError(
                    f"checkpoint integrity key unreadable: {_safe_error_text(exc)}"
                ) from exc
        except OSError as exc:
            raise CheckpointWriteError(
                f"checkpoint integrity key creation failed: {_safe_error_text(exc)}"
            ) from exc

    def _require_integrity_key(self) -> bytes:
        """Read the existing seal key (load path); a missing key is a fail-closed refusal.

        The key is deliberately NOT re-created here: a checkpoint that needs verifying
        while its installation key is gone can no longer be authenticated, so refusing
        (and keeping the file intact) is the only safe behavior.
        """
        try:
            return self.integrity_key_path.read_bytes()
        except (FileNotFoundError, OSError) as exc:
            raise CheckpointValidationError(
                "checkpoint integrity key is unavailable; the checkpoint cannot be "
                "authenticated and is refused (fail-closed): "
                f"{_safe_error_text(exc)}"
            ) from exc

    # -- capture --------------------------------------------------------------------------------

    def capture_payload(
        self,
        *,
        session_id: str,
        goal: str,
        subtasks_snapshot: dict[str, Any],
        budget_snapshot: dict[str, Any],
        limits: Limits,
        context_snapshot: dict[str, Any],
        session: SessionSnapshot | dict[str, Any] | None = None,
        environment: EnvironmentExpectations | dict[str, Any] | None = None,
        current_subtask_id: str | None = None,
        recent_history: list[str] | None = None,
        termination: TerminationState | dict[str, Any] | None = None,
        trigger: str | CheckpointTrigger = CheckpointTrigger.MANUAL,
        continuation_of: str | None = None,
        created_at: datetime | None = None,
    ) -> CheckpointPayload:
        """Build a validated payload from component snapshots (fail-closed on any defect).

        Pass the verbatim ``snapshot()`` output of :class:`SubtaskManager`,
        :class:`SessionBudgetTracker`, and :class:`ContextManager`, plus the validated
        :class:`Limits` in force. ``recent_history`` defaults to the context snapshot's
        bounded recent window.
        """
        if recent_history is None:
            recent_history = [str(item) for item in (context_snapshot.get("recent_history") or [])]
        recent_history = list(recent_history)[-RECENT_HISTORY_CAP:]
        recent_history = [entry[:HISTORY_ENTRY_MAX_CHARS] for entry in recent_history]
        # Normalize the elapsed-time counter to millisecond precision: the persisted text
        # must never contain long raw-float digit runs (13-19 digits), which downstream
        # whole-text secret scanners can false-positive on (Luhn credit-card pattern).
        # Sub-millisecond budget granularity is meaningless; restore re-bases the
        # monotonic anchor from this value, so 3 decimals is semantics-preserving.
        budget_snapshot = dict(budget_snapshot)
        elapsed = budget_snapshot.get("elapsed_seconds")
        if isinstance(elapsed, int | float) and not isinstance(elapsed, bool):
            budget_snapshot["elapsed_seconds"] = round(float(elapsed), 3)
        if not isinstance(session, SessionSnapshot):
            session = SessionSnapshot.model_validate(session or {})
        if not isinstance(environment, EnvironmentExpectations):
            environment = EnvironmentExpectations.model_validate(environment or {})
        if not isinstance(termination, TerminationState):
            termination = TerminationState.model_validate(termination or {})
        if dataclasses.asdict(limits) != dataclasses.asdict(limits.validate()):
            raise ValueError(
                "limits must already be canonical (Limits.validate() output); refusing to "
                "checkpoint limits the clamping mechanism would change"
            )
        return CheckpointPayload(
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            created_at=created_at if created_at is not None else _utc_now(),
            session_id=session_id,
            continuation_of=continuation_of,
            goal=goal,
            trigger=trigger.value if isinstance(trigger, CheckpointTrigger) else str(trigger),
            session=session,
            environment=environment,
            termination=termination,
            current_subtask_id=current_subtask_id,
            subtasks=subtasks_snapshot,
            budget=budget_snapshot,
            limits=dataclasses.asdict(limits),
            context=context_snapshot,
            recent_history=recent_history,
        )

    # -- atomic write -------------------------------------------------------------------------

    def write_checkpoint(self, **kwargs: Any) -> Path:
        """Capture (see :meth:`capture_payload`) then atomically write; returns the path."""
        return self.write(self.capture_payload(**kwargs))

    def write(self, payload: CheckpointPayload) -> Path:
        """Serialize (redacted) and atomically replace the session's checkpoint file.

        Order of failure: redaction refusal and size cap happen BEFORE any filesystem
        mutation; the temp file lives in the destination directory and is moved into
        place with ``os.replace``; any failure removes the temp file and leaves the
        previous valid checkpoint untouched.
        """
        text = self._serialize(payload)
        destination = self.checkpoint_path(payload.session_id)
        with self._lock:
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                fd, temp_name = tempfile.mkstemp(
                    prefix=_TEMP_PREFIX, suffix=".tmp", dir=destination.parent
                )
            except OSError as exc:
                raise CheckpointWriteError(
                    f"checkpoint directory preparation failed: {_safe_error_text(exc)}"
                ) from exc
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, destination)
            except BaseException as exc:
                with contextlib.suppress(OSError):
                    temp_path.unlink(missing_ok=True)
                raise CheckpointWriteError(
                    f"atomic checkpoint write failed (previous checkpoint left intact): "
                    f"{_safe_error_text(exc)}"
                ) from exc
        return destination

    def _serialize(self, payload: CheckpointPayload) -> str:
        """Redacted, SEALED JSON text of ``payload`` (fail-closed gates before any disk touch).

        Order: redact every string value -> gate -> compute the HMAC-SHA256 integrity
        seal over the canonical tamper-sensitive fields of the REDACTED data (the seal
        covers exactly what reaches the disk) -> serialize -> size cap.
        """
        data = payload.model_dump(mode="json")
        data, offenders = _redact_and_gate(data)
        if offenders:
            raise CheckpointRedactionError(
                "checkpoint payload still contains secret-like content after redaction at "
                f"{offenders}; refusing to write"
            )
        key = self._ensure_integrity_key()
        data["integrity"] = {
            "algorithm": SEAL_ALGORITHM,
            "digest": _compute_seal_digest(data, key),
        }
        text = json.dumps(data, ensure_ascii=False, sort_keys=True)
        if len(text.encode("utf-8")) > self._max_checkpoint_bytes:
            raise CheckpointWriteError(
                f"serialized checkpoint exceeds the {self._max_checkpoint_bytes} byte cap; "
                "refusing to persist unbounded state"
            )
        return text

    # -- load + validate (fail-closed) ----------------------------------------------------------

    def load(self, path: str | Path) -> CheckpointPayload:
        """Load and fully validate a checkpoint file; typed rejection on ANY defect.

        Unknown/newer schema versions, malformed JSON, structural/type violations,
        missing/malformed/MISMATCHING integrity seals, counter-ceiling cross-check
        failures, and any other self-consistency failure raise
        :class:`CheckpointValidationError`. The file is never deleted, modified, or
        partially loaded. The seal check runs BEFORE payload parsing so that tampering
        with any sealed field (budget counters, limits, subtask states, identity) is
        refused outright — a zeroed-counters file can never be accepted and can never
        refill a session budget on resume (spec section 8).
        """
        file_path = Path(path)
        try:
            size = file_path.stat().st_size
        except OSError as exc:
            raise CheckpointValidationError(
                f"checkpoint not readable: {_safe_error_text(exc)}"
            ) from exc
        if size > self._max_checkpoint_bytes:
            raise CheckpointValidationError(
                f"checkpoint file exceeds the {self._max_checkpoint_bytes} byte cap"
            )
        try:
            raw = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CheckpointValidationError(
                f"checkpoint not readable: {_safe_error_text(exc)}"
            ) from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CheckpointValidationError(
                f"checkpoint is not valid JSON (truncated or corrupt): {_safe_error_text(exc)}"
            ) from exc
        if not isinstance(data, dict):
            raise CheckpointValidationError("checkpoint must be a JSON object")
        version = data.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int) or version != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointValidationError(
                f"unsupported checkpoint schema_version {version!r}; expected "
                f"{CHECKPOINT_SCHEMA_VERSION} (unknown/newer versions are rejected)"
            )
        self._verify_integrity_seal(data)
        try:
            payload = CheckpointPayload.model_validate(data)
            _verify_budget_ceiling_cross_checks(payload)
        except (ValidationError, TypeError, ValueError) as exc:
            # pydantic wraps ValueError/TypeError from after-validators inconsistently
            # across code paths, so the raw types are caught here as well: a corrupt
            # checkpoint must ALWAYS surface as the typed validation error.
            raise CheckpointValidationError(
                f"checkpoint failed integrity validation: {_safe_error_text(exc)}"
            ) from exc
        return payload

    def _verify_integrity_seal(self, data: dict[str, Any]) -> None:
        """Verify the HMAC seal over the raw on-disk fields BEFORE any model parsing.

        Fail-closed on: missing seal, malformed seal, unavailable key, or digest
        mismatch (constant-time comparison). The checkpoint file itself is never
        touched by this method.
        """
        seal = data.get("integrity")
        if not isinstance(seal, dict):
            raise CheckpointValidationError(
                "checkpoint has no integrity seal; refusing an unauthenticated payload "
                "(fail-closed)"
            )
        try:
            parsed = IntegritySeal.model_validate(seal)
        except ValidationError as exc:
            raise CheckpointValidationError(
                f"checkpoint integrity seal is malformed: {_safe_error_text(exc)}"
            ) from exc
        key = self._require_integrity_key()
        expected = _compute_seal_digest(data, key)
        if not hmac.compare_digest(parsed.digest.encode("ascii"), expected.encode("ascii")):
            raise CheckpointValidationError(
                "checkpoint integrity seal mismatch: the file was modified after it was "
                "written; refusing to load a tampered checkpoint (fail-closed)"
            )
