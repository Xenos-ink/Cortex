"""Windows platform backend: identity, monitors/DPI, coordinate integrity, stop-checked input.

Conventions (binding for the runtime):
- Screenshot pixels are *screenshot-local*: ``(0, 0)`` is the top-left of the captured
  image. Action points validated by the runtime live in this space.
- Physical input coordinates are *virtual-screen pixels* (``SetCursorPos`` convention):
  secondary monitors may have negative origins. ``MonitorInfo.bounds`` uses the same
  ``(left, top, width, height)`` virtual-screen convention (mss convention).
- ``Observation.cursor_x/y`` are screenshot-local: the physical ``GetCursorPos`` position
  localized onto the captured monitor through the verified transform (they may be
  negative when the cursor sits outside the captured monitor).
- ``coordinate_scale_x/y`` are *input pixels per screenshot pixel*: multiply screenshot
  coordinates by the scale to obtain physical coordinates.

Coordinate-space integrity (``classify_coordinate_space``) is decided by measurement
first (screenshot dims vs. target-monitor physical dims): equal means
``verified_passthrough``; otherwise a uniform ratio that matches the monitor's queried
per-monitor DPI scale means ``scaled``; anything else (including DPI read from the
system fallback, i.e. estimated) is ``unverifiable`` and the executor refuses coordinate
input (fail closed).

Coordinate-transform invariant (F1, binding for every layer): EXACTLY ONE
screenshot-to-physical transform exists in the pipeline and it lives HERE —
``ComputerBackend._map_to_physical`` (``physical = origin + screenshot * scale`` via
:class:`CoordinateTransform`), applied exactly once at execution. Grounding validates
bounds in screenshot space and records the verified scale on the grounding result but
NEVER rewrites the point; no consumer may pre-scale or re-scale a point, or the executed
position lands at ``origin + screenshot * scale**2``.

Physical input is dispatched by pluggable :class:`InputEngine` implementations
(:class:`SendInputEngine` — raw Win32 ``SendInput`` via ctypes, the default — and
:class:`PyAutoGuiInputEngine`, the selectable pyautogui fallback). Both honor the same
safety contract: failsafe screen-corner checks raise :class:`InputBlockedError`, the
stop token is checked before every physical input by ``execute``, and a blocked
injection (``SendInput`` returning 0) fails closed. The engine is selected at backend
construction from ``CORTEX_INPUT_BACKEND`` (``sendinput`` | ``pyautogui``).

The module imports cleanly on non-Windows: all Win32 calls are guarded by
``IS_WINDOWS`` and input libraries are imported lazily inside
:class:`LocalComputerBackend`. The Win32 entry points read the module-level
``_user32``/``_kernel32``/``_shcore`` bindings at call time so tests can monkeypatch
them without a real desktop.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import io
import math
import os
import platform
import re
import shutil
import subprocess
import struct
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from PIL import Image

from ..core.models import (
    MAX_DURATION_MS,
    MAX_HOLD_MS,
    MAX_PLAN_SETTLE_MS,
    CoordinateSpace,
    GroundedAction,
    MonitorInfo,
    Observation,
    TextRegion,
    WindowInfo,
)
from ..core.state import StopToken
from .capture import (
    CAPTURE_BACKEND_ENV,
    INTERNAL_FRAME_REUSE_ENV,
    PNG_COMPRESS_LEVEL_ENV,
    PNG_OPTIMIZE_ENV,
    TEXT_PNG_ENV,
    _CaptureContext,
    _DXGI_ACQUIRE_TIMEOUT_MS,
    _DxgiDuplicator,
    _raw_payload_key,
)
from .client_budget import (
    pacing_budget_seconds,
    semantic_read_timeout_seconds,
    type_action_deadline_seconds,
)
from .clipboard import (
    CLIPBOARD_NON_TEXT,
    CLIPBOARD_RESTORE_DESTROYED,
    CLIPBOARD_RESTORE_FAILED,
    _clipboard_read_verdict,
    _clipboard_write_text,
)
from .coordinates import CoordinateTransform, classify_coordinate_space
from .errors import (
    CoordinateSpaceError,
    DisplayUnavailableError,
    FocusDriftError,
    InputBlockedError,
    LaunchTargetError,
    TextIntegrityError,
    UnsupportedActionError,
    WindowFocusError,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation-only names (lazy under __future__ annotations)
    # MonitorInfo is a runtime import above (shared with the models import).
    from .coordinates import CoordinateVerdict
    from .input_engine import InputEngine
    from .launch import AppWindowCandidate
from .input_engine import (
    DRAG_INTERPOLATE_ENV,
    DRAG_STEP_PAUSE_ENV,
    DRAG_STEP_PIXELS_ENV,
    INPUT_BACKEND_ENV,
    PYAUTOGUI_PAUSE_ENV,
    SENDINPUT_TYPE_INTERVAL,
    TYPE_INTERVAL_ENV,
    PyAutoGuiInputEngine,
    SendInputEngine,
    _SENDINPUT_FACTORY_DRAG_STEP_PAUSE,
    _SENDINPUT_FACTORY_DRAG_STEP_PIXELS,
)
from .launch import _launch_unresolved_suffix, _mapped_launch_path, validate_launch_needle
from .motion import (
    WAIT_MAX_SECONDS,
    _drag_segment_points,
    _motion_path_points,
    _sleep_for_wait_action,
    _wait_clamp_note,
    _wait_for_hold,
    interruptible_wait,
)
from .text_readers import (
    UIA_READ_ENV,
    UiaSemanticReader,
    Win32TextReader,
    _WINDOW_TEXT_MAX_CHARS,
    _uia_snapshot_to_text_regions,
    _uia_snapshot_to_ui_elements,
)
from .win32 import IS_WINDOWS, _KEYEVENTF_KEYUP, _SW_RESTORE, _VK_MENU, _kernel32, _user32
from .windows import (
    _select_target_monitor,
    _set_process_dpi_awareness,
    detect_system_dialog,
    enumerate_app_windows,
    enumerate_monitors,
    find_window_by_title,
    find_windows_by_title,
    get_cursor_position,
    is_window_owned_by,
    query_focus_target,
    query_foreground_window,
    query_stuck_modifiers,
)


#: Settle/gap policy between queued final-Enter-ish keystrokes (B3): a terminal-key
#: chord dispatched within this window of the previous keyboard dispatch waits out the
#: remainder first, so a fast follow_ups batch cannot drop the final Enter into an
#: input-stack race. ``CORTEX_KEY_DISPATCH_GAP`` restores/overrides (0 disables).
TERMINAL_KEYS: frozenset[str] = frozenset({"enter", "return", "numpadenter", "tab"})


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean env flag; garbage values fall back to the default."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _env_nonnegative_float(name: str, default: float) -> float:
    """Read a finite non-negative float env value; invalid/negative -> default."""
    value = _env_finite_float(name)
    if value is None or value < 0:
        return default
    return value


def _env_finite_float(name: str) -> float | None:
    """Read a finite float env value; unset/blank/non-finite -> ``None`` (WP2 port)."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except (ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _env_bounded_float(name: str, default: float, low: float, high: float) -> float:
    """Read a finite float only when it is inside the declared range.

    WP2 (ORVEX-2026-101 Batch 5 decision 4) deliberately uses fallback (rather than
    clamping) for out-of-range motion durations/settle values: an operator typo cannot
    silently turn a plan into a long wait. This helper is range-validation, not a
    fixed motion cap; the caller chooses the range from the governing contract.
    """
    value = _env_finite_float(name)
    if value is None or not (low <= value <= high):
        return default
    return value


def _env_bounded_int(name: str, default: int, low: int, high: int) -> int:
    """Read a finite integer env value only when it is inside ``[low, high]``."""
    value = _env_finite_float(name)
    if value is None or not (low <= value <= high):
        return default
    try:
        parsed = int(value)
    except (OverflowError, ValueError):
        return default
    return parsed if low <= parsed <= high else default


