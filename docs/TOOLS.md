# Tools — Cortex 0.7.6 tool reference

Current truth for the served MCP surface. The tool set, the action bounds, the
wait/evidence sentences and the reply token vocabulary here are pinned by
`tests/test_docs_truth.py` against the code; the prose semantics are current
truth traceable to the named modules. The reply shapes these tools return are
specified once in
[REPLY-CONTRACT.md](REPLY-CONTRACT.md); configuration knobs are in
[CONFIGURATION.md](CONFIGURATION.md).

## The twelve tools

| Tool | Kind | For |
|---|---|---|
| `start_session` | lifecycle | open a guarded session; returns `session_id` and the effective `limits` |
| `stop_session` | lifecycle | arm the kill path and end the session (idempotent) |
| `computer_observe` | observation | fresh full-screen capture + metadata |
| `computer_screenshot` | observation | compatibility alias of `computer_observe` (never prefer it) |
| `computer_zoom` | observation | fresh capture cropped to a region (observation-only, no action surface) |
| `computer_execute` | action | act: the 13-action vocabulary, `follow_ups`, the waypoint `plan` handshake |
| `computer_wait` | wait | blocking wait, up to one segment per call, with a `wait_id` |
| `computer_wait_status` | wait | block for the next wait segment, or peek with `block=false` |
| `computer_cancel_wait` | wait | end a wait early; reports elapsed/remaining |
| `computer_fetch_image` | evidence | re-serve a frame this session's replies carried, by `image_id` |
| `computer_record` | recording | record 0.5–10 s of the screen in the background; returns at once |
| `computer_record_result` | recording | collect a recording: timeline, actions, keyframe contact sheet, optional MP4 |

The set is exactly `EXPECTED_TOOLS` (`src/computer_use_mcp/cli.py`); the
`cortex-mcp probe` verifier refuses any tool not in it.

## start_session

```text
start_session(dry_run=False, require_approval=True, max_steps=30,
              max_retries_per_action=1, min_confidence=0.70,
              allowed_windows=None, allowed_processes=None, limits=None,
              resume_from_checkpoint=None, interference=None, image_delivery=None)
```

Creates a guarded session and returns the session state plus
`allowed_processes`, the effective `limits`, and `task_id`.

- `dry_run` defaults to **False** (live session). `dry_run=True` validates the
  full pipeline and dispatches nothing; every dry-run result message starts with
  the banner `DRY-RUN (no input dispatched):`, and its result token is `dry_run`
  with `ok:false`.
- `require_approval` defaults to True: interactive actions reply
  `requires_approval:true` until a call carries `approved=true`.
- `allowed_windows` / `allowed_processes` are allowlists; process identity is
  authoritative, and an unresolvable target while an allowlist is configured is
  a fail-closed rejection. Both also accept a comma/space-separated string.
- `limits` accepts any `Limits` field name (see
  [CONFIGURATION.md](CONFIGURATION.md)); unknown names or non-numeric values are
  rejected fail-closed (`invalid_limits`).
- `resume_from_checkpoint` resumes a session from a checkpoint FILE as a
  continuation — counters restored, never reset; the current environment is
  re-verified; approval is never resurrected. A resumed start reply carries
  `resumed: true`, `continuation_of` (the original session id) and
  `resumed_from_checkpoint` (the path that was loaded). HONEST SCOPE: the
  `before_session_end` checkpoint at stop time is written ONLY by a session
  that already carries the long-running runtime — i.e. a session that was
  itself started with `resume_from_checkpoint`; a FRESH session's stop writes
  NO checkpoint file, so resuming requires a checkpoint produced by a resumed
  session (or written externally). Checkpoints live at
  `<checkpoint dir>/<session_id>/checkpoint.json` (dir default
  `%TEMP%\computer-use-mcp\checkpoints`, env `COMPUTER_USE_MCP_CHECKPOINT_DIR`).
  A corrupt, tampered, or environment-mismatched checkpoint is a typed refusal
  (`invalid_checkpoint` / `checkpoint_error` / `resume_refused`) — never a
  partial restore.
