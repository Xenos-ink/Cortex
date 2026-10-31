"""Win32 clipboard transport: bind, open, read verdict, read/write text."""

from __future__ import annotations

import ctypes
import time

from .win32 import IS_WINDOWS, _kernel32, _user32


#: Win32 clipboard format for a UTF-16 string allocation (winuser.h).
_CF_UNICODETEXT = 13
#: GlobalAlloc flag for a movable (system-relocatable) allocation, the SetClipboardData norm.
_GMEM_MOVEABLE = 0x0002
#: Fixed internal OpenClipboard retry pacing: another process legitimately holding the
#: clipboard open is transient (clipboard viewers, office apps). Deliberately NOT an env
#: knob (no new timing knobs) and only ever reached on the via="clipboard" branch — the
#: default sendinput path pays zero cost.
_CLIPBOARD_OPEN_ATTEMPTS = 10
_CLIPBOARD_OPEN_RETRY_SECONDS = 0.01

_CLIPBOARD_ARGTYPES_BOUND = False


def _clipboard_bind() -> bool:
    """Bind the win32 clipboard prototypes once (64-bit-safe handle types); False off-Windows.

    Handle-returning functions (GlobalAlloc/GlobalLock/GetClipboardData) get
    ``c_void_p`` restypes so a >2^31 handle value can never be sign-truncated; the
    binding is idempotent and bounded to the clipboard path's first use.
    """
    global _CLIPBOARD_ARGTYPES_BOUND
    if not IS_WINDOWS or _user32 is None or _kernel32 is None:
        return False
    if _CLIPBOARD_ARGTYPES_BOUND:
        return True
    _user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    _user32.OpenClipboard.restype = ctypes.c_int
    _user32.CloseClipboard.argtypes = []
    _user32.CloseClipboard.restype = ctypes.c_int
    _user32.EmptyClipboard.argtypes = []
    _user32.EmptyClipboard.restype = ctypes.c_void_p
    _user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
    _user32.IsClipboardFormatAvailable.restype = ctypes.c_int
    _user32.GetClipboardData.argtypes = [ctypes.c_uint]
    _user32.GetClipboardData.restype = ctypes.c_void_p
    _user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    _user32.SetClipboardData.restype = ctypes.c_void_p
    _kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = ctypes.c_void_p
    _kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    _kernel32.GlobalUnlock.restype = ctypes.c_int
    _kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    _kernel32.GlobalFree.restype = ctypes.c_void_p
    _CLIPBOARD_ARGTYPES_BOUND = True
    return True


def _clipboard_open() -> bool:
    """Open the clipboard for this (unowned) thread with bounded retries; never raises."""
    if not _clipboard_bind():
        return False
    for attempt in range(_CLIPBOARD_OPEN_ATTEMPTS):
        try:
            if _user32.OpenClipboard(None):
                return True
        except (OSError, AttributeError, ValueError):
            return False
        if attempt + 1 < _CLIPBOARD_OPEN_ATTEMPTS:
            time.sleep(_CLIPBOARD_OPEN_RETRY_SECONDS)
    return False


#: D13 / repair 3.3 restore-discipline annotation values (additive action-message
#: suffixes; the ``Executed type.`` prefix and the ``integrity=`` suffix stay stable).
#: The first is a restore that was ATTEMPTED and failed. The second is a payload that
#: was DESTROYED because it could not be saved in the first place (the clipboard held
#: a non-text payload — a copied image, a file drop, RTF — which the transport's
#: ``EmptyClipboard`` destroyed). The second is a data-loss disclosure, so it must
#: never be reported under the first value.
CLIPBOARD_RESTORE_FAILED = "clipboard_restore=failed"
CLIPBOARD_RESTORE_DESTROYED = "clipboard_restore=non_text_destroyed"

#: The three ways a clipboard save can end (:func:`_clipboard_read_verdict`).
#: ``restorable`` — text is known and will be restored. ``non_text`` — the clipboard
#: OPENED and held no ``CF_UNICODETEXT``, so the next ``EmptyClipboard`` destroys a
#: non-text payload (a copied image, a file drop, RTF): a KNOWN loss, disclosed.
#: ``unknown`` — the open was refused or the read failed: an ABSENCE of knowledge, and
#: an absence of knowledge is never reported as a known loss.
CLIPBOARD_RESTORABLE = "restorable"
CLIPBOARD_NON_TEXT = "non_text"
CLIPBOARD_UNKNOWN = "unknown"


