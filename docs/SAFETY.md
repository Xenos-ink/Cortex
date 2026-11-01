# Safety — Cortex 0.7.6

The current safety model. Every live rule is enforced in a named module and
exercised by the test suite. Removed safety machinery (approval epochs, the
unattended modifier, dependency enforcement, health checks — all removed with
the internal loop) is recorded, clearly labelled, in
[history/REMOVED-LOOP-AND-INTERNALS.md](history/REMOVED-LOOP-AND-INTERNALS.md);
the per-release red-team log is
[history/RED-TEAM-v0.7.0.md](history/RED-TEAM-v0.7.0.md).
Companions: [ARCHITECTURE.md](ARCHITECTURE.md),
[TOOLS.md](TOOLS.md), [REPLY-CONTRACT.md](REPLY-CONTRACT.md),
[CONFIGURATION.md](CONFIGURATION.md).

## 1. Threat model

Deliberately pessimistic assumptions; every mechanism below exists because of
one of these lines:

1. **The model controls a desktop.** A proposed action becomes real physical
   input on a live Windows session (raw Win32 `SendInput` by default;
   `pyautogui` is the selectable fallback — both engines honor the same safety
   contract). The model is treated as a fallible, potentially manipulable
   proposer — never as an authority.
2. **Screen content is untrusted.** Screenshots, window titles, dialogs, web
   pages routinely contain text that reads like instructions, approvals, or
   authority. None of it is. No policy value in this system is derivable from
   what is visible on screen.
3. **The provider is untrusted.** The vision endpoint sits outside the trust
   boundary: its output is parsed strictly and fail-closed, its payloads carry
   no secrets and no policy internals beyond the doctrine block, and it is
   never given any capability.

Consequence: authority flows only from the operator (USER INTENT) and the local
policy (SYSTEM POLICY). Everything else is data.

## 2. Five-channel prompt doctrine (`pipeline/provider.py`)

Every model prompt is one system message of five labeled sections
(`=== CHANNEL ===` markers) plus a user message containing only the screenshot
and a fixed caption:

| Channel | Authority | Contents |
|---|---|---|
| `USER INTENT` (authoritative) | Yes | the operator's goal (redacted, clipped) |
| `SYSTEM POLICY` (authoritative) | Yes | fixed policy text + response schema; never contains screen-derived text |
| `TASK STATE` (authoritative) | Yes | controller-rendered task state |
| `MODEL SUGGESTION` (advisory only) | No | bounded history of the model's own prior decisions, redacted |
| `ENVIRONMENT CONTENT` (UNTRUSTED DATA) | No | screen-derived evidence only |

