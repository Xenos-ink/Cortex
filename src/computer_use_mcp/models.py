"""Core domain models for the computer-use runtime.

Backward-compatibility contract (master-mission section 6): existing fields keep their
names, types, and constraints; Wave 1 additions are strictly additive with defaults so
the existing suite and later waves keep working unchanged.

Conventions fixed here (binding for later waves):
- ``MonitorInfo.bounds`` / ``WindowInfo.bounds`` are ``(left, top, width, height)`` in
  virtual-screen pixels (mss convention), so they may contain negative left/top values.
- ``TextRegion`` x/y are screenshot-local pixel coordinates (non-negative).
- ``Point`` allows negative coordinates for multi-monitor virtual screens but stays
  bounds-checked (``-8192..16384``) to reject nonsense values.
- ``VerificationResult.outcome`` is the authoritative result; ``verified`` is a derived,
  serialized property that equals ``outcome == "verified"``. ``uncertain`` is never success.
- ``GroundedAction.risk`` is filled only by the safety classifier, never by the model.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field, model_validator


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


class ActionType(StrEnum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    DRAG = "drag"
    TYPE = "type"
    KEYPRESS = "keypress"
    SCROLL = "scroll"
    WAIT = "wait"
    DONE = "done"
    MOVE = "move"
    HOTKEY = "hotkey"
    FOCUS_WINDOW = "focus_window"


class RiskLevel(StrEnum):
    """Contextual risk classification.

    Filled only by the safety classifier (``SafetyPolicy``), never by the model/provider.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class CoordinateSpace(StrEnum):
    """Integrity classification of the screenshot vs physical input coordinate spaces.

    ``VERIFIED_PASSTHROUGH``: screenshot coordinates equal physical input coordinates.
    ``SCALED``: a known, verified transform exists between the two spaces.
    ``UNVERIFIABLE``: integrity cannot be established; the executor must refuse actions.
    """

    VERIFIED_PASSTHROUGH = "verified_passthrough"
    SCALED = "scaled"
    UNVERIFIABLE = "unverifiable"


class FailureClass(StrEnum):
    """Recovery taxonomy pinned by master-mission section 6 (recovery.py imports these)."""

    STALE_COORDINATES = "stale_coordinates"
    MOVED_UI = "moved_ui"
    WRONG_WINDOW = "wrong_window"
    UNEXPECTED_DIALOG = "unexpected_dialog"
    BLOCKED_UI = "blocked_ui"
    NAVIGATION_DRIFT = "navigation_drift"
    APP_CRASH = "app_crash"
    AUTH_REQUIRED = "auth_required"
    ALREADY_COMPLETED = "already_completed"
    UNRECOVERABLE = "unrecoverable"
    LOW_CONFIDENCE = "low_confidence"
    UNKNOWN = "unknown"


class TerminationReason(StrEnum):
    """Why a task left the running state; audited alongside ``TaskState`` completion."""

    COMPLETED = "completed"
    FAILED_VERIFICATION = "failed_verification"
    BLOCKED_SAFETY = "blocked_safety"
    APPROVAL_EXHAUSTED = "approval_exhausted"
    LIMIT_EXCEEDED = "limit_exceeded"
    STOPPED_BY_USER = "stopped_by_user"
    UNRECOVERABLE = "unrecoverable"
    PROVIDER_ERROR = "provider_error"


class Point(BaseModel):
    """Screen coordinate.

    Bounds cover multi-monitor virtual screens (negative left/up monitors) while staying
    bounds-checked to reject nonsense values.
    """

    x: int = Field(ge=-8_192, le=16_384)
    y: int = Field(ge=-8_192, le=16_384)


class MonitorInfo(BaseModel):
    """Identity of the monitor an observation was captured from."""

    id: str
    index: int = Field(ge=0)
    bounds: tuple[int, int, int, int]
    is_primary: bool = False
    dpi_scale_x: float = Field(default=1.0, gt=0)
    dpi_scale_y: float = Field(default=1.0, gt=0)


