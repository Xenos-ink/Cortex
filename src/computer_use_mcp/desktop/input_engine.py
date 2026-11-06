"""Pluggable physical input engines: raw SendInput (ctypes) and the pyautogui fallback."""

from __future__ import annotations

import ctypes
import time

from abc import ABC, abstractmethod

from typing import TYPE_CHECKING

from .errors import FocusDriftError, InputBlockedError, UnsupportedActionError, UnsupportedKeyError
from .motion import _DRAG_STEP_PAUSE_SECONDS

if TYPE_CHECKING:  # annotation-only names (lazy under __future__ annotations)
    from collections.abc import Callable
from .win32 import (
    IS_WINDOWS,
    _INPUT,
    _INPUT_KEYBOARD,
    _INPUT_MOUSE,
    _INPUT_UNION,
    _KEYBDINPUT,
    _KEYEVENTF_KEYUP,
    _KEYEVENTF_UNICODE,
    _MOUSEEVENTF_ABSOLUTE,
    _MOUSEEVENTF_LEFTDOWN,
    _MOUSEEVENTF_LEFTUP,
    _MOUSEEVENTF_MIDDLEDOWN,
    _MOUSEEVENTF_MIDDLEUP,
    _MOUSEEVENTF_MOVE,
    _MOUSEEVENTF_RIGHTDOWN,
    _MOUSEEVENTF_RIGHTUP,
    _MOUSEEVENTF_VIRTUALDESK,
    _MOUSEEVENTF_WHEEL,
    _MOUSEINPUT,
    _SM_CXVIRTUALSCREEN,
    _SM_CYVIRTUALSCREEN,
    _SM_XVIRTUALSCREEN,
    _SM_YVIRTUALSCREEN,
    _user32,
)

from .windows import get_cursor_position

_TYPE_INTERVAL_SECONDS = 0.01  # legacy per-char pacing (pyautogui fallback parity)

# --- input engine configuration (env-overridable; PERF-004) ---------------------------------
INPUT_BACKEND_ENV = "CORTEX_INPUT_BACKEND"  # "sendinput" (default) | "pyautogui"
PYAUTOGUI_PAUSE_ENV = "CORTEX_PYAUTOGUI_PAUSE"  # fallback-path PAUSE (default 0.0)
TYPE_INTERVAL_ENV = "CORTEX_TYPE_INTERVAL"  # typing pacing (backend/SendInput default 0.02 s/char since WP12a; 0 = unpaced)
DRAG_INTERPOLATE_ENV = "CORTEX_DRAG_INTERPOLATE"  # "0" restores the minimal teleport
# stroke on the DEFAULT engine (the interpolated stroke ships as the factory default
# since L1-NEW-2: freehand drawing apps sample held-button mouse moves — a
# press/teleport/release stroke paints only dots, not a continuous line)
DRAG_STEP_PAUSE_ENV = "CORTEX_DRAG_STEP_PAUSE"  # per-segment drag pacing (seconds)
DRAG_STEP_PIXELS_ENV = "CORTEX_DRAG_STEP_PIXELS"  # drag stroke granularity (pixels)
SENDINPUT_TYPE_INTERVAL = 0.0  # the STANDALONE engine's library default (unpaced); the REAL backend paces at 0.02 (backend.DEFAULT_TYPE_INTERVAL_SECONDS)
SENDINPUT_DRAG_STEP_PAUSE = 0.0  # minimal-segment drag: no per-segment cost
SENDINPUT_DRAG_STEP_PIXELS = 0  # 0 = the coarse legacy ~40 px interpolation geometry
_SENDINPUT_FACTORY_DRAG_STEP_PAUSE = 0.001  # L1-NEW-2 live default: ~1 ms per segment
_SENDINPUT_FACTORY_DRAG_STEP_PIXELS = 8  # L1-NEW-2 live default: ~8 px per segment
_SENDINPUT_CHUNK_EVENTS = 1000  # max events per SendInput call (typing chunks)
_WHEEL_DELTA = 120


# --- input engines (PERF-004): raw SendInput default, pyautogui fallback ----------------------

