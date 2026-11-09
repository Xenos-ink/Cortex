# Task recipes

Whole tasks, end to end: the exact calls (copy-paste, then edit the values),
the replies to expect, and when to stop. Conventions: every ```json block is
preceded by `<!-- tool: NAME -->`; placeholders `"S"` `session_id`, `"W"`
`wait_id`, `"R"` `record_id`, `"I"` `image_id` — copy real values from the
replies. Validated against the real tool schemas by tests/test_wp_t_examples.py.

Each recipe states its call count next to the wasteful version it replaces.
Two of those "was N" accounts come from the real field session sess_a59fef27
(ZCode, GLM-5.3-Flash, 118 turns, chess on Chess.com): the board-game recipe
(it played click, click, `computer_wait` 8-10 s, `computer_observe`, often
`computer_zoom` — 4-5 calls for one move) and the recording recipe (its 3
separate-call recordings all ended before the action ran). The other "was N"
counts are illustrative comparisons, not field measurements.

## Open a URL

Goal: open a page in a browser (msedge.exe / chrome.exe / firefox.exe — pick
the one installed) and see it loaded. 2 calls when the browser is already
running; 3 after a cold launch.

Cortex cannot launch a URL — an `ensure_app` target passes the launch-needle
charset `[A-Za-z0-9._ -]` only, so `https://…` cannot be a target: under the
default launch="server" policy the reply is `result:"failed"` with
`ensure_app_outcome:"launch_rejected"`; under launch="driver" or a process
allowlist it is `not_found`. The reliable path: attach to the browser first,
and send the address-bar chord ONLY after the reply says `reattached` with
the browser window focused — ctrl+l in a non-browser window types into
whatever has focus. `launched` is NOT safe to chord on: the launch path
starts the process and returns WITHOUT waiting for or focusing a window, and
a batch is not stopped by `launched` — so never put `ensure_app` as item 0 of
the ctrl+l batch.

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "ensure_app", "target": "msedge.exe", "follow_ups": [{"action": "wait", "delta": 3}]}
```

Branch on `ensure_app_outcome`:

- `reattached` — the browser window is named and focused; go to the chord.
- `launched` — the process started but no window is focused yet: send the
  SAME ensure_app again; it now reattaches (and focuses), then go to the
  chord. IF THE RE-SEND ANSWERS `launched` AGAIN, stop and report — each
  `launched` started another process; do not loop. (The trailing `wait 3`
  gives a slow launch's window time to appear before the re-send.)
- `ambiguous` — instances exist but none matches your token: focus a concrete
  window first, `focus_window` with the full title.
- `launch_rejected` / `launch_unresolved` / `not_found` — report; do not
  click around.

Then, in ONE call, address bar → URL → enter:

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "hotkey", "keys": ["ctrl", "l"], "expected_effect": "the address bar has focus", "follow_ups": [{"action": "type", "text": "https://chess.com", "expected_effect": "the address bar shows the URL"}, {"action": "keypress", "keys": ["enter"]}]}
```

Expect `verified` on the items; the reply image shows the page. The page title
changes — a later `expected_effect` like "the tab title reads Chess.com" is the
checkable proof. Stop when the effect you stated is `verified`; read the image
on any `uncertain` before repeating anything.

## Open or focus an app

Goal: an app running and focused. 1 call.

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "ensure_app", "target": "notepad.exe", "follow_ups": [{"action": "wait", "delta": 3}]}
```

Read `ensure_app_outcome`: `reattached` (attached and focused) = done;
`launched` (started server-side; no window focused yet — send the same
ensure_app again to attach and focus; IF IT ANSWERS `launched` AGAIN, stop
and report: each `launched` started another process, do not loop);
`ambiguous` = instances exist but none matches your doc-token — focus a
concrete window with `focus_window` + the full title; `launch_rejected` /
`launch_unresolved` / `not_found` = the app cannot be attached or launched —
report it, do not click around. Stop at the first good outcome.

## Click a button and verify it

Goal: press a button and KNOW it worked. 1 call; +1 only if uncertain.

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "click", "x": 640, "y": 360, "expected_effect": "the Save dialog closes"}
```