- `interference` is a dict of Interference Guard policy sections (`focus_guard`,
  `attach_or_launch`, `dialog_sentinel`, `focus_continuity`, `hotkey_guard`);
  omitted fields get protective defaults; unknown sections/fields/values are
  rejected (`invalid_interference`). See [SAFETY.md](SAFETY.md) §Interference.
- `image_delivery` (`"image"` default / `"text"`) fixes the session's screenshot
  delivery: precedence param > env `CORTEX_IMAGE_DELIVERY` > default. A
  non-vision host model must pass `"text"` — one image part in a text-only
  conversation gets the provider request rejected (400) and the session dies
  permanently. Invalid values are rejected (`invalid_image_delivery`) before any
  session exists.

At the session cap the refusal lists the live sessions (`live_sessions`, most
idle first, with idle/in-flight/age) plus `idle_timeout_seconds`; the idle
reaper (below) frees forgotten sessions automatically.

## computer_observe / computer_screenshot

```text
computer_observe(session_id, visual_view=None, visual_view_density=None, pixel_evidence=None)
computer_screenshot(session_id)   # alias, same reply
```

Returns MCP content blocks: one `TextContent` carrying the JSON metadata
(`observation` model dump, `digest` (sha256 of the payload), `observation_id`,
`active_app`, the additive one-line `text_summary`) and one `ImageContent`
carrying the screenshot — the raw base64 never travels as text. In a text-mode
session the reply is ONE text block and never an image. This explicit tool is
not rate-gated; every capture writes an audit row.

Trailing optionals: `visual_view` selects `"raw"` (default pass-through,
byte-identical) or `"grid"` (a coordinate-grid annotation whose labels show
canonical screenshot-space coordinates; `displayed + crop_origin = screen` is a
reader-side fact). An explicit request — raw or grid — also attaches the
`spatial_text` block (the OCR-once UIA-derived text regions with bboxes and
provenance; `null` plus `spatial_text_available: false` when the read degraded —
honest absence, never an empty block). `visual_view_density` selects the grid
density — closed enum `coarse|standard|fine` (default `coarse`); raw/absent view
requests ignore it silently. `pixel_evidence` (strict boolean) opts into
per-region measured-evidence scores (`"pixel_evidence_mode": "on"`); the default
omits the field entirely. Unknown view/density values
(`invalid_visual_view`-class) and non-boolean `pixel_evidence`
(`invalid_pixel_evidence`) are rejected BEFORE any capture; a view that cannot
be derived returns the executed/observed result honestly with no image block and
the `view_derive_failed` marker — never a silent raw substitution.

## computer_zoom

```text
computer_zoom(session_id, region=None, visual_view="raw",
              visual_view_density=None, pixel_evidence=None)
```

A FRESH observation cropped to `region = [left, top, width, height]` in
canonical screenshot space (the space action coordinates are grounded in).
Observation-only: no action surface, no queue interaction. Behavior:

- Fresh capture each call (new `observation_id`; the audit event carries
  `source: computer_zoom`).
- Fail-closed region validation: a 4-int sequence, width > 0, height > 0, fully
  inside the captured frame, coordinate space not unverifiable; any violation is
  the typed `invalid_region` error naming the bounds — never a clipped guess,
  never a silent full-frame fallback.
- The served image is the native-resolution crop (the crop IS the zoom); the
  outbound budget ladder still governs the served bytes and `visual_view_scale`
  reports any applied scale.
- `visual_view="grid"` renders the grid on the crop with the crop origin baked
  into the label VALUES (a label at crop-local `(x, y)` displays
  `(left + x, top + y)`); the result metadata carries top-level `crop_region`.
- The `spatial_text` block is always attached, filtered to the crop with
  `crop_region` + `crop_origin` provenance.
