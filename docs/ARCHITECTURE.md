# Architecture — Cortex 0.7.7

Current truth for the layered package as it exists in 0.7.7. Every claim is
traceable to a named module under `src/computer_use_mcp/`. Superseded and
removed machinery (the internal loop, the orchestration layer's removed halves,
the loop-era recovery/approval-epoch/health modules) is recorded, clearly
labelled, in [history/REMOVED-LOOP-AND-INTERNALS.md](history/REMOVED-LOOP-AND-INTERNALS.md);
release-by-release behavior lives in [../VERSIONS.md](../VERSIONS.md).
Companions: [TOOLS.md](TOOLS.md) (tool reference),
[REPLY-CONTRACT.md](REPLY-CONTRACT.md) (reply shapes),
[CONFIGURATION.md](CONFIGURATION.md) (limits + env),
[SAFETY.md](SAFETY.md) (safety model).

## 1. Scope and shape

- Single Python package, `src/` layout, stdio MCP server built on FastMCP
  (`server/` package; `FastMCP("Cortex")` in `server/instance.py`). The pip
  package is `computer-use-mcp`, the Python package `computer_use_mcp`, version
  0.7.7 (`core` truth: `pyproject.toml` and `src/computer_use_mcp/__init__.py`).
- Public launch paths (unchanged by the restructure):
  `python -m computer_use_mcp.server` (module entry `server/__main__.py`;
  logger name `computer_use_mcp.server`), `python -m computer_use_mcp.cli`,
  and the console scripts `cortex` / `computer-use-mcp` (both
  `computer_use_mcp.server:main`) and `cortex-mcp`
  (`computer_use_mcp.cli:main`).
- Layered package:

  ```
  core/       models, state, limits, textnorm        (pure: no I/O, no platform)
  security/   safety (risk engine), redaction, audit
  desktop/    backend, fake_backend, capture, clipboard, input_engine, motion,
              coordinates, errors, launch, win32, windows, focus_guard,
              interference, text_readers, pointer_ring
  vision/     observation, visual_views, text_substrates, evidence,
              pointer_sprite, recorder
  pipeline/   agent, grounding, validator, verification, provider
  planning/   plan, plan_validator, subtask_manager, checkpoint_manager,
              resume_manager, context_manager, long_running
  server/     instance, app, lifecycle, observe, execute, fetch, wait,
              wait_machine, record, record_machine, plan_surface, pointer_reply,
              replies, schemas, state, __main__
  cli.py      single module at the package root
  ```

