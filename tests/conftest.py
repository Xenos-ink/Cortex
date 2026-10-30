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

    N3 (VAL-WP6-02, strengthened in WP4b — the guard is now REAL at both layers, not
    just enforced by the gate's ``-W error::RuntimeWarning``):

    * an in-test call raises the RuntimeWarning directly → ``error`` makes it fail the
      test on the spot;
    * a gc-time warning (raised inside ``Coroutine.__del__`` when the filter above
      escalates it) is reported by pytest as ``PytestUnraisableExceptionWarning`` —
      the second filter escalates exactly that unraisable path to an error as well,
      so a forgotten await fails whichever test is live when the coroutine is
      collected, and never silently passes here.

    Scoped to the never-awaited message classes so unrelated unraisables and
    RuntimeWarnings keep their default handling.
    """
    never_awaited_error = pytest.mark.filterwarnings(
        "error:coroutine.*was never awaited:RuntimeWarning",
        "error:.*never awaited.*:pytest.PytestUnraisableExceptionWarning",
    )
    for item in items:
        item.add_marker(never_awaited_error)


def pytest_warning_recorded(
    warning_message: "warnings.WarningMessage", when: str, nodeid: str | None = None
) -> None:
    """N3 (VAL-WP6-02, WP4b): the belt-and-suspenders layer of the DELTA 2 guard.

    A gc-time ``RuntimeWarning: coroutine ... was never awaited`` (a forgotten await
    on an async tool such as ``stop_session``) does not always fire inside a test's
    warning-capture window — the gc may collect the coroutine between phases, where
    the per-test filter above is NOT active. This hook sees EVERY warning pytest
    records (including those unraisable-path re-reports), so a never-awaited
    coroutine anywhere in the run fails the run here, at any phase.
    """
    category = getattr(warning_message, "category", None)
    category_name = getattr(category, "__name__", "")
    text = str(getattr(warning_message, "message", ""))
    never_awaited = (
        category_name == "RuntimeWarning" and "was never awaited" in text
    ) or (
        # the gc-time path: pytest re-reports the escalated RuntimeWarning as a
        # PytestUnraisableExceptionWarning whose text embeds the original message
        category_name == "PytestUnraisableExceptionWarning" and "never awaited" in text
    )
    if never_awaited:
        if nodeid:
            # An in-test leak: the per-item ``filterwarnings`` error (above) already
            # fails THAT test cleanly — never mask it with a hook-level abort.
            return
        raise AssertionError(
            "DELTA 2/N3 guard: an un-awaited coroutine leaked outside any test's "
            f"warning window (gc-time collection, when={when!r}) — a server tool "
            f"coroutine was never awaited: {text}"
        )


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
