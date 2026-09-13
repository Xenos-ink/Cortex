"""R-03: ``ensure_app`` reattach never builds an empty-title focus call (A7b fix).

The reattach path used to call ``focus_window_title(window.title)`` with whatever
title the resolved instance carried — an EMPTY title constructed an invalid
``focus_window`` action that surfaced as a raw validation error instead of a focused
reattach. The fix: the focus call is built only from a non-empty title (the resolver
prefers the top-of-Z-order TITLED match) and a typed :class:`WindowFocusError` is
raised when no matched instance can name a focus target (loud, structured failure —
the agent's failure handling turns it into a normal error outcome, never a crash).

Both backend implementations are pinned: :class:`~computer_use_mcp.backend.
FakeComputerBackend` (the test parity surface) and :class:`~computer_use_mcp.backend.
LocalComputerBackend` (the real body, exercised via a probe-only instance the same way
the B11 ownership tests do).
"""

from __future__ import annotations

from typing import Any

import pytest

from computer_use_mcp.backend import (
    AppWindowCandidate,
    FakeComputerBackend,
    LocalComputerBackend,
    WindowFocusError,
)
from computer_use_mcp.models import WindowInfo

TITLED = WindowInfo(hwnd=11, pid=100, process_name="excel.exe", window_class="XLMAIN", title="Book1 - Excel")
UNTITLED = WindowInfo(hwnd=12, pid=100, process_name="excel.exe", window_class="XLMAIN", title="")
SECOND_TITLED = WindowInfo(hwnd=13, pid=100, process_name="excel.exe", window_class="XLMAIN", title="Report - Excel")


def _candidate(window: WindowInfo, doc_token: str | None = None, unsaved: bool = False) -> AppWindowCandidate:
    return AppWindowCandidate(window=window, doc_token=doc_token, unsaved_candidate=unsaved)


class _EnsureBackend(FakeComputerBackend):
    """Fake backend with an injected candidate population and a focus-call recorder."""

    def __init__(self, candidates: list[AppWindowCandidate], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.app_windows = candidates
        self.focus_calls: list[str] = []

    def focus_window_title(self, title: str) -> str:
        self.focus_calls.append(title)
        return f"Focused window '{title}'."


def test_reattach_skips_empty_title_and_focuses_the_titled_instance() -> None:
    """Mixed candidates (untitled first in Z-order): the TITLED one is focused; the
    reattach completes with NO validation error and a non-empty REATTACHED title."""
    backend = _EnsureBackend([_candidate(UNTITLED), _candidate(TITLED)])
    message = backend.ensure_app("excel")
    assert message.startswith("REATTACHED")
    assert "title='Book1 - Excel'" in message
    assert backend.focus_calls == ["Book1 - Excel"]  # never the empty title
    assert "hwnd=11" in message


def test_all_empty_titles_raise_the_typed_error_and_never_focus() -> None:
    backend = _EnsureBackend([_candidate(UNTITLED), _candidate(UNTITLED.model_copy(update={"hwnd": 99}))])
    with pytest.raises(WindowFocusError) as excinfo:
        backend.ensure_app("excel")
    assert "empty window title" in str(excinfo.value)
    assert backend.focus_calls == []  # no focus call was ever built


def test_titled_document_match_still_reattaches() -> None:
    """The doc-token reattach (``excel|book1``) keeps its title-resolution contract."""
    backend = _EnsureBackend([_candidate(TITLED, doc_token="Book1")])
    message = backend.ensure_app("excel|book1")
    assert message.startswith("REATTACHED") and "title='Book1 - Excel'" in message
    assert backend.focus_calls == ["Book1 - Excel"]


def test_empty_title_matches_only_with_doc_needle_fall_to_ambiguous() -> None:
    """A doc needle can never match an empty title; with only untitled candidates left
    unmatched the probe degrades to AMBIGUOUS_INSTANCE (the driver decides)."""
    backend = _EnsureBackend([_candidate(UNTITLED)])
    message = backend.ensure_app("excel|book1")
    assert message.startswith("AMBIGUOUS_INSTANCE")
    assert backend.focus_calls == []


def test_z_order_first_titled_match_wins_over_later_titled() -> None:
    backend = _EnsureBackend([_candidate(UNTITLED), _candidate(SECOND_TITLED), _candidate(TITLED)])
    message = backend.ensure_app("excel")
    assert "title='Report - Excel'" in message  # FIRST titled in Z-order wins
    assert backend.focus_calls == ["Report - Excel"]


def test_real_backend_body_matches_fake_parity(monkeypatch: Any) -> None:
    """The LocalComputerBackend ensure_app body behaves identically (probe-only
    instance, injected enumeration + focus recorder — no live desktop needed)."""
    backend = LocalComputerBackend.__new__(LocalComputerBackend)
    calls: list[str] = []

    def _enumerate(process_name: str) -> list[AppWindowCandidate]:
        return [_candidate(UNTITLED), _candidate(TITLED)]

    def _focus(title: str) -> str:
        calls.append(title)
        return f"Focused window '{title}'."

    monkeypatch.setattr(backend, "enumerate_app_windows", _enumerate)
    monkeypatch.setattr(backend, "focus_window_title", _focus)
    message = backend.ensure_app("excel")
    assert message.startswith("REATTACHED") and "title='Book1 - Excel'" in message
    assert calls == ["Book1 - Excel"]


def test_real_backend_typed_error_when_only_untitled_match(monkeypatch: Any) -> None:
    backend = LocalComputerBackend.__new__(LocalComputerBackend)
    calls: list[str] = []

    def _enumerate(process_name: str) -> list[AppWindowCandidate]:
        return [_candidate(UNTITLED)]

    def _focus(title: str) -> str:
        calls.append(title)
        return f"Focused window '{title}'."

    monkeypatch.setattr(backend, "enumerate_app_windows", _enumerate)
    monkeypatch.setattr(backend, "focus_window_title", _focus)
    with pytest.raises(WindowFocusError) as excinfo:
        backend.ensure_app("excel")
    assert "empty window title" in str(excinfo.value)
    assert calls == []