- Recorded deviation from the proposed Addendum 3 layout: the proposed
  `platform/` layer landed as `desktop/` (the whole OS-facing surface —
  backend, capture, input, focus guard, interference, launch — stays in one
  layer, which keeps the platform seam to a single import boundary for the
  pipeline); `textnorm.py` landed in `core/` (it is a pure, import-free text
  canonicalizer consumed by `security/`, and core is the home of pure
  primitives); `cli.py` is a single module at the package root instead of a
  `server/cli.py` (stdlib-only, deliberately independent of the server
  package's MCP imports). Reorganization only — no behavior change; the
  equivalence proof is recorded in VERSIONS.md.
- Windows-first execution (`desktop/backend.py` `LocalComputerBackend`); a
  faithful in-memory `FakeComputerBackend` implements the same contracts for
  tests and non-Windows import. Python >= 3.11; runtime deps: `mcp`,
  `pydantic`, `pydantic-settings`, `Pillow`, `mss`, `pyautogui` (selectable
  fallback input engine; the default physical-input path is raw Win32
  `SendInput` via stdlib ctypes), `httpx`, `av` (PyAV — the screen-recording
  encoder; imported lazily by `vision/recorder.py`, so a missing wheel degrades
  to the typed `encoder_unavailable` reply, never an import crash).
- Test/benchmark layout: `tests/` offline suite (the live-desktop e2e is gated
  behind `CUMCP_RUN_E2E=1` and is the release's final gate, not a dev
  prerequisite); `benchmarks/` measurement scaffolding with `--mode fake` /
  `--mode env` (no published scores).

## 2. Layering and the import graph

Hard layering rules (enforced by review and by a test): nothing below the
pipeline layer imports `agent` or the `server` package; `pipeline/provider.py`
does NOT import `pipeline/verification.py` — model-based visual verification is
a `ModelJudge` callback interface implemented by the controller
(`agent._provider_judge` adapts `provider.judge_change`), breaking the cycle.
`planning/plan.py` stays import-free of `server/` (an AST pin). Measured edges:

```
core/models.py        universal pydantic types + RESULT_TOKENS/ok rule   (stdlib+pydantic)
core/textnorm.py      matching-only text canonicalizer                   (stdlib only)
core/state.py         StopToken, TaskState, SessionContext/Registry      -> models
core/limits.py        Limits + LimitEnforcer + SessionBudgetTracker      (stdlib only)
security/redaction.py secret detection/redaction                         -> models, core.textnorm
security/audit.py     AuditEvent JSONL sink, Metrics                     -> redaction
security/safety.py    RiskLevel contextual engine                        -> models, core.textnorm
desktop/backend.py    Win32 identity/DPI/monitors, coordinate integrity,  -> models, state
                      stop-checked input, capture knobs, fakes
desktop/*             capture, clipboard, input_engine, motion, launch,   -> models/backend
                      focus_guard, interference, text_readers
vision/observation.py capture orchestration + digest                      -> desktop, models
vision/visual_views.py observation view providers (raw pass-through /      -> models, text_substrates
                      coordinate grid + density; PIL)
vision/text_substrates.py OCR-once spatial text layer (UIA default,       -> models (+PIL)
                      optional side package; pixel_evidence opt-in)
vision/evidence.py    per-session image evidence store + fetch semantics  -> models
vision/recorder.py    the screen-recording core (capture loop, timeline,   -> core.limits,
                      keyframes, contact sheet, the PyAV encode ladder)      pointer_sprite
                                                                             (+PIL; av lazy)
vision/pointer_sprite.py the Cortex cursor sprites + the outbound          (stdlib + PIL)
                      compositor (hotspot-exact, mask pastes)
desktop/pointer_ring.py the press/release ring fed at the backend's        (stdlib only)
                      dispatch choke point (pressed-state truth)
pipeline/grounding.py GroundingStrategy protocol + coordinate/region      -> models
                      impls + text-anchor/accessibility seams
pipeline/validator.py staleness/binding/allowlists                        -> models
pipeline/verification.py verification strategies + engine                  -> models (+PIL)
pipeline/provider.py  doctrine prompt, fail-closed parsing, lazy key,     -> models, redaction
                      judge endpoint
pipeline/agent.py     direct-action pipeline (run_single) + queue +       -> audit, desktop,
                      interference guard integration                        grounding, limits,
                                                                            models, vision,
                                                                            safety, state,
                                                                            validator,
                                                                            verification
planning/plan.py      waypoint plans, lease aggregate, failure taxonomy   -> models, security.redaction
planning/*            checkpoint/resume/context (live) + subtask/plan     -> models, limits,
                      validator helpers (checkpoint path)                   redaction
server/*              the twelve MCP tools, reply shaping, wait machine,     -> everything above
                      recording tools + wait-machine-shaped record machine,
                      lifecycle/kill funnel/idle reaper, schemas            (+ mcp SDK)
cli.py                install/update/probe/uninstall-ocr CLI              (stdlib only)
```

## 3. Pipeline → module mapping

| Pipeline stage | Module(s) | Notes |
|---|---|---|
| Observation | `desktop/backend.py`, `vision/observation.py` | `observation_id`, UTC `timestamp`, `MonitorInfo` (bounds/primary/DPI), `WindowInfo(hwnd,pid,process_name,exe_path,window_class,title,bounds)`, cursor in screenshot space, coordinate-space classification (`verified_passthrough` / `scaled` / `unverifiable`), the OCR-once `spatial_text` block, the bounded UIA semantic read (`ui_elements`, focused-control value) |
| State construction | `core/state.py` | `TaskState` (bounded histories), `StopToken`, `SessionRegistry` (default cap 4, fail-closed refusal, no eviction) |
| Grounding | `pipeline/grounding.py` | `GroundingRouter` over strategies: coordinate (real), region descriptor (real), text-anchor and accessibility seams (derive targets when the data exists, refuse fail-closed otherwise); non-spatial actions ground trivially |
| Action validation | `pipeline/validator.py` | bounds, confidence floor, coordinate-space refusal, window/process allowlists, staleness + `source_observation_id` binding (one automatic re-observe + re-validate on `STALE_OBSERVATION`) |
| Risk + approval | `security/safety.py` (+ `core/textnorm.py` canonical views) | contextual LOW/MEDIUM/HIGH/CRITICAL; approval upgrade never downgraded; CRITICAL blocked; see SAFETY.md |
| Execution | `desktop/backend.py` + `desktop/input_engine.py` | `execute(action, stop)` — `StopToken.ensure_live()` before every physical input (per typed chunk / drag segment); 100 ms-sliced interruptible waits; timed motion (`motion.py`) |
| Post-action observation | `vision/observation.py` | fresh capture, new `observation_id`; burst-exempt intra-call capture reused as the next premise |
| Semantic verification | `pipeline/verification.py` | strategy chain; outcome ∈ {verified, failed, uncertain}; first definitive wins; deterministic tiers before the pixel diff; the model judge only when both cheap tiers abstain |
| Evidence | `vision/evidence.py` | reply-bearing captures retained per session (16 frames / 24 MB / 3600 s) and served by `computer_fetch_image` |
| Plans | `planning/plan.py` + `server/plan_surface.py` | waypoint wire models, normalization (explicit timing), lease, per-step execution, terminal aggregate, failure classification |
| Waits | `server/wait.py` + `server/wait_machine.py` | blocking-segment waits on worker threads, tombstones, cancel fan-in |
| Audit | `security/audit.py` | per-session JSONL, write-time redaction, `Metrics` counters + latency percentiles |
| Session lifecycle | `server/lifecycle.py` | start/stop, the ONE kill-path funnel (`_close_stopped_bundle`), the idle reaper |
| Limits | `core/limits.py` | LIVE gates: action budget, screenshot rate, session cap, `max_wait_seconds`; the rest RESERVED — table in CONFIGURATION.md |

## 4. The action surfaces

### 4.1 The direct execute pipeline (`pipeline/agent.py`)

`ComputerUseAgent.run_single(state, action, approved, expected_effect)` — one
host-supplied action through the full gate chain per `computer_execute` call:

1. **OBSERVE** — rate-gated fresh capture (`min_screenshot_interval_ms`; the
   wait polls the stop token every 50 ms, max 2 s, then `LimitExceeded`).
   Queued `follow_ups` reuse the previous item's post-action capture.
2. **GROUND** — route grounding, attach `GroundingResult`; coordinate actions
   bind `source_observation_id`. Failure → typed `rejected` outcome.
3. **VALIDATE** — a fresh pre-execution observation (capture-free identity-probe
   reuse when the premise is millisecond-fresh, `CORTEX_VALIDATE_REUSE_MS`) and
   staleness enforcement against it. Rejection → typed `rejected` returned to
   the host. When either allowlist is configured and the action is
   `focus_window`, the resolved target is checked against BOTH allowlists before
   anything executes.
4. **RISK/POLICY** — `SafetyContext` evaluation; a raising policy denies
   fail-closed. Denied → typed `safety_denied`.
5. **APPROVAL** — when policy requires approval and the call did not pass
   `approved=True`: `requires_approval` reply, zero dispatch. Per-call
   authorization; audited.
6. **EXECUTE** — dry-run short-circuits with a stub result (explicit DRY-RUN
   banner). Otherwise per-action scope reset, stop check, guard pre-dispatch,
   `backend.execute(action, stop_token)`. Execution exceptions surface as typed
   errors — no in-loop recovery.
7. **RE-OBSERVE + VERIFY** — fresh post-action observation; the verification
   baseline is ALWAYS the grounding-source observation. `verified` →
   `result:"verified"`; a stated effect satisfied only by pixels stays
   `uncertain`; every verdict rides the reply honestly.
8. **REPLY** — the unified envelope (`result` token, `ok` derived from it,
   three confidences, redacted payload), plus `follow_up_results` queue
   bookkeeping and `interference_events` when present.

Verification-intent defaults (`_build_intent`; an explicit hint always wins):
`type` → control-text/window-title text evidence first; `move` → the
`cursor_at_target` predicate (±2 px); `focus_window` → `window_state` (title
contains the target); `keypress`/`hotkey` with a launch-prefix effect
("open ", "launch ", "start ", "switch to ", "focus ") → `window_state`;
everything else → visual change. Outer guards: a fired stop token refuses all
work; `LimitExceeded` propagates typed; any other exception → structured `error`
outcome.

### 4.2 Plans (`server/plan_surface.py` + `planning/plan.py`)

One waypoint step per call under a lease. Wire models are explicit (at most 10
points, at most 5 actions per point, explicit timing and policies); a submitted
plan that leans on internal defaults is rejected. The lease refuses
`computer_execute` / a second `plan` / `computer_wait` / `computer_cancel_wait`
/ the observation tools mid-lease; `computer_wait_status` and
`computer_fetch_image` stay allowed. Approval is decided ONCE on the first plan
call and inherited by every step and by `replace`; `continue` never re-asks.
The terminal aggregate (`plan_terminal_outcome`) is pure:
`ok = (result == "verified")` by construction; a verified-but-incomplete prefix
caps at `unknown`. Failure classification (B6–B8) maps the reply's own signals
(error / outcome kind / stop reason) onto the closed `PLAN_FAILURE_REASONS`
vocabulary with `unclassified` as the fallback; completeness of the mapping is
test-enforced. Full wire rules: `skills/cortex-skill/references/plan-protocol.md`;
driver doctrine: `benchmarks/tasks_hard/DRIVER-PROTOCOL.md`.

### 4.3 Waits (`server/wait.py` + `server/wait_machine.py`)

`computer_wait` blocks up to one segment (default 45 s, band 1..55) on a worker
thread sliced at `WAIT_CANCEL_SLICE_SECONDS = 0.1`, so cancellation lands within
one slice; `computer_wait_status` blocks the next segment or peeks;
`computer_cancel_wait` cancels. Caps: 4 waits per session, 16 process-wide
(`too_many_waits`); the last 8 finished waits per session are tombstoned so a
late cancel or status gets honest numbers. The effective request ceiling is
`min(limits.max_wait_seconds, WAIT_HARD_MAX_SECONDS = 10800)`; a request above
it is refused naming the cap, never silently shortened. Waits are tool-running
time: a session with a running wait is never idle-reaped; the idle count starts
when the wait ends.

### 4.4 The evidence store (`vision/evidence.py`)

Every capture a session's REPLIES actually carry is retained under an
`img_<16-hex>` id — exactly the bytes the reply carried (for a derived view, the
derived image). Retention: `EVIDENCE_MAX_FRAMES = 16`,
`EVIDENCE_MAX_BYTES = 24 MB`, `EVIDENCE_MAX_AGE_SECONDS = 3600`; retirements are
tombstoned (`retired_by` names count_cap / byte_cap / age_cap; 64 tombstones).
A reply that carries no image (opt-out, text mode, rejection, approval gate,
view-derive failure) retains nothing for that call. `computer_fetch_image` is
read-only, allowed during a plan lease, and answers wrong/foreign/malformed ids
with the byte-identical `image_not_found`.

### 4.5 The pointer composite (`vision/pointer_sprite.py` + `desktop/pointer_ring.py`)

Every image the agent receives carries the Cortex cursor composited at the real
pointer: `vision/pointer_sprite.py` loads the shipped sprite assets (16–48 px,
Normal and Pressed states, hotspot at the arrow tip) lazily and composites the
sprite onto a COPY of the outbound image — the hotspot lands exactly on
`(round(x), round(y))` in the image's own pixel space, the Pressed state adds a
double-stroke ring at the click point, off-image pointers draw nothing and report
`in_image: false`. The composite runs at the outbound choke points only (observe
raw/grid, zoom crops, the execute half-res and full-res images, plan-step images,
fetch output); the internal `Observation` frames, the verification/digest
captures, the identical-pixel payload cache and the evidence store's held bytes
stay CLEAN. Pressed state comes from `desktop/pointer_ring.py` — a bounded ring
of press/release events fed at the backend's single dispatch choke point (both
input engines flow through it; no new Win32 calls). The additive reply fields
`pointer {x, y, state, in_image}` and `last_click {x, y, button, age_ms}` ride
every image-bearing reply; `start_session(show_pointer=...)` /
`computer_observe(show_pointer=...)` / env `CORTEX_SHOW_POINTER` turn the
composite off (off = the byte-identical pre-0.7.7 reply).

