"""Grounding abstraction: strategies that resolve an action's target against an observation.

Layering (master-mission section 5): this module imports only ``models``. The controller
(E5/Wave 3) calls :class:`GroundingRouter.route` after a decision is proposed and before
validation/execution; the returned :class:`~computer_use_mcp.models.GroundingResult` is
attached to ``GroundedAction.grounding``. Strategies that *derive* a target location
(region / text anchor / accessibility) set ``GroundedAction.point`` in place, always in
SCREENSHOT space; :class:`CoordinateGroundingStrategy` never rewrites the point at all.

Coordinate-transform invariant (F1, binding for every layer): there is EXACTLY ONE
screenshot-to-physical transform in the pipeline, and it lives in the backend
(``ComputerBackend._map_to_physical``: ``physical = origin + screenshot * scale``),
applied exactly once at execution. Grounding validates bounds in screenshot space and
records the verified scale on the result (``GroundingResult.normalized=True`` means
"validated in a verified scaled space; scale recorded" — NOT that the point was
rewritten). No grounding strategy may pre-scale a point, and no consumer may re-scale
one, or the executed position lands at ``origin + screenshot * scale**2``.

Confidence doctrine (Goal.md section 6): grounding confidence is NOT model confidence.
- :class:`CoordinateGroundingStrategy` never invents confidence: it passes the decision's
  stated confidence through and labels it as coordinate-sanity evidence only.
- Semantic strategies (text anchor / accessibility) report the confidence of the
  *matching evidence* (e.g. OCR region confidence), which is a distinct quantity.

Failure doctrine: strategies raise :class:`UnsupportedGroundingError` (fail-closed)
when the observation carries no usable data or the target cannot be resolved. The router
NEVER silently falls back to coordinates when an explicitly requested non-coordinate
strategy fails.

P1 note: OCR (``Observation.ocr_text``) and accessibility (``Observation.ui_elements``)
data are populated by later waves; until then these strategies raise
:class:`UnsupportedGroundingError` and the runtime degrades gracefully (coordinate
grounding remains fully functional). That graceful degradation is the P0 deliverable.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from .models import ActionType, CoordinateSpace, GroundedAction, GroundingResult, Observation, Point

__all__ = [
    "AccessibilityGroundingStrategy",
    "CoordinateGroundingStrategy",
    "GroundingRouter",
    "GroundingStrategy",
    "RegionGroundingStrategy",
    "TextAnchorGroundingStrategy",
    "UnsupportedGroundingError",
]

#: Actions that never require a spatial target; the router grounds them trivially.
#: HOTKEY (compound chord) and FOCUS_WINDOW (window-title selector) are non-spatial too:
#: they carry ``keys`` / ``target`` instead of coordinates. MOVE is deliberately NOT
#: listed — point-bearing actions already route to the coordinate strategy automatically.
NON_SPATIAL_ACTIONS: frozenset[ActionType] = frozenset(
    {
        ActionType.TYPE,
        ActionType.KEYPRESS,
        ActionType.HOTKEY,
        ActionType.SCROLL,
        ActionType.WAIT,
        ActionType.DONE,
        ActionType.FOCUS_WINDOW,
    }
)


class UnsupportedGroundingError(Exception):
    """Raised fail-closed when a grounding strategy cannot resolve the action target.

    Attributes:
        strategy: Name of the strategy that refused (or ``"router"`` for router-level
            refusals such as "no strategy can ground this action").
        message: Human-readable explanation of why grounding is unsupported.
    """

    def __init__(self, strategy: str, message: str) -> None:
        super().__init__(f"[{strategy}] {message}")
        self.strategy = strategy
        self.message = message


@runtime_checkable
class GroundingStrategy(Protocol):
    """Protocol every grounding strategy implements.

    ``ground`` resolves the action's target against the observation and returns a
    :class:`~computer_use_mcp.models.GroundingResult`; strategies that *derive* a target
    location set ``action.point`` in place (always screenshot space — see the module
    docstring invariant). ``can_ground`` reports — without side effects —
    whether the strategy has the data it needs for this action/observation pair, so the
    router can select by capability availability.
    """

    @property
    def name(self) -> str:
        """Unique strategy name used in router hints and audit evidence."""
        ...

    def can_ground(self, action: GroundedAction, observation: Observation, target: str | None = None) -> bool:
        """Return True when this strategy has the data required to ground ``action``."""
        ...

    def ground(
        self,
        action: GroundedAction,
        observation: Observation,
        *,
        target: str | None = None,
    ) -> GroundingResult:
        """Resolve the target; raise :class:`UnsupportedGroundingError` when it cannot."""
        ...


def _describe_point(x: float, y: float) -> str:
    return f"({round(x)}, {round(y)})"


class CoordinateGroundingStrategy:
    """P0 real implementation: validate pixel coordinates against the observation.

    The model reports coordinates in *screenshot space* (it sees the screenshot of
    ``observation.width x observation.height``). This strategy bounds-checks the point
    in that space (and, when present — i.e. for drag — the ``to_point`` end point too)
    and, when the observation declares a verified scaled coordinate space
    (``coordinate_space`` is ``SCALED`` with scale factors), RECORDS the verified
    screenshot-to-input scale on ``GroundingResult`` (``normalized=True``) with full
    evidence. It does NOT rewrite ``action.point``: the point stays in screenshot space
    and the backend applies the single screenshot-to-physical transform
    (``origin + screenshot * scale``) exactly once at execution — see the module
    docstring invariant (F1: a pre-scaled point used to be scaled a second time by the
    backend, executing at ``origin + screenshot * scale**2``).

    Confidence: the decision's stated confidence is passed through unchanged and labeled —
    grounding confidence here is coordinate sanity, NOT model confidence.
    """

    @property
    def name(self) -> str:
        return "coordinate"

    def can_ground(self, action: GroundedAction, observation: Observation, target: str | None = None) -> bool:
        return (
            action.point is not None
            and observation.coordinate_space_verified
            and observation.coordinate_space is not CoordinateSpace.UNVERIFIABLE
        )

    def ground(
        self,
        action: GroundedAction,
        observation: Observation,
        *,
        target: str | None = None,
    ) -> GroundingResult:
        if action.point is None:
            raise UnsupportedGroundingError(self.name, "Action carries no point to ground.")
        if not observation.coordinate_space_verified or observation.coordinate_space is CoordinateSpace.UNVERIFIABLE:
            raise UnsupportedGroundingError(
                self.name,
                "Screenshot and input coordinate spaces are unverifiable; refusing to ground coordinates.",
            )

        x, y = action.point.x, action.point.y
        evidence = [
            f"Point {_describe_point(x, y)} bounds-checked against screenshot {observation.width}x{observation.height}."
        ]
        if not (0 <= x < observation.width and 0 <= y < observation.height):
            raise UnsupportedGroundingError(
                self.name,
                f"Point {_describe_point(x, y)} is outside the screenshot bounds "
                f"{observation.width}x{observation.height}.",
            )
        if action.to_point is not None:
            end_x, end_y = action.to_point.x, action.to_point.y
            evidence.append(
                f"End point {_describe_point(end_x, end_y)} bounds-checked against screenshot "
                f"{observation.width}x{observation.height}."
            )
            if not (0 <= end_x < observation.width and 0 <= end_y < observation.height):
                raise UnsupportedGroundingError(
                    self.name,
                    f"End point {_describe_point(end_x, end_y)} is outside the screenshot bounds "
                    f"{observation.width}x{observation.height}.",
                )

        scale_x = observation.coordinate_scale_x
        scale_y = observation.coordinate_scale_y
        if scale_x == 1.0 and scale_y == 1.0 and observation.coordinate_space is not CoordinateSpace.SCALED:
            evidence.append("Passthrough coordinate space; no scaling transform applies.")
            return GroundingResult(
                strategy=self.name,
                confidence=action.confidence,
                evidence=evidence,
                normalized=False,
                notes=(
                    "Coordinate sanity verified (bounds + space); confidence is the decision's "
                    "stated model confidence passed through, not a grounding quality score."
                ),
            )

        input_width = observation.input_width or round(observation.width * scale_x)
        input_height = observation.input_height or round(observation.height * scale_y)
        evidence.append(
            f"Verified scaled coordinate space: screenshot-to-input scale ({scale_x:.6g}, {scale_y:.6g}) "
            f"recorded; point {_describe_point(x, y)} stays in screenshot space "
            f"(input space {input_width}x{input_height})."
        )
        return GroundingResult(
            strategy=self.name,
            confidence=action.confidence,
            evidence=evidence,
            normalized=True,
            notes=(
                "Scaled coordinate space verified and its scale recorded; normalized=True means "
                "'validated with the scale recorded', NOT that the point was rewritten. The point "
                "stays in screenshot space and the backend applies the single screenshot-to-physical "
                "transform (origin + screenshot * scale) exactly once at execution. Confidence is the "
                "decision's stated model confidence passed through, not a grounding quality score."
            ),
        )


class RegionGroundingStrategy:
    """Visual region grounding: resolve an explicit rectangle descriptor to its center.

    The region descriptor is supplied via ``target`` either as a 4-tuple/sequence
    ``(left, top, width, height)`` (mss convention, matching ``MonitorInfo.bounds``) or as
    a string ``"left,top,width,height"``. The rectangle is bounds-checked against the
    screenshot and the action point becomes the region center. Without a region descriptor
    this strategy raises :class:`UnsupportedGroundingError` (P1 adds visual region search).
    """

    @property
    def name(self) -> str:
        return "region"

    @staticmethod
    def _parse_region(target: str | Sequence[float] | None) -> tuple[int, int, int, int] | None:
        if target is None:
            return None
        if isinstance(target, str):
            parts = [part.strip() for part in target.split(",")]
            if len(parts) != 4:
                return None
            try:
                left, top, width, height = (int(float(part)) for part in parts)
            except ValueError:
                return None
        else:
            try:
                values = [float(part) for part in target]
            except (TypeError, ValueError):
                return None
            if len(values) != 4:
                return None
            left, top, width, height = (int(value) for value in values)
        if width <= 0 or height <= 0:
            return None
        return left, top, width, height

    def can_ground(self, action: GroundedAction, observation: Observation, target: str | None = None) -> bool:
        return self._parse_region(target) is not None

    def ground(
        self,
        action: GroundedAction,
        observation: Observation,
        *,
        target: str | None = None,
    ) -> GroundingResult:
        region = self._parse_region(target)
        if region is None:
            raise UnsupportedGroundingError(
                self.name,
                "Region grounding requires a (left, top, width, height) descriptor via target.",
            )
        left, top, width, height = region
        center_x = left + width // 2
        center_y = top + height // 2
        if not (0 <= center_x < observation.width and 0 <= center_y < observation.height):
            raise UnsupportedGroundingError(
                self.name,
                f"Region center {_describe_point(center_x, center_y)} is outside the screenshot bounds "
                f"{observation.width}x{observation.height}.",
            )
        action.point = Point(x=center_x, y=center_y)
        return GroundingResult(
            strategy=self.name,
            confidence=0.9,
            evidence=[
                (
                    f"Region (left={left}, top={top}, width={width}, height={height}) resolved to center "
                    f"{_describe_point(center_x, center_y)} within screenshot "
                    f"{observation.width}x{observation.height}."
                )
            ],
            normalized=False,
            notes="Region descriptor accepted verbatim; visual region search arrives with P1 perception.",
        )


class TextAnchorGroundingStrategy:
    """OCR text-anchor grounding: match the semantic target against ``observation.ocr_text``.

    Matches the expected text (case-insensitive substring) against OCR regions and returns
    the matched region's center as the action point. Raises
    :class:`UnsupportedGroundingError` when the observation carries no OCR data (P1 fills
    ``Observation.ocr_text``) or when no region matches.
    """

    @property
    def name(self) -> str:
        return "text_anchor"

    def can_ground(self, action: GroundedAction, observation: Observation, target: str | None = None) -> bool:
        return bool(target) and bool(observation.ocr_text)

    def ground(
        self,
        action: GroundedAction,
        observation: Observation,
        *,
        target: str | None = None,
    ) -> GroundingResult:
        if not target:
            raise UnsupportedGroundingError(self.name, "Text-anchor grounding requires a semantic target string.")
        if not observation.ocr_text:
            raise UnsupportedGroundingError(
                self.name,
                "Observation carries no OCR text regions; text-anchor grounding is unavailable (P1 perception).",
            )
        needle = target.casefold()
        exact = [region for region in observation.ocr_text if region.text.casefold() == needle]
        partial = [
            region
            for region in observation.ocr_text
            if needle in region.text.casefold() or region.text.casefold() in needle
        ]
        candidates = exact or partial
        if not candidates:
            raise UnsupportedGroundingError(
                self.name, f"No OCR region matches the expected text {target!r}."
            )
        region = candidates[0]
        center_x = region.x + region.width // 2
        center_y = region.y + region.height // 2
        confidence = region.confidence if region.confidence is not None else 0.8
        action.point = Point(x=center_x, y=center_y)
        match_kind = "exact" if exact else "substring"
        return GroundingResult(
            strategy=self.name,
            confidence=confidence,
            evidence=[
                (
                    f"Expected text {target!r} matched OCR region {region.text!r} ({match_kind}) at "
                    f"(x={region.x}, y={region.y}, w={region.width}, h={region.height}); "
                    f"point set to center {_describe_point(center_x, center_y)}."
                )
            ],
            normalized=False,
            notes=(
                "Grounding confidence reflects the matched OCR region's confidence "
                "(grounding evidence quality), not the model's decision confidence."
            ),
        )


class AccessibilityGroundingStrategy:
    """Accessibility-tree grounding: find an element by name/role in ``observation.ui_elements``.

    Element schema is intentionally tolerant (``Observation.ui_elements`` is ``list[Any]``
    until the P1 accessibility integration pins it): dicts (or objects) exposing
    ``name``/``text``/``title``, optional ``role``/``type``/``control_type``, and bounds as
    a 4-sequence under ``bounds``/``rect`` (``(left, top, width, height)``) or flat
    ``x``/``y``/``width``/``height`` keys. Raises :class:`UnsupportedGroundingError` when
    the observation carries no UI elements, no element matches, or the matched element has
    no usable bounds.
    """

    @property
    def name(self) -> str:
        return "accessibility"

    @staticmethod
    def _element_field(element: Any, names: tuple[str, ...]) -> Any:
        for name in names:
            if isinstance(element, dict):
                if element.get(name) is not None:
                    return element[name]
            else:
                value = getattr(element, name, None)
                if value is not None:
                    return value
        return None

    @classmethod
    def _element_name(cls, element: Any) -> str:
        return str(cls._element_field(element, ("name", "text", "title")) or "")

    @classmethod
    def _element_bounds(cls, element: Any) -> tuple[int, int, int, int] | None:
        raw = cls._element_field(element, ("bounds", "rect"))
        if isinstance(raw, (tuple, list)) and len(raw) == 4:
            try:
                left, top, width, height = (int(value) for value in raw)
            except (TypeError, ValueError):
                return None
            if width > 0 and height > 0:
                return left, top, width, height
            return None
        left = cls._element_field(element, ("x", "left"))
        top = cls._element_field(element, ("y", "top"))
        width = cls._element_field(element, ("width", "w"))
        height = cls._element_field(element, ("height", "h"))
        if None in (left, top, width, height):
            return None
        try:
            l_i, t_i, w_i, h_i = int(left), int(top), int(width), int(height)
        except (TypeError, ValueError):
            return None
        if w_i > 0 and h_i > 0:
            return l_i, t_i, w_i, h_i
        return None

    def can_ground(self, action: GroundedAction, observation: Observation, target: str | None = None) -> bool:
        return bool(target) and bool(observation.ui_elements)

    def ground(
        self,
        action: GroundedAction,
        observation: Observation,
        *,
        target: str | None = None,
    ) -> GroundingResult:
        if not target:
            raise UnsupportedGroundingError(
                self.name, "Accessibility grounding requires a semantic target string."
            )
        if not observation.ui_elements:
            raise UnsupportedGroundingError(
                self.name,
                "Observation carries no accessibility elements; accessibility grounding is unavailable "
                "(P1 perception).",
            )
        needle = target.casefold()
        exact: list[Any] = []
        partial: list[Any] = []
        for element in observation.ui_elements:
            element_name = self._element_name(element)
            if not element_name:
                continue
            folded = element_name.casefold()
            if folded == needle:
                exact.append(element)
            elif needle in folded or folded in needle:
                partial.append(element)
        candidates = exact or partial
        if not candidates:
            raise UnsupportedGroundingError(
                self.name, f"No accessibility element matches the expected target {target!r}."
            )
        element = candidates[0]
        bounds = self._element_bounds(element)
        if bounds is None:
            raise UnsupportedGroundingError(
                self.name,
                f"Accessibility element matching {target!r} has no usable bounds; cannot derive a point.",
            )
        left, top, width, height = bounds
        center_x = left + width // 2
        center_y = top + height // 2
        role = self._element_field(element, ("role", "type", "control_type"))
        role_text = str(role) if role is not None else "unknown"
        confidence = self._element_field(element, ("confidence",))
        conf = float(confidence) if isinstance(confidence, (int, float)) else 0.8
        conf = min(max(conf, 0.0), 1.0)
        action.point = Point(x=center_x, y=center_y)
        match_kind = "exact" if exact else "substring"
        return GroundingResult(
            strategy=self.name,
            confidence=conf,
            evidence=[
                (
                    f"Target {target!r} matched accessibility element "
                    f"name={self._element_name(element)!r} role={role_text} ({match_kind}) with bounds "
                    f"(left={left}, top={top}, width={width}, height={height}); "
                    f"point set to center {_describe_point(center_x, center_y)}."
                )
            ],
            normalized=False,
            notes=(
                "Grounding confidence reflects the accessibility match quality "
                "(grounding evidence quality), not the model's decision confidence."
            ),
        )


class GroundingRouter:
    """Selects and applies a grounding strategy for an action.

    Selection order (master-mission section 6, Goal.md section 6):

    1. Explicit strategy hint (``strategy_hint``, e.g. from decision metadata) — the named
       strategy is used verbatim; if it cannot ground, :class:`UnsupportedGroundingError`
       propagates. The router NEVER silently falls back to coordinates for an explicitly
       requested non-coordinate strategy.
    2. Capability availability — the first strategy in preference order whose
       ``can_ground`` reports True: coordinate (when the action already carries a point),
       then accessibility, text anchor, region. A point-bearing action is always offered to
       the coordinate strategy first — even when the coordinate space is unverifiable — so
       its accurate fail-closed refusal (coordinate-space reason) propagates instead of the
       generic router fallback message.
    3. Coordinate default / trivial grounding — non-spatial actions (type, keypress,
       hotkey, scroll, wait, done, focus_window) without a target are grounded trivially
       with strategy ``"none"``; anything else that no strategy can ground raises
       :class:`UnsupportedGroundingError` (fail-closed).
    """

    def __init__(self, strategies: Sequence[GroundingStrategy] | None = None) -> None:
        self._strategies: list[GroundingStrategy] = list(strategies) if strategies is not None else [
            CoordinateGroundingStrategy(),
            AccessibilityGroundingStrategy(),
            TextAnchorGroundingStrategy(),
            RegionGroundingStrategy(),
        ]
        self._registry: dict[str, GroundingStrategy] = {
            strategy.name.casefold(): strategy for strategy in self._strategies
        }

    @property
    def strategies(self) -> list[GroundingStrategy]:
        return list(self._strategies)

    def route(
        self,
        action: GroundedAction,
        observation: Observation,
        *,
        strategy_hint: str | None = None,
        target: str | None = None,
    ) -> GroundingResult:
        """Ground ``action`` against ``observation``; fail-closed on any inability.

        Strategies that derive a target location update ``action.point`` in place
        (screenshot space); the coordinate strategy only validates and records the scale.
        Attach the returned result to ``action.grounding``.
        """
        if strategy_hint:
            strategy = self._registry.get(strategy_hint.casefold())
            if strategy is None:
                raise UnsupportedGroundingError(
                    "router",
                    f"Unknown grounding strategy hint {strategy_hint!r}; "
                    f"available: {sorted(self._registry)}.",
                )
            # Deliberate: a hint failure propagates — no silent fallback to coordinates.
            return strategy.ground(action, observation, target=target)

        # Preference 2a: the action already carries a point — coordinate grounding applies.
        # Deliberately attempted even when can_ground() refuses (unverifiable coordinate
        # space): point-bearing actions must receive the strategy's accurate fail-closed
        # refusal, not the generic router message (E6 finding D5).
        coordinate = self._registry.get("coordinate")
        if coordinate is not None and action.point is not None:
            return coordinate.ground(action, observation, target=target)

        for strategy in self._strategies:
            if strategy.name == "coordinate":
                continue
            if strategy.can_ground(action, observation, target):
                return strategy.ground(action, observation, target=target)

        if action.point is None and target is None and action.action in NON_SPATIAL_ACTIONS:
            return GroundingResult(
                strategy="none",
                confidence=action.confidence,
                evidence=[f"Action {action.action.value} is non-spatial; grounding not required."],
                normalized=False,
                notes="Trivial grounding for a non-spatial action; confidence is the decision's stated value.",
            )

        raise UnsupportedGroundingError(
            "router",
            "No grounding strategy can resolve this action: it has no point, no semantic target, "
            "and the observation carries no grounding data (OCR/accessibility).",
        )
