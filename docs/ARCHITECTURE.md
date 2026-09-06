# Architecture — Cortex

Status: production-hardening Waves 1–5 landed (including the E6 defect round D1–D10 and
the red-team fix round F1–F7), followed by the Cortex naming pass, the DRAG action, the
compact-change verification upgrade, and the move/hotkey/focus_window actions (full
pipeline support, allowlist-gated focus, deterministic verification). This document
describes the code as it exists
at the open-source release HEAD; every claim is traceable to a named module under
`src/computer_use_mcp/` (or `benchmarks/`, `tests/e2e/`) and, where noted, to the test
suite (standard suite: **496 passed, 7 skipped** — the skips are the gated
real-Windows E2E desktop tests; `ruff check src tests benchmarks` clean at HEAD;
observed on the reference machine). The layered rules and pinned contracts come from the
mission architecture (master mission §5–§6); where reality differs from the plan, this
document records reality.

## 1. Scope and shape

- Single Python package, `src/` layout, stdio MCP server built on FastMCP (`server.py`).
  The MCP server registers under the name `Cortex` (`FastMCP("Cortex")`); the pip
  package stays `computer-use-mcp`, the Python package `computer_use_mcp`, and
  `pyproject.toml` installs two console scripts — `cortex` and `computer-use-mcp` —
  pointing at the same entry point (`computer_use_mcp.server:main`).
- Windows-first execution (`LocalComputerBackend`); a faithful in-memory
  `FakeComputerBackend` implements the same contracts for tests and non-Windows import.
- Python >= 3.11; runtime deps: `mcp`, `pydantic`, `Pillow`, `mss`, `pyautogui`
  (win32), `httpx`. No OCR/UIA engines are installed (deliberate non-goal; see §12).
- Version: `0.2.0` (`__init__.py`; `pyproject.toml` aligned to the same value, F6).
- Test/benchmark layout: `tests/` unit+integration (fakes); `tests/e2e/` real-Windows
  E2E gated behind `CUMCP_RUN_E2E=1` (10 tests: 7 desktop + 3 benchmark-harness that
  run unconditionally); `benchmarks/` harness with `--mode fake` / `--mode env`
  (NO score claims). E2E runs generate evidence locally under `evidence/e2e/` (see
  §13); those artifacts are machine-local test outputs, not shipped documentation.

## 2. Module map (verified import graph)

Hard layering rules (enforced by review, honored by code): nothing below the controller
layer imports `agent` or `server`; `provider` does NOT import `verification` —
model-based visual verification is a `ModelJudge` callback interface implemented by the
controller (`agent._provider_judge` adapts `provider.judge_change`), breaking the cycle.

```
models.py          universal pydantic types          imports: (stdlib only)
redaction.py       secret detection/redaction        → models
state.py           StopToken, TaskState,             → models
                   SessionContext/Registry
limits.py          Limits + LimitEnforcer            → (stdlib only)
audit.py           AuditEvent JSONL sink, Metrics    → redaction
backend.py         Win32 identity/DPI/monitors,      → models, state (StopToken)
                   coordinate integrity, window-title
                   lookup (find_window_by_title),
                   stop-checked input, fakes
observation.py     capture orchestration + digest    → backend, models
grounding.py       GroundingStrategy protocol +      → models
                   coordinate/region impls + OCR/UIA stubs
validator.py       staleness/binding/allowlists      → models
verification.py    6 verification strategies         → models (+PIL)
provider.py        doctrine prompt, fail-closed      → models, redaction
                   parsing, lazy key, judge endpoint
safety.py          RiskLevel contextual engine       → models
recovery.py        FailureClass taxonomy + bounded   → limits, models, state
                   RecoveryController
agent.py           closed-loop phase machine         → audit, backend, grounding,
                   (ComputerUseAgent)                  limits, models, observation,
                                                       recovery, safety, state,
                                                       validator, verification
server.py          6 MCP tools + wiring              → agent, audit, backend,
                                                       limits, models, provider,
                                                       safety, state (+ mcp SDK)
```

Note: the plan sketched `limits → models, state`, `grounding → observation`, and
`validator → grounding` edges; the landed code keeps `limits`, `grounding`, and
`validator` free of those imports (they depend on `models` only or stdlib). The graph
above is measured from the actual imports.

## 3. Pipeline → module mapping