### 4.6 The recording family (`vision/recorder.py` + `server/record*.py`)

`computer_record` starts a background `ScreenRecorder` (one per session, 2 per
process): a daemon-side capture loop on its own mss instance, a fixed schedule
(0.5–10 s at 1–10 fps, exact frame counts with honest `dropped_frames`
accounting, a HARD wall ceiling an injected clock cannot defeat), frames held as
bounded JPEG bytes, a 1/4-scale noise-gated diff reduced to a change timeline,
timestamped keyframes (first, last, segment onsets + settled ends, largest
changes) and a bounded contact sheet. The dispatched-action ring
(`ActionRing`, fed from the pipeline's single dispatch finalize choke point)
correlates Cortex actions into the window. `computer_record_result` collects:
the timeline, the window's actions, the keyframe contact sheet (each keyframe
fetchable by `image_id` from the recording evidence pool) and, on request, an
H.264 MP4 encoded by PyAV (`av`, lazily imported) through a
bitrate/quality/resolution ladder under the blob cap (`CORTEX_RECORD_VIDEO_MAX_MB`,
default 2 MiB, band 1..8), the encoder preference (`CORTEX_RECORD_ENCODER`,
default `h264_mf,libx264` — Windows Media Foundation first, libx264 fallback)
and the client time budget (`over_budget` when the next rung cannot fit). The
worker encodes right after capture and is abortable: stop/cancel joins it off
the event loop, and an aborted encode is retried on a later collect while the
frames are held. Recordings live in a SEPARATE `RecordingEvidenceStore`
(`vision/evidence.py`): 24 keyframes / 16 MB / 3600 s covering the last 2
recordings per session; it never evicts an observation frame, and under pool
pressure the held frame payloads are evicted FIRST (the owner's JPEGs are freed
with them; the honest `frames_released` omission answers a later video request).
`save=true` writes the MP4 + contact-sheet PNG under the audit dir's
`recordings/` folder. Both tools dispatch no input: text sessions refuse
(`text_session_no_screen`), a plan lease allows them, and `record_start` /
`record_complete` / `record_cancelled` audit events fire once each per
recording.

