"""Interruptible waits, hold slicing, and the deterministic motion-path builders."""

from __future__ import annotations

import math
import time

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation-only names (lazy under __future__ annotations)
    from ..core.state import StopToken

WAIT_MAX_SECONDS = 10.0
WAIT_SLICE_SECONDS = 0.1
#: WP2 (F3 fix 5.4, C11/C·N3): the additive ``waited_seconds`` marker emitted in the
#: wait action's message ONLY when the request was outside ``[0, WAIT_MAX_SECONDS]``
#: and was therefore clamped.
WAIT_CLAMPED_MARKER = "waited_seconds"
_DRAG_SEGMENT_PIXELS = 40  # stroke interpolation granularity (~one segment per 40 px)
_DRAG_MIN_SEGMENTS = 4  # even tiny drags get a visibly interpolated stroke
_DRAG_STEP_PAUSE_SECONDS = 0.01  # per-segment pacing (same cadence as type)


def interruptible_wait(seconds: float, stop: StopToken | None = None) -> float:
    """Sleep sliced into 100 ms slices, aborting early when ``stop`` fires.

    Returns the number of seconds actually waited (slice-granular when interrupted).
    Never raises: a fired stop simply ends the wait early; callers check the token.
    """
    if seconds <= 0:
        return 0.0
    if stop is None:
        time.sleep(seconds)
        return seconds
    remaining = seconds
    waited = 0.0
    while remaining > 0:
        slice_seconds = min(WAIT_SLICE_SECONDS, remaining)
        if stop.wait(slice_seconds):
            return waited + slice_seconds
        waited += slice_seconds
        remaining -= slice_seconds
    return waited


def _sleep_for_wait_action(total_seconds: float, stop: StopToken) -> None:
    """Slice-sleep for ``wait`` actions; raises ``TaskStopped`` as soon as stop fires."""
    remaining = total_seconds
    while remaining > 0:
        stop.ensure_live()
        slice_seconds = min(WAIT_SLICE_SECONDS, remaining)
        if stop.wait(slice_seconds):
            stop.ensure_live()  # stop fired mid-slice: raise immediately
        remaining -= slice_seconds


def _wait_clamp_note(requested: float, effective: float) -> str:
    """The additive ``waited_seconds`` suffix, or ``""`` when the clamp did not bite.

    WP2 (F3 fix 5.4, C11 / C·N3). Emitted ONLY when the request was outside
    ``[0, WAIT_MAX_SECONDS]`` and was therefore clamped — an in-range ``delta`` produces
    the byte-identical ``Executed wait.`` it always did, so this costs a host nothing on
    the common path and tells a caller that asked for 15 s (and got 10) what it got.
    The leading space keeps the composed message a single space-separated suffix chain.
    """
    try:
        asked = float(requested)
        got = float(effective)
    except (TypeError, ValueError):  # pragma: no cover - delta is a validated number
        return ""
    if asked < 0.0 or asked > WAIT_MAX_SECONDS:
        return f" {WAIT_CLAMPED_MARKER}={got:g}"
    return ""


#: WP2 (ORVEX-2026-101 Batch 1, P1.3): hold-wait slice. Finer than the ``wait``-action
#: slice (0.1 s) so a stop arriving mid-hold is honored within <= one 50 ms slice
#: (the documented 25-50 ms band); ``stop.wait`` also wakes the instant the stop
#: event arms, so the slice bounds only the worst case, not the common path.
HOLD_SLICE_SECONDS = 0.05


def _sleep_for_hold(hold_seconds: float, stop: StopToken) -> None:
    """Slice-sleep for a held click (``hold_ms``); raises ``TaskStopped`` as soon as
    the stop fires — the button is released by the caller's ``finally`` either way."""
    remaining = hold_seconds
    while remaining > 0:
        stop.ensure_live()
        slice_seconds = min(HOLD_SLICE_SECONDS, remaining)
        if stop.wait(slice_seconds):
            stop.ensure_live()  # stop fired mid-slice: raise immediately
        remaining -= slice_seconds


def _wait_for_hold(hold_seconds: float, stop: StopToken | None) -> None:
    """The ONE hold wait every held action uses (click, key, and the fake alike).

    WP2 (ORVEX-2026-107): the click branch's inline ``stop is None`` fork, hoisted so a
    held KEY is not a second copy of it. The sliced ``_sleep_for_hold`` above is reused
    verbatim (D4) — a bare uninterruptible ``time.sleep`` is only correct when there is
    no token to honour, which is exactly what the caller passing ``None`` asserts.
    """
    if stop is None:
        time.sleep(hold_seconds)
    else:
        _sleep_for_hold(hold_seconds, stop)


