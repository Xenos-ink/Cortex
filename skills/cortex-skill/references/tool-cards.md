# Tool cards (one card per tool)

How to call each of the twelve Cortex tools and how to read each reply. The
decision table in SKILL.md picks the tool; these cards give the exact call.

Conventions (both enforced by tests/test_wp_t_examples.py):

- Every ```json block is preceded by a `<!-- tool: NAME -->` line naming the
  registered tool the arguments validate against.
- Placeholders: `"S"` = your real `session_id`; `"W"` a `wait_id`; `"R"` a
  `record_id`; `"I"` an `image_id` — copy the real value from the reply that
  issued it.

---

## start_session

- **Use when**: once, first — before any other call. The reply's `session_id` is REQUIRED on every later call.
- **Don't use when**: a live session already exists — reuse its `session_id`; never restart mid-task (raise `max_steps` instead).
- **Minimal call**:

<!-- tool: start_session -->
```json
{"max_steps": 100}
```

- **Common variants**:

<!-- tool: start_session -->
```json
{"max_steps": 300, "limits": {"max_actions": 300}}
```

Non-vision host only (this removes EVERY image from every reply, and `computer_record` refuses with `text_session_no_screen` — vision hosts must NOT copy it):

<!-- tool: start_session -->
```json
{"max_steps": 100, "image_delivery": "text"}
```

Unattended driving only where the host policy explicitly allows it:

<!-- tool: start_session -->
```json
{"require_approval": false, "max_steps": 100, "allowed_processes": ["mspaint.exe"]}
```

- **Read the reply**: `session_id` (copy it everywhere); the effective `limits` and the effective action cap — the message warns when `max_steps` and `max_actions` differ (the smaller governs); the `image_delivery` actually in force. Non-vision host: `image_delivery="text"` — one image block in a text-only history kills the session permanently.
- **Common mistake**: restarting a session because actions hit the cap — the refusal (`limit_exceeded` / the `max_steps` refusal) is fixed by a higher `max_steps` / `limits={"max_actions": N}` in a NEW session, and only then.

## stop_session

- **Use when**: the user's task is genuinely complete (or you must abandon it) — it arms the kill path; no further input runs.
- **Don't use when**: the task continues — an idle session is reaped on its own after 15 min. `done` is NOT a finish (it is a marker inside a flow); only `stop_session` ends the session — but never call `stop_session` mid-flow just to "reset" a stuck action.
- **Minimal call**:

<!-- tool: stop_session -->
```json
{"session_id": "S"}
```

- **Common variants**: none — it takes only `session_id`.
- **Read the reply**: the stop audit; the session is closed — later calls fail closed with `session_stopped`.
- **Common mistake**: stopping to "reset" a stuck action — read the verdict instead; a stop discards the session's evidence store.

## computer_observe

- **Use when**: you need the screen NOW and the last reply carried no image (a rejection, an opt-out, text mode, or the task's first look). Grounding material: dimensions, active window, cursor.
- **Don't use when**: the last execute/zoom reply already carried the post-action image — read it; or when you need small text — `computer_zoom`.
- **Minimal call**:

<!-- tool: computer_observe -->
```json
{"session_id": "S"}
```

- **Common variants**:

<!-- tool: computer_observe -->
```json
{"session_id": "S", "visual_view": "grid"}
```

<!-- tool: computer_observe -->
```json
{"session_id": "S", "show_pointer": false}
```

- **Read the reply**: the image + `image_outbound_scale` (divide read-off coordinates by it before aiming); `text_summary` — window title/process, cursor, changed/unchanged since your last observation; `image_id` (the frame is fetchable later by it); with a grid view, `spatial_text` (UIA text regions, canonical coordinates).
- **Common mistake**: re-observing immediately after every execute — the execute reply image already shows the result; that is the field session's main wasted turn.

## computer_screenshot

- **Use when**: never prefer it — it exists only as a compatibility alias of `computer_observe` (same capture, same metadata).
- **Don't use when**: always — call `computer_observe` instead.
- **Minimal call**:

<!-- tool: computer_screenshot -->
```json
{"session_id": "S"}
```

- **Common variants**: none.
- **Read the reply**: identical shape to `computer_observe`.
- **Common mistake**: reaching for it out of habit from other computer-use tools.

## computer_zoom

- **Use when**: small text or fine detail in ONE area — a fresh capture cropped to `region=[left, top, width, height]` (screenshot space), served at native resolution; or you want the coordinate grid over a crop.
- **Don't use when**: the region is readable in the image you already have; or you need the whole screen — `computer_observe`.
- **Minimal call**:

<!-- tool: computer_zoom -->
```json
{"session_id": "S", "region": [900, 300, 480, 320]}
```

- **Common variants**:

<!-- tool: computer_zoom -->
```json
{"session_id": "S", "region": [900, 300, 480, 320], "visual_view": "grid", "visual_view_density": "fine"}
```

- **Read the reply**: the native-res crop; grid labels show CANONICAL screenshot coordinates (`crop-local + crop_origin = screen`); `spatial_text` regions; `image_id`. Observation-only — it never dispatches input.
- **Common mistake**: a region outside the captured frame — typed `invalid_region`, never a clipped guess; shrink/shift the region. And zooming again when the previous reply's image was already legible.

## computer_execute

The one tool that dispatches input. `session_id` and `action` are required;
everything else depends on the action. State `expected_effect` whenever you
can — it is what verification checks; the reply carries the post-action image
(read it instead of re-observing).

- **Use when**: every input — every click, typed character, keypress, scroll, drag, move; plus `ensure_app`, flat waits, the waypoint-plan handshake.
- **Don't use when**: you only want to look — `computer_observe`/`computer_zoom` dispatch nothing and cost no actions.
- **Minimal call**:

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "click", "x": 640, "y": 360, "expected_effect": "the OK dialog closes"}
```

