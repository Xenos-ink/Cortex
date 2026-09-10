# Driver Protocol — harsh benchmark tasks (`benchmarks/tasks_hard/`)

Primary scoring instrument for the harsh tasks is **host-driven mode**: a ZCode LLM
agent (the "driver") completes each task through the real `mcp__cortex__*` MCP tools,
exactly like a real user. There is no vision API key on this machine, so the runner's
scripted provider cannot be the scoring instrument; the scripted harness
(`benchmarks/runner.py`) remains a plumbing validator only (`requires_env` for these
tasks — see `evidence/perf-004/p3/harness-validation.json`).

## Roles

- **Launcher** (measurement runner, not the driver): seeds fixtures, starts/stops the
  wall clock, invokes the scorer, owns evidence files.
- **Driver** (LLM agent under test): performs the task through `mcp__cortex__*` tools
  on the real desktop.
- **Scorer** (`benchmarks/score_task.py`): independent process; evaluates the YAML
  `predicates` against REAL machine/file/window state. Driver self-report is never
  consulted.

## Transport: spawn a fresh server per run (`benchmarks/mcp_client.py`)

Drivers do NOT attach to the session's long-lived MCP server. Each measurement run
spawns a **freshly-created cortex MCP server subprocess** against a GIVEN repo root
(baseline runs target a pristine git worktree; post-fix runs target the edited tree)
via `benchmarks/mcp_client.py` — a stdlib-only MCP stdio client (newline-delimited
JSON-RPC, initialize handshake + tools/list included). The client picks the target
tree's own `.venv` interpreter and forces `PYTHONPATH=<repo_root>/src`, so the spawned
server runs exactly the code in that tree.

Python API:

```python
from benchmarks.mcp_client import CortexClient
with CortexClient(r"C:\worktrees\baseline") as client:   # spawns + initialize + tools/list
    client.tool_names()                                  # capability detection
    client.has_capability("follow_ups")                  # True only on the post-fix server
    session = client.call("start_session", {...})
    client.call("computer_execute", {...})
    client.call("stop_session", {...})                   # ALWAYS stop the session...
# ...client.close() (context-manager exit) terminates the server subprocess
```

CLI (copy-paste example session):

```
# capability probe (no session needed):
python benchmarks/mcp_client.py --repo-root C:\worktrees\baseline list-tools --out tools.json

python benchmarks/mcp_client.py --repo-root C:\worktrees\baseline call start_session ^
  '{"dry_run": false, "require_approval": false, "max_steps": 400, "allowed_processes": ["EXCEL.EXE", "explorer.exe"]}'

python benchmarks/mcp_client.py --repo-root C:\worktrees\baseline call computer_execute ^
  '{"session_id": "<id>", "action": "type", "text": "hello"}'

python benchmarks/mcp_client.py --repo-root C:\worktrees\baseline call computer_observe '{"session_id": "<id>"}'

python benchmarks/mcp_client.py --repo-root C:\worktrees\baseline call stop_session '{"session_id": "<id>"}'
```

For interactive LLM drivers that cannot hold the stdio pipe themselves, spawn
`python benchmarks/driver_bridge.py <repo_root> <workdir> [--actions-log FILE]` once per
run: the driver drops `cmds/NNNN.json` tool-call files and reads `res/NNNN.json`
(screenshots decoded to PNG paths). With `--actions-log`, it also appends one line per
executed `computer_execute` — the polling input for `interrupt.py --at-action`.
One bridge = one run; `__exit__` ends it.

Exit codes: 0 ok; 2 usage/args error; 3 server failed to start or initialize; 4 tool
call failed (JSON-RPC error or isError). Server stderr goes to `--stderr-log` (default:
a temp file); it never blocks the transport.

### Batching (`follow_ups`) — post-fix servers only

A driver MUST probe capability before its first action, via the tools/list result
already returned at connect time (`client.has_capability("follow_ups")`, or search the
`tools.json` payload from `list-tools` for the string `follow_ups`):

