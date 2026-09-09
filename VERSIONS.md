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

## Unreleased

In flight toward v0.5.5 (mission ORVEX-CORTEX-055, 2026-09-08/09 — three live-usage
defect classes from FailedLog.txt, plus live-Kimi hardening).

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
  ensure_app + observe + click round-trips 0.14–1.4 s per tool call.

### Compatibility notes

- **Default behavior change:** a session with an allowlist can now server-LAUNCH an
  allowlisted `ensure_app` target (`launch="driver"` or `CORTEX_ATTACH_OR_LAUNCH=driver`
  restores never-launch). Non-allowlisted targets never launch — fail-closed unchanged.
- **Tool output shape:** executed `computer_execute` calls return `[TextContent,
  ImageContent]` blocks (was: dict with `screenshot_after_base64`); error/rejection shapes
  stay plain dicts. The image rides as a real image block under the 180 KB outbound budget.
- **Wire schemas:** Optional parameters advertise type-array forms instead of `anyOf`
  (runtime validation semantics unchanged; `limits` still fail-closed strict).


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
