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
- ``Subtask.results`` retains at most ``SUBTASK_RESULTS_CAP`` execution results with heavy
  payloads (screenshot base64) stripped before storage; ``MAX_SUBTASKS`` is the hard
  per-session subtask ceiling (50).
"""

from __future__ import annotations

import uuid
from collections import deque
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, PrivateAttr, computed_field, model_validator


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


class ActionType(StrEnum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    # Owner-commissioned additive sibling of click/double_click (v0.6.0): a single
    # RIGHT-button press at ``point`` — opens a context menu. Point-bearing and
    # grounded/validated/risk-classified/verified exactly like click.
    RIGHT_CLICK = "right_click"
    DRAG = "drag"
    TYPE = "type"
    KEYPRESS = "keypress"
    SCROLL = "scroll"
    WAIT = "wait"
    DONE = "done"
    MOVE = "move"
    HOTKEY = "hotkey"
    FOCUS_WINDOW = "focus_window"
    # T8 mechanism ii (additive sibling of focus_window): attach-or-launch probe.
    # Target = ``process[|doc-token]``; focuses an EXISTING instance and never spawns
    # one by default (REATTACHED / AMBIGUOUS_INSTANCE / NO_INSTANCE payloads).
    ENSURE_APP = "ensure_app"


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
    # Additive (master-mission 003 conflict C8): a whole subtask exhausted its bounded
    # in-subtask recovery; the orchestrator may plan a bounded replan of remaining subtasks.
    SUBTASK_FAILED = "subtask_failed"


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
    #: D15 (ORVEX-2026-108 WP4b) is ADDITIVE and defaults to ``False``: the region's
    #: label was longer than the semantic read's 200-char clip and is served CLIPPED,
    #: so a silent content loss cannot pass as a complete read.
    text_truncated: bool = False


class GroundingResult(BaseModel):
    """How an action's coordinates/target were derived, and how confident that was."""

    strategy: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    normalized: bool = False
    notes: str | None = Field(default=None, max_length=1_000)


#: WP2 (ORVEX-2026-101 Batch 1, P1.1): hard cap for the explicit click/key press
#: duration. ``hold_ms`` above this is rejected fail-closed at construction (and
#: again at the validator gate); ``None``/absent keeps the legacy press behavior
#: byte-identical.
MAX_HOLD_MS = 5_000

#: WP2 (ORVEX-2026-101 Batch 2, P2.1): hard cap for the explicit transition duration
#: (``duration_ms``, move-only). ``None``/absent keeps the legacy jump behavior
#: byte-identical; ``0`` is an explicit zero-duration jump.
MAX_DURATION_MS = 5_000