## 5. Pinned contracts

**Observation** (`core/models.py`): `observation_id` (uuid hex), UTC
`timestamp`, `coordinate_space` (`verified_passthrough` | `scaled` |
`unverifiable`), `monitor: MonitorInfo|None` (id, index, virtual-screen bounds,
is_primary, dpi_scale_x/y), `active_window_info: WindowInfo|None`, cursor
fields, `ocr_text: list[TextRegion]|None` and `ui_elements: list|None` — the
semantic fields populated by a bounded semantic read of the foreground window
(raw-UIA COM when the box allows it, else a Win32 `GetGUIThreadInfo` +
`WM_GETTEXT` fallback; every failure degrades silently to `None`), plus the
D15 dropped-element accounting (`ocr_dropped`, per-region `text_truncated`,
`region_count + omitted_count` accounting for the whole read) and the
OCR-once `spatial_text` layer (`vision/text_substrates.py`; UIA substrate
default, optional `cortex_text_ocr` side package, never part of any digest).

**GroundedAction lineage**: every action carries `action_id`; coordinate
actions carry `source_observation_id` (the validator rejects missing bindings
and drift); `drag` carries both endpoints and fails closed unless both are
present; `hotkey` requires 2–12 key names; `focus_window`/`ensure_app` require
a non-empty `target` (max 200 chars, deliberately not the `text` redaction
channel). `risk` is filled only by the safety classifier; `hold_ms` /
`duration_ms` / `plan_point_settle_ms` are bounded fields (0..5000 / 0..5000 /
0..200) rejected at construction otherwise.

