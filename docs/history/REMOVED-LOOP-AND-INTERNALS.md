# HISTORICAL — describes v0.5.6–v0.7.5, not current

This file records machinery that was REMOVED from Cortex. Nothing here describes
the 0.7.6 surface; it is kept so the design history and the pinned audit
vocabulary stay traceable. Current truth lives in README.md,
docs/ARCHITECTURE.md, docs/SAFETY.md, docs/TOOLS.md, docs/REPLY-CONTRACT.md and
docs/CONFIGURATION.md. Per-release behavior changes live in VERSIONS.md.

## The internal autonomous loop (removed by user order, v0.5.6; internals removed v0.6.0)

The server was once driven by an internal LLM/vision decide loop: an internal
model decided one action per model call inside `run_goal`, with a subtask
orchestration layer above the executor. Both were removed by explicit user
order: the server is driven exclusively by a host agent making direct tool calls.

- **Removed tools** (v0.5.6): `run_goal`, `create_subtask`, `list_subtasks`,
  `run_subtask`, `get_session_progress`. A host that still calls them gets
  "Unknown tool".
- **Removed internals** (v0.6.0): the agent decide-loop block (`run`,
  `_decide`/`_call_provider`, the `_done/_blocked/_stopped` result builders,
  failure/dismiss handling), the provider `decide`/`decide_full`/
  `plan_subtasks`/`summarize_context` endpoints with their `_LazyProvider`
  delegates, the context-summarizer plumbing, the subtask mutation APIs
  (create/start/complete/fail/pause/resume/requeue and the fixed TRANSITIONS
  table), the `PlanValidator` classes (only the pure `find_cycle`/
  `contains_control_characters` helpers survive, live via the checkpoint path),
  and the modules `recovery.py`, `approval.py`, `health.py` (deleted).
- **What survived the removal and is still live in 0.7.6**: the sealed
  checkpoint/resume machinery on `start_session(resume_from_checkpoint=…)`,
  the shared `SessionBudgetTracker` restoration and its sealed-overshoot
  fail-closed gate, the context snapshot/restore round-trip, and the
  `TerminationReason` vocabulary on `TaskState`/models for checkpoint and audit
  data compatibility.
- **Loop-only semantics replaced by the host surface**: per-call approval budget
  → per-call `approved=True`; in-run bounded recovery/re-decide → typed failure
  outcomes to the host; `completion_evidence="model_declared"` labeling →
  host-decided completion on evidenced `verified` outcomes.

## Bounded recovery (removed with the loop; `recovery.py` deleted v0.6.0)

Historical contract: `classify_failure` recognized sibling exceptions
structurally (by class name: `InputBlockedError`, `CoordinateSpaceError`,
`StaleObservationError`, `DisplayUnavailableError`, `UnsupportedGroundingError`,
provider errors) and validator `codes`; `TaskStopped` classified to `None` (a
stop is the kill path, never a failure). The 12-class taxonomy and its
strategies:

| FailureClass | Strategy | Behavior | Bounds |
|---|---|---|---|
| `STALE_COORDINATES` | `RECOVER_REOBSERVE` | re-observe, discard failed coordinates, re-decide | recovery budget |
| `MOVED_UI` | `RECOVER_REOBSERVE` | same | recovery budget |
| `WRONG_WINDOW` | `RECOVER_REOBSERVE` | same | recovery budget |
| `UNEXPECTED_DIALOG` | `RECOVER_REOBSERVE` | same | recovery budget |
| `NAVIGATION_DRIFT` | `RECOVER_REOBSERVE` | same | recovery budget |
| `BLOCKED_UI` | `RECOVER_DISMISS` (policy-probed) else `REPLAN` | one bounded Escape-key dismiss, then retry the instance | recovery budget + single-shot dismiss |
| `APP_CRASH` | `REPLAN` | re-decide from fresh state | recovery budget |
| `LOW_CONFIDENCE` | `RETRY_ONCE` | same instance, fresh grounding + validation, then replan | budget + `max_retries_per_action` |
| `AUTH_REQUIRED` | `TERMINATE_SAFELY` | credentials never auto-typed (hard rule) | — |
| `ALREADY_COMPLETED` | `COMPLETE` | goal already reached → completed | — |
| `UNRECOVERABLE` | `TERMINATE_SAFELY` | unrecoverable | — |
| `UNKNOWN` | `TERMINATE_SAFELY` | fail closed | — |

Bounds: `max_recovery_per_action = 2`, `max_recovery_per_task = 6` (defaults —
the fields survive as reserved `Limits` fields). Dismiss accounting (D6) kept
`action_total == action_success + action_failure`; dismiss inputs were never
gated by `check_action()`. `models.FailureClass` values survive as
checkpoint/audit data vocabulary. The 0.7.6 surface surfaces typed failures to
the host instead of recovering; only `STALE_OBSERVATION` still auto-retries once
(server-side re-observe + re-ground) before reporting the rejection.

## Approval epochs (removed with the loop; `approval.py` deleted v0.6.0)

Historical design: every approval in a long-running session lived inside an
epoch expiring on two independent axes — wall-clock age
(`approval_epoch_seconds`, default 1800 s, clamp 60..86400) or interactive
action count (`approval_epoch_actions`, default 50, clamp 1..1000), whichever
first. `require_approval=false` was modeled as a full-scope epoch that still
expired. After expiry every approval-requiring action was refused with
`requires_fresh_approval=True`; a per-action approval could never resurrect a
dead epoch. Material change (application/process changed, risk escalated, goal
or policy changed, environment changed) invalidated immediately. Fresh approval
was a NEW epoch from an explicit human grant only; nothing auto-renewed.

