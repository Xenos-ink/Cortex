"""Interference Guard runtime: binding, pre-dispatch verification, sentinel checks (T8).

The runtime half of the five-mechanism design (A12
``evidence/perf-004/t8/interference-immunity-design.md`` section 4); the policy surface
and event vocabulary live in :mod:`computer_use_mcp.interference`. The guard is a pure
PROTECTION UPGRADE: it only ever ADDS rejections or annotations — it cannot execute,
bypass, or weaken any existing gate (grounding -> validation -> allowlists -> focus
gate -> safety -> approval -> dry-run -> stop-token -> limits -> verification all run
unchanged).

Mechanisms implemented here:

- **FocusGuard (i)** — the session binds a target window identity (hwnd/pid/class/
  title) from the first allowlisted observation or an explicit ``focus_window`` /
  ``ensure_app`` reattach. Before EVERY input dispatch the foreground is compared
  against the binding: MATCH (or owned dialog / transient launcher) dispatches;
  FOREIGN aborts with ``FOCUS_TAKEN_BY ...`` (policy ``refocus_then_abort`` first makes
  ONE verified reattach attempt via the existing foreground-switch primitive;
  ``observe_only`` only annotates). A foreign foreground can only ever produce a
  rejection — the guard cannot click, type, or focus the foreign window.
- **AttachOrLaunch (ii)** — realized by the backend's ``ensure_app`` (identity string
  ``process[|doc-token]``); the guard re-binds after a REATTACHED outcome.
- **DialogSentinel (iii)** — after every executed action a cheap class/owner/title
  probe reports ``MODAL_DIALOG ...`` (with the post-action observation's control list
  so the driver can decide WITHOUT a second round-trip). Queue policy ``halt`` stops
  the batch with ``follow_ups_stopped_reason="modal_dialog"``. ``auto_handle`` ships
  EMPTY: no automatic clicks of any kind.
- **FocusContinuity (iv)** — before/after keyboard dispatches the backend's
  ``query_focus_target`` must belong to the bound window (or its owned dialog);
  drift aborts with ``FOCUS_DRIFTED ...`` (or annotates under ``warn``). A dropped
  terminal key is REPORTED, never re-sent (``resend_terminal_key=False``).
- **HotkeyGuard (v)** — pre-chord ``GetAsyncKeyState`` sweep of the chord's modifiers
  plus ctrl/alt/shift/win; a stuck modifier aborts with ``STUCK_MODIFIER ...`` (or, in
  the opt-in ``release`` mode, is cleared ONLY when every stuck modifier is one this
  session dispatched, via synthetic key-ups, then re-checked).

Arming doctrine: the guard is DORMANT until the session binds a target — either the
first observation whose active window matches the configured ``allowed_processes``, or
a driver-executed ``focus_window``/``ensure_app`` (the explicit declaration of the
session's target). Sessions without an allowlist and without an explicit focus bind
keep the guard dormant, so legacy single-window automation is unaffected while
multi-window interference protection activates exactly when a target is declared.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .backend import ComputerBackend, WindowFocusError
from .interference import (
    FOCUS_DRIFTED,
    FOCUS_TAKEN_BY,
    MODAL_DIALOG,
    REATTACHED,
    REFOCUS_HINT,
    STUCK_MODIFIER,
    TARGET_GONE,
    InterferencePolicy,
    format_focus_drifted,
    format_focus_identity_unknown,
    format_focus_taken_by,
    format_modal_dialog,
    format_stuck_modifier,
    format_target_gone,
)
from .models import ActionType, FailureClass, GroundedAction, Observation, WindowInfo

logger = logging.getLogger(__name__)

__all__ = [
    "FOCUS_DRIFTED",
    "FOCUS_TAKEN_BY",
    "MODAL_DIALOG",
    "STUCK_MODIFIER",
    "TARGET_GONE",
    "GuardVerdict",
    "InterferenceGuard",
]

#: Actions exempt from the pre-dispatch foreground check: they dispatch no input into
#: whatever holds focus (wait), no input at all (done), or they ARE the reattachment
#: path and must stay reachable while focus is stolen (focus_window / ensure_app).
_GUARD_EXEMPT_ACTIONS: frozenset[ActionType] = frozenset(
    {ActionType.WAIT, ActionType.DONE, ActionType.FOCUS_WINDOW, ActionType.ENSURE_APP}
)

#: Keyboard actions the focus-continuity check gates (mechanism iv).
_KEYBOARD_ACTIONS: frozenset[ActionType] = frozenset(
    {ActionType.TYPE, ActionType.KEYPRESS, ActionType.HOTKEY}
)

#: Max ui_elements entries summarized into a MODAL_DIALOG payload (bounded).
_DIALOG_CONTROL_LIST_CAP = 8


@dataclass
class GuardVerdict:
    """One guard decision: a structured event plus how the controller must treat it.

    ``blocking=True`` verdicts become rejections (pre-dispatch) or not-ok results with
    a named queue stop (post-action); ``blocking=False`` verdicts are annotations only
    (``observe_only`` / ``warn`` policies).
    """

    event: str
    failure_class: FailureClass
    message: str
    blocking: bool = True
    stop_reason: str | None = None
    hints: list[str] = field(default_factory=list)


def _exe_basename(info: WindowInfo | None) -> str:
    """Process basename of a WindowInfo (exe path fallback), '' when unknown."""
    if info is None:
        return ""
    if info.process_name:
        return str(info.process_name).strip().casefold().removesuffix(".exe")
    if info.exe_path:
        return str(info.exe_path).replace("\\", "/").rsplit("/", 1)[-1].casefold().removesuffix(".exe")
    return ""


def _process_matches(candidate: str | None, needle: str) -> bool:
    """Case-insensitive, ``.exe``-tolerant process-name match."""
    if not candidate:
        return False
    return candidate.strip().casefold().removesuffix(".exe") == needle.strip().casefold().removesuffix(".exe")


def _titles_overlap(first: str, second: str) -> bool:
    """Case-insensitive containment overlap between two window titles (hwnd-recycle test)."""
    a = (first or "").strip().casefold()
    b = (second or "").strip().casefold()
    if not a or not b:
        return True  # no title evidence either way: identity falls to pid+class
    return a in b or b in a


def _describe_target(info: WindowInfo | None) -> str:
    """Short human description of a window identity for FOCUS_DRIFTED payloads."""
    if info is None:
        return "none"
    title = info.title or "<untitled>"
    process = info.process_name or "unknown"
    return f"{title!r} ({process})"


def _describe_focus_target(target: dict[str, object] | None) -> str:
    """Short description of a backend focus-target probe for FOCUS_DRIFTED payloads."""
    if not target:
        return "unavailable"
    text = str(target.get("text") or "")
    window_class = str(target.get("window_class") or "?")
    root = target.get("root_hwnd")
    label = text[:60] if text else f"class={window_class}"
    return f"{label!r} (root hwnd={root})"


class InterferenceGuard:
    """The five-mechanism guard facade wired between the controller's existing gates."""

    def __init__(
        self,
        backend: ComputerBackend,
        policy: InterferencePolicy | None = None,
        *,
        emit: Callable[..., None] | None = None,
    ) -> None:
        from .interference import parse_interference

        self.backend = backend
        self.policy = policy if policy is not None else parse_interference(None)
        self._emit = emit  # audit sink (agent._audit-shaped); failures never break control
        self.bound: WindowInfo | None = None
        self._session_chord_keys: list[list[str]] = []
        self._refocus_attempted_for: set[str] = set()

    # ------------------------------------------------------------------ audit helper

    def _audit(self, result: str, payload: str, *, action: GroundedAction | None = None) -> None:
        if self._emit is None:
            return
        try:
            self._emit(
                "interference",
                action=action,
                result=result,
                metadata={"event": payload.split(" ", 1)[0], "payload": payload[:400]},
            )
        except Exception:
            logger.debug("guard audit write failed", exc_info=True)

    # ------------------------------------------------------------------ binding (mechanism i)

    @property
    def armed(self) -> bool:
        return self.bound is not None

    def maybe_bind(self, observation: Observation | None, allowed_processes: Sequence[str] | None = None) -> None:
        """Bind the session target from the first ALLOWLISTED observation (A12 arming doctrine).

        Binding happens when the observation carries a window identity AND its process
        matches the configured ``allowed_processes``. Sessions WITHOUT an allowlist stay
        dormant until the driver explicitly focuses a target (``focus_window`` /
        ``ensure_app`` reattach) — the guard must never bind to whatever window happens
        to hold focus at session start (that could be the user's own console).
        """
        if self.bound is not None or observation is None:
            return
        processes = [str(pattern) for pattern in (allowed_processes or []) if str(pattern).strip()]
        if not processes:
            return  # no allowlist: dormant until an explicit focus bind
        info = observation.active_window_info
        if info is None or (not info.process_name and not info.exe_path and not info.hwnd):
            return
        # B10 (c): an untitled/anonymous shell surface (desktop WorkerW, tasklist
        # thumbnails, ...) must never become the anchor — it would FOCUS_DRIFTED-abort
        # every later typed surface. Anchoring requires a real, titled window.
        if not (info.title or "").strip():
            return
        matched = any(
            _process_matches(info.process_name, pattern)
            or (_exe_basename(info) == str(pattern).strip().casefold().removesuffix(".exe"))
            for pattern in processes
        )
        if not matched:
            return
        self.rebind(info)

    def rebind(self, window: WindowInfo | None) -> None:
        """(Re-)bind the session target to ``window`` (focus_window / ensure_app reattach)."""
        if window is None:
            return
        self.bound = window.model_copy() if hasattr(window, "model_copy") else window
        self._refocus_attempted_for.clear()
        self._audit("bound", f"BOUND target={_describe_target(self.bound)}")

    def rebind_from_observation(self, observation: Observation | None) -> None:
        """Re-bind from a post-action observation (the focused window IS the new target)."""
        if observation is None:
            return
        info = observation.active_window_info
        if info is not None and (info.hwnd or info.process_name or info.title):
            self.rebind(info)

    def reanchor_after_success(self, new_window: WindowInfo | None) -> None:
        """B10 (b): follow the session's OWN verified transitions to a new surface.

        Called by the controller ONLY after a VERIFIED successful action. Rules:

        - same-process surface (the app's own dialog, the shell's Run dialog) ->
          re-anchor (the transitive same-process launcher doctrine);
        - the anchor is a launcher/dialog surface (``#32770`` or a configured transient
          launcher process) and a new app took over -> re-anchor (the launch WE caused);
        - the anchor is GONE -> re-anchor (dead identities must not pin the session);
        - anything else (a foreign window took the foreground without one of our
          verified actions causing it) -> KEEP the anchor, so the next pre-dispatch
          rejects with FOCUS_TAKEN_BY — the steal protection is untouched.

        Anchoring still requires a titled window (B10 (c)).
        """
        bound = self.bound
        if new_window is None or bound is None:
            return
        if not (new_window.title or "").strip():
            return  # never anchor to an anonymous surface
        if new_window.hwnd is not None and bound.hwnd is not None and new_window.hwnd == bound.hwnd:
            return  # unchanged surface
        same_process = (
            new_window.pid is not None
            and bound.pid is not None
            and new_window.pid == bound.pid
        )
        anchor_is_launcher = (
            (bound.window_class or "") == "#32770"
            or any(
                _exe_basename(bound)
                == str(pattern).strip().casefold().removesuffix(".exe")
                for pattern in self.policy.focus_guard.transient_launch_processes
            )
        )
        anchor_gone = False
        if bound.hwnd is not None:
            try:
                anchor_gone = not self.backend.is_window_alive(bound.hwnd)
            except Exception:  # noqa: BLE001 - a broken probe keeps the anchor
                anchor_gone = False
        if same_process or anchor_is_launcher or anchor_gone:
            self.rebind(new_window)

    # ------------------------------------------------------------------ B10 focus-surface rules

    def _focus_surface_is_ours(self, target: dict[str, object]) -> bool:
        """B10 (a): same-process / dialog-hosted / launcher-hosted focus is NOT drift.

        The keyboard focus lives INSIDE a control; the surface that receives it is the
        focus ROOT window. Typing into the shell's Run dialog, the app's own dialog, or
        a configured transient-launcher surface is deliberate driver work even when the
        root hwnd differs from the anchor.
        """
        bound = self.bound
        if bound is None:  # pragma: no cover - armed callers only
            return True
        root = target.get("root_hwnd")
        if root is not None and bound.hwnd is not None and int(root) == int(bound.hwnd):
            return True
        pid = target.get("pid")
        if pid is not None and bound.pid is not None and int(pid) == int(bound.pid):
            return True  # same-process surface (transitive owned/launcher windows)
        if self.policy.focus_guard.allow_owned_dialogs and str(
            target.get("root_window_class") or ""
        ) == "#32770":
            return True  # a system dialog hosts the focus (e.g. the Run dialog)
        process = str(target.get("process_name") or "")
        return any(
            process.strip().casefold().removesuffix(".exe")
            == str(pattern).strip().casefold().removesuffix(".exe")
            for pattern in self.policy.focus_guard.transient_launch_processes
        )

    # ------------------------------------------------------------------ chord log (mechanism v)

    def note_chord(self, keys: Sequence[str]) -> None:
        """Record a chord this session dispatched (the ``release`` mode's ownership log)."""
        self._session_chord_keys.append([str(key).strip().casefold() for key in keys])
        if len(self._session_chord_keys) > 64:  # bounded memory
            self._session_chord_keys.pop(0)

    def _session_modifiers(self) -> set[str]:
        ours: set[str] = set()
        for chord in self._session_chord_keys:
            ours.update(key for key in chord if key in {"ctrl", "alt", "shift", "win"})
        return ours

    # ------------------------------------------------------------------ matching helpers

    def _is_owned_dialog(self, foreground: WindowInfo, bound: WindowInfo) -> bool:
        """B11 (class-AGNOSTIC): is ``foreground`` a dialog owned by the session target?

        A window is OURS when it belongs to the SAME PROCESS as the bound window (the
        app's own modals: Excel's `bosa_sdm_XL9`, Word's `bosa_sdm_msword`, any custom
        class) or its GW_OWNER chain roots at the bound window (cross-process owned
        dialogs) — via the ONE shared backend ownership probe the sentinel uses, so
        guard and sentinel can never diverge again. Safety: the process allowlist and
        the foreign-window rejection are untouched — a foreign app's window (different
        process, not owner-chained) is still a FOCUS_TAKEN_BY rejection, and the
        `allow_owned_dialogs` policy still gates the whole exemption.
        """
        if not self.policy.focus_guard.allow_owned_dialogs:
            return False
        if foreground.pid is not None and bound.pid is not None and foreground.pid == bound.pid:
            return True  # same-process surface, ANY window class (class-agnostic)
        if foreground.hwnd is not None and bound.hwnd is not None:
            try:
                return bool(
                    self.backend.is_window_owned_by(int(foreground.hwnd), int(bound.hwnd))
                )
            except Exception:  # noqa: BLE001 - a broken probe keeps rejection semantics
                return False
        return False

    def _is_transient_launcher(self, foreground: WindowInfo) -> bool:
        """True when the foreground process is a configured transient launcher."""
        base = _exe_basename(foreground)
        return any(
            base == str(pattern).strip().casefold().removesuffix(".exe")
            for pattern in self.policy.focus_guard.transient_launch_processes
        )

    def _matches_binding(self, foreground: WindowInfo) -> bool:
        """MATCH or MATCH-ADJACENT per A12 mechanism (i) — dispatch may proceed."""
        bound = self.bound
        if bound is None:  # pragma: no cover - callers keep the dormant guard exempt
            return True
        if foreground.hwnd is not None and bound.hwnd is not None and foreground.hwnd == bound.hwnd:
            return True
        # hwnd recycle: same pid + class + overlapping title (conservative recovery).
        if (
            foreground.pid is not None
            and bound.pid is not None
            and foreground.pid == bound.pid
            and (foreground.window_class or "") == (bound.window_class or "")
            and _titles_overlap(foreground.title, bound.title)
        ):
            return True
        if self._is_owned_dialog(foreground, bound):
            return True
        return self._is_transient_launcher(foreground)

    # ------------------------------------------------------------------ mechanism (i): pre-dispatch

    def verify_pre_dispatch(self, action: GroundedAction) -> GuardVerdict | None:
        """Pre-dispatch gate: focus binding, stuck modifiers, keyboard-focus continuity.

        Returns ``None`` (proceed), a blocking verdict (reject), or a non-blocking
        verdict (annotate and proceed). Precedence per A12: FOCUS_TAKEN_BY first (a
        hotkey against a stolen foreground is a MISDELIVERY, not a no-op), then
        STUCK_MODIFIER, then FOCUS_DRIFTED.
        """
        if action.action in _GUARD_EXEMPT_ACTIONS:
            return self._verify_stuck_modifiers(action)  # hotkey hygiene applies to chords only
        guard_policy = self.policy.focus_guard
        if guard_policy.enabled and self.armed:
            verdict = self._verify_foreground(action)
            if verdict is not None:
                return verdict
        if self.policy.hotkey_guard.enabled and action.action in {ActionType.KEYPRESS, ActionType.HOTKEY}:
            verdict = self._verify_stuck_modifiers(action)
            if verdict is not None:
                return verdict
        if (
            self.policy.focus_continuity.enabled
            and self.armed
            and action.action in _KEYBOARD_ACTIONS
        ):
            verdict = self._verify_focus_continuity(action)
            if verdict is not None:
                return verdict
        return None

    def _verify_foreground(self, action: GroundedAction) -> GuardVerdict | None:
        """The FOCUS_TAKEN_BY / refocus / observe-only ladder (mechanism i)."""
        policy = self.policy.focus_guard
        try:
            foreground = self.backend.query_foreground_window()
        except Exception:  # noqa: BLE001 - a broken probe is a fail-closed identity loss
            foreground = None
        if foreground is None:
            payload = format_focus_identity_unknown()
            self._audit("focus_identity_unknown", payload, action=action)
            return GuardVerdict(
                event=payload,
                failure_class=FailureClass.WRONG_WINDOW,
                message=(
                    "Focus interference: the foreground window identity is unavailable; "
                    "refusing to dispatch (fail-closed)."
                ),
                blocking=policy.policy != "observe_only",
                hints=[REFOCUS_HINT],
            )
        if self._matches_binding(foreground):
            return None
        # B6: the bound identity may be GONE (window closed). Rejecting forever against
        # a dead identity deadlocks the session; with the default policy the guard
        # reports TARGET_GONE and unbinds so re-grounding / reattach can proceed.
        try:
            alive = self.backend.is_window_alive(self.bound.hwnd) if self.bound.hwnd else True
        except Exception:  # noqa: BLE001 - a broken probe keeps rejection semantics
            alive = True
        if not alive:
            gone = format_target_gone(self.bound.title or "", self.bound.hwnd)
            unbind = self.policy.focus_guard.on_target_gone == "unbind_and_report"
            if unbind:
                self.bound = None  # unbind: the guard returns to the dormant arming state
            self._audit("target_gone", gone, action=action)
            return GuardVerdict(
                event=gone,
                failure_class=FailureClass.WRONG_WINDOW,
                message=(
                    "The session target window is gone"
                    + ("; the binding was cleared. " if unbind else ". ")
                    + "Re-ground, then reattach by identity (ensure_app / focus_window)."
                ),
                blocking=True,
                hints=["hint: never relaunch blindly; ensure_app before any launch"],
            )
        payload = format_focus_taken_by(foreground)
        if policy.policy == "observe_only":
            self._audit("focus_taken_by_observed", payload, action=action)
            return GuardVerdict(
                event=payload,
                failure_class=FailureClass.WRONG_WINDOW,
                message=f"Focus interference observed (observe_only): {payload}",
                blocking=False,
                hints=[REFOCUS_HINT],
            )
        if policy.policy == "refocus_then_abort":
            refocused = self._attempt_refocus(action, foreground)
            if refocused is None:
                return None  # reattach verified: dispatch may proceed
            self._audit("focus_taken_by", f"{payload} {refocused}", action=action)
            return GuardVerdict(
                event=payload,
                failure_class=FailureClass.WRONG_WINDOW,
                message=(
                    "Focus interference: the OS-focused window is not the session target; "
                    f"reattach failed. {refocused}"
                ),
                blocking=True,
                hints=[REFOCUS_HINT, refocused],
            )
        self._audit("focus_taken_by", payload, action=action)
        return GuardVerdict(
            event=payload,
            failure_class=FailureClass.WRONG_WINDOW,
            message="Focus interference: the OS-focused window is not the session target.",
            blocking=True,
            hints=[REFOCUS_HINT],
        )

    def _attempt_refocus(self, action: GroundedAction, foreground: WindowInfo) -> str | None:
        """ONE verified reattach attempt to the bound target; None on verified success.

        Never touches the foreign window: the existing foreground-switch primitive is
        aimed at the BOUND title only and fails closed with ``WindowFocusError``. At
        most one attempt per action instance (bounded retries of the same instance hit
        the ``_refocus_attempted_for`` guard).
        """
        bound = self.bound
        if bound is None or not (bound.title or "").strip():
            return "no bound window title to reattach to"
        key = f"{action.action_id}:{bound.hwnd}"
        if key in self._refocus_attempted_for:
            return "reattach already attempted for this action; refusing to retry"
        self._refocus_attempted_for.add(key)
        try:
            self.backend.focus_window_title(bound.title)
        except WindowFocusError as exc:
            return f"refocus error: {exc}"
        except Exception as exc:  # noqa: BLE001 - any refocus failure is the safe abort
            return f"refocus error: {type(exc).__name__}: {exc}"
        try:
            after = self.backend.query_foreground_window()
        except Exception:  # noqa: BLE001 - a broken re-check is a failed reattach
            after = None
        if after is not None and self._matches_binding(after):
            return None
        return f"refocus verified failed: foreground is {_describe_target(after)}"

    # ------------------------------------------------------------------ mechanism (v): hotkeys

    def _verify_stuck_modifiers(self, action: GroundedAction) -> GuardVerdict | None:
        """Pre-chord stuck-modifier sweep (mechanism v): abort by default, release opt-in."""
        if action.action not in {ActionType.KEYPRESS, ActionType.HOTKEY} or not action.keys:
            return None
        try:
            stuck = self.backend.query_stuck_modifiers(list(action.keys))
        except Exception:  # noqa: BLE001 - a broken sweep must not block dispatch
            return None
        if not stuck:
            return None
        payload = format_stuck_modifier(stuck)
        if self.policy.hotkey_guard.on_stuck_modifier == "release":
            ours = self._session_modifiers()
            if all(name in ours for name in stuck):
                try:
                    self.backend.release_modifiers(stuck)
                except Exception:  # noqa: BLE001 - failed release degrades to abort
                    self._audit("stuck_modifier", payload, action=action)
                    return self._stuck_abort_verdict(stuck, action, "release failed")
                try:
                    still_stuck = self.backend.query_stuck_modifiers(list(action.keys))
                except Exception:  # noqa: BLE001
                    still_stuck = stuck
                if not still_stuck:
                    self._audit("stuck_modifier_released", payload, action=action)
                    return None
                payload = format_stuck_modifier(still_stuck)
            # Not our modifiers (or release failed): abort — NEVER release user keys.
        return self._stuck_abort_verdict(stuck, action)

    def _stuck_abort_verdict(
        self, stuck: Sequence[str], action: GroundedAction, detail: str | None = None
    ) -> GuardVerdict:
        payload = format_stuck_modifier(stuck)
        message = (
            "HotkeyGuard: modifiers are stuck down; refusing to dispatch the chord "
            "(a held ctrl rewrites the chord's meaning)"
        )
        if detail:
            message = f"{message} ({detail})"
        self._audit("stuck_modifier", payload, action=action)
        return GuardVerdict(
            event=payload,
            failure_class=FailureClass.UNKNOWN,
            message=message,
            blocking=True,
            hints=["hint: clear the held modifiers or configure hotkey_guard release mode"],
        )

    # ------------------------------------------------------------------ mechanism (iv): continuity

    def _verify_focus_continuity(self, action: GroundedAction) -> GuardVerdict | None:
        """Keyboard focus must belong to the bound window (or its owned dialog)."""
        try:
            target = self.backend.query_focus_target()
        except Exception:  # noqa: BLE001 - a broken probe leaves the check inert
            return None
        if not target:
            return None  # cannot verify -> check inert (the foreground gate still applies)
        bound = self.bound
        if bound is None:  # pragma: no cover - armed callers only
            return None
        if self._focus_surface_is_ours(target):
            return None  # B10 (a): same-process / dialog-hosted / launcher surface
        payload = format_focus_drifted(
            _describe_target(bound), _describe_focus_target(target)
        )
        self._audit("focus_drifted", payload, action=action)
        if self.policy.focus_continuity.on_drift == "warn":
            return GuardVerdict(
                event=payload,
                failure_class=FailureClass.WRONG_WINDOW,
                message=f"Focus drift observed (warn): {payload}",
                blocking=False,
            )
        return GuardVerdict(
            event=payload,
            failure_class=FailureClass.WRONG_WINDOW,
            message=(
                "Focus continuity: the keyboard focus is not the session target; refusing "
                "to dispatch keyboard input (text would land in a foreign field)."
            ),
            blocking=True,
            hints=[REFOCUS_HINT],
        )

    def verify_mid_type(self, action: GroundedAction) -> None:
        """Per-chunk hook for ``type`` actions: raise FocusDriftError on abort-policy drift.

        Slotted into the backend's per-chunk cadence (after the stop-token hook) so a
        focus steal MID-STRING stops the remaining chunks (the save-path-into-the-wrong-
        field moment). ``warn`` policy annotates without aborting; a broken probe is
        inert. Only runs when the guard is armed and continuity checking is enabled.
        """
        if not (self.policy.focus_continuity.enabled and self.armed):
            return
        if self.policy.focus_continuity.on_drift != "abort":
            return
        try:
            target = self.backend.query_focus_target()
        except Exception:  # noqa: BLE001 - a broken probe must not abort typing
            return
        if not target:
            return
        bound = self.bound
        if bound is None:  # pragma: no cover
            return
        if self._focus_surface_is_ours(target):
            return  # B10 (a): same-process / dialog-hosted / launcher surface
        payload = format_focus_drifted(_describe_target(bound), _describe_focus_target(target))
        self._audit("focus_drifted_mid_type", payload, action=action)
        from .backend import FocusDriftError

        raise FocusDriftError(payload)

    # ------------------------------------------------------------------ mechanism (iii): sentinel

    def post_action_events(self, action: GroundedAction, after: Observation | None) -> list[GuardVerdict]:
        """Post-action checks: modal-dialog sentinel + post-keyboard focus continuity."""
        verdicts: list[GuardVerdict] = []
        if self.policy.dialog_sentinel.enabled and after is not None:
            verdict = self._sentinel_check(action, after)
            if verdict is not None:
                verdicts.append(verdict)
        if (
            self.policy.focus_continuity.enabled
            and self.armed
            and action.action is ActionType.TYPE
        ):
            verdict = self._post_keyboard_check(action)
            if verdict is not None:
                verdicts.append(verdict)
        return verdicts

    def _sentinel_check(self, action: GroundedAction, after: Observation) -> GuardVerdict | None:
        """MODAL_DIALOG detection (cheap probes only; UIA only on the payload path)."""
        bound_hwnd = self.bound.hwnd if self.bound is not None else None
        try:
            dialog = self.backend.detect_system_dialog(
                bound_hwnd, self.policy.dialog_sentinel.title_table
            )
        except Exception:  # noqa: BLE001 - a broken probe never breaks the pipeline
            return None
        if not dialog:
            return None
        payload = format_modal_dialog(dialog)
        controls = _summarize_dialog_controls(after)
        if controls:
            payload = f"{payload} controls=[{'; '.join(controls)}]"
        halt = self.policy.dialog_sentinel.policy == "halt"
        self._audit("modal_dialog", payload, action=action)
        return GuardVerdict(
            event=payload,
            failure_class=FailureClass.UNEXPECTED_DIALOG,
            message=(
                "A modal dialog took the foreground after the action; handle it "
                "deliberately from the listed controls or halt."
                if halt
                else "A modal dialog took the foreground after the action (report-only)."
            ),
            blocking=False,  # the action already executed; halting is the queue's job
            stop_reason="modal_dialog" if halt else None,
            hints=[
                (
                    "hint: decide from the control list — the task's own dialog: handle "
                    "it; unexpected: halt and report; never blind-OK a dialog"
                )
            ],
        )

    def _post_keyboard_check(self, action: GroundedAction) -> GuardVerdict | None:
        """Post-type focus verification (mechanism iv). Never re-sends terminal keys."""
        try:
            target = self.backend.query_focus_target()
        except Exception:  # noqa: BLE001 - a broken probe never breaks the pipeline
            return None
        if not target or self.bound is None:
            return None
        bound = self.bound
        if self._focus_surface_is_ours(target):
            return None  # B10 (a): same-process / dialog-hosted / launcher surface
        payload = format_focus_drifted(_describe_target(bound), _describe_focus_target(target))
        self._audit("focus_drifted_post", payload, action=action)
        blocking = self.policy.focus_continuity.on_drift == "abort"
        return GuardVerdict(
            event=payload,
            failure_class=FailureClass.WRONG_WINDOW,
            message=(
                "Focus drifted after the keyboard dispatch; the typed content may have "
                "landed in a foreign field. Re-observe, re-click the intended control, "
                "then re-type (never retype blindly; terminal keys are never auto-resent)."
                if blocking
                else f"Focus drift observed (warn): {payload}"
            ),
            blocking=blocking,
            stop_reason="focus_drifted" if blocking else None,
            hints=[REFOCUS_HINT],
        )

    # ------------------------------------------------------------------ attach-or-launch binding

    def note_ensure_app_outcome(self, message: str) -> None:
        """Re-bind after a REATTACHED ensure_app outcome (mechanism ii's identity bind)."""
        if message.startswith(REATTACHED):
            # REATTACHED title='...' hwnd=... — bind via the reattach's own title lookup
            # (the post-action observation re-binds when it carries the identity).
            start = message.find("title='")
            if start >= 0:
                end = message.find("'", start + len("title='"))
                if end > start:
                    title = message[start + len("title='") : end]
                    try:
                        window = self.backend.find_window_by_title(title)
                    except Exception:  # noqa: BLE001 - a lookup failure skips the rebind
                        window = None
                    if window is not None:
                        self.rebind(window)


def _summarize_dialog_controls(after: Observation) -> list[str]:
    """Bounded control list from the post-action observation's ``ui_elements``."""
    controls: list[str] = []
    elements = after.ui_elements or []
    for element in elements:
        if len(controls) >= _DIALOG_CONTROL_LIST_CAP:
            break
        if isinstance(element, dict):
            name = element.get("name") or element.get("value")
            control_type = element.get("control_type") or element.get("type") or "control"
            if isinstance(name, str) and name.strip():
                controls.append(f"{control_type} {name.strip()[:60]!r}")
        elif isinstance(element, str) and element.strip():
            controls.append(element.strip()[:60])
    return controls