#: Virtual-key codes for the named keys accepted in ``keypress``/``hotkey`` actions
#: (the provider contract passes keys verbatim in pyautogui vocabulary). Letter/digit
#: VKs are layout-independent; other printable characters resolve through
#: ``VkKeyScanW`` exactly like pyautogui's ``VkKeyScanA`` mapping (layout parity).
_VK_NAMED_KEYS: dict[str, int] = {
    "ctrl": 0x11, "ctrlleft": 0xA2, "ctrlright": 0xA3,
    "alt": 0x12, "altleft": 0xA4, "altright": 0xA5,
    "shift": 0x10, "shiftleft": 0xA0, "shiftright": 0xA1,
    "win": 0x5B, "winleft": 0x5B, "winright": 0x5C, "cmd": 0x5B,
    "enter": 0x0D, "return": 0x0D, "numpadenter": 0x0D, "\n": 0x0D, "\r": 0x0D,
    "tab": 0x09, "\t": 0x09, "space": 0x20, " ": 0x20,
    "backspace": 0x08, "del": 0x2E, "delete": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pgup": 0x21, "pageup": 0x21, "pgdn": 0x22, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "esc": 0x1B, "escape": 0x1B, "capslock": 0x14, "numlock": 0x90,
    "scrolllock": 0x91, "pause": 0x13, "print": 0x2A,
    "printscreen": 0x2C, "prntscrn": 0x2C, "prtsc": 0x2C, "prtscr": 0x2C,
    "apps": 0x5D, "help": 0x2F, "execute": 0x2B, "select": 0x29,
    "sleep": 0x5F, "cancel": 0x03, "clear": 0x0C,
    "accept": 0x1E, "convert": 0x1C, "nonconvert": 0x1D, "final": 0x18,
    "modechange": 0x1F, "kana": 0x15, "hanguel": 0x15, "hangul": 0x15,
    "hanja": 0x19, "kanji": 0x19,
    "num0": 0x60, "num1": 0x61, "num2": 0x62, "num3": 0x63, "num4": 0x64,
    "num5": 0x65, "num6": 0x66, "num7": 0x67, "num8": 0x68, "num9": 0x69,
    "multiply": 0x6A, "add": 0x6B, "sep": 0x6C, "separator": 0x6C,
    "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
    "nexttrack": 0xB0, "prevtrack": 0xB1, "stop": 0xB2, "playpause": 0xB3,
    "volumemute": 0xAD, "volumeup": 0xAF, "volumedown": 0xAE,
    "launchmail": 0xB4, "launchmediaselect": 0xB5, "launchapp1": 0xB6, "launchapp2": 0xB7,
    "browserback": 0xA6, "browserforward": 0xA7, "browserrefresh": 0xA8,
    "browserstop": 0xA9, "browsersearch": 0xAA, "browserfavorites": 0xAB,
    "browserhome": 0xAC,
}
_VK_NAMED_KEYS.update({f"f{index}": 0x70 + index - 1 for index in range(1, 25)})
_VK_NAMED_KEYS.update({chr(0x61 + index): 0x41 + index for index in range(26)})  # a-z
_VK_NAMED_KEYS.update({str(digit): 0x30 + digit for digit in range(10)})  # 0-9
# Uppercase letters are deliberately NOT in the table: 'A' resolves through the
# lowercase entry with needs_shift=True (pyautogui needsShift parity).

_MOUSE_BUTTON_FLAGS: dict[str, tuple[int, int]] = {
    "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
    "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
    "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
}


def _virtual_screen_metrics() -> tuple[int, int, int, int]:
    """Virtual-desktop ``(x, y, width, height)`` via ``GetSystemMetrics`` (fail closed)."""
    if IS_WINDOWS and _user32 is not None:
        get_metrics = getattr(_user32, "GetSystemMetrics", None)
        if get_metrics is not None:
            try:
                x = int(get_metrics(_SM_XVIRTUALSCREEN))
                y = int(get_metrics(_SM_YVIRTUALSCREEN))
                width = int(get_metrics(_SM_CXVIRTUALSCREEN))
                height = int(get_metrics(_SM_CYVIRTUALSCREEN))
            except (OSError, AttributeError, ValueError):
                x = y = width = height = 0
            if width > 0 and height > 0:
                return (x, y, width, height)
    raise InputBlockedError(
        "Virtual-desktop metrics are unavailable; refusing physical input (fail closed)."
    )


def _primary_screen_size() -> tuple[int, int]:
    """Primary-monitor pixel size via ``GetSystemMetrics`` (pyautogui's ``size()`` parity)."""
    if IS_WINDOWS and _user32 is not None:
        get_metrics = getattr(_user32, "GetSystemMetrics", None)
        if get_metrics is not None:
            try:
                width = int(get_metrics(0))
                height = int(get_metrics(1))
                if width > 0 and height > 0:
                    return (width, height)
            except (OSError, AttributeError, ValueError):
                pass
    return (1920, 1080)


def _to_absolute_65535(value: int, origin: int, extent: int) -> int:
    """Normalize one physical axis to ``MOUSEEVENTF_ABSOLUTE``'s 0-65535 range.

    Bijection over the axis: ``0 -> 0`` and ``origin + extent - 1 -> 65535`` so the
    inverse (``_from_absolute_65535``) restores the exact pixel. ``extent <= 1``
    degenerates to 0.
    """
    if extent <= 1:
        return 0
    clamped = min(max(value, origin), origin + extent - 1)
    return round((clamped - origin) * 65535 / (extent - 1))


def _from_absolute_65535(normalized: int, origin: int, extent: int) -> int:
    """Inverse of :func:`_to_absolute_65535` (used by the parity tests)."""
    if extent <= 1:
        return origin
    return origin + round(normalized * (extent - 1) / 65535)


def _mouse_move_event(x: int, y: int, metrics: tuple[int, int, int, int]) -> _INPUT:
    """Absolute ``MOUSEEVENTF_MOVE`` event over the whole virtual desktop.

    ``MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK`` maps 0-65535 onto the entire
    virtual desktop (multi-monitor correct; pyautogui's own normalization uses primary
    metrics only — a latent bug the SendInput engine deliberately does not copy).
    """
    origin_x, origin_y, width, height = metrics
    return _INPUT(
        type=_INPUT_MOUSE,
        union=_INPUT_UNION(
            mi=_MOUSEINPUT(
                dx=_to_absolute_65535(x, origin_x, width),
                dy=_to_absolute_65535(y, origin_y, height),
                mouseData=0,
                dwFlags=_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK,
                time=0,
                dwExtraInfo=0,
            )
        ),
    )