- **Common variants**: see the sub-cards below.

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "type", "text": "hello world", "expected_effect": "the field reads hello world"}
```

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "click", "x": 500, "y": 400, "expected_effect": "my pawn reaches e4", "follow_ups": [{"action": "wait", "delta": 8}]}
```

- **Read the reply**: `result`/`ok` and the `verification` block (`outcome`, `evidence`, `verification_method`) — `verified` means done, move on; `uncertain` means dispatched but unproven — READ THE ATTACHED IMAGE before doing anything; the post-action image (+ `image_id`, `pointer`, `last_click`) — the primary evidence; `image_outbound_scale` before aiming further clicks.
- **Common mistake**: a re-click on `uncertain` — the first input may have landed; a double-apply (two moves, two saves) is worse than one cheap read.

### computer_execute · click / double_click / right_click

- **Use when**: pressing a button, a link, a board square (`click`); opening a context menu (`right_click`); selecting a word or double-tap targets (`double_click`). No `triple_click` exists — use `double_click` or repeated clicks.
- **Don't use when**: you have not verified the target position from the current image; coordinates from an old frame get one automatic staleness re-check, not infinite forgiveness.
- **Minimal call**:

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "click", "x": 640, "y": 360, "expected_effect": "the ribbon button is pressed"}
```

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "right_click", "x": 300, "y": 220, "expected_effect": "the context menu is open"}
```

- **Read the reply**: the verification outcome; the image — `last_click` and the Pressed sprite mark where THIS call clicked; `uncertain` on a canvas click is common (no UIA target, pixels-only evidence): read the image first.
- **Common mistake**: re-sending the same click because the verdict was not `verified` — inspect, then decide.

### computer_execute · type

- **Use when**: text into a field. `text` ≤ 2000 chars — split longer text at line boundaries into several types and re-observe between them.
- **Don't use when**: the target app drops per-character injection (OpenGL/console, auto-indenting editors) — use `via="clipboard"`; or a single key — `keypress`.
- **Minimal call**:

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "type", "text": "quarterly report", "expected_effect": "the field reads quarterly report"}
```

- **Common variants**:

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "type", "text": "line one\nline two", "via": "clipboard", "expected_effect": "both lines are in the editor"}
```

- **Read the reply**: `partial_delivery` + `delivered_chars` (a mid-type focus drift typed PART of the text — retype only deliberately, after re-observing; absence = nothing dispatched); `paced=partial` = the tail went in UNPACED — re-observe before anything else.
- **Common mistake**: retyping the whole string after a partial delivery — you may double the first half.

### computer_execute · keypress / hotkey

- **Use when**: one key (`keypress`, `keys=["enter"]`); a chord of 2-12 key names (`hotkey`, `keys=["ctrl","s"]`). Names only — "ctrl", "a", "enter", "esc"; never text.
- **Don't use when**: you mean to type text; or a single-key press on `hotkey` (rejected — 2-12 keys).
- **Minimal call**:

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "hotkey", "keys": ["ctrl", "s"], "expected_effect": "the document is saved"}
```

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "keypress", "keys": ["enter"]}
```