#: WP2 (ORVEX-2026-102 §5.2, SPEC 5.2): hard band for the EXPLICIT per-point settle a
#: plan declares (a waypoint's ``settle_ms``; the plan surface itself lands in WP7).
#: ``0`` is a legal, meaningful value — it says "this point settles not at all". The
#: upper edge is the ORVEX-2026-101 decision-4 ceiling, so a plan may still ask for
#: the longest settle that was ever configurable.
MAX_PLAN_SETTLE_MS = 200


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

    ``via`` (v0.7.1, additive; ``type`` actions ONLY) selects the text-entry transport:
    ``None``/``"sendinput"`` keeps the default per-character dispatch (byte-identical to
    the pre-via behavior); ``"clipboard"`` sets CF_UNICODETEXT then dispatches the ctrl+v
    chord through the existing hotkey path, for OpenGL/console apps whose input stack
    does not process per-character injection. The transport NEVER bypasses any gate:
    classification, redaction, and approval run on ``text`` identically for both values.

    ``hold_ms`` (WP2, ORVEX-2026-101 Batch 1, additive; widened by ORVEX-2026-107 to
    ``click``/``right_click``/``keypress``/``hotkey``) is an explicit press duration
    in milliseconds. On a click it dispatches press -> sliced interruptible wait ->
    release instead of the legacy single click event (whose physical down->up gap
    measures ~0.03-0.16 ms). On a key action it holds EVERY named key down
    simultaneously for the duration and releases them in reverse order (ORVEX-2026-107
    D1 — "chord, then wait" would be vacuous, because a chord has already released
    everything by the time it returns). ``None`` (absent) keeps the legacy path
    byte-identical (still ONE atomic ``chord`` batch); ``0`` is an explicit zero hold
    through the SAME split press/release path, never a chord fast-path (D2 — adjacent
    values must not diverge in call count or release order). Bounds are fail-closed
    (``0..MAX_HOLD_MS``); ``double_click`` is deliberately excluded — a two-click
    duration is a different, deferred semantic. The duration NEVER bypasses any gate:
    classification, redaction, and approval run identically for held and legacy
    dispatches.

    ``duration_ms`` (WP2, ORVEX-2026-101 Batch 2, additive; ``move`` actions ONLY) is
    an explicit CONNECTED-MOTION duration in milliseconds: the backend interpolates a
    deterministic path from the current cursor position to the target and dispatches
    it over the requested time (the internal motion engine) instead of the legacy
    single absolute jump. ``None`` (absent) keeps the legacy jump byte-identical;
    ``0`` is an explicit zero-duration jump. Bounds are fail-closed
    (``0..MAX_DURATION_MS``). Motion is an INTERNAL detail: the model declares
    locations and actions only, and the duration NEVER bypasses any gate —
    classification, redaction, and approval run identically for timed and legacy moves.

    ``plan_point_settle`` (WP2, ORVEX-2026-101 Batch 3, P3.2/D15) is an INTERNAL
    queue-attached marker — deliberately NOT mirrored on ``ActionSpec``, so no host
    payload can carry it (P3.2 item 9: no surface). The plan queue sets it on plan
    elements only: ``True`` on a plan point's LAST item (the backend settles ONCE
    there, after that dispatch and before its post-action observation), ``False`` on
    the transition moves of points that HAVE actions (they settle NOT after the move;
    the point settles at its last action), and ``None`` (absent — every non-plan
    action and every ``to_grounded`` conversion) keeps the legacy settle policy
    byte-identical. There is no validator by design: the marker is never host data.
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
    via: str | None = Field(default=None, max_length=20)
    # WP2 (ORVEX-2026-101 Batch 1): explicit press duration in ms; valid ONLY on the
    # HOLD_ACTIONS family (fail-closed validator below); None keeps the legacy path.
    hold_ms: int | None = Field(default=None, ge=0, le=MAX_HOLD_MS)
    # WP2 (ORVEX-2026-101 Batch 2): explicit transition duration in ms; move-only
    # (fail-closed validator below); None keeps the legacy jump.
    duration_ms: int | None = Field(default=None, ge=0, le=MAX_DURATION_MS)
    # WP2 (ORVEX-2026-101 Batch 3, P3.2/D15): INTERNAL plan-settle marker (see the
    # docstring above). Never host-visible: ActionSpec does not mirror it, and
    # to_grounded leaves it None (the legacy settle policy).
    plan_point_settle: bool | None = Field(default=None)
    # WP2 (ORVEX-2026-102 §5.2, SPEC 5.2): INTERNAL, plan-only, exactly like
    # ``plan_point_settle`` above — ActionSpec deliberately does NOT mirror it, so no
    # host payload on the flat surface can carry a settle, and ``to_grounded`` leaves
    # it None. Set ONLY by the plan normalizer's stamping producer
    # (``computer_use_mcp.plan.stamped_plan_actions``, from the point's declared
    # ``settle_ms`` — the WP7a producer this field's WP2 comment originally deferred
    # as a seam). ``None`` means "no declared settle" and the dispatch path falls
    # back to the module policy.
    plan_point_settle_ms: int | None = Field(default=None, ge=0, le=MAX_PLAN_SETTLE_MS)
    action_id: str = Field(default_factory=_new_id)
    risk: RiskLevel | None = None
    grounding: GroundingResult | None = None
    # Internal B4-8 evidence only: set after an input-engine call actually returns.
    # Private attributes are excluded from model_dump/JSON, so this cannot become a
    # host-visible plan marker; the queue uses it solely to distinguish a cancellation
    # before dispatch from an input that was sent but could not be verified.
    _dispatch_started: bool = PrivateAttr(default=False)
    # WP2 (ORVEX-2026-101 Batch 5, decision 2): bounded internal evidence for a timed
    # motion that observed cursor interference and immediately re-asserted its sample
    # target. It is private so the established ActionSpec/JSON surface is unchanged;
    # the agent copies it into the additive queue-entry/audit ``repositions`` field
    # after execution.
    _repositions: list[dict[str, Any]] = PrivateAttr(default_factory=list)

    #: v0.7.1: the ONLY valid non-None ``via`` values, and only on ``type`` actions.
    VIA_TRANSPORTS: ClassVar[tuple[str, ...]] = ("sendinput", "clipboard")

    #: WP2 (ORVEX-2026-101 Batch 1, widened by ORVEX-2026-107): the ONLY actions a
    #: non-None ``hold_ms`` is valid on. ``keypress``/``hotkey`` joined because a held
    #: KEY is the capability the objective needs (continuous motion); the click pair
    #: keeps the ORVEX-2026-101 hold. ``double_click`` is deliberately NOT here.
    HOLD_ACTIONS: ClassVar[tuple[ActionType, ...]] = (
        ActionType.CLICK,
        ActionType.RIGHT_CLICK,
        ActionType.KEYPRESS,
        ActionType.HOTKEY,
    )

    @model_validator(mode="after")
    def _via_type_only_known_values(self) -> GroundedAction:
        """``via`` is a type-action transport selector: fail closed on any misuse.

        v0.7.1 (Defect C): ``None``/``"sendinput"`` keeps the per-character dispatch;
        ``"clipboard"`` selects the paste transport. A ``via`` on ANY non-type action,
        or an unknown value on a type action, is a malformed action and fails
        construction (surfacing as the typed ``invalid_action`` boundary error) so it
        can never reach the backend silently ignored.
        """
        if self.via is None:
            return self
        if self.action is not ActionType.TYPE:
            raise ValueError(
                "via is only valid on type actions; "
                f'received via="{self.via}" on action="{self.action.value}"'
            )
        if self.via not in self.VIA_TRANSPORTS:
            allowed = " or ".join(f'"{value}"' for value in self.VIA_TRANSPORTS)
            raise ValueError(f"via must be {allowed}; received {self.via!r}")
        return self

    @model_validator(mode="after")
    def _hold_ms_hold_family_only(self) -> GroundedAction:
        """``hold_ms`` is a press/hold duration: fail closed on any misuse.

        WP2 (ORVEX-2026-101 Batch 1, P1.1), same additive-failure precedent as
        ``via``, and the same fail-closed shape it keeps after ORVEX-2026-107 widened
        the legal set from the click family to the click family PLUS
        ``keypress``/``hotkey``: a ``hold_ms`` on ANY action outside ``HOLD_ACTIONS``
        is a malformed action and fails construction (surfacing as the typed
        ``invalid_action`` boundary error) so it can never reach the backend silently
        ignored. ``double_click`` stays deliberately excluded — an explicit duration
        for a two-click sequence is a different, deferred semantic. ``None`` (absent)
        is the legacy path (one atomic ``chord``) and is never rejected.
        """
        if self.hold_ms is None:
            return self
        allowed = " or ".join(f'"{value.value}"' for value in self.HOLD_ACTIONS)
        if self.action not in self.HOLD_ACTIONS:
            raise ValueError(
                "hold_ms is only valid on click/right_click/keypress/hotkey actions; "
                f'received hold_ms={self.hold_ms} on action="{self.action.value}" '
                f"(valid on: {allowed})"
            )
        return self

    @model_validator(mode="after")
    def _duration_ms_move_only(self) -> GroundedAction:
        """``duration_ms`` is a transition (move) duration: fail closed on any misuse.

        WP2 (ORVEX-2026-101 Batch 2, P2.1), the same additive-failure precedent as
        ``hold_ms``: a ``duration_ms`` on ANY non-move action is a malformed action
        and fails construction (surfacing as the typed ``invalid_action`` boundary
        error) so it can never reach the backend silently ignored. ``None`` (absent)
        is the legacy jump and never rejected.
        """
        if self.duration_ms is None:
            return self
        if self.action is not ActionType.MOVE:
            raise ValueError(
                "duration_ms is only valid on move actions; "
                f'received duration_ms={self.duration_ms} on action="{self.action.value}"'
            )
        return self

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
    def _right_click_requires_point(self) -> GroundedAction:
        """A right_click needs a target point (screenshot space) — fail closed.

        Same construction contract as click/double_click: a context-menu request
        without coordinates is malformed and never reaches the backend.
        """
        if self.action is ActionType.RIGHT_CLICK and self.point is None:
            raise ValueError("right_click actions require a point (screenshot space)")
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

    @model_validator(mode="after")
    def _ensure_app_requires_target(self) -> GroundedAction:
        """An ensure_app needs a non-empty ``process[|doc-token]`` target — fail closed."""
        if self.action is ActionType.ENSURE_APP and not (self.target or "").strip():
            raise ValueError("ensure_app actions require a non-empty target (process[|doc-token])")
        return self