def _mouse_flag_event(dw_flags: int, mouse_data: int = 0) -> _INPUT:
    """Relative-flag mouse event (button/wheel); dx/dy unused at (0, 0) with no MOVE."""
    return _INPUT(
        type=_INPUT_MOUSE,
        union=_INPUT_UNION(
            mi=_MOUSEINPUT(
                dx=0, dy=0, mouseData=mouse_data & 0xFFFFFFFF, dwFlags=dw_flags, time=0, dwExtraInfo=0
            )
        ),
    )


def _key_event(vk: int, scan: int, dw_flags: int) -> _INPUT:
    """One ``KEYBDINPUT`` event (``KEYEVENTF_UNICODE`` uses ``scan`` as the code unit)."""
    return _INPUT(
        type=_INPUT_KEYBOARD,
        union=_INPUT_UNION(
            ki=_KEYBDINPUT(wVk=vk & 0xFFFF, wScan=scan & 0xFFFF, dwFlags=dw_flags, time=0, dwExtraInfo=0)
        ),
    )


def _char_event_groups(text: str) -> list[list[tuple[int, int, int]]]:
    """WP12c (VAL-WP12-02 C4): per-CHARACTER event groups for the paced path.

    A surrogate pair travels in ONE group (one SendInput call, one sleep after
    it), so the per-character focus probe and pacing run once per CHARACTER,
    never per surrogate half. ``\\n``/``\\r`` become a VK_RETURN pair and ``\\t``
    a VK_TAB pair; every source character is exactly one group.
    """
    groups: list[list[tuple[int, int, int]]] = []
    for char in text:
        if char in ("\n", "\r"):
            groups.append([(0x0D, 0, 0), (0x0D, 0, _KEYEVENTF_KEYUP)])
            continue
        if char == "\t":
            groups.append([(0x09, 0, 0), (0x09, 0, _KEYEVENTF_KEYUP)])
            continue
        encoded = char.encode("utf_16_le")
        group: list[tuple[int, int, int]] = []
        for index in range(0, len(encoded), 2):
            code_unit = int.from_bytes(encoded[index : index + 2], "little")
            group.append((0, code_unit, _KEYEVENTF_UNICODE))
            group.append((0, code_unit, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP))
        groups.append(group)
    return groups


def _text_to_key_units(text: str) -> list[tuple[int, int, int]]:
    """Whole-string typing as ``KEYEVENTF_UNICODE`` event triples ``(vk, scan, flags)``.

    Layout-proof: every character is injected as a UTF-16 code unit (VK_PACKET), so
    keyboard-layout remapping (e.g. Arabic) cannot garble it. ``'\\n'``/``'\\r'`` become
    ``VK_RETURN`` and ``'\\t'`` becomes ``VK_TAB`` (pyautogui ``write`` parity). Astral
    characters split into surrogate pairs. Built from :func:`_char_event_groups` (the
    per-character pacing groups), so both paths share one event construction.
    """
    return [unit for group in _char_event_groups(text) for unit in group]


def _resolve_key_events(key: str) -> tuple[int, bool]:
    """Resolve one key name to ``(virtual_key, needs_shift)``; unknown -> error.

    C1 (DELTA 13, VAL-WP13-12): there is NO cross-action cache. ``VkKeyScanW``
    is LAYOUT-DEPENDENT (the user switches between Arabic and English
    layouts), so a process-global cache would (a) make a later keypress send
    the WRONG key after a layout switch and (b) bypass the Z2 unmappable-key
    refusal for a character the new layout cannot produce. The static
    ``_VK_NAMED_KEYS`` table — layout-independent by construction — is
    consulted first; everything else resolves FRESH on every call. Within ONE
    action the press and the release share one mapping because the backend's
    pre-check (:meth:`ComputerBackend._ensure_keys_dispatchable`) resolves
    every key once and PASSES the mapping down (``chord_resolved`` /
    ``key_down_resolved`` / ``key_up_resolved``); nothing is reused across
    actions. Raises :class:`~computer_use_mcp.desktop.errors.UnsupportedKeyError`
    (a ``ValueError`` subclass) when the key cannot be mapped (Z2,
    LIVE-WP13-02).
    """
    if len(key) > 1:
        # L3 (LIVE-WP13-02): a MULTI-character key name is always a KEY NAME —
        # it can never be text (text travels via KEYEVENTF_UNICODE on type
        # actions) — so "F4", "Enter", "ESC" resolve case-insensitively to the
        # table entries. Single characters keep the exact legacy path below
        # (an uppercase letter such as "A" must keep its shift meaning).
        lowered = _VK_NAMED_KEYS.get(key.lower())
        if lowered is not None:
            return (lowered, False)
    if len(key) == 1:
        lowered = _VK_NAMED_KEYS.get(key.lower())
        if lowered is not None:
            return (lowered, key.lower() != key)  # uppercase letter: inject shift too
    if not IS_WINDOWS or _user32 is None:
        raise UnsupportedKeyError([key])
    try:
        result = int(_user32.VkKeyScanW(ctypes.c_wchar(key))) & 0xFFFF
    except (OSError, AttributeError, ValueError, TypeError):
        result = 0xFFFF  # VkKeyScanW returns -1 (0xFFFF) for unmappable characters
    if result == 0xFFFF:
        raise UnsupportedKeyError([key])
    vk = result & 0xFF
    needs_shift = bool(result & 0x0100)
    return (vk, needs_shift)