- `verified`: done — do not re-observe to "confirm" a verified step.
- `uncertain`: LOOK AT THE ATTACHED IMAGE FIRST. Dialog gone / title changed /
  field populated → it landed; continue. Ambiguous → ONE cheap read
  (`computer_observe`, or wait for the UI to settle first) and decide from that.
  NEVER blind re-click: the first click may have landed; a second Save can
  produce a duplicate file or a confirm-overwrite dialog.
- `failed`: the evidence shows the effect is absent — now a corrected re-send
  is justified (new coordinates if the image showed why it missed).

## Type a long text

Goal: put a long or multi-line text into a field/editor. 1 call per ≤ 2000
chars (was: one per-character `type` that an auto-indenting editor mangled,
plus retype turns).

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "type", "text": "SELECT * FROM orders WHERE region = 'EMEA';", "via": "clipboard", "expected_effect": "the editor shows the full statement"}
```

Read `partial_delivery` / `delivered_chars` (mid-type focus drift typed PART of
the text — re-observe, then retype only the missing tail) and `paced=partial`
(the tail went in UNPACED — re-observe before anything else). Longer than 2000
chars: split at line boundaries into several clipboard types, re-observe after
each. Stop when the stated "the field reads …" is `verified` or settled by the
image.

## A board-game move plus waiting for the opponent — ONE call

Goal: play a move, give the opponent time, and see the position after their
reply. 1 call (the field session sess_a59fef27 played this as 4-5 calls:
click, click, `computer_wait` 8-10 s, `computer_observe`, often
`computer_zoom`).

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "click", "x": 500, "y": 400, "expected_effect": "my pawn is selected", "follow_ups": [{"action": "click", "x": 500, "y": 320, "expected_effect": "my pawn moved"}, {"action": "wait", "delta": 8}]}
```

Reading it:

- The TOP-LEVEL `result`/`verification` describe ITEM 0 ONLY — the select
  click (execute.py keeps the flat verdict fields on item 0). The move's own
  verdict is `follow_up_results[1]`; the end state is the attached image,
  which `image_after_item` names.
- The reply image (and `image_id`) shows the state AFTER THE LAST EXECUTED ITEM
  — after the wait, i.e. after the opponent replied. `image_after_item` names
  the item (here 2). `follow_up_results[i].result` is per item;
  `follow_ups_stopped_reason` null = the batch ran to completion.
