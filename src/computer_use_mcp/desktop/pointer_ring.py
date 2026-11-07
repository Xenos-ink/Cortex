"""Per-backend pointer-event ring (ORVEX-2026-109 WP-C2, DELTA 1 MINOR 1-3).

A pure-Python, lock-protected ring of the last ``RING_CAPACITY`` pointer events
(``down`` / ``up``), recorded in PHYSICAL virtual-screen coordinates where the
backend dispatches clicks / press-release primitives. It records NOTHING by
itself: the callers (:mod:`computer_use_mcp.desktop.backend`,
``fake_backend``) hook it at the existing dispatch sites, after the engine call
returns, so dispatch timing and order are byte-identical to the pre-WP-C
behavior (the ring adds no syscall and never blocks dispatch).

DELTA 1 safety and fidelity:

* ``record`` NEVER raises (every failure is swallowed inside the ring), so a
  recording failure can never break a dispatch or skip a release;
* a plain click's ``down`` edge is recorded with the timestamp taken BEFORE
  ``engine.click`` dispatches (``record(..., at=...)``), so ``pressed_at``
  covers the real press window;
* drag presses carry ``source="drag"`` and are EXCLUDED from ``last_click()``
  — the field reports click-type presses (click / right_click /
  double_click) only.

Consumers (WP-C3): ``last_click()`` feeds the additive ``last_click`` reply
field, and ``pressed_at()`` classifies a capture timestamp as inside a press
window (``[down, up + PRESS_LINGER_SECONDS]``). A recorder thread may read the
ring concurrently with dispatch — every read snapshots under the same lock.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

#: How long after an ``up`` a timestamp still counts as "inside the press"
#: (named constant per WP-C2; the 80 ms linger covers the encode delay between
#: the physical release and the post-action capture).
PRESS_LINGER_SECONDS = 0.080

#: Ring depth: 64 events covers every pointer dispatch of a full flat call
#: (click batch + follow_ups) several times over.
RING_CAPACITY = 64

#: Where a press came from: a click-type action (``click`` / ``right_click`` /
#: ``double_click``) or the press/release pair that starts/ends a drag. Only
#: ``click`` presses surface in ``last_click()`` (DELTA 1 MINOR 3).
SOURCE_CLICK = "click"
SOURCE_DRAG = "drag"


@dataclass(frozen=True)
class PointerEvent:
    """One pointer edge, in PHYSICAL virtual-screen coordinates."""

    t_monotonic: float
    kind: str  # "down" | "up"
    x: int
    y: int
    button: str
    source: str = SOURCE_CLICK  # "click" | "drag"


class PointerRing:
    """Bounded, thread-safe ring of :class:`PointerEvent` records."""

    def __init__(
        self,
        capacity: int = RING_CAPACITY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._events: deque[PointerEvent] = deque(maxlen=max(1, int(capacity)))
        self._clock = clock

    def record(
        self,
        kind: str,
        x: int,
        y: int,
        button: str,
        *,
        at: float | None = None,
        source: str = SOURCE_CLICK,
    ) -> None:
        """Append one pointer edge. NEVER raises — a recording failure can
        never break a dispatch or skip a button release (DELTA 1 MINOR 1).
        ``at`` pins the edge's timestamp (a plain click's ``down`` is recorded
        with the moment BEFORE ``engine.click`` dispatched, DELTA 1 MINOR 2)."""
        try:
            event = PointerEvent(
                t_monotonic=self._clock() if at is None else float(at),
                kind="down" if kind == "down" else "up",
                x=int(x),
                y=int(y),
                button=str(button),
                source=SOURCE_DRAG if source == SOURCE_DRAG else SOURCE_CLICK,
            )
            with self._lock:
                self._events.append(event)
        except Exception:  # noqa: BLE001 - the ring must never break dispatch
            pass

    def events(self) -> list[PointerEvent]:
        with self._lock:
            return list(self._events)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def last_click(self) -> dict[str, object] | None:
        """The most recent completed CLICK-type press: ``{x, y, button, t}`` or
        ``None`` (DELTA 1 MINOR 3: drag presses are excluded).

        A press is (down, later up) of the SAME button with no intervening
        down of that button. ``t`` is the DOWN event's ``time.monotonic``.
        """
        events = self.events()
        for i in range(len(events) - 1, -1, -1):
            down = events[i]
            if down.kind != "down" or down.source != SOURCE_CLICK:
                continue
            for later in events[i + 1 :]:
                if later.kind == "down" and later.button == down.button:
                    break  # a newer press supersedes this one
                if later.kind == "up" and later.button == down.button:
                    return {
                        "x": down.x,
                        "y": down.y,
                        "button": down.button,
                        "t": down.t_monotonic,
                    }
        return None

    def pressed_at(self, t: float) -> bool:
        """True when ``t`` falls in ``[down, up + PRESS_LINGER_SECONDS]`` of a press.

        A press whose ``up`` never arrived (button still held) counts from its
        ``down`` onward — a held button is a pressed button. Drag presses count
        here (a held button is held regardless of source); only ``last_click``
        filters them out.
        """
        events = self.events()
        for i, down in enumerate(events):
            if down.kind != "down":
                continue
            up_t: float | None = None
            superseded = False
            for later in events[i + 1 :]:
                if later.kind == "down" and later.button == down.button:
                    superseded = True
                    break
                if later.kind == "up" and later.button == down.button:
                    up_t = later.t_monotonic
                    break
            if superseded:
                continue
            if up_t is None:
                if t >= down.t_monotonic:
                    return True
            elif down.t_monotonic <= t <= up_t + PRESS_LINGER_SECONDS:
                return True
        return False
