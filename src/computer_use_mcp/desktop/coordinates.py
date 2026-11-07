"""Coordinate-space classification and the single screenshot-to-physical transform."""

from __future__ import annotations

from dataclasses import dataclass

from typing import TYPE_CHECKING

from ..core.models import CoordinateSpace

if TYPE_CHECKING:  # annotation-only names (lazy under __future__ annotations)
    from ..core.models import MonitorInfo, Observation

_SCALE_TOLERANCE = 0.02


@dataclass(frozen=True)
class CoordinateTransform:
    """Maps screenshot-local pixels to/from physical virtual-screen pixels for one monitor.

    ``physical = origin + screenshot * scale`` where ``origin`` is the monitor's
    virtual-screen ``(left, top)`` and ``scale`` is input pixels per screenshot pixel.
    This is the ONLY screenshot-to-physical transform in the pipeline (see module
    docstring invariant): it is owned by the backend and applied exactly once at
    execution. Values are rounded to whole pixels (round-half-even, matching
    ``int(round(...))``).
    """

    origin_x: int = 0
    origin_y: int = 0
    scale_x: float = 1.0
    scale_y: float = 1.0

    def to_physical(self, x: float, y: float) -> tuple[int, int]:
        """Convert screenshot-local pixels to physical virtual-screen pixels."""
        return (
            round(self.origin_x + x * self.scale_x),
            round(self.origin_y + y * self.scale_y),
        )

    def to_screenshot(self, x: float, y: float) -> tuple[int, int]:
        """Convert physical virtual-screen pixels to screenshot-local pixels."""
        return (
            round((x - self.origin_x) / self.scale_x),
            round((y - self.origin_y) / self.scale_y),
        )

    @classmethod
    def from_observation(cls, observation: Observation) -> CoordinateTransform:
        """Build the transform recorded on an observation; identity when no monitor."""
        monitor = observation.monitor
        if monitor is None:
            return cls()
        return cls(
            origin_x=monitor.bounds[0],
            origin_y=monitor.bounds[1],
            scale_x=observation.coordinate_scale_x,
            scale_y=observation.coordinate_scale_y,
        )


@dataclass(frozen=True)
class CoordinateVerdict:
    """Result of coordinate-space integrity classification for one capture."""

    space: CoordinateSpace
    scale_x: float
    scale_y: float
    input_width: int
    input_height: int


def classify_coordinate_space(
    screenshot_width: int,
    screenshot_height: int,
    monitor: MonitorInfo,
    *,
    dpi_estimated: bool = False,
) -> CoordinateVerdict:
    """Classify screenshot-vs-input coordinate integrity for a captured monitor.

    Measurement first: screenshot dims equal to the monitor's physical dims means
    ``VERIFIED_PASSTHROUGH``. Otherwise a uniform ratio matching the monitor's queried
    per-monitor DPI scale (within tolerance, DPI not estimated) means ``SCALED`` with the
    measured scale stored. Everything else is ``UNVERIFIABLE`` (fail closed).
    """
    monitor_width, monitor_height = monitor.bounds[2], monitor.bounds[3]
    if monitor_width <= 0 or monitor_height <= 0:
        return CoordinateVerdict(
            space=CoordinateSpace.UNVERIFIABLE,
            scale_x=1.0,
            scale_y=1.0,
            input_width=max(screenshot_width, 1),
            input_height=max(screenshot_height, 1),
        )
    input_width, input_height = monitor_width, monitor_height
    if screenshot_width == input_width and screenshot_height == input_height:
        return CoordinateVerdict(
            space=CoordinateSpace.VERIFIED_PASSTHROUGH,
            scale_x=1.0,
            scale_y=1.0,
            input_width=input_width,
            input_height=input_height,
        )
    scale_x = input_width / screenshot_width
    scale_y = input_height / screenshot_height
    uniform = abs(scale_x - scale_y) <= _SCALE_TOLERANCE
    matches_dpi = uniform and not dpi_estimated and all(
        abs(measured - expected) <= max(_SCALE_TOLERANCE, abs(expected) * _SCALE_TOLERANCE)
        for measured, expected in (
            (scale_x, monitor.dpi_scale_x),
            (scale_y, monitor.dpi_scale_y),
        )
    )
    if matches_dpi:
        return CoordinateVerdict(
            space=CoordinateSpace.SCALED,
            scale_x=scale_x,
            scale_y=scale_y,
            input_width=input_width,
            input_height=input_height,
        )
    return CoordinateVerdict(
        space=CoordinateSpace.UNVERIFIABLE,
        scale_x=scale_x,
        scale_y=scale_y,
        input_width=input_width,
        input_height=input_height,
    )