- **Read the reply**: the verification outcome (save/save-as dialogs change the window title — a checkable effect); a typed rejection carrying `invalid_action` teaches the closest valid shape.
- **Common mistake**: `{"action": "key", ...}` — does not exist; and text sentences inside `keys`.

### computer_execute · scroll

- **Use when**: wheel scrolling a page or list. POSITIVE delta = UP (away from you); band -20..20. The wheel turns at the CURRENT CURSOR POSITION — `move` there first.
- **Don't use when**: a drag is needed (scrollbar thumb), or you expect the wheel to reach a far-off element — chain scrolls with `follow_ups`.
- **Minimal call**:

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "move", "x": 640, "y": 400, "follow_ups": [{"action": "scroll", "delta": -5}]}
```

- **Read the reply**: the image shows the new scroll position (compare against the previous frame).
- **Common mistake**: scrolling without moving the pointer — the wheel lands wherever the cursor was.

### computer_execute · drag

- **Use when**: moving a slider, a window, a piece by dragging. `x`/`y` = start, `x2`/`y2` = end — BOTH endpoints required.
- **Don't use when**: click-to-select then click-to-place is available (board games usually) — two clicks verify better.
- **Minimal call**:

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "drag", "x": 500, "y": 400, "x2": 700, "y2": 300, "expected_effect": "the slider sits at the right end"}
```

- **Read the reply**: the image (the piece/slider position); `uncertain` is likely — pixels-only evidence.
- **Common mistake**: one endpoint only — `invalid_action` with the shape hint.

### computer_execute · move

- **Use when**: parking the pointer BEFORE a scroll, or positioning for hover UI. Needs both `x`/`y`.
- **Don't use when**: as a "verify" step — a move alone usually has no visible effect and degrades to `uncertain` unless you state a hover effect.
- **Minimal call**:

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "move", "x": 640, "y": 400}
```

- **Read the reply**: `pointer` in later image replies confirms where the cursor sits.
- **Common mistake**: expecting a move to click — it never presses.

### computer_execute · wait / done (markers)

- **Use when**: `wait` — a SHORT in-queue pause (≤ 10 s) inside `follow_ups`, e.g. letting the opponent move; `done` — marking "my part of this flow is handled". Both are markers: `result:"marker"`, no screen claim, `done` does NOT end the session.
- **Don't use when**: a pause over 10 s, or one you may cancel — the `computer_wait` tool; and never `done` instead of `stop_session` to finish.
- **Minimal call**:

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "click", "x": 500, "y": 400, "follow_ups": [{"action": "wait", "delta": 8}]}
```

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "done"}
```

- **Read the reply**: `waited_seconds=<effective>` when the cap bit; a `wait` delta outside -20..20 is REJECTED (the queue item takes seconds here, not wheel notches).
- **Common mistake**: a large `delta` as a long sleep — rejected or capped; use `computer_wait`.

### computer_execute · ensure_app / focus_window

- **Use when**: attach to (and, under the default launch="server" policy, launch) an app: `target="<process>"` or `"<process>|<doc-token>"`; bring an existing window forward by title: `focus_window`. The launch needle allows `[A-Za-z0-9._ -]` only — no paths, no URLs.
- **Don't use when**: the app window is already focused (its title is in the last reply's `text_summary`); or the target contains a path/URL — the launch needle gate refuses it (`result:"failed"` with `ensure_app_outcome:"launch_rejected"` when a launch is attempted).
- **Minimal call**:

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "ensure_app", "target": "mspaint.exe"}
```

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "focus_window", "target": "Untitled - Notepad"}
```

- **Read the reply**: the typed `ensure_app_outcome` — `reattached` (good: an instance matched and its window is now focused), `launched` (the process was started server-side; NO window was waited for or focused — send the same `ensure_app` again, which now reattaches and focuses), `ambiguous` (instances exist but none matches the doc-token you gave — retry with a different token, or `focus_window` with a full title), `launch_rejected` (the needle failed the charset gate under the default launch="server" policy; reply is `result:"failed"`) / `launch_unresolved` / `not_found` (no instance under launch="driver" or an allowlist — report, do not guess); verification_method `ensure_app_probe` (no screen claim).
- **Common mistake**: putting a URL in `target` — under the default launch="server" the reply is `result:"failed"` with `ensure_app_outcome:"launch_rejected"`, under launch="driver" or an allowlist `not_found`; see references/recipes.md for the URL recipe.

### computer_execute · follow_ups

- **Use when**: 2-6 DETERMINISTIC steps known in advance (click → type → save; move + wait; click + click + wait for the opponent). Each item re-grounds from the fresh post-action capture; the reply image shows the state AFTER THE LAST EXECUTED ITEM (`image_after_item` names it) — one call can act, wait, and see.
- **Don't use when**: any step depends on reading the screen first — issue that step as its own call; or more than 5 items.
- **Minimal call**:

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "click", "x": 500, "y": 400, "expected_effect": "my pawn is selected", "follow_ups": [{"action": "click", "x": 500, "y": 320, "expected_effect": "the pawn moves"}, {"action": "wait", "delta": 8}]}
```

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "click", "x": 400, "y": 300, "follow_ups": [{"action": "type", "text": "report"}, {"action": "hotkey", "keys": ["ctrl", "s"], "hold_ms": 150}]}
```

- **Read the reply**: `follow_up_results[i].result` per item AND `follow_ups_stopped_reason` (null = the batch ran to completion); `image_after_item` — WHICH item's frame the attached image shows; an `uncertain`/`failed` verdict on an executed item does NOT stop the batch — read that item's entry.
- **Common mistake**: after a batch STOP, re-sending the whole call — earlier items already dispatched; send only the unexecuted items (from `follow_up_results`).

### computer_execute · record= (record around the action)

- **Use when**: the effect is transient (an animation, a toast, a menu that closes) and ONE post-action screenshot cannot prove it. The recording starts JUST BEFORE this call's first input; collect with `computer_record_result`.
- **Don't use when**: a plain `computer_record` in a separate call — with 15-30 s model turns it always ends before you act (the field session lost all 3 recordings this way).
- **Minimal call**:

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "click", "x": 980, "y": 44, "expected_effect": "the toggle flips on", "record": {"duration_s": 4}}
```