class WindowInfo(BaseModel):
    """Strong window/process identity (title-only matching is demoted to a fallback)."""

    hwnd: int | None = None
    pid: int | None = None
    process_name: str | None = None
    exe_path: str | None = None
    window_class: str | None = None
    title: str = ""
    bounds: tuple[int, int, int, int] | None = None


class TextRegion(BaseModel):
    """A text region detected by OCR (P1); coordinates are screenshot-local."""

    text: str
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class GroundingResult(BaseModel):
    """How an action's coordinates/target were derived, and how confident that was."""

    strategy: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    normalized: bool = False
    notes: str | None = Field(default=None, max_length=1_000)


class GroundedAction(BaseModel):
    """A validated, executable action proposal.

    ``action_id`` is generated automatically so every action instance is uniquely
    auditable; ``source_observation_id`` binds the action to the observation its
    coordinates were derived from (staleness protection). ``drag`` actions carry both
    endpoints in screenshot space: ``point`` is the drag start and ``to_point`` the drag
    end (both required for drag, ignored for other action types). ``target`` is the
    focus_window channel — the window title to bring to the foreground (required for
    ``focus_window``, ``None`` for every other action type). It is a dedicated field,
    deliberately NOT ``text``: ``text`` is the redaction/secret-scan channel, while
    ``target`` is a window-identity selector audited and allowlist-gated separately.
    """

    action: ActionType
    point: Point | None = None
    to_point: Point | None = None
    text: str | None = Field(default=None, max_length=2_000)
    keys: list[str] = Field(default_factory=list, max_length=12)
    delta: int = Field(default=0, ge=-20, le=20)
    reason: str = Field(default="", max_length=500)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_observation_id: str | None = None
    expected_effect: str | None = Field(default=None, max_length=500)
    target: str | None = Field(default=None, max_length=200)
    action_id: str = Field(default_factory=_new_id)
    risk: RiskLevel | None = None
    grounding: GroundingResult | None = None

    @model_validator(mode="after")
    def _drag_requires_start_and_end(self) -> GroundedAction:
        """A drag needs both endpoints: ``point`` is the start, ``to_point`` the end.

        Exactly one endpoint (or neither) is a malformed drag and fails construction —
        fail closed so a half-specified press-move-release can never reach the backend.
        """
        if self.action is ActionType.DRAG and (self.point is None or self.to_point is None):
            raise ValueError("drag actions require both point (start) and to_point (end)")
        return self

    @model_validator(mode="after")
    def _move_requires_point(self) -> GroundedAction:
        """A move needs a target point (screenshot space) — fail closed otherwise."""
        if self.action is ActionType.MOVE and self.point is None:
            raise ValueError("move actions require a point (screenshot space)")
        return self

    @model_validator(mode="after")
    def _hotkey_requires_compound_keys(self) -> GroundedAction:
        """A hotkey is a compound chord: 2..12 non-empty key names, fail closed.

        Single-key presses deliberately stay on ``keypress``; an under/over-sized chord
        or a blank key name never reaches the backend.
        """
        if self.action is ActionType.HOTKEY:
            stripped = [key.strip() for key in self.keys]
            if not 2 <= len(stripped) <= 12 or any(not key for key in stripped):
                raise ValueError(
                    "hotkey actions require 2 to 12 non-empty key names "
                    "(single-key presses belong on keypress actions)"
                )
        return self

    @model_validator(mode="after")
    def _focus_window_requires_target(self) -> GroundedAction:
        """A focus_window needs a non-empty target window title — fail closed otherwise."""
        if self.action is ActionType.FOCUS_WINDOW and not (self.target or "").strip():
            raise ValueError("focus_window actions require a non-empty target window title")
        return self


