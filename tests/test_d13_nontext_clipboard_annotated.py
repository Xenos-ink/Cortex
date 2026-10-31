"""D·N1 / repair 3.3 — the action SAYS when a non-text clipboard was destroyed.

``_clipboard_read_text`` answers ``None`` for a clipboard holding no ``CF_UNICODETEXT``
(a copied image, a file drop, RTF-only) AND for a genuinely EMPTY or unreadable
clipboard.  The transport's write path then calls ``EmptyClipboard()`` and the restore
is skipped because ``prior is None`` — so the user's copied image is gone,
permanently, with a success-sounding message and no mention of it.  The repro (D13-7)
read:

    clipboard AFTER transport = 'PASTE-ME'
    message mentions the loss = False

The fix splits those answers at the producer (``_clipboard_read_verdict`` — the
three-way ``restorable`` / ``non_text`` / ``unknown``) and appends an annotation of the
SAME shape as the existing ``clipboard_restore=failed``, with its own distinct value.
This is the data-loss disclosure; it ships independently of the settle-ordering fix
(3.2), and it never claims a loss the backend cannot actually prove.
"""

from __future__ import annotations

import ctypes
from typing import Any

import pytest

import computer_use_mcp.backend as backend_module
from computer_use_mcp import backend as B
from computer_use_mcp.backend import (
    CLIPBOARD_RESTORE_DESTROYED,
    CLIPBOARD_RESTORE_FAILED,
    _clipboard_read_text,
    _clipboard_read_verdict,
)
from computer_use_mcp.core.models import ActionType, GroundedAction

# --- in-memory clipboard doubles ------------------------------------------------------------------


class ClipboardDouble:
    """The simulated OS clipboard: ``value`` is the content, ``None`` = no text format."""

    def __init__(self, value: str | None = "", *, fail_writes: set[str] | None = None) -> None:
        self.value = value
        self.ops: list[str] = []
        self.fail_writes = fail_writes or set()

    # the production primitive signatures
    def read_text(self) -> str | None:
        self.ops.append("READ")
        return self.value

    def verdict(self) -> tuple[str | None, str]:
        """F3/3.3: production saves through the three-way verdict, so the double does too."""
        self.ops.append("READ")
        return (self.value, "restorable" if self.value is not None else "non_text")

    def write_text(self, text: str) -> bool:
        self.ops.append("WRITE")
        if text in self.fail_writes:
            return False
        self.value = text
        return True


class LateReadingApp:
    """A paste consumer that reads the clipboard when it processes the chord."""

    def __init__(self, clipboard: ClipboardDouble) -> None:
        self.clipboard = clipboard
        self.pasted: list[str] = []
        self.control = ""

    def chord(self, keys: list[str]) -> None:
        self.clipboard.ops.append("CHORD")
        self.pasted.append(self.clipboard.value or "")
        self.control += self.pasted[-1]


class _Engine:
    def __init__(self, app: LateReadingApp) -> None:
        self._app = app

    def chord(self, keys: list[str]) -> None:
        self._app.chord(keys)

    def release_modifiers(self, keys: list[str]) -> None:  # pragma: no cover
        pass

    def type_text(self, text: str, before_chunk: Any = None) -> None:  # pragma: no cover
        raise AssertionError("the clipboard transport must not use the per-char engine")


def _run(
    text: str,
    clipboard: ClipboardDouble,
    *,
    blind: bool = False,
    read_verdict: Any = None,
) -> tuple[str, Exception | None]:
    """Drive the REAL ``LocalComputerBackend._execute_type_via_clipboard`` once.

    ``read_verdict`` overrides the producer verdict (the default derives it from
    ``clipboard.value``, which is the real distinction: a ``str`` is restorable, a
    ``None`` is a non-text payload).
    """
    app = LateReadingApp(clipboard)
    instance = object.__new__(B.LocalComputerBackend)
    instance._engine = _Engine(app)  # type: ignore[attr-defined]
    instance._last_key_dispatch = 0.0
    instance._last_focus_transition = 0.0
    instance._semantic_reader = None
    instance.read_focused_control_value = lambda: None if blind else app.control  # type: ignore[method-assign]
    # the real backend records whether the read-back had ANY readable target; a blind
    # reader is what makes the ``TYPE_UNCONFIRMED no-readable-target`` marker fire
    instance._last_read_had_target = not blind
    instance._apply_focus_settle = lambda: None  # type: ignore[method-assign]
    instance._apply_key_dispatch_gap = lambda keys: None  # type: ignore[method-assign]
    saved = (
        B._clipboard_read_text,
        B._clipboard_write_text,
        B._clipboard_read_verdict,
    )
    B._clipboard_read_text = clipboard.read_text  # type: ignore[assignment]
    B._clipboard_write_text = clipboard.write_text  # type: ignore[assignment]
    B._clipboard_read_verdict = read_verdict or clipboard.verdict  # type: ignore[assignment]
    raised: Exception | None = None
    message = ""
    try:
        message = instance._execute_type_via_clipboard(
            GroundedAction(action=ActionType.TYPE, text=text, via="clipboard", confidence=1.0),
            None,
            None,
        )
    except Exception as exc:  # noqa: BLE001 - a raise is a decisive observation
        raised = exc
    finally:
        (
            B._clipboard_read_text,
            B._clipboard_write_text,
            B._clipboard_read_verdict,
        ) = saved  # type: ignore[assignment]
    return message, raised


