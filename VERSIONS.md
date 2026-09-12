# VERSIONS

A per-release ledger for Cortex (`computer-use-mcp`). Newest first. Reconstructed
from the repository's git history (`git log --oneline`) and the
`docs/ARCHITECTURE.md` header. Internal engineering-process designations are omitted
on purpose — releases are described by content only. The version in `pyproject.toml`
is the authoritative released version.

**Convention for future entries:** while work is in flight, land its notes under the
topmost `## Unreleased` heading; at release time they are folded into a new
`## vX.Y.Z (date, mission)` entry with **Added / Changed / Fixed / Performance**
sections and a **Compatibility notes** line, and `## Unreleased` is emptied again.
Planned work per upcoming version: see **[ROADMAP.md](ROADMAP.md)**.

## v0.5.8 (2026-09-12) — RELEASED (global `cortex-mcp` CLI: install/update subcommands with no-clobber agent registration)

A single terminal command now performs the whole install/update lifecycle that
previously required hand-editing each agent's MCP configuration file.

### Added

- **Global `cortex-mcp` terminal command** — a `[project.scripts]` console entry
  (also runnable as `python -m computer_use_mcp.cli`; stdlib-only CLI) with two
  subcommands:
  - **`install`** — provisions `<repo>/.venv` and the editable install, then registers
    the Cortex MCP server into every recognized agent config file found on the machine
    (zcode, claude, cursor, codex, kimi). Every file is backed up before the first write
    (`<file>.cortex-backup-<timestamp>`; an existing backup is never overwritten). An
    existing cortex entry is never clobbered: entries that already match the target are
    left untouched (UNCHANGED); entries that differ are reported and skipped
    (SKIPPED-EXISTS-USE-FORCE) unless `--force` is given. `--dry-run` prints the full
    plan with zero writes; `--agents a,b` (or `all`) filters the targets. The served
    surface is verified with a zero-input stdio probe that expects exactly the five
    tools and no `anyOf`/`$ref` schema tokens.
  - **`update`** — `git fetch` plus a `--ff-only` merge of `origin/main` (clean abort
    when the branch is not fast-forwardable — the command never resets), then an
    editable refresh of both installs, package-version verification before/after, and
    the same stdio probe.

### Compatibility notes

- Existing cortex registrations are never modified without `--force` — manual setups
  are preserved (files whose cortex entry differs are not rewritten at all).
- When a write does happen, the config file is rewritten with standard JSON
  indentation and the original bytes are backed up first; every unrelated key is
  preserved.
- The global console-script refresh targets the system interpreter and is best-effort:
  without an elevated terminal it prints a warning (the fallback
  `python -m computer_use_mcp.cli` always works) and never changes the exit code.

## v0.5.7 (2026-09-11) — RELEASED (end-to-end speed: window-identity queue batching, half-res action JPEGs, honest alias launches and verdicts)

End-to-end latency analysis showed the driving model's turns dominate wall
time; this release removes the server-side multipliers: per-action image bytes
that grew the driver's context, batches halted by ordinary pixel change, and
turns burned decoding false failures.

### Changed

- **`follow_ups` batches continue while the attached window identity is unchanged
** —
  ordinary pixel changes from earlier queue items (drawing, typing,
  dialogs) no longer stop a batch; each item re-grounds from the fresh post-action
  capture. The queue still stops on TRUE staleness — the attached window's identity
  (hwnd/title/bounds) changed or the window closed since the queued premise — plus
  safety rejection, approval requirement, validator/grounding rejection, dispatch
  error, or a named interference event. `CORTEX_QUEUE_STRICT_DIGEST=1` restores the
  v0.5.6 whole-screen premise checks.
- **Executed-response images default to half-resolution JPEG** — the DEFAULT
  `computer_execute` response image is a 0.5x JPEG (q60, ~60KB typical at 1080p) so a
  driving model's context stops growing by a 150-230KB PNG per action. An explicit
  `include_screenshot_after=true` ships full resolution, and
  `CORTEX_ACTION_IMAGE_FULL=1` restores full-res defaults. `computer_observe` /
  `computer_screenshot` and text-mode (`image_delivery="text"`) semantics are
  unchanged.
