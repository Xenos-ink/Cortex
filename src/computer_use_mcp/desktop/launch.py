"""Launch-needle validation, launch-path mapping, and attach-window title heuristics."""

from __future__ import annotations

import os
import re

from typing import TYPE_CHECKING

from .errors import LaunchTargetError

if TYPE_CHECKING:  # annotation-only names (lazy under __future__ annotations)
    from ..core.models import WindowInfo


#: Generic unsaved-document title conventions for attach-or-launch discovery (T8):
#: leading tokens apps give brand-new unsaved docs + restore-suffixed file patterns
#: (``<name>.xlsx1`` windows Excel restores beside a crashed session). Title
#: heuristics only — DATA, never per-app CODE (A12 mechanism ii).
UNSAVED_DOC_LEADING_TOKENS: frozenset[str] = frozenset(
    {"book", "untitled", "document", "presentation", "workbook", "sheet", "drawing", "image"}
)
_UNSAVED_RESTORE_SUFFIX = re.compile(
    r"\.(xlsx|xlsm|xls|docx|doc|pptx|ppt|txt|csv|rtf|png|jpg|jpeg|bmp|one|odt|ods)\d+(\.|$)",
    re.IGNORECASE,
)

#: REM-C (V-2 F3): the launch-needle allowlist — alphanumerics, dot, underscore,
#: space, hyphen ONLY. Everything else (path separators ``/`` ``\\``, ``:`` drive
#: syntax, quotes, cmd.exe metacharacters ``& | > < % ^ ( ) ; , = ! @ # $ * + ? [ ] { } ` ~``
#: and other punctuation) is a typed rejection BEFORE anything spawns.
LAUNCH_NEEDLE_PATTERN = re.compile(r"^[A-Za-z0-9._ -]+$")


def validate_launch_needle(needle: str) -> str:
    """Validate an ``ensure_app`` launch needle; return it unchanged when valid.

    Accepts exactly the process-name charset ``^[A-Za-z0-9._ -]+$`` (alphanumerics,
    dot, underscore, space, hyphen) so no path, quote, or cmd.exe metacharacter can
    ride through to a spawn; anything else raises :class:`LaunchTargetError`
    (fail-closed, before any process is created).
    """
    if not isinstance(needle, str) or not LAUNCH_NEEDLE_PATTERN.match(needle):
        raise LaunchTargetError(needle if isinstance(needle, str) else repr(needle))
    return needle


#: v0.7.1 (Defect B, field Blender case): optional needle->executable mapping for
#: explicitly-authorized ``ensure_app`` launches of GUI executables that are NOT on
#: PATH (they install under ``C:\\Program Files\\...`` and ``shutil.which`` never
#: resolves them — the field tester got a silent bare NO_INSTANCE and nothing
#: spawned). Value shape: semicolon-separated ``needle=path`` pairs, e.g.
#: ``CORTEX_LAUNCH_PATHS="blender=C:\Program Files\Blender Foundation\Blender 4.2\blender.exe"``.
#: Design-level and generic: any app, no hardcoded names anywhere.
LAUNCH_PATHS_ENV = "CORTEX_LAUNCH_PATHS"


def parse_launch_paths(raw: str | None) -> dict[str, str]:
    """Parse ``CORTEX_LAUNCH_PATHS`` (``needle=path;needle=path``) fail-safe.

    Returns ``{casefolded needle: path}`` for the whole-needle, case-insensitive
    lookup in :func:`_mapped_launch_path`. EVERY malformed entry is skipped
    silently-safe — never fatal, and a malformed entry can never spawn anything:

    - entry without ``=`` (or an empty needle / empty path) -> skipped;
    - the NEEDLE side must pass :func:`validate_launch_needle` (same charset gate
      as the launch itself — no path separators, quotes, or shell metacharacters;
      ``=`` and ``;`` are outside the charset, so the first ``=`` partition and the
      ``;`` split are unambiguous for any valid needle) -> else skipped;
    - the PATH side must exist as a FILE right now (``os.path.isfile`` — a missing
      path or a directory is skipped, never spawned); one surrounding quote pair is
      stripped because quoted paths are the common hand-written form;
    - a duplicated needle: the LAST valid entry wins.

    Parsed at use-site (``_launch_process`` entry — the launch path is not hot);
    zero cost added to any dispatch path.
    """
    mapping: dict[str, str] = {}
    if not isinstance(raw, str) or not raw.strip():
        return mapping
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        needle, sep, path = entry.partition("=")
        needle = needle.strip()
        path = path.strip()
        if not sep or not needle or not path:
            continue
        if len(path) >= 2 and path[0] == path[-1] and path[0] in ('"', "'"):
            path = path[1:-1].strip()
        if not path:
            continue
        try:
            validate_launch_needle(needle)
        except LaunchTargetError:
            continue  # malformed needle side: skipped fail-safe
        if not os.path.isfile(path):
            continue  # the path side must be an existing FILE
        mapping[needle.casefold()] = path
    return mapping