- Result shape mirrors `computer_observe`; text-mode sessions get the
  single-text-block treatment.

## computer_execute

```text
computer_execute(session_id, action, x=None, y=None, text=None, keys=None,
                 delta=0, approved=False, expected_effect=None, x2=None, y2=None,
                 target=None, via=None, include_screenshot_after=None,
                 follow_ups=None, visual_view=None, plan=None, plan_action=None)
```

Validates and executes one action through the full pipeline: grounding →
validation (staleness) → risk/approval → execution → re-observe → verification.
There is deliberately no `key` action (single keys use `keypress`) and no
`triple_click`.

### The 13-action vocabulary

| Action | Parameters | Notes |
|---|---|---|
| `click` | `x`, `y` | screenshot coordinates |
| `double_click` | `x`, `y` | screenshot coordinates |
| `right_click` | `x`, `y` | one right-button press — opens a context menu |
| `drag` | `x`, `y`, `x2`, `y2` | start and end point, both required; both endpoints grounded and bounds-checked |
| `move` | `x`, `y` | hover the cursor, no click; outside plans motion and settle are instant |
| `type` | `text` | typed per transport (below); optional `via` |
| `keypress` | `keys` | up to 12 key names, e.g. `["ctrl", "s"]` |
| `hotkey` | `keys` | compound chord of 2–12 key names; single keys stay on `keypress` |
| `scroll` | `delta` | band `-20..20`; POSITIVE = UP, away from you; the wheel turns at the CURRENT CURSOR POSITION — move the pointer to the target region first |
| `wait` | `delta` (seconds) | short in-queue pause capped at 10 s (`waited_seconds=<effective>` in the message when clamped); for longer pauses use the `computer_wait` tool |
| `focus_window` | `target` | window title (max 200 chars); resolved and allowlist-checked before any foregrounding call |
| `ensure_app` | `target` | `process` or `process\|doc-token`; attach-or-launch probe (below) |
| `done` | — | completion marker (`result: "marker"`, no screen claim; does NOT end the session — only `stop_session` does) |

### Bounds (enforced at the model layer)

- Coordinates `x`/`y`/`x2`/`y2` and plan waypoint coordinates: `-8192..16384`
  (screenshot space).
- `text` ≤ 2000 chars; `expected_effect` ≤ 500 chars; `target` ≤ 200 chars;
  `keys` ≤ 12 names; `hold_ms` 0..5000 (click/right_click/keypress/hotkey only);
  `duration_ms` 0..5000 (timed motion, `move`/plan points); plan `settle_ms`
  0..200; `delta` `-20..20`.
- `follow_ups`: at most 5 items, each passing the full independent pipeline.
- A waypoint plan: at most 10 points, at most 5 actions per point, per-point
  `timeout_ms` 0..60000.
- Scope note: `hold_ms` and `duration_ms` are NOT parameters of
  `computer_execute` — they ride a `follow_ups` entry or a plan point action.