def _drag_segment_points(
    start: tuple[int, int], end: tuple[int, int], *, step_pixels: int = 0
) -> list[tuple[int, int]]:
    """Interpolated stroke waypoints from ``start`` to ``end`` (inclusive).

    Segments are ~``_DRAG_SEGMENT_PIXELS`` px long (Chebyshev distance) with a floor of
    ``_DRAG_MIN_SEGMENTS`` so even short drags draw a smooth stroke. The final waypoint
    is exactly ``end``. Shared by the real backend's interpolated drag mode (and the
    fake backend's simulated cursor walk, so both stroke identically).

    ``step_pixels`` (L1-NEW-2) tightens the granularity when the engine asks for a
    dense stroke — drawing apps sample held-button WM_MOUSEMOVE events, so ~40 px
    hops leave dotted/broken lines; ``step_pixels=8`` yields ~8 px per waypoint.
    ``step_pixels <= 0`` keeps the legacy ~40 px geometry unchanged.
    """
    distance = max(abs(end[0] - start[0]), abs(end[1] - start[1]))
    if step_pixels > 0:
        segments = distance // step_pixels + (1 if distance % step_pixels else 0)
    else:
        segments = -(-distance // _DRAG_SEGMENT_PIXELS)
    segments = max(_DRAG_MIN_SEGMENTS, segments)
    return [
        (
            round(start[0] + (end[0] - start[0]) * index / segments),
            round(start[1] + (end[1] - start[1]) * index / segments),
        )
        for index in range(1, segments + 1)
    ]


# --- WP2 (ORVEX-2026-101 Batch 2, P2.2): the deterministic timed-motion path builder ---------
# An ADDITION next to the drag builder — not one line of the drag behavior above is
# touched. Pure, deterministic, zero randomness (the repository carries no `random` in
# src/): the same inputs always produce the same point list, byte-identically.

#: Motion shapes (D6): the linear target equation plus the quadratic Bezier family.
_MOTION_SHAPES: tuple[str, ...] = ("linear", "quadratic_bezier")
#: Motion easings (D20): the identity and the closed-form smoothstep polynomial.
_MOTION_EASINGS: tuple[str, ...] = ("linear", "smoothstep")
#: Hard cap on returned points per motion. The segment count is capped one below so
#: the inclusive start sample never pushes the list past 128 points.
_MOTION_MAX_POINTS = 128
#: Two-slice floor: even the shortest timed motion gets start/mid/end.
_MOTION_MIN_SEGMENTS = 2


def _motion_path_points(
    start: tuple[int, int],
    end: tuple[int, int],
    duration_ms: int,
    shape: str = "linear",
    bend_px: int = 0,
    easing: str = "linear",
    target_interval_ms: int = 12,
    bounds: tuple[int, int, int, int] | None = None,
) -> list[tuple[int, int]]:
    """Deterministic timed-motion waypoints from ``start`` to ``end`` (inclusive).

    WP2 (ORVEX-2026-101 Batch 2, P2.2); the formulas:

    - linear geometry: ``x(u') = x0 + (x1-x0)·u'``; the simple bend adds
      ``y(u') += 4h·u'(1-u')`` — the peak displacement is exactly ``h`` at the
      midpoint (``u'=0.5``) and zero at both ends; ``h > 0`` displaces toward ``+y``
      (screen-down; decision D19).
    - quadratic Bezier: ``P(u') = (1-u')²P0 + 2(1-u')u'C + u'²P1`` — the control
      point ``C`` is derived internally from ``h`` (``C = mid + 2h·n̂``, ``n̂`` the
      unit normal chosen with a non-negative y-component), so ``B(0.5) = mid + h·n̂``:
      the peak magnitude is exactly ``|h|``. No control-point parameter exists on any
      surface.
    - easing re-schedules TIME only (D20): ``u' = easing(u)`` with ``linear`` the
      identity and ``smoothstep = 3u²-2u³``; the geometric curve is unchanged.
    - sampling: ``segments = clamp(ceil(duration_ms / target_interval_ms), 2, 127)``
      -> 3..128 points (two-slice floor, 128-point cap). The FIRST sample is
      ``start`` exactly and the LAST is ``end`` exactly (the drag builder's
      final-point discipline): arrival accuracy is independent of shape/easing.
    - screen envelope (decision 5): when ``bounds=(left, top, width, height)`` is
      supplied in PHYSICAL virtual-screen pixels, ``bend_px`` is silently reduced
      (never rejected) to the largest same-sign value whose complete sampled path
      remains inside that rectangle. The reduction is a bounded search over the
      declared room, not a fixed pixel cap. Endpoints remain exact and take
      precedence if a caller supplies an already out-of-screen endpoint.

    Arguments are validated fail-closed (garbage raises ``ValueError``, never a
    silently degraded path); ``start == end`` returns ``[]`` (the zero-length
    ``_drag_waypoints`` precedent); ``duration_ms <= 0`` raises — the caller only
    invokes this function on the timed branch (the legacy jump never calls it).
    All coordinates are PHYSICAL virtual-screen pixels: every path point is computed
    AFTER the single ``_map_to_physical`` transform — no scaling inside the engine.
    """
    if not isinstance(start, tuple) or not isinstance(end, tuple) or len(start) != 2 or len(end) != 2:
        raise ValueError("start and end must be (x, y) tuples")
    for name, value in (("start", start), ("end", end)):
        for component in value:
            if isinstance(component, bool) or not isinstance(component, int):
                raise ValueError(  # noqa: TRY004 - mirrors the models' ValueError style
                    f"{name} coordinates must be integers; got {component!r}"
                )
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms <= 0:
        raise ValueError(f"duration_ms must be a positive integer; got {duration_ms!r}")
    if isinstance(bend_px, bool) or not isinstance(bend_px, int):
        raise ValueError(  # noqa: TRY004 - mirrors the models' ValueError style
            f"bend_px must be an integer; got {bend_px!r}"
        )
    if (
        isinstance(target_interval_ms, bool)
        or not isinstance(target_interval_ms, int)
        or target_interval_ms <= 0
    ):
        raise ValueError(
            f"target_interval_ms must be a positive integer; got {target_interval_ms!r}"
        )
    if shape not in _MOTION_SHAPES:
        allowed = " or ".join(f'"{value}"' for value in _MOTION_SHAPES)
        raise ValueError(f"shape must be {allowed}; received {shape!r}")
    if easing not in _MOTION_EASINGS:
        allowed = " or ".join(f'"{value}"' for value in _MOTION_EASINGS)
        raise ValueError(f"easing must be {allowed}; received {easing!r}")
    if bounds is not None and (
        not isinstance(bounds, (tuple, list))
        or len(bounds) != 4
        or any(isinstance(value, bool) or not isinstance(value, int) for value in bounds)
        or bounds[2] <= 0
        or bounds[3] <= 0
    ):
        raise ValueError(
            "bounds must be a (left, top, width, height) tuple of positive integers"
        )
    if start == end:
        return []

    # Decision 5: the screen is the only bend envelope. A bounded binary search over
    # the same-sign bend magnitude finds the largest value whose *complete sampled*
    # path fits the physical rectangle. This is deliberately independent of a fixed
    # pixel ceiling and works for both the simple bend and the quadratic control hull.
    if bounds is not None and bend_px:
        left, top, width, height = bounds
        right, bottom = left + width, top + height

        def _fits(candidate: int) -> bool:
            candidate_points = _motion_path_points(
                start,
                end,
                duration_ms,
                shape=shape,
                bend_px=candidate,
                easing=easing,
                target_interval_ms=target_interval_ms,
                bounds=None,
            )
            return all(
                left <= point_x < right and top <= point_y < bottom
                for point_x, point_y in candidate_points
            )

        if not _fits(bend_px):
            sign = 1 if bend_px > 0 else -1
            low, high = 0, abs(bend_px)
            while low < high:
                middle = (low + high + 1) // 2
                if _fits(sign * middle):
                    low = middle
                else:
                    high = middle - 1
            bend_px = sign * low

    x0, y0 = start
    x1, y1 = end
    dx = x1 - x0
    dy = y1 - y0
    length = math.hypot(dx, dy)

    def _ease(u: float) -> float:
        if easing == "linear":
            return u
        return 3.0 * u * u - 2.0 * u * u * u  # smoothstep (closed form, deterministic)

    def _point(u: float) -> tuple[int, int]:
        eased = _ease(u)
        if shape == "quadratic_bezier":
            if length > 0:
                normal_x, normal_y = -dy / length, dx / length
                if normal_y < 0:  # D19: keep the normal whose y-component points +y
                    normal_x, normal_y = -normal_x, -normal_y
                # ny == 0 (a perfectly vertical move): the only normal is horizontal —
                # kept as-is (deterministic); the peak magnitude is |h| either way.
                control_x = (x0 + x1) / 2 + 2 * bend_px * normal_x
                control_y = (y0 + y1) / 2 + 2 * bend_px * normal_y
            else:
                control_x, control_y = float(x0), float(y0)
            one_minus = 1.0 - eased
            x = (
                one_minus * one_minus * x0
                + 2 * one_minus * eased * control_x
                + eased * eased * x1
            )
            y = (
                one_minus * one_minus * y0
                + 2 * one_minus * eased * control_y
                + eased * eased * y1
            )
        else:
            x = x0 + dx * eased
            y = y0 + dy * eased
            if bend_px:
                y += 4.0 * bend_px * eased * (1.0 - eased)
        return (round(x), round(y))

    segments = -(-duration_ms // target_interval_ms)  # ceil(duration/interval)
    segments = max(_MOTION_MIN_SEGMENTS, min(segments, _MOTION_MAX_POINTS - 1))
    points = [_point(index / segments) for index in range(segments + 1)]
    points[0] = (x0, y0)  # start exact: 0.0 px at the path origin
    points[-1] = (x1, y1)  # end exact: 0.0 px arrival, independent of shape/easing
    return points