# --- the producer distinguishes "non-text" from "we could not tell" --------------------------------


def _patch_reader(
    monkeypatch: pytest.MonkeyPatch,
    *,
    openable: bool,
    available: bool,
    handle: int,
    text: str | None,
    count: int = 1,
) -> None:
    """Patch EVERY user32/kernel32 call the reader makes — including ``CloseClipboard``
    (DELTA 1 NIT: no real user32 call at all, which also makes these tests run on a
    non-Windows checkout where ``_user32``/``_kernel32`` are None)."""
    import types as _types

    monkeypatch.setattr(backend_module, "_clipboard_open", lambda: openable)
    if backend_module._user32 is None:
        monkeypatch.setattr(backend_module, "_user32", _types.SimpleNamespace())
    if backend_module._kernel32 is None:
        monkeypatch.setattr(backend_module, "_kernel32", _types.SimpleNamespace())
    monkeypatch.setattr(
        backend_module._user32, "IsClipboardFormatAvailable", lambda fmt: available
    )
    # DELTA 1 (VAL-WP4b MINOR): the empty-clipboard discriminator (0 formats = nothing
    # was on the clipboard; >0 without CF_UNICODETEXT = a non-text payload).
    monkeypatch.setattr(backend_module._user32, "CountClipboardFormats", lambda: count)
    monkeypatch.setattr(backend_module._user32, "GetClipboardData", lambda fmt: handle)
    monkeypatch.setattr(backend_module._user32, "CloseClipboard", lambda: 1)
    payload = ((text or "") + "\x00").encode("utf-16-le")
    monkeypatch.setattr(
        backend_module._kernel32,
        "GlobalLock",
        lambda ptr: ctypes.create_string_buffer(payload, len(payload)) if ptr.value else 0,
    )


def test_a_refused_open_is_unknown_not_a_known_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_reader(monkeypatch, openable=False, available=False, handle=0, text=None)
    assert _clipboard_read_verdict() == (None, "unknown")


def test_a_clipboard_without_the_text_format_is_non_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """A copied image / file drop / RTF-only: openable, formats present, but no
    ``CF_UNICODETEXT`` — a KNOWN non-text payload."""
    _patch_reader(monkeypatch, openable=True, available=False, handle=0, text=None, count=5)
    assert _clipboard_read_verdict() == (None, "non_text")


def test_a_truly_empty_clipboard_is_unknown_not_non_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """DELTA 1 (VAL-WP4b MINOR): ZERO formats means NOTHING was on the clipboard, so
    ``EmptyClipboard`` destroys nothing — the verdict is ``unknown`` (never a claimed
    ``non_text`` loss the code cannot prove)."""
    _patch_reader(monkeypatch, openable=True, available=False, handle=0, text=None, count=0)
    assert _clipboard_read_verdict() == (None, "unknown")


def test_a_missing_handle_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_reader(monkeypatch, openable=True, available=True, handle=0, text="hello")
    assert _clipboard_read_verdict() == (None, "unknown")


def test_a_readable_text_clipboard_is_restorable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_reader(monkeypatch, openable=True, available=True, handle=1, text="OLD")
    assert _clipboard_read_verdict() == ("OLD", "restorable")


def test_an_empty_string_is_still_a_restorable_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CF_UNICODETEXT`` holding ``""`` is a REAL payload — restored as ``""``."""
    _patch_reader(monkeypatch, openable=True, available=True, handle=1, text="")
    text, reason = _clipboard_read_verdict()
    assert reason == "restorable"
    assert text == ""


def test_the_legacy_reader_keeps_its_exact_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_clipboard_read_text`` still answers ``str | None``; nothing else changes."""
    _patch_reader(monkeypatch, openable=True, available=False, handle=0, text=None)
    assert _clipboard_read_text() is None