Screen text is never authority: it enters only under `ENVIRONMENT CONTENT` (or,
as the model's own prior output, under `MODEL SUGGESTION`); the policy text is
a constant; the policy instructs the model to report instruction-like content in
`suspicious_content` instead of complying. Provider output is parsed into a
fixed pydantic schema whose fields are data — nothing in it can change policy,
grant approval, or reach the stop token.

## 3. Risk classification (`security/safety.py`)

Every action is classified from action type + target text/coordinates + active
window/process identity + goal context. Unknown action types and high-risk
actions with insufficient context escalate to `CRITICAL`
(`risk_unresolvable_fail_closed`) — fail closed.

**CRITICAL** (blocked pending explicit authorization — and no MCP tool grants
it today):

| Category | Trigger patterns |
|---|---|
| `shell_execution` | powershell, pwsh, cmd.exe, `cmd /c`, `bash -c`, invoke-expression, start-process, certutil |
| `registry_write` | `reg add/delete/import/restore/load/save`, regedit |
| `disk_destructive` | `format X:`, diskpart, wipe disk; the Arabic word for "format" |
| `file_deletion` | del/rd/rmdir/remove-item/rm/erase; delete files/folders |
| `destructive_sql` | drop database/table/schema, truncate, delete from |
| `credential_change` | change/reset/remove password/pin/credentials; delete account |
| `security_change` | disable firewall/antivirus/defender/UAC; add exclusion |
| `financial_transaction` | purchase, checkout, pay/confirm payment, transfer money, enter credit card |
| `external_send` | send/forward email/message, click send, post message/comment |

**HIGH** (always requires approval): `install_uninstall`, `process_kill`,
`system_settings_change`, `elevation`, `network_config`, `wide_delete`,
`clipboard_credential` (paste password/credentials/secret/token/API key).

**MEDIUM** (approval when the session requires it): `navigation`,
`file_modification`, `window_identity_drift`, `suspicious_delete_term` (Arabic
destructive terms), `keyboard_shortcut_state_change` (keypress/hotkey with
delete/backspace/win/alt/ctrl), `window_focus_change` (`focus_window` — always
MEDIUM), `unverified_target_application` (click/type family with unknown
identity).

**LOW** (routine): `plain_text_entry`, `known_application_interaction`,
`low_routine_action` (keypress/hotkey without state-changing keys, scroll, wait,
done, move), `completion`.

Merge rules: the legacy gates run first (stopped session, step budget,
sensitive-typed-text block, state-changing keys, interactive-action approval
defaults); classification then fills risk/category/reason and the approval
requirement is **upgraded, never downgraded**. `HIGH` → requires approval;
`CRITICAL` → blocked. Dry-run semantics untouched.

Queue (`follow_ups`) is ZERO-BYPASS: every item runs the full independent
pipeline; the queue stops on TRUE premise staleness (the attached window's
identity changed or closed), safety rejection, approval requirement,
validator/grounding rejection, or a named interference event; the stop token is
checked between items. Default: an executed item whose verification is `failed`
no longer flushes later items — the honest failed verdict rides the item's
`follow_up_results` entry; `CORTEX_QUEUE_STRICT_VERIFY=1` restores
stop-on-failed.

Limitation (stated honestly): classification is deterministic pattern + context
matching (English patterns plus a small Arabic set), not semantic
understanding. Novel destructive phrasing in other languages may classify lower
than a human would. Compensations: approval defaults, allowlists, typed
outcomes with host re-grounding, verification.

### 3.1 Matching-only normalization (`core/textnorm.py`)

Zero-width/variation-selector/tatweel and invisible-separator characters split
the tokens every text matcher matches on. One canonicalizer sits between action
text ingress and the safety gate, the classifier, and secret redaction:
`view(x) = TRANSLATE(NFKC(x))` in two views (delete view removes strip
characters; fold view maps them to space), merge never-downgrade, and an ASCII
fast path that is byte-identical to the single-pass matcher. The strip set is
the 437-codepoint invisible-character family (all category Cf, variation
selectors, tatweel) plus the nine invisible non-Cf splitters. **Type-integrity
invariant**: normalization is matching-only — dispatched text keeps its original
bytes, and redaction returns the original string unchanged when no pattern
matches any view. Residual: cross-script homoglyphs are not NFKC-addressable
(future TR39-class hardening item; pinned skip in the test suite).

### 3.2 Destructive-intent grammar (`security/safety.py`)

A verb-anchored severity floor over normalized token components: V1
unambiguous destructive verbs (`delete`, `remove`, `erase`, `wipe`, `truncate`,
`purge`, `destroy`) flag with any non-function-word object in the window
(floors at MEDIUM, new category `destructive_intent`); V2 contextual verbs
(`drop`, `clear`, `empty`, `format`, `kill`, `shutdown`, `restart`) require a
high-consequence object-class hit (V1/V2 + class object floors at CRITICAL).
Function words do not consume window slots; a copula closes the window
(mention-form prose stays benign); bounded morphology (s/es/ed/ing with
e-drop/doubling) carries inflected forms of the same verb. Floors only — the
grammar can upgrade a verdict, never lower one; every pre-existing pattern
still fires first. Pinned trade: narrative past tense with a class noun flags
(over-block direction).

## 4. Fail-closed rules

| Condition | Behavior | Where |
|---|---|---|
| Unknown risk / insufficient context for a high-risk action | escalate to CRITICAL → blocked | `safety.classify` |
| Unknown action type | CRITICAL → blocked | `safety.classify` |
| Unverifiable coordinate space | grounding refuses; validator rejects; executor raises `CoordinateSpaceError` | grounding/validator/backend |
| Missing observation binding (coordinate action) | reject (`missing_observation_binding`) | `validator` |
| Stale screen identity (hwnd/process/monitor/dimensions/space drifted) | reject (`STALE_OBSERVATION`) → one automatic re-observe + re-ground, never blind execution | `validator`, `agent` |
| Uncertain verification | never success — rides the reply honestly (`ok:false`) | verification/agent |
| Policy itself raises | deny fail-closed | `agent` |
| Verification raises | degrade to `uncertain` | `agent` |
| Audit write fails | logged and swallowed — telemetry never breaks the loop | `agent` |
| Process allowlist configured but identity unavailable | reject (`process_identity_unavailable`) | `validator` |
| `focus_window` target outside either allowlist, or unresolvable while one is configured | refused BEFORE any foregrounding call (`process_not_allowed` / `window_not_allowed` / `process_identity_unavailable` / `window_identity_unavailable`) | `agent` |
| Session idle past the timeout with nothing in flight | stopped through the kill path (`source="idle_timeout"`); later calls `session_stopped` | `server/lifecycle.py` |
| Session cap reached after the idle sweep | `session_limit_exceeded` listing `live_sessions` + `idle_timeout_seconds` | `server/lifecycle.py` |

## 5. Approval semantics

- **Per-call authorization**: `computer_execute(approved=True)` authorizes that
  single call when policy requires approval. CRITICAL actions are never cleared
  by the flag: `SafetyPolicy.evaluate(..., authorized=True)` exists at the
  policy API, but NO MCP tool passes it — CRITICAL is always blocked today
  (fail safe, and honest about it).
- **In a plan**: approval is decided ONCE on the first plan call and inherited
  by every step and by `replace`; `continue` never re-asks; a gated step
  replies `not_executed` + `requires_approval:true`. Per-item safety
  classification still applies to every action.
- **Approval message contents** (asserted by test): action, target identity,
  why (category + reason), risk level, consequence, and how to approve. Screen
  content, model suggestions, and the message itself can never authorize.
- A resumed session starts with NOTHING granted — approval is never resurrected
  from checkpoint data.

## 6. Emergency stop and the idle reaper

- `StopToken` wraps `threading.Event`; `stop()` is idempotent and thread-safe;
  `ensure_live()` raises `TaskStopped`. One token per session.
- **Checked before every physical input**: on execute entry, before each click,
  before each typed chunk, before hotkeys, before a move, before scroll; waits
  are sliced (100 ms; wait-machine cancellation slices at 0.1 s) so a stop ends
  the wait within one slice.
- **One stop, one observable outcome**: `stop_session`, the idle reaper, and an
  internally-armed kill path route through the same `_close_stopped_bundle`
  cleanup; every later call on the id returns the identical `session_stopped`
  error.
- **The idle reaper**: a session with no tool call for 15 minutes (default; env
  `CORTEX_SESSION_IDLE_TIMEOUT_SECONDS`, band 60..86400, `0` disabled) and
  nothing in flight is stopped through the same kill path, audited with
  `source="idle_timeout"`; any call resets the count; sweeping is lazy (every
  tool entry + before the session-limit check). A running wait is never reaped.
- **The model can never reach the stop setter** — structural: the token lives
  only in server-held session objects; provider output is data.
- Residual honesty: this is a cooperative, in-process stop. There is no
  OS-level kill switch; if the whole process is compromised, the only physical
  backstop is the failsafe screen corner (replicated by BOTH input engines and
  mapped to `InputBlockedError`).

## 7. Secrets policy (`security/redaction.py`)

- **Detection choke point**: `redact_text` / `contains_secret` are the single
  detection point; every sink calls exactly them — the audit JSONL (write-time
  redaction + wholesale redaction under sensitive-named metadata keys), the
  provider request body, model context, checkpoints (per-value redaction plus
  the fail-closed `contains_secret` gate that REFUSES the write), resume error
  text, `safe_repr`, and the MCP response path (executed AND non-executed
  shapes: `message`, `action.text`, `action.reason`, `verification.note`,
  `verification.evidence`).
- **Patterns**: AWS access key, JWT, private-key block and header, bearer
  authorization header, standalone bearer token, basic-auth URL,
  password/passphrase assignment, token/API-key/secret assignment,
  Luhn-validated card numbers, plus value-shape families for untagged bearer
  tokens (`ghp_`-class GitHub, `github_pat_`, `xox[abprs]` Slack, `npm_`,
  `xocr_`). The assignment grammar is filler-tolerant (spaced compound nouns,
  `pwd`/`pass`, copula arms `password is/was/are`, a prose-vs-secret token-shape
  rule, at most two prose decoys, bridge-continued secret-run consumption).
  Matching runs on the canonical view(s) (§3.1); benign ASCII input is
  byte-identical pass-through.
- **Typed-text secret block**: `type` actions whose text resembles a secret,
  credential, or destructive command are denied with approval required — they
  never reach the backend. Negation is not understood ("do NOT delete
  anything" blocks — over-block direction).
- **Provider treated as untrusted**: payloads redacted before dispatch; no
  policy internals beyond the doctrine block; response size capped; raw
  responses never executed; request bodies never logged.
- **Checkpoint redaction**: every string value passes redaction; a value still
  secret-like after redaction REFUSES the checkpoint write before any disk
  touch. No secrets, no screenshots in checkpoints.
- **Screenshot redaction (honest scope)**: pattern-based for text;
  `redact_image` blurs only explicitly supplied regions; pixel-level detection
  is the no-op hook. Screenshots are not persisted to disk by the server.

## 8. Limits and resource isolation

- LIVE gates on the direct path: the action budget (`max_actions`), the
  screenshot-rate gate (`min_screenshot_interval_ms`, protecting FRESH
  captures; intra-call captures are burst-exempt but recorded), the session cap
  (`max_sessions` — refusal, never eviction), `max_wait_seconds` on wait
  requests, and the session's `max_steps` (enforced by the safety gate and the
  plan surface). Trips raise `LimitExceeded` (or a typed refusal) → fail
  closed. The remaining limits fields are RESERVED (accepted, validated,
  checkpointed; the loop-era trip points are gone — see docs/history/). Full
  table with defaults and bands: [CONFIGURATION.md](CONFIGURATION.md).
- **Unthrottled explicit observation (accepted-by-design)**: the rate gate does
  not cover `computer_observe` / `computer_screenshot` / `computer_zoom` —
  explicit client tools. Every capture emits an audit row, so a hammering client
  generates audit volume at its own discretion; it cannot bypass any safety
  gate (observation alone never acts).
- **Per-session isolation**: each session owns its context, state, backend,
  agent, enforcer, audit logger, and metrics. No shared mutable state between
  sessions; unknown ids fail closed (`unknown_session`). Sessions are in-process
  — isolation within one process, not OS/VM isolation.

## 9. Windows identity, allowlists, and the launch truth

- **Strong identity** (`desktop/backend.py`): hwnd (root owner), pid, process
  name (exe basename), exe path, window class, title, bounds. Field failures
  degrade to `None`, never crash observation.
- **Process allowlist**: authoritative when the OS reports process identity;
  an active process outside the list is a violation even when the title matches
  (case-insensitive, `.exe`-tolerant); identity unavailable while an allowlist
  is configured is a fail-closed rejection.
- **Focus pre-foreground gate**: a `focus_window` target is resolved through
  `find_window_by_title` and checked against BOTH allowlists BEFORE any
  foregrounding call; all four rejection paths reuse the validator's typed
  shape; the backend never runs on a disallowed target.
- **Launch truth (ensure_app)**: the enforced default is
  `launch="server"` — `ensure_app` MAY spawn a target process server-side when
  no existing instance matches. When NO process allowlist is configured, any
  resolvable target may launch; when one IS configured, only an allowlisted
  target may. `launch="driver"` (or env `CORTEX_ATTACH_OR_LAUNCH=driver`)
  restores never-launch. The launch is never silent: the payload carries
  exactly one typed `ensure_app_outcome` (`launched` / `launch_rejected` /
  `launch_unresolved` / `reattached` / `ambiguous` / `not_found`), and a real
  spawn is prefixed `LAUNCHED` in the message. The needle→path mapping
  (`CORTEX_LAUNCH_PATHS`) is LOCAL TRUSTED CONFIGURATION in the host env block;
  malformed entries are skipped fail-safe and nothing is ever spawned from one.
- **Staleness binding**: the fresh pre-execution observation must match the
  grounding-source observation on hwnd, process, monitor identity, screenshot
  dimensions, and coordinate space; drift is `STALE_OBSERVATION` → one
  automatic re-observe + re-ground, then the typed rejection to the host.

## 10. Known residual risks (honest)

1. **No OS-level sandbox.** No VM, job object, or AppContainer isolation; a
   sufficiently wrong action can still affect the machine between the policy
   check and the input.
2. **Coordinate safety rests on measured coordinate-space verification**,
   including the fail-closed `dpi_estimated` path.
3. **Text-level verification needs text data**: `text_predicate` is opt-in and
   inert without OCR regions; the UIA read covers control text where the box
   exposes it.
4. **Single-monitor e2e only.** The live-desktop suite passed on a
   single-monitor reference box; multi-monitor logic is covered by unit tests
   with fake monitor sets.
5. **Risk classification is heuristic** (regex + context, English + limited
   Arabic); novel phrasings may under-classify. Compensations listed in §3.
6. **CRITICAL authorization is not operator-wired** — always blocked, never
   approvable.
7. **Model-judge quality is external**; deterministic strategies always run
   first; a judge's `uncertain` is passed through, never upgraded.
8. **Pixel-diff blind spots below both counters** (sub-threshold, low-contrast,
   sparse changes) stay `uncertain` — never a false success. Fine-grained
   application-internal state is best verified with application-state
   strategies (injectable via the documented protocol).
9. **No pixel-level screenshot secret detection** (text patterns + explicit
   region blur only).
10. **In-process stop only** (§6 residual note).
11. **Provider key handling assumes a local operator** (process environment).
12. **Explicit observe calls are unthrottled** (§8).
13. **Windows foregrounding is best-effort.** `SetForegroundWindow` can be
    refused; `focus_window` detects the refusal and fails with a typed
    `WindowFocusError`; the previously-focused window is NOT restored on a
    refusal — the host re-observes and re-decides. Deterministic verification
    means a refused focus can never be reported `verified`.
14. **Type-integrity degradation**: controls that cannot expose a readable
    value degrade to honest `unverified` and never double-apply; a confirmed
    still-wrong buffer fails with `TextIntegrityError`. The one-off OS
    input-stack wedge class is bounded by the pacing knobs
    (`CORTEX_FOCUS_SETTLE_SECONDS`, `CORTEX_KEY_DISPATCH_GAP`) and surfaces as
    a typed failure, never a silent partial dispatch.
15. **Accepted red-team trade classes (bounded, named, test-pinned)**:
    Cyrillic/mixed-script homoglyphs (fail-open; needs TR39-class mapping);
    seedless commit-key adoption of the next titled window (fail-open,
    adjudicated — allowlists still gate dispatch, the bound is one action);
    the `sk_live_`/`rk_live_`/`pk_live_` stripe family matches no pattern yet
    (adding it is additive); pure-lowercase ≤ 10 copula values treated as
    prose (fail-open boundary, digits/capitals/symbols defeat it); capitalized
    +digit tokens are secret-shaped (fail-closed by ruling — some prose
    over-blocks); decorated prose near marker vocabulary hard-blocks
    (over-block by design); narrative past tense with a class noun flags;
    the fusion end-boundary omission (INFO; the delete view covers every
    whole-token reading). Outside-vocabulary imperatives and typo'd verbs
    remain unflagged by the no-stemming closed-vocabulary design. The
    adversarial cycle that adjudicated these is recorded in
    [history/RED-TEAM-v0.7.0.md](history/RED-TEAM-v0.7.0.md).
16. **Clipboard abort path**: when an abort (failsafe, stop token, blocked
    input) lands between the clipboard set and the paste chord, the prior text
    is restored (best effort) but a destroyed non-text payload is not disclosed
    and a failed restore is not annotated. Recorded in VERSIONS.md; fixing it
    needs an annotation channel on the abort path.

## 11. Interference Guard

The Interference Guard only ADDS fail-closed behavior; no existing gate,
allowlist, approval, or limit is weakened and no new bypass exists.

| Concern | Behavior |
|---|---|
| Foreign foreground at dispatch | REJECTION (`FOCUS_TAKEN_BY`); `refocus_then_abort` reuses the verified foreground switch aimed only at the bound title; `observe_only` annotates |
| Bound window closed | `TARGET_GONE` + binding cleared (default) — no infinite reject loop against a dead identity |
| Modal dialog after an action | reported with its control list; queued batches HALT (`modal_dialog`); auto-handling ships DISABLED and resolves only the exact configured identity with an audit event per resolution |
| Keyboard focus outside the target | `FOCUS_DRIFTED` rejection before dispatch; mid-type drift aborts the in-flight type (carrying `partial_delivery`); terminal keys are never auto-resent (double-submit risk) |
| Stuck modifiers before a chord | `STUCK_MODIFIER` rejection; opt-in `release` touches ONLY modifiers this session dispatched |
| ensure_app launches | server-side launch requires the `launch="server"` policy (the DEFAULT) — the allowlist restricts ONLY when one is configured (see §9 launch truth); `CORTEX_ATTACH_OR_LAUNCH=driver` or `launch="driver"` restores never-launch |
| Launch outcome honesty | the payload carries exactly one typed outcome (`launched` / `launch_rejected` / `launch_unresolved=path-lookup-missed` / `launch_unresolved=spawn-failed (<error>)`); `launch=driver` payloads carry no launch status and never spawn |
| Malformed launch mapping | skipped fail-safe: never fatal, nothing spawned FROM a malformed entry |
| Policy parsing | fail-closed (`invalid_interference`) — a malformed policy can never silently disable protection |

**Launch-act / re-anchor policy**: the session anchor is adopted only from
session surfaces (hwnd membership, same-pid descendant, owner-chained dialog);
everything else is refused with `REANCHOR_REFUSED` (annotation-only, anchor
kept). The keyboard launch-act marker arms ONLY for a commit-key chord
(`enter`/`return`/`numpadenter`) into a launcher/dialog anchor and only after
the pre-dispatch gates pass — a REJECTED chord never dispatches and is never a
launch act. A TYPE into the launcher records bounded seed tokens; a seeded
launch act adopts only a candidate whose process/title correlates with the
seed. A seedless pure commit key keeps the documented adoption shape (the
adjudicated residual in §10). A dead anchor unbinds via `TARGET_GONE`;
reattachment happens only by explicit identity (`ensure_app`/`focus_window`).