- **Common variants**:

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "click", "x": 980, "y": 44, "record": {"duration_s": 6, "region": [800, 200, 640, 480], "include_video": true}}
```

- **Read the reply**: the additive `record` block — `started: true` + `record_id` (collect with `computer_record_result`; its timeline contains THIS call's actions) or `started: false` with a closed `reason` (`recording_limit`, `no_action`, ...) — a recording problem NEVER refuses the action; `cancelled: "action_refused"` = the action was refused before input and the recording was cancelled. Without `"include_video": true` (or `"save": true`) the recording still runs, but the collect reply carries `video.omitted: "not_requested"` — no video was ever asked for.
- **Common mistake**: treating a `started: false` as an action failure — the action itself ran.

### computer_execute · approval (requires_approval / not_executed)

- **Use when**: a reply comes back `ok:false, result:"not_executed", requires_approval:true` — a risk gate held the action; nothing dispatched, so nothing to undo. `approved` is a plain boolean (no token): read the gate's reason, and when the user's policy permits this action, re-send with `"approved": true` (one turn). CRITICAL-risk actions are never approvable. When the user's policy allows unattended driving, start the session with `require_approval: false` (only where host policy explicitly allows); do NOT pre-approve gates you have not read. In a BATCH a hold at item k>0 shows only as the queue stop reason `approval_required` — `follow_ups_stopped_reason: "approval_required"` (the same token a plan step's `failure_reason` carries) with `follow_up_results[k].result: "not_executed"` — the top-level verdict stays item 0's (pipeline/agent.py:358, :2000-2005); re-send ONLY the unexecuted items, with `approved: true`.
- **Don't use when**: the reply is any OTHER rejection (`rejected`, `invalid_action`, safety) — approval never fixes those; fix the named complaint. Inside a plan, approval is decided ONCE on the first plan call and inherited by every step — a gated step cannot be fixed by re-sending: `plan_action="stop"`, then resubmit a plan containing ONLY the points not yet executed (from `remaining`/`not_executed`).
- **Batches**: `approved` applies to EVERY item (pipeline/agent.py:1609 — the safety policy evaluates each item independently), and the queue stops at an approval hold (agent.py:2000-2005). Held at item 0: NOTHING ran — re-send the identical batch with `"approved": true`. Held at item k>0: the items before k already ran — send ONLY the unexecuted items in a new call, with `"approved": true`.
- **Minimal call**: the RE-SEND, after you have read the gate's reason. Never use it as a first send.

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "click", "x": 640, "y": 360, "approved": true}
```