- **Docs re-aligned** — README / ARCHITECTURE / SAFETY now teach the queue-continuation
  and half-res-image defaults (the pre-0.5.7 text described the old whole-screen
  digest stop and ~1-2 MB PNG default); an `ensure_app` docstring said `os.startfile`
  while the code uses `Popen` — fixed.

### Fixed

- **`ensure_app` bare-name resolution and honest `launched=`** — bare app
  names now resolve to `.exe` / Windows Store execution aliases before spawning, and
  the `NO_INSTANCE` payload only reports `launched=` when the process actually
  started (a failed spawn is reported as a failure, never as a launch).
- **Honest `uncertain` for evidence-free stated effects** — a
  stated-effect action with no deterministic verification signal and zero/sub-threshold
  pixel evidence now returns `uncertain` (0.4) instead of a definitive false `failed`
  ("Expected change was not observed"); the input dispatched and the driver can
  re-observe instead of retrying blindly.

### Performance

- Context bytes per action drop from a ~150-230KB PNG to a ~60KB half-res JPEG by
  default; window-identity queue batching lets one model turn cover up to 5 actions
  without false stops — directly attacking the ~97%-of-wall-time model-turn cost.

### Compatibility notes

- **Default behavior changes:** executed-response images are half-res JPEG unless
  `include_screenshot_after=true` or `CORTEX_ACTION_IMAGE_FULL=1`; queued batches
  continue through ordinary pixel change and stop on true window-identity staleness
  unless `CORTEX_QUEUE_STRICT_DIGEST=1` restores whole-screen premise checks.
  Safety semantics unchanged: zero-bypass per-item pipeline, safety rejections and
  named interference events still stop the batch, stop token still checked between
  items.

## v0.5.5 (2026-09-09) — RELEASED (three live-usage defect classes + live-host hardening)

Mission ORVEX-CORTEX-055: root-caused from FailedLog.txt (967-line live session),
fixed, red-teamed, and validated live on Kimi Code + GLM-5V driving MS Paint.

### Fixed

- **Computer-use slowness drivers** — false "Expected change was not observed" verdicts
  for focus-type clicks (a new deterministic `FocusChangeStrategy` verifies via UIA
  focused-element / active-window identity / digest corroboration before the pixel tier);
  `follow_ups` queues no longer abort on UNCERTAIN verdicts (stop only on definitive
  failure), so batches actually batch; provider-judge image re-encoding eliminated
  (base64 reused verbatim).
- **Failed full takeover** — `ensure_app` now LAUNCHES allowlisted targets server-side by
  default (`attach_or_launch.launch` default `driver`→`server`; `CORTEX_ATTACH_OR_LAUNCH`
  env knob restores the old never-launch default), resolves Windows Store execution
  aliases (`%LOCALAPPDATA%\Microsoft\WindowsApps\<needle>`) so Store apps like Paint
  actually spawn, and an allowlisted `ensure_app` target is exempt from the
  foreground-process gate that previously made bootstrap impossible from a fresh session;
  the `process_not_allowed` rejection now names the active process and teaches the remedy.
  Launch hardening: typed `LaunchTargetError` charset validation before any spawn,
  `shell=True` removed (list argv via `shutil.which` → alias → raw name).
- **Screenshots truncated / delivered as base64 text** — `computer_execute` now returns
  MCP content blocks in parity with `computer_observe` (slim TextContent + real
  ImageContent); `include_screenshot_after=false` honored on the follow_ups path too;
  follow-up entries are slim (double serialization eliminated); outbound images on BOTH
  observe and execute bounded by `CORTEX_RESULT_IMAGE_MAX_KB` (default 180 KB, JPEG q85 +
  downscale ladder, internal PNG pipeline untouched); observe metadata `ocr_text` /
  `ui_elements` capped at 20 entries with `<field>_omitted_count`.