def _clipboard_read_verdict() -> tuple[str | None, str]:
    """Read the current clipboard AND say which of the three cases it is.

    D13 / repair 3.3 (ORVEX-2026-108 WP4b). The pre-fix reader answered ``None`` for
    BOTH "there is no text format here" and "the open/read failed", and the
    transport's write path calls ``EmptyClipboard()`` either way — so a user's copied
    image was destroyed silently. Splitting the answer is what lets the action
    DISCLOSE the loss instead of hiding it behind a success-sounding message.

    Returns ``(text, reason)``:

    * ``(str, "restorable")`` — text read (possibly ``""``; an empty string is a REAL
      restorable payload, not an absent one);
    * ``(None, "non_text")`` — the clipboard opened and has no ``CF_UNICODETEXT``;
    * ``(None, "unknown")`` — the open was refused, the handle was missing, or the
      read raised. Never claims a loss it cannot prove.
    """
    if not _clipboard_open():
        return (None, CLIPBOARD_UNKNOWN)
    try:
        try:
            if not _user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
                # DELTA 1 (VAL-WP4b MINOR): distinguish a truly EMPTY clipboard (no
                # formats AT ALL — nothing was on it, so ``EmptyClipboard`` destroys
                # nothing) from a NON-TEXT payload (a copied image, a file drop, RTF
                # — a KNOWN loss, disclosed). An empty clipboard is an absence of
                # payload, not a destroyed one: reporting ``non_text`` there would
                # claim a loss the code cannot prove.
                if not _user32.CountClipboardFormats():
                    return (None, CLIPBOARD_UNKNOWN)
                return (None, CLIPBOARD_NON_TEXT)
            handle = _user32.GetClipboardData(_CF_UNICODETEXT)
            if not handle:
                return (None, CLIPBOARD_UNKNOWN)
            pointer = _kernel32.GlobalLock(ctypes.c_void_p(handle))
            if not pointer:
                return (None, CLIPBOARD_UNKNOWN)
            try:
                return (ctypes.wstring_at(pointer), CLIPBOARD_RESTORABLE)
            finally:
                _kernel32.GlobalUnlock(ctypes.c_void_p(handle))
        except (OSError, AttributeError, ValueError):
            return (None, CLIPBOARD_UNKNOWN)
    finally:
        try:
            _user32.CloseClipboard()
        except (OSError, AttributeError, ValueError):
            pass


def _clipboard_read_text() -> str | None:
    """Best-effort CF_UNICODETEXT read of the CURRENT clipboard; ``None`` on ANY failure.

    Used ONLY for the save half of the clipboard transport's restore discipline: a
    ``None`` here means "nothing restorable is known" (non-text content, open refused,
    read failed) and the restore step is skipped silently — a failed SAVE must never
    fail or even annotate the action.

    D13 / repair 3.3: the transport now calls :func:`_clipboard_read_verdict` instead,
    which splits the two ``None``s. This function keeps its EXACT contract (and is
    what every non-transport caller uses) — it is a thin projection of the verdict.
    """
    return _clipboard_read_verdict()[0]


def _clipboard_write_text(text: str) -> bool:
    """Replace the clipboard with ``text`` as CF_UNICODETEXT; ``True`` on success.

    Sequence: encode ONCE -> open (bounded retries) -> EmptyClipboard (takes
    ownership) -> GlobalAlloc a ``(UTF-16 code units + 1) * 2``-byte buffer WITH its
    terminating NUL -> SetClipboardData (the system OWNS the handle on success; on
    failure WE still own it and GlobalFree it before closing). Any failure returns
    ``False``; never raises.

    F-01 (RED-1, fixed): the size counts UTF-16 CODE UNITS, never Python characters —
    an astral char (emoji, CJK Ext-B) is TWO wchars, so a character-counted allocation
    shipped the block WITHOUT its terminating NUL and pasted text silently lost its
    final character (identical corruption on the restore path). The encode runs BEFORE
    the clipboard is opened: a text that cannot be represented as UTF-16 (a lone
    surrogate raises ``UnicodeEncodeError``) is refused with ``False`` while the user's
    current clipboard content stays untouched, and the SAME encoded bytes are allocated
    and copied (single source of truth for both the size and the payload).
    """
    try:
        payload = text.encode("utf-16-le") + b"\x00\x00"
    except (OSError, ValueError):  # UnicodeEncodeError: refused BEFORE any clipboard op
        return False
    if not _clipboard_open():
        return False
    try:
        try:
            _user32.EmptyClipboard()
            size = len(payload)  # == (len(text.encode('utf-16-le')) // 2 + 1) * 2
            handle = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, size)
            if not handle:
                return False
            pointer = _kernel32.GlobalLock(ctypes.c_void_p(handle))
            if not pointer:
                _kernel32.GlobalFree(ctypes.c_void_p(handle))
                return False
            try:
                ctypes.memmove(pointer, payload, size)
            finally:
                _kernel32.GlobalUnlock(ctypes.c_void_p(handle))
            if not _user32.SetClipboardData(_CF_UNICODETEXT, ctypes.c_void_p(handle)):
                _kernel32.GlobalFree(ctypes.c_void_p(handle))  # ownership stayed with us
                return False
            return True
        except (OSError, AttributeError, ValueError):
            return False
    finally:
        try:
            _user32.CloseClipboard()
        except (OSError, AttributeError, ValueError):
            pass