class Observation(BaseModel):
    """A captured computer state with identity, timing, and coordinate-space metadata."""

    image_base64: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    active_window: str | None = None
    cursor_x: int | None = Field(default=None, ge=-8_192, le=16_384)
    cursor_y: int | None = Field(default=None, ge=-8_192, le=16_384)
    input_width: int | None = Field(default=None, gt=0)
    input_height: int | None = Field(default=None, gt=0)
    coordinate_scale_x: float = Field(default=1.0, gt=0)
    coordinate_scale_y: float = Field(default=1.0, gt=0)
    coordinate_space_verified: bool = True
    redactions_applied: bool = False
    observation_id: str = Field(default_factory=_new_id)
    timestamp: datetime = Field(default_factory=_utc_now)
    coordinate_space: CoordinateSpace = CoordinateSpace.VERIFIED_PASSTHROUGH
    monitor: MonitorInfo | None = None
    active_window_info: WindowInfo | None = None
    ocr_text: list[TextRegion] | None = None
    ui_elements: list[Any] | None = None

    @model_validator(mode="after")
    def _sync_coordinate_space(self) -> Observation:
        """Keep the legacy boolean and the richer enum mutually consistent.

        When ``coordinate_space`` is explicitly provided it wins and the legacy boolean is
        re-derived; otherwise the boolean (plus scale factors) classifies the enum. When
        both are provided the enum is authoritative. An explicitly-provided legacy boolean
        is never overwritten unless the enum itself was provided.
        """
        if "coordinate_space" in self.model_fields_set:
            self.coordinate_space_verified = self.coordinate_space is not CoordinateSpace.UNVERIFIABLE
        elif self.coordinate_space_verified:
            self.coordinate_space = CoordinateSpace.VERIFIED_PASSTHROUGH
        elif self.coordinate_scale_x != 1.0 or self.coordinate_scale_y != 1.0:
            self.coordinate_space = CoordinateSpace.SCALED
        else:
            self.coordinate_space = CoordinateSpace.UNVERIFIABLE
        return self


class GroundingValidation(BaseModel):
    valid: bool
    reasons: list[str] = Field(default_factory=list)


class VerificationResult(BaseModel):
    """Outcome of semantic verification.

    ``outcome`` is authoritative: ``verified`` actions proved the intended state
    transition, ``failed`` actions did not, and ``uncertain`` is never success. The legacy
    ``verified`` boolean remains readable (and serialized) as a derived property, and the
    legacy constructor form ``VerificationResult(verified=..., ...)`` still works: it is
    translated to ``outcome`` when ``outcome`` is not explicitly provided.
    """

    outcome: Literal["verified", "failed", "uncertain"] = "uncertain"
    changed: bool
    note: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    verification_method: str = "none"
    observation_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _derive_outcome_from_legacy_verified(cls, data: Any) -> Any:
        if isinstance(data, dict) and "outcome" not in data and "verified" in data:
            data = dict(data)
            data["outcome"] = "verified" if data.pop("verified") else "failed"
        return data

    @computed_field
    @property
    def verified(self) -> bool:
        """Derived convenience flag: True only when the outcome is exactly ``verified``."""
        return self.outcome == "verified"


class ExecutionResult(BaseModel):
    ok: bool
    action: GroundedAction
    message: str
    verification: VerificationResult | None = None
    screenshot_after_base64: str | None = None
    retry_count: int = 0


class SessionState(BaseModel):
    session_id: str
    step_count: int = 0
    max_steps: int = Field(default=30, ge=1, le=500)
    max_retries_per_action: int = Field(default=1, ge=0, le=5)
    min_confidence: float = Field(default=0.70, ge=0.0, le=1.0)
    dry_run: bool = True
    require_approval: bool = True
    stopped: bool = False
    allowed_windows: list[str] = Field(default_factory=list)
    pending_approval_token: str | None = None


class AgentDecision(BaseModel):
    status: Literal["action", "done", "blocked"]
    action: GroundedAction | None = None
    summary: str = Field(default="", max_length=1_000)
    expected_change: str | None = Field(default=None, max_length=500)
