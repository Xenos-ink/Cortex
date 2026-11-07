"""Backend error hierarchy raised by the desktop modules."""

from __future__ import annotations



class BackendError(RuntimeError):
    """Base class for backend failures the controller can classify (Goal.md section 8)."""


class InputBlockedError(BackendError):
    """Physical input was blocked (pyautogui failsafe corner, blocked desktop, fake block).

    Recovery mapping hint: ``FailureClass.BLOCKED_UI``.
    """


class DisplayUnavailableError(BackendError):
    """Screen capture or monitor enumeration failed.

    Recovery mapping hint: ``FailureClass.UNRECOVERABLE`` (or ``APP_CRASH`` when
    transient capture failures repeat).
    """


class CoordinateSpaceError(BackendError):
    """Coordinate-space integrity could not be verified, so coordinate input was refused.

    Recovery mapping hint: re-observe and re-ground (``FailureClass.STALE_COORDINATES``).
    """


class UnsupportedActionError(BackendError):
    """The backend received an action type it cannot execute."""


class UnsupportedKeyError(ValueError):
    """Z2 (LIVE-WP13-02 sess_138fbd60): a keypress/hotkey names a key the
    engine cannot map — an unknown key name, or a character the ACTIVE
    keyboard layout cannot produce (e.g. ``"'"`` under an Arabic layout).

    A typed REFUSAL, not an operation error: it is raised BEFORE any input is
    dispatched (every key is mapped before the first key-down/chord event), so
    nothing reached the machine and the pipeline converts it into a
    ``rejected`` reply naming each unsupported key. ``ValueError`` base keeps
    the historical ``pytest.raises(ValueError)`` contract.
    """

    def __init__(self, unsupported_keys: list[str]) -> None:
        self.unsupported_keys = list(unsupported_keys)
        shown = ", ".join(repr(key) for key in self.unsupported_keys)
        super().__init__(
            f"unsupported keyboard character or key name: {shown} — a printable"
            ' character belongs on action="type" (text="..."); key names are'
            ' case-insensitive (e.g. "ctrl", "a", "enter", "esc", "f4")'
        )


class WindowFocusError(BackendError):
    """A focus_window action could not resolve or activate the requested window.

    Deliberately NOT mapped into any recovery name-set: it classifies as
    ``FailureClass.UNKNOWN`` (bounded, fail-closed) like any unrecognized backend
    failure. Documented residual: the previously-focused window is not restored on a
    refusal — the caller re-observes and re-decides from actual state.
    """


class FocusDriftError(BackendError):
    """Keyboard focus moved away from the session target mid-dispatch (T8 mechanism iv).

    Raised by a ``type`` action's per-chunk focus hook when the focused control stops
    belonging to the bound window: the in-flight ``type`` is aborted immediately (no
    further chunks are dispatched — text must never land in a foreign field). Carries
    the FOCUS_DRIFTED event payload for the controller to surface verbatim.

    ORVEX-2026-108 §1.1 K-05: when the abort happened mid-chunked-type, the raiser
    also sets ``delivered_chars`` (int) — the number of characters already dispatched
    in earlier chunks before the abort — so the reply can carry
    ``partial_delivery.delivered_chars``. ``None``/absent means "nothing was
    dispatched"; the agent only publishes the partial-delivery block when the count
    is a positive int.
    """

    #: Chars actually dispatched before the abort (set by the chunked type path).
    delivered_chars: int | None = None


class TextIntegrityError(BackendError):
    """Typed text failed chunk-integrity verification and one verified retype did not
    restore it (R-04).

    Raised ONLY after the read-back (focused-control value via the existing
    UIA/``WM_GETTEXT`` path) showed a dropped suffix, the missing suffix was re-dispatched
    once, and the re-read STILL disagrees with the expected accumulated text. A second
    repair attempt would be blind (it re-sends text without knowing why the first landed
    short — the B3/B8 double-submit doctrine forbids that), so the action fails honestly
    with the landed-vs-expected evidence instead. The target's text content is left
    EXACTLY as observed — no deletion or cursor surgery is attempted (app-agnostic
    repair is out of scope by design). Recovery hint: re-observe, then let the DRIVER
    decide (retype in place, clear the field first, or abandon).
    """


class LaunchTargetError(BackendError):
    """An ``ensure_app`` launch target (process needle) failed launch validation.

    REM-C (V-2 F3/B7.b): ``_launch_process`` once spawned the needle through
    ``subprocess.Popen([needle], shell=True)``, handing a quote-breakout needle
    (``x" & victim.bat``) to cmd.exe. Validation now runs BEFORE any spawn and
    rejects everything outside ``^[A-Za-z0-9._ -]+`` (no path separators, colons,
    quotes, or cmd.exe metacharacters) — a hostile target never reaches a process
    and surfaces as ``launch_rejected=LaunchTargetError`` in the NO_INSTANCE
    payload instead of spawning.
    """

    def __init__(self, needle: str) -> None:
        self.needle = needle
        super().__init__(
            f"launch target {needle!r} failed validation; only process-name "
            "characters [A-Za-z0-9._ -] are allowed (no paths, quotes, or "
            "shell metacharacters)"
        )

