# Worked examples (reference)

Read the worked examples when a verdict is ambiguous in practice, not just in theory.

## The save click

You click **Save** in an editor. The reply comes back `uncertain`, and the image shows
the window still open with the dialog up.

- The verdict is not a failure: the click was dispatched and the file may well be
  written. It is not a success either: nothing proved it.
- **Look at the image.** Does the title bar show a modification marker? Does the
  editor's status region report the save? The image may settle it outright (case a).
- The image is not a probability: reading `verification.confidence: 0.9` and calling it
  a 90 % chance the save happened is a category error — a number does not inspect the
  screen. `confidence` is not a success probability.
- **Do not click Save again just because the verdict was not `verified`.** A second
  click may produce a second save, a duplicate filename, or a confirm-overwrite
  dialog. Inspect first, decide after.

### (a) When the image settles the question

A `type` into a field returns `uncertain` (the read-back could not confirm), and the
attached image shows the field containing the full string: the evidence answers the
stated effect — treat the type as done and move on. Nothing was re-typed. The same
shape: a `click` returns `uncertain` (focus-only, no pixels to judge) and the image
shows the ribbon button now pressed — it landed; continue.

### (b) When the image cannot settle it

A `click` on Save returns `uncertain`; the image still shows the dialog mid-animation.
Nothing distinguishes "saved" from "not yet saved".

1. **Keep the uncertainty.** Report the step as unproven; do not say the save happened.
2. **Buy one cheap read.** One `computer_observe` after the UI settles — or a
   `computer_fetch_image` of a later frame by its `image_id` — costs one call and can
   end the ambiguity.
3. **Decide from that.** Saved state → it happened, continue. Dialog still asking,
   unchanged → the effect is absent, and re-sending the click is now justified, not
   blind.
4. **If even that is ambiguous**, say so and stop guessing. An honest unproven step
   beats a fabricated success.

## An example plan

This declaration passes its own explicitness gate (copy it, then edit the values):

```json
{"points": [
    {"id": "focus-field", "x": 300, "y": 400, "actions": [],
     "arrival_condition": "the cursor is in the field", "duration_ms": 120,
     "settle_ms": 60, "timeout_ms": 5000, "on_failure": "stop", "on_uncertain": "stop"},
    {"id": "type-and-save", "x": 300, "y": 400,
     "actions": [{"action": "type", "text": "hello",
                  "expected_effect": "the field reads hello"},
                 {"action": "hotkey", "keys": ["ctrl", "s"]}],
     "arrival_condition": "the document is saved", "duration_ms": 0,
     "settle_ms": 100, "timeout_ms": 10000, "on_failure": "stop", "on_uncertain": "continue"}],
 "transition_duration_ms": 120, "point_timeout_ms": 10000, "settle_ms": 60,
 "on_failure": "stop", "on_uncertain": "continue"}
```

Drive it: submit with `action="done"`, read the step reply, then
`computer_execute(session_id, action="done", plan_action="continue")` per step.
`"actions": []` on the first point declares a crossing point. Point 2 carries
per-point overrides (`duration_ms: 0`, `settle_ms: 100`) to show the plan-or-point
rule; with the per-point keys present, the plan-level ones could be dropped — but
EVERY point must then state them.

## A wait loop

```text
computer_wait(session_id, 200)          # blocks up to one 45 s segment
-> status:"running", wait_id:"wt_...", remaining_seconds:155
computer_wait_status(session_id, "wt_...")   # blocks the next segment; repeat
-> status:"completed", elapsed_seconds:200   # ACT now
```

Need it gone early? `computer_cancel_wait(session_id, "wt_...")` answers
`cancelled` with elapsed/remaining, or `completed` if it had already finished.

## A follow_ups batch

```text
computer_execute(session_id, action="click", x=10, y=20, follow_ups=[
    {"action": "type", "text": "report"},
    {"action": "hotkey", "keys": ["ctrl", "s"], "hold_ms": 150},
    {"action": "keypress", "keys": ["enter"]}])
```

Deterministic steps only. Read `follow_up_results[i].result` per item AND
`follow_ups_stopped_reason` (None = the batch ran to completion); an item that
dispatched but did not verify rides its honest verdict while the rest still runs.

## Long or multi-line text (code, VBA, scripts)

```text
computer_execute(session_id, action="type", text=<300 lines of VBA>, via="clipboard")
```

Editors that auto-indent or auto-complete (VBA editor, code editors) mangle
per-character bursts: the paced budget covers ~1800 chars at the default pace
(less at a smaller client timeout) and `paced=partial` in the reply means the
tail went in UNPACED — re-observe and never retype blind. One `via="clipboard"`
type pastes the whole text in a single step instead.
