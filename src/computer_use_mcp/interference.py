"""Interference-immunity policy surface and event vocabulary (T8, A12 design section 4).

This module is the pure-policy half of the Interference Guard: the
:class:`InterferencePolicy` config model accepted by ``start_session(interference=...)``
(trailing-optional, fail-closed parsing exactly like ``limits``) and the single-string
event payloads the driver parses from ``reasons`` / verification notes / ``text_summary``.
The runtime half (binding, pre-dispatch verification, sentinel checks) lives in
:mod:`computer_use_mcp.focus_guard`; this module imports only ``models`` (layering rule).

Design contract (A12 ``evidence/perf-004/t8/interference-immunity-design.md``):

- Five mechanisms, one policy surface, every default protective:

  =====================  =========================================================
  focus_guard            session target-window binding + pre-dispatch foreground
                         check (``abort`` | ``refocus_then_abort`` | ``observe_only``)
  attach_or_launch       pre-launch reattachment doctrine (``ensure_app``); the server
                         may launch an allowlisted target by default
                         (``launch="server"``; ``"driver"`` never launches)
  dialog_sentinel        modal-dialog interception post-action (``halt`` |
                         ``report``); ``auto_handle`` ships EMPTY — no auto-clicks
  focus_continuity       typing focus continuity (``on_drift`` abort|warn);
                         ``resend_terminal_key`` default False (never re-sends)
  hotkey_guard           pre-chord stuck-modifier sweep (``abort`` | ``release``)
  =====================  =========================================================

- Unknown policy fields/values are REJECTED (fail-closed, mirroring
  ``server._parse_limits``) so a typo can never silently disable protection.
- Event vocabulary (single parseable strings): ``FOCUS_TAKEN_BY``, ``FOCUS_DRIFTED``,
  ``MODAL_DIALOG``, ``REATTACHED``, ``AMBIGUOUS_INSTANCE``, ``NO_INSTANCE``,
  ``STUCK_MODIFIER`` — plus the additive ``FOCUS_IDENTITY_UNKNOWN`` (fail-closed
  foreground-identity loss; A12's ``on_identity_unknown: abort`` payload).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "ATTACH_OR_LAUNCH_ENV",
    "DEFAULT_DIALOG_TITLE_TABLE",
    "DEFAULT_TRANSIENT_LAUNCH_PROCESSES",
    "REFOCUS_HINT",
    "AttachOrLaunchPolicy",
    "DialogSentinelPolicy",
    "FocusContinuityPolicy",
    "FocusGuardPolicy",
    "HotkeyGuardPolicy",
    "InterferencePolicy",
    "format_ambiguous_instance",
    "format_focus_drifted",
    "format_focus_identity_unknown",
    "format_focus_taken_by",
    "format_modal_dialog",
    "format_no_instance",
    "format_reattached",
    "format_stuck_modifier",
    "format_target_gone",
    "parse_interference",
]

#: Event names (the driver-facing vocabulary; kept as constants, never magic strings).
FOCUS_TAKEN_BY = "FOCUS_TAKEN_BY"
FOCUS_DRIFTED = "FOCUS_DRIFTED"
MODAL_DIALOG = "MODAL_DIALOG"
REATTACHED = "REATTACHED"
AMBIGUOUS_INSTANCE = "AMBIGUOUS_INSTANCE"
NO_INSTANCE = "NO_INSTANCE"
STUCK_MODIFIER = "STUCK_MODIFIER"
FOCUS_IDENTITY_UNKNOWN = "FOCUS_IDENTITY_UNKNOWN"
TARGET_GONE = "TARGET_GONE"

#: Fail-closed hint appended to focus-interference rejections (weak-driver guidance).
REFOCUS_HINT = (
    "hint: re_ground_or_refocus — re-observe, then reattach to the session target by "
    "identity (focus_window / ensure_app); never act on the foreign window, never "
    "launch a replacement instance"
)

#: A12 default transient-launcher processes: Run/file dialogs the task legitimately
#: launches through (bounded list; hosts may override via config).
DEFAULT_TRANSIENT_LAUNCH_PROCESSES = ["explorer.exe"]

#: Generic dialog-title conventions (casefold substrings; configurable per session).
DEFAULT_DIALOG_TITLE_TABLE = [
    "save as",
    "confirm save as",
    "save changes",
    "open",
    "error",
    "warning",
    "properties",
]


class FocusGuardPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    """Mechanism (i): session target-window binding + pre-dispatch foreground check."""

    enabled: bool = True
    policy: Literal["abort", "refocus_then_abort", "observe_only"] = "abort"
    allow_owned_dialogs: bool = True
    transient_launch_processes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_TRANSIENT_LAUNCH_PROCESSES)
    )
    on_identity_unknown: Literal["abort"] = "abort"
    # B6 (T8): the bound window was closed. "unbind_and_report" (default) reports
    # TARGET_GONE and clears the binding so re-grounding/reattach can proceed;
    # "keep_binding" keeps rejecting against the dead identity (host choice).
    on_target_gone: Literal["unbind_and_report", "keep_binding"] = "unbind_and_report"

    @field_validator("transient_launch_processes")
    @classmethod
    def _strip_processes(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if value and not cleaned:
            raise ValueError("transient_launch_processes must contain non-empty strings")
        return cleaned


#: REM-B env knob: restore the pre-REM-B default of never launching server-side.
#: Only the value ``driver`` (case-insensitive, whitespace-trimmed) selects the old
#: behavior; any other value (unset, empty, bogus) fails safe to the new ``server``
#: default. Read at POLICY-PARSE time (``parse_interference``/model validation), so
#: runtime env changes take effect for the NEXT session without a process restart.
ATTACH_OR_LAUNCH_ENV = "CORTEX_ATTACH_OR_LAUNCH"


def _default_launch_policy() -> str:
    """Resolve the default ``attach_or_launch.launch`` value from the environment.

    Fail-safe (H2a): only the value ``driver`` (case-insensitive, whitespace-trimmed)
    restores the old default; anything else — unset, empty, whitespace,
    unrecognized — yields ``server`` so a typo can never silently disable Cortex's
    ability to take over.
    """
    import os

    raw = os.getenv(ATTACH_OR_LAUNCH_ENV)
    if raw is not None and raw.strip().casefold() == "driver":
        return "driver"
    return "server"


class AttachOrLaunchPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    """Mechanism (ii): pre-launch reattachment (``ensure_app``) doctrine.

    REM-B (H2a takeover): the ``launch`` DEFAULT is ``"server"`` — ``ensure_app``
    may spawn the target process server-side when no existing instance matches.
    Cortex can then bootstrap control of an app that is not already foreground
    (the logged failure mode: the driving model had to use another tool to launch
    Paint, which failed twice). Every other safety property is unchanged: the
    agent gate still requires the TARGET process to be allowlisted whenever an
    allowlist is configured, safety/approval semantics still apply to the launch
    action, and StopToken/limits are untouched. ``launch="driver"`` keeps the
    pre-REM-B never-launch behavior and is also selectable as the DEFAULT via the
    ``CORTEX_ATTACH_OR_LAUNCH=driver`` environment knob (fail-safe parsing:
    unrecognized values fall back to the ``"server"`` default). An omitted
    ``launch`` resolves from the environment AT VALIDATION TIME (not import time),
    so runtime env changes apply to the next parsed policy.
    """

    enabled: bool = True
    #: ``None`` means "not stated by the host" — resolved from the env knob by the
    #: model validator below; the stored value is always concrete (never None).
    launch: Literal["driver", "server"] | None = None

    @model_validator(mode="after")
    def _resolve_launch_default(self) -> "AttachOrLaunchPolicy":
        """Omitted/unstated ``launch`` resolves from the env at validation time.

        Only ``CORTEX_ATTACH_OR_LAUNCH=driver`` restores the pre-REM-B never-launch
        default; anything else fails safe to ``server`` (REM-B H2a takeover fix).
        """
        if self.launch is None:
            object.__setattr__(self, "launch", _default_launch_policy())
        return self


class AutoHandleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    """One host-authorized dialog auto-resolution (OFF by default; audited per use).

    ``title`` and/or ``window_class`` identify the dialog (both must match when both
    are given); ``button`` is the control name of the designated resolution button.
    """

    title: str | None = Field(default=None, min_length=1, max_length=200)
    window_class: str | None = Field(default=None, min_length=1, max_length=200)
    button: str = Field(min_length=1, max_length=200)

    @field_validator("title", "window_class", "button")
    @classmethod
    def _non_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("auto_handle fields must not be blank")
        return value

    @field_validator("button")
    @classmethod
    def _button_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("auto_handle requires a designated button control name")
        return value


class DialogSentinelPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    """Mechanism (iii): modal-dialog interception after every executed action."""

    enabled: bool = True
    policy: Literal["halt", "report"] = "halt"
    auto_handle: list[AutoHandleSpec] = Field(default_factory=list)  # EMPTY: no auto-clicks
    title_table: list[str] = Field(default_factory=lambda: list(DEFAULT_DIALOG_TITLE_TABLE))

    @field_validator("title_table")
    @classmethod
    def _strip_titles(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip().casefold() for item in value if isinstance(item, str) and item.strip()]
        if value and not cleaned:
            raise ValueError("title_table must contain non-empty strings")
        return cleaned


class FocusContinuityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    """Mechanism (iv): typing focus continuity (pre/post keyboard-dispatch focus check)."""

    enabled: bool = True
    on_drift: Literal["abort", "warn"] = "abort"
    resend_terminal_key: bool = False  # a dropped Enter is REPORTED, never auto-resent


class HotkeyGuardPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    """Mechanism (v): hotkey dispatch hardening (pre-chord stuck-modifier sweep)."""

    enabled: bool = True
    on_stuck_modifier: Literal["abort", "release"] = "abort"


class InterferencePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    """The whole Interference Guard policy (``start_session(interference=...)``).

    Every field is optional with the protective default shown here; parsing is
    fail-closed (unknown keys/values raise) exactly like ``limits``.
    """

    focus_guard: FocusGuardPolicy = Field(default_factory=FocusGuardPolicy)
    attach_or_launch: AttachOrLaunchPolicy = Field(default_factory=AttachOrLaunchPolicy)
    dialog_sentinel: DialogSentinelPolicy = Field(default_factory=DialogSentinelPolicy)
    focus_continuity: FocusContinuityPolicy = Field(default_factory=FocusContinuityPolicy)
    hotkey_guard: HotkeyGuardPolicy = Field(default_factory=HotkeyGuardPolicy)


def parse_interference(raw: dict[str, Any] | None) -> InterferencePolicy:
    """Parse a client-supplied interference policy dict, fail-closed.

    ``None``/empty yields the A12 protective defaults. Non-mapping input raises
    ``TypeError``; unknown keys (any level) raise ``ValueError``; wrong value types or
    unknown enum values raise ``TypeError``/``ValueError`` via pydantic — so a malformed
    policy can never silently weaken protection.
    """
    if raw is None:
        return InterferencePolicy()
    if not isinstance(raw, dict):
        raise TypeError(
            f"interference must be a mapping of policy sections, got {type(raw).__name__}."
        )
    known = set(InterferencePolicy.model_fields)
    unknown = sorted(str(key) for key in raw if key not in known)
    if unknown:
        raise ValueError(
            f"Unknown interference policy sections: {unknown}. Valid sections: {sorted(known)}."
        )
    return InterferencePolicy.model_validate(raw)


# --- event payload formatters (single parseable strings) -------------------------------------


def _quote(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text.replace("'", "'") + "'"


def format_focus_taken_by(info: Any) -> str:
    """``FOCUS_TAKEN_BY title='...' process=... hwnd=... class='...'`` from a WindowInfo."""
    process = getattr(info, "process_name", None) or getattr(info, "exe_path", None) or "unknown"
    return (
        f"{FOCUS_TAKEN_BY} title={_quote(getattr(info, 'title', ''))} "
        f"process={process} hwnd={getattr(info, 'hwnd', None) or 0} "
        f"class={_quote(getattr(info, 'window_class', None))}"
    )


def format_focus_identity_unknown() -> str:
    """``FOCUS_IDENTITY_UNKNOWN`` — the foreground identity could not be read (fail closed)."""
    return (
        f"{FOCUS_IDENTITY_UNKNOWN} foreground window identity is unavailable; "
        "refusing to dispatch (fail-closed on_identity_unknown=abort)"
    )


def format_focus_drifted(expected: Any, actual: Any) -> str:
    """``FOCUS_DRIFTED expected='...' actual='...'`` from two focus-target descriptions."""
    return f"{FOCUS_DRIFTED} expected={_quote(expected)} actual={_quote(actual)}"


def _dialog_field(dialog: Any, key: str, default: Any = None) -> Any:
    """Read a dialog-probe field from a mapping or an attribute object (probe shape)."""
    if isinstance(dialog, dict):
        return dialog.get(key, default)
    return getattr(dialog, key, default)


def format_modal_dialog(dialog: Any) -> str:
    """``MODAL_DIALOG title='...' class='...' hwnd=... owner_hwnd=... matched=<...>``."""
    return (
        f"{MODAL_DIALOG} title={_quote(_dialog_field(dialog, 'title', ''))} "
        f"class={_quote(_dialog_field(dialog, 'window_class'))} "
        f"hwnd={_dialog_field(dialog, 'hwnd', 0) or 0} "
        f"owner_hwnd={_dialog_field(dialog, 'owner_hwnd', 0) or 0} "
        f"matched={_quote(_dialog_field(dialog, 'matched', 'unknown'))}"
    )


def format_reattached(title: str, hwnd: int | None) -> str:
    """``REATTACHED title='...' hwnd=...`` — attach-or-launch focused an existing instance."""
    return f"{REATTACHED} title={_quote(title)} hwnd={hwnd or 0}"


def format_ambiguous_instance(candidates: Sequence[Any]) -> str:
    """``AMBIGUOUS_INSTANCE candidates=[... ]`` — unsaved-work risk; the driver decides.

    Each candidate renders as ``title=... unsaved=...``; the payload NEVER launches and
    NEVER closes anything (discovery only).
    """
    def _candidate_title(item: Any) -> str:
        title = getattr(item, "title", None)
        if title:
            return str(title)
        return str(getattr(getattr(item, "window", None), "title", "") or "")

    rendered = [
        f"title={_quote(_candidate_title(item))}"
        f" unsaved={str(bool(getattr(item, 'unsaved_candidate', False))).lower()}"
        for item in candidates
    ]
    return f"{AMBIGUOUS_INSTANCE} candidates=[{'; '.join(rendered)}] do_not_launch=true"


def format_no_instance(target: str, launch: str) -> str:
    """``NO_INSTANCE target='...' launch=<driver|server>`` — no existing window matched."""
    return (
        f"{NO_INSTANCE} target={_quote(target)} launch={launch} "
        "(no existing window matched; with launch=driver the driver may launch through "
        "its normal flow)"
    )


def format_stuck_modifier(keys: Sequence[str]) -> str:
    """``STUCK_MODIFIER keys=[...]`` — modifiers held down before a chord dispatch."""
    return f"{STUCK_MODIFIER} keys=[{','.join(keys)}]"


def format_target_gone(title: str, hwnd: int | None) -> str:
    """``TARGET_GONE title='...' hwnd=...`` — the bound window no longer exists (B6).

    With the default ``on_target_gone="unbind_and_report"`` the guard clears the
    binding after reporting, so the driver's re-ground / reattach actions proceed
    instead of being rejected forever against a dead identity.
    """
    return (
        f"{TARGET_GONE} title={_quote(title)} hwnd={hwnd or 0} "
        "(the bound window no longer exists; re-ground, then reattach by identity "
        "via ensure_app / focus_window)"
    )
