# Waypoint plan protocol (reference)

Read this before submitting or driving any `plan`. A plan is the EXPLICIT-TIMING mode:
outside a plan a single action is instant (0 ms move, 0 ms settle), so a submitted plan
must state every timing, verification and uncertainty decision itself — anything
missing is rejected with a teach-in message BEFORE any capture or motion (zero effect).

## Submitting

```json
{"points": [
   {"id": "open", "x": 420, "y": 180,
    "actions": [{"action": "click", "expected_effect": "menu opens"}],
    "arrival_condition": "the menu is open", "duration_ms": 150,
    "settle_ms": 80, "timeout_ms": 5000, "on_failure": "stop", "on_uncertain": "stop"}],
 "transition_duration_ms": 150, "point_timeout_ms": 5000, "settle_ms": 80,
 "on_failure": "stop", "on_uncertain": "continue", "final_wait_ms": 1000}
```

- Start with `computer_execute(session_id, action="done", plan={...})`. A plan call
  carries NO flat action parameters — a flat action sent alongside `plan` is refused
  (`invalid_action`, zero effect). `action` stays `"done"`.
- Scale: at most 10 points (`ids` unique) and at most 5 actions per point. An explicit
  `"actions": []` declares a crossing point (move, verify the cursor, move on) — an
  ABSENT key is not one.
- Explicit keys, each stated on the plan OR on every point: `transition_duration_ms`
  / per-point `duration_ms` (0 = instant jump, 1..5000 ms); `settle_ms` (0 = no
  settle, 1..200 ms); `point_timeout_ms` (0..60000 ms) / per-point `timeout_ms`;
  `on_failure` and `on_uncertain` (each `"stop"` or `"continue"`). What is verified:
  `arrival_condition` on every point carrying an input action (an action-level
  `expected_effect` covers it). An explicit `delta` (0..10 s) on every `wait` action
  inside a point — `final_wait_ms` (0..5000 ms) is a separate optional trailing wait
  after the last point, not a substitute.
- Wire strictness (fail-closed): point actions carry NO x/y — the point IS the
  location; only a point `drag` carries `x2`/`y2` (the drag end). Numbers must be
  plain integers (no floats, strings or booleans); unknown keys are rejected; a
  JSON-encoded string plan is tolerated at runtime but validated identically.
- Motion is Cortex's, not yours: the transition moves by itself and Cortex re-targets
  when the pointer moves mid-motion (the move shows in `evidence_by_backend`
  `repositions`). Never send a path or per-pixel route. `hold_ms` rides a plan point's
  action (or a `follow_ups` entry), never the top-level call.

## The stepwise handshake

ONE call executes ONE step. The step reply carries `step_index`, `result`, `ok`,
`message`, `failure_reason`, `not_executed`, `evidence_by_backend`,
`executed_prefix`, `remaining`, `next_step`, `blocked`, the `plan` aggregate, and the
step's captured frame (`image_id`, fetchable).

- Step `result` is one of `verified` / `uncertain` / `failed` / `rejected` /
  `not_executed`, with `ok` following the one ok rule. An uncertain step is
  "dispatched, unproven" — the stated `on_uncertain`/`on_failure` policy acts INSIDE
  one step only: with `"stop"` the rest of THAT POINT is skipped and the queue stops
  with `policy_stop`, but the cursor still advances past the attempted step and the
  next `continue` runs the NEXT point. An uncertain step is still REPORTED uncertain
  everywhere.
- **Cursor truth:** `continue` runs the NEXT point after a step that dispatched at
  least one item — even when its verification abstained or failed. A step refused
  before any dispatch keeps the cursor, so `continue` re-executes that same step.
  There is no retry verb: retry is `plan_action="replace"` with the corrected plan.
- When the walk completes (the last step verifies itself), the same reply is the
  TERMINAL aggregate over the whole prefix: `result` in `verified` / `unknown` /
  `failed` / `stopped` and the terminal ok rule (reply-contract.md). A last step that
  does not verify itself PARKS the lease: `continue` then delivers the terminal
  verdict; `replace` retries.
- `plan_action` moves (sent with `action="done"`, NO `plan` in the call):
  `"continue"` runs the next point; `"stop"` ends the plan (`result:"stopped"`,
  prefix kept, lease released, `not_executed` names what never ran); `"replace"` +
  the new `plan` swaps it AND executes the replacement's first step in the same
  call. Anything else is `invalid_plan_action` and the lease stays; `plan_action`
  without a lease is also `invalid_plan_action`. Under an ACTIVE lease a `plan` sent
  with `continue`/`stop` (or with no verb) is the `plan_lease` rejection — only
  `replace` accepts a plan.

## The lease

While a lease is open (`blocked:true`), these are refused with the typed `plan_lease`
rejection, zero effect: a flat `computer_execute` action, `follow_ups`, a second
`plan`, `computer_observe`, `computer_zoom`, `computer_screenshot`, `computer_wait`,
`computer_cancel_wait`. Two read-only exceptions: `computer_fetch_image` stays allowed
during a lease (fetch a step's frame by the reply's `image_id`), and
`computer_wait_status` stays allowed during a lease.

Approval is decided once on the first plan call (its `approved` parameter); `replace`
inherits it and deliberately never reads the replace call's `approved` — an unapproved
plan cannot escalate; `continue` never re-asks. A gated step replies
`result:"not_executed"` + `failure_reason: "approval_required"` +
`requires_approval:true` (the point's transition move may already have dispatched; the
input action never did). To approve mid-plan: `plan_action="stop"`, then resubmit ONLY
the points not yet executed — read them off `remaining` / `not_executed`, never include
the executed prefix — with `approved=true`. A resubmitted plan starts a FRESH lease and
re-runs whatever it contains, so a whole-plan resubmit would repeat every
already-executed step.