def _chord_key_units(
    keys: list[str], resolved: dict[str, tuple[int, bool]] | None = None
) -> list[tuple[int, int, int]]:
    """Chord events ``(vk, scan, flags)``: press in order, release in reverse order.

    All events land in ONE SendInput call — injected input is serialized into the input
    stream (never interleaved with real user input), which makes batched chords safer
    than four separate ``keybd_event`` calls. Shift that a member requires (via
    ``VkKeyScanW``) is pressed first and released last (stuck-modifier hygiene).
    Z2 (LIVE-WP13-02): EVERY key is mapped BEFORE any event is built — one
    unmappable key raises :class:`UnsupportedKeyError` naming ALL unsupported
    members, with zero events dispatched.
    C1 (DELTA 13): ``resolved`` is the backend pre-check's PER-ACTION mapping
    (key name -> ``(vk, needs_shift)``); when a key is present in it the
    mapping is used as-is and the resolver is NOT consulted again — the press
    and the release of one action share the exact resolution the pre-check
    got, even if the keyboard layout changes mid-action.
    """
    unsupported: list[str] = []
    resolutions: list[tuple[int, bool]] = []
    for key in keys:
        mapped = resolved.get(key) if resolved is not None else None
        if mapped is not None:
            resolutions.append(mapped)
            continue
        try:
            resolutions.append(_resolve_key_events(key))
        except UnsupportedKeyError:
            unsupported.append(key)
    if unsupported:
        raise UnsupportedKeyError(unsupported)
    units: list[tuple[int, int, int]] = []
    shift_pressed = False
    for vk, needs_shift in resolutions:
        if needs_shift and not shift_pressed:
            units.append((0x10, 0, 0))
            shift_pressed = True
        units.append((vk, 0, 0))
    for vk, needs_shift in reversed(resolutions):
        units.append((vk, 0, _KEYEVENTF_KEYUP))
        if needs_shift and shift_pressed:
            units.append((0x10, 0, _KEYEVENTF_KEYUP))
            shift_pressed = False
    if shift_pressed:  # unreachable by construction; belt-and-braces release
        units.append((0x10, 0, _KEYEVENTF_KEYUP))
    return units


def _chord_key_halves(
    keys: list[str], resolved: dict[str, tuple[int, bool]] | None = None
) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    """Split :func:`_chord_key_units` into its ``(press, release)`` halves at the first key-up.

    WP2 (ORVEX-2026-107): a HELD key is this split, not a new event model.
    ``_chord_key_units`` already emits the press half with ``flags == 0`` and the
    release half in reverse order with each synthesized shift released immediately
    after every key that required it, so splitting inherits reverse-order release and
    that per-key shift hygiene for free — a rewrite of the event model would only risk
    regressing an invariant that is already correct. C1 (DELTA 13): ``resolved``
    is the per-action pre-check mapping, passed straight through.
    """
    units = _chord_key_units(keys, resolved)
    first_up = next(
        (index for index, (_vk, _scan, flags) in enumerate(units) if flags & _KEYEVENTF_KEYUP),
        len(units),
    )
    return units[:first_up], units[first_up:]