# --- AL-002 adaptive visual representations (additive constants; MISSION-CRF-AVR-008) --------
#: Registry keys of the observation view providers (AL-002 Component 1). Extensible (R2):
#: registering a new provider adds a key here and in the registry; the DEFAULT path never
#: reads this tuple (absent request = byte-identical behavior, I-1).
VISUAL_VIEWS: tuple[str, ...] = ("raw", "grid")

#: The view served when a request names none (AL-002): pass-through, byte-identical to the
#: pre-AL-002 server (I-1). The server NEVER auto-selects a different view — the host
#: picks per call, or the default stands.
DEFAULT_VISUAL_VIEW = "raw"


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
    #: D15 (ORVEX-2026-108 WP4b, additive): how many text elements THIS READ knew
    #: about but did not serve — the reader's own element/wall-clock caps and
    #: offscreen/unreadable elements, plus the rect-less/text-less drops in the
    #: conversion. The ``spatial_text`` block's ``omitted_count`` adds this to its own
    #: cap-drop count, so ``region_count + omitted_count`` accounts for the whole
    #: read. ``0`` means "nothing was dropped" (and a degraded read, where ``ocr_text``
    #: is ``None``, ships ``spatial_text_available: false`` instead of a block).
    ocr_dropped: int = Field(default=0, ge=0)

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

    # --- R-5 mechanical-speed private stash (never serialized; additive) -------------------
    # ``_frame``: the capture-time RGB frame (a PIL Image) when the producing backend
    # supports internal-frame reuse (LocalComputerBackend). Consumers that need pixels
    # (verification pixel-diff, outbound re-encode) use it to skip a base64+PNG decode
    # of a frame the pipeline JUST encoded; anything absent falls back to decoding
    # ``image_base64`` exactly as before, so tests/fakes are unaffected.
    _frame: Any = PrivateAttr(default=None)
    # ``_captured_monotonic``: ``time.perf_counter()`` at capture (set by the agent's
    # ``_observe``), the freshness clock for the validate-phase capture-reuse window.
    _captured_monotonic: float = PrivateAttr(default=0.0)
    # ``_frame_raw`` (R-7): the capture-time RAW pixel bytes the stashed ``_frame``
    # was built from, when — and only when — the producing backend holds one readback
    # buffer per capture (dxgi packed BGRA, mss BGRX DIB). The verification diff's
    # identical-frame short-circuit memcmpes two stashes (equal bytes == equal pixels,
    # ~0.5 ms vs a ~25 ms full-frame diff); absent on every other path (decoded PNGs,
    # tests, fakes) and never serialized.
    _frame_raw: Any = PrivateAttr(default=None)
    # AL-002 OCR-once spatial-text cache (Component 2, additive; the ``_frame``
    # precedent): ``_spatial_text`` is the bounded spatial-text block (or None when the
    # UIA read degraded — honest absence, never an empty lie), ``_spatial_text_derived``
    # the memo flag, ``_spatial_text_compute_count`` the H21 instrumentation datum
    # (test-visible only). Never serialized (pydantic PrivateAttr), never part of any
    # digest (``observation.observation_digest`` reads only ``image_base64``), never
    # visible in any tool result or audit event as state (I-3). Lifetime = the
    # Observation object's lifetime — deliberately NO cross-observation store (ST-03).
    _spatial_text_derived: bool = PrivateAttr(default=False)
    _spatial_text: dict | None = PrivateAttr(default=None)
    _spatial_text_compute_count: int = PrivateAttr(default=0)


