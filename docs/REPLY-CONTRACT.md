# Reply contract — Cortex 0.7.6

The full reference for reply shapes. One envelope, identical semantics for every
tool reply, every `follow_up_results[i]` item, every plan step, the terminal
plan, and the wait/cancel/fetch replies. No compatibility flags, no per-tool
variants. The shipped driver skill carries the same contract in
`skills/cortex-skill/references/reply-contract.md`; the two documents do not
contradict — this one is the doc-side home of the per-surface subsets and the
typed error codes. Pinned by `tests/test_docs_truth.py` and
`tests/reply_contract.py` (every reply the suite produces is run through the
contract checks).

## Envelope

- `ok` is True iff `result` is `verified` or `marker`. Everything else —
  `uncertain`, `failed`, `rejected`, `not_executed`, `dry_run`, `stopped`,
  `unknown`, a lifecycle refusal — is `ok:false`.
- `message` on every reply (human-readable; `waited_seconds=`, `integrity=`,
  `elapsed_ms=` (calls that ran ≥ 3 s), `client_timeout_s=<n>` (on
  `start_session`, the effective host budget) and the type-integrity `branch=`
  ladder token ride as `key=value` suffixes INSIDE
  the message, not as fields). `branch=` appears only on `integrity=unverified`
  type replies and names the verification ladder's giving-up reason — the closed
  token list is derived from the code by a truth test and documented in the
  skill glossary.
- `result` on every action-bearing reply — one closed token set (below).
- Dispatched input actions carry a `verification` block: `outcome`
  (`verified` / `uncertain` / `failed`), `confidence`, `evidence[]`,
  `verification_method` (what decided: a strategy name — `screenshot_diff`,
  `window_state`, `process_state`, `ui_control_text`, `deterministic_predicate`,
  `text_predicate`, `focus_change`, `model_visual` — or a special tier:
  `no_op_marker`, `dry_run`, `ensure_app_probe`, `interference_post_action`).
  Three separate confidences exist — model, grounding, verification — and none
  of them is a success probability.
- Rejected replies: `{ok:false, result:"rejected", message, reasons}` with a
  gate-named `reasons` list and NO `error` key. (Execute-surface plan refusals —
  `plan_lease` / `invalid_plan_action` — additionally keep the typed `error` key
  and the handshake echo keys `blocked` / `executed_prefix` / `remaining`.)
  One gate-named reason outside the plan surfaces: `unknown_argument` — a tool
  received an argument it does not declare (e.g. a direct `hold_ms=`); the
  message names the bad arguments, lists the valid ones and teaches where
  per-item values belong.
- A gated reply: `ok:false, result:"not_executed", requires_approval:true`. The
  string `approval_required` exists ONLY as a `failure_reason` value — never as
  a reply key.
- `partial_delivery: {delivered_chars: N}` appears on a rejected reply whose
  input may have partially landed (the mid-type focus drift case); absence of
  the key means nothing was dispatched.
- `ensure_app_outcome`: the typed launch outcome on ensure_app replies
  (`reattached` / `ambiguous` / `not_found` / `launched` / `launch_rejected` /
  `launch_unresolved`), `None` otherwise.
- `image_outbound_scale` on every image-bearing reply: the measured applied
  downscale, `None` for raw pass-through. Read it before aiming clicks off a
  returned image (screen coordinates = image coordinates × 1/scale).
- Plan step and terminal replies carry `evidence_by_backend` =
  `{captures, repositions}` (store captures plus retained lease repositions) —
  there is no bare `evidence` plan key.
- `interference_events` on plan and rejected replies where events accumulated.
- `text_summary` exists only on `computer_observe` / `computer_zoom` replies —
  never on an execute reply.
- After a batch: `follow_up_results` AND `follow_ups_stopped_reason` (`null` =
  the batch ran to completion; `time_budget` = the call's wall-clock budget ran
  out — executed items keep their entries, nothing new was dispatched, and the
  driver RESENDS the remaining items in a new call); an `uncertain` or `failed`
  verdict on an item that already dispatched does NOT stop the batch.

## The `result` token set (closed)

| Token | Meaning | ok |
|---|---|---|
| `verified` | declared effect established by fresh evidence | true |
| `marker` | intentional non-screen claim (`done`/`wait` handled) | true |
| `uncertain` | dispatched, unproven — never silently upgraded | false |
| `failed` | proven not achieved, or operation error | false |
| `rejected` | a gate refused (zero effect, except `partial_delivery`) | false |
| `not_executed` | intended but never dispatched (approval pending, cutoff) | false |
| `dry_run` | validated, deliberately not dispatched (`dry_run:true` row) | false |
| `stopped` | host-requested plan stop; prefix kept, nothing verified | false |
| `unknown` | terminal plan aggregate: at least one step uncertain, none failed | false |