def test_the_annotation_vocabulary_is_disjoint() -> None:
    """A failed restore and a destroyed payload are different events."""
    assert CLIPBOARD_RESTORE_DESTROYED != CLIPBOARD_RESTORE_FAILED
    assert CLIPBOARD_RESTORE_DESTROYED.split("=")[0] == CLIPBOARD_RESTORE_FAILED.split("=")[0]
    assert "destroyed" in CLIPBOARD_RESTORE_DESTROYED


# --- the annotation rides the action message ----------------------------------------------------------


def test_a_destroyed_non_text_clipboard_is_annotated() -> None:
    clipboard = ClipboardDouble(None)  # no text format: a copied image

    message, raised = _run("PASTE-ME", clipboard)

    assert raised is None
    # The user's image is gone — and the action says so.
    assert clipboard.value == "PASTE-ME"
    assert CLIPBOARD_RESTORE_DESTROYED in message


def test_the_annotation_names_the_destruction_not_merely_a_failure() -> None:
    message, _raised = _run("PASTE-ME", ClipboardDouble(None))

    assert "destroyed" in message
    assert CLIPBOARD_RESTORE_FAILED not in message  # a DISTINCT value, not the old one


def test_an_empty_clipboard_is_not_annotated() -> None:
    """Nothing was lost: there was no payload to destroy."""
    message, raised = _run("PASTE-ME", ClipboardDouble(""))

    assert raised is None
    assert "clipboard_restore" not in message


def test_an_empty_string_clipboard_is_written_back_to_empty_string() -> None:
    """DELTA 1 (VAL-WP4b MINOR): a prior clipboard of ``""`` is a REAL restorable
    payload — the restore WRITE must happen (ops READ, WRITE, CHORD, WRITE) and the
    clipboard ends EMPTY again. Reverting the restore to ``if prior:`` (truthiness)
    would skip the write and fail this test."""
    clipboard = ClipboardDouble("")

    message, raised = _run("PASTE-ME", clipboard)

    assert raised is None
    assert clipboard.ops == ["READ", "WRITE", "CHORD", "WRITE"]
    assert clipboard.value == ""
    assert "clipboard_restore" not in message


def test_an_unknown_read_claims_no_destruction() -> None:
    """A refused read is an ABSENCE of knowledge — the backend must not claim a loss."""
    message, _raised = _run("PASTE-ME", ClipboardDouble(None), read_verdict=lambda: (None, "unknown"))

    assert CLIPBOARD_RESTORE_DESTROYED not in message


def test_a_normal_text_clipboard_is_not_annotated() -> None:
    message, _raised = _run("PASTE-ME", ClipboardDouble("OLD-USER-TEXT"))

    assert "clipboard_restore" not in message
    assert "integrity=verified(8/8)" in message


def test_a_failed_restore_keeps_its_own_annotation() -> None:
    clipboard = ClipboardDouble("OLD-USER-TEXT", fail_writes={"OLD-USER-TEXT"})

    message, _raised = _run("PASTE-ME", clipboard)

    assert CLIPBOARD_RESTORE_FAILED in message
    assert CLIPBOARD_RESTORE_DESTROYED not in message


def test_the_annotation_appears_on_the_unverified_branch_too() -> None:
    """A blind target returns the ``unverified`` row — the loss is still disclosed."""
    message, _raised = _run("PASTE-ME", ClipboardDouble(None), blind=True)

    assert CLIPBOARD_RESTORE_DESTROYED in message
    assert "TYPE_UNCONFIRMED" in message


def test_an_empty_type_action_still_short_circuits_before_the_clipboard() -> None:
    """The pre-existing empty-text contract: no clipboard churn at all."""
    clipboard = ClipboardDouble(None)

    message, raised = _run("", clipboard)

    assert raised is None
    assert clipboard.ops == []
    assert CLIPBOARD_RESTORE_DESTROYED not in message


def test_the_non_text_transport_still_pastes_the_intended_text() -> None:
    """The disclosure does not change what the target receives."""
    clipboard = ClipboardDouble(None)

    message, _raised = _run("PASTE-ME", clipboard)

    assert clipboard.ops == ["READ", "WRITE", "CHORD"]
    assert clipboard.value == "PASTE-ME"
    assert CLIPBOARD_RESTORE_DESTROYED in message


def test_the_restored_clipboard_is_untouched_for_a_text_payload() -> None:
    clipboard = ClipboardDouble("OLD-USER-TEXT")

    _run("PASTE-ME", clipboard)

    assert clipboard.ops == ["READ", "WRITE", "CHORD", "WRITE"]
    assert clipboard.value == "OLD-USER-TEXT"