class GroundingValidation(BaseModel):
    valid: bool
    reasons: list[str] = Field(default_factory=list)


class CaptureProvenance(BaseModel):
    """Identity evidence for the two pixel frames a visual verification consumed.

    v0.7.1 Defect A (additive, purely diagnostic): a confident ``Mean pixel
    difference 0.000000`` from a dead/frozen capture source is indistinguishable
    from a genuinely static screen unless the frames themselves carry identity.
    The provenance is computed by the verification tier from the frames ALREADY in
    hand at verification time (sha256 + byte size of each side's frame bytes) —
    never from new captures. ``algorithm`` names the hash so the evidence can never
    silently change meaning.
    """

    algorithm: str = "sha256"
    before_sha256: str
    after_sha256: str
    before_bytes: int = Field(ge=0)
    after_bytes: int = Field(ge=0)


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
    # v0.7.1 Defect A (additive, None-preserving): capture identity for the visual
    # tier's two in-hand frames. Diagnostic only — it NEVER alters ``outcome``;
    # older results (every non-pixel strategy, legacy constructors) leave it None.
    capture_provenance: CaptureProvenance | None = None

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


# --- unified reply contract (ORVEX-2026-108 MASTER MISSION §1) -------------------------------

#: The closed §1.2 ``result`` token set. Every ``computer_execute`` reply (flat and
#: every ``follow_up_results[i]`` item) carries exactly one of these tokens; plan and
#: wait surfaces reuse the same set (their per-surface subsets live in the contract).
RESULT_TOKENS: tuple[str, ...] = (
    "verified",      # declared effect established by fresh evidence    -> ok True
    "uncertain",     # dispatched, unproven; never silently upgraded    -> ok False
    "failed",        # proven not achieved, or operation error          -> ok False
    "rejected",      # a gate refused (zero effect, except partial)     -> ok False
    "not_executed",  # intended but never dispatched (approval, cutoff) -> ok False
    "dry_run",       # validated, deliberately not dispatched           -> ok False
    "marker",        # intentional non-screen claim (done/wait)         -> ok True
    "stopped",       # host-requested stop; nothing verified            -> ok False
    "unknown",       # terminal plan aggregate: >=1 uncertain, none failed -> ok False
)