Per-surface subsets:

- The flat execute surface and follow-up items allow `verified`, `uncertain`,
  `failed`, `rejected`, `not_executed`, `dry_run`, `marker` and `stopped` (the
  approval gate on the flat surface is `not_executed`).
- Plan steps: `verified` / `uncertain` / `failed` / `rejected` /
  `not_executed`.
- The terminal plan: `verified` / `unknown` / `failed` / `stopped`.
- The wait family: `marker` / `rejected` / `failed`.
- Fetch: `verified` / `rejected`.
- The record family (`computer_record` / `computer_record_result`): `marker`
  (a start or a peek), `verified` (a finished recording, collected),
  `stopped` (a recording cancelled early — the frames captured so far are
  kept and reported; for a recording this is NOT the terminal-plan meaning),
  `failed` (`capture_failed` / `record_worker_failed`), `rejected` (bands,
  caps, text session).  A malformed / out-of-bounds / unverifiable REGION is
  the typed `invalid_region` ERROR (`ok:false`, an `error` key, NO `result`
  token — the same shape as the observe family's refusals), never a
  `rejected` reply.  A peek may report `status: "encoding"` (DELTA 2: the
  capture finished and the video encode is still running) instead of
  `"recording"`.
- `computer_observe` / `computer_screenshot` / `computer_zoom` carry NO `result`
  token (read-only; their refusals are `ok:false` + typed error).

## Terminal plan honesty

When a plan walk completes, the terminal reply's verdict covers the WHOLE
prefix: `ok` is True only for `result: "verified"`; `unknown` and `stopped` are
never success. A partial-but-verified prefix is capped at `unknown` (work the
plan declared but that produced no evidence is an absence of evidence, not a
success); any failed step caps it at `failed`. Plan STEP replies report
`uncertain`; the `unknown` token is reserved for the terminal aggregate. Read
`failure_reason` + `not_executed` before choosing continue/stop/replace.

## The recording family (computer_record / computer_record_result)

The screen recorder (ORVEX-2026-109 WP-R + DELTA 1) follows the same envelope:

- Start and peek replies are `marker` (`status: "recording"`, with
  `elapsed_seconds` / `remaining_seconds`); a finished collection is
  `verified`; an early cancel is `stopped` — for a RECORDING this means the
  capture ended early and the frames captured so far are kept and reported
  (`cancelled: true` in the payload), NOT the terminal-plan meaning.
- A broken capture is `failed` with reasons `["capture_failed"]` (the grabber
  raised — secure desktop, UAC, locked workstation) or
  `["record_worker_failed"]` (any worker exception), plus the error text in
  `message` and the frames captured before the failure. Never a silent empty
  `verified`.
- The `video` entry on a finished payload ALWAYS says what happened, from a
  closed omission set: `included` (a blob is attached to THIS reply), or
  `omitted` = `not_requested` | `too_large` | `encoder_unavailable` |
  `encode_failed` | `over_budget` | `frames_released` (the held frames were
  evicted under the recording pool's byte cap before any video was stored).
  `included` appears only when a blob is attached to THIS reply; the encoder
  actually used rides as `encoder` (`h264_mf` or `libx264`).  `too_large` /
  `encoder_unavailable` / `encode_failed` / `frames_released` are FINAL for
  the recording (later calls reuse them), with two retried cases:
  `over_budget` (a later call has a fresh budget) and an `encode_failed`
  carrying `aborted: true` (a stop/cancel aborted the encode; a later collect
  retries while the frames are held).  Video may be requested on EITHER call.
- Recording keyframes fetch through `computer_fetch_image` like observation
  frames.  DELTA 2: the fetch publishes `frame_to_canonical` —
  `{"scale": <canonical px per frame px>, "origin": [x, y]}` (the canonical
  region origin) — so a keyframe pixel maps back with
  `canonical = origin + pixel × scale`.  This maps the STORED keyframe
  pixels: a copy served downscaled must first be divided by its
  `image_outbound_scale`.  The `pointer` block is reported in
  CANONICAL coordinates (the same space observation fetches use); the
  inverted `coordinate_scale_*` is deliberately OMITTED for recording
  frames.  `coordinate_space: "recording_frame"` names the frame space the
  sprite was drawn in.

## The `failure_reason` vocabulary (closed)

Plan step and terminal replies always carry `failure_reason` (`None` when the
step verified), from the closed 16-token vocabulary `PLAN_FAILURE_REASONS`
(`planning/plan.py`):

| Token | Class |
|---|---|
| `verification_failed` | a verification answered `failed` |
| `verification_uncertain` | a step abstained (including an ensure_app follow-up that dispatched without proving a target) |
| `execution_error` | the backend/operation raised (`action_error`) |
| `limit_exceeded` | a resource limit refused work |
| `cancelled` | the terminal aggregate was cancelled |
| `safety_denied` | the safety policy denied |
| `approval_required` | a step was gated and never dispatched |
| `digest_surprise` | the post-action digest disagreed with the queued premise |
| `rejected` | a pre-dispatch refusal (and the class for `stuck_modifier` / `target_gone` stops) |
| `invalid_action` | a malformed action or payload |
| `invalid_follow_up` | a queue item failed the GroundedAction shape check |
| `policy_stop` | the step's `on_failure`/`on_uncertain` policy stopped the batch |
| `modal_dialog` | the dialog sentinel stopped the batch |
| `focus_drifted` | a focus-interference stop (also the class for `focus_taken_by` / `focus_identity_unknown`) |
| `no_post_action_observation` | the post-action capture never arrived |
| `unclassified` | the documented fallback for a state none of the tables names |

The CLASS mapping is conditional: when the step's FIRST item is refused, the
kind table wins and the step reports `rejected`; when a LATER item of a step is
refused (the queue stopped), the stop-reason table maps focus events to
`focus_drifted` and refusals to `rejected`. The exact cause stays verbatim in
`follow_ups_stopped_reason` and in the entry's `reasons`.

## Verification strictness

Verification is strict: a stated `expected_effect` must be established by a
deterministic tier (window/UI identity, process state, control-text read-back,
focus change); a stated `expected_effect` satisfied only by a pixel change stays
`uncertain` — stating an effect opts into strictness, never leniency. State
CHECKABLE effects: a window title or state, a process state, a UI control's
text, a focus change — not "the screen changes". `uncertain` is never upgraded
silently, and model / grounding / verification confidence are three separate
numbers. A type-integrity read-back of `integrity=mismatch` demotes a
`verified` verdict to `uncertain` — a masked or transformed field (password
dots, formatted numbers) may legitimately hold something that differs. A
trusted genuine partial is repaired with exactly ONE verified retype (the
message reports `healed=<n>`); `integrity=mismatch` never triggers a repair,
and a buffer that is STILL wrong after that retype fails the action with
`TextIntegrityError` — a read-back that merely raises is an operation error
(`error` `action_error`), not a drop confirmation. `unverified` never triggers
a repair and never double-applies. See
`skills/cortex-skill/references/glossary.md` for the full integrity-status and
interference-label tables.

## Error-code cheat sheet (typed `error` / `reasons` tokens)

`invalid_action` (malformed action, teach-in message) · `invalid_limits` ·
`invalid_interference` · `invalid_image_delivery` · `invalid_show_pointer` ·
`invalid_visual_view` · `invalid_region` (zoom crop outside the frame) ·
`invalid_pixel_evidence` · `view_derive_failed` · `session_stopped` · `session_limit_exceeded` (with
`live_sessions` + `idle_timeout_seconds`) · `invalid_wait_duration` ·
`wait_duration_exceeds_max` · `too_many_waits` · `unknown_wait` · `wait_failed` ·
`image_expired` · `image_not_found` · `frame_too_large` · `store_disposed` ·
`plan_lease` · `invalid_plan_action` · `digest_surprise` · `limit_exceeded`
(with the `limit` key naming the cap) · `invalid_checkpoint` ·
`checkpoint_error` (a non-validation checkpoint failure during a resume) ·
`resume_refused`.

The interference event vocabulary (`reasons`, `interference_events`, audit
notes): `FOCUS_TAKEN_BY`, `FOCUS_IDENTITY_UNKNOWN`, `FOCUS_DRIFTED`,
`MODAL_DIALOG`, `REATTACHED`, `AMBIGUOUS_INSTANCE`, `NO_INSTANCE`,
`STUCK_MODIFIER`, `TARGET_GONE`, `REANCHOR_REFUSED`. Driver doctrine:
`benchmarks/tasks_hard/DRIVER-PROTOCOL.md`.