- **Weak-model tool-call tolerance (live GLM-5V on Kimi Code)** — integral floats, numeric
  strings, and fractional coordinates accepted for x/y/x2/y2 (rounded, clean typed errors
  otherwise); `keys` accepts a bare string or JSON-encoded array; `follow_ups` accepts a
  single dict / JSON string; `allowed_processes`/`allowed_windows` accept comma- or
  space-separated strings; **all Optional parameters advertise plain type-array JSON
  schemas (`{"type":["integer","null"]}`) instead of `anyOf` unions**, which a live vision
  host rejected wholesale; `computer_execute`'s FastMCP return annotation relaxed to `Any`
  (the `dict[str, object]` annotation crashed block-list responses with a Pydantic
  DictModel error through real hosts).

### Performance

- Measured live (Kimi Code + GLM-5V driving Paint, session metrics): observation p50
  120–164 ms, execution p50 11.6–119 ms, verification p50 94–110 ms per action;
  ensure_app + observe + click round-trips 0.14–1.4 s per tool call. Loop mechanics are no
  longer the bottleneck; model inference time dominates end-to-end step latency.

### Compatibility notes

- **Default behavior change:** a session with an allowlist can now server-LAUNCH an
  allowlisted `ensure_app` target (`launch="driver"` or `CORTEX_ATTACH_OR_LAUNCH=driver`
  restores never-launch). Non-allowlisted targets never launch — fail-closed unchanged.
- **Tool output shape:** executed `computer_execute` calls return `[TextContent,
  ImageContent]` blocks (was: dict with `screenshot_after_base64`); error/rejection shapes
  stay plain dicts. The image rides as a real image block under the 180 KB outbound budget.
- **Wire schemas:** Optional parameters advertise type-array forms instead of `anyOf`
  (runtime validation semantics unchanged; `limits` still fail-closed strict).

## v0.5.6 (2026-09-11) — RELEASED (truthful verification & resilient follow_ups batching)

Focus-type clicks with a stated expectation could false-fail when the intended
transition was invisible to the pixel diff; that false verdict flushed `follow_ups`
batches; validation rejections carried a generic stamp. All three classes are fixed
here. This release also ships the v0.5.5 hardening work below (desktop-safe E2E
gating, run_goal removal, 3x mechanical speed).

### Fixed

- **False "failed" verification on focus-type clicks** — a focus-type click
  whose stated `expected_effect` describes a focus/dialog transition is often
  invisible to the pixel diff; a sub-threshold visual diff now degrades to an honest
  `uncertain` instead of a definitive false `failed` (legacy semantics preserved for
  unflagged intents; uncertain is never success, and a real above-threshold change
  still verifies).
- **`follow_ups` batches flushed by a false failure** — a queued batch no longer
  stops on an executed item's verification `failed`: the input dispatched, the next
  item re-grounds from the fresh post-action capture, and per-item truthful verdicts
  (ok=False + evidence) are still returned; `CORTEX_QUEUE_STRICT_VERIFY=1` restores
  the v0.5.5 stop-on-failed behavior.
- **Generic rejection stamps** — validation rejections name the
  actual gate (allowlist / staleness / focus) instead of a generic
  "Grounding rejected." stamp, so weak drivers stop burning turns decoding `reasons`.
- **Desktop-safety gating of the real-input E2E suite** — plain
  `pytest tests` runs used to execute the real-Windows E2E tests whenever
  `CUMCP_RUN_E2E` leaked into the environment (for example when the variable leaks into the
  environment from another tooling process). The gate is now a loud, fail-closed contract pinned by
  tests: e2e-marked tests SKIP BY DEFAULT with a skip reason that states the exact
  opt-in (`CUMCP_RUN_E2E=1 python -m pytest tests/e2e`) and WARNs that opting in
  drives real keyboard/mouse input on the live desktop; only the exact variable name
  AND exact value `1` enable it (`true`/`yes`/`on`/`2` and any
  `E2E_*`/`REAL*`/`DESKTOP*` variable stay closed — proven against leakage).

### Changed

- **Served tool docs re-taught** — the `computer_execute` description, module
  docstrings, and SAFETY/ARCHITECTURE behavior text now teach the new queue semantics
  (uncertain AND executed-failed verdicts continue the batch), the gate-naming
  rejection messages, and the `CORTEX_QUEUE_STRICT_VERIFY` escape hatch.