- Plain `hotkey` timing (DELTA 8): a hotkey WITHOUT `hold_ms` holds its keys
  for 50 ms before releasing them in reverse order — the zero-separation
  instant chord was ignored by Windows 11 Store apps (packaged Notepad:
  4 of 5 plain alt+f4 trials did nothing live; hold_ms=50 closed all 3 held trials).
  An explicit `hold_ms` wins; `hold_ms=0` is the explicit zero-duration split
  press/release with zero wait — NOT the old single-batch chord, which no longer
  exists for a hotkey. `keypress` actions are unchanged.
  During the 50 ms hold the user's own keystrokes can combine with the held
  modifiers; a stop mid-hold releases the keys (a stuck modifier is covered by
  the next chord's stuck-modifier sweep).

### Type transports (`via`, type actions only)

- `via=null`/`"sendinput"` (default): per-character `KEYEVENTF_UNICODE`
  injection.
- `via="clipboard"`: save the current clipboard text, set `CF_UNICODETEXT`,
  dispatch ONE ctrl+v chord, then best-effort restore the prior clipboard in
  `finally`. For OpenGL/console apps that drop per-character injection (field
  case: Blender's Python Console). The transport never bypasses any gate — the
  same classification, redaction and approval decision is computed from the
  identical text for both transports. A restore failure on the executed path is
  typed-annotated (`clipboard_restore=failed`) and never fails the action. On an
  ABORT between the set and the chord the prior text is restored (when readable)
  but the annotation is lost — a known abort-path limitation, disclosed in
  VERSIONS.md. A blind type (no readable focused target) reports
  `TYPE_UNCONFIRMED no-readable-target` with a hint to verify visually with a
  stated `expected_effect` (and, on the default transport, to retry with
  `via="clipboard"`). `via` on any non-type action, or an unknown value, is a
  typed `invalid_action` rejection.

### Approval and expected_effect

- `approved=true` authorizes this one call when policy requires approval (always
  for HIGH risk; interactive actions when the session requires approval). It
  never clears a CRITICAL block.
- `expected_effect` opts into semantic verification: a stated effect must be
  established by a deterministic tier (window/UI identity, process state, control
  text, focus change) — an effect satisfied only by a pixel change stays
  `uncertain`. State CHECKABLE effects, not "the screen changes".

### include_screenshot_after and follow_ups

- `include_screenshot_after=false` omits the image block from the response (that
  call then retains NO frame — no `image_id` exists for it). The DEFAULT
  executed-response image is a half-resolution JPEG;
  `include_screenshot_after=true` ships full resolution.
- `follow_ups` (≤ 5 items) queues actions that each re-ground from the fresh
  post-action capture. The queue continues while the attached window identity
  (hwnd/title/bounds) is unchanged and stops on TRUE staleness, safety
  rejection, approval requirement, validator/grounding rejection, or a named
  interference event. An executed item's `uncertain`/`failed` verdict rides its
  `follow_up_results` entry while the batch continues; results arrive in
  `follow_up_results` + `follow_ups_stopped_reason` (`null` = ran to completion).

### Waypoint plans (`plan`, `plan_action`)

A plan is an ordered list of waypoints (at most 10), each a screenshot-space
point with at most 5 explicit actions, explicit timing (`duration_ms` per point
or `transition_duration_ms` on the plan, `settle_ms` 0..200, `point_timeout_ms`
0..60000) and explicit `on_failure`/`on_uncertain` policies (`stop`/`continue`).
One waypoint step executes per call; `plan_action` is `continue` / `stop` /
`replace`. Under an active lease, `computer_execute` and a second `plan` are
refused with the typed `plan_lease` error; `computer_wait_status` and
`computer_fetch_image` stay allowed. Full wire rules, the lease, cursor rules
and the approval-once rule: `skills/cortex-skill/references/plan-protocol.md`
(and [REPLY-CONTRACT.md](REPLY-CONTRACT.md) for the step/terminal reply
shapes). A driver-facing doctrine walkthrough lives at
`benchmarks/tasks_hard/DRIVER-PROTOCOL.md`.

### ensure_app outcomes

`ensure_app` reattaches by identity, never by clicking blind. Outcomes are
typed in the `ensure_app_outcome` enum: `reattached` / `ambiguous` /
`not_found` / `launched` / `launch_rejected` / `launch_unresolved`. Launch
truth: the default policy is `launch="server"` — when no instance matches and NO
process allowlist is configured, any resolvable target may spawn server-side
(marked `LAUNCHED` in the message); with an allowlist, only allowlisted targets
may launch. `launch="driver"` (or env `CORTEX_ATTACH_OR_LAUNCH=driver`) restores
never-launch. Launch failures are `failed`, `ambiguous` is `uncertain`.
Needle→executable mapping for apps not on PATH: env `CORTEX_LAUNCH_PATHS`
(see [CONFIGURATION.md](CONFIGURATION.md)).

## computer_wait / computer_wait_status / computer_cancel_wait

- `computer_wait(session_id, duration_seconds, label=None)` BLOCKS up to one
  segment (env `CORTEX_WAIT_SEGMENT_SECONDS`, default 45 s, band 1..55 — the
  default derives from CORTEX_CLIENT_TIMEOUT_SECONDS (default 60) minus a 15 s
  margin; the band tops out at client timeout − 5 and an out-of-band override
  falls back to the derived default), then returns `status: "completed"` (with
  `elapsed_seconds` — act now) or `status: "running"` with the `wait_id`,
  `elapsed_seconds`, `remaining_seconds` and a poll hint. `duration_seconds`
  must be > 0 and finite; the hard ceiling is
  `min(limits.max_wait_seconds, 10800)` seconds — a request above the effective
  cap is REFUSED naming the cap (`wait_duration_exceeds_max`), never silently
  shortened. A wait consumes zero `max_actions` slots.
- `computer_wait_status(session_id, wait_id, block=True)` blocks for the NEXT
  segment (default) and returns `completed` / `running` / `cancelled`;
  `block=false` peeks without waiting.
- `computer_cancel_wait(session_id, wait_id)` ends a wait early: `cancelled`
  (with elapsed/remaining), `completed` (it had finished; remaining 0), or
  `failed` + `error` `wait_failed`. A duplicate cancel is safe (same numbers
  plus `already_cancelled: true`). Unknown and foreign ids get the
  byte-identical `unknown_wait`.
- Caps: 4 waits per session, 16 process-wide (`too_many_waits`); only the last 8
  finished waits are remembered per session (tombstones). If YOUR CLIENT cancels
  a blocking wait call, the wait keeps running — call `computer_wait_status`
  again. A plan lease refuses `computer_wait` / `computer_cancel_wait`;
  `computer_wait_status` stays allowed. A running wait is never idle-reaped; the
  idle count starts when the wait ends.

## computer_fetch_image

```text
computer_fetch_image(session_id, image_id)
```

Re-serves, as a real image block, exactly the bytes a reply of THIS session
carried under that `image_id` (`computer_observe`, `computer_screenshot`,
`computer_zoom`, `computer_execute`). The verified reply carries `image_id`,
`captured_at`, `age_seconds`, `width`/`height`, `image_format`, the
`image_outbound_scale` of the returned copy, the source `observation_id`/
`digest`, and the retention bounds. A fetched image is a picture of
`captured_at`, not the present: use it to look BACK and `computer_observe` for
NOW. Retention: 16 frames / 24 MB / 3600 s per session, in memory only. A frame
retired by a cap answers `image_expired` (with `retired_by`); a wrong, foreign,
or malformed id answers the byte-identical `image_not_found` (an id can never
probe another session's images); `store_disposed` after teardown. A text-mode
session never produces image bytes, so no id is ever issued on one. Fetch is a
read-only retrieval and stays ALLOWED during a plan lease.

## stop_session

```text
stop_session(session_id)
```

The kill switch: arms the stop token (checked before every physical input, at
every loop checkpoint, inside waits), closes the session, and audits `stop` +
`emergency_stop`. Every later call on that id fails closed with
`session_stopped`. Idempotent. Nothing a model outputs can reach the stop
setter.

## The idle reaper (session lifetime)

A session with no tool call for 15 minutes (env
`CORTEX_SESSION_IDLE_TIMEOUT_SECONDS`, band 60..86400, `0` disabled) and nothing
in flight is stopped automatically through the SAME kill path as `stop_session`
(audited with `source="idle_timeout"`; later calls get `session_stopped` naming
the inactivity stop). Any tool call addressing the session — including a
rejected one — resets the count. Idle sessions are swept lazily (every tool
entry, and in `start_session` before the limit check), so a registry full of
forgotten sessions frees itself. A running wait is never reaped. See
[CONFIGURATION.md](CONFIGURATION.md) for the knob.