**Coordinate-transform invariant (F1)**: exactly one screenshot→physical
transform exists — `physical = origin + screenshot * scale`, applied exactly
once at execution in the backend. Grounding validates bounds in screenshot
space and records the verified scale; no strategy pre-scales a point.

**VerificationResult**: `outcome` ∈ {`verified`, `failed`, `uncertain`} is
authoritative; `uncertain` is never success anywhere; `verification_method`
names the deciding tier; `capture_provenance` records the visual tier's two
frames (diagnostic only, never alters `outcome`). The pixel tier counts a
screen as changed when the mean pixel difference reaches its threshold
(`DEFAULT_DIFF_THRESHOLD = 1.0`) or at least
`STRONG_CHANGE_MIN_PIXELS = 50` pixels changed by `STRONG_PIXEL_DELTA = 40`
or more per channel.

The default verification chain (first definitive outcome wins; an all-uncertain
chain combines into `uncertain`):

| Strategy (`verification_method`) | `verified` means | Without data |
|---|---|---|
| `deterministic_predicate` | a caller-supplied predicate returned True over (before, after) | `uncertain` |
| `window_state` | the active window title and/or bounds match the stated expectation | no window identity → `uncertain` |
| `process_state` | the foreground process name and/or pid matches | no process identity → `uncertain` |
| `ui_control_text` | the expected text appears in the window title or a control's value/name (the deciding tier for `type` when the text is visible) | no control text → `uncertain` |
| `text_predicate` | the expected text appears in the OCR regions of the after-observation (opt-in tier) | no OCR → `uncertain` |
| `focus_change` | deterministic focus/window/digest signals confirm a stated-effect click's focus transition | no signal → `uncertain` |
| `screenshot_diff` | pixels changed consistently with the expectation (mean-diff or strong-pixel counters above) | decode failure → `uncertain` |
| `model_visual` | the configured vision judge confirmed the transition | no judge → `uncertain` |