**Prolonged unattended execution (raise-only modifier)**: after
`PROLONGED_UNATTENDED_SECONDS = 3600` without human interaction, `LOW` was
treated as `MEDIUM` and `MEDIUM` as `HIGH` (never a downgrade; no fifth
RiskLevel); non-routine actions needed a fresh epoch and a standing grant could
not start new subtasks (`unattended_hold`). The 0.7.6 approval surface is
`safety.py requires_approval` + the per-call `approved` flag; approval is never
resurrected from data on resume.

## Health monitor (removed with the loop; `health.py` deleted v0.6.0)

Historical design: boundary-evaluated environment verification (no background
thread, no scheduler, not a second execution loop). `maybe_check()` ran only
when due (default every 600 s, clamp 60..3600, first call on a fresh monitor
due). The world was read through five injected probes; probe failure was
fail-closed data. Verdict matrix: unexpected foreground application (or its
identity unavailable) → `UNSAFE` (epoch invalidated, execution stopped
`blocked_safety`); title drift / stale observation / hung indicators /
unverifiable expectation → `DEGRADED` (one bounded re-evaluation, then pause);
everything verified → `HEALTHY`. Recommendations were routing data; the caller
decided.

## Subtask orchestration layer (removed with the loop; tools removed v0.5.6)

Historical contracts, kept for the audit vocabulary they left behind:

- **Subtask entities** (`subtask_manager.py`, thread-safe, no execution):
  `subtask_id` 1..128 chars, `description` 1..2000, statuses
  `pending/running/completed/failed/blocked/paused` through a fixed TRANSITIONS
  table (`pending -> failed` legal; terminal states move nowhere), `depends_on`
  ≤ 50, results capped at 20 with `screenshot_after_base64` stripped,
  hard ceiling `MAX_SUBTASKS = 50`, deterministic validation order
  (ceiling → description → id → dependency shape → self-dependency → unknown
  dependency → duplicate dependency → cycle), bounded read APIs
  (counts, never payloads), fail-closed snapshot/restore.
- **Dependency graph**: `ready_set()` = pending with all dependencies completed
  in creation order; `fail()` transitively moved `pending|paused` dependents to
  `blocked`; dead-branch doctrine (`_dead_ids`) with bounded replan
  (≤ 3 attempts/session, replacements never depending on dead ids) else
  `UNRECOVERABLE` termination — never continuation with unknown correctness.
- **Deterministic plan validator** (`plan_validator.py`): an LLM plan was an
  UNTRUSTED SUGGESTION; eleven stable rejection codes (`malformed_plan`,
  `malformed_subtask_entry`, `too_many_subtasks`, `empty_plan`,
  `duplicate_subtask_id`, `unknown_dependency`, `self_dependency`,
  `dependency_cycle`, `invalid_status`, `non_pending_status`, `unsafe_content`);
  any violation rejected the WHOLE plan; accepted entries created in
  deterministic topological order.
- **Context manager** (live summarizer part removed; snapshot/restore live):
  recent window deque 5..10 entries × 1000 chars with surrogate-safe
  truncation, plan notes 50 × 500, summaries 20 × 300, deterministic fallback
  summary on summarizer absence/failure.
- **Shared budgets** (§15.11 of the old architecture): per-subtask fresh
  `LimitEnforcer` scope, monotonic write-back onto `SessionBudgetTracker`,
  step budget capped to remaining session steps. The tracker and its restore
  are STILL LIVE (checkpoint/resume); the loop-era trip points are not.
- **Typed error codes** that remain in the domain vocabulary (pinned by
  tests): `subtask_not_ready`, `unmet_dependencies`, `subtask_limit_exceeded`,
  `unknown_subtask`, `subtask_already_exists`, `invalid_subtask`,
  `unknown_dependency`, `self_dependency`, `dependency_cycle`,
  `invalid_transition`, `runtime_busy`, `planner_unavailable`,
  `plan_rejected`, `subtask_not_runnable`. The checkpoint/resume refusal codes
  ARE live on the start_session surface: `invalid_checkpoint`,
  `checkpoint_error` (a non-validation `CheckpointError` during resume —
  `server/lifecycle.py`, `_resume_error_response`) and `resume_refused`.

## Session-ceiling enforcement — RESERVED, NOT ENFORCED (v0.6.0)

The session-level `Limits` fields (`max_session_seconds`, `max_session_actions`,
`max_session_model_calls`, `max_session_steps`, `max_subtasks`,
`context_summarize_every`, `approval_epoch_seconds`, `approval_epoch_actions`,
`health_check_interval`) are still accepted and validated on the wire, clamped
exactly as before, stored in checkpoints and re-clamped on resume — but their
loop-era enforcement trip points died with the loop. The live remainder: the
sealed-overshoot gate (a sealed checkpoint with inflated counters is restored
EXACT and then fails closed with `SessionBudgetExceeded` the moment gated work
is attempted) and the per-task limits that still gate the direct path
(screenshot rate, action budget, session count). See docs/CONFIGURATION.md.

## The five/six-tool wire narratives (superseded 0.7.6)

For release-history readers: between v0.5.6 and v0.7.5 the served surface was
five tools (`start_session`, `stop_session`, `computer_observe`,
`computer_screenshot`, `computer_execute`), then six with the observation-only
`computer_zoom` (v0.7.5). v0.7.6 serves ten (the wait family and
`computer_fetch_image` joined). The old README's five-tool capability narrative,
its per-tool reference, the worked example whose executed reply keyed the
screenshot as `screenshot_after_base64`, and the loop-era backward-compatibility
decisions (§10 of the old docs/ARCHITECTURE.md) described those surfaces and are
superseded by the unified reply contract: every action-bearing reply carries a
closed `result` token with `ok = (result ∈ {verified, marker})`, and every
image-bearing reply discloses `image_outbound_scale`. See docs/REPLY-CONTRACT.md.