- Canvas clicks often come back `uncertain` (pixels-only evidence — the field
  session's constant). Read the image: selection highlight / piece moved = it
  landed; continue. Ambiguous move state → ONE `computer_observe` after the
  wait, decide, and only then consider a corrective click.
- `computer_zoom` ONLY when a detail you must read is truly unreadable in the
  reply image (a clock, a coordinate label). Never zoom as a routine fifth call.

Stop when the position confirms your move (and the opponent's reply, if that is
the goal). If the batch STOPPED (`follow_ups_stopped_reason` non-null), only the
items after the last executed one are unexecuted — send those, not the whole call.

## Wait for a page load / an element to appear

Goal: a load or remote reply you cannot act through. Two tools, two cases:

- The wait sits BETWEEN your actions (click "submit", then wait for the result
  page, then verify): keep it inside the SAME call as a trailing follow_up —
  one call, and the reply image shows the post-wait state:

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "click", "x": 640, "y": 520, "expected_effect": "the form submits", "follow_ups": [{"action": "wait", "delta": 5}]}
```

- The wait is a STANDALONE pause (nothing to dispatch meanwhile, could run
  minutes, or you may want to cut it short): the `computer_wait` tool — it tells
  you when over and costs no actions:

<!-- tool: computer_wait -->
```json
{"session_id": "S", "duration_seconds": 90, "label": "page load"}
```

If it answers `status: "running"`, continue with:

<!-- tool: computer_wait_status -->
```json
{"session_id": "S", "wait_id": "W"}
```

Repeat until `completed`. A wait reply carries NO image — before acting, take
ONE `computer_observe` to see what the wait produced, and act on that. Rule
of thumb: waiting INSIDE a flow → trailing follow_up; waiting INSTEAD of a
flow → `computer_wait` (a timed sleep — it does not watch for an element; the
image after it is your check). Never a large flat `{"action": "wait"}` delta
(capped at 10 s), never an observe-polling loop.

## Read small text with zoom and the grid

Goal: read a small label or read off exact coordinates. 1 call.

<!-- tool: computer_zoom -->
```json
{"session_id": "S", "region": [900, 300, 480, 320], "visual_view": "grid"}
```

The crop is native resolution; grid labels are CANONICAL screenshot coordinates
(a label at crop-local (x, y) is screen (left + x, top + y)) — aim clicks
directly at them. `spatial_text` lists the UIA text regions with coordinates.
Zoom again only for a different region; stop when the text is read.

## Record an effect around an action

Goal: prove a transient effect (an animation, a toast, a menu that closes) that
one screenshot cannot. 2 calls (the field session sess_a59fef27 started
`computer_record` as a SEPARATE call 3 times — every recording ended 15-30 s
before the model's next tool call, so the timelines were empty).

<!-- tool: computer_execute -->
```json
{"session_id": "S", "action": "click", "x": 980, "y": 44, "expected_effect": "the toggle flips on", "record": {"duration_s": 4, "include_video": true}}
```

The reply's `record` block: `started: true`, `record_id`, `status: "recording"`,
`ends_at`. A recording problem NEVER refuses the action (`started: false` names
the reason — the action itself still ran). Then collect:

<!-- tool: computer_record_result -->
```json
{"session_id": "S", "record_id": "R"}
```

Reading it:

- `timeline` — WHEN the screen moved (segments with bbox + change ratio);
  `actions` — what Cortex dispatched inside the window (this call's own click
  is in there; empty = nothing ran).
- `keyframes` — timestamped contact sheet; `computer_fetch_image` any keyframe's
  `image_id` for a full-size look.
- The video: this example asked with `"include_video": true` — without it (or
  `"save": true`) the collect reply carries `video.omitted: "not_requested"`
  and there is no video entry at all. Delivery defaults to `video_delivery:
  "file"` — the MP4 is written under the recordings folder and the reply
  carries its absolute `path` for the human (or a host) to open. `"inline"`
  is ONLY for hosts known to pass video to the model: the field host
  (ZCode) truncated each inline video result at 49,998 characters.
  `video.omitted` names a closed reason; `over_budget`, `write_failed`, and
  an `encode_failed` carrying `aborted: true` retry on a later collect.

Stop when the timeline/keyframes answer the question (did the effect happen,
and when).

## Look back at an earlier frame

Goal: re-examine a frame a past reply carried (a keyframe, a click point). 1 call.

<!-- tool: computer_fetch_image -->
```json
{"session_id": "S", "image_id": "I"}
```

Read `captured_at`/`age_seconds` — it is a picture of the past; if you need
NOW, `computer_observe`. Keyframes additionally carry `frame_to_canonical`
(`{"scale", "origin"}`) to convert stored pixels to screen coordinates (divide
a downscaled served copy by its `image_outbound_scale` first). No `image_id`
from that reply? It retained nothing — observe instead; never hunt for an id.
Stop once you have read what you came for.

## Finish cleanly

Goal: end the session so nothing further can dispatch. 1 call.

<!-- tool: stop_session -->
```json
{"session_id": "S"}
```

`done` is NOT the finish — it is a marker inside a flow and does not end the
session; only `stop_session` does. Afterwards every call on that id fails
closed with `session_stopped`. Report the result to the user with the evidence
verdicts you actually hold — `uncertain` stays "unproven", never a success claim.