For model-judge intents the cheap-first ladder applies: the deterministic tiers
run first, the pixel diff second, the provider judge only when both abstain —
a deterministic verdict always skips the judge.

**Bounded late-repaint recovery (E-02)**: the post-action capture lands
16–32 ms after dispatch, so a UI that repaints a millisecond later used to be
missed forever (measured cliff: 0 ms → `verified`, 1 ms → `uncertain`). When
the verdict is `uncertain` SPECIFICALLY because the two frames were
byte-identical, the agent waits
`_IDENTICAL_FRAME_RECHECK_SETTLE_SECONDS = 0.05` s and takes exactly ONE more
capture (`_IDENTICAL_FRAME_RECHECK_LIMIT = 1` — never a poll loop). The
re-capture fires only when a fresh frame could plausibly carry new evidence:
the action states an `expected_effect`, or the action class is repaint-prone
(`type` / `scroll`). An effect-less `click`/`move`/`keypress` on a static
screen pays ZERO extra captures — captures per action stays at the 3.0
baseline on the fake backend
(`pipeline/agent.py:175-205`).

**The unified reply contract**: `RESULT_TOKENS` (the closed nine-token set) and
`ok_from_result` (`ok` True iff the token is `verified` or `marker`) live once
in `core/models.py:602-624`; every reply builder derives `ok` through that one
function. `EnsureAppOutcome` is the typed six-value launch-outcome enum.
Full shapes: [REPLY-CONTRACT.md](REPLY-CONTRACT.md).

**Confidence separation** (three distinct numbers, carried end-to-end):
model confidence (`model_confidence`, client-asserted — `computer_execute`
hardcodes 1.0), grounding confidence (`GroundingResult.confidence`), and
verification confidence (`VerificationResult.confidence`). Execution success is
`ok`, derived from the result token — not a fourth confidence.

**ComputerBackend**: `observe() -> Observation`; `execute(action, stop) -> str`
— implementations must `stop.ensure_live()` immediately before every physical
input. `find_window_by_title` resolves a top-level window by title
(case-insensitive, precedence exact > prefix > substring, first in Z-order
wins within a class); backends without enumeration return `None` and the
allowlist gate fails closed.

## 6. Checkpoints and resume (live orchestration remainder)

- **Atomic, redacted, versioned, sealed** (`planning/checkpoint_manager.py`):
  serialize → per-value redact → secret gate (a value still secret-like after
  redaction REFUSES the write) → size cap (`MAX_CHECKPOINT_BYTES = 8 MB`) →
  temp file + fsync + `os.replace`. `CHECKPOINT_SCHEMA_VERSION` gates loading;
  every checkpoint carries an HMAC-SHA256 seal over the tamper-sensitive fields,
  keyed by a per-installation key file (`<base>/.integrity_key`, created
  exclusively, never recreated on load — a missing key refuses the resume).
  The seal defends the FILE; a same-user attacker who can replace the key is
  out of scope (OS user boundary).
