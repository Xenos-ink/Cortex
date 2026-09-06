"""Action validation: grounding sanity, staleness/observation binding, allowlists.

Public entry point (backward compatible): :class:`GroundingValidator.validate` keeps the
legacy positional shape ``validate(action, observation, state)`` and the legacy
:class:`~computer_use_mcp.models.GroundingValidation` result contract (``valid`` +
human-readable ``reasons``), so the current ``agent.py``/``server.py`` call sites and the
existing suite work unchanged. New capabilities are additive keyword parameters.

Failure-reporting pattern (single pattern, per Wave-2 contract): validate() NEVER raises
for rejection candidates — it returns a structured :class:`ValidationOutcome` (a
``GroundingValidation`` subclass) carrying machine-readable ``codes`` plus the typed
rejection instance in ``outcome.error`` (a non-serialized private attribute). The
controller (E5/Wave 3) reads ``outcome.codes`` / ``outcome.error`` and maps
:class:`StaleObservationError` to ``FailureClass.STALE_COORDINATES`` before re-observing.

Staleness doctrine (P0-H): coordinate actions must carry ``source_observation_id`` and the
fresh pre-execution observation must match the source observation on active-window HWND,
monitor identity/bounds, screenshot dimensions, coordinate space, and active process
identity. Any drift is a typed :class:`StaleObservationError` — the action is rejected and
the controller re-observes instead of blindly executing stale coordinates.

Allowlist doctrine (P0-G): ``allowed_processes`` is authoritative when the observation
carries process identity (process name / exe path); a process outside the list is a
violation even when the window title matches. The legacy title substring allowlist is kept
verbatim as a fallback for observations without window identity; when
``WindowInfo`` is available, exact-title and process matching are preferred (the substring
check is retained additively for compatibility).
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, PrivateAttr

from .models import (
    ActionType,
    CoordinateSpace,
    GroundedAction,
    GroundingValidation,
    Observation,
    SessionState,
)

__all__ = [
    "GroundingRejection",
    "GroundingValidator",
    "MissingObservationBindingError",
    "ObservationBindingMismatchError",
    "ProcessIdentityUnavailableError",
    "ProcessNotAllowedError",
    "StaleObservationError",
    "ValidationOutcome",
    "WindowIdentityUnavailableError",
]

#: Actions whose execution consumes a screen point and therefore require observation binding.
#: Drag consumes two points (start ``point`` + end ``to_point``) and binds like click;
#: MOVE consumes one point (a cursor reposition) and binds like click too.
COORDINATE_ACTIONS: frozenset[ActionType] = frozenset(
    {ActionType.CLICK, ActionType.DOUBLE_CLICK, ActionType.DRAG, ActionType.MOVE}
)

#: Legacy confidence-floor exemptions, preserved verbatim from the prototype.
_CONFIDENCE_EXEMPT: frozenset[str] = frozenset({"wait", "done"})


class GroundingRejection(Exception):
    """Base type for structured validation rejections attached to a ``ValidationOutcome``.

    Instances are data, not control flow: ``validate()`` attaches them to the outcome
    instead of raising, keeping one reporting pattern for all callers.
    """

    code: str = "grounding_rejected"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")
        self.detail = detail


class MissingObservationBindingError(GroundingRejection):
    """A coordinate action lacks ``source_observation_id`` (P0-H fail-closed)."""

    code = "missing_observation_binding"


class ObservationBindingMismatchError(GroundingRejection):
    """The action's ``source_observation_id`` does not match the source observation."""

    code = "observation_binding_mismatch"


class StaleObservationError(GroundingRejection):
    """The fresh observation drifted from the action's source observation (P0-H).

    Attributes:
        detail: Which identity check drifted (e.g. ``active_window_hwnd``,
            ``monitor_identity``, ``monitor_bounds``, ``screenshot_dimensions``,
            ``coordinate_space``, ``active_process``).
        reason: Stable machine reason code, always ``"STALE_OBSERVATION"``.
    """

    code = "STALE_OBSERVATION"

    def __init__(self, detail: str, reason: str = "STALE_OBSERVATION") -> None:
        super().__init__(detail)
        self.reason = reason


