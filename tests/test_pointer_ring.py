"""DELTA 1 M9: PointerRing unit tests (supersede, linger, capacity, lock, drag filter).

The ring is the pure-Python press log behind the additive ``last_click`` /
``pressed_at`` consumers; these pins cover the behaviors the reply-level tests
cannot reach on a fake dispatch.
"""

from __future__ import annotations

import threading
import time

from computer_use_mcp.desktop.pointer_ring import (
    PRESS_LINGER_SECONDS,
    RING_CAPACITY,
    PointerRing,
)


class _FakeClock:
    def __init__(self) -> None:
        self._now = 1000.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds

    @property
    def now(self) -> float:
        return self._now


def test_last_click_returns_the_most_recent_completed_press() -> None:
    ring = PointerRing()
    assert ring.last_click() is None  # empty ring
    ring.record("down", 10, 20, "left")
    ring.record("up", 10, 20, "left")
    click = ring.last_click()
    assert click is not None
    assert (click["x"], click["y"], click["button"]) == (10, 20, "left")
    assert isinstance(click["t"], float)


def test_last_click_superseded_by_a_newer_press() -> None:
    ring = PointerRing()
    ring.record("down", 1, 1, "left")
    ring.record("up", 1, 1, "left")
    ring.record("down", 2, 2, "left")
    ring.record("up", 2, 2, "left")
    click = ring.last_click()
    assert click is not None and (click["x"], click["y"]) == (2, 2)
    # An UNCOMPLETED newer press does not erase the older completed one...
    ring.record("down", 3, 3, "left")
    click = ring.last_click()
    assert click is not None and (click["x"], click["y"]) == (2, 2)
    # ...until it completes.
    ring.record("up", 3, 3, "left")
    click = ring.last_click()
    assert click is not None and (click["x"], click["y"]) == (3, 3)


def test_last_click_excludes_drag_presses() -> None:
    """DELTA 1 MINOR 3: a drag's mouse_down/up is a real press but NOT a click —
    ``last_click`` reports click-type presses only."""
    ring = PointerRing()
    ring.record("down", 5, 5, "left", source="drag")
    ring.record("up", 50, 60, "left", source="drag")
    assert ring.last_click() is None
    # A click after the drag surfaces (and only it).
    ring.record("down", 7, 8, "left")
    ring.record("up", 7, 8, "left")
    click = ring.last_click()
    assert click is not None and (click["x"], click["y"]) == (7, 8)


def test_pressed_at_covers_the_press_window_and_lingers() -> None:
    clock = _FakeClock()
    ring = PointerRing(clock=clock)
    ring.record("down", 1, 1, "left")
    clock.advance(0.05)  # still held
    assert ring.pressed_at(clock.now) is True
    ring.record("up", 1, 1, "left")
    inside = clock.now
    clock.advance(PRESS_LINGER_SECONDS / 2)
    assert ring.pressed_at(clock.now) is True, "inside the 80 ms linger"
    clock.advance(PRESS_LINGER_SECONDS)  # past up + 80 ms
    assert ring.pressed_at(clock.now) is False
    assert ring.pressed_at(inside) is True  # a timestamp inside the window stays true


def test_pressed_at_open_press_counts_from_the_down() -> None:
    ring = PointerRing()
    ring.record("down", 2, 2, "left")
    assert ring.pressed_at(time.monotonic()) is True, "a held button is pressed"


def test_capacity_bounds_the_ring() -> None:
    ring = PointerRing()
    for i in range(RING_CAPACITY + 20):
        ring.record("down", i, i, "left")
        ring.record("up", i, i, "left")
    assert len(ring.events()) == RING_CAPACITY
    click = ring.last_click()
    assert click is not None and click["x"] == RING_CAPACITY + 19  # the newest survives


def test_concurrent_record_and_read_never_corrupts() -> None:
    ring = PointerRing()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for i in range(500):
                ring.record("down", i, i, "left")
                ring.record("up", i, i, "left")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(500):
                events = ring.events()
                assert all(event.kind in ("down", "up") for event in events)
                ring.last_click()
                ring.pressed_at(time.monotonic())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader), threading.Thread(target=reader)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    assert ring.last_click() is not None


def test_record_never_raises_and_never_breaks_dispatch() -> None:
    """DELTA 1 MINOR 1: a recording failure is swallowed inside the ring — it can
    never break a dispatch or skip a release."""

    class _BrokenClock:
        def __call__(self) -> float:
            raise RuntimeError("clock exploded")

    ring = PointerRing(clock=_BrokenClock())
    ring.record("down", 1, 1, "left")  # must not raise
    ring.record("up", 1, 1, "left")
    assert ring.events() == []