class InputEngine(ABC):
    """Physical-input contract used by :class:`LocalComputerBackend.execute`.

    Implementations must raise :class:`InputBlockedError` when input is blocked
    (failsafe corner, blocked desktop/UIPI) and must never partially dispatch silently
    (a zero-event success is a contract violation). Pacing attributes
    (``click_interval``/``type_interval``/``drag_interpolate``/``drag_step_pause``)
    are read by the backend to keep stroke/stop-check policy engine-agnostic;
    ``drag_step_pixels`` (L1-NEW-2) additionally selects stroke granularity
    (``0`` = legacy ~40 px geometry — additive, unset stubs are unaffected).
    """

    click_interval: float = 0.0
    type_interval: float = 0.0
    drag_interpolate: bool = False
    drag_step_pause: float = 0.0
    #: L1-NEW-2: stroke granularity in pixels when the interpolated drag is on.
    #: ``0``/``None`` = the legacy ~40 px geometry; the backend's SendInput factory
    #: default builds the dense ~8 px stroke freehand apps need. Additive attribute —
    #: engine stubs that never set it keep the legacy behavior unchanged.
    drag_step_pixels: int = 0

    @abstractmethod
    def move(self, x: int, y: int) -> None:
        """Reposition the cursor to physical virtual-screen ``(x, y)``."""

    @abstractmethod
    def click(self, x: int, y: int, clicks: int = 1, button: str = "left") -> None:
        """Move to ``(x, y)`` and press/release ``button`` (default left) ``clicks`` times.

        ``button="right"`` (v0.6.0 right_click) presses/releases the right button —
        unsupported names raise ``ValueError`` (same vocabulary as ``mouse_down``).
        """

    @abstractmethod
    def mouse_down(self, button: str = "left") -> None:
        """Press a mouse button (drag start)."""

    @abstractmethod
    def mouse_up(self, button: str = "left") -> None:
        """Release a mouse button (drag end; the backend guarantees this runs)."""

    @abstractmethod
    def type_text(self, text: str, before_chunk: Callable[[], None] | None = None) -> None:
        """Type a whole string; ``before_chunk`` runs before every dispatch chunk."""

    @abstractmethod
    def chord(self, keys: list[str]) -> None:
        """Press and release a chord of key names (pyautogui vocabulary)."""

    # WP2 (ORVEX-2026-107, D3): the held-key half of the contract. These are the
    # snake_case mirror of ``mouse_down``/``mouse_up``: a held key is the EXISTING
    # ``chord`` event list split at its midpoint (see :func:`_chord_key_halves`), so an
    # engine dispatches one half per call and the BACKEND owns the wait between them —
    # the same division of labour the proven held-click path already has.
    #
    # DELTA 1 (VAL-WP2 F3): the base implementations FAIL HONESTLY instead of silently
    # succeeding. The no-op default let an engine that never implemented the pair
    # return "Executed keypress." while nothing was pressed — success reported for
    # input that never happened. Still deliberately NOT ``@abstractmethod`` (that
    # would break third-party and stub engines at CONSTRUCTION time); the failure is
    # moved to CALL time as a typed :class:`UnsupportedActionError`, which the
    # pipeline maps onto a §1 failed reply. Both shipped engines override the pair,
    # and the suite's test engines implement them.
    def key_down(self, key: str) -> None:
        """Press ``key`` and LEAVE IT DOWN.

        The caller is responsible for a matching :meth:`key_up` —
        ``LocalComputerBackend`` guarantees it in a ``finally``, including on a
        mid-hold stop. A key left physically down rewrites every subsequent user
        keystroke machine-wide, so an implementation that cannot honour that contract
        must not be selected.
        """
        raise UnsupportedActionError(
            "this input engine does not implement key_down/key_up; "
            "held-key input cannot be dispatched on it"
        )

    def key_up(self, key: str) -> None:
        """Release ``key`` (the mirror of :meth:`key_down`)."""
        raise UnsupportedActionError(
            "this input engine does not implement key_down/key_up; "
            "held-key input cannot be dispatched on it"
        )

    @abstractmethod
    def scroll(self, delta: int) -> None:
        """Scroll by ``delta`` wheel notches (positive = up, pyautogui parity)."""

    def release_modifiers(self, keys: list[str]) -> None:
        """Synthetic key-up for the NAMED modifiers only (HotkeyGuard ``release`` mode).

        Concrete engines override this; the base implementation is a deliberate no-op
        so existing engine implementations keep working unchanged. Callers must only
        ever name modifiers this session dispatched (the guard matches against its own
        chord log) — a key-up for a key we did not press is never sent.
        """
        return

    # C1 (DELTA 13, VAL-WP13-12): the PER-ACTION resolved-input contract. The
    # backend's pre-check maps every key of one action ONCE and passes the
    # mapping down so the press AND the release use exactly that resolution
    # (a mid-action layout change can never make the release differ), while
    # NOTHING is reused across actions (``VkKeyScanW`` is layout-dependent).
    # The base implementations FALL BACK to the name-based methods, so engines
    # that resolve internally per call (pyautogui, test stubs) keep working
    # unchanged — they simply resolve fresh every call and never cache.
    def chord_resolved(
        self, keys: list[str], resolved: dict[str, tuple[int, bool]]
    ) -> None:
        """Dispatch a chord using the backend pre-check's per-action mapping."""
        self.chord(keys)

    def key_down_resolved(
        self, key: str, resolved: dict[str, tuple[int, bool]]
    ) -> None:
        """Press ``key`` using the per-action mapping (fallback: by name)."""
        self.key_down(key)

    def key_up_resolved(
        self, key: str, resolved: dict[str, tuple[int, bool]]
    ) -> None:
        """Release ``key`` using the per-action mapping (fallback: by name)."""
        self.key_up(key)


