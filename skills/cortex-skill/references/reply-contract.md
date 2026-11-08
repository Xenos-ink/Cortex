# Reply contract (reference)

Read this when a reply shape or key name surprises you. One envelope, identical
semantics for every tool reply, every `follow_up_results[i]` item, every plan step, the
terminal plan, and the wait/cancel/fetch replies. No compatibility flags, no per-tool
variants.

## Envelope

- ok is True iff result is `verified` or `marker`. Everything else — `uncertain`,
  `failed`, `rejected`, `not_executed`, `dry_run`, `stopped`, `unknown`, a lifecycle
  refusal — is `ok:false`.
- `message` on every reply (human-readable; `waited_seconds=` and `integrity=` ride as
  `key=value` suffixes INSIDE the message, not as fields).
- `result` on every action-bearing reply — one closed token set (below).
- Dispatched input actions carry a `verification` block: `outcome` (`verified`/
  `uncertain`/`failed`), `confidence`, `evidence[]`, `verification_method` (what
  decided: a strategy name — `screenshot_diff`, `window_state`, `process_state`,
  `ui_control_text`, `focus_change`, … — or a special tier: `no_op_marker`, `dry_run`,
  `ensure_app_probe`, `interference_post_action`).
- Rejected replies: `{ok:false, result:"rejected", message, reasons}` with a gate-named
  `reasons` list and NO `error` key. (Execute-surface plan refusals — `plan_lease` /
  `invalid_plan_action` — additionally keep the typed `error` key and the handshake
  echo keys `blocked` / `executed_prefix` / `remaining`; a recording's `invalid_region`
  is likewise a typed `error` reply, never a `rejected` result.)
- A gated reply: `ok:false, result:"not_executed", requires_approval:true`. The string
  `approval_required` exists ONLY as a `failure_reason` value — never as a reply key.
- `partial_delivery: {delivered_chars: N}` appears on a rejected reply whose input may
  have partially landed (the mid-type focus drift case); absence of the key means
  nothing was dispatched.
- `ensure_app_outcome`: the typed launch outcome on ensure_app replies (`reattached` /
  `ambiguous` / `not_found` / `launched` / `launch_rejected` / `launch_unresolved`),
  `None` otherwise.
- `image_outbound_scale` on every image-bearing reply: the measured applied downscale,
  `None` for raw pass-through. Read it before aiming clicks off a returned image.
- `pointer {x, y, state, in_image}` and `last_click {x, y, button, age_ms} | null` on
  every image-bearing reply: the composited Cortex cursor in canonical coordinates
  (`state` `normal` / `pressed` — Pressed plus a ring means that call clicked there;
  `in_image:false` = the pointer was off this image and nothing was drawn) and the
  session's most recent click with its age. A capture with no known cursor emits
  neither key. `start_session(show_pointer=false)` (default source: env
  `CORTEX_SHOW_POINTER`) removes both; `computer_observe(show_pointer=...)` overrides
  one call.
- plan step and terminal replies carry `evidence_by_backend` = `{captures, repositions}`
  (store captures plus retained lease repositions) — there is no bare `evidence` plan
  key.
- `interference_events` on plan and rejected replies where events accumulated.

## The `result` token set (closed)

| Token | Meaning |
|---|---|
| `verified` | declared effect established by fresh evidence (ok:true) |
| `marker` | intentional non-screen claim (`done`/`wait` handled; ok:true) |
| `uncertain` | dispatched, unproven — never silently upgraded |
| `failed` | proven not achieved, or operation error |
| `rejected` | a gate refused (zero effect, except `partial_delivery`) |
| `not_executed` | intended but never dispatched (approval pending, cutoff) |
| `dry_run` | validated, deliberately not dispatched (`dry_run:true` row) |
| `stopped` | host-requested plan stop; prefix kept, nothing verified |
| `unknown` | terminal plan aggregate: at least one step uncertain, none failed |

