"""In-process fake backend mirroring the real backend's contracts for offline tests."""

from __future__ import annotations

import base64
import io
import os
import time

from PIL import Image

from ..core.models import MonitorInfo, Observation, WindowInfo
from .backend import (
    ComputerBackend,
    FOCUS_TRANSITION_SETTLE_SECONDS,
    KEY_DISPATCH_GAP_SECONDS,
    TERMINAL_KEYS,
    TYPE_INTEGRITY_ENABLED,
    TYPE_UNCONFIRMED_HINT_CLIPBOARD,
    TYPE_UNCONFIRMED_HINT_SENDINPUT,
    TYPE_UNCONFIRMED_MARKER,
    TYPE_VERIFY_SETTLE_SECONDS,
    _CaptureContext,
    _format_integrity_status,
    _mark_action_dispatched,
    _normalize_landed_text,
    _validate_action_defense,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation-only names (lazy under __future__ annotations)
    from collections.abc import Callable, Sequence
    from ..core.models import GroundedAction
    from ..core.state import StopToken
    from .launch import AppWindowCandidate
from .coordinates import CoordinateTransform, classify_coordinate_space
from .errors import InputBlockedError, LaunchTargetError, WindowFocusError
from .launch import _mapped_launch_path, validate_launch_needle
from .motion import (
    WAIT_MAX_SECONDS,
    _drag_segment_points,
    _sleep_for_wait_action,
    _wait_clamp_note,
    _wait_for_hold,
)
from .pointer_ring import PointerRing
from .windows import _select_target_monitor, _title_match_rank


class FakeComputerBackend(ComputerBackend):
    """Faithful in-memory backend mirroring LocalComputerBackend's contracts for tests.

    Implements the same observe/execute/wait contracts, StopToken checks, coordinate
    space classification (the shared ``classify_coordinate_space``), coordinate
    refusal, cursor localization, and wait slicing as the real backend — driven by
    injectable state instead of a real desktop.

    Constructor params:
    - ``width``/``height``: screenshot dimensions (legacy positionals preserved).
    - ``monitors``: fake monitor set (``MonitorInfo`` list, physical bounds + DPI scale).
      Defaults to one primary monitor exactly matching the screenshot dimensions.
    - ``active_window``: fake foreground window identity (``WindowInfo``), or None.
    - ``windows``: fake top-level window list for title lookup (``WindowInfo`` list,
      first entry = top of Z-order). Defaults to ``[active_window]`` when an active
      window is configured, else ``[]``. ``set_windows`` replaces it.
    - ``cursor``: fake cursor in *physical virtual-screen coordinates* (GetCursorPos
      convention — may be negative on secondary monitors), or None for no cursor.
    - ``input_blocked``: when True, ``execute`` raises ``InputBlockedError``.

    Simulation methods: ``set_active_window`` (focus change), ``set_window_bounds``
    (window move), ``set_monitors`` (display/DPI change), ``set_cursor``,
    ``set_screenshot_size`` (resolution change / space mismatch), ``set_input_blocked``,
    ``set_windows`` (window population for focus_window / allowlist probes).

    Recorded state: ``executed`` (actions whose simulated input completed — a pre-stopped,
    input-blocked, or coordinate-refused execute records nothing, and neither does a wait
    interrupted by the stop token), ``observed`` (every observation produced),
    ``width``/``height`` (current screenshot dims). For drag actions the stroke is
    simulated as a stop-checked cursor walk (the real backend's INTERPOLATED waypoint
    policy — the SendInput default dispatches a minimal stroke instead, but the
    stop-per-segment and always-release contract is identical): ``drags`` records the mapped physical ``(start, end)`` pairs of completed
    drags, the cursor ends at the drag end point, and ``_drag_button_down`` mirrors the
    held mouse button (always ``False`` after execute, including on a mid-stroke stop —
    the release guarantee is part of the contract). Drawing pixels in the fake canvas is
    not required; drag verification in tests uses visual_change with a flip or predicate.
    ``move`` actions reposition the simulated cursor to the mapped physical point
    (unverifiable coordinate spaces raise ``CoordinateSpaceError`` identically).
    ``focus_window`` resolves ``action.target`` through ``find_window_by_title`` (same
    exact > prefix > substring precedence as the real backend): a miss raises
    ``WindowFocusError`` and records nothing; a hit swaps ``active_window`` (a copy) and
    appends the target to ``focused``.
    """

    def __init__(
        self,
        width: int = 1280,
        height: int = 720,
        *,
        monitors: list[MonitorInfo] | None = None,
        active_window: WindowInfo | None = None,
        windows: list[WindowInfo] | None = None,
        cursor: tuple[int, int] | None = None,
        input_blocked: bool = False,
        initial_clipboard: str | None = None,
        clipboard_app_latency_s: float = 0.0,
    ) -> None:
        self.width = width
        self.height = height
        self.monitors = (
            list(monitors)
            if monitors
            else [
                MonitorInfo(
                    id="monitor-0",
                    index=0,
                    bounds=(0, 0, width, height),
                    is_primary=True,
                )
            ]
        )
        self.active_window = active_window
        self.windows: list[WindowInfo] = (
            list(windows) if windows is not None else ([active_window] if active_window else [])
        )
        self.focused: list[str] = []
        self._cursor_physical = cursor
        self._input_blocked = input_blocked
        self.executed: list[GroundedAction] = []
        self.drags: list[tuple[tuple[int, int], tuple[int, int]]] = []
        self._drag_button_down = False
        # WP2 (ORVEX-2026-107): the simulated keyboard — keys currently held by a
        # ``hold_ms`` keypress/hotkey. Always cleared by ``_simulate_held_keys``'s
        # ``finally``, including on a mid-hold stop (an aborted hold must not leave a
        # simulated key stuck, the fake-side twin of a physically stuck key).
        self.held_keys: list[str] = []
        # DELTA 1 (F2): the simulated mouse buttons currently held by a ``hold_ms``
        # click/right_click (same discipline as ``held_keys``: always cleared in the
        # ``finally``, including on a mid-hold stop), and the timed moves RECORDED by
        # duration-bearing ``move`` actions (endpoints + the declared duration).
        self.held_buttons: list[str] = []
        self.timed_moves: list[tuple[tuple[int, int], tuple[int, int], int]] = []
        self.observed: list[Observation] = []
        self._active_context: _CaptureContext | None = None
        # --- T8 interference-probe surface (inert by default; tests inject state) ---
        self.focus_target: dict[str, object] | None = None
        self.system_dialog: dict[str, object] | None = None
        self.app_windows: list[AppWindowCandidate] = []
        self.stuck_modifiers: list[str] = []
        self.released_modifiers: list[str] = []
        # (child_hwnd, ancestor_hwnd) pairs the fake reports as GW_OWNER-owned.
        self.owned_windows: set[tuple[int, int]] = set()
        self.ensure_app_calls: list[str] = []
        self.launched_processes: list[str] = []
        # launch-resolution simulation: ``store_aliases`` are casefolded
        # names resolvable ONLY through the Store execution alias (they launch and
        # record as "<name>.exe" — the reparse point's real name);
        # ``launch_unresolvable`` are names that fail to resolve/start (the fake
        # degrades to the NO_INSTANCE payload WITHOUT a ``launched=`` claim, exactly
        # like the real soft-failure contract). Both default empty: the launch always
        # succeeds and records the needle verbatim (pre-058 behavior).
        self.store_aliases: set[str] = set()
        self.launch_unresolvable: set[str] = set()
        self._last_key_dispatch: float = 0.0
        self._last_focus_transition: float = 0.0
        # R-04 integrity read-back injection: the fake focused control's value.
        # ``None`` (default) = "cannot verify" — type actions report
        # ``integrity=unverified`` exactly like the real backend with no readable
        # control; a string is compared against the typed text (CRLF-normalized,
        # same policy as the real backend, including the simulated prefix repair).
        self.focused_control_value: str | None = None
        # v0.7.1 (Defect C) clipboard-transport simulation: ``clipboard_value`` models
        # the OS clipboard (save/set/restore discipline, parity with the real ctypes
        # path); ``clipboard_pastes`` records every simulated ctrl+v paste payload so
        # tests can assert the transport actually dispatched the chord.
        #
        # D13 / repair 3.2 (WP4b): ``clipboard_app_latency_s`` models a target
        # application that needs wall time to PROCESS a queued chord before it reads
        # the clipboard — the real shape (``SendInputEngine.chord`` only queues). With
        # it non-zero the recorded paste payload is read from the clipboard AT DELIVERY
        # TIME, so a restore ordered before the paste becomes visible to the fake (it
        # was not, while the double recorded the local ``text`` variable).
        self.clipboard_value: str | None = initial_clipboard
        self.clipboard_pastes: list[str] = []
        self.clipboard_app_latency_s = max(0.0, float(clipboard_app_latency_s))
        # WP-C2 (ORVEX-2026-109): the SAME pointer-event ring the real backend keeps
        # (see LocalComputerBackend) — offline tests drive press state through it.
        self.pointer_ring = PointerRing()

    def set_windows(self, windows: list[WindowInfo]) -> None:
        """Replace the fake top-level window list (first entry = top of Z-order)."""
        self.windows = list(windows)

    def find_window_by_title(self, target: str) -> WindowInfo | None:
        """Same matching rule as the real backend: exact > prefix > substring, Z-order."""
        needle = (target or "").strip().casefold()
        if not needle:
            return None
        best: tuple[int, int, WindowInfo] | None = None  # (rank, z_index, window)
        for z_index, window in enumerate(self.windows):
            rank = _title_match_rank(window.title, needle)
            if rank is None:
                continue
            if best is None or rank > best[0]:  # first-in-Z-order wins ties
                best = (rank, z_index, window)
        return best[2] if best is not None else None

    # --- T8 interference-probe surface (mirrors LocalComputerBackend's contract) -----

    def query_foreground_window(self) -> WindowInfo | None:
        """The fake foreground window (``active_window``), like the real identity probe."""
        return self.active_window.model_copy() if self.active_window else None

    def is_window_alive(self, hwnd: int | None) -> bool:
        """A fake window is alive while it is active or listed in ``windows``."""
        if hwnd is None:
            return False
        if self.active_window is not None and self.active_window.hwnd == hwnd:
            return True
        for window in self.windows:
            if isinstance(window, WindowInfo) and window.hwnd == hwnd:
                return True
        return False

    def is_window_owned_by(self, hwnd: int | None, ancestor_hwnd: int | None) -> bool:
        """Injected ownership probe (set ``owned_windows`` pairs to simulate modals)."""
        if hwnd is None or ancestor_hwnd is None:
            return False
        return (int(hwnd), int(ancestor_hwnd)) in self.owned_windows

    def _note_focus_transition(self) -> None:
        """Same B8 transition record as :class:`LocalComputerBackend`."""
        self._last_focus_transition = time.monotonic()

    def _apply_focus_settle(self) -> None:
        """Same B8 settle contract as :class:`LocalComputerBackend`."""
        if FOCUS_TRANSITION_SETTLE_SECONDS <= 0:
            return
        elapsed = time.monotonic() - self._last_focus_transition
        remaining = FOCUS_TRANSITION_SETTLE_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def query_focus_target(self) -> dict[str, object] | None:
        """Injected focus-target probe (set ``focus_target`` to simulate keyboard focus)."""
        return dict(self.focus_target) if self.focus_target is not None else None

    def detect_system_dialog(
        self, bound_hwnd: int | None, title_table: Sequence[str] | None = None
    ) -> dict[str, object] | None:
        """Injected modal-dialog probe (set ``system_dialog`` to simulate a dialog)."""
        return dict(self.system_dialog) if self.system_dialog is not None else None

    def enumerate_app_windows(self, process_name: str) -> list[AppWindowCandidate]:
        """Injected app-window population (set ``app_windows`` to simulate instances)."""
        return list(self.app_windows)

    def query_stuck_modifiers(self, keys: Sequence[str]) -> list[str]:
        """Injected stuck-modifier sweep (set ``stuck_modifiers`` to simulate sticks)."""
        return list(self.stuck_modifiers)

    def release_modifiers(self, keys: list[str]) -> None:
        """Record a release-mode key-up sweep and CLEAR the simulated sticks (T8)."""
        self.released_modifiers.extend(keys)
        self.stuck_modifiers = [name for name in self.stuck_modifiers if name not in set(keys)]

    def _apply_key_dispatch_gap(self, keys: list[str]) -> None:
        """Same B3 settle/gap contract as :class:`LocalComputerBackend`."""
        if KEY_DISPATCH_GAP_SECONDS <= 0:
            return
        folded = {str(key).strip().casefold() for key in keys}
        if not (folded & TERMINAL_KEYS):
            return
        elapsed = time.monotonic() - self._last_key_dispatch
        remaining = KEY_DISPATCH_GAP_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def focus_window_title(self, title: str) -> str:
        """Same verified-refocus contract: a miss raises ``WindowFocusError``."""
        candidate = self.find_window_by_title(title)
        if candidate is None:
            raise WindowFocusError(f"no window matching '{title}'")
        self.active_window = candidate.model_copy()
        self.focused.append(title)
        return f"Focused window '{candidate.title or title}'."

    def ensure_app(self, target: str, allow_launch: bool = False) -> str:
        """Simulated attach-or-launch over the injected ``app_windows`` population."""
        from .interference import (
            LAUNCHED,
            format_ambiguous_instance,
            format_no_instance,
            format_reattached,
        )

        self.ensure_app_calls.append(target)
        process_needle, _, doc_needle = target.partition("|")
        process_needle = process_needle.strip()
        doc_needle = doc_needle.strip().casefold()
        candidates = self.enumerate_app_windows(process_needle)
        if not candidates:
            payload = format_no_instance(target, "server" if allow_launch else "driver")
            if allow_launch:
                # REM-C (V-2 F3): same typed validation as the real backend — a
                # hostile needle is never recorded as spawned and never reaches
                # ``launched=`` (launch_rejected=LaunchTargetError folds into the
                # NO_INSTANCE payload instead). An UNRESOLVABLE target likewise
                # never claims ``launched=`` — since v0.7.1 (Defect B) it carries
                # the SAME typed ``launch_unresolved=path-lookup-missed`` suffix
                # the real backend emits (real-vs-fake payload parity), instead of
                # the pre-0.7.1 silent bare probe; a Store-alias name or a
                # ``CORTEX_LAUNCH_PATHS`` mapping launches and records the real
                # ``<name>.exe`` identity.
                from .interference import format_launch_unresolved_path_miss

                try:
                    validate_launch_needle(process_needle)
                except LaunchTargetError:
                    return f"{payload} launch_rejected=LaunchTargetError"
                resolved = self._resolve_fake_launch_target(process_needle)
                if resolved is None:
                    payload = f"{payload} {format_launch_unresolved_path_miss()}"
                    return payload
                self.launched_processes.append(resolved)
                # WP4b (C8 / dev F3 fix 5.1): real-vs-fake payload PARITY on the
                # revised leading token — a real spawn reads ``LAUNCHED ...``, not
                # ``NO_INSTANCE ...``.
                payload = (
                    f"{LAUNCHED} target={target!r} launch=server launched={resolved} "
                    f"(no existing window matched; the process was started server-side)"
                )
            return payload

        def _doc_matches(candidate: AppWindowCandidate) -> bool:
            if not doc_needle:
                return True
            token = (candidate.doc_token or "").casefold()
            title = (candidate.window.title or "").casefold()
            return doc_needle in token or doc_needle in title

        matches = [item for item in candidates if _doc_matches(item)]
        if matches:
            # R-03 (A7b), identical to LocalComputerBackend.ensure_app: the focus call
            # is built ONLY from a non-empty title; prefer the top-of-Z-order TITLED
            # match; a typed WindowFocusError when no matched instance can name one.
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
        # WP4b (C·N2): EVERY candidate is reported, each with its own ``unsaved=``
        # flag — identical to LocalComputerBackend.ensure_app (see that comment).
        return format_ambiguous_instance(candidates)

    def _resolve_fake_launch_target(self, process_needle: str) -> str | None:
        """alias-launch launch-resolution simulation mirroring the real resolution order.

        Returns the NAME the launch records (``None`` = nothing spawned): a
        ``CORTEX_LAUNCH_PATHS`` mapping hit (v0.7.1 parity — the mapping goes FIRST
        on the real backend too) yields the mapped file's real basename; an
        unresolvable needle (``launch_unresolvable``, .exe-tolerant — the fake's
        model of "PATH/alias miss", which the real backend reports as
        ``launch_unresolved=path-lookup-missed``) yields ``None`` so the
        NO_INSTANCE payload stays honest; a Store-alias name
        (``store_aliases``) yields ``<name>.exe`` (the reparse point's real name);
        everything else keeps the pre-058 default — the needle itself, verbatim.
        """
        mapped = _mapped_launch_path(process_needle)
        if mapped:
            return os.path.basename(mapped)
        needle = (process_needle or "").strip().casefold().removesuffix(".exe")
        if needle in {n.strip().casefold().removesuffix(".exe") for n in self.launch_unresolvable}:
            return None
        if needle in {n.strip().casefold().removesuffix(".exe") for n in self.store_aliases}:
            return needle + ".exe"
        return process_needle

    def set_active_window(self, window: WindowInfo | None) -> None:
        """Swap the fake foreground window (simulates focus change between observations)."""
        self.active_window = window

    def set_window_bounds(self, bounds: tuple[int, int, int, int]) -> None:
        """Move/resize the fake foreground window (simulates a window move)."""
        if self.active_window is None:
            raise ValueError("No active window configured; call set_active_window first.")
        self.active_window = self.active_window.model_copy(update={"bounds": bounds})

    def set_monitors(self, monitors: list[MonitorInfo]) -> None:
        """Replace the fake monitor set (simulates a display or DPI change)."""
        if not monitors:
            raise ValueError("At least one monitor is required.")
        self.monitors = list(monitors)

    def set_cursor(self, position: tuple[int, int]) -> None:
        """Set the fake cursor in physical virtual-screen coordinates (GetCursorPos convention)."""
        self._cursor_physical = position

    def last_click(self) -> dict[str, object] | None:
        """WP-C2: the most recent completed press from the pointer ring (real-backend parity)."""
        return self.pointer_ring.last_click()

    def set_screenshot_size(self, width: int, height: int) -> None:
        """Change the fake screenshot dimensions (simulates resolution change/mismatch)."""
        if width <= 0 or height <= 0:
            raise ValueError("Screenshot dimensions must be positive.")
        self.width = width
        self.height = height

    def set_input_blocked(self, blocked: bool) -> None:
        """When True, execute() raises InputBlockedError (simulates a blocked desktop)."""
        self._input_blocked = blocked

    def observe(self, monitor_index: int | None = None) -> Observation:
        """Produce a full Observation from the injected fake state (same contract as real)."""
        cursor_physical = self._cursor_physical
        monitor = _select_target_monitor(self.monitors, cursor_physical, monitor_index)
        verdict = classify_coordinate_space(self.width, self.height, monitor, dpi_estimated=False)
        cursor_local: tuple[int, int] | None = None
        if cursor_physical is not None:
            cursor_local = CoordinateTransform(
                origin_x=monitor.bounds[0],
                origin_y=monitor.bounds[1],
                scale_x=verdict.scale_x,
                scale_y=verdict.scale_y,
            ).to_screenshot(*cursor_physical)
        window = self.active_window.model_copy() if self.active_window else None
        self._active_context = _CaptureContext(monitor=monitor, verdict=verdict)
        observation = Observation(
            image_base64=self._white_png(),
            width=self.width,
            height=self.height,
            active_window=(window.title or None) if window else None,
            cursor_x=cursor_local[0] if cursor_local else None,
            cursor_y=cursor_local[1] if cursor_local else None,
            input_width=verdict.input_width,
            input_height=verdict.input_height,
            coordinate_scale_x=verdict.scale_x,
            coordinate_scale_y=verdict.scale_y,
            coordinate_space=verdict.space,
            monitor=monitor.model_copy(),
            active_window_info=window,
            redactions_applied=False,
        )
        self.observed.append(observation)
        return observation

    def execute(
        self,
        action: GroundedAction,
        stop: StopToken | None = None,
        focus_hook: Callable[[], None] | None = None,
        allow_launch: bool = False,
    ) -> str:
        """Record and simulate one action; the stop token gates every simulated input."""
        _validate_action_defense(action)
        if stop is not None:
            stop.ensure_live()
        if action.action == "wait":
            total = min(max(action.delta, 0), WAIT_MAX_SECONDS)
            if stop is None:
                time.sleep(total)
            else:
                _sleep_for_wait_action(total, stop)
            self.executed.append(action)
            _mark_action_dispatched(action)
            return f"Simulated {action.action}.{_wait_clamp_note(action.delta, total)}"
        if self._input_blocked:
            raise InputBlockedError("FakeComputerBackend is configured to block physical input.")
        if action.action in {"click", "double_click", "right_click"}:
            if action.point is None:
                raise ValueError("A point is required for click actions")
            # right_click parity: the fake performs the identical screenshot->physical
            # transform a real right-button press would (cursor lands on the point).
            self._cursor_physical = self._map_to_physical(action.point.x, action.point.y)
            button = "right" if action.action == "right_click" else "left"
            # DELTA 1 (F2): the fake MODELS a held click like it models a held key —
            # record the hold (sliced, stop-honouring, always recorded as released).
            # A fake that silently ignored hold_ms would let the unit suite diverge
            # from the real backend (the divergence class this repo forbids).
            if action.hold_ms is not None:
                if action.action == "double_click":
                    # Unreachable through every gate (model + validator + defense);
                    # kept as the same last-line refusal the real path has.
                    raise ValueError("hold_ms is not valid on double_click actions")
                self._simulate_held_click(action, button, stop)
            else:
                # WP-C2: the same ring record the real dispatch site writes — one
                # down/up pair per click batch, in PHYSICAL coordinates, after the
                # simulated dispatch completes (ordering/timing unchanged).
                point = self._cursor_physical
                self.pointer_ring.record("down", point[0], point[1], button)
                self.pointer_ring.record("up", point[0], point[1], button)
        elif action.action == "move":
            if action.point is None:
                raise ValueError("A point is required for move actions")
            # Unverifiable coordinate spaces raise CoordinateSpaceError identically to
            # click (shared _map_to_physical refusal) and record nothing.
            previous = self._cursor_physical
            self._cursor_physical = self._map_to_physical(action.point.x, action.point.y)
            # DELTA 1 (F2): a duration-bearing move is RECORDED as a timed move (the
            # O3 precedent: the fake simulates no motion physics — harness only — but
            # the timed intent and its endpoints are observable, so a test can tell a
            # timed move from a jump).
            if action.duration_ms:
                self.timed_moves.append((previous, self._cursor_physical, action.duration_ms))
        elif action.action == "focus_window":
            target = (action.target or "").strip()
            candidate = self.find_window_by_title(target)
            if candidate is None:
                raise WindowFocusError(f"no window matching '{target}'")
            self.active_window = candidate.model_copy()
            self.focused.append(target)
            self._note_focus_transition()  # B8 parity: a fake focus switch is a transition
        elif action.action == "ensure_app":
            target = (action.target or "").strip()
            if not target:
                raise ValueError("A target is required for ensure_app actions")
            return self.ensure_app(target, allow_launch=allow_launch)
        elif action.action in {"keypress", "hotkey"}:
            # B3/B8 settle parity: the fake models both pacing contracts.
            keys = list(action.keys)
            if stop is not None:
                stop.ensure_live()
            self._apply_focus_settle()
            self._apply_key_dispatch_gap(keys)
            # WP2 (ORVEX-2026-107): the fake MODELS the hold rather than skipping it —
            # a fake that silently ignored hold_ms would let the whole unit suite
            # diverge from the real backend, which is the divergence class this repo
            # forbids. The wait is the SAME shared ``_sleep_for_hold`` the real path
            # uses (D4), so slicing and cancellation behave identically on both sides.
            if action.hold_ms is not None:
                self._simulate_held_keys(action, keys, stop)
            self._last_key_dispatch = time.monotonic()
        elif action.action == "type":
            if action.text is None:
                raise ValueError("Text is required for type actions")
            if stop is not None:
                stop.ensure_live()
            self._apply_focus_settle()
            self._last_key_dispatch = time.monotonic()  # B3 clock parity
            if (getattr(action, "via", None) or "sendinput") == "clipboard":
                # v0.7.1 (Defect C): fake parity for the paste transport — same save/set/
                # paste/restore discipline as the real ctypes path, including the
                # mid-transport abort restore (a pre-stopped token raises AFTER the set).
                self.executed.append(action)  # this branch returns before the shared append
                _mark_action_dispatched(action)
                return self._simulate_type_clipboard(action, stop)
            if not action.text or not TYPE_INTEGRITY_ENABLED:
                message = "Simulated type."
            else:
                total = len(action.text)
                landed_n = (
                    _normalize_landed_text(self.focused_control_value)
                    if self.focused_control_value is not None
                    else None
                )
                if landed_n is None:
                    # All-blind (no readable target — the simulated Blender shape):
                    # the typed v0.7.1 diagnostic rides the additive suffix chain.
                    message = (
                        "Simulated type. "
                        + _format_integrity_status(0, total, mismatch=0, unverified=total, healed=0)
                        + " branch=blind_read"
                        + f" {TYPE_UNCONFIRMED_MARKER} ({TYPE_UNCONFIRMED_HINT_SENDINPUT})"
                    )
                else:
                    expected_n = _normalize_landed_text(action.text)
                    # WP12c (VAL MINOR): the old tail-match heal
                    # (``_landed_prefix_len`` > 0 -> retype) reproduced the B1 false
                    # heal ('tett' + 'test' -> verified healed=3, field 'tettest').
                    # The fake now classifies by the COMMON PREFIX with the typed
                    # text only: a genuinely missing suffix (buffer == a proper
                    # typed prefix) heals; a diverged tail is a mismatch; a foreign
                    # buffer is unverified transformed_buffer. The fake has NO
                    # baseline (its documented model: the field starts empty), so
                    # the real ladder's tail_unproven / landed_not_at_tail /
                    # caret cases (which need a baseline) do not exist here.
                    if landed_n.endswith(expected_n):
                        message = "Simulated type. " + _format_integrity_status(
                            total, total, mismatch=0, unverified=0, healed=0
                        )
                    else:
                        k = 0
                        while (
                            k < min(len(landed_n), len(expected_n))
                            and landed_n[k] == expected_n[k]
                        ):
                            k += 1
                        if k == len(landed_n) and 0 < k < len(expected_n):
                            # Genuinely missing suffix: simulate the verified retype
                            # landing (repair = exactly the missing suffix; an agreed
                            # EMPTY buffer is unverified — never repaired).
                            missing = expected_n[k:]
                            self.focused_control_value = landed_n + missing
                            message = "Simulated type. " + _format_integrity_status(
                                total, total, mismatch=0, unverified=0, healed=len(missing)
                            )
                        elif k == 0:
                            if landed_n == "":
                                # Agreed empty buffer: unverified, never repaired (fix3);
                                # WP12b parity: branch=not_landed (nothing of ours landed).
                                message = "Simulated type. " + _format_integrity_status(
                                    0, total, mismatch=0, unverified=total, healed=0
                                ) + " branch=not_landed"
                            else:
                                # WP12b parity: a foreign/masked buffer shares nothing
                                # with the typed text — honest unverified (never a
                                # failure), branch=transformed_buffer.
                                message = "Simulated type. " + _format_integrity_status(
                                    0, total, mismatch=0, unverified=total, healed=0
                                ) + " branch=transformed_buffer"
                        else:
                            # WP12b B1(c): began correctly (k correct characters) and
                            # then DIVERGED — positive corruption evidence -> mismatch.
                            message = "Simulated type. " + _format_integrity_status(
                                0, total, mismatch=total, unverified=0, healed=0
                            )
            self.executed.append(action)  # this branch returns before the shared append
            _mark_action_dispatched(action)
            return message
        elif action.action == "drag":
            if action.point is None or action.to_point is None:
                raise ValueError("Both a start point and an end point are required for drag actions")
            start = self._map_to_physical(action.point.x, action.point.y)
            end = self._map_to_physical(action.to_point.x, action.to_point.y)
            if stop is not None:
                stop.ensure_live()  # before moveTo(start)
            self._cursor_physical = start
            if stop is not None:
                stop.ensure_live()  # before mouseDown
            self._drag_button_down = True
            # DELTA 1 MINOR 3 parity: the fake's drag press is tagged source="drag"
            # (never surfaces in last_click — click-type presses only).
            self.pointer_ring.record("down", start[0], start[1], "left", source="drag")
            _mark_action_dispatched(action)
            try:
                for segment_x, segment_y in _drag_segment_points(start, end):
                    if stop is not None:
                        stop.ensure_live()  # before every stroke segment
                    self._cursor_physical = (segment_x, segment_y)
                if stop is not None:
                    stop.ensure_live()  # before mouseUp
            finally:
                # Same release guarantee as the real backend: the button is never left
                # held, including on a mid-stroke TaskStopped.
                self._drag_button_down = False
                self.pointer_ring.record("up", end[0], end[1], "left", source="drag")
            self.drags.append((start, end))
        self.executed.append(action)
        _mark_action_dispatched(action)
        return f"Simulated {action.action}."

    def _simulate_held_keys(
        self, action: GroundedAction, keys: list[str], stop: StopToken | None
    ) -> None:
        """WP2 (ORVEX-2026-107): fake parity for a held keypress/hotkey.

        Mirrors the real ``_dispatch_held_keys`` discipline exactly — stop-checked
        key-downs, ``_mark_action_dispatched`` once the keys are down (so an
        interrupted hold is reported as dispatched-but-incomplete rather than
        never-sent), the shared sliced ``_sleep_for_hold`` wait, and a ``finally`` that
        ALWAYS clears the simulated keyboard. An aborted hold must not be recorded in
        ``executed``: the append happens after this returns, which is the fake's
        version of the real backend's "dispatched but not verified" split.
        """
        hold_seconds = action.hold_ms / 1000.0
        keys_down = 0
        try:
            for key in keys:
                if stop is not None:
                    stop.ensure_live()  # before every simulated key_down
                self.held_keys.append(key)
                keys_down += 1
            _mark_action_dispatched(action)
            _wait_for_hold(hold_seconds, stop)
            if stop is not None:
                stop.ensure_live()  # before the first simulated key_up
        finally:
            # The simulated keyboard is ALWAYS cleared, including on a mid-hold
            # TaskStopped; reverse press order, so this is also the release order.
            del self.held_keys[len(self.held_keys) - keys_down :]

    def _simulate_held_click(self, action: GroundedAction, button: str, stop: StopToken | None) -> None:
        """DELTA 1 (F2): fake parity for a held click/right_click.

        The held-click discipline of the real backend, in memory: the simulated button
        goes DOWN (recorded in ``held_buttons``), ``_mark_action_dispatched`` fires once
        the press is down, the wait is the SAME shared sliced ``_sleep_for_hold`` the
        real path uses, and the ``finally`` ALWAYS releases the button — including on a
        mid-hold ``TaskStopped`` (the stop is the kill path and must never leave input
        held). An aborted hold must not be recorded in ``executed``: the shared append
        happens after this returns, the fake's version of "dispatched but not
        verified".
        """
        hold_seconds = action.hold_ms / 1000.0
        pressed = False
        try:
            if stop is not None:
                stop.ensure_live()  # before the simulated press
            self.held_buttons.append(button)
            pressed = True
            point = self._cursor_physical or (0, 0)
            self.pointer_ring.record("down", point[0], point[1], button)
            _mark_action_dispatched(action)
            _wait_for_hold(hold_seconds, stop)
            if stop is not None:
                stop.ensure_live()  # before the simulated release
        finally:
            if pressed:
                self.held_buttons.remove(button)  # the release ALWAYS runs
                point = self._cursor_physical or (0, 0)
                self.pointer_ring.record("up", point[0], point[1], button)

    def _simulate_type_clipboard(self, action: GroundedAction, stop: StopToken | None) -> str:
        """Fake parity for ``via="clipboard"`` (v0.7.1 Defect C): save/set/paste/restore.

        Mirrors the real transport's discipline in memory: ``clipboard_value`` is saved,
        set to the action text, the paste payload is recorded in ``clipboard_pastes``,
        and the prior value is restored — including on a MID-TRANSPORT abort (a
        pre-stopped token raises AFTER the set, before the paste; the restore still
        runs). With a readable ``focused_control_value`` the paste APPENDS the text at
        the cursor and the single read-back confirms ``integrity=verified``; with
        ``None`` (the simulated Blender shape) the outcome is the honest
        ``integrity=unverified`` + ``TYPE_UNCONFIRMED no-readable-target`` diagnostic.

        D13 / repair 3.2 (WP4b) — THE DOUBLE'S BLIND SPOT IS CLOSED. This used to
        record the LOCAL ``text`` variable, which made it structurally incapable of
        detecting a restore ordered before the paste: the real transport's zero-width
        window shipped unchanged against 141 green typing tests. The recorded payload
        is now read off ``clipboard_value`` **at delivery time**, after the optional
        ``app_latency_s`` sleep that models a target which defers the clipboard read
        (exactly what ``SendInputEngine.chord`` does — it only QUEUES). The restore is
        skipped on the abort path, matching the real transport's abort-vs-dispatched
        split, and the dispatched path restores after the settle (nothing to wait for
        on an abort).
        """
        text = action.text or ""
        total = len(text)
        if not text:
            return "Simulated type."
        prior = self.clipboard_value
        aborted = True
        try:
            self.clipboard_value = text  # the set
            if stop is not None:
                stop.ensure_live()  # mid-transport abort point (mirrors the real pre-chord check)
            if self.clipboard_app_latency_s > 0:
                time.sleep(self.clipboard_app_latency_s)  # the target is still waking up
            # the ctrl+v chord: read the CLIPBOARD, not the local variable
            self.clipboard_pastes.append(self.clipboard_value or "")
            aborted = False
            if self.focused_control_value is not None:
                # A paste lands at the cursor: append, then the single read-back below.
                self.focused_control_value = self.focused_control_value + text
        finally:
            # Restore discipline (in-memory; never raises, never dispatches). As in
            # the real transport the abort path restores immediately and the
            # dispatched path restores after the settle — there is nothing to wait
            # for on an abort.
            if aborted or prior is not None:
                if self.clipboard_app_latency_s > 0 and not aborted:
                    time.sleep(TYPE_VERIFY_SETTLE_SECONDS)
                if prior is not None:
                    self.clipboard_value = prior
        if self.focused_control_value is None:
            return (
                "Simulated type. via=clipboard "
                + _format_integrity_status(0, total, mismatch=0, unverified=total, healed=0)
                + f" {TYPE_UNCONFIRMED_MARKER} ({TYPE_UNCONFIRMED_HINT_CLIPBOARD})"
            )
        return "Simulated type. via=clipboard " + _format_integrity_status(
            total, total, mismatch=0, unverified=0, healed=0
        )

    def _white_png(self) -> str:
        image = Image.new("RGB", (self.width, self.height), "white")
        output = io.BytesIO()
        image.save(output, format="PNG")
        return base64.b64encode(output.getvalue()).decode("ascii")

    def _build_default_context(self) -> _CaptureContext:
        monitor = _select_target_monitor(self.monitors, self._cursor_physical, None)
        verdict = classify_coordinate_space(self.width, self.height, monitor, dpi_estimated=False)
        return _CaptureContext(monitor=monitor, verdict=verdict)
