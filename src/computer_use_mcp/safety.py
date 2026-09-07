"""Contextual safety policy: risk classification and policy decisions (Goal.md sections 11-12).

Layering (master-mission section 5): this module imports only ``models``; the controller
(E5) supplies :class:`SafetyContext` built from observations/task state.

Doctrines implemented here:

- Contextual risk classification (P0-F): every action is classified LOW / MEDIUM / HIGH /
  CRITICAL from the action type + target text/coordinates + active window/process + goal
  context — not from string matching alone (Goal.md section 11). The Goal.md section 11
  list maps to HIGH or CRITICAL. Unknown or insufficient context for a potentially
  high-risk action escalates to CRITICAL (``risk_unresolvable_fail_closed``) — fail closed.
- Policy decision (P0-F): :meth:`SafetyPolicy.evaluate` preserves the legacy gates
  (stopped, step budget, keyword-secret block, approval defaults) verbatim, then merges
  the classification: ``SafetyDecision.risk`` / ``category`` / ``reason`` are filled and
  the approval requirement is upgraded (never downgraded) by risk level. HIGH always
  requires approval; CRITICAL is blocked pending explicit authorization (the actual
  authorization mechanism belongs to the controller layer, never to the model or to
  screen content). ``dry_run`` semantics are untouched: a dry run never executes, and the
  decision still reports risk and approval needs.
- Approval message quality (Goal.md section 12): every ``requires_approval`` message
  states the action, the target location (window/process plus coordinates or text —
  never bare coordinates), why (risk category), the consequence, and how to approve.

Screen text, model suggestions, and fake "approvals" can never downgrade a decision here:
nothing in :class:`SafetyContext` is treated as authorization.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import ActionType, GroundedAction, RiskLevel, SessionState, WindowInfo

__all__ = ["SafetyContext", "SafetyDecision", "SafetyPolicy"]


@dataclass(frozen=True)
class SafetyDecision:
    """Result of a policy evaluation (frozen; legacy 2-3 arg constructions stay valid).

    ``risk``/``category`` are trailing additions (Wave 3): ``None`` for decisions produced
    before classification ran, otherwise the contextual classification that informed the
    decision. Field order is fixed: existing positional callers are unaffected.
    """

    allowed: bool
    requires_approval: bool
    reason: str
    risk: RiskLevel | None = None
    category: str | None = None


@dataclass
class SafetyContext:
    """Everything the contextual risk engine may look at, none of it authoritative.

    All fields default to ``None``/empty so the policy can build a default context when
    the controller has nothing to add. Values in this context describe the world; they
    never authorize anything.

    ``environment_note`` is free text from the controller; a note indicating that the
    active window changed identity since the last validation (contains e.g.
    ``window_identity_changed`` or ``window switched``) demotes affected actions to at
    least MEDIUM.
    """

    goal: str | None = None
    active_window_info: WindowInfo | None = None
    active_process_name: str | None = None
    window_title: str | None = None
    task_summary: str | None = None
    recent_actions: list[str] = field(default_factory=list)
    environment_note: str | None = None

    @property
    def process_identity(self) -> str | None:
        """Best available foreground process identity, or None when unknown."""
        if self.active_process_name:
            return self.active_process_name
        info = self.active_window_info
        if info is not None:
            if info.process_name:
                return info.process_name
            if info.exe_path:
                return info.exe_path.replace("\\", "/").rsplit("/", 1)[-1] or None
        return None

    @property
    def title_identity(self) -> str | None:
        """Best available foreground window title, or None when unknown."""
        if self.window_title:
            return self.window_title
        info = self.active_window_info
        if info is not None and info.title:
            return info.title
        return None

    @property
    def identity_known(self) -> bool:
        """True when the target window/process identity is available.

        When False, potentially high-risk actions cannot be resolved and escalate to
        CRITICAL (``risk_unresolvable_fail_closed``).
        """
        return self.process_identity is not None or self.title_identity is not None

    def describe_identity(self) -> str:
        """Human-readable target identity for approval messages (never bare coordinates)."""
        process = self.process_identity
        title = self.title_identity
        if process and title:
            return f"process {process!r}, window {title!r}"
        if process:
            return f"process {process!r} (window title unknown)"
        if title:
            return f"window {title!r} (process unknown)"
        return "unknown application (window/process identity unavailable)"


#: Keys whose keypress can alter application state (legacy set, preserved verbatim).
_STATE_CHANGING_KEYS: frozenset[str] = frozenset({"delete", "backspace", "win", "alt", "ctrl"})

#: Marker text a controller may put in ``SafetyContext.environment_note`` when the active
#: window changed identity since the last validation (P0-H drift seen at classify time).
WINDOW_IDENTITY_CHANGED_MARKER = "window_identity_changed"

_MAX_TEXT_SNIPPET = 80

# --- Goal.md section 11 category patterns -------------------------------------------------
# Each entry: (compiled pattern, category). Scanned against action text + reason,
# case-insensitively. Substring/Arabic terms are handled separately below.

_CRITICAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(?:powershell|pwsh|cmd\.exe|command\s+prompt|cmd\s*/c|bash\s+-c|sh\s+-c"
            r"|invoke-expression|iex\s|start-process|wscript|cscript|certutil)\b",
            re.IGNORECASE,
        ),
        "shell_execution",
    ),
    (
        re.compile(r"\breg\s+(?:add|delete|import|restore|load|save)\b|\bregedit\b", re.IGNORECASE),
        "registry_write",
    ),
    (
        re.compile(
            r"\bformat\s+[a-z][:\\]|\bdiskpart\b|\bwipe\s+(?:disk|drive)\b|\bformat-volume\b",
            re.IGNORECASE,
        ),
        "disk_destructive",
    ),
    (
        re.compile(
            r"\bdel\s+\S|\brd\s+/[sq]|\b(?:rmdir|remove-item)\b|\brm\s+-\w|\brm\s+\S|\berase\s+\S"
            r"|\bdelete\s+(?:the\s+)?(?:file|files|folder|folders|directory|directories|all"
            r"|everything)\b|\b(?:delete|remove|erase)\s+(?:all\s+)?(?:the\s+)?"
            r"(?:files?|folders?|directories?)\b",
            re.IGNORECASE,
        ),
        "file_deletion",
    ),
    (
        re.compile(
            r"\bdrop\s+(?:database|table|schema|index|view)\b|\btruncate\s+table\b"
            r"|\bdelete\s+from\s+\w+",
            re.IGNORECASE,
        ),
        "destructive_sql",
    ),
    (
        re.compile(
            r"\b(?:change|reset|update|remove|delete|rotate)\s+(?:the\s+|my\s+|your\s+)?"
            r"(?:admin\s+|user\s+|root\s+|login\s+)?(?:password|passwd|passphrase|pin|credentials?)\b"
            r"|\b(?:delete|remove)\s+(?:the\s+)?(?:account|user)\b",
            re.IGNORECASE,
        ),
        "credential_change",
    ),
    (
        re.compile(
            r"\b(?:disable|turn\s+off|deactivate|weaken)\s+(?:the\s+)?(?:firewall|antivirus"
            r"|defender|uac|user\s+account\s+control|security|protection)\b"
            r"|\badd\s+(?:an\s+)?exclusion\b",
            re.IGNORECASE,
        ),
        "security_change",
    ),
    (
        re.compile(
            r"\b(?:purchase|checkout|place\s+(?:an\s+)?order|buy\s+now|pay\s+now"
            r"|complete\s+(?:the\s+)?payment|confirm\s+(?:the\s+)?payment"
            r"|transfer\s+(?:money|funds)|send\s+(?:money|payment)|wire\s+transfer"
            r"|enter\s+(?:the\s+)?credit\s+card)\b",
            re.IGNORECASE,
        ),
        "financial_transaction",
    ),
    (
        re.compile(
            r"\b(?:send|forward)\s+(?:an?\s+|the\s+)?(?:email|e-mail|message|mail)\b"
            r"|\b(?:click|press|hit)\s+send\b|\breply\s+all\b"
            r"|\bpost\s+(?:this|it|the)\s+(?:message|comment)\b",
            re.IGNORECASE,
        ),
        "external_send",
    ),
)

_HIGH_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(?:install|uninstall|reinstall|msiexec|setup\.exe|run\s+the\s+installer)\b",
            re.IGNORECASE,
        ),
        "install_uninstall",
    ),
    (
        re.compile(
            r"\b(?:taskkill|kill\s+(?:the\s+)?(?:process|app|task)|end\s+task|end\s+process"
            r"|force\s+quit|terminate\s+(?:the\s+)?(?:process|app))\b",
            re.IGNORECASE,
        ),
        "process_kill",
    ),
    (
        re.compile(
            r"\b(?:control\s+panel|system\s+settings|windows\s+settings|device\s+manager"
            r"|services\.msc|task\s+scheduler|group\s+policy|gpedit|change\s+(?:the\s+)?settings)\b",
            re.IGNORECASE,
        ),
        "system_settings_change",
    ),
    (
        re.compile(r"\brun\s+as\s+administrator\b|\belevate\b|\badministrator\s+privileges\b", re.IGNORECASE),
        "elevation",
    ),
    (
        re.compile(
            r"\b(?:netsh|network\s+(?:settings|configuration|adapter)|adapter\s+settings"
            r"|proxy\s+settings|dns\s+(?:server|settings)|vpn\s+connection"
            r"|change\s+the\s+(?:dns|ip))\b",
            re.IGNORECASE,
        ),
        "network_config",
    ),
    (
        re.compile(
            r"\b(?:empty|clear|purge)\s+(?:the\s+)?recycle\s+bin\b"
            r"|\bpermanently\s+delete\s+everything\b",
            re.IGNORECASE,
        ),
        "wide_delete",
    ),
    (
        re.compile(
            r"\bpaste\s+(?:the\s+)?(?:password|credentials?|secret|token|api\s?key)\b",
            re.IGNORECASE,
        ),
        "clipboard_credential",
    ),
)

_MEDIUM_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?:^|\s)(?:https?://|www\.)|\bnavigate\s+to\b"
            r"|\bopen\s+(?:the\s+)?(?:website|url|site|webpage)\b"
            r"|\.(?:com|org|net|io|edu|gov)(?:\s|$|/|\")",
            re.IGNORECASE | re.MULTILINE,
        ),
        "navigation",
    ),
    (
        re.compile(
            r"\b(?:save\s+as|save\s+the\s+file|move\s+to|copy\s+to|rename|new\s+folder|export"
            r"|overwrite|replace\s+the\s+file)\b",
            re.IGNORECASE,
        ),
        "file_modification",
    ),
)

#: Arabic destructive terms (Goal.md section 11 multilingual note): delete/wipe. Their
#: presence in typed text or reason is suspicious — at least MEDIUM; command patterns
#: above still escalate to CRITICAL when they also match.
_SUSPICIOUS_ARABIC_TERMS: tuple[str, ...] = ("حذف", "مسح")
#: Arabic for "format" (disk formatting) — destructive on its own.
_CRITICAL_ARABIC_TERMS: tuple[str, ...] = ("تهيئة",)

#: Human-readable "why" per category (used verbatim in approval/block messages).
_CATEGORY_WHY: dict[str, str] = {
    "shell_execution": "the action text requests shell or command-prompt execution",
    "registry_write": "the action text modifies the Windows registry",
    "disk_destructive": "the action text targets disk formatting or partitioning",
    "file_deletion": "the action text deletes files or directories",
    "destructive_sql": "the action text contains destructive SQL",
    "credential_change": "the action text changes credentials or accounts",
    "security_change": "the action text weakens security controls",
    "financial_transaction": "the action text performs a purchase or financial transaction",
    "external_send": "the action text sends content to external recipients",
    "install_uninstall": "the action text installs or removes software",
    "process_kill": "the action text terminates a running process",
    "system_settings_change": "the action text changes system settings",
    "elevation": "the action text requests elevated privileges",
    "network_config": "the action text changes network configuration",
    "wide_delete": "the action text performs a wide-scope deletion",
    "clipboard_credential": "the action pastes credential material",
    "navigation": "the action navigates to a new URL or site",
    "file_modification": "the action writes, moves, or renames files",
    "window_identity_drift": "the active window changed identity since the last validation context",
    "suspicious_delete_term": "the text contains destructive delete terminology",
    "keyboard_shortcut_state_change": "the key combination can alter application state",
    "window_focus_change": "the action brings a different window to the foreground",
    "unverified_target_application": "the target application or window identity is unknown",
    "risk_unresolvable_fail_closed": (
        "the action looks high-risk but the context lacks window/process identity to resolve it"
    ),
    "plain_text_entry": "plain text entry into a known target",
    "known_application_interaction": "interaction with an identified application window",
    "completion": "task completion marker",
    "low_routine_action": "routine action with no state-changing potential",
}

#: Human-readable consequence per category (approval/block message quality, Goal.md 12).
_CATEGORY_CONSEQUENCE: dict[str, str] = {
    "shell_execution": "Arbitrary operating-system commands could run with this process's privileges.",
    "registry_write": "Windows registry configuration could be permanently modified.",
    "disk_destructive": "Storage volumes or partitions could be irreversibly erased.",
    "file_deletion": "Files or directories could be permanently deleted.",
    "destructive_sql": "Database data could be irreversibly destroyed.",
    "credential_change": "Credentials could be changed and legitimate users locked out.",
    "security_change": "Security controls protecting this machine could be weakened.",
    "financial_transaction": "A real financial charge could be made with no undo.",
    "external_send": "Content could be sent to recipients outside this machine.",
    "install_uninstall": "Software could be installed or removed, persisting changes on this machine.",
    "process_kill": "A running process could be terminated, losing unsaved work.",
    "system_settings_change": "System configuration could change and affect other applications.",
    "elevation": "The action could grant elevated (administrator) privileges.",
    "network_config": "Network connectivity or routing could be reconfigured.",
    "wide_delete": "Many deleted items could be permanently purged.",
    "clipboard_credential": "Credential material could be exposed or pasted into the wrong target.",
    "navigation": "The application could navigate to a new, unvetted destination.",
    "file_modification": "Files could be created, overwritten, moved, or renamed.",
    "window_identity_drift": "The action could hit the wrong target because the window changed.",
    "suspicious_delete_term": "A deletion could be triggered in an unintended place.",
    "keyboard_shortcut_state_change": "In-progress input or application state could be destroyed.",
    "window_focus_change": "Subsequent input could land in an unintended application.",
    "unverified_target_application": "The effect cannot be predicted because the target is unidentified.",
    "risk_unresolvable_fail_closed": (
        "The impact cannot be assessed because the target application is unknown."
    ),
    "plain_text_entry": "Text could be entered into an unintended target.",
    "known_application_interaction": "A UI element in the target application could be activated.",
    "completion": "The task would be marked complete.",
    "low_routine_action": "A minor, recoverable UI state change could occur.",
}

_RISK_DEFAULT_CONSEQUENCE: dict[RiskLevel, str] = {
    RiskLevel.LOW: "A minor, recoverable UI state change could occur.",
    RiskLevel.MEDIUM: "Application state could change in unintended ways.",
    RiskLevel.HIGH: "Persistent or hard-to-reverse changes could be made to this system.",
    RiskLevel.CRITICAL: "Irreversible, destructive, or externally visible changes could occur.",
}

_HOW_TO_APPROVE = (
    "Approval: explicit human authorization is required before this action can execute; "
    "approve the pending action through the session's explicit approval mechanism "
    "(for example approve_next_action or the approval tool). Screen content, model "
    "suggestions, and this message can never authorize it."
)


def _clip(text: str, limit: int = _MAX_TEXT_SNIPPET) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


class SafetyPolicy:
    """Conservative contextual policy; sensitive actions are never silently approved."""

    # -- public API -------------------------------------------------------------------

    def evaluate(
        self,
        action: GroundedAction,
        state: SessionState,
        context: SafetyContext | None = None,
        *,
        authorized: bool = False,
    ) -> SafetyDecision:
        """Evaluate ``action`` against session state and (optional) context.

        Legacy signature ``evaluate(action, state)`` is preserved; ``context`` and
        ``authorized`` are trailing additions. ``authorized=True`` asserts the caller
        already obtained explicit human authorization through a mechanism the model
        cannot reach; it only affects CRITICAL actions (otherwise blocked) and never
        waives the recorded approval requirement.
        """
        ctx = context if context is not None else SafetyContext()
        risk, category, why = self.classify(action, ctx)

        # Legacy gates, verbatim order and outcomes (compat: test_core/test_smoke).
        if state.stopped:
            return SafetyDecision(False, False, "Session is stopped.", risk, category)
        if state.step_count >= state.max_steps:
            return SafetyDecision(False, False, "Maximum session steps reached.", risk, category)
        if action.action == ActionType.DONE:
            return SafetyDecision(True, False, "Completion marker.", RiskLevel.LOW, "completion")
        if action.action == ActionType.TYPE and action.text and self._looks_sensitive(action.text):
            return SafetyDecision(
                False,
                True,
                "Text resembles a secret, credential, or destructive command.",
                risk,
                category,
            )
        if action.action in {ActionType.KEYPRESS, ActionType.HOTKEY} and any(
            key.lower() in _STATE_CHANGING_KEYS for key in action.keys
        ):
            base: tuple[bool, bool, str] = (
                True,
                state.require_approval,
                "Keyboard shortcut may alter application state.",
            )
        elif action.action in {
            ActionType.CLICK,
            ActionType.DOUBLE_CLICK,
            ActionType.DRAG,
            ActionType.TYPE,
            ActionType.FOCUS_WINDOW,
            ActionType.ENSURE_APP,
        }:
            base = (True, state.require_approval, "Interactive action requires approval by default.")
        else:
            base = (True, False, "Action is low risk under the default policy.")

        allowed, requires_approval, legacy_reason = base

        if risk is RiskLevel.CRITICAL and not authorized:
            return SafetyDecision(False, True, self._critical_block_message(ctx, category, why), risk, category)

        if risk is RiskLevel.HIGH:
            requires_approval = True  # upgraded, never downgraded (P0-F)

        if not allowed:  # legacy block below CRITICAL keeps its legacy message
            return SafetyDecision(False, requires_approval, legacy_reason, risk, category)

        if requires_approval:
            prefix = "Caller-declared explicit authorization recorded. " if authorized else ""
            message = prefix + self._approval_message(action, ctx, risk, category, why)
        else:
            message = legacy_reason
        return SafetyDecision(True, requires_approval, message, risk, category)

    def classify(
        self, action: GroundedAction, context: SafetyContext | None = None
    ) -> tuple[RiskLevel, str, str]:
        """Classify ``action`` contextually; return ``(risk, category, reason)``.

        The category is a stable machine string (e.g. ``shell_execution``); the reason is
        a human-readable "why". See the module docstring for the doctrine.
        """
        ctx = context if context is not None else SafetyContext()
        haystack = "\n".join(part for part in (action.text, action.reason) if part)

        for pattern, category in _CRITICAL_PATTERNS:
            if pattern.search(haystack):
                return RiskLevel.CRITICAL, category, self._why(category)
        if any(term in haystack for term in _CRITICAL_ARABIC_TERMS):
            return RiskLevel.CRITICAL, "disk_destructive", self._why("disk_destructive")

        for pattern, category in _HIGH_PATTERNS:
            if pattern.search(haystack):
                if not ctx.identity_known:
                    return (
                        RiskLevel.CRITICAL,
                        "risk_unresolvable_fail_closed",
                        self._why("risk_unresolvable_fail_closed"),
                    )
                return RiskLevel.HIGH, category, self._why(category)

        if action.action in {ActionType.FOCUS_WINDOW, ActionType.ENSURE_APP}:
            # Always MEDIUM with a stable category (placed before the drift/routine
            # scans): foregrounding a different window redirects all subsequent input.
            # T8: ensure_app is focus-like (attach-or-launch redirects input too) and
            # additionally never launches without explicit host policy (agent gate).
            return (
                RiskLevel.MEDIUM,
                "window_focus_change",
                self._why("window_focus_change"),
            )
        if self._environment_notes_window_drift(ctx):
            return (
                RiskLevel.MEDIUM,
                "window_identity_drift",
                self._why("window_identity_drift"),
            )
        for pattern, category in _MEDIUM_PATTERNS:
            if pattern.search(haystack):
                return RiskLevel.MEDIUM, category, self._why(category)
        if any(term in haystack for term in _SUSPICIOUS_ARABIC_TERMS):
            return RiskLevel.MEDIUM, "suspicious_delete_term", self._why("suspicious_delete_term")

        if action.action in {ActionType.KEYPRESS, ActionType.HOTKEY}:
            if any(key.lower() in _STATE_CHANGING_KEYS for key in action.keys):
                return (
                    RiskLevel.MEDIUM,
                    "keyboard_shortcut_state_change",
                    self._why("keyboard_shortcut_state_change"),
                )
            return RiskLevel.LOW, "low_routine_action", self._why("low_routine_action")

        if action.action in {ActionType.SCROLL, ActionType.WAIT, ActionType.DONE, ActionType.MOVE}:
            return RiskLevel.LOW, "low_routine_action", self._why("low_routine_action")

        if action.action in {ActionType.CLICK, ActionType.DOUBLE_CLICK, ActionType.DRAG, ActionType.TYPE}:
            if not ctx.identity_known:
                return (
                    RiskLevel.MEDIUM,
                    "unverified_target_application",
                    self._why("unverified_target_application"),
                )
            if action.action == ActionType.TYPE:
                return RiskLevel.LOW, "plain_text_entry", self._why("plain_text_entry")
            return (
                RiskLevel.LOW,
                "known_application_interaction",
                self._why("known_application_interaction"),
            )

        # Unknown action type: fail closed.
        return (
            RiskLevel.CRITICAL,
            "risk_unresolvable_fail_closed",
            self._why("risk_unresolvable_fail_closed"),
        )

    # -- legacy keyword gate (preserved verbatim) ---------------------------------------

    @staticmethod
    def _looks_sensitive(text: str) -> bool:
        lowered = text.lower()
        markers = (
            "password", "api_key", "apikey", "secret", "token", "credential",
            "rm ", "rm -", "del ", "erase ", "format ", "shutdown", "restart",
            "powershell", "cmd.exe", "reg delete", "remove-item", "drop database",
            "delete account", "delete user", "reset password",
        )
        return any(marker in lowered for marker in markers)

    # -- message construction (P0-F, Goal.md section 12) ---------------------------------

    @staticmethod
    def _why(category: str) -> str:
        return _CATEGORY_WHY.get(category, f"the action matches risk category {category!r}")

    def _consequence_for(self, risk: RiskLevel, category: str | None) -> str:
        if category is not None and category in _CATEGORY_CONSEQUENCE:
            return _CATEGORY_CONSEQUENCE[category]
        return _RISK_DEFAULT_CONSEQUENCE[risk]

    def _describe_action(self, action: GroundedAction) -> str:
        kind = action.action.value if hasattr(action.action, "value") else str(action.action)
        if action.action in {ActionType.CLICK, ActionType.DOUBLE_CLICK} and action.point is not None:
            return f"{kind} at screenshot coordinates (x={action.point.x}, y={action.point.y})"
        if (
            action.action == ActionType.DRAG
            and action.point is not None
            and action.to_point is not None
        ):
            return (
                f"{kind} from screenshot coordinates (x={action.point.x}, y={action.point.y}) "
                f"to (x={action.to_point.x}, y={action.to_point.y})"
            )
        if action.action == ActionType.TYPE and action.text:
            return f"{kind} text {_clip(action.text)!r}"
        if action.action == ActionType.KEYPRESS and action.keys:
            return f"{kind} {'+'.join(action.keys)}"
        if action.action == ActionType.FOCUS_WINDOW and action.target:
            return f"{kind} window {action.target!r}"
        if action.action == ActionType.ENSURE_APP and action.target:
            return f"{kind} app {action.target!r}"
        if action.action == ActionType.SCROLL:
            return f"{kind} by {action.delta}"
        return kind

    def _describe_target(self, action: GroundedAction, ctx: SafetyContext) -> str:
        """Target location: application identity plus coordinates/text — never bare coordinates."""
        identity = ctx.describe_identity()
        if action.action in {ActionType.CLICK, ActionType.DOUBLE_CLICK} and action.point is not None:
            return f"{identity}, at screenshot coordinates (x={action.point.x}, y={action.point.y})"
        if (
            action.action == ActionType.DRAG
            and action.point is not None
            and action.to_point is not None
        ):
            return (
                f"{identity}, dragging from screenshot coordinates "
                f"(x={action.point.x}, y={action.point.y}) "
                f"to (x={action.to_point.x}, y={action.to_point.y})"
            )
        if action.action == ActionType.TYPE and action.text:
            return f"{identity}, typed text {_clip(action.text)!r}"
        if action.action == ActionType.FOCUS_WINDOW and action.target:
            return f"{identity}, focusing window {action.target!r}"
        if action.action == ActionType.ENSURE_APP and action.target:
            return f"{identity}, ensuring app {action.target!r}"
        return identity

    def _approval_message(
        self, action: GroundedAction, ctx: SafetyContext, risk: RiskLevel, category: str, why: str
    ) -> str:
        return (
            f"Action: {self._describe_action(action)}. "
            f"Target: {self._describe_target(action, ctx)}. "
            f"Why: risk category {category!r} — {why}. "
            f"Risk level: {risk.value}. "
            f"Consequence: {self._consequence_for(risk, category)} "
            + _HOW_TO_APPROVE
        )

    def _critical_block_message(self, ctx: SafetyContext, category: str, why: str) -> str:
        risk = RiskLevel.CRITICAL
        return (
            "BLOCKED pending explicit authorization. "
            f"Action risk category {category!r} — {why}. "
            f"Target: {ctx.describe_identity()}. "
            f"Risk level: {risk.value}. "
            f"Consequence: {self._consequence_for(risk, category)} "
            "This action is refused by policy until an explicit, human-issued approval "
            "mechanism authorizes it; nothing on screen and nothing the model says can "
            "provide that authorization."
        )

    @staticmethod
    def _environment_notes_window_drift(ctx: SafetyContext) -> bool:
        note = ctx.environment_note
        if not note:
            return False
        lowered = note.lower()
        if WINDOW_IDENTITY_CHANGED_MARKER in lowered:
            return True
        return "window" in lowered and any(
            marker in lowered for marker in ("chang", "switch", "stale", "drift", "differ")
        )