class PyAutoGuiInputEngine(InputEngine):
    """pyautogui-backed engine: the selectable fallback (same contract, legacy pacing).

    Preserves the pre-SendInput behavior exactly (click interval 0.08 s, per-char
    write + 0.01 s sleep, interpolated drag stroke) except ``PAUSE``: the mission's
    PAUSE economics set it to 0 by default (``CORTEX_PYAUTOGUI_PAUSE`` restores 0.1).
    ``FailSafeException`` maps onto :class:`InputBlockedError` as before.
    """

    def __init__(
        self,
        pyautogui_module: object,
        *,
        pause: float = 0.0,
        click_interval: float = 0.08,
        type_interval: float = _TYPE_INTERVAL_SECONDS,
        drag_interpolate: bool = True,
        drag_step_pause: float = _DRAG_STEP_PAUSE_SECONDS,
        failsafe: bool = True,
    ) -> None:
        self._pa = pyautogui_module
        self._pa.FAILSAFE = failsafe
        self._pa.PAUSE = pause
        self.click_interval = click_interval
        self.type_interval = type_interval
        self.drag_interpolate = drag_interpolate
        self.drag_step_pause = drag_step_pause

    def _perform(self, input_call: Callable[[], None]) -> None:
        """Run one pyautogui input, mapping FailSafeException onto InputBlockedError."""
        try:
            input_call()
        except self._pa.FailSafeException as exc:
            raise InputBlockedError(
                "pyautogui failsafe triggered (mouse in a screen corner); physical input halted."
            ) from exc

    def _perform_release(self, release_call: Callable[[], None]) -> None:
        """Run one pyautogui RELEASE with FAILSAFE suspended around that single call.

        DELTA 1 (VAL-WP2 F1 BLOCKER): pyautogui's own FAILSAFE fires INSIDE
        ``keyUp``/``mouseUp`` — the corner is the user's abort gesture, and refusing a
        release would leave a button or key physically DOWN (stuck input rewrites all
        later keystrokes system-wide). So release primitives suspend
        ``self._pa.FAILSAFE`` for the duration of the one release call and restore the
        EXACT previous value in a ``finally``. Everything else is unchanged: any other
        pyautogui failure still propagates (and a FailSafeException is still mapped if
        an implementation ignores the suspension), so the error contract for releases
        is identical apart from the corner.
        """
        previous = getattr(self._pa, "FAILSAFE", True)
        self._pa.FAILSAFE = False
        try:
            self._perform(release_call)
        finally:
            self._pa.FAILSAFE = previous

    def move(self, x: int, y: int) -> None:
        self._perform(lambda: self._pa.moveTo(x, y))

    def click(self, x: int, y: int, clicks: int = 1, button: str = "left") -> None:
        self._perform(
            lambda: self._pa.click(x, y, clicks=clicks, interval=self.click_interval, button=button)
        )

    def mouse_down(self, button: str = "left") -> None:
        self._perform(lambda: self._pa.mouseDown(button=button))

    def mouse_up(self, button: str = "left") -> None:
        # DELTA 1 (F1): the halt-safe action — failsafe suspended for this one call.
        self._perform_release(lambda: self._pa.mouseUp(button=button))

    def type_text(self, text: str, before_chunk: Callable[[], None] | None = None) -> None:
        sent = 0
        for char in text:
            if before_chunk is not None:
                try:
                    before_chunk()
                except FocusDriftError as exc:
                    # K-05 (ORVEX-2026-108): this engine dispatches ONE CHARACTER per
                    # hook call, so `sent` is the exact number of characters already
                    # landed in THIS call when the focus guard aborted. The caller
                    # (backend `_execute_type`) adds the offset of earlier chunks.
                    exc.delivered_chars = sent
                    raise
            self._perform(lambda char=char: self._pa.write(char))
            sent += 1
            if self.type_interval > 0:
                time.sleep(self.type_interval)

    def chord(self, keys: list[str]) -> None:
        self._perform(lambda: self._pa.hotkey(*keys))

    def key_down(self, key: str) -> None:
        # Routed through _perform so a failsafe triggered DURING a hold maps onto the
        # same InputBlockedError every other pyautogui dispatch raises (WP2, 107).
        self._perform(lambda: self._pa.keyDown(key))

    def key_up(self, key: str) -> None:
        # DELTA 1 (F1): the halt-safe action — failsafe suspended for this one call.
        self._perform_release(lambda: self._pa.keyUp(key))

    def release_modifiers(self, keys: list[str]) -> None:
        """Key-up each named modifier via pyautogui (release mode, T8).

        DELTA 1 (F1): each release is the halt-safe action — failsafe suspended for
        that one call (restored exactly afterwards, even on failure).
        """
        for key in keys:
            self._perform_release(lambda key=key: self._pa.keyUp(key))

    def scroll(self, delta: int) -> None:
        self._perform(lambda: self._pa.scroll(delta))