Per-surface subsets: the flat execute surface and follow-up items allow verified,
uncertain, failed, rejected, not_executed, dry_run, marker and stopped (the approval
gate on the flat surface is `not_executed`); plan steps verified/uncertain/failed/
rejected/not_executed; the terminal plan verified/unknown/failed/stopped; the wait
family marker/rejected/failed; fetch verified/rejected; observe/zoom carry NO result
token (read-only; their refusals are `ok:false` + typed error); the record family
marker (a start or a peek) / verified (a finished recording, collected) / stopped
(a recording cancelled early — what was captured is kept and reported; for a
recording this is NOT the terminal-plan meaning) / failed (`capture_failed` /
`record_worker_failed`) / rejected (band or argument refusals).

## The recording family (computer_record / computer_record_result)

- Start and peek replies are `marker` with `status`: `recording` (capture running) or
  `encoding` (capture finished, the video encode still running), plus
  `elapsed_seconds` / `remaining_seconds` on a peek. `cancel=true` answers
  `stopped` with what was captured.
- A finished collection is `verified` carrying `record_id`, `duration_ms`,
  `effective_fps`, `dropped_frames`, `region`, the timeline segments, the window's
  `actions`, `keyframes` (each with a fetchable `image_id`), the contact sheet image,
  and a per-reply `video` entry: `included` (`bytes` / `width` / `height` / `fps` /
  `codec` / `encoder` — with the DEFAULT `video_delivery="file"` the entry carries
  `delivery: "file"` plus the absolute `path` of the MP4 written under the
  recordings folder and attaches NO blob; `"inline"` keeps the embedded resource
  for hosts known to pass video to the model) or `omitted` naming one closed
  reason — `not_requested` /
  `too_large` / `encoder_unavailable` / `encode_failed` / `over_budget` (retried
  on a later collection: `over_budget`, `write_failed`, or an `encode_failed`
  carrying `aborted: true`) / `frames_released` / `write_failed`.
- Fetched recording keyframes publish `frame_to_canonical` (`{"scale", "origin"}`),
  which maps the STORED keyframe pixels to canonical screenshot coordinates (a copy
  served downscaled must first be divided by its `image_outbound_scale`); the
  inverted `coordinate_scale_*` is omitted for recording frames.

## Terminal honesty

When a plan walk completes, the terminal reply's verdict covers the WHOLE prefix:
ok is True only for `result: "verified"`; `unknown` and `stopped` are never success.
A partial-but-verified prefix is capped at `unknown`; any failed step caps it at
`failed`. Read `failure_reason` + `not_executed` before choosing continue/stop/replace.

## Verification strictness

Verification is strict: a stated `expected_effect` must be established by a
deterministic tier (window/UI identity, process state, control-text read-back, focus
change); a stated `expected_effect` satisfied only by a pixel change stays `uncertain`
— stating an effect opts into strictness, never leniency. State CHECKABLE effects: a
window title or state, a process state, a UI control's text, a focus change — not
"the screen changes". `uncertain` is never upgraded silently, and model / grounding /
verification confidence are three separate numbers.

## Error-code cheat sheet (typed `error` / `reasons` tokens)

`invalid_action` (malformed action, teach-in message) · `invalid_limits` ·
`invalid_interference` · `invalid_image_delivery` · `invalid_visual_view` ·
`invalid_pixel_evidence` ·
`view_derive_failed` · `session_stopped` · `session_limit_exceeded` ·
`invalid_wait_duration` · `wait_duration_exceeds_max` · `too_many_waits` ·
`unknown_wait` · `wait_failed` · `image_expired` · `image_not_found` ·
`frame_too_large` · `store_disposed` · `plan_lease` · `invalid_plan_action` ·
`invalid_show_pointer` · `invalid_region` (a zoom crop or a recording region
outside the frame — for a recording this is a typed `error` reply, not a
`rejected` result) · `invalid_record_duration` · `invalid_record_fps` ·
`invalid_record_keyframes` · `invalid_record_param` ·
`text_session_no_screen` · `recording_limit` ·
`unknown_record` · `record_expired` · `record_failed` (the recording worker
could not start) · `digest_surprise` ·
`limit_exceeded` (with the `limit` key naming the cap).
