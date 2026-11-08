# Glossary (reference)

Read this when a reply names an interference label, a `failure_reason` you do not
recognize, or a type-integrity status.

## Interference labels

The guard raises two DIFFERENT classes of event, and they must never be read the same
way:

**Pre-dispatch refusals** — `focus_taken_by`, `focus_identity_unknown`, `stuck_modifier`,
`target_gone` fire BEFORE any input is dispatched. The reply is a named label, not a
verdict about an effect: there is no effect, because nothing was sent.

| Label | Event token | Fires when | What to do |
|---|---|---|---|
| `focus_taken_by` | `FOCUS_TAKEN_BY` | a foreign window really holds the foreground and your target is still alive | re-observe, reattach by identity (`ensure_app` / `focus_window`); never act on the foreign window |
| `focus_identity_unknown` | `FOCUS_IDENTITY_UNKNOWN` | the OS foreground identity could not be read at all — fail-closed, NOT a foreign window | re-observe once; if it persists the probe is broken — report it, never retry blind |
| `stuck_modifier` | `STUCK_MODIFIER` | a modifier the chord carries is physically held down (a held ctrl rewrites ctrl+s into s) | clear the held key, or ask the host for `hotkey_guard.on_stuck_modifier="release"`; never re-send the chord blind |
| `target_gone` | `TARGET_GONE` | the bound window no longer exists; the default policy clears the binding | re-observe, reattach by identity; never relaunch blindly |

**Post-action / mid-type events** — `FOCUS_DRIFTED` and `MODAL_DIALOG` mean input WAS
dispatched. A `FOCUS_DRIFTED` after a keyboard action demotes a `verified` verdict to
`failed` (`interference_post_action` — the input landed but possibly not in your
window); a `MODAL_DIALOG` may stop a batch after executed items
and rides the step/batch replies as an event; a mid-type focus drift is a REJECTION
carrying `partial_delivery` with `delivered_chars`. For all three the first attempt
may have landed: corroborate before ever re-typing.

`focus_taken_by` and `target_gone` are NOT the same: a steal means the window is alive
(reattaching brings the task back); `target_gone` means the window is closed
(reattaching must find or launch a NEW instance by identity). A steal is a KNOWN wrong
window; an unknown identity is NO window information.

**A pre-dispatch label is not a verdict.** `verified` / `uncertain` / `failed` describe
the effect of something that dispatched; a pre-dispatch label describes a gate that
stopped it, so none of the three applies — not `failed` (nothing dispatched), not
`uncertain` (nothing to corroborate), not `verified` (not done). Restore the named
precondition, then send the action. Labels and verdicts contrast most sharply on an
unchanged screen: after a `focus_taken_by` type the empty field proves the guard worked
(reattach and resend); after an `uncertain` type the same empty field means the text
probably DID land (corroborate with one read before ever re-typing).

## The `failure_reason` vocabulary (plan steps and terminal replies)

`failure_reason` is always present on a plan step or terminal reply, `None` when the step verified, and draws on ONE closed vocabulary: `verification_failed`, `verification_uncertain`, `execution_error`, `limit_exceeded`, `cancelled`, `safety_denied`, `approval_required`, `digest_surprise`, `rejected`, `invalid_action`, `invalid_follow_up`, `policy_stop`, `modal_dialog`, `focus_drifted`, `no_post_action_observation`, `unclassified`. The CLASS mapping is conditional: when a LATER item of a step is refused, `focus_taken_by` and `focus_identity_unknown` report the class `focus_drifted` while `stuck_modifier` and `target_gone` report `rejected` — but when the step's FIRST item is refused, the kind table wins and ALL of them report `rejected`. The exact
cause stays verbatim in `follow_ups_stopped_reason` and the entry's `reasons`.

## Type-integrity statuses (type actions)

`integrity=` is a message suffix on type replies:

- `integrity=verified` — the focused control's read-back matched the text.
- `integrity=partial` — read-back only partially confirmed; NOT a veto.
- `integrity=unverified` — the verification ladder could not decide. Since WP12b
  the reply names WHICH ladder branch gave up as a `branch=` suffix inside the
  message (closed vocabulary, derived from the code by a truth test):
  `blind_read` (no readable focused target), `read_timeout` (the UIA semantic
  read hit its wall or the reader's single COM owner was still busy with a hung
  provider — WP13 DELTA 2; the read never happened, so nothing is verified and
  nothing is repaired; a degraded BASELINE read demotes the whole action the
  same way, and no `TYPE_UNCONFIRMED` marker fires — the target exists, the
  provider was slow), `verify_over_budget` (the whole type action must fit its
  deadline: a verification read or horizon sleep the remaining deadline can no
  longer cover stops verifying — never verified, never repaired; WP13 DELTA 3),
  `unstable_confirm`,
  `agreed_empty`, `not_landed` (the buffer still equals the pre-dispatch
  baseline), `no_baseline` (the baseline read was blind — never repaired),
  `read_truncated` (the Win32 WM_GETTEXT read sat at its 8192-character
  truncation cap, so the value's tail proves nothing — UIA reads are not
  truncated and keep full verification; never repaired),
  `transformed_buffer` (a masked or transformed field — password dots — shares
  nothing with the typed text: it must NOT report a failure),
  `landed_not_at_tail` (the text is in the buffer but not at its tail — caret
  mid-text, autocomplete), `horizon_blind`, `horizon_unconfirmed`,
  `fast_budget_exhausted`, `repair_read_blind`, `repair_unconfirmed`,
  `repair_over_budget` (the repair did not fit the per-action deadline and was
  never typed), `tail_unproven` (the buffer ends with the typed text but the
  known baseline proves no insertion — e.g. the text was already there and the
  dispatch dropped entirely). NOT a veto — corroborate visually with a stated
  `expected_effect`.
- `integrity=mismatch` — POSITIVE evidence the typed text landed and then
  diverged: the read-back starts with the pre-dispatch baseline plus a non-empty
  correct leading part of the typed text and then differs (e.g. `tett` for
  `test`, `brown xxx` for `brown fox`). A matching window title can no longer
  stand as proof, so the verdict is demoted to `uncertain` — never silently
  `verified`, and never a confirmed failure either.

`integrity=mismatch` means the drop is NOT proven good: the verdict is demoted to
`uncertain`. WP12d notes: typing that REPLACES a selection (address bar after
Ctrl+L, rename fields) now reports `unverified` branch=`tail_unproven` — the
baseline cannot prove an insertion, which is honest and NOT a veto. A
`TextIntegrityError` is different from both: it fires only AFTER the one verified
retype still did not produce the typed text at the control's tail — the reply is
`failed` with `error` `action_error` — an operation error, not an integrity
verdict, and the duplicate-typing hazard is why the repair is baseline-gated.
Either way: re-observe
the field and decide from evidence; never retype blind on the mismatch alone, and
never conclude success from the title alone. `partial` is an annotated partial
confirmation, and `unverified` is an annotated non-decision of the verification
ladder (a blind read, a transformed buffer, a stall or an over-budget repair —
see the `branch=` list above) — neither is a veto.

`follow_ups_stopped_reason: "time_budget"` is the follow-up chain's wall clock:
the call's per-call budget ran out before an item could run (each queued item
runs on the REMAINING budget — a wait/hold that would overrun it, or a type item
that could no longer run paced, is never dispatched). Executed items keep their
entries; the driver RESENDS the remaining items in a new call.

## Recording reply tokens

The recording family answers with its own small closed vocabulary. A peek carries
`status` `recording` (capture running) or `encoding` (the video encode still
running). A missing video names one omission reason — `not_requested`,
`too_large`, `encoder_unavailable`, `encode_failed`, `over_budget` (retried on a
later collection: `over_budget` itself, `write_failed`, or an `encode_failed`
carrying `aborted: true` — a stop/cancel aborted the encode and the frames are
still held), `frames_released`
(the held frames were evicted under the recording pool's byte cap, so video can no
longer be produced for this recording), `write_failed` (the default file
delivery could not write the MP4 — retried on a later collect while the video is
retained). `unknown_record` answers a wrong or
foreign `record_id`, `record_expired` a recording that fell out of the retention
ring, and `recording_limit` the 1-per-session / 2-per-process caps. The
`pointer` / `last_click` fields ride recording keyframe fetches exactly as the
reply contract describes.