#: §1.1 ok-rule inputs: the ONLY result tokens that may pair with ``ok is True``.
OK_TRUE_RESULTS: frozenset[str] = frozenset({"verified", "marker"})


def ok_from_result(result: str | None) -> bool:
    """The §1.1 ok-rule, in one shared place: ``ok is True`` iff ``result ∈ {verified, marker}``.

    Every reply builder derives ``ok`` from the reply's ``result`` token through THIS
    function — no per-shape ``ok`` rule may exist anywhere else.
    """
    return result in OK_TRUE_RESULTS


#: §1.1 S-12: the typed ``ensure_app_outcome`` Literal enum. Before this field the
#: launch-vs-reattach-vs-ambiguity outcome was conveyed ONLY by a free-text ``message``
#: prefix; a driver can now switch on a closed enum instead of parsing a string.
#: ``None`` on every non-``ensure_app`` action (and when the payload is not an
#: ``ensure_app`` shape at all, so an unrecognised form stays visible instead of being
#: filed under a plausible-looking value).
EnsureAppOutcome = Literal[
    "reattached",
    "ambiguous",
    "not_found",
    "launched",
    "launch_rejected",
    "launch_unresolved",
]


class ExecutionResult(BaseModel):
    ok: bool
    action: GroundedAction
    message: str
    verification: VerificationResult | None = None
    screenshot_after_base64: str | None = None
    retry_count: int = 0
    # ORVEX-2026-108 §1.2: this row was a DRY RUN — the action was validated and then
    # deliberately NOT dispatched, so no post-action capture and no verification
    # happened. ``ok`` is False on such a row (§1.1: dry_run is never ok=True); this
    # marker names WHY, so a host can tell "not executed on purpose" from "executed
    # and failed" without parsing ``message``. The top-level ``result`` token for the
    # reply is ``"dry_run"`` (assigned by the reply builder, not here).
    dry_run: bool = False
    # §1.1 S-12: the TYPED outcome of an ``ensure_app`` execution (the closed
    # :data:`EnsureAppOutcome` enum), or ``None`` on every other action. Additive;
    # ``ok`` semantics are the §1.1 ok-rule, unchanged.
    ensure_app_outcome: EnsureAppOutcome | None = None
    # R-5 (W4): the post-action capture's RGB frame (never serialized; additive). The
    # server's outbound bounding uses it to skip re-decoding the PNG it just encoded;
    # absent (legacy results, tests) the outbound path decodes exactly as before.
    _frame: Any = PrivateAttr(default=None)