- **Cadence**: every `CHECKPOINT_EVERY_STEPS = 50` steps or
  `CHECKPOINT_EVERY_SECONDS = 1800`, whichever first, plus lifecycle triggers
  (`before_session_end` at stop, best-effort — durability never blocks
  stopping). HONEST SCOPE: only a session that already carries the
  long-running runtime — i.e. a session that was itself started with
  `resume_from_checkpoint` — has a runtime to checkpoint with, so a FRESH
  session's stop writes NO checkpoint file (fresh-session checkpoint creation
  does not exist in 0.7.7; `server/lifecycle.py` guards the write with
  `isinstance(runtime, LongRunningRuntime)`).
- **Resume as a continuation** (`planning/resume_manager.py`): seal and
  deterministic ceiling cross-checks verified BEFORE any restore
  (`invalid_checkpoint` / `resume_refused` at the MCP surface; the fresh
  session is discarded — nothing partially restores). Counters restored, never
  reset (monotonic; the elapsed anchor is re-based so wall-clock gaps cannot
  refill the duration budget). Limits are the checkpoint's own (re-clamped;
  canonical equality enforced at load) — a resume can never enlarge the budget.
  The current environment is re-verified against the checkpoint's recorded
  foreground process/window and allowlists; a mismatch (or missing current
  identity, treated as a mismatch) is a fail-closed refusal. Approval is never
  resurrected — a resumed session starts with nothing granted.
- **The sealed-overshoot gate**: a sealed checkpoint with inflated counters is
  restored EXACT and then fails closed with `SessionBudgetExceeded` at the
  budget seam the moment gated work is attempted.

## 7. Audit and metrics (`security/audit.py`)

**AuditEvent**: `timestamp` (UTC), `session_id`, `task_id`, `observation_id`,
`action_id`, `event_type`, `active_app`, `risk`, `result`, `duration_ms`,
`metadata` (dict, redacted). Sink: one JSONL file per session under
`COMPUTER_USE_MCP_LOG_DIR`; redaction enforced at write time (every string and
metadata value passes `redact_text`; values under sensitive-named metadata keys
are redacted wholesale); audit write failures never break the control loop.
Event families: the executor events (`observation`, `grounding`, `validation`,
`safety`, `approval`, `execution`, `verification`, `failure`, `stop`,
`emergency_stop`, `limit_exceeded`, `session_start`, `session_stop`), the
queue event, the interference event, the plan events (`plan_start`,
`plan_point`, `plan_complete`, `plan_stopped`), and the checkpoint/resume
events. Every executed action emits exactly ONE `verification` event carrying
the FINAL verdict (after demotions; markers and ensure_app probes included).

**Metrics** (`Metrics.snapshot()`): counters (`task_started/completed/failed`,
`action_total/success/failure`, `verification_verified/failed/uncertain`,
`grounding_failure`, `safety_block`, `approval_requested/granted/denied`,
`retry_total`, `screenshot_count`, dynamically-created extras) and latency
summaries (bounded 1024-sample deques with count/avg/p50/p95/max:
`observation_ms`, `model_ms`, `execution_ms`, `verification_ms`, `task_ms`).

## 8. Session lifecycle and the kill funnel (`server/lifecycle.py`)

- Sessions are capped (`max_sessions`, default 4); a refused create returns
  `session_limit_exceeded` LISTING the live sessions (`live_sessions`, most idle
  first, with `idle_seconds` / `in_flight` / `age_seconds`) plus
  `idle_timeout_seconds` — an agent that lost its ids can stop one to free a
  slot.
- **One stop, one observable outcome**: `stop_session`, the idle reaper, and an
  internally-armed kill path all funnel through `_close_stopped_bundle` — token
  armed, `stopped` mirrored onto state, stop + `emergency_stop` audited (once),
  bundle removed from the store and the registry, a bounded snapshot retained
  (capped at 1024). Every later call on that id returns the identical
  `session_stopped` error; an idle-reaped session's message names the inactivity
  stop (`source="idle_timeout"`, with `idle_seconds`).
- **The idle reaper**: a session idle past the timeout (default 900 s; env
  `CORTEX_SESSION_IDLE_TIMEOUT_SECONDS`, band 60..86400, `0` disabled,
  fallback-not-clamp) with nothing in flight is stopped through the same kill
  path. Any tool call addressing the session resets the count; sweeping is lazy
  (every tool entry + `start_session` before the limit check). A running wait is
  never reaped. (Addendum 2 + follow-up.)