- **Unique-window isolation in the E2E suite** — e2e app
  tests can no longer attach to anything they did not launch themselves. Notepad
  instances are launched with scratch files whose names embed a run-unique token
  (`cumcp-e2e-<pid>-<n>-<ts>`, so the window title carries it) and attach goes through
  a marker-only helper (`attach_window_by_unique_title`) that treats the user's own
  same-app windows as invisible: zero marker matches fail loudly (TimeoutError, never
  a fallback attach), ambiguous matches raise. Calculator attach is PID-scoped to the
  process the suite launched; the browser page title embeds the same run-unique token.
  Teardown is hardened to close exactly what was opened (exact-hwnd `close_window`
  instead of a class/title re-search; `kill_process_tree` stays pid-scoped). All
  pinned without real input in `tests/test_r8_pins.py` (gate fail-closed
  matrix, marker-only attach with stubbed enumeration, exact-hwnd teardown,
  marker-presence checks).

### Performance

- **Mechanical per-action speed** — the direct-action pipeline now
  runs one full capture instead of three on the steady-state path, and verification
  diffs raw frames instead of re-decoding the PNG it just watched get encoded.
  Per-action profiling showed each direct
  action paid THREE full captures (direct_request + validate + post_action,
  p50 132–174 ms each) plus a two-PNG-decode pixel diff (p50 101 ms); the honest
  live baseline was ~428 ms mechanical per action (excluding the host's own observe
  calls), ~582 ms including them — not the 125 ms figure previously reported.
  Changes, all verification-semantics-preserving:
  - **Identity-probe-guarded validate reuse** — a DIRECT action whose premise was
    captured in the same tool call and is still inside the freshness window
    (`CORTEX_VALIDATE_REUSE_MS`, default 1500; `0` restores full re-capture) runs a
    capture-free identity probe (monitors + foreground window + coordinate space —
    exactly the dimensions the validator's staleness check consumes; ~0.8 ms measured)
    instead of re-capturing the identical screen. NO drift → the premise is reused
    (audited truthfully: `phase=validate, reused=True, duration=0.0`); ANY drift →
    the probe becomes the validate observation and the P0-H `STALE_OBSERVATION`
    rejection + single re-observe recovery fire exactly as before. Queued
    follow-ups NEVER reuse (their premise is a previous item's post-action capture);
    backends without the probe keep the full validate capture.
  - **Raw-frame verification fast path** — the backend stashes the capture-time RGB
    frame on the Observation (private `_frame`, never serialized); the pixel-diff
    tier and the FocusChange digest corroboration use it directly instead of
    base64+PNG-decoding both sides (two ~20–36 ms decodes per verification → 0).
    No stash / one-sided stash / size-mismatched stash falls back to the exact
    legacy decode; verdicts and evidence are byte-identical (pinned).
  - **One-histogram diff math** — the screen-wide mean is derived from the diff's
    own 768-bin histogram instead of a second full-frame `ImageStat` pass; bit-
    identical values (pinned against the historical computation), thresholds
    (`STRONG_PIXEL_DELTA=40`, `STRONG_CHANGE_MIN_PIXELS=50`,
    `DEFAULT_DIFF_THRESHOLD=1.0`) untouched.
  - **Outbound JPEG from the raw frame** — `_bound_outbound_image` accepts the
    capture frame and skips re-decoding the just-encoded PNG; ladder outputs are
    byte-identical (pinned), internal PNG untouched.
  - **Persistent mss capture instance** — one held instance per backend (DIB stays
    allocated) instead of a fresh `mss.MSS()` per grab; any grab failure drops the
    instance fail-safe. `CORTEX_INTERNAL_FRAME_REUSE=0` disables the frame/payload
    stashes (pure legacy path).
  - **Identical-pixel payload dedupe** — a capture whose raw bytes are byte-identical
    to the previous capture reuses the previous PNG payload verbatim (PNG
    determinism: identical pixels → identical bytes; digests/staleness unaffected).
  - **`CORTEX_PNG_COMPRESS_LEVEL`** (0–9, default unset = PIL's own = today's bytes)
    — opt-in encode/size trade for mechanical throughput.
  Measured on a real desktop (real capture, input stubbed): per-action p50
  198.5 ms paced (was ~428 ms live), verification p50 101→33.7 ms, observe p50
  ~70–98 ms, identity probe 0.8 ms. The remaining floor is two GDI BitBlt
  captures (~40 ms each, hardware-bound) + two lossless PNG encodes (~27–80 ms on
  noisy content) + one full-frame diff (~30 ms) — reaching the 52 ms bar would
  require removing a mandatory capture or the lossless internal format, both
  verification-semantics changes this wave is forbidden to make. Back-to-back
  host calls still wait behind `min_screenshot_interval_ms=250` (contractual).
 

### Changed

- **The `run_goal` tool family is REMOVED** — `run_goal`, `create_subtask`,
  `list_subtasks`, `run_subtask`, and `get_session_progress` no longer exist; the
  server now exposes exactly five deterministic tools (`start_session`,
  `computer_observe`, `computer_screenshot`, `computer_execute`, `stop_session`).
  The internal autonomous loop (an internal LLM/vision decide phase deciding one
  action per model call) was removed because the server is driven exclusively by a
  host agent making direct tool calls with a real model — the loop doubled the
  failure surface and was the sole home of two confirmed defects: an unset
  `VISION_API_KEY` surfacing as an in-loop HTTP 401 instead of a clean typed error,
  and an unbounded ~500 KB screenshot payload serialized as text inside loop
  results. Both defect classes are nullified by the removal. Every direct
  `computer_execute` call passes through the identical grounding, validation,
  risk, approval, execution, and verification pipeline, so the per-action
  guarantees are unchanged; loop-only semantics (per-call approval budget,
  in-run bounded recovery/re-decide, `completion_evidence="model_declared"`
  labeling) are replaced by per-call `approved=True`, typed failure outcomes
  surfaced to the host, and host-decided completion on evidenced `verified`
  outcomes. The sealed checkpoint/resume machinery survives on
  `start_session(resume_from_checkpoint=…)`; the subtask-orchestration modules
  and their domain rules remain pinned by unit tests but are no longer reachable
  from any tool. Tests: loop-only suites removed, shared-behavior suites
  retargeted to the five-tool surface (not weakened); E2E and benchmark runner
  retargeted to direct calls.

**Compatibility notes:** BREAKING for any host that still called `run_goal`,
`create_subtask`, `list_subtasks`, `run_subtask`, or `get_session_progress` —
those calls now fail with "Unknown tool"; hosts must drive actions through
`computer_observe`/`computer_execute` (the documented direct-control pattern).
No surviving tool changed name, parameters, or response shape; the wire-schema
wins from v0.5.5 (content-block executes, flat type-array optionals, no
`outputSchema` on the block tools) are intact.

- **Docstring re-teaching for misclassification-prone models (L1-NEW-1):** live
  evidence showed the default non-vision model READ the `image_delivery` teaching,
  understood it, then self-misclassified ("my host is multimodal") and died on the
  default image mode anyway. The `start_session` tool description (and
  `computer_observe`'s cross-pointer) now teach the model to judge by what it
  RECEIVES (text-only inputs → `image_delivery="text"`, required; unsure → "text";
  text mode never crashes any model — a vision model only loses screenshot pixels)
  instead of asking it to classify itself; README/ARCHITECTURE document
  `CORTEX_IMAGE_DELIVERY=text` in the MCP server `env` block as the deterministic
  fallback for hosts whose default model cannot view images. Wording only — the
  D1 mechanism, defaults, and precedence are unchanged.

**Compatibility notes:** default `follow_ups` queue semantics changed — an
EXECUTED item whose verification outcome is `failed` no longer stops the batch (the
honest failed verdict still rides its per-item `follow_up_results` entry); set
`CORTEX_QUEUE_STRICT_VERIFY=1` to restore the strict stop-on-failed behavior.
Rejection `message`s now name the actual gate instead of the literal
"Grounding rejected." — drivers matching that exact string must match the new
gate-naming messages (`Action rejected by grounding/staleness check/validation/focus
allowlist: …`); the structured `reasons` payloads are unchanged. Unflagged
visual-change intents (drag, scroll, type, unstated expectations) keep their exact
pre-REM-B verification semantics; a flagged focus-type click can now answer
`uncertain` where it previously false-answered `failed` (never a new success).


## v0.5.0 (2026-09-07) — RELEASED (performance & effectiveness)

Validated by the project's internal QA process: full suite green, contracts
backward-compatible.

### Performance

- **SendInput input engine** — raw Win32 `SendInput` via stdlib ctypes is now the default physical-input path (PyAutoGUI becomes the selectable fallback; `CORTEX_INPUT_BACKEND=sendinput|pyautogui`), with fail-closed injection-error detection.
- **PNG encoding `optimize=False` by default** (−201 ms/frame measured; `CORTEX_PNG_OPTIMIZE=1` restores the old behavior).
- **Observe reuse** — the post-action capture becomes the next step's loop-top observation: 3 full captures per action step instead of 4 (measured before/after).
- **Rate-gate burst exemption** — `min_screenshot_interval_ms` (default 250) now protects only FRESH observations (loop-top capture, host-driven observes); intra-step verification captures (staleness probe, post-action capture) are burst-exempt but still recorded, so session-wide capture volume stays enforced and bounded.
- **Verification ladder** — for model-judge intents: deterministic window/process/text/predicate strategies first, the pixel-diff supporting check second, and the provider model judge only when both cheap tiers are inconclusive; a deterministic verdict always skips the judge.
- **`follow_ups` batching** — `computer_execute` accepts up to 5 queued action specs; each passes the full independent pipeline; the queue stops at the first verification failure, safety rejection, approval requirement, or post-action digest surprise, with per-item results in the additive `follow_up_results` field.

### Added

- **`include_screenshot_after`** opt-out on `computer_execute` — omit the heavy `screenshot_after_base64` payload (~1–2 MB) when the verification verdict is what matters.
- **`text_summary`** — additive one-line summary on `computer_observe`/`computer_screenshot` text blocks (active window title/process, cursor position, focused-control hint when UIA data exists, changed/unchanged note versus the previous observation).
- **UIA semantic fields** — optional `ocr_text` / `ui_elements` population from UIA snapshots (focused element first) where the platform supplies them; omitted gracefully when absent.

### Changed

- **`start_session` default flip: `dry_run` now defaults to `False`** (live session). The old `dry_run=True` default silently produced no-op sessions that cost a full agent turn to discover; pass `dry_run=True` explicitly for validation-only sessions (results carry the unmistakable `DRY-RUN (no input dispatched):` banner). `require_approval` still defaults to `True`.

### Fixed

- **`find_window_by_title` implemented** — EnumWindows enumeration with root-owner resolution (`GetAncestor(GA_ROOT)`) and case-insensitive matching with exact > prefix > substring precedence; window-title resolution no longer falls through to "unavailable" on the platform backend.

**Compatibility notes:** the `dry_run` default flip is the one behavioral change callers must review (loops that relied on the accidental no-op default must pass `dry_run=True` explicitly). Everything else is additive or strictly faster: tool names, parameter positions, and response shapes are unchanged, and no limit was removed or weakened.

## v0.4.1 (2026-09-06, hotfix — vision fix)

### Fixed

- `computer_observe` / `computer_screenshot` now return the screenshot as a real MCP `ImageContent` block (alongside the metadata text block) so vision-capable client models receive an actual image; the raw base64 never travels as text.

**Compatibility notes:** bug fix only; tool signatures and the metadata payload are unchanged.

## v0.4.0 (2026-09-06) — Long-Running Sessions

### Added

- Subtask decomposition with deterministic, fail-closed plan validation (duplicate ids, unknown/self/cyclic dependencies, oversize plans, invalid statuses, unsafe content reject the whole plan); hard ceiling of 50 subtasks per session.
- Dependency-ordered sequential execution; a failed subtask moves its transitive dependents to `blocked` automatically; bounded replan (at most 3 attempts per session) with replacement entries that never depend on dead ids.
- Atomic, sealed, versioned checkpoints — schema-version-gated loading, HMAC-SHA256 integrity seal keyed by a per-installation key file, 8 MB cap, redaction-enforced (secret-like values refuse the write) — written every 50 steps / 30 minutes and at lifecycle triggers; `start_session(resume_from_checkpoint=…)` resumes as a continuation: counters restored, never reset; the current environment re-verified; approval always fresh.
- Approval epochs (default 30 min / 50 interactive actions) and shared session budgets (`max_session_seconds`, `max_session_actions`, `max_session_model_calls`, `max_session_steps`, `max_subtasks`, `context_summarize_every`, … — validated and clamped fail-closed like the nine per-task limits).
- Four new MCP tools: `create_subtask` (works without any planner/LLM key), `list_subtasks`, `run_subtask`, and `get_session_progress` (deterministic progress percentage computed from subtask-manager state, never invented).

**Compatibility notes:** strictly additive. The six existing tools keep their names, parameter positions, and response shapes; `run_goal(..., auto_subtasks=True)` and `start_session(..., resume_from_checkpoint=…)` are trailing optionals — omitting them keeps byte-identical single-goal default behavior.

## v0.3.0 (2026-09-06 — move / hotkey / focus_window actions)

### Added

- `move` (hover without click), `hotkey` (compound 2–12-key shortcut chords), and `focus_window` (foreground a named window) actions with full pipeline support — grounding, validation, risk classification, execution, and deterministic verification defaults (`move` → cursor-position predicate, ±2 px; `focus_window` → window-state check against the requested title, never pixels; `hotkey` → `keypress` semantics with `window_state` promotion for open/launch/switch effects).

### Fixed

- `focus_window` targets are resolved and checked against the `allowed_windows` title allowlist (and the process allowlist) before any foregrounding call — all four rejection paths fail closed.

**Compatibility notes:** additive action vocabulary only; `focus_window` is risk-classified at least MEDIUM (category `window_focus_change`). No existing action, parameter, or response shape changed.

## v0.2.0 (2026-09-06) — initial production build / open-source release

First published release (developed internally as 0.1.0; the version was aligned
0.1.0 → 0.2.0 before the open-source release commit).

### Added

- The closed-loop executor: OBSERVATION → GROUNDING → VALIDATION → RISK/AUTHORIZATION → EXECUTION → RE-OBSERVE → SEMANTIC VERIFICATION, with bounded recovery/replanning (12-class failure taxonomy, 2 attempts per action, 6 per task, no blind coordinate retries) and an explicit `termination_reason` on every exit.
- Six core MCP tools: `start_session`, `computer_screenshot`, `computer_observe`, `computer_execute`, `run_goal`, `stop_session`.
- Safety and limits: contextual risk engine (`LOW`/`MEDIUM`/`HIGH`/`CRITICAL`), approval gates and `CRITICAL` blocking, nine hard limits with fail-closed enforcement, thread-safe stop token checked before every physical input, Win32 window/process identity, per-monitor DPI with measured coordinate-space classification (unverifiable spaces refuse coordinate input), and the single screenshot-to-input scale transform invariant.
- Secret redaction (10 detection patterns) enforced on the audit sink, provider payloads, and tool responses; five-channel prompt-injection doctrine; redacted JSONL audit trail plus metrics (19 counters, 5 latency summaries).
- `drag` action (press-move-release with stop-checked segments, both endpoints grounded and bounds-checked); compact-change pixel verification (strong-pixel detection for thin strokes and small controls).
- Real-Windows E2E suite (gated behind `CUMCP_RUN_E2E=1`) and OSWorld-2.0-aligned benchmark scaffolding (`benchmarks/` — harness only, no published scores).

**Compatibility notes:** initial release — no prior version to be compatible with. Windows-only execution backend (PyAutoGUI + Win32); Python ≥ 3.11; OCR and UIA are deliberate extension-point stubs that refuse fail-closed.