# --- host-path action queue + observation summary (PERF-004, additive) -----------------------

#: Hard cap on ``follow_ups`` accepted by one ``computer_execute`` call (PERF-004 C7):
#: a queued batch is speculative planning (UFO2-style), deliberately small; every entry
#: still passes the FULL independent pipeline; blocking failures (rejections, safety,
#: approval, digest surprise) stop the queue while an executed item's uncertain OR
#: failed verification verdict rides its per-item entry (;
#: ``CORTEX_QUEUE_STRICT_VERIFY=1`` restores stop-on-failed).
MAX_FOLLOW_UPS = 5


class ActionSpec(BaseModel):
    """One queued host action in a ``computer_execute`` ``follow_ups`` list (PERF-004 C7).

    Mirrors the ``computer_execute`` tool surface (same field names/semantics, additive
    model — existing models are untouched). Each spec is converted to a full
    :class:`GroundedAction` and passes the complete independent pipeline
    (ground -> validate -> safety -> approval -> execute -> verify) exactly like a
    single action; ``approved`` from the enclosing call applies to every queue item
    (approval semantics are evaluated per item by the safety policy).
    """

    action: ActionType
    x: int | None = Field(default=None, ge=-8_192, le=16_384)
    y: int | None = Field(default=None, ge=-8_192, le=16_384)
    x2: int | None = Field(default=None, ge=-8_192, le=16_384)
    y2: int | None = Field(default=None, ge=-8_192, le=16_384)
    text: str | None = Field(default=None, max_length=2_000)
    keys: list[str] = Field(default_factory=list, max_length=12)
    delta: int = Field(default=0, ge=-20, le=20)
    target: str | None = Field(default=None, max_length=200)
    expected_effect: str | None = Field(default=None, max_length=500)
    # v0.7.1 (Defect C): pass-through of the type-action transport selector so a queued
    # {"action": "type", "via": "clipboard"} entry behaves EXACTLY like the equivalent
    # single action. Validation (type-only + known values) happens in GroundedAction.
    via: str | None = Field(default=None, max_length=20)
    # WP2 (ORVEX-2026-101 Batch 1, P1.1): pass-through of the explicit press duration
    # so a queued {"action": "click", "hold_ms": 250} entry behaves EXACTLY like the
    # equivalent single action. Validation (HOLD_ACTIONS-only + bounds) happens in
    # GroundedAction; None keeps the legacy queued behavior byte-identical.
    hold_ms: int | None = Field(default=None, ge=0, le=MAX_HOLD_MS)
    # WP2 (ORVEX-2026-101 Batch 2, P2.1): pass-through of the explicit transition
    # duration so a queued {"action": "move", "duration_ms": 200} entry behaves
    # EXACTLY like the equivalent single action. Validation (move-only + bounds)
    # happens in GroundedAction; None keeps the legacy queued-move jump
    # byte-identical.
    duration_ms: int | None = Field(default=None, ge=0, le=MAX_DURATION_MS)

    def to_grounded(self, *, reason_prefix: str = "Explicit MCP action") -> GroundedAction:
        """Build the :class:`GroundedAction` this spec denotes (validation happens there)."""
        return GroundedAction(
            action=self.action,
            point=None if self.x is None or self.y is None else {"x": self.x, "y": self.y},
            to_point=None if self.x2 is None or self.y2 is None else {"x": self.x2, "y": self.y2},
            text=self.text,
            keys=list(self.keys),
            delta=self.delta,
            reason=reason_prefix,
            confidence=1.0,
            expected_effect=self.expected_effect,
            target=self.target,
            via=self.via,
            hold_ms=self.hold_ms,
            duration_ms=self.duration_ms,
        )


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


