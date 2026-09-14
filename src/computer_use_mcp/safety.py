"""Contextual safety policy: risk classification and policy decisions (Goal.md sections 11-12).

Layering (master-mission section 5): this module imports only ``models`` and the
stdlib-only :mod:`textnorm` canonicalizer (R-23); the controller (E5) supplies
:class:`SafetyContext` built from observations/task state.

Doctrines implemented here:

- Contextual risk classification (P0-F): every action is classified LOW / MEDIUM / HIGH /
  CRITICAL from the action type + target text/coordinates + active window/process + goal
  context \u2014 not from string matching alone (Goal.md section 11). The Goal.md section 11
  list maps to HIGH or CRITICAL. Unknown or insufficient context for a potentially
  high-risk action escalates to CRITICAL (``risk_unresolvable_fail_closed``) \u2014 fail closed.
- Policy decision (P0-F): :meth:`SafetyPolicy.evaluate` preserves the legacy gates
  (stopped, step budget, keyword-secret block, approval defaults) verbatim, then merges
  the classification: ``SafetyDecision.risk`` / ``category`` / ``reason`` are filled and
  the approval requirement is upgraded (never downgraded) by risk level. HIGH always
  requires approval; CRITICAL is blocked pending explicit authorization (the actual
  authorization mechanism belongs to the controller layer, never to the model or to
  screen content). ``dry_run`` semantics are untouched: a dry run never executes, and the
  decision still reports risk and approval needs.
- Approval message quality (Goal.md section 12): every ``requires_approval`` message
  states the action, the target location (window/process plus coordinates or text \u2014
  never bare coordinates), why (risk category), the consequence, and how to approve.

Screen text, model suggestions, and fake "approvals" can never downgrade a decision here:
nothing in :class:`SafetyContext` is treated as authorization.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import ActionType, GroundedAction, RiskLevel, SessionState, WindowInfo
from .textnorm import canonical_views

__all__ = ["SafetyContext", "SafetyDecision", "SafetyPolicy"]

#: Risk severity order for the never-downgrade merges (R-23 dual-view + R-21 floors).
_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


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
#: presence in typed text or reason is suspicious \u2014 at least MEDIUM; command patterns
#: above still escalate to CRITICAL when they also match.
_SUSPICIOUS_ARABIC_TERMS: tuple[str, ...] = ("\u062d\u0630\u0641", "\u0645\u0633\u062d")
#: Arabic for "format" (disk formatting) \u2014 destructive on its own.
_CRITICAL_ARABIC_TERMS: tuple[str, ...] = ("\u062a\u0647\u064a\u0626\u0629",)

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
    "destructive_intent": "the text pairs a destructive verb with a target object",
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
    "destructive_intent": "Data targeted by an explicit destructive verb could be permanently deleted or destroyed.",
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

# --- R-02 keyword-gate vocabulary (word-boundary matching, not substrings) ----------------

#: Single-token markers for :meth:`SafetyPolicy._looks_sensitive`. Credential nouns
#: include the plural forms the legacy substring gate also matched (zero weakening of
#: secret/credential detection); command words stay in their imperative form.
_SENSITIVE_TOKEN_MARKERS: frozenset[str] = frozenset(
    {
        "password", "passwords",
        "api_key", "api_keys", "api-key", "api-keys",
        "apikey", "apikeys",
        "secret", "secrets",
        "token", "tokens",
        "credential", "credentials",
        "rm", "del", "erase", "format", "shutdown", "restart", "powershell",
        "remove-item",
    }
)

#: Multi-token phrase markers (consecutive whole tokens; whitespace-tolerant). The
#: ``("cmd", "exe")`` pair is the tokenized form of the legacy ``cmd.exe`` marker.
_SENSITIVE_PHRASE_MARKERS: tuple[tuple[str, ...], ...] = (
    ("cmd", "exe"),
    ("reg", "delete"),
    ("drop", "database"),
    ("delete", "account"),
    ("delete", "user"),
    ("reset", "password"),
)

#: Head nouns of the benign citation phrase "DOI token(s)": suppressed only when the
#: immediately preceding token is "doi". Every other occurrence of "token"/"tokens"
#: (including "api tokens", "access token") still blocks, exactly as before.
_DOI_TOKEN_HEAD_NOUNS: frozenset[str] = frozenset({"token", "tokens"})

#: Words, snake_case identifiers, and hyphenated compounds as single tokens — so
#: ``"closed-form"`` is one token and ``"rm "`` can never hide inside it.
_TEXT_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+(?:-[a-z0-9_]+)*")


def _token_components(lowered: str) -> list[tuple[str, bool]]:
    """Matching sequence of ``(component, from_compound)`` pairs.

    Tokens are split on ``_`` (the identifier convention): ``"client_secret"`` yields
    ``("client", True), ("secret", True)`` so compound identifier naming blocks exactly
    as it did under the substring gate, while ``"closed-form"`` stays one hyphenated
    token and can never hide ``"rm"``. ``from_compound`` is True only for components of
    a MULTI-part token, so a bare English word (``key``) stays inert while the same
    word as a compound tail (``api_key``) blocks.
    """
    components: list[tuple[str, bool]] = []
    for token in _TEXT_TOKEN_PATTERN.findall(lowered):
        parts = token.split("_")
        compound = len(parts) > 1
        components.extend((part, compound) for part in parts)
    return components


#: Final components of underscore-compound markers, so ``api_key``/``auth_key``-style
#: identifiers still block after the component split (singular + plural).
_COMPOUND_MARKER_TAILS: frozenset[str] = frozenset(
    {
        "key", "keys", "secret", "secrets", "token", "tokens",
    }
)


# --- R-21 destructive-intent grammar (additive floors over NORMALIZED components) ----------

#: V1 — unambiguous destructive verbs. V1 + any object inside the window floors the
#: verdict at MEDIUM (``destructive_intent``) and fires the keyword gate. Whole-token
#: matches only; NOMINALIZATIONS (``deletion``/``reformatting``) and typos stay
#: distinct tokens and remain benign (the no-stemming design).
#:
#: S4 repair (RT-D1-04, Commander-approved policy): the bounded inflection family
#: (:func:`_inflect_verb_forms` — s/es/ed/d/ing with e-drop and consonant doubling)
#: is allowed on V1 verbs, so gerund/past/third-person forms of the SAME verb carry
#: the same intent ("wiping the old drive", "deleted the database"). The shell
#: abbreviations ``del``/``rm`` stay exact tokens (they are command names, not
#: English verbs — morphology on them is meaningless). Mention-form copula guards
#: still apply: "wiping is configured" stays benign.
_V1_VERBS: frozenset[str] = frozenset(
    {"delete", "remove", "erase", "wipe", "truncate", "purge", "destroy"}
)
#: V2 — contextual destructive verbs, flagged only with a high-consequence object hit.
#: S4: inflected V2 forms ("formatting the drive") flag ONLY with the class-object
#: hit, exactly like their base forms ("formatting the document" stays benign).
_V2_VERBS: frozenset[str] = frozenset(
    {"drop", "clear", "empty", "format", "kill", "shutdown", "restart"}
)


def _inflect_verb_forms(verb: str) -> frozenset[str]:
    """Bounded inflection family of one verb (S4/RT-D1-04): s/es, d/ed, ing.

    Handles e-drop ("wipe" -> "wiping"), plain suffixing ("destroy" -> "destroyed"),
    and the CVC / double-consonant doubling rule ("format" -> "formatting",
    "drop" -> "dropping"); y/w/x finals never double ("empty" -> "emptying").
    The produced forms are a CLOSED set per verb — this is not a stemmer: derived
    nouns ("deletion"), prefixes ("reformatting"), and typos never match.
    """
    forms = {verb}
    if verb.endswith("e"):
        forms.update({verb + "s", verb + "d", verb[:-1] + "ing"})
        return frozenset(forms)
    forms.update({verb + "s", verb + "es", verb + "ed", verb + "ing"})
    if len(verb) >= 3:
        third, second, last = verb[-3], verb[-2], verb[-1]
        if third not in "aeiou" and second in "aeiou" and last not in "aeiouwxy":
            forms.update({verb + last + "ed", verb + last + "ing"})
    return frozenset(forms)


_V1_FORMS: frozenset[str] = _V1_VERBS | {"del", "rm"} | frozenset().union(
    *(_inflect_verb_forms(verb) for verb in _V1_VERBS)
)
_V2_FORMS: frozenset[str] = frozenset().union(
    *(_inflect_verb_forms(verb) for verb in _V2_VERBS)
)
#: Function words inside the object window are skipped, never object candidates.
_FUNCTION_WORDS: frozenset[str] = frozenset(
    {"the", "a", "an", "my", "your", "this", "that", "these", "those", "all", "everything",
     "now", "entire", "whole", "old", "please", "kindly"}
)
#: Copulas close the window: a verb followed by a copula is a mention, not an
#: imperative ("delete is a word in the dictionary" stays benign) — the same
#: noun+copula-adjacency notion the redaction assignment grammar uses.
_COPULAS: frozenset[str] = frozenset({"is", "are", "was", "were", "be", "been", "being"})
#: High-consequence object class — decides the severity tier (V1/V2 verb + class
#: object -> CRITICAL). The class decides the TIER only; the V1 open-object MEDIUM
#: floor does not depend on noun enumeration.
_HIGH_CONSEQUENCE_NOUNS: frozenset[str] = frozenset(
    {
        "database", "databases", "table", "tables", "schema", "schemas",
        "index", "indexes", "indices", "view", "views", "row", "rows", "record", "records",
        "disk", "disks", "drive", "drives", "partition", "partitions", "volume", "volumes",
        "account", "accounts", "user", "users", "password", "passwords",
        "credential", "credentials", "file", "files", "folder", "folders",
        "directory", "directories", "weight", "weights", "model", "models",
        "checkpoint", "checkpoints", "backup", "backups", "repo", "repos",
        "repository", "repositories", "branch", "branches", "project", "projects",
        "workspace", "workspaces", "history", "log", "logs", "key", "keys",
        "environment", "environments", "everything", "all",
    }
)
#: Object window: the number of NON-function components examined after the verb.
#: S2 repair (RT-D1-01): function words are SKIPPED, never object candidates, and no
#: longer consume window slots — "delete the entire old production database" reaches
#: its object through three function words.
_INTENT_WINDOW = 3


def _destructive_intent_tier(components: list[tuple[str, bool]]) -> RiskLevel | None:
    """Severity tier implied by the verb grammar over gate token components, or None.

    V1 verb form + any object inside the :data:`_INTENT_WINDOW` window -> MEDIUM;
    V1/V2 verb form + a high-consequence object in the window -> CRITICAL. Function
    words are skipped without consuming window slots (S2/RT-D1-01); a copula closes
    the window (mention-form verbs stay benign). Floors only — callers may upgrade an
    existing verdict, never downgrade one.
    """
    count = len(components)
    for index in range(count):
        verb = components[index][0]
        if verb in _V1_FORMS:
            open_object_scored = False
        elif verb in _V2_FORMS:
            open_object_scored = True  # V2: only class-object hits may flag
        else:
            continue
        floor: RiskLevel | None = None
        scanned = 0
        position = index
        while scanned < _INTENT_WINDOW:
            position += 1
            if position >= count:
                break
            word = components[position][0]
            if word in _COPULAS:
                break  # mention-form verb; the imperative window is closed
            if word in _FUNCTION_WORDS:
                continue  # filler: skipped, does not consume a window slot (S2)
            scanned += 1
            if word in _HIGH_CONSEQUENCE_NOUNS:
                return RiskLevel.CRITICAL
            if not open_object_scored and floor is None:
                floor = RiskLevel.MEDIUM  # first open object; keep scanning for class nouns
        if floor is not None:
            return floor
    return None


def _word_alternation(words: frozenset[str]) -> str:
    # Longest-first so compound verbs (``delete``) win over their prefixes (``del``)
    # without relying on backtracking; sorted for a byte-stable compiled pattern.
    return "(?:" + "|".join(sorted(words, key=len, reverse=True)) + ")"


_COPULA_ALT = _word_alternation(_COPULAS)
_FUNC_ALT = _word_alternation(_FUNCTION_WORDS)
_VERB_ALT = _word_alternation(_V1_FORMS | _V2_FORMS)
_V1_ALT = _word_alternation(_V1_FORMS)
_NOUN_ALT = _word_alternation(_HIGH_CONSEQUENCE_NOUNS)

#: classify-side floor for the MEDIUM tier (V1 form + filler-tolerant object). The
#: skip group is tempered on copulas, mirroring the component scanner the keyword
#: gate uses, and — S2 (RT-D1-01) — allows up to THREE function-word groups between
#: the verb and its object (the component scanner skips them without limit). Bounded
#: quantifiers only.
_DESTRUCTIVE_INTENT_MEDIUM = re.compile(
    rf"\b{_V1_ALT}\W+"
    rf"(?:(?!{_COPULA_ALT}\b){_FUNC_ALT}\W+){{0,3}}"
    rf"(?!(?:{_COPULA_ALT}|{_FUNC_ALT})\b)\w",
    re.IGNORECASE,
)
#: classify-side floor for the CRITICAL tier (V1/V2 form + high-consequence object).
_DESTRUCTIVE_INTENT_CRITICAL = re.compile(
    rf"\b{_VERB_ALT}\W+"
    rf"(?:(?!{_COPULA_ALT}\b){_FUNC_ALT}\W+){{0,3}}"
    rf"(?!{_COPULA_ALT}\b){_NOUN_ALT}\b",
    re.IGNORECASE,
)

# --- S7 repair (RT-D1-07 "new push"): fold-view re-fusion ---------------------------------
#
# Mixed-position zero-width payloads ("fo\\u200bmat\\u200bC:", "de\\u200blete\\u200bthe
# \\u200bdatabase") carry an invisible character BOTH inside a keyword AND at a token
# boundary. The canonical views heal exactly one role each: the delete view fuses the
# separator ("formatC:" — the \\s-anchored patterns miss), the fold view splits the
# keyword ("for mat C:" — no verb/marker token). Neither single view matches, so the
# never-downgrade merge has nothing to merge. The repair stays at the view/scan level:
# on the fold view's component stream, ADJACENT components whose CONCATENATION is a
# known keyword token (marker, compound tail, verb form, class noun, or phrase member)
# re-join as a virtual fused component — dictionary-driven and bounded (2- and 3-token
# joins only, keyword-set membership required), so no general fuzzy/stem matching is
# introduced and benign text without strip characters never takes this path.

_FUSION_KEYWORDS: frozenset[str] = (
    _SENSITIVE_TOKEN_MARKERS
    | _COMPOUND_MARKER_TAILS
    | _V1_FORMS
    | _V2_FORMS
    | _HIGH_CONSEQUENCE_NOUNS
    | {word for phrase in _SENSITIVE_PHRASE_MARKERS for word in phrase}
)


def _fused_components(components: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """Fold-view component stream plus virtual re-fused keyword tokens (S7)."""
    count = len(components)
    out: list[tuple[str, bool]] = []
    for index in range(count):
        out.append(components[index])
        if index + 1 >= count:
            continue
        first = components[index][0]
        for join in (
            first + components[index + 1][0],
            first + components[index + 1][0] + components[index + 2][0]
            if index + 2 < count
            else "",
        ):
            if join and join in _FUSION_KEYWORDS:
                out.append((join, True))
    return out


#: P1: necessary condition helper set — every V1/V2 verb form in one lookup.
_VERB_FORM_LOOKUP: frozenset[str] = _V1_FORMS | _V2_FORMS


def _view_has_intent_verb(view: str) -> bool:
    """Cheap prerequisite for the regex intent floor: does ANY V1/V2 verb form appear
    as a word-delimited token in the view?

    The floor regexes anchor on ``\\b<verb-form>\\W+``, so a floor hit requires the
    form to be a complete word in the view's token stream — whole token, or a
    hyphen-split part of one ("delete-all" carries the word "delete" to the regex).
    A miss therefore skips both floor regexes with a provably identical outcome (P1);
    the scan is one ``findall`` over the lowered view plus set lookups.
    """
    for token in _TEXT_TOKEN_PATTERN.findall(view.lower()):
        if token in _VERB_FORM_LOOKUP:
            return True
        if "-" in token:
            for part in token.split("-"):
                if part in _VERB_FORM_LOOKUP:
                    return True
    return False


def _clip(text: str, limit: int = _MAX_TEXT_SNIPPET) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "\u2026"


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
        # P1 perf restructure: canonicalize the haystack ONCE here and reuse the views
        # for both the classification and (for TYPE actions) the keyword gate — the
        # gate previously re-canonicalized the same text a second time. When the
        # action carries no separate reason, the haystack IS the typed text and the
        # gate consumes these exact views.
        haystack = "\n".join(part for part in (action.text, action.reason) if part)
        views = canonical_views(haystack)
        gate_flags: list[bool] | None = None
        if action.action == ActionType.TYPE and action.text:
            gate_views = views if not action.reason else canonical_views(action.text)
            gate_flags = [
                SafetyPolicy._view_is_sensitive(view, fusion=position > 0)
                for position, view in enumerate(gate_views)
            ]

        risk, category, why = self._classify_views(action, ctx, views)

        # Legacy gates, verbatim order and outcomes (compat: test_core/test_smoke).
        if state.stopped:
            return SafetyDecision(False, False, "Session is stopped.", risk, category)
        if state.step_count >= state.max_steps:
            return SafetyDecision(False, False, "Maximum session steps reached.", risk, category)
        if action.action == ActionType.DONE:
            return SafetyDecision(True, False, "Completion marker.", RiskLevel.LOW, "completion")
        if gate_flags is not None and any(gate_flags):
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
            ActionType.RIGHT_CLICK,
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

        R-23: the haystack is matched on its canonical view(s) — one view (byte-identical
        to the raw text) for every ASCII haystack, otherwise the dual delete/fold views
        merged never-downgrade (risk = max; ties keep the first view). R-21: an additive
        ``destructive_intent`` severity floor from the verb grammar may upgrade the
        verdict, never downgrade one.
        """
        ctx = context if context is not None else SafetyContext()
        haystack = "\n".join(part for part in (action.text, action.reason) if part)
        return self._classify_views(action, ctx, canonical_views(haystack))

    def _classify_views(
        self,
        action: GroundedAction,
        ctx: SafetyContext,
        views: tuple[str, ...],
    ) -> tuple[RiskLevel, str, str]:
        """Classify the precomputed canonical views (P1: one canonicalization per call).

        The full classifier body runs on every view (a gate-failing view can still carry
        a pattern-class verdict — "install the app" is HIGH with no gate marker), merged
        never-downgrade. Once a view classifies CRITICAL the merge can only reproduce
        that verdict, so the remaining views and the intent floor are skipped — a pure
        short-circuit with a provably identical result.
        """
        verdict = self._classify_haystack(action, ctx, views[0])
        for view in views[1:]:
            if verdict[0] is RiskLevel.CRITICAL:
                break  # the never-downgrade merge cannot exceed CRITICAL
            other = self._classify_haystack(action, ctx, view)
            if _RISK_ORDER[other[0]] > _RISK_ORDER[verdict[0]]:
                verdict = other
        if verdict[0] is not RiskLevel.CRITICAL:
            floor = self._destructive_intent_floor(views)
            if floor is not None and _RISK_ORDER[floor[0]] > _RISK_ORDER[verdict[0]]:
                return floor
        return verdict

    def _destructive_intent_floor(
        self, views: tuple[str, ...]
    ) -> tuple[RiskLevel, str, str] | None:
        """``destructive_intent`` floor over the canonical haystack views, or None.

        P1: the regex floor can only fire when a view contains a V1/V2 verb form as a
        word-delimited token, so a verb-free view skips both regex passes with a
        provably identical result. S7: on fold views (the dual-view strip-character
        path) the component tier additionally runs over the RE-FUSED component stream
        — the mixed-position payloads split their verb in the raw fold view, so the
        re-fused reading is the only one that carries it.
        """
        medium = False
        for position, view in enumerate(views):
            if position:
                fused = _fused_components(_token_components(view.lower()))
                tier = _destructive_intent_tier(fused)
                if tier is RiskLevel.CRITICAL:
                    return (RiskLevel.CRITICAL, "destructive_intent", self._why("destructive_intent"))
                medium = medium or tier is RiskLevel.MEDIUM
            if not _view_has_intent_verb(view):
                continue  # no verb-form token -> neither floor regex can match
            if _DESTRUCTIVE_INTENT_CRITICAL.search(view):
                return (RiskLevel.CRITICAL, "destructive_intent", self._why("destructive_intent"))
            medium = medium or bool(_DESTRUCTIVE_INTENT_MEDIUM.search(view))
        if medium:
            return (RiskLevel.MEDIUM, "destructive_intent", self._why("destructive_intent"))
        return None

    def _classify_haystack(
        self, action: GroundedAction, ctx: SafetyContext, haystack: str
    ) -> tuple[RiskLevel, str, str]:
        """Legacy contextual classification of one haystack (existing precedence verbatim)."""
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

        if action.action in {
            ActionType.CLICK,
            ActionType.DOUBLE_CLICK,
            ActionType.RIGHT_CLICK,
            ActionType.DRAG,
            ActionType.TYPE,
        }:
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

    # -- legacy keyword gate (R-02: word-boundary matching, same vocabulary) ------------

    @staticmethod
    def _looks_sensitive(text: str) -> bool:
        """True when ``text`` resembles a secret, credential, or destructive command.

        R-02: markers are matched against whole text tokens (words, identifiers,
        hyphenated compounds) and exact token phrases — the same marker vocabulary the
        legacy substring gate used, with word-boundary precision. The substring form
        false-blocked benign prose: ``"rm "`` inside ``"closed-form "``, ``"del "``
        inside ``"model "``, ``"token"`` inside ``"tokens"``/``"DOI token"``.
        Underscore-separated components of a token are matched individually (the
        identifier convention: ``client_secret``, ``user_token``), so compound naming
        still blocks. Every value-bearing usage (``token=ghp_…``,
        ``password: hunter2``, ``api_key=…``) keeps the keyword as a standalone token
        and still blocks, and credential nouns keep their plural forms so plural
        mentions block exactly as before. Only derived-word false positives
        (``tokenize``, ``shutdowns``) and the ``DOI token(s)`` citation phrase pass now.

        R-23: matching runs on the canonical view(s) of ``text`` (ASCII input is
        matched byte-identically to before). R-21: the destructive-intent verb grammar
        (V1 verb + object, or V1/V2 verb + high-consequence object, within the window)
        is additive — it can only widen what blocks, never narrow it. S7: on the
        dual-view (strip-character) path, the fold view additionally matches on
        re-fused keyword tokens — mixed-position invisible characters split the
        keyword in the fold view while fusing the separator in the delete view, and
        neither single view alone sees the payload's plain reading.
        """
        for position, view in enumerate(canonical_views(text)):
            if SafetyPolicy._view_is_sensitive(view, fusion=position > 0):
                return True
        return False

    @staticmethod
    def _view_is_sensitive(view: str, *, fusion: bool = False) -> bool:
        """Legacy marker/phrase gate for one canonical view + the R-21 verb grammar.

        ``fusion=True`` (S7) marks a FOLD view of strip-character-bearing text: token
        boundaries in it may be healed invisible-character insertions, so adjacent
        components that re-join into a known keyword token are matched too.
        """
        components = _token_components(view.lower())
        if fusion:
            components = _fused_components(components)
        for index, (token, from_compound) in enumerate(components):
            sensitive = token in _SENSITIVE_TOKEN_MARKERS or (
                from_compound and token in _COMPOUND_MARKER_TAILS
            )
            if not sensitive:
                continue
            previous = components[index - 1][0] if index else ""
            if token in _DOI_TOKEN_HEAD_NOUNS and previous == "doi":
                continue  # "DOI token(s)": digital-object-identifier citation context
            return True
        count = len(components)
        for phrase in _SENSITIVE_PHRASE_MARKERS:
            size = len(phrase)
            for start in range(count - size + 1):
                window = tuple(components[position][0] for position in range(start, start + size))
                if window == phrase:
                    return True
        return _destructive_intent_tier(components) is not None

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
        if (
            action.action in {ActionType.CLICK, ActionType.DOUBLE_CLICK, ActionType.RIGHT_CLICK}
            and action.point is not None
        ):
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
        """Target location: application identity plus coordinates/text \u2014 never bare coordinates."""
        identity = ctx.describe_identity()
        if (
            action.action in {ActionType.CLICK, ActionType.DOUBLE_CLICK, ActionType.RIGHT_CLICK}
            and action.point is not None
        ):
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
            f"Why: risk category {category!r} \u2014 {why}. "
            f"Risk level: {risk.value}. "
            f"Consequence: {self._consequence_for(risk, category)} "
            + _HOW_TO_APPROVE
        )

    def _critical_block_message(self, ctx: SafetyContext, category: str, why: str) -> str:
        risk = RiskLevel.CRITICAL
        return (
            "BLOCKED pending explicit authorization. "
            f"Action risk category {category!r} \u2014 {why}. "
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
