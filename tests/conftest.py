"""Shared fixtures for the unit suite (no side effects at import time).

The ``RecordingEngine`` input stub lives in its own top-level module
(``tests/recording_engine.py``) because this tree is imported root-less and the module
name ``conftest`` is ambiguous between ``tests/`` and ``tests/e2e/``.
"""

from __future__ import annotations

from typing import Any

import pytest

import computer_use_mcp.backend as backend_module
from computer_use_mcp.backend import LocalComputerBackend


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    """DELTA 2 (WP6) regression guard: an un-awaited server tool coroutine is an ERROR.

    Since WP6 DELTA 1/F4 the wait-family tools and ``stop_session`` are ASYNC
    coroutines; a caller that forgets to await one (``server.stop_session(...)`` in
    sync code) silently leaves the session RUNNING forever and only shows up as a
    ``RuntimeWarning: coroutine ... was never awaited`` at garbage-collection time.
    Every test in the suite therefore escalates exactly that warning class to an
    error, so a sync caller missed anywhere (tests, benchmarks/e2e bridges) fails
    the suite instead of leaking a live session. Scoped to the never-awaited
    message so unrelated RuntimeWarnings keep their default handling here.
    """
    never_awaited_error = pytest.mark.filterwarnings(
        "error:coroutine.*was never awaited:RuntimeWarning"
    )
    for item in items:
        item.add_marker(never_awaited_error)


@pytest.fixture(scope="session")
def real_backend() -> LocalComputerBackend:
    """ONE real Windows backend per pytest process, shared across test modules.

    The process can declare its DPI awareness exactly once, so constructing
    ``LocalComputerBackend`` a second time in the same process degrades that instance
    (awareness fallback misreports, DPI marked estimated -> fail-closed space
    classification). Sharing a single session-scoped instance keeps the real-desktop
    stop-check smoke tests deterministic regardless of module collection order.
    Construction performs no window/input side effects beyond the one-time DPI
    awareness declaration, monitor enumeration, and the semantic-reader warm-up; tests
    that drive input inject a stubbed ``_engine`` (see ``tests/recording_engine.py``).
    """
    if not backend_module.IS_WINDOWS:
        pytest.skip("Real Win32 backend requires Windows.")
    return LocalComputerBackend()