# --- subtask domain (master-mission 003, additive; orchestration wiring is a later wave) ---

#: Hard cap on subtasks per session (SubtasksProtocol section 1: strict maximum of 50).
MAX_SUBTASKS = 50

#: Bounded per-subtask retention of :class:`ExecutionResult` records (conflict C5: heavy
#: fields such as screenshot base64 are stripped before storage; oldest records evicted).
SUBTASK_RESULTS_CAP = 20


class SubtaskStatus(StrEnum):
    """Lifecycle status of one subtask (SubtasksProtocol section 1; ``paused`` included)."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    PAUSED = "paused"


class SubtaskFailureInfo(BaseModel):
    """Failure/recovery info retained on a failed subtask (SubtasksProtocol section 4)."""

    failure_class: FailureClass | None = None
    error: str = Field(default="", max_length=2_000)
    recovery_attempts: int = Field(default=0, ge=0)
    last_known_state: str = Field(default="", max_length=1_000)
    recorded_at: datetime = Field(default_factory=_utc_now)


class Subtask(BaseModel):
    """One decomposed unit of work with dependencies and bounded results.

    ``subtask_id`` is a stable, serializable identifier: it is preserved verbatim by
    serialization round-trips and checkpoint restore, so dependency references never dangle.
    ``results`` keeps at most :data:`SUBTASK_RESULTS_CAP` :class:`ExecutionResult` records
    (newest retained); heavy payloads (``screenshot_after_base64``) must be stripped by the
    recorder before storage — the re-wrap validator below guarantees the deque can never
    grow past the cap, even on a restore path, so no serialization round-trip can unbound it.
    """

    subtask_id: str = Field(default_factory=_new_id, min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=2_000)
    status: SubtaskStatus = SubtaskStatus.PENDING
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_SUBTASKS)
    created_at: datetime = Field(default_factory=_utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    recovery_attempts: int = Field(default=0, ge=0)
    results: deque[ExecutionResult] = Field(
        default_factory=lambda: deque(maxlen=SUBTASK_RESULTS_CAP),
        max_length=SUBTASK_RESULTS_CAP,
    )
    failure: SubtaskFailureInfo | None = None

    @model_validator(mode="after")
    def _rebound_results(self) -> Subtask:
        """Re-wrap ``results`` with the retention cap (validated data loses ``maxlen``)."""
        if self.results.maxlen != SUBTASK_RESULTS_CAP:
            self.results = deque(self.results, maxlen=SUBTASK_RESULTS_CAP)
        return self


class SubtaskPlanEntry(BaseModel):
    """One validated (normalized) entry of an LLM-proposed plan.

    Constructed only by the plan validator after its deterministic checks pass; pydantic
    constraints here are the fail-closed backstop, never the primary rejection mechanism.
    """

    subtask_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=2_000)
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_SUBTASKS)
    status: SubtaskStatus = SubtaskStatus.PENDING


class SubtaskPlan(BaseModel):
    """A validated plan: at least one, at most :data:`MAX_SUBTASKS`, entries."""

    entries: list[SubtaskPlanEntry] = Field(min_length=1, max_length=MAX_SUBTASKS)