def _mapped_launch_path(process_needle: str) -> str | None:
    """The ``CORTEX_LAUNCH_PATHS`` mapped executable for ``process_needle``, or ``None``.

    Whole-needle, case-insensitive match (``"blender"`` maps ``"BLENDER"`` too, but
    ``"blender.exe"`` is a DIFFERENT needle — map the exact needle you drive). The
    parser is fail-safe: malformed entries never resolve, never spawn, never raise.
    """
    return parse_launch_paths(os.environ.get(LAUNCH_PATHS_ENV)).get(process_needle.casefold())


def _launch_unresolved_suffix(exc: BaseException, *, resolved: bool) -> str:
    """Typed NO_INSTANCE suffix for an authorized launch that produced no process.

    v0.7.1 (Defect B) honesty contract: a charset-valid needle that missed every
    resolution step reports ``launch_unresolved=path-lookup-missed`` (the raw-name
    spawn's FileNotFoundError IS the "not found anywhere" evidence), while a
    resolved target whose spawn raised reports the real OS error via
    ``launch_unresolved=spawn-failed (<ExcType>: <msg[:120]>)``. The driver-supplied
    needle is never embedded in either suffix (path-lookup-missed is fully static;
    spawn-failed's bounded OS text may echo the local operator-configured target).
    """
    from .interference import (
        format_launch_unresolved_path_miss,
        format_launch_unresolved_spawn_failed,
    )

    if not resolved and isinstance(exc, FileNotFoundError):
        return format_launch_unresolved_path_miss()
    return format_launch_unresolved_spawn_failed(exc)


class AppWindowCandidate:
    """One visible top-level window of a process, for attach-or-launch discovery (T8).

    ``doc_token`` is the leading segment of the title before the ``" - "`` app suffix
    (``"Book1 - Excel"`` -> ``"Book1"``); ``unsaved_candidate`` flags generic
    unsaved-document title conventions (leading "Book"/"Untitled"/... tokens or a
    restore-suffixed ``<file>.xlsx1`` pattern). Title heuristics only — DATA, never
    per-app code (A12 mechanism ii).
    """

    __slots__ = ("doc_token", "unsaved_candidate", "window")

    def __init__(self, window: WindowInfo, doc_token: str | None, unsaved_candidate: bool) -> None:
        self.window = window
        self.doc_token = doc_token
        self.unsaved_candidate = unsaved_candidate

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"AppWindowCandidate(title={self.window.title!r}, doc_token={self.doc_token!r}, "
            f"unsaved_candidate={self.unsaved_candidate!r})"
        )


def doc_token_from_title(title: str) -> str | None:
    """Leading segment of a window title before the ``" - "`` app suffix, or the title.

    ``"Book1 - Excel"`` -> ``"Book1"``; ``"t1_source.txt - Notepad"`` -> ``"t1_source.txt"``;
    a title without the suffix is its own doc token. Empty titles yield ``None``.
    """
    text = (title or "").strip()
    if not text:
        return None
    return text.split(" - ", 1)[0].strip() or None


def is_unsaved_candidate_title(title: str) -> bool:
    """True when a title matches generic unsaved/restore conventions (heuristic flag)."""
    token = doc_token_from_title(title)
    if token is None:
        return False
    first_word = token.split(" ", 1)[0].casefold()
    stripped = first_word.rstrip("0123456789")  # "book1" -> "book", "untitled" -> "untitled"
    if first_word in UNSAVED_DOC_LEADING_TOKENS or stripped in UNSAVED_DOC_LEADING_TOKENS:
        return True
    return bool(_UNSAVED_RESTORE_SUFFIX.search(token))