- If `follow_ups` is exposed by `computer_execute` (post-fix server): consecutive
  independent actions SHOULD be sent as one `computer_execute` call carrying a
  `follow_ups` list of actions instead of N separate calls. Dependent actions (an
  action whose coordinates/behavior depend on the previous action's outcome) still go
  one-at-a-time, re-observing in between. `computer_observe` after a batch covers the
  batch's net effect for verification.
- If absent (pre-fix server): fall back to exactly one action per `computer_execute`
  call. Never invent batch parameters — the runtime will reject unknown arguments.

### Runtime cautions (observed on the pre-fix server)

- Tool-level success is NOT action success: a `computer_execute` result envelope can be
  ok while the embedded JSON says `"ok": false` (e.g. "Grounding rejected."). Drivers
  MUST parse the returned JSON's `ok` field, not just the transport result.
- With `allowed_processes` configured, a `focus_window` action may fail closed ("target
  window could not be resolved ... failing closed") even for an allowlisted app whose
  window the server cannot resolve to the allowlist. Put the target app in the
  foreground before spawning the client (launcher-side) or focus by clicking through
  real actions instead.
- One client = one run: spawn, run the whole task, `stop_session`, close. Never reuse a
  client across tasks (session registry limits) and never leave one open — `close()`
  guarantees no orphan server process.

## Interference Guard events (T8 servers — parse before you act)

A T8 server binds the session to a target window identity and verifies it around every
input dispatch. Rejections carry SINGLE-LINE event payloads in `reasons`; post-action
events ride `interference_events` + the verification note; queued batches stop with a
NAMED `follow_ups_stopped_reason` (`focus_taken_by`, `modal_dialog`, `focus_drifted`,
`stuck_modifier`, `target_gone`, `focus_identity_unknown`). Rules:

- **On `FOCUS_TAKEN_BY title=... process=...`**: re-ground with `computer_observe`; if
  the named window is NOT the task's, do NOT act on it and do NOT launch a replacement
  app — reattach by identity (`focus_window` to the task window's title, or
  `ensure_app`) and re-verify `active_window` before resuming. If reattach fails, STOP
  and report; the user may be using the machine. NEVER act on the foreign window,
  NEVER relaunch on a whim.
- **On `TARGET_GONE title=...`**: the bound window was CLOSED (or the session target
  died). The binding was cleared for you (default policy). Re-ground, then reattach by
  identity via `ensure_app` (preferred: it attaches to an EXISTING instance and NEVER
  launches) or `focus_window`; only on `NO_INSTANCE` may you launch through the normal
  Run-dialog flow. Run-dialog (Win+R) interactions are transient — re-verify the
  active window per dialog; do not assume the earlier binding survived.
- **On `MODAL_DIALOG title=... controls=[...]`**: read the payload control list; decide
  deliberately — the task's own dialog: handle it from the listed controls; unexpected:
  halt and report. NEVER blind-OK a dialog; NEVER continue a typed path into a window
  that is not the intended one.
- **On `FOCUS_DRIFTED expected=... actual=...`**: re-observe, re-click the intended
  control, then re-type. NEVER retype blindly (text lands wherever focus is).
- **On `STUCK_MODIFIER keys=[...]`**: a modifier is physically held down. Re-send the
  chord only after the state is clear (single-action retry); if it persists, stop and
  report (the machine may be in use).
- **Instance doctrine**: before launching ANY application, call `ensure_app`
  (`target="process"` or `"process|doc-token"`, e.g. `"excel|book1"`); treat
  `REATTACHED` as success; only launch on `NO_INSTANCE`; on `AMBIGUOUS_INSTANCE`
  inspect the listed candidates (unsaved-work risk) and decide deliberately.
- **Verification hygiene**: declare `expected_effect` as concrete observable text or a
  `visual change` INTENT — the literal generic string produced false-negatives on real
  type actions (anomaly B2). On this box the OCR text-predicate is disabled by default;
  typed content verifies via window title / UI-control text, else via the pixel-diff
  tier — so type actions verify deterministically WITHOUT OCR.
- **Settle after activation (B5/B8/B9)**: after ANY focus transition (win+r, launch,
  `focus_window`, `ensure_app` REATTACHED), `computer_observe` once (>= 1 s) before the
  first `type` — keys sent during the activation race can be dropped, misdelivered, or
  land with the selection cleared (a foreign focus click kills an existing select-all).
  After a reattach, re-issue the selection (e.g. `ctrl+a`) BEFORE typing over it; never
  assume a selection survived a focus change.

## Launcher flow (per task, per run)

```
1. python benchmarks/score_task.py --seed --task benchmarks/tasks_hard/<task>.yaml
   # deterministic re-seed of the task's INPUT fixtures (outputs are never touched)
2. record start_utc (UTC ISO-8601) immediately before handing the task to the driver
3. driver runs the task (below)
4. record end_utc when the driver reports completion (or budget is exhausted)
5. python benchmarks/score_task.py --task <yaml> --run-record run.json --out scoring.json
6. only after scoring: close leftover app windows / clean the bench area
```

`run.json` (launcher-written, next to the evidence dir):

```json
{
  "task": "h1-excel-employee-sheet",
  "start_utc": "2026-09-06T14:03:11.500000+00:00",
  "end_utc": "2026-09-06T14:07:42.100000+00:00",
  "evidence_dir": "evidence/perf-004/p3/runs/h1-run-01",
  "actions": 87,          // executed_actions from the driver session metrics
  "model_calls": 93,      // session model_calls counter
  "retries": 2,           // audited recovery/retry events
  "notes": "optional free text"
}
```

## Driver rules

1. The driver receives **only** the `prompt` field of the task YAML (verbatim). It is
   NOT given: the `predicates`, `anti_gaming`, `human_reference` or `verification`
   blocks, the fixtures directory, or any scorer source.
2. The driver works through the real tool surface exactly like a human user, over the
   per-run transport above (`benchmarks/mcp_client.py` spawning a fresh server from the
   launcher's chosen repo root): `start_session` (with `dry_run=false`,
   `require_approval=false` and the task's `allowed_processes` as the process allowlist)
   followed by `computer_observe` / `computer_execute` actions (click / type / keypress /
   drag / scroll / focus_window / wait). (`run_goal` was removed with the internal
   loop; there is no loop tool — drive every action as a direct `computer_execute`.)
   One session per task run; one client per session.
3. Wall time is measured **externally** by the launcher (step 2/4 above) — never taken
   from driver claims or session self-timing.
4. **Do not open, read or modify anything under `benchmarks/tasks_hard/fixtures/` or
   `evidence/`**; input data is provided at the Desktop paths stated in the prompt.
5. Leave the task's application windows OPEN when finished — at least one predicate
   class inspects live window state (e.g. the saved Notepad title, the mspaint canvas).
   The launcher closes everything after scoring (step 6).
6. Do not touch the output paths with anything except the required application (no
   scripting the file into existence from a shell — that is a task integrity violation,
   and the run record must note it if observed).

## Scorer contract

`score_task.py` is stdlib-only plus PIL (already in the venv; used only for the mspaint
pixel analysis). It reads the task YAML's `predicates` list and evaluates each kind
against the live machine:

- `file_exists`, `file_fresh` — output present / mtime >= start_utc.
- `xlsx_contains_strings`, `xlsx_min_string_hits` — strings across
  `xl/sharedStrings.xml` + inline strings (xlsx read as stdlib zip + XML).
- `xlsx_formula` — regex against `<f>` elements in `xl/worksheets/sheet1.xml`.
- `xlsx_cell_value` — cached `<v>` at a cell ref; `expected` literal or recomputed from
  the canonical fixture (`expected_from_fixture`).
- `xlsx_bold_cells` — cell style index -> `cellXfs` -> font `<b/>` in `xl/styles.xml`.
- `xlsx_rows_matching` — count of column strings matching a regex (bulk-entry row floor).
- `xlsx_spot_checks` — exact cell values against the canonical fixture grid.
- `xlsx_computed_totals` — totals recomputed by the scorer from the canonical fixture
  and matched in the required row order.
- `text_contains_normalized` — punctuation/space-insensitive containment.
- `window_title_contains` — real EnumWindows/GetWindowText scan (ctypes, no new deps).
- `process_window_count`, `window_state_unchanged`, `focus_steal_recovered` — T8
  realism predicates (live-window identity/state/foreground + launcher interrupt-event
  proof); see "New scorer predicates" below.
- `paint_rect_analysis` — scorer takes its own screenshot of the live mspaint window
  (GDI via ctypes), locates the white canvas, computes the drawn shape's bounding box
  and fill fraction (PIL), compares to the target within tolerance.

Output `scoring.json`: `{task, wall_time_s, human_reference_s, ratio, completed,
precision, actions, model_calls, retries, notes, predicates:[{name, outcome, evidence}]}`.
`completed` = every predicate passed; `precision` = passed/total; `ratio` =
wall_time_s / human_reference_s.

## Reseeding doctrine

Inputs are re-seeded **deterministically before every run** (`--seed`); scoring itself
is read-only. t6_inbox is wiped and recreated flat at seed time. Output paths
(`*_out_*`) are never seeded — a pre-existing output would be rejected by
`file_fresh` (mtime must post-date start_utc).

## Human references

`benchmarks/make_references.py` replays a fixed competent-human action script per task
through the real backend (`computer_use_mcp.backend.LocalComputerBackend`) at calibrated
cadence (Fitts ~0.6 s/click, ~220 ms/char, ~1.0 s dialog waits; provisional numbers,
reconciled with `evidence/perf-004/p1/cadence.json` when it lands) and times itself ->
`evidence/perf-004/p3/human-references.json`. References are **calibrated references,
not real human runs**; they are superseded when a real human performs each task. Tasks
whose UI replay is too brittle to be deterministic carry an arithmetic reference
(action list x cadence) and say so in `method_log`.

## Realism tasks (`r1-*`, `r2-*`) and interruption injection

The realism wave adds two human-style tasks next to the h-series. They load through the
same `load_tasks`/scorer path (`seed_for_task` derives their `r` fixture prefix; seeding
`r1` also wipes+recreates the empty input container `Desktop\cortex-bench\r1_target`).

- **`r1-dialog-navigation`** — Notepad append + Save As routed through the GUI common
  dialog: the driver must navigate INTO `Desktop\cortex-bench\r1_target` and create the
  nested folder `r1_archive` with the dialog's OWN controls (file-list selection + Enter,
  the New folder button, breadcrumb/tree/address-bar dropdown, Browse). Typing a full
  path into the dialog's address bar or filename field is tolerated only as ONE step of
  the flow; the FOLDER itself must be reached/created via dialog UI. The scorer records
  nothing about how — it checks the file landed at
  `r1_target\r1_archive\r1_out_note.txt` with carried-over content, and (new predicates)
  exactly one Notepad window with the saved doc plus the window title.
- **`r2-interruption-recovery`** — Notepad in-place append+save while the HARNESS injects
  a disturbance mid-task. The task YAML carries an `interrupt` block:
  `{"at_action": N, "type": "foreign_window"|"modal", "spec": {...}, "note": ...}`. The
  event/policy names in that note (FOCUS_TAKEN_BY / MODAL_DIALOG / FOCUS_DRIFTED) are
  INFORMATIONAL for the driver. The launcher injects via `benchmarks/interrupt.py`
  (below), which writes `interrupt-log.json` (proof the interruption fired, with
  timestamp) and `interrupt-window-snapshot.json` (injection-time window state) into the
  run's evidence dir; the run record passes both to the scorer via the
  `interrupt_log` and `interrupt_snapshot` keys, consumed by the
  `focus_steal_recovered` and `window_state_unchanged` predicates.

### `benchmarks/interrupt.py` (launcher-owned; never the driver)

```
# fire once the bridge's actions log shows >= N executed driver actions (BETWEEN actions):
python benchmarks/interrupt.py inject --task-yaml benchmarks/tasks_hard/r2-interruption-recovery.yaml \
    --evidence-dir evidence/perf-004/p3/runs/r2-run1 \
    --at-action 3 --actions-log evidence/perf-004/p3/runs/r2-run1/actions.jsonl
# time-based fallback: --after-seconds S; one-shot type override: --type modal
python benchmarks/interrupt.py cleanup --log <evidence-dir>/interrupt-log.json   # kill disturbance pids
```

`--at-action` counts EXECUTED driver actions in the actions JSONL written by
`benchmarks/driver_bridge.py --actions-log` (one line per executed `computer_execute`).
`foreign_window` launches a throwaway Notepad document (`%TEMP%\cortex-interrupt\...`)
and puts it in the foreground; `modal` dirties a throwaway document and closes its
window so the native "Do you want to save changes?" `#32770` modal appears. Injection
outputs: `interrupt-log.json` (`fired`, `type`, `timestamp_utc`, `at_action_observed`,
`focus_ok`, disturbance identity, pids) and `interrupt-window-snapshot.json` (title,
class, pid, RECT in physical pixels, edit-text sha256) — the scorer compares the LIVE
window against this snapshot, so the window must be photographed in its resting state
(the injector waits for rect stabilization).

### New scorer predicates (T8 realism; stdlib/ctypes only)

- `process_window_count` — `{process: "notepad.exe", title_token: "...", expected: 1,
  class?: "..."}`: exactly `expected` visible top-level windows of that process (exe
  basename, case-insensitive) whose title contains `title_token`. Proves "one
  task-relevant window, no stray doc/sheet"; a disturbance window with a different
  document does not match the token.
- `window_state_unchanged` — `{snapshot: <path>} | {snapshot_from_run_record:
  "interrupt_snapshot", ...}, allow_closed: true, tolerance_px: 4`: the snapshot window
  must be gone (when `allow_closed`) or untouched — same class, title token present,
  live rect within tolerance of the snapshot rect, and Edit-text sha256 unchanged (no
  keystroke landed in it). Both sides read physical pixels (`set_dpi_awareness`).
- `focus_steal_recovered` — `{process, title_token, interrupt_log?: <path> |
  interrupt_log_from_run_record: "interrupt_log", require_event: true}`: the launcher's
  interrupt event shows `fired: true` AND the task's window is the OS foreground at
  scoring time. Artifact correctness is enforced by the remaining predicates
  (`completed` requires all).

Negative coverage: `evidence/perf-004/p3/runs/scorer-negative-r.json` (13 checks — wrong
window count, moved/closed/missing-snapshot states, not-foreground, missing/false
interrupt event — every predicate must FAIL on the wrong state and PASS on the fixed one).

## T8 event-handling rules for drivers (interference immunity)

On post-fix servers the runtime may emit structured interference events in `reasons` /
verification notes / `text_summary` (design: `evidence/perf-004/t8/interference-immunity-design.md`).
Drivers MUST handle them deliberately:

- **On `FOCUS_TAKEN_BY title=... process=... hwnd=... class=...`** — the OS-focused
  window is not the session target and the action was REJECTED (not executed). Re-ground
  with `computer_observe`; if the named window is NOT the task's, do NOT act on it and do
  NOT launch a replacement instance — reattach by identity: `focus_window` to the task
  window's exact title (this re-binds the session target). Verify via a fresh observe,
  then resume. If reattach fails (WindowFocusError / identity unavailable), RETRY once
  or twice; if it still fails, stop and report — the launcher may apply the documented
  escalation (put the task window in the foreground launcher-side; h1-r5 precedent).
  NEVER type into, click, move or close the foreign window (r2's
  `window_state_unchanged` predicate would fail, and the user's work could be lost).
- **On `MODAL_DIALOG title=... class='#32770' ...`** — read the payload's control list
  and decide: a dialog the task itself raised (Save As, Confirm Save As, an app error
  box the driver must dismiss) is handled deliberately; anything unexpected is halted
  and reported, never blind-OK'd. Never continue a queued batch across a modal.
- **On `FOCUS_DRIFTED expected=... actual=...`** — keyboard focus is not where the
  driver thinks. Re-observe, re-click the intended control (focus_window to the task
  window first if the whole window lost foreground), then re-type. Never re-type blindly.
- **Instance doctrine** — before launching any application, check `active_window` /
  existing windows; treat a `REATTACHED` result as success and only launch on
  `NO_INSTANCE`; on `AMBIGUOUS_INSTANCE` inspect candidates (unsaved-work risk). Never
  launch a second copy of the task's app+document.
- **Verification hygiene** — tool-level ok does NOT prove effect: the embedded JSON's
  `ok`/`verification` fields decide, and screenshot-diff verification false-negatives
  are common on this box (B2) — verify typed content and dialog state with a follow-up
  `computer_observe` (window title, on-screen text) instead of trusting one result.

### Rehearsal findings (2026-09-07, pre-T8-guards build + live guard build; see
`evidence/perf-004/p3/runs/r1-run0-type-stall/ABORT-NOTE.json` and the four run notes)

1. With a process allowlist configured and a NON-allowlisted window foreground
   (e.g. the ZCode console), ALL driver input is rejected fail-closed. Launchers:
   put an allowlisted surface (desktop) in the foreground before the run starts.
2. Typing text containing BACKSLASHES stalled the pre-T8 server >10 min (blocked, 0
   CPU). Use forward-slash paths when typing into the Run dialog, or re-run after the
   T8 engineer's fixes; the run record must note any stall.
3. The Run dialog pre-fills the last command SELECTED — typing replaces it; clicking
   inside the field first collapses the selection and typing APPENDS (r1-run1's
   "notepadnotepad" error modal). Handle app/error modals deliberately (OK) and redo
   the step.
4. Guard-build gaps to coordinate with the T8 engineer (observed live):
   - the transient-launcher exception (design section 4(i)) is not implemented: typing
     into the explorer Run dialog requires a `focus_window` re-bind per dialog, and the
     re-bind dies with the dialog (a closed bound window deadlocks the session until a
     focusable target reappears);
   - `focus_window` on the ALREADY-foreground window fails ("foreground did not
     change") — drivers cannot re-bind after a crash-replace; launcher minimize-once is
     the workaround;
   - the first keyboard input after a restore/re-bind can drop or misplace keys
     (caret race: footer landed at caret 0 with its first char lost) — drivers must
     verify their edit (observe/on-disk) after re-bind, not assume.
   - selection state can be lost across a kill-focus (select-all before typing is not
     interruption-safe; rebuild the whole document if in doubt).