| Pipeline stage | Module(s) | Notes |
|---|---|---|
| Observation | `backend.py`, `observation.py` | `observation_id`, UTC `timestamp`, `MonitorInfo` (bounds/primary/DPI), `WindowInfo(hwnd,pid,process_name,exe_path,window_class,title,bounds)`, cursor localized to screenshot space, coordinate-space classification |
| State construction | `state.py` | `TaskState` (goal, subgoal, plan_notes, histories as bounded deques, budgets, termination reason), `StopToken`, `SessionRegistry` (max 4, fail-closed refusal) |
| Action proposal | `provider.py` | `decide_full` → `ProviderDecision`; five-channel doctrine prompt; strict pydantic parse; fail-closed on any garbage |
| Perception/Grounding | `grounding.py` | `GroundingRouter` over `GroundingStrategy` implementations: coordinate (real), region descriptor (real), text-anchor (P1 stub), accessibility (P1 stub); fail-closed `UnsupportedGroundingError`. `NON_SPATIAL_ACTIONS` covers type, keypress, **hotkey** (carries `keys`), scroll, wait, done, **focus_window** (carries `target`) — grounded trivially with strategy `none`; point-bearing actions (click/double_click/drag/**move**) route to the coordinate strategy |
| Action validation | `validator.py` | bounds, confidence floor, coordinate-space refusal, window/process allowlists, staleness + `source_observation_id` binding (`COORDINATE_ACTIONS` = click/double_click/drag/**move** — `move` binds its point like click); `missing_text`/`missing_keys`/`missing_target` (focus_window requires a non-empty `target`) |
| Risk classification + policy + approval | `safety.py` | contextual LOW/MEDIUM/HIGH/CRITICAL; approval upgrade never downgraded; CRITICAL blocked pending explicit authorization; structured approval messages |
| Execution | `backend.py` | `execute(action, stop)` — `StopToken.ensure_live()` before every physical input and between typed characters; interruptible 100 ms-sliced waits |
| Post-action observation | `observation.py` | fresh capture, new `observation_id`; rate-gated by `min_screenshot_interval_ms` |
| Semantic verification | `verification.py` | `VerificationStrategy` chain; outcome ∈ {verified, failed, uncertain}; first definitive wins; all-uncertain combines into `uncertain` |
| Recovery/Replanning | `recovery.py` + `agent.py` | FailureClass → bounded plan; re-observe + re-ground; never blind same-coordinate retry |
| Audit/Telemetry | `audit.py` | per-session JSONL with write-time redaction; `Metrics` counters + latency percentiles |
| Limits | `limits.py` | 9 limit classes enforced centrally in `agent.py` (+ session cap in `server.py`/`state.py`) |

## 4. Closed-loop phase machine (`agent.py`)

`ComputerUseAgent.run(goal, state, approval)` executes one task; `run_single(state,
action, approved, expected_effect)` runs one client-supplied action through the same
phases (`computer_execute`). Per iteration of `run`:

1. **Loop-top checks** — stop token (`ensure_live`), task duration (`check_task_duration`).
2. **OBSERVE** (`_observe`) — rate-gated fresh capture; while waiting for the
   screenshot gate the stop token is polled every 50 ms (max 2 s, then `LimitExceeded`).
   Audited `observation`; `screenshot_count` + `observation_ms` recorded.
3. **DECIDE** (`_decide`) or reuse a `pending` action from recovery — model call
   wrapped fail-closed: any provider exception is audited, consumes the step, and
   classifies as `LOW_CONFIDENCE` recovery (never raises out of `run`). After the call
   the stop token is re-checked: a user stop outranks a just-arrived model decision.
   Decision points: `status == "done"` → COMPLETED (honestly marked, F3: the
   synthetic verification states the outcome is MODEL-ASSERTED with
   `completion_evidence="model_declared"`, an explicit no-independent-evidence note,
   and an audited `verification` event with result `model_declared`);
   `status == "blocked"`/no action →
   UNRECOVERABLE (a deliberate refusal is not replanned); otherwise continue.
4. **GROUND** (`_ground`) — route grounding, attach `GroundingResult`, and for
   coordinate actions bind `source_observation_id` to the grounding-source observation.
   Failure → recovery.
5. **VALIDATE** — a second fresh pre-execution observation is captured and
   `GroundingValidator.validate(..., current_observation=...)` enforces staleness
   against it (HWND/process/monitor/dimensions/space). The validation capture checks
   screen identity, not pixels, and never becomes the verification baseline.
   Rejection → recovery (`codes` → FailureClass). When either allowlist is
   configured and the action is a `focus_window`, the resolved target window is
   checked against BOTH **before** anything executes (`_focus_allowlist_rejection`):
   process/exe outside `allowed_processes` → `process_not_allowed`; title outside
   `state.allowed_windows` (casefolded exact-or-substring, mirroring the validator's
   `_window_allowed`) → `window_not_allowed`; an unresolvable target →
   `process_identity_unavailable` (process allowlist configured) /
   `window_identity_unavailable` (title allowlist configured) — all fail closed with
   the validator's typed rejection shape and `WRONG_WINDOW` recovery mapping, and the
   backend never runs on a disallowed target.
6. **RISK/POLICY** (`_evaluate_safety`) — builds `SafetyContext` (goal, window/process
   identity, recent actions) and evaluates; a raising policy denies fail-closed.
   Denied → `BLOCKED_SAFETY` termination.
7. **APPROVAL** — when `requires_approval`: an approval already granted for THIS
   action instance (`_approved_action_ids`) is honored without re-consuming budget;
   otherwise the callback decides. Denied/unavailable → `APPROVAL_EXHAUSTED`.
8. **EXECUTE** — dry-run short-circuits with a stub result (`verified=False`, explicit
   note). Otherwise `check_action`, stop check, `backend.execute(action, stop_token)`.
   Execution exception → recovery.
9. **RE-OBSERVE + VERIFY** (`_build_intent`, `_verify`) — fresh post-action
   observation; verification baseline is ALWAYS the grounding-source observation (the
   pre-action one), fixing the prototype's baseline-shift bug. `verified` → continue;
   `uncertain` on a `wait` action → continue with audited note (the single documented
   carve-out); otherwise failure/uncertain → recovery.
10. **RECOVER/REPLAN** (`_handle_failure`) — classify → `RecoveryPlan` → loop control
    (see §6).
11. **Terminal handling** — `finally` always: `task.terminate(reason)`, `task_ms`
    latency, `session_stop` audit event with termination reason.

Verification-intent defaults (`_build_intent`; an explicit hint naming a
`VerificationKind` always wins): `type` → `expected_text`; `move` → `predicate`
(`cursor_at_target` — the after-observation cursor must sit within ±2 px
(`_CURSOR_TOLERANCE_PX = 2`) of the requested point on both axes; missing cursor
fields yield `None` → `uncertain`, never success); `focus_window` → `window_state`
(the active window title must contain `action.target`, case-insensitive — deterministic
via `active_window_info`, never pixels); `keypress`/`hotkey` whose expected effect
starts with a launch prefix ("open ", "launch ", "start ", "switch to ", "focus ") →
`window_state`; everything else (click/double_click/drag/scroll/wait, and hotkey
without a launch-prefix effect) → `visual_change`. When an expected effect is stated, a
change is REQUIRED (unchanged screen = failed); with no stated expectation, pixels
alone stay ambiguous (identical screen → `uncertain` → recovery).

Outer guards: `TaskStopped` → `STOPPED_BY_USER` + `emergency_stop` audit;
`LimitExceeded` → `LIMIT_EXCEEDED` + `limit_exceeded` audit; any other exception →
`UNRECOVERABLE` with a structured fail-closed result (never propagates out of `run`).
Loop exhaustion without termination → `LIMIT_EXCEEDED`.

`TerminationReason` (8 values, `models.py`): `completed`, `failed_verification`,
`blocked_safety`, `approval_exhausted`, `limit_exceeded`, `stopped_by_user`,
`unrecoverable`, `provider_error`.

## 5. Pinned contracts

**Observation** (`models.py`): legacy fields kept (`image_base64`, `width`, `height`,
`active_window` (title), `cursor_x/y`, `input_width/height`,
`coordinate_scale_x/y`, `coordinate_space_verified`, `redactions_applied`); added
`observation_id` (uuid hex), `timestamp` (UTC), `coordinate_space`
(`verified_passthrough` | `scaled` | `unverifiable` — synced with the legacy boolean by
a model validator, enum wins when provided), `monitor: MonitorInfo|None` (id, index,
bounds `(left,top,w,h)` virtual-screen, is_primary, dpi_scale_x/y),
`active_window_info: WindowInfo|None`, plus P1 stubs `ocr_text: list[TextRegion]|None`
and `ui_elements: list|None` (always `None` in P0).

**GroundedAction lineage**: every action instance carries `action_id` (uuid, default)
for auditing and approval binding. Coordinate actions carry
`source_observation_id` (set during GROUND) — the validator rejects coordinate actions
without it or when the fresh observation drifted (`missing_observation_binding`,
`observation_binding_mismatch`, `STALE_OBSERVATION`). `drag` actions carry both
endpoints in screenshot space (`point` is the start, `to_point` the end; construction
fails closed unless both are present) and are treated as coordinate actions: grounding
bounds-checks both endpoints, the validator bounds-checks both, and the safety policy
classifies drag like click/double-click. `move` requires a `point` (screenshot space);
`hotkey` requires 2–12 non-empty key names (a compound chord — single-key presses
deliberately stay on `keypress`); `focus_window` requires a non-empty `target` (max
200 chars) — a dedicated window-identity selector, deliberately NOT `text` (`text` is
the redaction/secret-scan channel; `target` is audited and allowlist-gated separately).
All three fail closed at construction. `risk` is filled only by the
safety classifier, never by the model; `grounding` holds the `GroundingResult`
(strategy, confidence, evidence, normalized).

**Coordinate-transform invariant (F1)**: exactly one screenshot→physical transform
exists in the pipeline — `ComputerBackend._map_to_physical`
(`physical = origin + screenshot * scale` via `CoordinateTransform`), applied exactly
once at execution. Grounding validates bounds in screenshot space and records the
verified scale on the result (`GroundingResult.normalized=True` means "validated in a
verified scaled space; scale recorded" — NOT that the point was rewritten); no
grounding strategy pre-scales a point and no consumer re-scales one, or the executed
position would land at `origin + screenshot * scale**2` (the defect F1 eliminated).
Strategies that *derive* a target (region / text anchor / accessibility) set the point
in place, always in screenshot space.

**VerificationResult**: `outcome` ∈ {`verified`, `failed`, `uncertain`} is
authoritative; `verified` is a derived serialized property equal to
`outcome == "verified"`; the legacy constructor form `VerificationResult(verified=…)`
is translated. Added `evidence: list[str]`, `verification_method: str`,
`observation_id`. UNCERTAIN is never success anywhere (asserted by a dedicated test).
The `screenshot_diff` strategy counts a screen as changed when the mean pixel
difference reaches its threshold (`DEFAULT_DIFF_THRESHOLD = 1.0`) **or** at least
`STRONG_CHANGE_MIN_PIXELS = 50` pixels changed by `STRONG_PIXEL_DELTA = 40` or more
per channel — a compact strong change (a drawn stroke, a redrawn digit, a small
control) verifies even when the screen-wide mean barely moves, while sub-threshold
flicker (caret, clock tick) stays ambiguous/`uncertain`.

**Confidence separation** (four distinct quantities, carried end-to-end):

| Quantity | Where | Reported in |
|---|---|---|
| Model confidence | `AgentDecision.action.confidence` (client-asserted for direct calls — `computer_execute` hardcodes `1.0`, redefined, see §10) | `model_confidence` |
| Grounding confidence | `GroundingResult.confidence` (coordinate sanity passthrough, or OCR/element match quality) | `grounding_confidence` |
| Execution success | `ExecutionResult.ok` | `ok` |
| Verification confidence | `VerificationResult.confidence` (evidence-grounded per strategy) | `verification_confidence` |

**SafetyDecision**: `allowed`, `requires_approval`, `reason` (legacy order preserved)
plus `risk: RiskLevel|None`, `category: str|None`. `SafetyPolicy.evaluate(action,
state[, context])` keeps the legacy signature; `classify(action, context)` is the
contextual engine. See `docs/SAFETY.md` for the taxonomy.

**ComputerBackend**: `observe() -> Observation`; `execute(action, stop: StopToken|None)
-> str` — implementations must call `stop.ensure_live()` immediately before every
physical input; a fired token raises `TaskStopped` with zero inputs performed.
`find_window_by_title(target) -> WindowInfo | None` resolves a top-level window by
title — case-insensitive matching with precedence exact > prefix > substring
(`_title_match_rank`), the first candidate in Z-order winning within a match class —
and populates the winner exactly like `query_foreground_window`; backends without
window enumeration return `None` (the agent-level focus allowlist gate then fails
closed with `process_identity_unavailable`).

## 6. Recovery taxonomy (`recovery.py` + `models.FailureClass`)

`classify_failure` recognizes sibling exceptions structurally (by class name: 
`InputBlockedError`, `CoordinateSpaceError`, `StaleObservationError`,
`DisplayUnavailableError`, `UnsupportedGroundingError`, provider errors) and validator
`codes` — recovery stays below the controller and imports no siblings.
`TaskStopped` classifies to `None`: a stop is the kill path, never a failure.

| FailureClass | Strategy | Behavior | Bounds |
|---|---|---|---|
| `STALE_COORDINATES` | `RECOVER_REOBSERVE` | re-observe, discard failed coordinates, re-decide with fresh grounding | recovery budget (below) |
| `MOVED_UI` | `RECOVER_REOBSERVE` | same | recovery budget |
| `WRONG_WINDOW` | `RECOVER_REOBSERVE` | same | recovery budget |
| `UNEXPECTED_DIALOG` | `RECOVER_REOBSERVE` | same | recovery budget |
| `NAVIGATION_DRIFT` | `RECOVER_REOBSERVE` | same | recovery budget |
| `BLOCKED_UI` | `RECOVER_DISMISS` (policy-probed) else `REPLAN` | one bounded Escape-key dismiss via the stop-checked backend, then retry the same instance; if dismissal not permitted/failed → re-decide | recovery budget + dismiss is single-shot |
| `APP_CRASH` | `REPLAN` | re-decide from fresh state | recovery budget |
| `LOW_CONFIDENCE` | `RETRY_ONCE` | same approved action instance with fresh grounding + validation, then replan | recovery budget + `max_retries_per_action` |
| `AUTH_REQUIRED` | `TERMINATE_SAFELY` | credentials are never auto-typed (hard rule); terminates `BLOCKED_SAFETY` | — |
| `ALREADY_COMPLETED` | `COMPLETE` | goal state already reached → `completed` | — |
| `UNRECOVERABLE` | `TERMINATE_SAFELY` | `unrecoverable` | — |
| `UNKNOWN` | `TERMINATE_SAFELY` | fail closed, `unrecoverable` | — |

Bounds: `max_recovery_per_action = 2`, `max_recovery_per_task = 6` (defaults). Budget
exhaustion → `TERMINATE_SAFELY` with the phase-mapped termination reason
(`decide` → `provider_error`, `verify` → `failed_verification`, else
`unrecoverable`). Recovery retries of an approved action instance do not re-consume
the `run_goal` approval budget (bound by `action_id`, see §10). Dismiss accounting
(D6): every dismiss attempt increments `recovery_dismiss_total` and `action_total`;
a successful dismiss also consumes action budget (`record_action`) while a failed one
increments `action_failure` only — keeping `action_total == action_success +
action_failure`. Dismiss inputs are never gated by `check_action()`: the recovery
machinery must not be blockable by the model-action budget (the bounded recovery
budget gates it instead).

## 7. StopToken / kill path

- `StopToken` (in `state.py`) wraps `threading.Event`: `stop()` idempotent and safe
  from any thread; `ensure_live()` raises `TaskStopped`; `wait(timeout)` supports
  interruptible sleeps.
- One token per session, created in `SessionRegistry.create()` and held in
  `SessionContext`; the server arms it in `stop_session` (which also sets
  `state.stopped` and audits `stop` + `emergency_stop`).
- Checked at: loop top; before each provider call; during the screenshot-rate wait
  (50 ms polling); before validation capture; immediately before execution; inside
  recovery dismiss attempts; before and between every physical input in
  `backend.execute` (per typed character); inside waits sliced at 100 ms
  (`_sleep_for_wait_action`, `interruptible_wait`).
- **The model can never reach the stop setter.** Enforcement: the token lives only in
  server-held session objects and the agent; the provider receives plain data (redacted
  strings, the pydantic `Observation`, `list[str]` history) — no object graph reaches
  the token; provider output is parsed into a fixed-schema pydantic decision (data, no
  callables); the agent re-checks the token right after the model responds so a model
  "done" cannot outrank a stop; the approval callback receives only
  `(GroundedAction, str)` copies. A thread-based test asserts zero further inputs after
  a mid-run stop, and a dedicated test asserts the setter is unreachable from provider
  output.

## 8. Limits (`limits.py`)

Defaults per master mission §6; `Limits.validate()` clamps into safe ranges and
`LimitExceeded` (carrying the field name) terminates the task cleanly.

| Limit | Default | Clamp range | Enforced by |
|---|---|---|---|
| `max_task_seconds` | 900.0 | 1..3600 | `check_task_duration` (monotonic clock) |
| `max_actions` | 100 | 1..500 | `check_action` before execution |
| `max_retries_per_action` | 5 | 0..5 | `check_retry` in same-instance retry |
| `max_recovery_per_action` | 2 | 0..10 | recovery budget check |
| `max_recovery_per_task` | 6 | 0..50 | recovery budget check |
| `max_model_calls` | 60 | 1..1000 | `check_model_call` before DECIDE |
| `min_screenshot_interval_ms` | 250 | 0..60000 | rate gate in `_observe` (wait ≤ 2 s then fail) |
| `max_context_items` | 50 | 1..1000 | `check_context_items` before provider call |
| `max_sessions` | 4 | 1..64 | `SessionRegistry` (fail-closed refusal, no eviction) + `check_session_count` |

`start_session(limits=…)` accepts a dict of these field names; unknown names or
non-numeric values are rejected fail-closed (`invalid_limits`).

## 9. Audit + metrics (`audit.py`)

**AuditEvent schema** (Goal.md §17 field set): `timestamp` (UTC), `session_id`,
`task_id`, `observation_id`, `action_id`, `event_type`, `active_app` (process name,
title fallback), `risk`, `result`, `duration_ms`, `metadata` (dict, redacted).

Event types (15): `observation`, `model_decision`, `grounding`, `validation`,
`safety`, `approval`, `execution`, `verification`, `recovery`, `failure`, `stop`,
`emergency_stop`, `limit_exceeded`, `session_start`, `session_stop`.

Sink: one JSONL file per session (`audit_<sanitized-session-id>.jsonl`) under
`COMPUTER_USE_MCP_LOG_DIR` (server default: `<temp>/cortex/logs/<session_id>/`).
Redaction is enforced at write time — every string field and metadata value passes
`redact_text`; values under obviously sensitive metadata keys (password/token/key/
secret/credential/auth/cookie, word-bounded) are redacted wholesale. Audit write
failures never break the control loop (logged, swallowed).

**Metrics** (`Metrics.snapshot()`): registered counters — `task_started`,
`task_completed`, `task_failed`, `action_total`, `action_success`, `action_failure`,
`verification_verified`, `verification_failed`, `verification_uncertain`,
`grounding_failure`, `safety_block`, `approval_requested`, `approval_granted`,
`approval_denied`, `retry_total`, `recovery_total`, `recovery_success`,
`model_calls`, `screenshot_count` — plus `recovery_dismiss_total`, created on first
use by the BLOCKED_UI dismiss path (unknown counters are created dynamically).
Latencies (bounded 1024-sample deques with
count/avg/p50/p95/max): `observation_ms`, `model_ms`, `execution_ms`,
`verification_ms`, `task_ms`. `run_goal` returns the full snapshot per call.

## 10. Backward-compatibility decisions (binding)

1. **Tool surface**: the 6 tool names, stdio transport, and parameter positions are
   preserved; additions are trailing optional parameters only —
   `start_session(..., allowed_processes=None, limits=None)` and
   `computer_execute(..., expected_effect=None, x2=None, y2=None, target=None)` (the
   trailing `x2`/`y2` carry the drag end point; the trailing `target` carries the
   focus_window window title). (`computer_observe` gained no new
   parameter in the landed code.) Response shapes keep their top-level keys; new
   fields are additive.
2. **`computer_execute` `confidence=1.0` redefinition**: retained and redefined as the
   client-asserted *model confidence* for a direct caller action (not a model
   proposal). Grounding confidence, coordinate-space verification, staleness, risk
   classification, approval, and verification apply independently; the response now
   reports `model_confidence` / `grounding_confidence` / `verification_confidence`
   separately. `expected_effect` opts into semantic verification: a stated effect must
   be observed or the result reports failed/uncertain — never silently successful.
   Documented degradation: direct calls whose intent is `expected_text` with no OCR
   evidence fall back to the deterministic visual-change check; the outcome still
   comes from evidence and uncertain is never upgraded.
3. **`run_goal` approval budget**: exactly `approval_budget = 1` per call when
   `approve_next_action=True`. The grant is bound to the approved action *instance id*
   (`_approved_action_ids`), so bounded recovery retries of the same instance never
   re-consume budget; a new distinct action after exhaustion is denied fail-closed and
   surfaces via `requires_approval: true` in the response.
4. **`stop_session`**: signature and return shape unchanged; now arms the thread-safe
   StopToken kill path and audits `stop` + `emergency_stop`.
5. **Lazy provider key**: `start_session` never requires an API key. The server wraps
   the provider factory in `_LazyProvider` (construction deferred to the first model
   call, failures remembered and surfaced fail-closed at decide-time), and
   `OpenAICompatibleVisionProvider` resolves the key lazily (`api_key` argument →
   `VISION_API_KEY` → `OPENAI_API_KEY`), raising typed `ProviderError` on first use
   without a key. This is a dry-run usability fix, not a break.
6. **State compat**: `SessionState` fields, `step_count` persistence across calls,
   the `computer_screenshot` alias, `computer_observe`'s
   `{observation, digest, observation_id, active_app}` shape, and the
   `Observation`/`GroundedAction`/`VerificationResult` legacy fields/constructors all
   survive (the 10 pre-mission tests never regressed through Waves 1–3).

## 11. MCP tool reference

Error convention: typed failures return structured payloads `{"ok": false, "error":
<code>, "message": …}` (codes: `task_stopped`, `limit_exceeded` + `limit`,
`session_limit_exceeded` + `max_sessions`, `session_stopped`, `unknown_session` +
`session_id`, `invalid_limits`, `invalid_action`, `action_error`) — no tracebacks
leak. Stopped sessions (F7) refuse identically regardless of which flavor armed the
stop: `stop_session` and an internally-armed kill path both route through the shared
`_close_stopped_bundle` cleanup (bundle removed from the store AND the registry, a
bounded snapshot retained in `_stopped_sessions`, capped at 1024 entries), and every
subsequent tool call on that session id returns `session_stopped`.

**`start_session(dry_run=True, require_approval=True, max_steps=30,
max_retries_per_action=1, min_confidence=0.70, allowed_windows=None,
allowed_processes=None, limits=None)`**
→ `SessionState` dump plus `allowed_processes`, `limits` (string), `task_id`.
Creates the registry entry, per-session audit dir, metrics, agent.

**`stop_session(session_id)`**
→ `{ok, session_id, message, task_id, termination_reason}`; arms the kill path via the
shared cleanup. Idempotent: a second call on an already-stopped session returns
`{ok: true, message: "Session already stopped.", task_id, termination_reason: null}`.

**`computer_observe(session_id)`** (alias `computer_screenshot`)
→ `{observation: <Observation model dump incl. identity/monitor/coordinate fields>,
digest: sha256 of the screenshot payload, observation_id, active_app}`.
**No rate gate on this explicit client tool** (measured ~46 captures/s; every capture
emits an audit row). The `run_goal` internal loop is the rate-gated path
(`min_screenshot_interval_ms`); accepted-by-design for explicit client calls — clients
wanting throttling should self-limit (F5).

**`computer_execute(session_id, action, x=None, y=None, text=None, keys=None,
delta=0, approved=False, expected_effect=None, x2=None, y2=None, target=None)`**
→ executed: `ExecutionResult` dump + the three confidence fields, with the response
path redacted (F2: `message`, `action.text`, `action.reason`, `verification.note`,
`verification.evidence` pass `redact_text` before leaving the server — the audit sink
and provider payload were already enforced; the response was the one remaining
unredacted surface);
`rejected`: `{ok:false, message:"Grounding rejected.", reasons:[…]}`
(one automatic re-observe + re-validate happens first on `STALE_OBSERVATION`);
`safety_denied`: `{ok:false, message}` (includes all CRITICAL blocks);
`approval_required`: `{ok:false, requires_approval:true, message}`.
For `action="drag"`, `x`/`y` are the drag start and `x2`/`y2` the drag end (both
required, screenshot coordinates); grounding and validation bounds-check both
endpoints, and the backend interpolates the stroke in segments with the button always
released — including on a mid-stroke stop.

The three newer actions:

- **`move`** (`x`/`y` required): a cursor reposition with no click — the single
  physical input is preceded by a stop-token check, followed by a 0.05 s settle sleep.
  `move` is a coordinate action: it grounds, bounds-checks, and binds its
  `source_observation_id` exactly like click. Classified `LOW`
  (`low_routine_action`); verified via the deterministic `cursor_at_target`
  predicate (±2 px).
- **`hotkey`** (`keys` required, 2–12 non-empty key names — enforced at construction
  and again in the backend; the validator rejects an empty list with `missing_keys`):
  a compound chord executed through pyautogui `hotkey`, stop-checked immediately
  before the single input. Single-key presses stay on `keypress`. Risk: `LOW` without
  state-changing keys, `MEDIUM` (`keyboard_shortcut_state_change`) when any key is
  `ctrl`/`alt`/`win`/`delete`/`backspace`.
- **`focus_window`** (`target` required, max 200 chars): the target title is resolved
  through `find_window_by_title` BEFORE execution — when either allowlist is
  configured, `_focus_allowlist_rejection` checks the resolved target against BOTH
  before any foregrounding call: process/exe outside `allowed_processes` →
  `process_not_allowed`; title outside `allowed_windows` → `window_not_allowed`; an
  unresolvable target → `process_identity_unavailable` (process allowlist) /
  `window_identity_unavailable` (title allowlist) — reusing the validator's typed
  rejection shape (→ `WRONG_WINDOW` recovery mapping throughout). The Win32
  sequence restores the window when minimized (`IsIconic` → `ShowWindow(SW_RESTORE)`),
  performs the standard `AttachThreadInput` foreground switch with an ALT `keybd_event`
  nudge, then re-reads `GetForegroundWindow()` — if the foreground did not actually
  move, the backend raises `WindowFocusError`, a typed `BackendError` deliberately
  absent from recovery's structural name-sets: it classifies `UNKNOWN` → fail-closed
  (`TERMINATE_SAFELY`, `unrecoverable` in the loop; `action_error` for direct calls),
  never a blind retry. Documented residual: the previously-focused window is NOT
  restored on a refusal — the caller re-observes actual state and re-decides.
  Classified `MEDIUM` (`window_focus_change`) always; verified via deterministic
  `window_state` (active-window title contains the target), never pixels.

**`run_goal(session_id, goal, approve_next_action=False)`**
→ `{ok, approval_budget_remaining, results: [ExecutionResult…], session_id, task_id,
termination_reason, stopped, requires_approval, step_count, metrics}`. Refinements:
`ok` is `bool(results) and all(...)` — an empty result list is NOT a success (D2);
each result may carry `suspicious_content` (the provider's injection report for that
action, persisted as data on the agent, capped at 200 entries, redacted at the audit
sink — D3) and `completion_evidence: "model_declared"` whenever the result's
verification came from the provider-done path (F3: model-asserted completion is
labeled, never presented as an evidenced check — see §5/§4); every result payload is
redacted on the way out (F2). If an internally-armed kill path terminated the run,
the same bundle hygiene as `stop_session` applies before returning (F7).

**`stop_session` / registry limits**: sessions are capped (`max_sessions`, default 4);
a refused create returns `session_limit_exceeded`.

Test/extension seam: module-level `_backend_factory` / `_provider_factory` are invoked
per `start_session`; tests monkeypatch them and inspect wiring via `_get_bundle`.

## 12. Extension points (P1)

- **OCR grounding/verification**: populate `Observation.ocr_text` (list of
  `TextRegion`, screenshot-local coordinates). `TextAnchorGroundingStrategy` (grounding
  by text match) and `TextPredicateStrategy` (expected-text verification) activate
  automatically from data; until then they fail closed with
  `UnsupportedGroundingError` / `uncertain` and the runtime degrades gracefully to
  coordinate grounding — the graceful degradation itself is the P0 deliverable.
- **UIA/accessibility grounding**: populate `Observation.ui_elements` (tolerant
  element schema: name/text/title, optional role, bounds). 
  `AccessibilityGroundingStrategy` activates from data.
- **Model-based visual verification**: inject a `ModelJudge` into
  `VerificationEngine(strategies=…, judge=…)` (`ModelVisualStrategy`), or rely on the
  controller adapting `provider.judge_change` when an intent of kind `model_judge` is
  requested (the default path; degrades to `uncertain` without a configured provider).
- **Application-specific verification strategies**: the `VerificationStrategy` protocol
  + `VerificationEngine` injection seam is proven in production conditions by the E2E
  suite, which injects `WindowTextPredicateStrategy` (real Edit-control read via
  `WM_GETTEXT`) and `CalcDisplayPredicateStrategy` (real Calculator display Static read
  against a `calc_display_equals:<value>` marker) ahead of the default chain — see §13.
- **Hierarchical planning seam**: `TaskState.subgoal` + bounded `plan_notes` exist and
  the TASK STATE channel of the doctrine prompt renders them; a planner can be added
  above the phase machine without contract changes.
- **Benchmark runner**: landed as scaffolding — `benchmarks/runner.py` with
  `--mode fake` (default; `fakeworld.py` fake desktop) and `--mode env` (real apps via
  `appwin.py`), 9 task YAMLs (`benchmarks/tasks/`, JSON-compatible YAML 1.2 subset,
  OSWorld-2.0-aligned categories). Harness only: every artifact carries the disclaimer
  "Harness validation output — NOT benchmark scores."; running the harness in fake
  mode produces a validation artifact locally. No score
  may be claimed until a real vision provider is plugged in and a measured run exists.
- **Pixel-level screenshot secret detection**: `redaction._scan_image_for_secrets` is
  a stable hook returning `[]`; implementing it activates region blur automatically.
- **Platform adapters**: `ComputerBackend` is the seam for non-Windows or remote
  execution; `FakeComputerBackend` demonstrates the full contract (monitors, window
  identity, coordinate classification, stop checks, input blocking).

## 13. Real-Windows E2E suite and benchmark scaffolding (Wave 4)

**E2E suite** (`tests/e2e/`, E7): live-desktop tests driving REAL applications
(Notepad, classic Calculator, Microsoft Edge on a local page) through the runtime's own
MCP tool surface (`start_session` → `computer_observe` → `run_goal`/`computer_execute`)
with deterministic scripted providers — no vision model, no network. 10 tests total:

| Test | What it proves |
|---|---|
| `test_notepad_window_identity_observation` | `WindowInfo` hwnd/pid/process/exe/class populated on real Windows (P0-G/I) |
| `test_notepad_type_semantic_verification` | semantic typing verification (P0-A) |
| `test_notepad_moved_window_recovery` | moved-window fault → `WRONG_WINDOW` → bounded recovery (P0-B) |
| `test_notepad_window_switch_stale_observation` | true `STALE_OBSERVATION` rejection on window switch (P0-H) |
| `test_calculator_clicks_and_display_verification` | grounded clicks + display-predicate verification |
| `test_calculator_division_precision` | precise small-target clicks, verified display value |
| `test_browser_local_page_window_state_verification` | local page in Edge verified via window state |
| `test_benchmark_harness.py` (3 tests, NOT e2e-marked) | benchmark runner validation in fake mode; runs in the standard suite |

Gate mechanics (`tests/e2e/conftest.py`): desktop tests carry `@pytest.mark.e2e`
(registered in conftest; pyproject untouched) and are auto-skipped unless
`CUMCP_RUN_E2E=1`; the gate tests the marker itself (`get_closest_marker`), not path
keywords. Per-test deadline fixture (`CUMCP_E2E_TEST_TIMEOUT`, default 240 s) instead
of pytest-timeout (no new dependencies). Every desktop test writes evidence to
`evidence/e2e/<test_name>/` (observation dumps with window identity, downscaled
screenshots, audit excerpt, transcript, result.json) and appends to
`evidence/e2e/runs.md`; these artifacts are generated locally on the machine that
runs the suite — test outputs, not shipped documentation. Runtime-dependent assertions
are cross-checked independently against real Win32 state so the runtime cannot
self-certify.

Because the default strategy chain cannot semantically verify several real desktop
transitions, the suite injects application-specific deterministic strategies (the
documented `VerificationStrategy` protocol) via the `_get_bundle`/`VerificationEngine`
seam: `WindowTextPredicateStrategy` (real Edit control via `WM_GETTEXT`) and
`CalcDisplayPredicateStrategy` (real display Static against `calc_display_equals:<value>`,
claimed for `predicate` and `visual_change` intents). Both read real window state;
`uncertain` is never upgraded.

Measured environment findings (documented, not silently worked around):

1. `calc.exe` on Server 2022 is a stub that exits and re-launches `win32calc.exe`
   (`CalcFrame`) in a different process; the suite launches `win32calc.exe` directly.
2. A single-digit Calculator change is a mean pixel difference of ~0.2 on a
   1920x1080 screenshot — far below the 1.0 mean-diff threshold — which motivated
   the compact-change upgrade: `ScreenshotDiffStrategy` now also counts
   strongly-changed pixels (per-channel delta >= `STRONG_PIXEL_DELTA = 40`,
   threshold `STRONG_CHANGE_MIN_PIXELS = 50`), so a redrawn digit or thin stroke
   verifies while sub-threshold flicker (caret, clock tick) stays `uncertain`.
   Application-state/window-text strategies remain the stronger verification path
   for application-internal state.
3. Window-bounds-only moves do NOT trip `STALE_OBSERVATION` (staleness checks hwnd/
   pid/process/monitor identity/dimensions/coordinate space — not window bounds);
   the moved-window test shows semantic verification catching the miss
   (`WRONG_WINDOW` → bounded recovery) while window-switch exercises the true
   staleness rejection.
4. `LocalComputerBackend`'s DPI fallback ladder can downgrade a pre-set
   per-monitor-v2 process to "system" awareness; the backend must be the first
   awareness setter in a process. On this single-monitor box (system DPI == monitor
   DPI) observations still classify `verified_passthrough`, so it is harmless here.
5. Foreground can be stolen by the hosting console between launch and observation;
   tests focus the target window explicitly before asserting foreground-derived state.

**Benchmark scaffolding** (`benchmarks/`, E7): `runner.py` executes 9 task YAMLs
(`benchmarks/tasks/`, JSON-compatible YAML 1.2 subset — no new parser dependency)
through the server tool surface with a deterministic scripted provider; it runs via
`python -m benchmarks.runner` or as a direct script path (F4).
`--mode fake` (default) runs everything against `fakeworld.py`'s fake desktop;
`--mode env` runs real applications on this box via `appwin.py` (Notepad / win32calc /
Edge). Collected metrics per task: grounding strategy/confidence, per-action
verification outcomes, recovery events by failure class, safety blocks (expected vs
false), actions/task, model calls, and latency summaries. `benchmarks/results/` is
gitignored; a fake-mode validation run produces its artifact locally. One recorded
validation run (run id
`harness-validation-fake-001`: 9 tasks, 7 run, 2 `requires_env`, 7 completed,
0 failed, 1 recovery event, 1 expected safety block, 0 false blocks, 0 violations)
— like every artifact — carries the disclaimer
"Harness validation output — NOT benchmark scores.". These are harness-validation
numbers, not performance claims; scores require a real vision-provider run.

## 14. Known gaps (honest)

- OCR/UIA are stubs (above); `ocr_text`/`ui_elements` are always `None` in P0.
- Model-based visual verification requires a configured provider; otherwise uncertain.
- Screenshot redaction is text-pattern based + explicit-region blur only.
- Multi-monitor logic is unit-tested with fake monitor sets (100/125/150% DPI); the
  real-Windows E2E ran on a single-monitor box only.
- The E2E suite uses deterministic scripted providers; no E2E has been driven by a
  real vision model, and default-chain semantic verification of application-internal
  state (edit text, calculator display) currently requires injected strategies rather
  than shipping in the default chain.
- Benchmark output is harness validation only; no score exists.
- No OS-level sandbox or VM isolation; `pyautogui` failsafe corner is the only
  physical backstop.
