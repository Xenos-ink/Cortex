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
- `integrity=unverified` — no read-back was possible (no accessibility tree); NOT a
  veto — corroborate visually with a stated `expected_effect`.
- `integrity=mismatch` — the focused control's read-back disagrees with the typed
  text: a matching window title can no longer stand as proof, so the verdict is
  demoted to `uncertain` — never silently `verified`, and never a confirmed failure
  either.

`integrity=mismatch` means the drop is NOT proven good: the verdict is demoted to
`uncertain`, and a masked or transformed field (password dots, formatted numbers) may
legitimately hold something that differs from the typed text. A `TextIntegrityError`
is different: the read-back itself raised, so the reply is `failed` with `error`
`action_error` — an operation error, not an integrity verdict. Either way: re-observe
the field and decide from evidence; never retype blind on the mismatch alone, and
never conclude success from the title alone. `partial` and `unverified` are annotated
absences of read-back, not vetoes.