- **The model can never reach the stop setter**: the token lives only in
  server-held session objects; the provider receives plain redacted data and its
  output is parsed into a fixed pydantic schema — no object graph reaches the
  token, structurally.

## 9. Extension points

- **Text-anchor / accessibility grounding**: populate `ocr_text` /
  `ui_elements`; the strategies derive targets from data and refuse fail-closed
  otherwise, degrading to coordinate grounding. The UIA semantic read that
  populates `ui_elements` is LIVE by default (`CORTEX_UIA_READ`).
- **OCR verification**: `text_predicate` is the opt-in last-resort tier
  (`CORTEX_OCR_TEXT_VERIFICATION`); the `cortex_text_ocr` side package
  (`sidepackages/`) is the optional Windows.Media.Ocr substrate behind the
  `TextSubstrate` seam.
- **Model-judge verification**: inject a `ModelJudge` into
  `VerificationEngine`, or rely on the controller adapting
  `provider.judge_change` (the default path; degrades to `uncertain` without a
  configured provider). Lazy key: the provider resolves `VISION_API_KEY` →
  `OPENAI_API_KEY` at first use and failures surface fail-closed at judge time.
- **Application-specific verification strategies**: the `VerificationStrategy`
  protocol + `VerificationEngine` injection seam is proven by the e2e suite's
  injected strategies.
- **Pixel-level screenshot secret detection**: `redaction._scan_image_for_secrets`
  is a stable hook returning `[]`; implementing it activates region blur.
- **Non-Windows / remote backends**: `ComputerBackend` is the seam;
  `FakeComputerBackend` demonstrates the full contract.

## 10. Real-Windows e2e and benchmarks

- `tests/e2e/` drives real applications (Notepad, classic Calculator via
  `win32calc.exe`, Edge on a local page) through the server's own MCP tool
  surface — no vision model, no network. Desktop tests are auto-skipped BY
  DEFAULT; the gate opens only for the exact `CUMCP_RUN_E2E=1` (fail-closed
  against every near-miss spelling; pinned). Every e2e app instance carries a
  run-unique window token and attach goes through marker-only enumeration, so
  the suite can never attach to, type into, or close a window it did not launch;
  teardown closes exactly the launched instance. Per-test deadline fixture,
  local evidence artifacts under `evidence/e2e/` (machine-local test outputs).
  Because the default chain cannot semantically verify several real transitions,
  the suite injects application-specific deterministic strategies via the
  documented protocol.
- `benchmarks/` is measurement scaffolding only: task YAMLs
  (OSWorld-2.0-aligned), `benchmarks/tasks_hard/` with
  `benchmarks/tasks_hard/DRIVER-PROTOCOL.md` (the driver-facing doctrine the
  tool-surface events and suffixes are specified against), and a runner with
  `--mode fake` (default) / `--mode env`. Every artifact carries
  "Harness validation output — NOT benchmark scores."; no scores are published.

## 11. The Interference Guard (summary)

The Interference Guard (`desktop/interference.py` policy +
`desktop/focus_guard.py` runtime) only ADDS rejections or annotations between
the existing gates — no existing gate is weakened. Five policy sections on
`start_session(interference={...})`, parsed fail-closed
(`invalid_interference`): `focus_guard` (bound-target foreground verification),
`attach_or_launch` (ensure_app attach-before-launch doctrine; the DEFAULT
launch policy is `"server"` — see SAFETY.md for the launch truth),
`dialog_sentinel` (modal-dialog interception; `auto_handle` ships EMPTY),
`focus_continuity` (keyboard-focus verification; mid-type drift aborts the
in-flight type), `hotkey_guard` (pre-chord stuck-modifier sweep). Events
(`FOCUS_TAKEN_BY`, `FOCUS_IDENTITY_UNKNOWN`, `FOCUS_DRIFTED`, `MODAL_DIALOG`,
`REATTACHED`, `AMBIGUOUS_INSTANCE`, `NO_INSTANCE`, `STUCK_MODIFIER`,
`TARGET_GONE`, `REANCHOR_REFUSED`) appear in `reasons`, `interference_events`,
verification notes, and audit events. Full behavior and residual risks:
[SAFETY.md](SAFETY.md) §Interference Guard.