class ProcessNotAllowedError(GroundingRejection):
    """The active process is outside ``allowed_processes`` (P0-G)."""

    code = "process_not_allowed"


class ProcessIdentityUnavailableError(GroundingRejection):
    """Process identity is unknown while a process allowlist is configured (fail-closed)."""

    code = "process_identity_unavailable"


class WindowIdentityUnavailableError(GroundingRejection):
    """The focus_window target window could not be resolved while a window-title
    allowlist is configured (fail-closed)."""

    code = "window_identity_unavailable"


def _title_matches_allowlist(title: str, allowed_windows: list[str]) -> bool:
    """Casefolded exact-or-substring title allowlist match for a single candidate title.

    Mirrors :meth:`GroundingValidator._window_allowed` for the agent's focus_window
    gate: that check accepts the active window on an exact ``WindowInfo.title`` match
    with a substring fallback on the best available active title; for ONE candidate
    title the two branches collapse to exact-or-substring over it. An empty title
    never matches (fail closed).
    """
    folded = (title or "").casefold()
    if not folded:
        return False
    return any(pattern.casefold() in folded for pattern in allowed_windows)


class ValidationOutcome(GroundingValidation):
    """Structured validation result: legacy fields plus machine-readable codes.

    ``valid``/``reasons`` behave exactly like the legacy ``GroundingValidation``.
    ``codes`` holds one stable reason code per rejection
    (``missing_observation_binding``, ``observation_binding_mismatch``,
    ``STALE_OBSERVATION``, ``process_not_allowed``, ``process_identity_unavailable``,
    ``confidence_below_floor``, ``coordinate_space_unverifiable``,
    ``window_not_allowed``, ``window_identity_unavailable``, ``point_out_of_bounds``,
    ``missing_point``, ``missing_text``, ``missing_keys``, ``missing_target``).
    ``error`` (private, never serialized) carries the first typed rejection instance
    for controllers that branch on exception type.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    codes: list[str] = Field(default_factory=list)
    _error: GroundingRejection | None = PrivateAttr(default=None)

    @property
    def ok(self) -> bool:
        """Alias for ``valid`` (structured-outcome naming used by the Wave-2 contract)."""
        return self.valid

    @property
    def error(self) -> GroundingRejection | None:
        """The first typed rejection attached to this outcome, if any."""
        return self._error


def _normalize_process(name: str) -> str:
    """Casefold and strip a trailing ``.exe`` so ``Notepad`` matches ``notepad.exe``."""
    return name.strip().casefold().removesuffix(".exe")


def _process_matches(candidate: str, allowed: str) -> bool:
    cand = _normalize_process(candidate)
    want = _normalize_process(allowed)
    if not cand or not want:
        return False
    if cand == want:
        return True
    # Tolerate executable paths on either side: fall back to basename comparison.
    cand_base = cand.replace("\\", "/").rsplit("/", 1)[-1]
    want_base = want.replace("\\", "/").rsplit("/", 1)[-1]
    return cand_base == want_base


def _exe_matches(exe_path: str, allowed: str) -> bool:
    return _process_matches(exe_path, allowed)


class GroundingValidator:
    """Validates a grounded action against the observation it will be executed on."""

    def validate(
        self,
        action: GroundedAction,
        observation: Observation,
        state: SessionState | None = None,
        current_observation: Observation | None = None,
        *,
        allowed_windows: list[str] | None = None,
        allowed_processes: list[str] | None = None,
        min_confidence: float | None = None,
        enforce_observation_binding: bool | None = None,
    ) -> ValidationOutcome:
        """Validate ``action`` against ``observation`` and return a structured outcome.

        Args:
            action: The proposed action.
            observation: The source observation the action was grounded from.
            state: Legacy third positional parameter (``SessionState``). May be ``None``
                for stateless callers; when an :class:`~computer_use_mcp.models.Observation`
                is passed positionally here it is re-interpreted as
                ``current_observation`` for call-shape tolerance.
            current_observation: Fresh pre-execution observation; when provided, staleness
                enforcement (P0-H) runs against it.
            allowed_windows: Window-title allowlist; defaults to ``state.allowed_windows``.
            allowed_processes: Process name/exe allowlist (authoritative when window
                identity is available; fail-closed when identity is unknown).
            min_confidence: Confidence floor; defaults to ``state.min_confidence``.
            enforce_observation_binding: Force the ``source_observation_id`` requirement
                for coordinate actions; defaults to auto (enabled when
                ``current_observation`` is provided).
        """
        if isinstance(state, Observation):  # tolerate new-style positional current_observation
            if current_observation is None:
                current_observation = state
            state = None

        reasons: list[str] = []
        codes: list[str] = []
        first_error: GroundingRejection | None = None

        def reject(code: str, reason: str, error: GroundingRejection | None = None) -> None:
            nonlocal first_error
            reasons.append(reason)
            codes.append(code)
            if error is not None and first_error is None:
                first_error = error

        floor = min_confidence if min_confidence is not None else (state.min_confidence if state else 0.0)
        if action.confidence < floor and action.action not in _CONFIDENCE_EXEMPT:
            reject(
                "confidence_below_floor",
                f"Confidence {action.confidence:.2f} is below {floor:.2f}.",
            )

        if not observation.coordinate_space_verified or observation.coordinate_space is CoordinateSpace.UNVERIFIABLE:
            reject(
                "coordinate_space_unverifiable",
                "Screenshot and input coordinate spaces differ; refusing to execute until DPI mapping "
                "is verified.",
            )

        # --- allowlists (P0-G) -------------------------------------------------------------
        effective_windows = allowed_windows if allowed_windows is not None else (
            list(state.allowed_windows) if state else []
        )
        if effective_windows and not self._window_allowed(observation, effective_windows):
            reject("window_not_allowed", "Active window is not in the configured allowlist.")
        if allowed_processes:
            violation = self._process_allowlist_violation(observation, allowed_processes)
            if violation is not None:
                error: GroundingRejection
                if violation == "process_identity_unavailable":
                    error = ProcessIdentityUnavailableError(
                        "Active process identity is unavailable; cannot verify the process allowlist."
                    )
                    reject(
                        "process_identity_unavailable",
                        "Active process identity is unavailable; the process allowlist cannot be verified.",
                        error,
                    )
                else:
                    error = ProcessNotAllowedError(
                        f"Active process {violation!r} is not in the configured process allowlist."
                    )
                    reject(
                        "process_not_allowed",
                        f"Active process {violation!r} is not in the configured process allowlist.",
                        error,
                    )

        # --- staleness / observation binding (P0-H) ----------------------------------------
        enforce_binding = enforce_observation_binding if enforce_observation_binding is not None else (
            current_observation is not None
        )
        if enforce_binding and action.action in COORDINATE_ACTIONS:
            if not action.source_observation_id:
                reject(
                    "missing_observation_binding",
                    "Coordinate action is missing source_observation_id; refusing to execute against a "
                    "possibly stale screen.",
                    MissingObservationBindingError(
                        "Coordinate action has no source_observation_id binding."
                    ),
                )
            elif (
                observation.observation_id
                and action.source_observation_id != observation.observation_id
            ):
                reject(
                    "observation_binding_mismatch",
                    "Action's source_observation_id does not match the observation it is validated against.",
                    ObservationBindingMismatchError(
                        f"Action bound to {action.source_observation_id!r} but validated against "
                        f"{observation.observation_id!r}."
                    ),
                )
        if current_observation is not None:
            drift = self._staleness_drift(observation, current_observation)
            if drift is not None:
                reject(
                    "STALE_OBSERVATION",
                    f"Stale observation detected ({drift}); re-observe before executing.",
                    StaleObservationError(drift),
                )

        # --- action-shape checks (legacy, verbatim) -----------------------------------------
        if action.action in COORDINATE_ACTIONS:
            if action.point is None:
                reject("missing_point", "Click actions require a grounded point.")
            else:
                max_x, max_y = self._point_bounds(action, observation)
                if action.point.x >= max_x or action.point.y >= max_y:
                    reject(
                        "point_out_of_bounds",
                        "Grounded point is outside the current screenshot bounds.",
                    )
                if action.to_point is not None and (
                    action.to_point.x >= max_x or action.to_point.y >= max_y
                ):
                    reject(
                        "point_out_of_bounds",
                        "Drag end point is outside the current screenshot bounds.",
                    )
        if action.action == ActionType.TYPE and not action.text:
            reject("missing_text", "Type actions require non-empty text.")
        if action.action in {ActionType.KEYPRESS, ActionType.HOTKEY} and not action.keys:
            reject("missing_keys", "Keypress/hotkey actions require at least one key.")
        if action.action == ActionType.FOCUS_WINDOW and not (action.target or "").strip():
            reject(
                "missing_target",
                "focus_window actions require a non-empty target window title.",
            )

        outcome = ValidationOutcome(valid=not reasons, reasons=reasons, codes=codes)
        outcome._error = first_error
        return outcome

    @staticmethod
    def _point_bounds(action: GroundedAction, observation: Observation) -> tuple[int, int]:
        """Effective upper bounds for the action point, honoring normalized input space.

        When a grounding strategy normalized the point into input space (scaled coordinate
        space), bounds are the input dimensions; otherwise the screenshot dimensions (the
        legacy behavior, preserved verbatim for ungrounded actions).
        """
        grounding = action.grounding
        if grounding is not None and grounding.normalized and observation.coordinate_space_verified:
            input_width = observation.input_width or round(
                observation.width * observation.coordinate_scale_x
            )
            input_height = observation.input_height or round(
                observation.height * observation.coordinate_scale_y
            )
            return input_width, input_height
        return observation.width, observation.height

    @staticmethod
    def _window_allowed(observation: Observation, allowed_windows: list[str]) -> bool:
        """Title allowlist: exact/preferred match via WindowInfo, legacy substring fallback."""
        info = observation.active_window_info
        if info is not None and info.title:
            title_fold = info.title.casefold()
            if any(pattern.casefold() == title_fold for pattern in allowed_windows):
                return True
        active = observation.active_window or (info.title if info is not None else "") or ""
        return any(pattern.casefold() in active.casefold() for pattern in allowed_windows)

    @staticmethod
    def _process_allowlist_violation(
        observation: Observation, allowed_processes: list[str]
    ) -> str | None:
        """Return the offending process identity, or None when the allowlist is satisfied.

        Fail-closed: when the observation carries no process identity at all, the violation
        is reported as the sentinel ``"process_identity_unavailable"``.
        """
        info = observation.active_window_info
        identity: str | None = None
        if info is not None:
            if info.process_name:
                identity = info.process_name
            elif info.exe_path:
                identity = info.exe_path
        if identity is None:
            return "process_identity_unavailable"
        for allowed in allowed_processes:
            if info.process_name and _process_matches(info.process_name, allowed):
                return None
            if info.exe_path and _exe_matches(info.exe_path, allowed):
                return None
        return identity

    @staticmethod
    def _staleness_drift(source: Observation, current: Observation) -> str | None:
        """Return which identity dimension drifted, or None when the screen identity holds."""
        if (source.width, source.height) != (current.width, current.height):
            return "screenshot_dimensions"
        if source.coordinate_space != current.coordinate_space:
            return "coordinate_space"

        source_info = source.active_window_info
        current_info = current.active_window_info
        if source_info is not None and current_info is not None:
            if source_info.hwnd is not None and current_info.hwnd is not None and source_info.hwnd != current_info.hwnd:
                return "active_window_hwnd"
            if (
                source_info.pid is not None
                and current_info.pid is not None
                and source_info.pid != current_info.pid
            ):
                return "active_process"
            source_process = source_info.process_name or (
                source_info.exe_path.rsplit("\\", 1)[-1] if source_info.exe_path else None
            )
            current_process = current_info.process_name or (
                current_info.exe_path.rsplit("\\", 1)[-1] if current_info.exe_path else None
            )
            if source_process and current_process and not _process_matches(source_process, current_process):
                return "active_process"
        elif source.active_window is not None and current.active_window is not None:
            if source.active_window.casefold() != current.active_window.casefold():
                return "active_window_title"

        source_monitor = source.monitor
        current_monitor = current.monitor
        if source_monitor is not None and current_monitor is not None:
            if (source_monitor.id, source_monitor.index) != (current_monitor.id, current_monitor.index):
                return "monitor_identity"
            if tuple(source_monitor.bounds) != tuple(current_monitor.bounds):
                return "monitor_bounds"
        return None