def _env_nonnegative_int(name: str, default: int) -> int:
    """Read a finite non-negative int env value; invalid/zero/negative -> default."""
    value = _env_finite_float(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except (OverflowError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _env_int(name: str, default: int) -> int:
    """Read a finite signed int env value; invalid input falls back to default."""
    value = _env_finite_float(name)
    if value is None:
        return default
    try:
        return int(value)
    except (OverflowError, ValueError):
        return default


def _env_choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    """Read an enum env value; garbage/unknown falls back to default."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    return value if value in allowed else default


def _env_positive_int(name: str, default: int) -> int:
    """Read a positive int env value; garbage/zero/negative/non-finite -> default."""
    value = _env_finite_float(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except (OverflowError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _env_int_in_range(name: str, default: int | None, low: int, high: int) -> int | None:
    """Read a finite int env value clamped to ``[low, high]``; invalid -> default.

    R-5 helper for the PNG compress-level knob: the default (``None``) means "PIL's
    own default" so the encoded bytes stay byte-identical unless the operator opts in.
    WP2 port alignment: reads through ``_env_finite_float`` so a non-finite payload
    (``1e400`` -> inf) falls back to the default instead of raising ``OverflowError``
    at import time.
    """
    value = _env_finite_float(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except (OverflowError, ValueError):
        return default
    return max(low, min(high, parsed))


#: Settle/gap policy between queued final-Enter-ish keystrokes (B3, T8): a terminal-key
#: chord dispatched within this many seconds of the previous keyboard dispatch waits
#: out the remainder first, so a fast follow_ups batch cannot drop the final Enter into
#: an input-stack race. ``CORTEX_KEY_DISPATCH_GAP`` overrides (0 disables).
KEY_DISPATCH_GAP_SECONDS = _env_nonnegative_float("CORTEX_KEY_DISPATCH_GAP", 0.05)

#: B8 settle policy (T8): keyboard input is paced behind a RECENT focus transition
#: (focus_window / ensure_app reattach) for this many seconds — the first keys sent
#: while the activated window's thread input queue is still settling are the ones that
#: drop or misdeliver (the B5 wedge topology; the A5 residual risk class).
#: ``CORTEX_FOCUS_SETTLE_SECONDS`` overrides (0 disables). Re-dispatch of the dropped
#: keys is deliberately NOT automatic (a re-issued Enter can double-submit).
FOCUS_TRANSITION_SETTLE_SECONDS = _env_nonnegative_float("CORTEX_FOCUS_SETTLE_SECONDS", 0.3)

# --- R-04 keystroke-burst resilience: chunked typing + integrity verification ---------------
#: Master switch for typed-text integrity verification (default ON). ``0``/``off``
#: restores the exact pre-R-04 behavior (single whole-string dispatch, no read-back,
#: plain ``Executed type.`` message). ``CORTEX_TYPE_INTEGRITY`` overrides.
TYPE_INTEGRITY_ENABLED = _env_bool("CORTEX_TYPE_INTEGRITY", True)
#: Backend-level typing chunk size in characters (``CORTEX_TYPE_CHUNK_CHARS``). Each
#: chunk is dispatched as one engine call and verified against the focused control's
#: read-back value before the next chunk is sent, bounding how much text a burst drop
#: can corrupt before it is detected. 64 chars ≈ one SendInput batch per chunk at the
#: default event budget.
TYPE_CHUNK_CHARS = _env_int_in_range("CORTEX_TYPE_CHUNK_CHARS", 64, 1, 4096)
#: WP12a (user ruling 2026-11-02, "pause by default + flag corruption"): the REAL
#: backend's DEFAULT per-character typing pace. Live runs 4-7 proved Windows 11
#: packaged Notepad (RichEditD2DPT, Arabic layout) corrupts/drops fast unicode
#: bursts even without the product (live runs 4-7: the RAW one-burst leg
#: truncated the text to 'e2e-ty'; the unpaced PRODUCT leg substituted its tail).
#: One diagnostic trial each was exact — 15 ms raw, 20 ms product — so the REAL
#: SendInput path paces by default (WP12c: the paced engine sleeps once per
#: CHARACTER; a surrogate pair travels in one SendInput call). The env
#: override still wins and `0` restores the unpaced burst; the standalone
#: SendInputEngine()/FakeComputerBackend library defaults stay 0.0 so the offline
#: suite stays fast.
DEFAULT_TYPE_INTERVAL_SECONDS = 0.02
#: K1 (LIVE-WP13-02 runs 3-4): a PLAIN hotkey chord (no hold_ms given) holds
#: its keys for this long before releasing them in reverse order. A plain
#: chord is ONE SendInput batch with zero time between the key-down and
#: key-up events, which Windows 11 Store apps IGNORED live (packaged Notepad
#: 11.2607: only 1 of 5 plain alt+f4 chords closed the window, pixel diff 0;
#: the held hold_ms=50 form closed 3 of 3 trials). An EXPLICIT hold_ms still wins, and
#: hold_ms=0 keeps its documented meaning: the explicit zero-duration split
#: press/release with zero wait — NOT the old single-batch chord, which is no
#: longer reachable for a hotkey.
DEFAULT_HOTKEY_HOLD_MS = 50
#: WP13 (field e1/e2, Addendum 4): the HOST's per-tool-call timeout — one knob,
#: ``CORTEX_CLIENT_TIMEOUT_SECONDS`` (default 60, band 10..600; out-of-band or
#: unparsable values fall back to the default). Every long-blocking bound is
#: DERIVED from it with a fixed safety margin: at 60 the margins reproduce the
#: shipped 45 s deadline / 36 s pacing budget / 45 s wait segments; at a 30 s
#: host (ZCode) every bound drops to 15 s so no call can block past the host's
#: timeout. Single source of truth: desktop.client_budget (imported below).
#: One wall-clock deadline per type action — pacing (counted per CHARACTER since
#: the WP12c per-character pacing: a surrogate pair travels in ONE SendInput call
#: with ONE sleep), the per-character probes, verification and a possible repair
#: must together stay inside the derived deadline. The paced-dispatch share of
#: that deadline is 80% of it; the dispatch loop checks the deadline per chunk
#: and finishes the rest UNPACED if the paced rest would overrun; a repair runs
#: only if it fits the REMAINING deadline, else the reply is unverified with
#: branch=repair_over_budget (never typed).


#: Settle before the post-dispatch integrity read-back: SendInput returns when events
#: are ACCEPTED by the input stack, not when the target app has processed them —
#: reading too early would mistake queue lag for a drop.
#: ``CORTEX_TYPE_VERIFY_SETTLE_SECONDS`` overrides (0 disables the wait).
TYPE_VERIFY_SETTLE_SECONDS = _env_nonnegative_float("CORTEX_TYPE_VERIFY_SETTLE_SECONDS", 0.05)
#: Spacing between the quick reads of the fast verification path (default 0.04 s):
#: close enough to keep the happy path within the ~150 ms overhead target, far enough
#: that each read observes a later buffer state. ``CORTEX_TYPE_VERIFY_CONFIRM_GAP_SECONDS``
#: overrides.
TYPE_VERIFY_CONFIRM_GAP_SECONDS = _env_nonnegative_float("CORTEX_TYPE_VERIFY_CONFIRM_GAP_SECONDS", 0.04)
#: Total quick reads the fast path may spend (first read + up to N-1 confirm/poll
#: reads). The live profile shows read1 at +50 ms is routinely MID-DRAIN (58 of 81
#: chars); the poll follows the monotone growth until the buffer stabilizes or
#: completes, instead of paying the 2.5 s horizon on every such action.
#: ``CORTEX_TYPE_VERIFY_FAST_MAX_READS`` overrides (clamped 2..8).
TYPE_VERIFY_FAST_MAX_READS = _env_int_in_range("CORTEX_TYPE_VERIFY_FAST_MAX_READS", 4, 2, 8)
#: Landing-lag horizon — ESCALATION ONLY (fix3): consulted ONLY when the quick pair is
#: suspicious (shrink/divergence) or agrees on a genuine partial, before any repair
#: decision. The 2026-09-13 live evidence measured buffers stale-short at +2.0 s that
#: drained later; a repair decided on such a read double-applies. The happy path never
#: waits here. ``CORTEX_TYPE_VERIFY_STABILITY_SECONDS`` overrides.
TYPE_VERIFY_STABILITY_SECONDS = _env_nonnegative_float("CORTEX_TYPE_VERIFY_STABILITY_SECONDS", 2.5)
#: Repair bound: a dropped suffix is re-dispatched AT MOST ONCE per action, and only
#: after the double-read-stable buffer PROVED text is missing (verified retype — never
#: blind). A confirmed still-wrong buffer raises :class:`TextIntegrityError`.
TYPE_MAX_RETYPES_PER_CHUNK = 1

# --- WP2 (ORVEX-2026-101 Batch 2, P2.2/P2.3): internal motion-engine configuration ----------
# Motion is an INTERNAL detail (the governing user clarification: the model declares
# locations and actions only). These keys are the internal optional overrides — the
# only host-facing override surface is the optional ``duration_ms`` field on move
# actions / plan points. Defaults are the RFC §3.6 approved values.
MOVE_DURATION_ENV = "CORTEX_MOVE_DURATION_MS"  # ms; default 0 = the legacy jump (outside plans)
PLAN_MOVE_DURATION_ENV = "CORTEX_PLAN_MOVE_DURATION_MS"  # ms; the in-plan transition default
MOVE_SHAPE_ENV = "CORTEX_MOVE_SHAPE"  # "linear" | "quadratic_bezier"
MOVE_BEND_PX_ENV = "CORTEX_MOVE_BEND_PX"  # signed peak offset; +h displaces toward +y (D19)
MOVE_EASING_ENV = "CORTEX_MOVE_EASING"  # "linear" | "smoothstep" (timing-only, D20)
PLAN_SAMPLE_INTERVAL_ENV = "CORTEX_PLAN_SAMPLE_INTERVAL_MS"  # sampling cadence, band 8-16
MOVE_SETTLE_ENV = "CORTEX_MOVE_SETTLE_MS"  # cursor-reposition settle (P3.2/D15)

#: WP2 (ORVEX-2026-101 Batch 2, P2.2/P2.3): resolved motion-engine defaults (RFC §3.6).
#: Outside plans, a ``move`` without ``duration_ms`` reads this. Decision 4 declares
#: accepted operator values 200..500ms; the commander-approved Batch-5 scope keeps the
#: *unset* default at 0 so legacy instant moves remain intact.
#: ORVEX-2026-102 §5.1 / SPEC 5.1 leaves this at 0 — the outside-plan move is
#: INSTANT by policy, and this is the value that must never be "fixed" upward.
MOVE_DURATION_MS = _env_bounded_int(MOVE_DURATION_ENV, 0, 200, 500)
#: Inside plans, an unset transition duration reads this. ORVEX-2026-102 §5.2 /
#: SPEC 5.2 (conflict C3): inside a plan the duration is REQUIRED FROM THE MODEL,
#: so this constant is a FAIL-SAFE for an unstated value, not a behaviour. The
#: default is 0 (an instant jump) and the accepted band is 0..500. Consulted ONLY by
#: the plan normalizer (``computer_use_mcp.plan.normalize_plan`` — WP7a owns the
#: consumer; a flat action never touches this constant, pinned by
#: tests/test_w5_motion_policy.py).
PLAN_MOVE_DURATION_MS = _env_bounded_int(PLAN_MOVE_DURATION_ENV, 0, 0, 500)
#: Motion shape family (D6): linear by default — the functional benefit of curves is
#: NOT PROVEN; the jump already lands with 0 px error.
MOVE_SHAPE = _env_choice(MOVE_SHAPE_ENV, "linear", ("linear", "quadratic_bezier"))
#: Default signed bend in px (0 = straight). +h displaces the path peak toward +y (D19).
#: DELTA 1 (F7): the knob is BOUNDED — band -5000..5000 px, out-of-band falls back to
#: the 0 default (fallback-not-clamp, like every motion knob). The SCREEN envelope is
#: the real bound and stays: path construction still silently reduces any bend (in-band
#: or not) to the largest same-sign value whose complete sampled path fits the monitor
#: rectangle (decision 5; see ``_motion_path_points``).
MOVE_BEND_PX = _env_bounded_int(MOVE_BEND_PX_ENV, 0, -5_000, 5_000)
#: Default easing (D20, timing-only): linear identity; "smoothstep" = 3u²-2u³.
MOVE_EASING = _env_choice(MOVE_EASING_ENV, "linear", ("linear", "smoothstep"))
#: Motion sampling cadence in ms. DELTA 1 (F7): fallback-NOT-clamp in the declared
#: 8..16 band — an out-of-band value falls back to the 12 default instead of being
#: silently clamped to an edge (the K-12 class: with clamping, an operator typo
#: silently means a different cadence; with fallback it means the shipped cadence).
#: Consumed by the flat timed-move sampling (NOT plan-only), so the knob stays.
PLAN_SAMPLE_INTERVAL_MS = _env_bounded_int(PLAN_SAMPLE_INTERVAL_ENV, 12, 8, 16)
#: Cursor-reposition settle in MILLISECONDS. ORVEX-2026-102 §5.1 / SPEC 5.1
#: (conflict C3) reverses the legacy 50ms default: outside plans the move is
#: INSTANT and the settle is 0ms, so the DEFAULT is 0.0 and the accepted band is
#: 0..200 — range-validation-with-fallback, not clamping (an out-of-band value falls
#: back to the default). An operator who WANTS a settle sets ``CORTEX_MOVE_SETTLE_MS``
#: to 1..200. THIS IS A BEHAVIOR CHANGE vs v0.7.5: the unconditional 50 ms
#: post-move settle is gone (recorded for the VERSIONS Compatibility notes).
MOVE_SETTLE_MS = _env_bounded_float(MOVE_SETTLE_ENV, 0.0, 0.0, 200.0)
#: Seconds form consumed by the dispatch paths (the historical module constant, now
#: env-resolved; kept under the same name so every existing reference holds).
#: With the shipped 0.0 default both guarded settle sites below (``execute()``'s
#: plan-point tail and the unmarked move branch) are short-circuited by their
#: ``> 0`` guards: zero settle sleep on every path, with NO new branch and NO new
#: ``time.sleep`` site.
_MOVE_SETTLE_SECONDS = MOVE_SETTLE_MS / 1000.0  # settle after a cursor reposition
#: Control types whose value is ALWAYS exposed by both read paths when the control is
#: readable — a ``None`` value on these means the control is EMPTY, not unreadable.
_ALWAYS_VALUED_CONTROL_TYPES = frozenset({"Edit", "Document"})


def _landed_prefix_len(landed_n: str, expected_n: str) -> int:
    """Longest prefix of ``expected_n`` that the real buffer ``landed_n`` ENDS WITH.

    Suffix-diff semantics (fix2 redesign): typed text lands at the control's tail, so
    the buffer's TAIL is matched against the expected text's prefixes and NO baseline
    read is needed — pre-existing control content is simply tolerated. ``k ==
    len(expected_n)`` means fully landed; ``k > 0`` is the measured dropped-burst
    signature (repair = exactly ``expected[k:]``); ``k == 0`` with a NON-empty buffer
    means nothing of ours is recognizable (masked/transformed/foreign — appending
    would double-apply, so that class is never repaired).
    """
    for k in range(len(expected_n), -1, -1):
        if landed_n.endswith(expected_n[:k]):
            return k
    return 0


def _normalize_landed_text(value: str) -> str:
    """Normalize a control's read-back value for comparison against typed text.

    Apps store a typed Enter (``\\n`` → VK_RETURN) as ``\\r\\n`` (edit controls), so
    both sides are folded to bare ``\\n`` before comparison. Everything else is compared
    verbatim (a masked/password field will NOT normalize to the typed text — that is
    correct: such fields report ``mismatch`` and are never "repaired").
    """
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _split_type_chunks(text: str, size: int) -> list[str]:
    """Split ``text`` into backend-level typing chunks of at most ``size`` code points.

    Python strings are code-point sequences, so a chunk boundary can never separate a
    UTF-16 surrogate pair: an astral character is ONE element here, and the engine's
    ``_text_to_key_units`` (which performs the UTF-16 surrogate encoding for
    ``KEYEVENTF_UNICODE``) always emits an astral character's down+up pair within
    whichever chunk contains it. ``size < 1`` or empty text yields a single
    empty/whole chunk respectively (an empty text is a legacy no-op upstream).
    """
    if size < 1 or len(text) <= size:
        return [text]
    return [text[start : start + size] for start in range(0, len(text), size)]


def _typed_index_for_norm_len(text: str, norm_len: int) -> int:
    """Index into ``text`` whose normalized prefix has exactly ``norm_len`` code units.

    Maps a read-back-derived length (normalized ``\\r\\n`` counting) back onto the
    ORIGINAL typed text so a repair re-dispatches exactly the typed characters that are
    still missing. Clamps to ``len(text)``.
    """
    if norm_len <= 0:
        return 0
    count = 0
    index = 0
    while index < len(text) and count < norm_len:
        if text[index] == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
            index += 2  # typed CRLF is ONE normalized unit
        else:
            index += 1
        count += 1
    return index


def _format_integrity_status(
    verified: int, total: int, *, mismatch: int, unverified: int, healed: int
) -> str:
    """Additive action-message suffix summarizing one ``type`` dispatch's integrity.

    Statuses: ``verified`` (every chunk's read-back matched), ``partial`` (some chunks
    matched, some could not be read), ``unverified`` (no read-back was possible — never
    claimed as verified), ``mismatch`` (at least one chunk's read-back disagreed and was
    not repairable by the bounded verified retype). ``healed`` reports characters
    delivered by a successful verified retype.
    """
    if mismatch:
        status = "mismatch"
    elif unverified >= total:
        status = "unverified"
    elif unverified:
        status = "partial"
    else:
        status = "verified"
    suffix = f"integrity={status}({verified}/{total})"
    if healed:
        suffix += f" healed={healed}"
    return suffix


# --- v0.7.1 (Defect C): clipboard text-entry transport (raw ctypes, zero new deps) -----------
#: Typed diagnostic for the honest no-read-back outcome (additive action-message suffix;
#: the ``Executed type.`` prefix and the ``integrity=`` suffix stay stable). Fired when the
#: final status is ``unverified`` AND the semantic reader could not even identify a
#: readable focused target (reader absent/unavailable, read failed, or NO focused element
#: in the snapshot) — the OpenGL/console-app shape (field case: Blender). A focused
#: control that EXISTS but exposes no value does NOT fire the marker: a target is
#: present, the app merely does not report its text.
TYPE_UNCONFIRMED_MARKER = "TYPE_UNCONFIRMED no-readable-target"
#: Hint after the marker on the DEFAULT per-char transport: the actionable escape hatch
#: is the clipboard transport plus visual verification with a stated expected_effect.
TYPE_UNCONFIRMED_HINT_SENDINPUT = (
    'verify visually with a stated expected_effect (or retry with via="clipboard")'
)
#: Clipboard-transport variant: the transport is already the recommended one, so the hint
#: names only the verification path.
TYPE_UNCONFIRMED_HINT_CLIPBOARD = (
    "verify visually with a stated expected_effect (screenshot/observe after the action)"
)


def _validate_action_defense(action: GroundedAction) -> None:
    """WP2: fail-closed optional-field gate shared by real and fake backends.

    The third enforcement site for the new optional fields (after the models'
    construction validators and the pipeline validator's hold gate): a
    ``model_construct``-style bypass carrying a misuse past both upstream gates dies
    HERE, before a single engine call could be made. ``hold_ms=None`` /
    ``duration_ms=None`` / absent plan markers ride through untouched.
    """
    if action.hold_ms is not None:
        if action.action not in GroundedAction.HOLD_ACTIONS:
            # ORVEX-2026-107: message only — the legal set is
            # click/right_click/keypress/hotkey now; the type/range gate below is
            # untouched, so a bad hold_ms is still stopped for the reason that matters.
            raise ValueError("hold_ms is only valid on click/right_click/keypress/hotkey actions")
        if (
            isinstance(action.hold_ms, bool)
            or not isinstance(action.hold_ms, int)
            or not 0 <= action.hold_ms <= MAX_HOLD_MS
        ):
            raise ValueError(
                f"hold_ms must be an integer between 0 and {MAX_HOLD_MS}; "
                f"got {action.hold_ms!r}"
            )
    if action.duration_ms is not None:
        if action.action != "move":
            raise ValueError("duration_ms is only valid on move actions")
        if (
            isinstance(action.duration_ms, bool)
            or not isinstance(action.duration_ms, int)
            or not 0 <= action.duration_ms <= MAX_DURATION_MS
        ):
            raise ValueError(
                f"duration_ms must be an integer between 0 and {MAX_DURATION_MS}; "
                f"got {action.duration_ms!r}"
            )
    if (
        isinstance(action.delta, bool)
        or not isinstance(action.delta, int)
        or not -20 <= action.delta <= 20
    ):
        raise ValueError(f"delta must be an integer between -20 and 20; got {action.delta!r}")
    if action.plan_point_settle is not None and not isinstance(
        action.plan_point_settle, bool
    ):
        raise ValueError(
            "plan_point_settle must be a boolean when present; "
            f"got {action.plan_point_settle!r}"
        )
    if action.plan_point_settle_ms is not None and (
        isinstance(action.plan_point_settle_ms, bool)
        or not isinstance(action.plan_point_settle_ms, int)
        or not 0 <= action.plan_point_settle_ms <= MAX_PLAN_SETTLE_MS
    ):
        raise ValueError(
            "plan_point_settle_ms must be an integer between 0 and "
            f"{MAX_PLAN_SETTLE_MS}; got {action.plan_point_settle_ms!r}"
        )


def _plan_point_settle_seconds(action: GroundedAction) -> float:
    """The settle a marked plan-point item owes, in seconds (WP2, SPEC 5.2).

    The settle is a thing the MODEL STATES inside a plan: the effective value is the
    point's declared ``settle_ms`` (carried on the internal, plan-only
    ``GroundedAction.plan_point_settle_ms`` — E-01's wiring) when one was stated, and
    otherwise the module policy ``_MOVE_SETTLE_SECONDS`` — which is ``0.0`` on the
    shipped path. This helper deliberately does NOT introduce a new sleep site: the
    settle branch still calls the one ``time.sleep`` it always called, now with the
    resolved per-action value.
    """
    declared = action.plan_point_settle_ms
    if declared is None:
        return _MOVE_SETTLE_SECONDS
    return declared / 1000.0


def _mark_action_dispatched(action: GroundedAction) -> None:
    """Record private B4-8 evidence after an input/fake dispatch actually returns."""
    action._dispatch_started = True


class ComputerBackend(ABC):
    """Platform contract: observation capture, stop-checked input, interruptible waiting."""

    _active_context: _CaptureContext | None = None

    @abstractmethod
    def observe(self) -> Observation:
        """Capture a full Observation of the current computer state."""

    @abstractmethod
    def execute(
        self,
        action: GroundedAction,
        stop: StopToken | None = None,
        focus_hook: Callable[[], None] | None = None,
        allow_launch: bool = False,
    ) -> str:
        """Execute one grounded action and return a human-readable result message.

        ``stop`` is optional for backward compatibility: when None no cancellation checks
        are possible and the action still executes. Implementations must call
        ``StopToken.ensure_live()`` immediately before every physical input call.
        Raises ``TaskStopped`` when a provided stop token has fired (zero inputs are
        performed in that case).

        ``focus_hook`` (T8, trailing optional): for ``type`` actions, a zero-arg callable
        invoked before every dispatch chunk AFTER the stop-token check (the FocusGuard's
        per-chunk focus-continuity probe piggybacks this cadence). Implementations
        without chunking invoke it once before dispatching; the hook may raise
        :class:`FocusDriftError` to abort the in-flight type. Omitted (legacy callers)
        keeps behavior byte-identical.

        ``allow_launch`` (T8, trailing optional): the ONLY path through which
        ``ensure_app`` may spawn a process (host-authorized server-side launch policy);
        the default False keeps ensure_app a pure attach-probe.
        """

    def query_foreground_window(self) -> WindowInfo | None:
        """Strong identity of the current foreground window; ``None`` when unavailable."""
        return None

    def identity_probe(self, reference: Observation | None = None) -> Observation | None:
        """Current screen-IDENTITY snapshot without a pixel capture (R-5 W1 guard).

        Returns a lightweight Observation carrying EXACTLY the fields the validator's
        staleness check consumes — screenshot dimensions, coordinate space/scales,
        active-window identity, and monitor identity/bounds — with the reference's
        image payload reused verbatim (the pixels are not part of any identity
        dimension). ``None`` (the default for backends without the capability) means
        "cannot probe": the caller falls back to a full validate capture exactly as
        before. The probe NEVER fabricates identity: every field is read from the OS
        the same way ``observe()`` reads it; anything unreadable degrades to ``None``
        and the caller re-captures.
        """
        return None

    def is_window_alive(self, hwnd: int | None) -> bool:
        """Whether a previously bound window still exists (B6 TARGET_GONE probe).

        Default ``True``: backends that cannot track liveness keep the conservative
        rejection semantics (TARGET_GONE never fires on a guess).
        """
        return True

    def is_window_owned_by(self, hwnd: int | None, ancestor_hwnd: int | None) -> bool:
        """GW_OWNER-chain ownership probe (B11 shared rule); default ``False``.

        Backends that cannot compute ownership keep the conservative semantics (the
        guard then relies on the same-process rule only).
        """
        return False

    def query_focus_target(self) -> dict[str, object] | None:
        """Keyboard-focus target probe (T8 mechanism iv); ``None`` = cannot verify."""
        return None

    def detect_system_dialog(
        self, bound_hwnd: int | None, title_table: Sequence[str] | None = None
    ) -> dict[str, object] | None:
        """Modal-dialog foreground probe (T8 mechanism iii); ``None`` = no dialog found."""
        return None

    def enumerate_app_windows(self, process_name: str) -> list[AppWindowCandidate]:
        """Process-filtered window enumeration for attach-or-launch (T8 mechanism ii)."""
        return []

    def query_stuck_modifiers(self, keys: Sequence[str]) -> list[str]:
        """Modifiers currently held down before a chord (T8 mechanism v); empty = clear."""
        return []

    def release_modifiers(self, keys: list[str]) -> None:
        """Synthetic key-up for NAMED session-dispatched modifiers (release mode, T8)."""
        return

    def focus_window_title(self, title: str) -> str:
        """Refocus primitive for the guard's ``refocus_then_abort`` policy (T8).

        Reuses the verified foreground-switch sequence; raises
        :class:`WindowFocusError` on refusal (never a silent failure).
        """
        raise UnsupportedActionError("This backend cannot focus windows by title.")

    def ensure_app(self, target: str, allow_launch: bool = False) -> str:
        """Attach-or-launch probe (T8 mechanism ii); REATTACHED/AMBIGUOUS_INSTANCE/NO_INSTANCE."""
        raise UnsupportedActionError("This backend cannot ensure applications.")

    def wait(self, seconds: float, stop: StopToken | None = None) -> float:
        """Interruptible sleep; returns the seconds actually waited.

        Early-returns (without raising) when the stop token fires; callers detect the
        abort via the returned duration or by checking the token themselves.
        """
        return interruptible_wait(seconds, stop)

    def find_window_by_title(self, target: str) -> WindowInfo | None:
        """Resolve a window title to strong identity; ``None`` when it cannot be found.

        Documented default: backends without window enumeration report no windows (the
        agent-level focus allowlist then fails closed with
        ``process_identity_unavailable``). Platform backends override this.
        """
        return None

    def _map_to_physical(self, x: int, y: int) -> tuple[int, int]:
        """Map screenshot-local coordinates to physical input coordinates.

        This is the ONLY screenshot-to-physical transform in the pipeline (module
        docstring invariant, F1): ``physical = origin + screenshot * scale``, applied
        exactly once here at execution. Action points therefore always arrive in
        screenshot space — grounding validates bounds and records the scale but never
        pre-scales the point.

        Uses the context recorded by the most recent ``observe()`` (the observation the
        action was grounded from); refuses coordinate input when that context is
        ``UNVERIFIABLE``. Without any prior observation, coordinates are assumed to be
        physical virtual-screen pixels (legacy passthrough behavior).
        """
        context = self._active_context
        if context is None:
            context = self._build_default_context()
            self._active_context = context
        if context.verdict.space is CoordinateSpace.UNVERIFIABLE:
            raise CoordinateSpaceError(
                "Coordinate-space integrity could not be verified; refusing coordinate input."
            )
        return context.transform.to_physical(x, y)

    @abstractmethod
    def _build_default_context(self) -> _CaptureContext:
        """Best-effort coordinate context when execute() runs before any observe()."""


class LocalComputerBackend(ComputerBackend):
    """Real Windows backend: Win32 identity/DPI/monitors + stop-checked engine input.

    The physical-input hot path is a pluggable :class:`InputEngine`:

    - ``sendinput`` (default): raw Win32 ``SendInput`` via ctypes — batched absolute
      mouse moves/clicks, layout-proof ``KEYEVENTF_UNICODE`` typing, and an
      interpolated ~8 px drag stroke paced ~1 ms per segment (L1-NEW-2: drawing
      apps sample held-button moves, not a press/teleport/release triplet);
      ``CORTEX_DRAG_INTERPOLATE=0`` restores the minimal teleport stroke.
    - ``pyautogui`` (selectable fallback, ``CORTEX_INPUT_BACKEND=pyautogui``): the
      legacy path with ``PAUSE=0`` by default (``CORTEX_PYAUTOGUI_PAUSE`` restores it).

    Both engines honor the same contract: failsafe screen-corner checks raise
    :class:`InputBlockedError`, and ``execute`` checks the stop token before every
    physical input.
    """

    def __init__(
        self,
        *,
        input_engine: str | None = None,
        png_optimize: bool | None = None,
        uia_read: bool | None = None,
        type_interval: float | None = None,
        pyautogui_pause: float | None = None,
        drag_interpolate: bool | None = None,
        png_compress_level: int | None = None,
    ) -> None:
        self._system = platform.system()
        if self._system != "Windows":
            raise RuntimeError(
                "LocalComputerBackend currently requires Windows. "
                "Use FakeComputerBackend for tests or add a platform adapter."
            )
        self.dpi_awareness: str = _set_process_dpi_awareness()
        self.dpi_estimated: bool = False
        self._monitors: list[MonitorInfo] = []
        import mss  # type: ignore[import-not-found]

        self.mss_dpi_neutralized = self._neutralize_mss_dpi_awareness(mss)
        # mss.MSS is the supported factory on mss >= 10.2; mss.mss() is the legacy path.
        self._mss_factory: Callable[[], object] = getattr(mss, "MSS", None) or mss.mss
        import pyautogui  # type: ignore[import-not-found]

        self._pyautogui = pyautogui
        pyautogui.FAILSAFE = True
        self._engine = self._build_input_engine(
            input_engine, type_interval, pyautogui_pause, drag_interpolate
        )
        self.png_optimize: bool = (
            _env_bool(PNG_OPTIMIZE_ENV, False) if png_optimize is None else png_optimize
        )
        # R-5 (W6): PNG compress_level knob. The default resolves to PIL's own default
        # (6), so the encoded bytes are IDENTICAL to the pre-R-5 pipeline unless the
        # operator opts in (level 1 measured ~21.6 ms vs ~27 ms per 1920x1080 frame at
        # +45% size — a pure speed/size trade for mechanical throughput).
        self.png_compress_level: int | None = (
            _env_int_in_range(PNG_COMPRESS_LEVEL_ENV, None, 0, 9)
            if png_compress_level is None
            else max(0, min(9, int(png_compress_level)))
        )
        # R-5 (W5): one persistent mss capture instance for the backend's life. A fresh
        # instance per grab paid a first-BitBlt warmup every capture; a held instance
        # keeps its DIB section allocated. Any grab failure discards the instance so
        # the next capture gets a fresh one (fail-safe against a lost DC).
        self._mss_capture: object | None = None
        # R-6: capture backend selection. "dxgi" lazily constructs ONE
        # _DxgiDuplicator for the backend's life (primary output); any failure
        # during construction or grab permanently falls back to the mss BitBlt
        # path (fail-open: a broken fast path may cost speed, never a capture).
        self._capture_backend: str = (
            "dxgi" if os.getenv(CAPTURE_BACKEND_ENV, "").strip().casefold() == "dxgi"
            else "blt"
        )
        self._dxgi: _DxgiDuplicator | None = None
        # R-6 encode-skip (session attribute; default False): True = every capture in
        # THIS session encodes the fast raw-pixel key instead of a lossless PNG (the
        # server sets it only for image_delivery="text" sessions, where no image block
        # is ever emitted, so the only payload consumers are the digest and the
        # staleness equality checks — both served by the deterministic key).
        # ``_raw_key_enabled`` (computed below, after frame reuse resolves) is the
        # invariant gate: True only when frame reuse is also on (the raw frame is
        # then the pixel source of record for verification).
        self._raw_payload_keys: bool = False
        # R-6 (dxgi): one-entry payload cache for the duplication path, keyed by the
        # sha256 of the packed BGRA bytes (the blt cache compares the raw bytes
        # directly; the dxgi readback materializes its packed bytes only inside
        # _readback, so a hash key avoids holding/comparing another 8 MB buffer).
        self._dxgi_payload_cache: tuple[tuple[int, int], str, str] | None = None
        # R-5 (W2/W6): internal-frame reuse — capture-time RGB frames travel with the
        # Observation (verification diff without a PNG re-decode) and the previous
        # capture's identical-pixel payload is reused instead of re-encoded.
        # ``CORTEX_INTERNAL_FRAME_REUSE=0`` disables both (pure legacy path).
        self._internal_frame_reuse: bool = _env_bool(INTERNAL_FRAME_REUSE_ENV, True)
        self._payload_cache: tuple[tuple[int, int], Any, str] | None = None
        # R-6 encode-skip invariant gate (see the comment at ``_raw_payload_keys``):
        # raw keys only when frame reuse is on (the stash is the pixel source of
        # record) AND the CORTEX_TEXT_PNG kill-switch is not set.
        self._raw_key_enabled: bool = self._internal_frame_reuse and (
            not _env_bool(TEXT_PNG_ENV, False)
        )
        self._uia_enabled: bool = _env_bool(UIA_READ_ENV, True) if uia_read is None else uia_read
        self._semantic_reader: UiaSemanticReader | Win32TextReader | None = None
        if self._uia_enabled:
            uia_reader = UiaSemanticReader()
            # Raw UIA COM first; the Win32 window-text reader is the sanctioned fallback
            # (PERF-004: this hardening-affected box refuses IUIAutomation from
            # CUIActivation via raw COM in every process — see change-log).
            # WP13 DELTA-2: the warm runs under the derived WARM wall (full call
            # budget share, capped at 10 s). Only a warm that RAN and FAILED
            # swaps to the Win32 reader; a warm that TIMED OUT keeps the UIA
            # reader in place (the queued warm may still complete on the owner
            # thread — later reads then work).
            warm_state = uia_reader.warm()
            self._semantic_reader = uia_reader if warm_state != "failed" else Win32TextReader()
        # Single warm only: the DELTA-1 double warm (swap-check + unconditional
        # re-warm) re-ran the whole COM warm pointlessly; the Win32 fallback
        # reader's warm is a no-op binding check that its own constructor
        # already covers.
        self._refresh_monitors()
        # B3 settle/gap policy: monotonic timestamp of the last keyboard dispatch
        # (0.0 = epoch; the first dispatch of a session never waits).
        self._last_key_dispatch: float = 0.0
        # B8 settle policy: monotonic timestamp of the last focus transition.
        self._last_focus_transition: float = 0.0

    def _build_input_engine(
        self,
        input_engine: str | None,
        type_interval: float | None,
        pyautogui_pause: float | None,
        drag_interpolate: bool | None,
    ) -> InputEngine:
        """Resolve the input engine from the argument/env/default precedence."""
        name = (input_engine or os.getenv(INPUT_BACKEND_ENV) or "sendinput").strip().lower()
        if name not in {"sendinput", "pyautogui"}:
            raise ValueError(
                f"unknown input engine {name!r}: expected 'sendinput' or 'pyautogui'"
            )
        interval = (
            _env_nonnegative_float(
                TYPE_INTERVAL_ENV,
                # WP12b MINOR: the paced default is a SendInput-path mitigation
                # (Windows 11 Notepad burst corruption); the pyautogui fallback
                # keeps its previous unpaced default.
                DEFAULT_TYPE_INTERVAL_SECONDS if name == "sendinput" else SENDINPUT_TYPE_INTERVAL,
            )
            if type_interval is None
            else max(0.0, type_interval)
        )
        engine: InputEngine
        if name == "pyautogui":
            pause = (
                _env_nonnegative_float(PYAUTOGUI_PAUSE_ENV, 0.0)
                if pyautogui_pause is None
                else max(0.0, pyautogui_pause)
            )
            engine = PyAutoGuiInputEngine(self._pyautogui, pause=pause, type_interval=interval)
        else:
            # L1-NEW-2 live default: the SendInput drag stroke is interpolated
            # (~8 px segments), paced (~1 ms each — a 400 px line ≈ 80 ms, far
            # under the ~150 ms drag budget and the ~52 ms/action target is
            # untouched for every other action). A bare SendInputEngine keeps the
            # legacy minimal defaults; the FACTORY wires the dense stroke so the
            # unit-pinned engine contract stays frozen. ``CORTEX_DRAG_INTERPOLATE=0``
            # restores the teleport stroke (applied uniformly by the override
            # block below); ``CORTEX_DRAG_STEP_PAUSE`` / ``CORTEX_DRAG_STEP_PIXELS``
            # retune cadence and granularity.
            engine = SendInputEngine(
                type_interval=interval,
                drag_interpolate=True,
                drag_step_pause=_env_nonnegative_float(
                    DRAG_STEP_PAUSE_ENV, _SENDINPUT_FACTORY_DRAG_STEP_PAUSE
                ),
                drag_step_pixels=_env_positive_int(
                    DRAG_STEP_PIXELS_ENV, _SENDINPUT_FACTORY_DRAG_STEP_PIXELS
                ),
            )
        if drag_interpolate is not None:
            engine.drag_interpolate = drag_interpolate
        else:
            env_flag = os.getenv(DRAG_INTERPOLATE_ENV)
            if env_flag is not None and env_flag.strip():
                engine.drag_interpolate = _env_bool(DRAG_INTERPOLATE_ENV, engine.drag_interpolate)
        return engine

    @staticmethod
    def _neutralize_mss_dpi_awareness(mss_module: object) -> bool:
        """Stop mss from re-setting process DPI awareness; this backend owns it.

        mss (10.x) calls ``shcore.SetProcessDpiAwareness(2)`` in every instance
        constructor and crashes with ``PermissionError`` (E_ACCESSDENIED) whenever
        awareness is already set — which this backend deliberately does first
        (per-monitor-v2). Returns True when the neutralization applied.
        """
        try:
            from mss.windows import gdi  # type: ignore[import-not-found]

            def _no_awareness(self: object) -> None:
                return None

            gdi.MSSImplGdi._set_dpi_awareness = _no_awareness  # type: ignore[attr-defined]
            return True
        except (AttributeError, ImportError, TypeError):
            return False

    def _per_monitor_dpi_available(self) -> bool:
        return self.dpi_awareness in {"per_monitor_v2", "per_monitor"}

    def _mss_monitors(self) -> list[dict[str, int]] | None:
        try:
            with self._mss_factory() as capture:
                return list(capture.monitors)
        except Exception:  # noqa: BLE001 - any mss failure degrades to the enum path
            return None

    def _mss_grab(self, region: dict[str, int]) -> Any:
        """BitBlt one region through the backend's persistent mss instance (R-5 W5).

        A held instance keeps its DIB section allocated, so steady-state grabs skip the
        per-capture first-BitBlt warmup a fresh ``mss.MSS()`` pays. Any failure closes
        and discards the instance (fail-safe: the next grab opens a fresh one) and the
        exception propagates to the existing capture-failure handling.
        """
        capture = self._mss_capture
        if capture is None:
            capture = self._mss_factory()
            self._mss_capture = capture
        try:
            return capture.grab(region)
        except Exception:
            # The instance's DC/DIB may be gone (mode change, session lock); drop it so
            # the NEXT capture starts clean instead of persisting a dead handle.
            try:
                close = getattr(capture, "close", None)
                if callable(close):
                    close()
            finally:
                self._mss_capture = None
            raise

    def _refresh_monitors(self) -> None:
        monitors, dpi_estimated = enumerate_monitors(
            self._mss_monitors(), per_monitor_dpi=self._per_monitor_dpi_available()
        )
        self._monitors = monitors
        self.dpi_estimated = dpi_estimated

    def observe(self, monitor_index: int | None = None) -> Observation:
        """Capture a full Observation of the current computer state.

        Targets the monitor containing the cursor (fallback: primary monitor), or
        ``monitor_index`` when provided. Raises ``DisplayUnavailableError`` when capture
        fails; window/cursor identity and the UIA semantic read degrade to None instead
        of failing.

        R-5 (W2/W6): the capture-time RGB frame is stashed on the returned Observation
        (private ``_frame``) and the encoded PNG payload is kept in a ONE-ENTRY cache —
        a capture whose raw pixels are byte-identical to the previous capture's reuses
        the previous payload (PNG encoding is deterministic, so identical pixels produce
        identical payloads BY CONSTRUCTION; digests, staleness proofs, and pixel diffs
        are unaffected). ``CORTEX_INTERNAL_FRAME_REUSE=0`` disables both stashes.
        """
        if self._system != "Windows":
            raise DisplayUnavailableError("Screen capture requires Windows.")
        self._refresh_monitors()
        cursor_physical = get_cursor_position()
        monitor = _select_target_monitor(self._monitors, cursor_physical, monitor_index)
        left, top, width, height = monitor.bounds
        if width <= 0 or height <= 0:
            raise DisplayUnavailableError(f"Monitor {monitor.id} has invalid bounds {monitor.bounds}.")
        encoded, image_width, image_height, frame = self._grab_png(left, top, width, height)
        window_info = query_foreground_window()
        verdict = classify_coordinate_space(
            image_width, image_height, monitor, dpi_estimated=self.dpi_estimated
        )
        cursor_local: tuple[int, int] | None = None
        if cursor_physical is not None:
            cursor_local = CoordinateTransform(
                origin_x=left, origin_y=top, scale_x=verdict.scale_x, scale_y=verdict.scale_y
            ).to_screenshot(*cursor_physical)
        self._active_context = _CaptureContext(monitor=monitor, verdict=verdict)
        ocr_text, ui_elements, ocr_dropped = self._uia_semantic_fields(monitor, verdict)
        observation = Observation(
            image_base64=encoded,
            width=image_width,
            height=image_height,
            active_window=(window_info.title or None) if window_info else None,
            cursor_x=cursor_local[0] if cursor_local else None,
            cursor_y=cursor_local[1] if cursor_local else None,
            input_width=verdict.input_width,
            input_height=verdict.input_height,
            coordinate_scale_x=verdict.scale_x,
            coordinate_scale_y=verdict.scale_y,
            coordinate_space=verdict.space,
            monitor=monitor,
            active_window_info=window_info,
            ocr_text=ocr_text,
            ui_elements=ui_elements,
            ocr_dropped=ocr_dropped,
            redactions_applied=False,
        )
        if self._internal_frame_reuse:
            observation._frame = frame
            # R-7: the capture's raw pixel buffer (when the producing path holds one
            # readback buffer per capture — dxgi packed BGRA / mss BGRX DIB) rides with
            # the frame so the verification diff can memcmp identical screens instead
            # of paying a full-frame diff for a guaranteed (0.0, 0). Never set on any
            # other path (decoded/converted frames have no unique raw of record).
            frame_raw = getattr(frame, "_frame_raw", None)
            if frame_raw is not None:
                observation._frame_raw = frame_raw
            # R-5: capture-time freshness clock (the agent's validate-reuse window and
            # the live pins read this; harmless zero for consumers that never reuse).
            observation._captured_monotonic = time.perf_counter()
        return observation

    def identity_probe(self, reference: Observation | None = None) -> Observation | None:
        """Current screen identity without a pixel capture (R-5 W1 staleness guard).

        Reads the SAME OS identity channels ``observe()`` reads — monitor list,
        cursor position, target-monitor selection, foreground window — and runs the
        SAME coordinate-space classification. The reference's image payload and
        pixel-derived metadata (``ocr_text``/``ui_elements``) are carried over: the
        probe answers one question only ("has any identity dimension the validator
        checks drifted since the reference capture?"), and pixels are not an identity
        dimension. Any failure returns ``None`` (caller falls back to a full capture —
        the probe can never weaken the staleness check, only cheapen its no-drift
        case).
        """
        if self._system != "Windows" or reference is None:
            return None
        try:
            self._refresh_monitors()
            cursor_physical = get_cursor_position()
            monitor = _select_target_monitor(
                self._monitors, cursor_physical, reference.monitor.index if reference.monitor else None
            )
            left, top, width, height = monitor.bounds
            # The reference's screenshot dimensions are re-derived from the CURRENT
            # monitor geometry the same way observe() derives them: a display-mode
            # change shows up as a bounds change here.
            window_info = query_foreground_window()
            verdict = classify_coordinate_space(
                width, height, monitor, dpi_estimated=self.dpi_estimated
            )
            cursor_local: tuple[int, int] | None = None
            if cursor_physical is not None:
                cursor_local = CoordinateTransform(
                    origin_x=left, origin_y=top, scale_x=verdict.scale_x, scale_y=verdict.scale_y
                ).to_screenshot(*cursor_physical)
            self._active_context = _CaptureContext(monitor=monitor, verdict=verdict)
            return Observation(
                image_base64=reference.image_base64,
                width=width,
                height=height,
                active_window=reference.active_window,
                cursor_x=cursor_local[0] if cursor_local else None,
                cursor_y=cursor_local[1] if cursor_local else None,
                input_width=verdict.input_width,
                input_height=verdict.input_height,
                coordinate_scale_x=verdict.scale_x,
                coordinate_scale_y=verdict.scale_y,
                coordinate_space=verdict.space,
                monitor=monitor,
                active_window_info=window_info,
                ocr_text=reference.ocr_text,
                ui_elements=reference.ui_elements,
                redactions_applied=reference.redactions_applied,
            )
        except Exception:  # noqa: BLE001 - probe failure means "cannot probe"
            return None

    def _uia_semantic_fields(
        self, monitor: MonitorInfo, verdict: CoordinateVerdict
    ) -> tuple[list[TextRegion] | None, list[dict[str, object]] | None, int]:
        """Semantic snapshot -> the optional ``ocr_text``/``ui_elements`` fields.

        Silent degradation everywhere: an unavailable reader, a failed COM call, or an
        exceeded read budget leaves both fields ``None`` (or returns the partial data
        already gathered) and never raises into the observation loop. D15: the third
        element is the read's own known-but-dropped count (0 when nothing was dropped),
        riding ``Observation.ocr_dropped`` into the ``spatial_text`` block's additive
        ``omitted_count`` channel, so a partial read is fully accounted for.
        """
        reader = self._semantic_reader
        if reader is None or not reader.available:
            return (None, None, 0)
        try:
            snapshot = reader.read()
        except Exception:  # noqa: BLE001 - a broken reader must never break an observation
            return (None, None, 0)
        if snapshot is None:
            return (None, None, 0)
        origin = (monitor.bounds[0], monitor.bounds[1])
        scale = (verdict.scale_x, verdict.scale_y)
        try:
            ui_elements = _uia_snapshot_to_ui_elements(snapshot)
            ocr_text, dropped = _uia_snapshot_to_text_regions(snapshot, origin, scale)
        except Exception:  # noqa: BLE001 - conversion must never break an observation
            return (None, None, 0)
        return (ocr_text or None, ui_elements or None, dropped)

    def read_focused_control_value(self) -> str | None:
        """R-04 integrity read-back: the focused control's value, or ``None``.

        Reuses the existing semantic read path (UIA ``Value`` property, or the
        Win32 ``WM_GETTEXT`` fallback) -- the same read that feeds
        ``ui_elements`` -- scoped to the FOCUSED element only. ``None`` means the
        value CANNOT be verified right now (no reader, read failed, control exposes
        no value); callers must degrade honestly and must never treat ``None`` as a
        match or as evidence of a drop (no verified retype on an unreadable target).
        The read is also tolerant of a partially-initialized backend (no reader
        attribute at all, e.g. ``__new__``-built test doubles): that degrades to
        ``None`` (unverified) instead of breaking typing.

        Empty-versus-unreadable: both readers collapse an empty value to ``None``, but
        an Edit/Document control ALWAYS exposes its value when readable, so a ``None``
        value on such a control means the control is EMPTY (returned as ``''``) -- not
        unreadable. Any other control type without a value stays ``None`` (unverified;
        never verified against, never repaired).

        v0.7.1 (Defect C) additive diagnostic stash: ``self._last_read_had_target``
        records whether the read could even IDENTIFY a focused target (reader present
        and available, read succeeded, snapshot carried a focused element). ``False``
        — the OpenGL/console shape, field case Blender — lets the type dispatcher
        append the typed ``TYPE_UNCONFIRMED no-readable-target`` marker to an
        ``unverified`` outcome; a valueless-but-present control (``True``) keeps the
        legacy message byte-identical. Never consulted as typing evidence.
        """
        reader = getattr(self, "_semantic_reader", None)
        # WP13 DELTA-3 m4 (VAL-WP13-01): a warm that TIMED OUT and whose queued
        # warm then FAILED (CoCreateInstance error) leaves this session with a
        # permanently unavailable UIA reader — swap lazily to the Win32
        # fallback on the next read. (Duck-typed on the reader's warm_state so
        # test-double readers behave identically.)
        if (
            getattr(reader, "warm_state", None) == "failed"
            and not getattr(reader, "available", False)
        ):
            self._semantic_reader = Win32TextReader()
            reader = self._semantic_reader
        # WP12e (VAL MINOR): remember WHICH reader produced the value — the raw-cap
        # truncation rule (backend._read_landed_normalized) applies ONLY to the
        # truncating Win32 WM_GETTEXT reader; UIA values are not truncated.
        self._last_value_source = "none"
        self._last_read_degraded = None
        if reader is None:
            self._last_read_had_target = False
            return None
        if not getattr(reader, "available", False):
            # WP13 DELTA-3 m4: a read that arrives while a UIA warm is still
            # pending (never resolved, or timed out) is a READ TIMEOUT (the
            # provider may still answer) — NOT a blind read. Readers that do
            # not model a warm at all (test doubles, the Win32 fallback) have
            # no pending state and stay plain blind.
            if getattr(reader, "warm_state", "done") in (None, "timeout"):
                self._last_read_degraded = "read_timeout"
                # N2 (VAL-WP13-02): the target may be perfectly readable — the
                # provider is only slow — so the TYPE_UNCONFIRMED
                # no-readable-target marker and the clipboard hint must NOT
                # fire on this path either.
                self._last_read_had_target = True
            else:
                self._last_read_degraded = None
                self._last_read_had_target = False
            return None
        try:
            snapshot = reader.read()
        except Exception:  # noqa: BLE001 - a broken reader must never break typing
            self._last_read_had_target = False
            return None
        self._last_value_source = getattr(reader, "value_source", "win32")
        self._last_read_degraded = getattr(reader, "last_degraded_reason", None) or None
        focused = snapshot.get("focused") if isinstance(snapshot, dict) else None
        if not isinstance(focused, dict):
            # WP13 DELTA-3 m6 (VAL-WP13-01): a READ TIMEOUT is not a missing
            # target — the focused control EXISTS (the reader is available);
            # the provider was slow, so the TYPE_UNCONFIRMED no-readable-target
            # marker and the clipboard hint must NOT fire on it.
            if snapshot is None and self._last_read_degraded == "read_timeout":
                self._last_read_had_target = True
            else:
                self._last_read_had_target = False
            return None
        self._last_read_had_target = True
        value = focused.get("value")
        if isinstance(value, str):
            return value
        if focused.get("control_type") in _ALWAYS_VALUED_CONTROL_TYPES:
            return ""
        return None

    def _dxgi_grab(self, width: int, height: int) -> Image.Image | None:
        """R-6: Desktop Duplication grab of the primary output; None = use mss.

        The lazily-built duplicator is a singleton; ANY failure (construction,
        device lost, session disconnect, unexpected bounds) permanently disables
        the dxgi path for this backend instance and the caller takes the mss
        BitBlt grab — a broken fast path degrades to the R-5 pipeline exactly,
        never to a failed capture. The returned frame is validated against the
        EXPECTED monitor bounds: a bounds mismatch (mode change since init,
        duplicated output spanning monitors) also degrades to mss.
        """
        if self._capture_backend != "dxgi":
            return None
        dup = self._dxgi
        if dup is None:
            try:
                dup = _DxgiDuplicator()
            except Exception:
                self._capture_backend = "blt"  # permanent fallback for this session
                self._dxgi = None
                return None
            self._dxgi = dup
        try:
            frame = dup.grab(timeout_ms=_DXGI_ACQUIRE_TIMEOUT_MS)
        except Exception:
            # The duplication is unusable (device lost / access revoked / lock);
            # drop it and degrade permanently to the legacy path.
            try:
                dup.close()
            except Exception:
                pass
            self._dxgi = None
            self._capture_backend = "blt"
            return None
        if frame is None or frame.size != (width, height):
            return None
        return frame

    def _grab_png(
        self, left: int, top: int, width: int, height: int
    ) -> tuple[str, int, int, Image.Image]:
        """Capture a monitor region; return ``(base64_png, width, height, rgb_frame)``.

        PNG encoding defaults to ``optimize=False`` (PERF-004: −201 ms/frame measured,
        and a slightly SMALLER payload on real UI content); ``CORTEX_PNG_OPTIMIZE=1``
        (or the ``png_optimize`` constructor argument) restores the old behavior.
        ``CORTEX_PNG_COMPRESS_LEVEL`` (0..9) overrides PIL's compress level; the default
        leaves it at PIL's own (byte-identical to the pre-R-5 pipeline).

        R-5 payload cache (W2/W6): the previous capture's raw BGRA bytes and encoded
        payload are retained; when the new capture's raw bytes are byte-identical, the
        previous payload is reused verbatim (PNG is deterministic — identical pixels
        encode to identical bytes, so every digest/staleness/diff consumer sees exactly
        what a fresh encode would have produced) and the PNG encode is skipped entirely.
        The stashed RGB frame is returned for the verification fast path (W2).

        R-6 encode-skip (text sessions only, ``backend._raw_payload_keys=True`` set by
        the server): the payload is the deterministic raw-pixel key
        (``sha256(raw BGRA)`` + dimensions) instead of a PNG — no image block will ever
        be emitted from it, and digest/staleness both compare bytes so the key serves
        them exactly (distinct pixels -> distinct key; identical pixels -> identical
        key, the property the R-5 cache relies on). The RGB frame stays stashed for
        verification. ``CORTEX_TEXT_PNG=1`` (or reuse off) disables the raw key: PNG
        payloads return.
        """
        try:
            # R-6: try Desktop Duplication first when enabled; on ANY failure or
            # bounds mismatch the mss BitBlt grab below runs EXACTLY as before
            # (fail-open). The DXGI path produces the same RGB frame the mss
            # path would at the same instant (verified: mean-diff 0.003 between
            # back-to-back DXGI and GDI grabs; residual = in-flight animation).
            image = self._dxgi_grab(width, height)
            raw_bgra = None
            if image is not None:
                raw_size = image.size
                if self._raw_key_enabled and self._raw_payload_keys:
                    # R-6 (dxgi): the packed BGRA readback is the pixel source; hash
                    # it for both the key and the one-entry dxgi payload cache.
                    dxga_key = _raw_payload_key(image.tobytes(), image.width, image.height)
                    cached = self._dxgi_payload_cache
                    if cached is not None and cached[0] == raw_size and cached[1] == dxga_key:
                        encoded = cached[2]
                    else:
                        encoded = dxga_key
                        self._dxgi_payload_cache = (raw_size, dxga_key, encoded)
                    return encoded, image.width, image.height, image
            if image is None:
                raw = self._mss_grab({"left": left, "top": top, "width": width, "height": height})
                raw_bgra = raw.raw
                # R-5 (W2 decode-free frame): decode the BGRA DIB directly through PIL's
                # BGRX raw decoder — pixel-identical to the historical ``raw.rgb`` reorder +
                # ``frombytes`` copy (proven byte-identical PNG output) without building an
                # intermediate 6 MB RGB bytes object per capture.
                image = Image.frombuffer("RGB", raw.size, raw_bgra, "raw", "BGRX", 0, 1)
                # R-7: mss's DIB bytes are the pixel source of record for this capture —
                # same identical-screen memcmp contract as the dxgi packed readback.
                image._frame_raw = raw_bgra  # noqa: SLF001 - private R-7 stash
                raw_size = raw.size
        except Exception as exc:
            raise DisplayUnavailableError(f"Screen capture failed: {exc}") from exc

        if self._raw_key_enabled and self._raw_payload_keys:
            # R-6 encode-skip: deterministic raw-pixel key (never a PNG; never set for
            # image-mode sessions). Reuse-of-key semantics equal the PNG cache: the
            # key is a pure function of the pixels.
            encoded = _raw_payload_key(raw_bgra, raw_size[0], raw_size[1])
            return encoded, image.width, image.height, image

        encoded: str | None = None
        if self._internal_frame_reuse and self._payload_cache is not None:
            cached_size, cached_bytes, cached_payload = self._payload_cache
            if (
                raw_bgra is not None
                and cached_size == raw_size
                and cached_bytes == raw_bgra
            ):
                encoded = cached_payload

        if encoded is None:
            output = io.BytesIO()
            if self.png_compress_level is None:
                image.save(output, format="PNG", optimize=self.png_optimize)
            else:
                image.save(
                    output, format="PNG", optimize=self.png_optimize,
                    compress_level=self.png_compress_level,
                )
            encoded = base64.b64encode(output.getvalue()).decode("ascii")
            if self._internal_frame_reuse and raw_bgra is not None:
                self._payload_cache = (raw_size, raw_bgra, encoded)

        return encoded, image.width, image.height, image

    def execute(
        self,
        action: GroundedAction,
        stop: StopToken | None = None,
        focus_hook: Callable[[], None] | None = None,
        allow_launch: bool = False,
    ) -> str:
        """Execute one action; the stop token is checked before every physical input.

        The physical input itself is dispatched by the selected :class:`InputEngine`
        (SendInput by default, pyautogui fallback). Every engine dispatch is preceded by
        a stop-token check (per typing chunk / drag segment, exactly like per pyautogui
        call before), and engines raise :class:`InputBlockedError` on failsafe/blocked
        input, preserving the pre-SendInput error semantics. ``focus_hook`` (T8) runs
        before every ``type`` dispatch chunk after the stop check (focus continuity).

        WP2 (ORVEX-2026-101 Batch 3, P3.2/D15): an action the plan queue marked with
        ``plan_point_settle=True`` (its plan point's LAST item) settles ONCE here —
        after the dispatch, before the pipeline's post-action observation, the same
        position the legacy move settle occupied. Unmarked actions keep the exact
        legacy behavior. The settle DURATION is the declared
        ``plan_point_settle_ms`` (E-01's wiring: the model-stated value, consumed by
        :func:`_plan_point_settle_seconds`); ``None`` falls back to the module policy.
        The producer is ``computer_use_mcp.plan.stamped_plan_actions`` (WP7a): it
        stamps ``plan_point_settle``/``plan_point_settle_ms`` from a waypoint's
        declared ``settle_ms`` during plan normalization; this consumer side is
        complete and proven by tests/test_plan_settle_wiring.py plus the WP7a
        stamping tests.
        """
        _validate_action_defense(action)
        message = self._execute_dispatched(action, stop, focus_hook, allow_launch)
        if action.plan_point_settle and _plan_point_settle_seconds(action) > 0:
            # D15: a plan point settles ONCE after its LAST action — never per motion
            # element. A raw sleep like the legacy settle (the queue re-checks the
            # stop token between items). SPEC 5.1: the module policy default is 0.0 ms
            # (the outside-plan move is instant), so this guard is false on the
            # shipped path unless a settle was declared or configured.
            time.sleep(_plan_point_settle_seconds(action))
        return message

    def _execute_dispatched(
        self,
        action: GroundedAction,
        stop: StopToken | None = None,
        focus_hook: Callable[[], None] | None = None,
        allow_launch: bool = False,
    ) -> str:
        """Dispatch body of :meth:`execute` (WP2 Batch 3 split: the plan-point settle
        tail lives in ``execute`` so it covers every branch, including early returns)."""
        if stop is not None:
            stop.ensure_live()
        engine = self._engine
        if action.action == "wait":
            total = min(max(action.delta, 0), WAIT_MAX_SECONDS)
            if stop is None:
                time.sleep(total)
            else:
                _sleep_for_wait_action(total, stop)
            return f"Executed {action.action}.{_wait_clamp_note(action.delta, total)}"
        if action.action in {"click", "double_click", "right_click"}:
            if action.point is None:
                raise ValueError("A point is required for click actions")
            physical = self._map_to_physical(action.point.x, action.point.y)
            if stop is not None:
                stop.ensure_live()
            # right_click (v0.6.0): one RIGHT-button press at the point (context menu);
            # same stop-check/single-transform discipline as the left-button clicks.
            clicks = 2 if action.action == "double_click" else 1
            button = "right" if action.action == "right_click" else "left"
            if action.hold_ms is not None:
                # WP2 (ORVEX-2026-101 Batch 1, P1.3): explicit press duration — move,
                # press, sliced interruptible wait, release. Engine-agnostic: consumes
                # the existing mouse_down/mouse_up contract primitives (P1.4: no engine
                # change, both engines comply). Schema (P1.1) + validator (P1.2) already
                # reject hold_ms on double_click before any motion; this fail-closed
                # guard is the last line so a model_construct bypass can never be
                # silently re-interpreted as a single held press. The computed duration
                # is resolved BEFORE the press so even malformed bypass payloads fail
                # with zero input dispatched.
                if action.action == "double_click":
                    raise ValueError("hold_ms is not valid on double_click actions")
                hold_seconds = action.hold_ms / 1000.0
                engine.move(physical[0], physical[1])
                _mark_action_dispatched(action)
                button_down = False
                hold_error: BaseException | None = None
                try:
                    if stop is not None:
                        stop.ensure_live()  # before mouse_down
                    engine.mouse_down(button)
                    button_down = True
                    _wait_for_hold(hold_seconds, stop)
                    if stop is not None:
                        stop.ensure_live()  # before mouse_up
                except BaseException as error:
                    hold_error = error
                    raise
                finally:
                    # The button is ALWAYS released — including on a mid-hold
                    # TaskStopped (the stop is the kill path and must never leave
                    # input held). A failing release never masks an in-flight error
                    # such as TaskStopped.
                    if button_down:
                        try:
                            engine.mouse_up(button)
                        except Exception:
                            if hold_error is None:
                                raise
            else:
                engine.click(physical[0], physical[1], clicks=clicks, button=button)
                _mark_action_dispatched(action)
        elif action.action == "drag":
            if action.point is None or action.to_point is None:
                raise ValueError("Both a start point and an end point are required for drag actions")
            start = self._map_to_physical(action.point.x, action.point.y)
            end = self._map_to_physical(action.to_point.x, action.to_point.y)
            if stop is not None:
                stop.ensure_live()  # before move(start)
            engine.move(start[0], start[1])
            _mark_action_dispatched(action)
            if stop is not None:
                stop.ensure_live()  # before mouseDown
            button_down = False
            drag_error: BaseException | None = None
            try:
                engine.mouse_down("left")
                button_down = True
                for segment_x, segment_y in self._drag_waypoints(start, end):
                    if stop is not None:
                        stop.ensure_live()  # before every stroke segment
                    engine.move(segment_x, segment_y)
                    if engine.drag_step_pause > 0:
                        time.sleep(engine.drag_step_pause)
                if stop is not None:
                    stop.ensure_live()  # before mouseUp
            except BaseException as error:
                drag_error = error
                raise
            finally:
                # The button is ALWAYS released — including on a mid-stroke TaskStopped
                # (P0-C: the stop is the kill path and must never leave input held). A
                # failing release never masks an in-flight error such as TaskStopped.
                if button_down:
                    try:
                        engine.mouse_up("left")
                    except Exception:
                        if drag_error is None:
                            raise
        elif action.action == "move":
            if action.point is None:
                raise ValueError("A point is required for move actions")
            physical = self._map_to_physical(action.point.x, action.point.y)
            # WP2 (ORVEX-2026-101 Batch 2, P2.3): the timed branch fires ONLY when a
            # duration exists (an explicit field value, or the outside-plans env
            # default when the field is absent). A bypass payload with a non-int or
            # negative duration fails closed here — the last line before dispatch.
            duration_ms = action.duration_ms
            if duration_ms is not None and (
                isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0
            ):
                raise ValueError(
                    f"duration_ms must be a non-negative integer; got {duration_ms!r}"
                )
            if duration_ms is None:
                duration_ms = MOVE_DURATION_MS
            if duration_ms > 0:
                # Timed connected motion — the proven drag-loop pattern (points +
                # per-point stop check + pacing), consuming the UNCHANGED engine.move
                # primitive (P2.5: no InputEngine contract change; engine-agnostic so
                # both engines comply). The path is computed in PHYSICAL pixels AFTER
                # the single _map_to_physical transform (F1) — start from the actual
                # cursor position, arrival exact regardless of shape/easing.
                start = get_cursor_position()
                if start is None:
                    raise RuntimeError(
                        "cursor position unavailable; cannot build a timed motion path"
                    )
                context = self._active_context
                if context is None:
                    context = self._build_default_context()
                    self._active_context = context
                path = _motion_path_points(
                    start,
                    physical,
                    duration_ms,
                    shape=MOVE_SHAPE,
                    bend_px=MOVE_BEND_PX,
                    easing=MOVE_EASING,
                    target_interval_ms=PLAN_SAMPLE_INTERVAL_MS,
                    bounds=context.monitor.bounds,
                )
                if not path:
                    # DELTA 1 (F6): a timed move to the CURRENT position builds an empty
                    # path — parity with the instant path: issue ONE engine.move to the
                    # endpoint and mark dispatched, instead of silently looping zero
                    # samples and never dispatching.
                    if stop is not None:
                        stop.ensure_live()
                    engine.move(physical[0], physical[1])
                    _mark_action_dispatched(action)
                else:
                    # DELTA 1 (F5): the pacing is DERIVED from the requested duration —
                    # duration_ms / SEGMENTS (segments = samples - 1) — so the declared
                    # duration is honoured across the whole legal band (a 5000 ms move
                    # now costs ~5000 ms of pacing, not the fixed 12 ms slice that
                    # capped it near ~1.5 s). Sample COUNT is unchanged (the 3..128
                    # cap keeps determinism) and the 120 ms example stays 11 samples x
                    # 12 ms, byte-identical (duration/segments = 120/10 = 12 ms).
                    pacing = duration_ms / max(len(path) - 1, 1) / 1000.0
                    for point_x, point_y in path:
                        if stop is not None:
                            stop.ensure_live()  # before every motion sample (drag-loop discipline)
                        engine.move(point_x, point_y)
                        _mark_action_dispatched(action)
                        # Decision 2 (Batch 5): after each physical sample, best-effort
                        # probe the existing physical cursor primitive. A foreign/user move
                        # beyond the same ±2px predicate tolerance is corrected
                        # immediately; no model round-trip and no timeout is involved. A
                        # backend without the probe (a test double) simply degrades to the
                        # normal path.
                        probe = None
                        try:
                            probe = self.probe_cursor_position()
                        except Exception:  # noqa: BLE001 - probe is best effort by contract
                            probe = None
                        if (
                            isinstance(probe, tuple)
                            and len(probe) == 2
                            and all(
                                isinstance(value, int) and not isinstance(value, bool)
                                for value in probe
                            )
                            and (
                                abs(probe[0] - point_x) > 2
                                or abs(probe[1] - point_y) > 2
                            )
                        ):
                            # Reassert the CURRENT deterministic sample immediately. The
                            # next iteration continues toward the same endpoint; this
                            # avoids a blind second full path while still restoring the
                            # expected position without waiting for the model.
                            if stop is not None:
                                stop.ensure_live()
                            engine.move(point_x, point_y)
                            _mark_action_dispatched(action)
                            action._repositions.append(
                                {
                                    "observed": [probe[0], probe[1]],
                                    "expected": [point_x, point_y],
                                    "attempt": len(action._repositions) + 1,
                                }
                            )
                            continue  # no pacing wait after an immediate correction
                        time.sleep(pacing)
            else:
                if stop is not None:
                    stop.ensure_live()  # before the single cursor reposition
                engine.move(physical[0], physical[1])
                _mark_action_dispatched(action)
            # WP2 (ORVEX-2026-101 Batch 3, P3.2/D15 + SPEC 5.1): the settle stays here
            # — after the dispatch, once per move — ONLY for unmarked actions (the
            # legacy policy: outside plans, and any action the queue did not mark). A
            # plan transition move (marker False) settles NOT here; its point settles
            # ONCE after the point's LAST action via the execute() tail. The DEFAULT
            # policy is 0.0 ms (SPEC 5.1: the outside-plan move is instant — a
            # behavior change vs v0.7.5's unconditional 50 ms); an operator who wants
            # a settle sets ``CORTEX_MOVE_SETTLE_MS`` to 1..200.
            if action.plan_point_settle is None and _MOVE_SETTLE_SECONDS > 0:
                time.sleep(_MOVE_SETTLE_SECONDS)  # settle ONCE (both paths); a move is not an input press
        elif action.action == "type":
            if action.text is None:
                raise ValueError("Text is required for type actions")
            return self._execute_type(action, stop, focus_hook)
        elif action.action == "keypress":
            if not action.keys:
                raise ValueError("At least one key is required")
            if stop is not None:
                stop.ensure_live()
            self._apply_focus_settle()  # B8 settle after a recent focus transition
            self._apply_key_dispatch_gap(action.keys)  # B3 settle/gap policy
            # WP2 (ORVEX-2026-107): hold_ms=None keeps the legacy single atomic chord
            # batch byte-identical (D5); hold_ms=0 is an explicit zero-duration SPLIT
            # press, not a chord fast-path (D2), so adjacent values never diverge in
            # call count or release order.
            if action.hold_ms is not None:
                self._dispatch_held_keys(action, list(action.keys), stop)
            else:
                engine.chord(list(action.keys))
                _mark_action_dispatched(action)
            self._last_key_dispatch = time.monotonic()
        elif action.action == "hotkey":
            # Sibling of keypress for COMPOUND chords (2..12 keys, passed verbatim in
            # pyautogui vocabulary); single-key presses deliberately stay on keypress.
            guard_keys = [key.strip() for key in action.keys]
            if not 2 <= len(guard_keys) <= 12 or any(not key for key in guard_keys):
                raise ValueError("Hotkey actions require 2 to 12 non-empty key names")
            if stop is not None:
                stop.ensure_live()  # immediately before the single chord input
            self._apply_focus_settle()  # B8 settle after a recent focus transition
            self._apply_key_dispatch_gap(guard_keys)  # B3 settle/gap policy
            # WP2 (ORVEX-2026-107): a held hotkey holds EVERY key SIMULTANEOUSLY for
            # the duration and releases them in reverse order — "chord, then wait"
            # would be vacuous, because a chord has already released everything by the
            # time it returns (D1). K1 (LIVE-WP13-02 runs 3-4): a PLAIN chord is ONE
            # SendInput batch with zero time between the key-down and key-up events,
            # which Windows 11 Store apps IGNORED live (packaged Notepad 11.2607:
            # only 1 of 5 plain alt+f4 chords closed the window, pixel diff 0; the
            # held hold_ms=50 form closed 3 of 3 trials) — a plain hotkey therefore uses the
            # held path with a 50 ms default hold (DEFAULT_HOTKEY_HOLD_MS). An
            # EXPLICIT hold_ms still wins; hold_ms=0 keeps its documented meaning,
            # the explicit zero-duration split press/release with zero wait —
            # NOT the old single-batch chord, which is no longer reachable for
            # a hotkey.
            if action.hold_ms is not None:
                self._dispatch_held_keys(action, list(action.keys), stop)
            else:
                self._dispatch_held_keys(
                    action,
                    list(action.keys),
                    stop,
                    hold_ms_override=DEFAULT_HOTKEY_HOLD_MS,
                )
            self._last_key_dispatch = time.monotonic()
        elif action.action == "scroll":
            if stop is not None:
                stop.ensure_live()
            engine.scroll(action.delta)
            _mark_action_dispatched(action)
        elif action.action == "focus_window":
            return self._execute_focus_window(action)
        elif action.action == "ensure_app":
            # T8 mechanism ii: attach-or-launch probe. Target format
            # ``process[|doc-token]``; launches only when the caller grants
            # ``allow_launch=True`` — the pipeline grants it under the
            # launch="server" policy (the default), restricted to allowlisted
            # targets only when an allowlist is configured. The outcome set without
            # a launch is REATTACHED / AMBIGUOUS_INSTANCE / NO_INSTANCE.
            target = (action.target or "").strip()
            if not target:
                raise ValueError("A target is required for ensure_app actions")
            return self.ensure_app(target, allow_launch=allow_launch)
        elif action.action == "done":
            return "No computer action requested."
        else:
            raise UnsupportedActionError(f"Unsupported action: {action.action}")
        return f"Executed {action.action}."

    def probe_cursor_position(self) -> tuple[int, int] | None:
        """WP2 (Batch 5 decision 2): best-effort PHYSICAL cursor position probe.

        Consumed by the timed-move interference check (the immediate re-assert).
        Returns the same physical virtual-screen coordinates ``get_cursor_position``
        reports, or ``None`` when the probe is unavailable (a backend/test double
        without the capability degrades to the normal path — never raises).
        """
        return get_cursor_position()

    def _dispatch_held_keys(
        self,
        action: GroundedAction,
        keys: list[str],
        stop: StopToken | None,
        hold_ms_override: float | None = None,
    ) -> None:
        """WP2 (ORVEX-2026-107): key-DOWN -> sliced interruptible wait -> key-UP
        (reverse order).

        The held-click discipline (Batch 1 P1.3) applied to keys, verbatim: the
        duration is resolved BEFORE the first press so even a malformed bypass payload
        dispatches nothing; ``stop.ensure_live()`` runs before every key-down AND
        before the release; ``_mark_action_dispatched`` fires the moment the down-half
        returns (input has physically left the process, so a mid-hold stop is honestly
        "dispatched but unverified", never "cancelled before dispatch"); the wait is
        the EXISTING ``_sleep_for_hold``/``HOLD_SLICE_SECONDS`` machinery (D4), never a
        key-specific fork.

        THE RELEASE IS UNCONDITIONAL. A key left physically down rewrites every
        subsequent user keystroke on the operator's machine system-wide — this
        ``finally`` is the whole reason the feature is safe, so it runs on EVERY exit
        path: a mid-hold ``TaskStopped``, an engine or wait exception, or a stop
        arriving between the last key-down and the wait. Every pressed key is released
        in reverse press order (D1), each release is ATTEMPTED even if an earlier one
        raises, and a failing release never masks the in-flight error.
        """
        hold_ms = hold_ms_override if hold_ms_override is not None else action.hold_ms
        hold_seconds = hold_ms / 1000.0
        engine = self._engine
        keys_down = 0
        hold_error: BaseException | None = None
        try:
            for key in keys:
                if stop is not None:
                    stop.ensure_live()  # before every key_down: no orphan half-pressed chord
                engine.key_down(key)
                keys_down += 1
            _mark_action_dispatched(action)
            _wait_for_hold(hold_seconds, stop)
            if stop is not None:
                stop.ensure_live()  # before the first key_up
        except BaseException as error:
            hold_error = error
            raise
        finally:
            release_error: BaseException | None = None
            for index in range(keys_down - 1, -1, -1):
                try:
                    engine.key_up(keys[index])
                except BaseException as error:  # noqa: BLE001 - a release must never abort the sweep
                    # ``BaseException``, not ``Exception``: one key-up raising must not
                    # abandon the remaining keys still physically down. The first
                    # failure is recorded and re-raised only if nothing was in flight.
                    if release_error is None:
                        release_error = error
            if release_error is not None and hold_error is None:
                raise release_error

    def _effective_type_deadline(self) -> float:
        """WP13 DELTA 1 #3: the deadline ONE type dispatch runs under.

        Inside a follow-up queue item (agent._run_action_queue) the queue caps
        this to the item's REMAINING share of the ONE-CALL budget via
        ``self._queue_deadline_seconds`` (None set = no queue item in flight, the
        derived deadline applies). The engine is per-session and queue items run
        sequentially, so the temporary override cannot interleave.
        """
        override = getattr(self, "_queue_deadline_seconds", None)
        return override if override is not None else type_action_deadline_seconds()

    def _cap_type_interval(self, text: str) -> float:
        """WP12b/c (VAL M1/M2 + C4): cap the paced budget by the sleep count.

        Since WP12c the paced engine sleeps once per CHARACTER (a surrogate pair
        travels in one SendInput call with one sleep — input_engine.py
        ``_char_event_groups``), so the sleep count IS ``len(text)``. The effective
        interval is ``min(configured, budget / sleeps)`` where the budget is the
        paced 80% share of :meth:`_effective_type_deadline` (queue-capped inside a
        follow-up item) and RETURNED so the repair can run under the same capped
        pace. The configured interval is remembered and restored by
        :meth:`_restore_type_interval` (the engine is per-session and actions run
        sequentially, so the temporary override cannot interleave). Unpaced
        engines are a no-op (returns 0.0).
        """
        self._original_type_interval = getattr(self._engine, "type_interval", 0.0)
        sleeps = len(text)
        self._capped_type_interval = 0.0
        if self._original_type_interval > 0 and sleeps > 0:
            pacing_budget = min(pacing_budget_seconds(), 0.8 * self._effective_type_deadline())
            self._capped_type_interval = min(self._original_type_interval, pacing_budget / sleeps)
            self._engine.type_interval = self._capped_type_interval
        return self._capped_type_interval

    def _restore_type_interval(self) -> None:
        """Restore the engine's configured typing interval after a capped call."""
        interval = getattr(self, "_original_type_interval", None)
        if interval is not None:
            self._engine.type_interval = interval
            self._original_type_interval = None

    def _execute_type(
        self,
        action: GroundedAction,
        stop: StopToken | None,
        focus_hook: Callable[[], None] | None,
    ) -> str:
        """Dispatch one ``type`` action (R-04): chunked typing + END-OF-ACTION
        integrity verification — FAST PATH FIRST, escalation only (fix3).

        Legacy path (integrity disabled or empty text) is byte-identical to pre-R-04:
        one engine call for the whole string. Otherwise the text is split into
        ``TYPE_CHUNK_CHARS`` chunks and dispatched chunk-by-chunk (stop token and the
        T8 focus hook ride every engine dispatch, no added gaps), and verification
        runs ONCE after the FINAL chunk settles — read-backs are NEVER interleaved
        mid-dispatch. WP12b/c: the paced budget is capped per action
        (``PACING_BUDGET_SECONDS`` inside ``TYPE_ACTION_DEADLINE_SECONDS`` = 45 s);
        the dispatch loop checks the deadline per chunk and finishes the rest
        UNPACED if the paced rest would overrun.

        A PRE-DISPATCH BASELINE read of the focused control is taken once (WP12b B1)
        — and WP12d N1 makes EVERY read blind when its RAW value sits at the
        reader's 8192-char WM_GETTEXT truncation cap — every repair and every
        ``mismatch`` verdict is GATED ON IT.

        FAST PATH (the happy case pays only quick reads and no horizon): read1 after
        ``TYPE_VERIFY_SETTLE_SECONDS``, then up to ``TYPE_VERIFY_FAST_MAX_READS`` total
        quick reads spaced ``TYPE_VERIFY_CONFIRM_GAP_SECONDS`` apart (default 0.05 s).
        Each later read may EQUAL the previous (stable buffer) or EXTEND it (the
        previous read was MID-DRAIN: the app had not yet appended the last keystrokes —
        the profiled live signature is 58 of 81 chars at +50 ms); a monotone extension
        of a real read is real evidence, not a disagreement. The moment a read ends
        with the full expected text AND — WP12c C2 — the KNOWN baseline proves the
        insertion form (buffer == baseline + typed text at one position) →
        ``verified``; a tail match the baseline cannot prove → ``unverified``
        branch=tail_unproven (the text may have been there already and the dispatch
        dropped). A first STABLE read that already proves the caret-insertion form
        against the baseline is accepted without the horizon (WP12c M3). Blind reads
        (``None``) or an agreed EMPTY buffer → ``unverified`` (never repaired).

        SLOW PATH (escalation only — the rare-and-suspicious case pays): a pair that
        SHRANK or DIVERGED, a stalled stable buffer, or growth that outlives the fast
        budget is confirmed across ``TYPE_VERIFY_STABILITY_SECONDS`` (the landing-lag
        horizon) with one more agreeing read before anything else, then resolved
        AGAINST THE BASELINE (WP12b B1): buffer == baseline + a proper prefix of the
        typed text (suffix genuinely missing) → retype EXACTLY the missing suffix
        ONCE under the SAME capped interval and only if it fits the remaining
        deadline (else unverified branch=repair_over_budget), then a quick re-verify
        pair; success → ``verified`` with ``healed=<n>``; a buffer that began
        correctly and then DIVERGED (positive corruption evidence — 'tett'/'test',
        'brown xxx'/'brown fox') → ``mismatch``, never retyped; a confirmed
        still-wrong buffer after a repair raises :class:`TextIntegrityError` (no
        second repair); an unconfirmable repair reports ``unverified`` honestly. A
        transformed/foreign buffer (shares nothing with the typed text) is honest
        ``unverified`` branch=transformed_buffer — a masked field must NOT report a
        failure. ``unverified`` NEVER triggers a retype (the anti-duplication gate),
        and every ``unverified`` names its ladder branch as a ``branch=`` suffix
        inside the message.

        The stop token is checked between chunks, before every read-back, and before
        the retype. The action message gains an additive ``integrity=...`` suffix —
        the prefix stays ``Executed type.``.
        """
        text = action.text
        if stop is not None:
            stop.ensure_live()
        self._apply_focus_settle()  # B8 settle after a recent focus transition
        before_chunk: Callable[[], None] | None = stop.ensure_live if stop is not None else None
        if focus_hook is not None:
            # T8 mechanism iv: the focus-continuity probe piggybacks the per-chunk
            # cadence AFTER the stop-token hook (stop discipline keeps precedence).
            def _chained() -> None:
                if stop is not None:
                    stop.ensure_live()
                focus_hook()

            before_chunk = _chained
        if (action.via or "sendinput") == "clipboard":
            # v0.7.1 (Defect C): the paste transport rides the field-proven chord path.
            # Zero cost for every other value: this branch is the ONLY new code the
            # default sendinput path touches (a single string compare).
            return self._execute_type_via_clipboard(action, stop, focus_hook)
        if not TYPE_INTEGRITY_ENABLED or not text:
            # Legacy single-call path. K-05: the shipped engines attach their exact
            # intra-call dispatched count to the FocusDriftError; an engine that does
            # not report leaves the class default (None -> 0 -> no partial_delivery
            # key), which is the conservative "no completed dispatch known" answer.
            self._cap_type_interval(text)
            try:
                self._engine.type_text(text, before_chunk=before_chunk)
            except FocusDriftError as exc:
                intra = exc.delivered_chars
                exc.delivered_chars = intra if isinstance(intra, int) and intra > 0 else 0
                raise
            finally:
                self._restore_type_interval()
            self._last_key_dispatch = time.monotonic()
            return "Executed type."

        total = len(text)
        delivered = 0
        # WP12b (VAL M1/M2) + WP13 A: ONE wall-clock deadline per type action,
        # DERIVED from the host's client timeout (60 -> 45 as shipped; 30 -> 15),
        # capped by the queue item's remaining share inside a follow-up chain
        # (WP13 DELTA 1 #3).
        deadline = time.monotonic() + self._effective_type_deadline()
        # WP12b (VAL B1a): one PRE-DISPATCH BASELINE read of the focused control
        # (the same bounded read path the verification reads use; it dispatches
        # nothing). None = unreadable: without a baseline the ladder can never
        # repair, and mismatch requires positive evidence against the baseline.
        # WP12d (VAL N1): _read_landed_normalized itself returns None for a RAW
        # read at the reader's 8192-char truncation cap — on EVERY read, so a
        # repair can never fire on a cap-truncated buffer.
        if stop is not None:
            stop.ensure_live()
        baseline_n = self._read_landed_normalized()
        # WP13 DELTA-3 m5 (VAL-WP13-01): a baseline read that TIMED OUT proves
        # nothing for the WHOLE action — a later healthy read must not be
        # accepted `verified` through the no-baseline tail-match rule.
        baseline_degraded = getattr(self, "_last_read_degraded", None) == "read_timeout"
        # WP12c (VAL C1): the cap is applied AFTER the stop-check and the baseline
        # read — anything that can raise between applying the cap and the dispatch
        # try: would otherwise leave the CAPPED interval on the engine for the rest
        # of the session.
        effective_interval = self._cap_type_interval(text)
        # WP12d (VAL R1 MINOR): " paced=partial" rides in the reply message when
        # the deadline forced the remaining text to finish unpaced (documented in
        # docs/CONFIGURATION.md and VERSIONS.md).
        pace_token = ""
        try:
            for chunk in _split_type_chunks(text, TYPE_CHUNK_CHARS):
                if stop is not None:
                    stop.ensure_live()  # between chunks (existing discipline)
                # WP12c (VAL C3): the deadline is checked per chunk. If the PACED
                # rest would overrun it, the remaining text finishes UNPACED (a
                # fast burst is a smaller hazard than an unbounded over-deadline
                # hang; documented in docs/CONFIGURATION.md).
                remaining = deadline - time.monotonic()
                if effective_interval > 0:
                    chars_left = total - delivered
                    if remaining <= 0 or chars_left * effective_interval > remaining:
                        self._engine.type_interval = 0.0
                        pace_token = " paced=partial"
                try:
                    self._engine.type_text(chunk, before_chunk=before_chunk)
                except FocusDriftError as exc:
                    # §1.1 K-05 (ORVEX-2026-108): the abort happened INSIDE this engine
                    # call — every EARLIER chunk fully landed (`delivered`), and the
                    # engine reports the exact prefix it dispatched within its own call
                    # (per-character granularity on both shipped engines). The aborting
                    # unit contributed nothing. Compose, never undercount completed work.
                    intra = exc.delivered_chars
                    exc.delivered_chars = delivered + (
                        intra if isinstance(intra, int) and intra > 0 else 0
                    )
                    raise
                delivered += len(chunk)
                self._last_key_dispatch = time.monotonic()
        finally:
            self._restore_type_interval()

        # --- end-of-action verification: FAST PATH FIRST (bounded drain poll) -----
        if stop is not None:
            stop.ensure_live()
        if TYPE_VERIFY_SETTLE_SECONDS > 0:
            time.sleep(TYPE_VERIFY_SETTLE_SECONDS)
        if stop is not None:
            stop.ensure_live()
        expected_n = _normalize_landed_text(text)
        # WP13 DELTA-3 m7 (VAL-WP13-01): the WHOLE type action (dispatch + every
        # verification read + horizon sleep) must fit the deadline. Before each
        # verification read (and its leading sleep) the ladder checks that the
        # remaining budget can still cover ONE read wall
        # (semantic_read_timeout_seconds) — if not, it stops verifying with the
        # typed branch ``verify_over_budget``: never verified, never a repair.
        verify_read_wall = semantic_read_timeout_seconds()
        self._verify_over_budget = False

        def _budget_allows_read(leading_sleep: float = 0.0) -> bool:
            return time.monotonic() + leading_sleep + verify_read_wall < deadline

        def _quick_read() -> str | None:
            if not _budget_allows_read(TYPE_VERIFY_CONFIRM_GAP_SECONDS):
                self._verify_over_budget = True
                return None
            if TYPE_VERIFY_CONFIRM_GAP_SECONDS > 0:
                time.sleep(TYPE_VERIFY_CONFIRM_GAP_SECONDS)
            if stop is not None:
                stop.ensure_live()
            return self._read_landed_normalized()

        def _horizon_read() -> str | None:
            if not _budget_allows_read(TYPE_VERIFY_STABILITY_SECONDS):
                self._verify_over_budget = True
                return None
            if TYPE_VERIFY_STABILITY_SECONDS > 0:
                time.sleep(TYPE_VERIFY_STABILITY_SECONDS)
            if stop is not None:
                stop.ensure_live()
            return self._read_landed_normalized()

        def _unverified(branch: str) -> str:
            """WP12a: the typed `branch=` token names WHICH ladder path gave up (it
            rides INSIDE the message like integrity=/waited_seconds= — no new reply
            key; docs/REPLY-CONTRACT.md sanctions key=value suffixes in messages)."""
            base = (
                "Executed type. "
                f"{_format_integrity_status(0, total, mismatch=0, unverified=total, healed=0)}"
                f" branch={branch}{pace_token}"
            )
            # v0.7.1 (Defect C) honest diagnostic: when the read-backs were blind because
            # there was NO readable focused target at all (OpenGL/console apps), say so in
            # typed vocabulary and teach the escape hatch. A valueless-but-present control
            # keeps the legacy message byte-identical. The getattr default preserves the
            # exact legacy output for doubles that patch the read method itself.
            if not getattr(self, "_last_read_had_target", True):
                return f"{base} {TYPE_UNCONFIRMED_MARKER} ({TYPE_UNCONFIRMED_HINT_SENDINPUT})"
            return base

        def _baseline_landing_proven(buffer: str) -> bool:
            """WP12c (VAL C2): with a KNOWN baseline, 'verified' is accepted ONLY
            when the buffer is the baseline with the typed text inserted at ONE
            position (length check first, then the insertion form via common
            prefix/suffix — linear). With no baseline, today's tail match governs
            (returns True — nothing to prove against)."""
            if baseline_n is None:
                # WP13 DELTA-3 m5: a baseline read that TIMED OUT proves
                # nothing — never accept `verified` through the no-baseline
                # tail-match rule (the caller reports branch=read_timeout).
                return not baseline_degraded
            if len(buffer) != len(baseline_n) + len(expected_n):
                return False
            p = 0
            while p < len(baseline_n) and buffer[p] == baseline_n[p]:
                p += 1
            s = 0
            while (
                s < len(baseline_n)
                and buffer[len(buffer) - 1 - s] == baseline_n[len(baseline_n) - 1 - s]
            ):
                s += 1
            # WP12d: a single insertion is provable from EITHER side — the greedy
            # prefix position overshoots when the insertion point sits inside the
            # prefix match, and vice versa for the suffix. Try both candidates.
            if buffer[p : p + len(expected_n)] == expected_n and buffer[p + len(expected_n):] == baseline_n[p:]:
                return True
            j = len(baseline_n) - s
            if buffer[j : j + len(expected_n)] == expected_n and buffer[:j] == baseline_n[:j]:
                return True
            return False

        def _repair_and_recheck(matched: int) -> str:
            """The ONE verified retype of the exact missing suffix + quick re-verify.

            WP12b (VAL M1/M2): the repair is typed under the SAME capped interval
            and ONLY if it fits the remaining wall-clock deadline — an over-budget
            repair is never typed (unverified, branch=repair_over_budget)."""
            missing = text[_typed_index_for_norm_len(text, matched) :]
            remaining = deadline - time.monotonic()
            needed = len(missing) * max(effective_interval, 0.0) + (
                TYPE_VERIFY_SETTLE_SECONDS + 2.0
            )
            if needed > remaining:
                return _unverified("repair_over_budget")
            if stop is not None:
                stop.ensure_live()  # before the verified retype
            # WP12c (VAL recheck MAJOR): the dispatch loop's restore already ran, so
            # the engine holds the CONFIGURED interval here — save it before
            # overriding with the capped pace and restore it in this finally, or
            # the capped value leaks into every later type of the session.
            repair_original = getattr(self._engine, "type_interval", 0.0)
            self._engine.type_interval = effective_interval  # the SAME capped pace
            try:
                self._engine.type_text(missing, before_chunk=before_chunk)
            except FocusDriftError as exc:
                # §1.1 K-05 (ORVEX-2026-108), N5 semantics (VAL-WP1-02): at this point
                # ``len(text) - len(missing)`` characters were CONFIRMED landed by
                # read-back, and the retype's already-dispatched prefix may still land
                # late (drain lag). Per the truthfulness rule — never report fewer
                # characters than may have landed — the count is the UPPER BOUND
                # ``len(text)`` (everything dispatched), not the read-back-confirmed
                # prefix.
                exc.delivered_chars = len(text)
                raise
            finally:
                self._engine.type_interval = repair_original
            self._last_key_dispatch = time.monotonic()
            if TYPE_VERIFY_SETTLE_SECONDS > 0:
                time.sleep(TYPE_VERIFY_SETTLE_SECONDS)
            if stop is not None:
                stop.ensure_live()
            recheck1 = self._read_landed_normalized()
            if recheck1 is None:
                if self._verify_over_budget:
                    return _unverified("verify_over_budget")
                return _unverified("repair_read_blind")
            recheck2 = _quick_read()
            if recheck2 is None or recheck2 != recheck1:
                # Repair landing UNCONFIRMED: honest "unknown", never a second retype.
                if self._verify_over_budget:
                    return _unverified("verify_over_budget")
                return _unverified("repair_unconfirmed")
            if recheck2.endswith(expected_n):
                # WP12c (VAL C2): after a repair the expected buffer is exactly
                # baseline + expected — with a KNOWN baseline the insertion form
                # must hold; a buffer that merely ENDS with the typed text is
                # still-wrong -> TextIntegrityError (never a false heal).
                if baseline_n is None or _baseline_landing_proven(recheck2):
                    healed = len(missing)
                    return (
                        "Executed type. "
                        f"{_format_integrity_status(total, total, mismatch=0, unverified=0, healed=healed)}"
                        f"{pace_token}"
                    )
                raise TextIntegrityError(
                    "typed-text integrity verification failed after one verified retype "
                    f"({len(missing)} retried characters still not landed correctly in the "
                    "focused control); refusing further blind repair. Re-observe and decide."
                )
            raise TextIntegrityError(
                "typed-text integrity verification failed after one verified retype "
                f"({len(missing)} retried characters still not landed correctly in the "
                "focused control); refusing further blind repair. Re-observe and decide."
            )

        def _resolve_trusted(trusted_n: str) -> str:
            """WP12b (VAL B1): BASELINE-GATED verdicts on the horizon-confirmed
            trusted buffer. A suffix-append repair is sound ONLY when the trusted
            buffer is exactly the pre-dispatch baseline plus a proper prefix of the
            typed text (the suffix genuinely missing); corruption is flagged
            ``mismatch`` ONLY on positive evidence against the baseline; everything
            else is an honest ``unverified`` naming its branch."""

            def _verified_full() -> str:
                return (
                    "Executed type. "
                    f"{_format_integrity_status(total, total, mismatch=0, unverified=0, healed=0)}"
                    f"{pace_token}"
                )

            def _mismatch() -> str:
                return (
                    "Executed type. "
                    f"{_format_integrity_status(0, total, mismatch=total, unverified=0, healed=0)}"
                    f"{pace_token}"
                )

            if trusted_n.endswith(expected_n):
                # (e) The drain completed; the typed text is at the tail.
                # WP12c (VAL C2): with a KNOWN baseline, accept ONLY the insertion
                # form (baseline + typed text at one position) — a buffer that
                # merely ends with the typed text (it was already there and the
                # dispatch dropped entirely) is honest unknown.
                if _baseline_landing_proven(trusted_n):
                    return _verified_full()
                if baseline_degraded:
                    # DELTA-3 m5: a DEGRADED baseline proves nothing — the
                    # visible read_timeout, not tail_unproven.
                    return _unverified("read_timeout")
                return _unverified("tail_unproven")
            if expected_n in trusted_n:
                # Landed but NOT at the tail: caret mid-text, inline autocomplete,
                # auto-closed brackets. With a baseline we can prove the exact
                # caret insertion; otherwise honest unknown (never mismatch, never
                # repair — VAL M3/Doubt B).
                if baseline_n is not None:
                    # WP12d (VAL R3 NIT): reuse the linear insertion-form check —
                    # the old per-position loop was quadratic on long buffers.
                    if _baseline_landing_proven(trusted_n):
                        return _verified_full()  # landed at the caret
                return _unverified("landed_not_at_tail")
            if baseline_n is None:
                # (d) No baseline: without it, no tail-shape can be attributed —
                # never repair.
                return _unverified("no_baseline")
            if not trusted_n.startswith(baseline_n):
                # The whole buffer changed from the baseline: transformed/foreign
                # content replaced it.
                return _unverified("transformed_buffer")
            new_part = trusted_n[len(baseline_n):]
            if new_part == "":
                # The buffer still EQUALS the baseline: nothing of ours landed
                # (drain lag or a full drop). k == 0 is excluded from the sound
                # repair rule — honest unknown.
                return _unverified("not_landed")
            k = 0
            while k < min(len(new_part), len(expected_n)) and new_part[k] == expected_n[k]:
                k += 1
            if k == len(new_part):
                # (b) Buffer == baseline + a PROPER prefix of the typed text: the
                # suffix is genuinely missing -> the one sound repair.
                return _repair_and_recheck(k)
            if k == 0:
                # (d) The new characters share NOTHING with the typed text:
                # password masks ('●●●●'), foreign input ('zzz'). A masked field
                # must NOT report a failure — honest unverified, never repair.
                return _unverified("transformed_buffer")
            # (c) 0 < k < len(expected_n) and the new part DIVERGED after k correct
            # characters (live: 'tett' vs 'test'; 'brown xxx' vs 'brown fox'):
            # POSITIVE evidence our text landed and then was corrupted -> flagged,
            # never retyped (appending would land after the corruption).
            return _mismatch()

        # WP13 DELTA-3 m7: the deadline must cover one read wall BEFORE the
        # first verification read — else the action stops verifying here.
        if not _budget_allows_read():
            return _unverified("verify_over_budget")
        prev = self._read_landed_normalized()
        if prev is None:
            # WP12e: a read at the Win32 WM_GETTEXT truncation cap gets its own
            # branch (read_truncated); WP13 DELTA-2: a read that hit the UIA
            # owner wall (or found the owner busy with a hung provider) is the
            # visible read_timeout branch; DELTA-3 m7: a read the remaining
            # deadline could not cover is verify_over_budget; a plain blind
            # read stays blind_read.
            if self._verify_over_budget:
                return _unverified("verify_over_budget")
            if getattr(self, "_last_read_degraded", None) == "read_timeout":
                return _unverified("read_timeout")
            if getattr(self, "_last_read_truncated", False):
                return _unverified("read_truncated")
            return _unverified("blind_read")
        if prev.endswith(expected_n):
            # Rare (dispatch already drained during the settle): single-read match is
            # accepted only after ONE quick confirming read agrees — and, WP12c
            # (VAL C2), only when the KNOWN baseline proves the insertion form.
            confirm = _quick_read()
            if confirm is not None and confirm == prev:
                if _baseline_landing_proven(prev):
                    return (
                        "Executed type. "
                        f"{_format_integrity_status(total, total, mismatch=0, unverified=0, healed=0)}"
                        f"{pace_token}"
                    )
                if baseline_degraded:
                    # DELTA-3 m5: a DEGRADED baseline proves nothing — the
                    # visible read_timeout, not tail_unproven.
                    return _unverified("read_timeout")
                return _unverified("tail_unproven")
            if confirm is None and self._verify_over_budget:
                # DELTA-4 N7 (VAL-WP13-02): a confirm read SKIPPED for budget is
                # verify_over_budget, not unstable_confirm.
                return _unverified("verify_over_budget")
            return _unverified("unstable_confirm")

        reads_left = TYPE_VERIFY_FAST_MAX_READS - 1
        escalated = False
        while reads_left > 0:
            reads_left -= 1
            current = _quick_read()
            if current is None:
                # DELTA-2: the typed read_timeout branch outranks the raw-cap
                # and plain blind branches (the read never happened at all);
                # DELTA-3 m7: a read skipped for budget is verify_over_budget.
                if self._verify_over_budget:
                    return _unverified("verify_over_budget")
                if getattr(self, "_last_read_degraded", None) == "read_timeout":
                    return _unverified("read_timeout")
                if getattr(self, "_last_read_truncated", False):
                    return _unverified("read_truncated")
                return _unverified("blind_read")
            if current.endswith(expected_n):
                # read grew monotonically into the full expected text (mid-drain at
                # first read) — verified, no horizon, no repair. WP12c (VAL C2):
                # with a KNOWN baseline the insertion form must hold.
                if _baseline_landing_proven(current):
                    return (
                        "Executed type. "
                        f"{_format_integrity_status(total, total, mismatch=0, unverified=0, healed=0)}"
                        f"{pace_token}"
                    )
                if baseline_degraded:
                    # DELTA-3 m5: a DEGRADED baseline proves nothing — the
                    # visible read_timeout, not tail_unproven.
                    return _unverified("read_timeout")
                return _unverified("tail_unproven")
            if not (current == prev or current.startswith(prev)):
                escalated = True
                break  # shrink/divergence: suspicious -> escalation
            if current == prev and current != "":
                # TWO CONSECUTIVE EQUAL NON-EMPTY reads: growth STALLED on a stable
                # buffer. WP12a: escalate regardless of the landed prefix — the
                # horizon-confirmed pair then decides: a genuinely missing suffix
                # (matched prefix > 0) gets the one sound retype; a substituted/
                # foreign tail (matched == 0 — the live 'brown xxx' shape) is
                # FLAGGED as mismatch and never retyped. Before WP12a the prefix==0
                # stall silently burned the fast budget into an unverified.
                # WP12c (VAL M3 residual): if the FIRST stable read ALREADY proves
                # the caret-insertion form against the KNOWN baseline, accept it
                # here — cheap and sound (two agreeing reads + the insertion
                # proof), no 2.5 s horizon needed.
                if baseline_n is not None and _baseline_landing_proven(current):
                    return (
                        "Executed type. "
                        f"{_format_integrity_status(total, total, mismatch=0, unverified=0, healed=0)}"
                        f"{pace_token}"
                    )
                escalated = True
                break
            prev = current
            if current == "":
                return _unverified("agreed_empty")
            # else: monotone growth or agreed non-matching buffer — keep polling
            # (pre-existing content may precede a still-pending drain).
        if escalated:
            # ESCALATION (suspicious pair or stalled partial): confirm across the
            # landing-lag horizon before any repair decision. A budget exhaustion on
            # pure growth is NOT escalated — the buffer is still draining, and the
            # honest fast answer is unverified (never a repair).
            trusted_n = _horizon_read()
            if trusted_n is None:
                if self._verify_over_budget:
                    return _unverified("verify_over_budget")
                return _unverified("horizon_blind")
            confirm_n = _quick_read()
            if confirm_n is None or confirm_n != trusted_n:
                if self._verify_over_budget:
                    return _unverified("verify_over_budget")
                return _unverified("horizon_unconfirmed")
            return _resolve_trusted(trusted_n)
        return _unverified("fast_budget_exhausted")

    def _execute_type_via_clipboard(
        self,
        action: GroundedAction,
        stop: StopToken | None,
        focus_hook: Callable[[], None] | None,
    ) -> str:
        """``via="clipboard"`` transport (v0.7.1 Defect C): set CF_UNICODETEXT, paste ctrl+v.

        Field evidence (Blender's Python Console): chords (hotkey ctrl+v, keypress enter)
        reach the app while per-character ``KEYEVENTF_UNICODE`` injection inserts nothing,
        so the text rides the chord path instead. Sequence:

        1. B8 focus settle + B3 gap policy, exactly like keypress/hotkey (no new knobs).
        2. Best-effort SAVE of the current clipboard (:func:`_clipboard_read_verdict`).
           ``None`` text + ``restorable`` is impossible; ``None`` text is either a
           non-text payload or an unreadable clipboard, and the reason distinguishes
           them (repair 3.3): ``non_text`` is a KNOWN loss and is DISCLOSED as
           ``clipboard_restore=non_text_destroyed``; ``unknown`` is an absence of
           knowledge and is never claimed.
        3. Set the action text as ``CF_UNICODETEXT``. A failure here raises
           :class:`InputBlockedError` (the established dispatch-could-not-proceed type;
           the payload names the clipboard cause) with ZERO keystrokes dispatched and the
           clipboard untouched.
        4. ONE ctrl+v chord through the EXISTING ``engine.chord`` path — failsafe,
           stop-token, and :class:`InputBlockedError` semantics ride it unchanged. The
           T8 focus hook runs once, pre-dispatch, like the single-chunk legacy path.
        5. **D13 / repair 3.2 (WP4b) — the settle, THEN the restore.**
           ``engine.chord`` only QUEUES keystrokes (SendInput returns before the
           target's wndproc runs), so the pre-fix ordering restored the clipboard ~5 µs
           after the chord returned while the transport's own
           ``TYPE_VERIFY_SETTLE_SECONDS`` (50 ms) was slept *afterwards*: a target that
           read the clipboard even slightly late pasted the user's OLD text. The window
           was zero-width BY CONSTRUCTION, so it lost every time. The rollback now
           happens after the settle.
        6. ``finally`` kept for the ABORT paths only: an exception between step 3 and
           step 4 (failsafe, stop token, :class:`InputBlockedError`) still restores,
           because on that path the chord was never dispatched and there is nothing to
           wait for. A restore failure is typed-annotated
           (``clipboard_restore=failed``) and NEVER raised; the restore dispatches no
           keystrokes.

        The save -> set -> paste ORDERING ITSELF IS UNCHANGED and must stay so: only
        the rollback's position moved. Privacy note: the user's clipboard now holds the
        agent's text for ~``TYPE_VERIFY_SETTLE_SECONDS`` longer than before.

        Verification is the honest single-read-back form (the per-char fast/escalation
        ladder does not apply to a single paste): confirmed endswith →
        ``integrity=verified``; blind or unconfirmed → ``integrity=unverified`` plus the
        ``TYPE_UNCONFIRMED no-readable-target`` diagnostic when no readable target
        exists. No repair is ever attempted on this transport (a re-paste could
        double-apply).
        """
        text = action.text or ""
        total = len(text)
        if not text:
            # Existing empty-text contract: a no-op, byte-identical, no clipboard churn.
            return "Executed type."
        if stop is not None:
            stop.ensure_live()
        self._apply_focus_settle()  # B8: pace behind a recent focus transition
        # B3 gap-policy parity (a ctrl+v chord carries no terminal key: policy no-op,
        # kept so the clipboard transport can never be faster-paced than keypress).
        self._apply_key_dispatch_gap(["ctrl", "v"])
        prior, prior_reason = _clipboard_read_verdict()
        restore_note = ""
        aborted = True
        try:
            if not _clipboard_write_text(text):
                raise InputBlockedError(
                    "Clipboard transport failed: the clipboard could not be set "
                    "(held by another process or allocation refused); no input dispatched."
                )
            if stop is not None:
                stop.ensure_live()
            if focus_hook is not None:
                # §1.1 K-05 (ORVEX-2026-108) delivered-chars rule for the PASTE
                # transport: the paste chord is all-or-nothing and the focus probe
                # runs exactly once BEFORE it — a FocusDriftError here means the
                # paste was never sent, so zero characters landed (delivered_chars
                # stays unset -> no partial_delivery key). There is no hook AFTER
                # the chord, so a post-paste drift cannot raise on this path; if the
                # chord was dispatched, the FULL text may have landed (upper bound:
                # len(text)) — an amount the reply already states via action.text.
                focus_hook()
            self._engine.chord(["ctrl", "v"])
            self._last_key_dispatch = time.monotonic()
            aborted = False
        finally:
            # ABORT PATH ONLY: the chord was never dispatched, so there is no paste to
            # wait for and the clipboard must go back immediately.
            if aborted and prior is not None and not _clipboard_write_text(prior):
                restore_note = f" {CLIPBOARD_RESTORE_FAILED}"
        # D13 / repair 3.2: the settle comes BEFORE the rollback — the target needs
        # the typed text still on the clipboard while it processes the queued chord.
        if TYPE_VERIFY_SETTLE_SECONDS > 0:
            time.sleep(TYPE_VERIFY_SETTLE_SECONDS)
        # Restore discipline: best-effort, silent-failure, ZERO extra keystrokes.
        if prior is not None and not _clipboard_write_text(prior):
            restore_note = f" {CLIPBOARD_RESTORE_FAILED}"
        elif prior is None and prior_reason == CLIPBOARD_NON_TEXT:
            # D13 / repair 3.3: the ``EmptyClipboard`` in step 3 DESTROYED a non-text
            # payload (a copied image, a file drop, RTF). Nothing can bring it back,
            # so the only honest thing left is to say so — same annotation shape,
            # distinct value.
            restore_note = (
                f" {CLIPBOARD_RESTORE_DESTROYED} (the clipboard held a non-text payload "
                "— a copied image/file — which EmptyClipboard destroyed; it could not "
                "be saved or restored)"
            )
        if stop is not None:
            stop.ensure_live()
        landed = self._read_landed_normalized()
        if landed is not None and landed.endswith(_normalize_landed_text(text)):
            status = _format_integrity_status(total, total, mismatch=0, unverified=0, healed=0)
            return f"Executed type. via=clipboard {status}{restore_note}"
        suffix = _format_integrity_status(0, total, mismatch=0, unverified=total, healed=0)
        base = f"Executed type. via=clipboard {suffix}{restore_note}"
        # Same marker discipline as the per-char path: the diagnostic names a MISSING
        # target, so a present-but-valueless control keeps the plain honest message.
        if not getattr(self, "_last_read_had_target", True):
            return f"{base} {TYPE_UNCONFIRMED_MARKER} ({TYPE_UNCONFIRMED_HINT_CLIPBOARD})"
        return base

    def _read_landed_normalized(self) -> str | None:
        """One read-back, normalized; ``None`` when the value is unavailable.

        WP12d (VAL-WP12-03 N1 BLOCKER): a RAW value at or above the reader's
        ``_WINDOW_TEXT_MAX_CHARS`` cap (text_readers.py — the Win32 reader returns
        at most the first 8192 characters) is TRUNCATED, so its tail proves
        nothing: such reads are BLIND on every ladder path (baseline, verification,
        horizon, repair recheck), which makes a repair on a truncated read
        impossible (the old behavior could double-type the missing "suffix" that
        was only cut off by the cap). Line-ending folding SHORTENS the text, so
        this check must use the RAW length, never the normalized one.
        """
        raw = self.read_focused_control_value()
        if raw is None:
            self._last_read_truncated = False
            return None
        # WP12e: the source attribute is set by read_focused_control_value; a test
        # double that replaces that method leaves it unset — treat as non-truncating.
        if getattr(self, "_last_value_source", "win32") == "win32" and len(raw) >= _WINDOW_TEXT_MAX_CHARS:
            # WP12e (VAL MINOR): only the Win32 WM_GETTEXT reader truncates at its
            # cap — UIA values are not truncated and keep full verification. A
            # capped Win32 read is BLIND and FLAGGED: the ladder answers
            # branch=read_truncated (never repaired, never a false verify).
            self._last_read_truncated = True
            return None
        self._last_read_truncated = False
        return _normalize_landed_text(raw)

    def _drag_waypoints(self, start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
        """Stroke waypoints for one drag (engine-selected policy).

        Interpolated on BOTH engines' factory defaults (L1-NEW-2: a
        press/teleport/release stroke makes freehand drawing apps draw dots —
        Paint samples held-button mouse moves, so intermediate waypoints restore
        a continuous stroke). Granularity is engine-driven: the SendInput
        factory default builds ~8 px segments (``drag_step_pixels``); engines
        that never set it (the pinned unit stubs, the pyautogui legacy parity)
        keep the ~40 px geometry. ``CORTEX_DRAG_INTERPOLATE=0`` (or
        ``drag_interpolate=False``) restores the minimal teleport stroke
        (move-press-move-release in three dispatch batches). Either way the stop
        token is checked before every segment and the button is always released.

        A zero-length drag (start == end) has no stroke to walk: no waypoints,
        just press-and-release at the spot.
        """
        if start == end:
            return []
        if self._engine.drag_interpolate:
            step_pixels = getattr(self._engine, "drag_step_pixels", 0)
            return _drag_segment_points(start, end, step_pixels=step_pixels)
        return [end]

    def find_window_by_title(self, target: str) -> WindowInfo | None:
        """Strong identity of the best-matching top-level window (delegates to the resolver).

        PERF-004 P0 bug fix: this method previously never overrode the
        :class:`ComputerBackend` stub and therefore ALWAYS returned ``None`` — the
        working module-level :func:`find_window_by_title` resolver existed but was only
        reachable through ``focus_window``'s internal call. Any consumer probing via the
        backend got false negatives.
        """
        return find_window_by_title(target)

    # --- interference probes + attach-or-launch (T8) -----------------------------------------

    def query_foreground_window(self) -> WindowInfo | None:
        """Strong identity of the current foreground window (the guard's per-dispatch probe)."""
        return query_foreground_window()

    def is_window_alive(self, hwnd: int | None) -> bool:
        """B6: ``IsWindow`` liveness probe for the FocusGuard's bound target."""
        if hwnd is None:
            return False
        try:
            if IS_WINDOWS and _user32 is not None and _user32.IsWindow(ctypes.c_void_p(int(hwnd))):
                return True
        except (OSError, AttributeError, TypeError, ValueError):
            return False
        return False

    def is_window_owned_by(self, hwnd: int | None, ancestor_hwnd: int | None) -> bool:
        """B11: the shared GW_OWNER-chain ownership probe (guard + sentinel aligned)."""
        return is_window_owned_by(hwnd, ancestor_hwnd)

    def query_focus_target(self) -> dict[str, object] | None:
        """Keyboard-focus target via ``GetGUIThreadInfo`` (T8 mechanism iv)."""
        return query_focus_target()

    def detect_system_dialog(
        self, bound_hwnd: int | None, title_table: Sequence[str] | None = None
    ) -> dict[str, object] | None:
        """Modal-dialog foreground probe (T8 mechanism iii); class/owner/title only."""
        return detect_system_dialog(bound_hwnd, title_table)

    def enumerate_app_windows(self, process_name: str) -> list[AppWindowCandidate]:
        """Process-filtered visible-window enumeration for attach-or-launch (T8)."""
        return enumerate_app_windows(process_name)

    def query_stuck_modifiers(self, keys: Sequence[str]) -> list[str]:
        """Pre-chord stuck-modifier sweep via ``GetAsyncKeyState`` (T8 mechanism v)."""
        return query_stuck_modifiers(keys)

    def release_modifiers(self, keys: list[str]) -> None:
        """Synthetic key-up for the named (session-dispatched) modifiers (release mode)."""
        self._engine.release_modifiers(list(keys))

    def close(self) -> None:
        """WP13 DELTA-3 m3 (VAL-WP13-01): stop the UIA reader's single COM owner
        thread — the sentinel request makes the owner release the CUIAutomation
        pointer ON the owner thread and CoUninitialize before exiting. Idempotent
        and never blocks; wired into the session-end funnel
        (``server.lifecycle._close_stopped_bundle``)."""
        reader = getattr(self, "_semantic_reader", None)
        stop = getattr(reader, "stop_owner_thread", None)
        if callable(stop):
            stop()

    def focus_window_title(self, title: str) -> str:
        """Refocus primitive reused by the guard's ``refocus_then_abort`` policy."""
        action = GroundedAction(
            action="focus_window", target=title, reason="InterferenceGuard refocus", confidence=1.0
        )
        return self._execute_focus_window(action)

    def _visible_title_hint(self, needle: str) -> str | None:
        """WP13 (field e3): READ-ONLY visible-window title scan for ensure_app hints.

        Enumerates VISIBLE top-level windows only (``IsWindowVisible`` filter,
        read-only EnumWindows via ``windows.find_windows_by_title``) and, when
        window TITLES contain ``needle`` case-insensitively, returns a typed
        hint naming the window title and EVERY matching owning process basename
        (deduplicated, capped at 3) — e.g. ``ensure_app_hint: a visible window
        titled 'WhatsApp' belongs to process 'WhatsApp.Root.exe' — retry with
        target='WhatsApp.Root.exe' or focus_window target='WhatsApp'``. Both taught
        arguments are REAL (``target`` on ensure_app and focus_window) so the
        taught retry passes the unknown-argument gate. Titles are
        user-controlled and spoofable: this is a HINT for the driver, NEVER an
        attach (the attach decision stays with the explicit target). Never
        raises (n6): any enumeration failure returns None — the hint can never
        turn a NO_INSTANCE reply into an error.
        """
        if not IS_WINDOWS or _user32 is None:
            return None
        needle_cf = (needle or "").strip().casefold()
        if not needle_cf:
            return None
        try:
            matches = find_windows_by_title(needle_cf, limit=3)
        except Exception:  # noqa: BLE001 - n6: the hint must never break the reply
            return None
        if not matches:
            return None
        info = matches[0]
        if not (info.title or "").strip():
            return None
        basenames: list[str] = []
        for match in matches:
            basename = (match.process_name or "").strip()
            if basename and basename not in basenames:
                basenames.append(basename)
        if not basenames:
            basenames = ["an unknown process"]
        named = ", ".join(repr(name) for name in basenames[:3])
        return (
            f"ensure_app_hint: a visible window titled {info.title!r} belongs to "
            f"process {named} — retry with target={basenames[0]!r} or focus_window "
            f"target={info.title!r} (titles are spoofable: hint only, never an attach)"
        )

    def ensure_app(self, target: str, allow_launch: bool = False) -> str:
        """Attach-or-launch probe (T8 mechanism ii) — bind to an EXISTING instance.

        Target format: ``process[|doc-token]`` (case-insensitive; the doc token is the
        leading title segment, e.g. ``excel|book1``). Resolution order:

        1. doc-identity match (when a doc token is given) or any visible window of the
           process (when not) -> focus it via the verified foreground switch ->
           ``REATTACHED title=... hwnd=...`` — never launches. The focus call is built
           ONLY from a non-empty title (R-03): an empty-title instance used to surface
           as a raw focus_window validation error; the resolver now prefers the
           top-of-Z-order TITLED match and raises the typed :class:`WindowFocusError`
           when no matched instance can name a focus target.
        2. No identity match but unsaved-candidate windows exist -> the structured
           ``AMBIGUOUS_INSTANCE`` payload (unsaved-work risk; the driver decides) —
           never launches, never closes anything. C·N2 (dev F3 fix 5.1): EVERY
           candidate is reported, each with its own ``unsaved=`` flag — the payload
           used to be built from the UNSAVED SUBSET alone, so a surface with one
           unsaved and one saved instance reported exactly one candidate (the saved
           one, the one the driver most needed to see, vanished from the payload).
        3. Nothing matches -> ``NO_INSTANCE target=... launch=...``. Only when the host
           explicitly authorized server-side launches (``allow_launch=True`` AND the
           session policy ``attach_or_launch.launch == "server"`` — enforced by the
           caller) AND the process is resolvable is ``Popen`` used.  The enforced
           DEFAULT policy is ``launch="server"``
           (``interference._default_launch_policy``), so with no process allowlist
           configured a ``NO_INSTANCE`` DOES spawn; ``CORTEX_ATTACH_OR_LAUNCH=driver``
           restores never-launch.

        v0.7.1 (Defect B) honest-payload contract for the AUTHORIZED (``launch=server``)
        launch outcome — the payload carries EXACTLY ONE of:

        - ``launched=<exe>`` — a process was really spawned (the actual basename);
        - ``launch_rejected=LaunchTargetError`` — the needle failed charset validation
          (nothing spawned, REM-C);
        - ``launch_unresolved=path-lookup-missed (...)`` — a valid needle that missed
          every resolution step (``CORTEX_LAUNCH_PATHS`` mapping, PATH, Store alias,
          raw-name spawn) — the field Blender case, no longer silent;
        - ``launch_unresolved=spawn-failed (<ExcType>: <msg[:120]>)`` — a resolved
          target whose ``Popen`` raised (the real OS error, bounded).

        ``launch=driver`` payloads never spawn and never carry the launch
        suffixes above; since WP13 (field e3) a NO_INSTANCE reply in EITHER
        launch mode may carry the additive ``ensure_app_hint:`` message suffix
        when a matching visible window exists. Supported way to launch GUI
        executables not on PATH: configure
        ``CORTEX_LAUNCH_PATHS`` (``needle=path;...``, see README "Environment
        variables") — the mapping is consulted FIRST.
        """
        from .interference import (
            LAUNCHED,
            format_ambiguous_instance,
            format_no_instance,
            format_reattached,
        )

        process_needle, _, doc_needle = target.partition("|")
        process_needle = process_needle.strip()
        doc_needle = doc_needle.strip().casefold()
        candidates = self.enumerate_app_windows(process_needle)
        if not candidates:
            payload = format_no_instance(target, "server" if allow_launch else "driver")
            # WP13 (field e3): a READ-ONLY visible-window title scan — when a window
            # TITLE contains the target, the hint names the owning process basename
            # so the driver can retry with the real target (e.g. WhatsApp.Root) or
            # focus the window by title. NEVER an attach: titles are user-controlled
            # and spoofable, so this is a HINT, never automatic behaviour.
            title_hint = self._visible_title_hint(process_needle)
            if title_hint:
                payload = f"{payload} {title_hint}"
            if allow_launch:
                # REM-C (V-2 F3): a typed needle rejection folds into the NO_INSTANCE
                # payload — nothing is spawned, ``launched=`` stays absent.
                try:
                    launched = self._launch_process(process_needle)
                except LaunchTargetError:
                    return f"{payload} launch_rejected=LaunchTargetError"
                if launched:
                    # WP4b (C8 / dev F3 fix 5.1): a REAL launch used to keep the
                    # ``NO_INSTANCE`` leading token, because the payload was built
                    # BEFORE the spawn attempt and the token was never revised. The
                    # leading token is now ``LAUNCHED`` — the first word of the
                    # message is what a driver (or a human reading a transcript)
                    # looks at, and it was saying the opposite of what happened.
                    # Every status suffix below is unchanged, so a consumer that
                    # reads ``launched=``/``launch_unresolved=`` still works.
                    # ``launch=server`` is kept: it is the policy that authorised
                    # the spawn, and a consumer reading the policy must not have to
                    # infer it.
                    payload = (
                        f"{LAUNCHED} target={target!r} launch=server launched={launched} "
                        f"(no existing window matched; the process was started server-side)"
                    )
                else:
                    # v0.7.1 (Defect B): an authorized launch that produced no process
                    # is NEVER silent — _launch_process leaves the typed reason on
                    # ``_launch_diagnostic`` (a stubbed launch, as in the pre-existing
                    # tests, sets nothing and the payload stays the bare probe).
                    diagnostic = getattr(self, "_launch_diagnostic", None)
                    self._launch_diagnostic = None
                    if diagnostic:
                        payload = f"{payload} {diagnostic}"
            return payload

        def _doc_matches(candidate: AppWindowCandidate) -> bool:
            if not doc_needle:
                return True
            token = (candidate.doc_token or "").casefold()
            title = (candidate.window.title or "").casefold()
            return doc_needle in token or doc_needle in title

        matches = [item for item in candidates if _doc_matches(item)]
        if matches:
            # R-03 (A7b): build the focus call ONLY from a non-empty title — focusing an
            # empty-title instance used to construct an invalid focus_window call that
            # surfaced as a raw validation error instead of a focused reattach. Prefer
            # the top-of-Z-order TITLED match; when no matched instance can name a
            # focus target, fail LOUDLY with the typed error (never an empty title).
            titled = [item for item in matches if (item.window.title or "").strip()]
            if not titled:
                raise WindowFocusError(
                    f"ensure_app resolved {process_needle!r} but all {len(matches)} "
                    "matched instance(s) have an empty window title; no valid focus "
                    "target (reattach by identity is unresolvable)"
                )
            window = titled[0].window
            self.focus_window_title(window.title)
            return format_reattached(window.title, window.hwnd)
        # WP4b (C·N2 / dev F3 fix 5.1): EVERY candidate is reported, each with its own
        # ``unsaved=`` flag.  This used to build the payload from the UNSAVED SUBSET
        # alone, so a surface with one unsaved and one saved instance reported exactly
        # one candidate — the saved one, the one the driver most needed to see,
        # vanished from the payload.  The disambiguation information (which title to
        # pick) is precisely what a driver reads this to get.  The branch NEVER
        # launches and NEVER closes anything, before or after.
        return format_ambiguous_instance(candidates)

    def _launch_process(self, process_needle: str) -> str | None:
        """Best-effort direct launch of an explicitly-authorized process.

        REM-C (V-2 F3/B7.b) hardening:

        - the needle is validated FIRST (:func:`validate_launch_needle`,
          ``^[A-Za-z0-9._ -]+$`` only) — an invalid needle raises
          :class:`LaunchTargetError` BEFORE any spawn (nothing is created);
        - the validated needle is resolved via the ``CORTEX_LAUNCH_PATHS`` mapping
          (v0.7.1, FIRST), then ``shutil.which``, and spawned as a plain one-element
          argv with ``shell=False`` (the cmd.exe quote-breakout surface is gone);
        - any soft failure (``FileNotFoundError`` etc.) keeps the REM-B contract:
          ``None`` (payload degrade), never an exception — only an INVALID needle
          is a typed rejection; since v0.7.1 the payload also carries the typed
          ``launch_unresolved=...`` reason (see below), so the degrade is no
          longer silent.

        REM-E (live-test gap, Store execution aliases): on this machine Paint and
        other Microsoft Store apps are reachable ONLY through the execution-alias
        reparse point under ``%LOCALAPPDATA%\\Microsoft\\WindowsApps\\<name>``
        (``shutil.which`` returns None for the bare name — the alias dir is not on
        the resolved PATH as a normal executable — and a raw-name Popen raises
        FileNotFoundError, so the launch degraded to the NO_INSTANCE probe with
        nothing spawned). The alias is now tried BETWEEN ``which`` and the
        raw-name fallback: order is (1) ``shutil.which(needle)``; (2) the Store
        alias path when it EXISTS under ``%LOCALAPPDATA%`` (alias resolution runs
        only on win32); (3) the raw-name fallback (Windows CreateProcess PATH
        search) unchanged. The needle is already charset-validated by then, so
        the alias join can never smuggle a path or metacharacter; launching
        through the alias stays a plain ``shell=False`` Popen on the alias path.

        Live evidence: the alias reparse file is
        named WITH its extension (``WindowsApps\\mspaint.exe``), so a BARE needle
        ("mspaint") found neither ``WindowsApps\\mspaint`` nor anything via
        ``which`` and silently degraded to the raw-name fallback (which failed:
        no mspaint process ever appeared while the NO_INSTANCE message implied a
        launch policy was honored). Bare names now resolve to the alias exactly
        like ".exe" names: the alias probe tries ``<needle>`` AND
        ``<needle>.exe`` (charset-validated either way). The return value is the
        basename of the spawn target that ACTUALLY started — ``launched=`` in the
        NO_INSTANCE payload is only ever emitted when a process was really
        spawned, and names the real executable (a bare "mspaint" launched through
        the alias reports ``launched=mspaint.exe``).

        v0.7.1 (Defect B, field Blender case) — mapping-first resolution + typed
        failure reasons: a GUI executable installed outside PATH (``shutil.which``
        -> ``None``) used to degrade to a SILENT ``None`` (bare NO_INSTANCE, zero
        explanation, nothing spawned). Two changes, contract-preserving:

        - ``CORTEX_LAUNCH_PATHS`` (:data:`LAUNCH_PATHS_ENV`, parsed fail-safe by
          :func:`parse_launch_paths` at this method's entry) is consulted FIRST:
          resolution order is now **mapped path -> ``shutil.which`` -> Store
          alias -> raw-name fallback** (the later legs byte-identical to the
          REM-E order). The external return contract is UNCHANGED — the basename
          of the target that ACTUALLY started, or ``None`` on any failure — so
          every pre-existing pin (``is None`` on failure) keeps holding.
        - the typed failure reason travels OUT-OF-BAND on
          ``self._launch_diagnostic`` (read and cleared by ``ensure_app``; a
          stubbed ``_launch_process``, as used by existing tests, simply never
          sets it): ``launch_unresolved=path-lookup-missed`` when nothing
          resolved (the raw-name spawn's FileNotFoundError), or
          ``launch_unresolved=spawn-failed (<ExcType>: <msg[:120]>)`` when a
          resolved target's ``Popen`` raised. ``ensure_app`` folds exactly one
          suffix into the NO_INSTANCE payload — never a silent bare probe, never
          a false ``launched=`` claim.
        """
        process_needle = validate_launch_needle(process_needle)  # raises before any spawn
        self._launch_diagnostic: str | None = None  # fresh per attempt (consumed by ensure_app)
        spawn_target: str | None = _mapped_launch_path(process_needle)
        resolved = spawn_target is not None
        if not resolved:
            spawn_target = shutil.which(process_needle)
            resolved = spawn_target is not None
        if not resolved:
            alias = self._store_alias_path(process_needle)
            if alias:
                spawn_target = alias
                resolved = True
        # raw-name fallback (Windows CreateProcess PATH search): spawn_target stays
        # None here and the bare, already-charset-validated needle goes to Popen.
        target = spawn_target if spawn_target else process_needle
        try:
            subprocess.Popen([target], shell=False)
            return os.path.basename(target)
        except Exception as exc:  # noqa: BLE001 - a launch failure degrades to the payload
            self._launch_diagnostic = _launch_unresolved_suffix(exc, resolved=resolved)
            return None

    @staticmethod
    def _store_alias_path(process_needle: str) -> str | None:
        """The Windows Store execution-alias path for ``process_needle``, or ``None``.

        Tries ``%LOCALAPPDATA%\\Microsoft\\WindowsApps\\<needle>`` and
        ``<needle>.exe`` (the reparse points are named WITH the extension, so bare
        names must resolve through the ``.exe`` alias). win32-only; the needle is
        charset-validated before any caller reaches this, so the join cannot
        smuggle path separators or metacharacters.
        """
        if not IS_WINDOWS:
            return None
        localappdata = os.environ.get("LOCALAPPDATA", "")
        if not localappdata:
            return None
        alias_dir = os.path.join(localappdata, "Microsoft", "WindowsApps")
        candidates = [process_needle]
        if not process_needle.casefold().endswith(".exe"):
            candidates.append(process_needle + ".exe")
        for candidate in candidates:
            alias_path = os.path.join(alias_dir, candidate)
            if os.path.exists(alias_path):
                return alias_path
        return None

    def _apply_key_dispatch_gap(self, keys: list[str]) -> None:
        """B3 settle/gap policy: pace terminal-key chords behind the previous dispatch.

        When ``keys`` contains a terminal key (enter/return/tab) and the previous
        keyboard dispatch happened less than ``KEY_DISPATCH_GAP_SECONDS`` ago, sleep out
        the remainder first. A dropped final Enter in a fast batch (anomaly B3) is an
        input-stack race; the gap is the deliberate, configurable exception to the
        zero-pacing doctrine (default 0.05 s; ``CORTEX_KEY_DISPATCH_GAP=0`` disables).
        """
        if KEY_DISPATCH_GAP_SECONDS <= 0:
            return
        folded = {str(key).strip().casefold() for key in keys}
        if not (folded & TERMINAL_KEYS):
            return
        elapsed = time.monotonic() - self._last_key_dispatch
        remaining = KEY_DISPATCH_GAP_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _execute_focus_window(self, action: GroundedAction) -> str:
        """Bring the window matching ``action.target`` to the foreground (Win32 sequence).

        Sequence: restore when minimized (``IsIconic`` -> ``ShowWindow(SW_RESTORE)``),
        then the standard ``AttachThreadInput`` foreground switch with an ALT
        keybd_event nudge, then verify ``GetForegroundWindow()`` actually moved — a
        refusal raises :class:`WindowFocusError` (documented residual: the previously
        focused window is not restored; the caller re-observes). The stop token is
        checked once by the ``execute`` header; this path performs no pyautogui input.

        B7 (T8): focusing the ALREADY-foreground window is an idempotent no-op success —
        running the AttachThreadInput/ALT-nudge sequence against the current foreground
        can refuse and turn a legitimate re-bind into a spurious WindowFocusError.
        """
        target = (action.target or "").strip()
        if not target:
            raise ValueError("A target window title is required for focus_window actions")
        if not IS_WINDOWS or _user32 is None:
            raise WindowFocusError("Window focus requires Windows.")
        candidate = find_window_by_title(target)
        if candidate is None or candidate.hwnd is None:
            raise WindowFocusError(f"no window matching '{target}'")
        hwnd = int(candidate.hwnd)
        try:
            if _user32.IsIconic(hwnd):
                _user32.ShowWindow(hwnd, _SW_RESTORE)
            elif int(_user32.GetForegroundWindow() or 0) == hwnd:
                self._note_focus_transition()
                return f"Focused window '{candidate.title or target}'."
            foreground = _user32.GetForegroundWindow()
            fg_thread = _user32.GetWindowThreadProcessId(int(foreground or 0), None)
            me = _kernel32.GetCurrentThreadId()
            _user32.AttachThreadInput(me, fg_thread, True)
            try:
                _user32.keybd_event(_VK_MENU, 0, 0, 0)
                _user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)  # ALT nudge
                _user32.SetForegroundWindow(hwnd)
            finally:
                _user32.AttachThreadInput(me, fg_thread, False)
            focused = int(_user32.GetForegroundWindow() or 0)
        except (OSError, AttributeError) as exc:
            raise WindowFocusError(f"Window focus failed: {exc}") from exc
        if focused != hwnd:
            raise WindowFocusError(
                f"SetForegroundWindow refused focus for '{candidate.title or target}'; "
                "the foreground window did not change."
            )
        self._note_focus_transition()
        return f"Focused window '{candidate.title or target}'."

    def _note_focus_transition(self) -> None:
        """B8: record a focus-transition instant for the post-activation settle policy.

        The first keyboard dispatch following a window activation races the freshly
        activated thread's input queue (the A5 residual risk class: dropped/misdelivered
        first keys, the B5 wedge topology). Keyboard actions pace themselves behind
        ``FOCUS_TRANSITION_SETTLE_SECONDS`` (``CORTEX_FOCUS_SETTLE_SECONDS``).
        """
        self._last_focus_transition = time.monotonic()

    def _apply_focus_settle(self) -> None:
        """B8: pace keyboard input behind a recent focus transition (configurable)."""
        if FOCUS_TRANSITION_SETTLE_SECONDS <= 0:
            return
        elapsed = time.monotonic() - self._last_focus_transition
        remaining = FOCUS_TRANSITION_SETTLE_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _build_default_context(self) -> _CaptureContext:
        """Passthrough-by-measurement context when executing before any observation."""
        self._refresh_monitors()
        monitor = _select_target_monitor(self._monitors, get_cursor_position(), None)
        verdict = classify_coordinate_space(
            monitor.bounds[2], monitor.bounds[3], monitor, dpi_estimated=self.dpi_estimated
        )
        return _CaptureContext(monitor=monitor, verdict=verdict)