- **Read the reply**: the re-send's normal verdict — the approval applied to this call only.
- **Common mistake**: re-sending a PART-executed batch whole — the earlier items double-apply; per-item `follow_up_results` tells you exactly which items never ran.

### computer_execute · the waypoint plan handshake (plan / plan_action)

- **Use when**: one call must walk several LOCATIONS with actions, with explicit timing and per-step verification. Read references/plan-protocol.md BEFORE submitting — a plan missing any explicitness field is rejected before any motion.
- **Don't use when**: `follow_ups` covers it (one window, known coordinates); a plan and the flat surface are mutually exclusive on one call.
- **Minimal call**:

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "done", "plan": {"points": [{"id": "open-menu", "x": 420, "y": 180, "actions": [{"action": "click"}], "arrival_condition": "menu is open", "duration_ms": 120, "settle_ms": 60, "timeout_ms": 5000, "on_failure": "stop", "on_uncertain": "stop"}, {"id": "save", "x": 850, "y": 720, "actions": [{"action": "click"}], "arrival_condition": "the document is saved", "duration_ms": 120, "settle_ms": 60, "timeout_ms": 10000, "on_failure": "stop", "on_uncertain": "continue"}], "transition_duration_ms": 120, "point_timeout_ms": 10000, "settle_ms": 60, "on_failure": "stop", "on_uncertain": "continue"}}
```

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "done", "plan_action": "continue"}
```

- **Read the reply**: the first call executes ONE step and returns `blocked: true` with `next_step`/`remaining`; a plan LEASE is now active — flat actions, observe/zoom and `computer_wait` are refused with `plan_lease` until the lease releases. Step/terminal replies carry `failure_reason` (closed vocabulary), `not_executed`, `evidence_by_backend`. `ok` is true ONLY when the terminal `result` is `verified`.
- **Common mistake**: re-observing under a lease (refused — costs nothing but a turn); resubmitting the whole plan after a stop (re-runs the executed prefix — a resubmitted plan starts a fresh lease).

## computer_wait

- **Use when**: waiting for something you cannot act on yet — a page load, an opponent's move, a long install. Blocks up to one segment per call (default 45 s) and TELLS you when over; costs zero `max_actions`.
- **Don't use when**: the pause is INSIDE a batch of actions — a trailing `{"action": "wait"}` follow_up keeps the same call; or you expect under ~10 s inside a flow — the flat wait.
- **Minimal call**:

<!-- tool: computer_wait -->
```json
{"session_id": "S", "duration_seconds": 90, "label": "waiting for the opponent"}
```

- **Common variants**:

<!-- tool: computer_wait -->
```json
{"session_id": "S", "duration_seconds": 5}
```

- **Read the reply**: `status:"completed"` + `elapsed_seconds` — ACT NOW; `status:"running"` + `wait_id` + `remaining_seconds` — continue with `computer_wait_status`. Over the effective cap (`min(max_wait_seconds, 10800)`) the request is REFUSED, never shortened.
- **Common mistake**: polling `computer_observe` every few seconds instead of one wait — each poll is a full turn (~22 s in the field session).

## computer_wait_status

- **Use when**: a `computer_wait` replied `status:"running"` — block for the NEXT segment; repeat until `completed`. `block=false` peeks without waiting.
- **Don't use when**: no live `wait_id` exists — the tool cannot conjure one; start with `computer_wait`.
- **Minimal call**:

<!-- tool: computer_wait_status -->
```json
{"session_id": "S", "wait_id": "W"}
```

- **Common variants**:

<!-- tool: computer_wait_status -->
```json
{"session_id": "S", "wait_id": "W", "block": false}
```

- **Read the reply**: `completed` (act now) / `running` (call again) / `cancelled` (someone ended it).
- **Common mistake**: re-calling `computer_wait` with a fresh duration instead of continuing the live wait — you lose `remaining_seconds` and stack waits (caps: 4 per session, 16 process-wide).

## computer_cancel_wait

- **Use when**: the thing you waited for appeared early, or the wait is pointless — end it now.
- **Don't use when**: the wait already completed — the reply says `completed` with `remaining: 0` (that is success, not an error).
- **Minimal call**:

<!-- tool: computer_cancel_wait -->
```json
{"session_id": "S", "wait_id": "W"}
```

- **Common variants**: none — duplicates are safe (`already_cancelled: true`).
- **Read the reply**: `cancelled` with elapsed/remaining, or `completed`, or `failed` + `wait_failed`.
- **Common mistake**: abandoning a running wait without cancelling — it holds one of the 4 per-session wait slots.

## computer_fetch_image

- **Use when**: you need to LOOK BACK at a frame an earlier reply carried — re-examine a keyframe, re-check a click point from its `image_id`.
- **Don't use when**: you need the screen NOW — `computer_observe`; or no `image_id` exists (a reply that carried no image retained NOTHING — never hunt for an id, re-observe).
- **Minimal call**:

<!-- tool: computer_fetch_image -->
```json
{"session_id": "S", "image_id": "I"}
```

- **Common variants**: none — `image_id` comes from the reply that carried the image.
- **Read the reply**: the exact stored frame + `captured_at`/`age_seconds` (a fetch is a picture of the past, not the present); `frame_to_canonical` (`{"scale", "origin"}`) to map keyframe pixels to screen coordinates — divide a downscaled copy by `image_outbound_scale` first.
- **Common mistake**: treating a fetched old frame as current state — check `age_seconds` before acting on it.

## computer_record

- **Use when**: recording a screen effect WITHOUT acting inside this call (or around a plan you will drive next); 0.5–10 s, monitor or `region`. It returns AT ONCE — keep calling tools while it records.
- **Don't use when**: the effect happens around one of YOUR actions — `computer_execute(..., record={...})` in the same call; a separate `computer_record` ends before your next turn (all 3 field recordings died this way).
- **Minimal call**:

<!-- tool: computer_record -->
```json
{"session_id": "S", "duration_s": 6}
```

- **Common variants**:

<!-- tool: computer_record -->
```json
{"session_id": "S", "duration_s": 8, "region": [800, 200, 640, 480], "fps": 10, "save": true}
```

- **Read the reply**: the marker — `record_id`, `status:"recording"`, `ends_at`, effective fps/region. Bands are fail-closed: duration 0.5..10 s, fps 1..10, keyframes 2..12 — out of band is refused, never shortened. Caps: 1 active recording per session, 2 per process (`recording_limit`). Without `include_video` (or `save`) — on this call or the later collect — the collect reply carries `video.omitted: "not_requested"`: the timeline and keyframes exist, no video was ever requested.
- **Common mistake**: collecting immediately with `block=true` and doing nothing else — you idle one turn; keep working, collect later (or start it with execute `record=`).

## computer_record_result

- **Use when**: collecting a recording (after `computer_record` or an execute `record=`): the change timeline, the actions dispatched inside the window, the keyframe contact sheet, and on request the video.
- **Don't use when**: the recording is still mid-flight and you have other work — peek with `block=false` instead of blocking.
- **Minimal call**:

<!-- tool: computer_record_result -->
```json
{"session_id": "S", "record_id": "R"}
```

- **Common variants**:

<!-- tool: computer_record_result -->
```json
{"session_id": "S", "record_id": "R", "block": false}
```

Ask for the video (the result call's value wins over the start flag):

<!-- tool: computer_record_result -->
```json
{"session_id": "S", "record_id": "R", "include_video": true}
```

<!-- tool: computer_record_result -->
```json
{"session_id": "S", "record_id": "R", "cancel": true}
```

- **Read the reply**: `timeline` segments (WHEN the screen moved, how much), `actions` (what Cortex dispatched inside the window — empty means nothing ran), `keyframes` (each with its `image_id` — fetch one at full size); the video entry exists ONLY when `include_video` (or `save`) was set — otherwise the reply carries `video.omitted: "not_requested"`. With the default `video_delivery="file"` the video entry carries the absolute MP4 `path` (plus `video_path` on the payload); `video_delivery="inline"` is only for hosts known to pass video. `video.omitted` names a closed reason; retried on a later collect: `over_budget`, `write_failed`, or an `encode_failed` carrying `aborted: true`.
- **Common mistake**: `video_delivery="inline"` on a host that truncates large tool results (the field host cut each video result at 49,998 characters) — the MP4 vanishes from the reply; use the default file path.
