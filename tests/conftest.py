"""Shared fixtures for the unit suite (no side effects at import time).

The ``RecordingEngine`` input stub lives in its own top-level module
(``tests/recording_engine.py``) because this tree is imported root-less and the module
name ``conftest`` is ambiguous between ``tests/`` and ``tests/e2e/``.
"""

from __future__ import annotations

import pytest

import computer_use_mcp.backend as backend_module
from computer_use_mcp.backend import LocalComputerBackend


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