class SendInputEngine(InputEngine):
    """Raw Win32 SendInput engine (stdlib ctypes, zero new dependencies).

    - Mouse: absolute ``MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK`` events
      normalized 0-65535 over the WHOLE virtual desktop (multi-monitor correct).
    - Keyboard: batched ``KEYEVENTF_UNICODE`` (VK_PACKET) for typing — layout-proof,
      fixes keyboard-layout-dependent garbled input (e.g. Arabic); chords are batched
      into one call and modifiers are always released in reverse order.
    - Failsafe parity: the pyautogui screen-corner check is replicated before every
      dispatch batch and raises the same :class:`InputBlockedError`.
    - Fail-closed: a ``SendInput`` return of 0 (blocked by UIPI or another thread)
      raises :class:`InputBlockedError` — strictly stronger than pyautogui, which
      silently swallows ``PermissionError``/``OSError`` from ``mouse_event``.

    Pacing: the standalone engine stays batched and unpaced (whole-string
    dispatch); the REAL backend paces it at 0.02 s/char by default via
    ``type_interval`` (WP12a). The drag
    stroke policy is ENGINE-ATTRIBUTE-driven (L1-NEW-2): a bare engine keeps the
    legacy minimal defaults (teleport stroke, zero pause), while the backend's
    factory builds the live default — interpolated dense drag (~8 px segments
    with ~1 ms per-segment pacing: freehand apps sample held-button mouse moves,
    and a teleport stroke paints dots; a 400 px line completes in ~80 ms).
    ``CORTEX_DRAG_INTERPOLATE=0`` restores the teleport stroke on the live path.
    ``type_interval``/``drag_step_pause``/``click_interval`` tune per-event
    cadence. ``_dispatch`` is the injectable thunk for
    tests (mock ``backend_module._user32`` instead — the binding is read at call time).
    """

    def __init__(
        self,
        *,
        failsafe: bool = True,
        click_interval: float = 0.0,
        type_interval: float = SENDINPUT_TYPE_INTERVAL,
        drag_interpolate: bool = False,
        drag_step_pause: float = SENDINPUT_DRAG_STEP_PAUSE,
        drag_step_pixels: int = SENDINPUT_DRAG_STEP_PIXELS,
    ) -> None:
        if not IS_WINDOWS or _user32 is None:
            raise RuntimeError("SendInputEngine requires Windows.")
        self.failsafe = failsafe
        self.click_interval = click_interval
        self.type_interval = type_interval
        self.drag_interpolate = drag_interpolate
        self.drag_step_pause = drag_step_pause
        self.drag_step_pixels = max(0, int(drag_step_pixels))

    # -- dispatch plumbing ---------------------------------------------------------------

    def _dispatch(self, events: list[_INPUT]) -> int:
        """The injectable SendInput thunk: returns the number of events accepted."""
        array = (_INPUT * len(events))(*events)
        return int(_user32.SendInput(len(array), array, ctypes.sizeof(_INPUT)))

    def _cursor_in_failsafe_corner(self) -> bool:
        position = get_cursor_position()
        if position is None:
            return False
        width, height = _primary_screen_size()
        corners = {(0, 0), (0, height - 1), (width - 1, 0), (width - 1, height - 1)}
        return position in corners

    def _send(self, events: list[_INPUT]) -> None:
        """Failsafe-check, dispatch one batch, and fail closed on a zero result."""
        if not events:
            return
        if not IS_WINDOWS or _user32 is None:
            raise InputBlockedError("Physical input requires Windows.")
        if self.failsafe and self._cursor_in_failsafe_corner():
            raise InputBlockedError(
                "SendInput failsafe triggered (mouse in a screen corner); physical input halted."
            )
        self._send_release(events)

    def _send_release(self, events: list[_INPUT]) -> None:
        """Dispatch one RELEASE batch, SKIPPING ONLY the failsafe corner check.

        WP2 DELTA 1 (VAL-WP2 F1 BLOCKER): a release is the HALT-SAFE action. The
        failsafe corner is the user's own abort gesture (fling the mouse into a
        corner); if ``mouse_up``/``key_up``/``release_modifiers`` refused there, the
        abort would leave a button or keys PHYSICALLY DOWN — stuck input rewrites
        every later keystroke system-wide, which is strictly worse than the input the
        user is aborting. So every release primitive routes through THIS path: the
        corner check is skipped, and everything else is IDENTICAL to ``_send`` — the
        non-Windows guard and the zero-return fail-closed check (``InputBlockedError``
        when SendInput is blocked by UIPI or another input source) both stay.
        Press/down/move/click/type/chord/scroll keep the corner check unchanged.
        """
        if not events:
            return
        if not IS_WINDOWS or _user32 is None:
            raise InputBlockedError("Physical input requires Windows.")
        if self._dispatch(events) == 0:
            raise InputBlockedError(
                "SendInput was blocked (UIPI or another input source); physical input halted."
            )

    # -- InputEngine interface -------------------------------------------------------------

    def move(self, x: int, y: int) -> None:
        self._send([_mouse_move_event(x, y, _virtual_screen_metrics())])

    def click(self, x: int, y: int, clicks: int = 1, button: str = "left") -> None:
        if clicks < 1:
            raise ValueError("clicks must be >= 1")
        try:
            down_flag, up_flag = _MOUSE_BUTTON_FLAGS[button]
        except KeyError as exc:
            raise ValueError(f"unsupported mouse button: {button!r}") from exc
        metrics = _virtual_screen_metrics()
        if clicks == 1 or self.click_interval <= 0:
            events = [_mouse_move_event(x, y, metrics)]
            for _ in range(clicks):
                events.append(_mouse_flag_event(down_flag))
                events.append(_mouse_flag_event(up_flag))
            self._send(events)
            return
        self._send([_mouse_move_event(x, y, metrics),
                    _mouse_flag_event(down_flag),
                    _mouse_flag_event(up_flag)])
        for _ in range(clicks - 1):
            time.sleep(self.click_interval)
            self._send([_mouse_flag_event(down_flag),
                        _mouse_flag_event(up_flag)])

    def mouse_down(self, button: str = "left") -> None:
        try:
            down_flag, _up_flag = _MOUSE_BUTTON_FLAGS[button]
        except KeyError as exc:
            raise ValueError(f"unsupported mouse button: {button!r}") from exc
        self._send([_mouse_flag_event(down_flag)])

    def mouse_up(self, button: str = "left") -> None:
        try:
            _down_flag, up_flag = _MOUSE_BUTTON_FLAGS[button]
        except KeyError as exc:
            raise ValueError(f"unsupported mouse button: {button!r}") from exc
        # DELTA 1 (F1): a release is the halt-safe action — skips the failsafe corner
        # check ONLY (the zero-return fail-closed check stays); see ``_send_release``.
        self._send_release([_mouse_flag_event(up_flag)])

    def type_text(self, text: str, before_chunk: Callable[[], None] | None = None) -> None:
        if not text:
            return
        units = _text_to_key_units(text)

        def _batch_code_points(unit_batch: list[tuple[int, int, int]]) -> int:
            """MINOR-N4 (VAL-WP1-02): count CODE POINTS, never UTF-16 code units.

            An astral character travels as a surrogate PAIR — two down events — but is
            ONE character; the second (low-surrogate) down event must not count, or the
            engine's inner unit disagrees with the outer chunk offsets (code points).
            A counted unit is a DOWN event (no KEYUP flag) that is not a low surrogate;
            ``\\n``/``\\t`` key events count one each.
            """
            return sum(
                1
                for _vk, code_unit, flags in unit_batch
                if not flags & _KEYEVENTF_KEYUP
                and not (flags & _KEYEVENTF_UNICODE and 0xDC00 <= code_unit <= 0xDFFF)
            )

        if self.type_interval > 0:
            # WP12c (VAL-WP12-02 C4): pace PER CHARACTER. One character — BMP or
            # astral — is ONE SendInput call with ONE sleep after it, so the focus
            # probe runs once per character and the sleep budget counts characters.
            sent_chars = 0
            for group in _char_event_groups(text):
                if before_chunk is not None:
                    try:
                        before_chunk()
                    except FocusDriftError as exc:
                        # K-05 (ORVEX-2026-108): hook cadence == dispatch cadence (one
                        # character per paced chunk); an abort BEFORE ``_send`` means
                        # that character contributed nothing. ``sent_chars`` is the
                        # exact CODE POINTS dispatched in THIS call; the caller adds
                        # the offset of earlier chunks.
                        exc.delivered_chars = sent_chars
                        raise
                self._send([_key_event(vk, scan, flags) for vk, scan, flags in group])
                sent_chars += _batch_code_points(group)
                time.sleep(self.type_interval)
            return

        # Unpaced: the whole string in large SendInput batches (offline/library fast
        # path; the real backend paces by default since WP12a).
        events = [_key_event(vk, scan, flags) for vk, scan, flags in units]
        chunk_size = _SENDINPUT_CHUNK_EVENTS - (_SENDINPUT_CHUNK_EVENTS % 2)

        sent_chars = 0
        for start in range(0, len(events), chunk_size):
            batch_units = units[start : start + chunk_size]
            if before_chunk is not None:
                try:
                    before_chunk()
                except FocusDriftError as exc:
                    # K-05 (ORVEX-2026-108): hook cadence == dispatch cadence (one
                    # BATCH of characters per hook call on the unpaced path — and an
                    # abort BEFORE ``_send`` means that batch contributed nothing).
                    # ``sent_chars`` is the exact CODE POINTS dispatched in THIS
                    # call; the caller adds the offset of earlier chunks.
                    exc.delivered_chars = sent_chars
                    raise
            self._send(events[start : start + chunk_size])
            sent_chars += _batch_code_points(batch_units)

    def chord(self, keys: list[str]) -> None:
        self._send([_key_event(vk, scan, flags) for vk, scan, flags in _chord_key_units(keys)])

    def chord_resolved(
        self, keys: list[str], resolved: dict[str, tuple[int, bool]]
    ) -> None:
        """C1 (DELTA 13): build the chord from the backend pre-check's mapping —
        the resolver is NOT consulted again (nothing re-resolved, nothing
        cached across actions)."""
        self._send(
            [_key_event(vk, scan, flags) for vk, scan, flags in _chord_key_units(keys, resolved)]
        )

    def key_down(self, key: str) -> None:
        """Press-half of the existing chord event list for one key (WP2, ORVEX-2026-107).

        A key that needs shift (``VkKeyScanW``) has shift pressed FIRST here and it is
        released LAST by ``key_up`` — the same stuck-modifier hygiene ``chord`` already
        gets, inherited by the split rather than reinvented. Routing through ``_send``
        keeps failsafe and zero-return fail-closed parity for free.
        """
        press, _release = _chord_key_halves([key])
        self._send([_key_event(vk, scan, flags) for vk, scan, flags in press])

    def key_up(self, key: str) -> None:
        """Release-half of the existing chord event list for one key (WP2).

        DELTA 1 (F1): a release is the halt-safe action — skips the failsafe corner
        check ONLY (the zero-return fail-closed check stays); see ``_send_release``.
        """
        _press, release = _chord_key_halves([key])
        self._send_release([_key_event(vk, scan, flags) for vk, scan, flags in release])

    def key_down_resolved(
        self, key: str, resolved: dict[str, tuple[int, bool]]
    ) -> None:
        """C1 (DELTA 13): press-half built from the PER-ACTION pre-check mapping."""
        press, _release = _chord_key_halves([key], resolved)
        self._send([_key_event(vk, scan, flags) for vk, scan, flags in press])

    def key_up_resolved(
        self, key: str, resolved: dict[str, tuple[int, bool]]
    ) -> None:
        """C1 (DELTA 13): release-half from the SAME per-action mapping (the
        halt-safe release path is unchanged)."""
        _press, release = _chord_key_halves([key], resolved)
        self._send_release([_key_event(vk, scan, flags) for vk, scan, flags in release])

    def release_modifiers(self, keys: list[str]) -> None:
        """Key-up each named modifier in ONE SendInput batch (release mode, T8).

        DELTA 1 (F1): a release is the halt-safe action — skips the failsafe corner
        check ONLY; see ``_send_release``.
        """
        events: list[_INPUT] = []
        for key in keys:
            vk, _needs_shift = _resolve_key_events(key)
            events.append(_key_event(vk, 0, _KEYEVENTF_KEYUP))
        self._send_release(events)

    def scroll(self, delta: int) -> None:
        """Turn the wheel at the CURRENT CURSOR POSITION: POSITIVE = UP, away from
        you (negative reads further down). The engine injects no pointer motion, so
        the caller must ``move`` the pointer to the target region first.
        """
        self._send([_mouse_flag_event(_MOUSEEVENTF_WHEEL, (delta * _WHEEL_DELTA) & 0xFFFFFFFF)])
