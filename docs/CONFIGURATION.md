# Configuration — limits and environment variables (Cortex 0.7.6)

Current truth for every knob the code reads. Pinned by
`tests/test_docs_truth.py`: the limits table below is re-derived at test time
from `core/limits.py` (defaults and clamp bounds), the env-var NAMES are
grepped from the source, and the knob rows whose defaults/bands the code
resolves through helpers are pinned value-by-value — a code change or a doc
drift on those fails the test.

## Limits (`start_session(limits={...})`)

`limits` accepts a dict of these field names; values are clamped into the band
by `Limits.validate()`; unknown names or non-numeric values are rejected
fail-closed (`invalid_limits`).

**LIVE on the direct path**: `max_actions` (the action budget),
`min_screenshot_interval_ms` (the screenshot-rate gate protecting FRESH
captures; intra-call verification captures are burst-exempt but recorded), and
`max_sessions` (the registry cap — a new session at capacity is refused, never
evicting; idle sessions are swept before the check). `max_wait_seconds` bounds
every `computer_wait` request (effective cap `min(max_wait_seconds, 10800)`).
The session setting `max_steps` (band 1..500) is also LIVE — enforced by the
safety gate and the plan surface's budget accounting.

**RESERVED — accepted and validated, not enforced** (the loop-era trip points
were removed; see docs/history/): every other session-level field. What still
applies: the checkpoint cross-checks (`budget.subtasks <= max_subtasks`,
`budget.steps <= max_session_steps`) and the sealed-overshoot gate on resume.

| Field | Default | Band | Status | Meaning |
|---|---|---|---|---|
| `max_task_seconds` | 900.0 | 1..3600 | advertised, no production call site | per-task duration |
| `max_actions` | 100 | 1..500 | LIVE | input actions per task (waits and markers cost none) |
| `max_retries_per_action` | 5 | 0..5 | RESERVED | loop-era same-instance retries |
| `max_recovery_per_action` | 2 | 0..10 | RESERVED | loop-era recovery budget |
| `max_recovery_per_task` | 6 | 0..50 | RESERVED | loop-era recovery budget |
| `max_model_calls` | 60 | 1..1000 | RESERVED | loop-era decide-phase calls |
| `min_screenshot_interval_ms` | 250 | 0..60000 | LIVE | fresh-capture rate gate |
| `max_context_items` | 50 | 1..1000 | RESERVED | loop-era context size |
| `max_sessions` | 4 | 1..64 | LIVE | concurrent sessions (fail-closed refusal, no eviction) |
| `max_session_seconds` | 14400.0 | 1..86400 | RESERVED | session wall-clock duration |
| `max_session_actions` | 2000 | 1..2000 | RESERVED | actions across a session |
| `max_session_model_calls` | 500 | 1..500 | RESERVED | model calls across a session |
| `max_session_steps` | 500 | 1..500 | RESERVED | loop steps across a session |
| `max_subtasks` | 50 | 1..50 | RESERVED | subtask ceiling (checkpoint cross-check bound) |
| `context_summarize_every` | 25 | 1..500 | RESERVED | steps between context compressions |
| `approval_epoch_seconds` | 1800.0 | 60..86400 | RESERVED | loop-era approval-epoch wall-clock half |
| `approval_epoch_actions` | 50 | 1..1000 | RESERVED | loop-era approval-epoch action half |
| `health_check_interval` | 600.0 | 60..3600 | RESERVED | loop-era boundary health checks |
| `max_wait_seconds` | 10800.0 | 0..10800 | LIVE | per-request `computer_wait` bound |

Session defaults on `start_session` (not part of `limits`): `max_steps` 30
(band 1..500), `max_retries_per_action` 1 (band 0..5), `min_confidence` 0.70.

## Environment variables

Precedence for delivery knobs is param > env > default unless noted. Fail-safe
parsing: a malformed value falls back to the documented default (never clamps a
typo into a different meaning where the row says "fallback-not-clamp").

### Session lifecycle and delivery

| Variable | Default | Values / band | Purpose |
|---|---|---|---|
| `CORTEX_SESSION_IDLE_TIMEOUT_SECONDS` | 900 (15 min) | accepted 60..86400; `0` disabled; out-of-band → default (fallback-not-clamp) | the idle reaper: a session with no tool call this long, and nothing in flight, is stopped through the stop_session kill path; any call resets the count |
| `CORTEX_IMAGE_DELIVERY` | `image` | `text` \| `image`; garbage → `image` | default screenshot delivery for ALL sessions; `text` suppresses every outbound image block (non-vision-safe). Overridden per session by `start_session(image_delivery=...)` |
| `CORTEX_RESULT_IMAGE_MAX_KB` | 180 | KB, > 0, capped at 100000 | the outbound image budget on observe/zoom/execute replies (JPEG q85 + downscale ladder; internal PNG untouched) |
| `CORTEX_ACTION_IMAGE_FULL` | off | exactly `1` = on | restore FULL-resolution defaults for the executed-response image (the default is a half-resolution JPEG) |
| `COMPUTER_USE_MCP_LOG_DIR` | `<temp>/cortex/logs` | directory | root for per-session audit logs (`audit_<session>.jsonl`) |
| `COMPUTER_USE_MCP_CHECKPOINT_DIR` | `<temp>/computer-use-mcp/checkpoints` | directory | root for per-session checkpoint files (one `checkpoint.json` + the `.integrity_key` seal key) |
| `LOG_LEVEL` | `INFO` | logging level | server log level (stderr) |

### The wait family

| Variable | Default | Values / band | Purpose |
|---|---|---|---|
| `CORTEX_WAIT_SEGMENT_SECONDS` | 45 | accepted 1..55; out-of-band → default (fallback-not-clamp) | how long one `computer_wait` / `computer_wait_status` call blocks; always kept under the MCP client's 60 s tool timeout |

### Verification and pipeline

| Variable | Default | Values / band | Purpose |
|---|---|---|---|
| `CORTEX_VALIDATE_REUSE_MS` | 1500 | `0` disables; negative → default | freshness window for the capture-free validate-reuse probe on direct actions |
| `CORTEX_QUEUE_STRICT_VERIFY` | off | exactly `1` = on | restore stop-on-failed `follow_ups` queue semantics (default: the honest failed verdict rides the item, the batch continues) |
| `CORTEX_QUEUE_STRICT_DIGEST` | off | exactly `1` = on | restore whole-screen premise checking for queued batches (default: only TRUE window-identity staleness stops) |
| `CORTEX_DIFF_FAST` | on | `0` = off | the raw-frame pixel-diff fast path (disable for the legacy decode path) |
| `CORTEX_OCR_TEXT_VERIFICATION` | off | truthy = opt-in | enable the OCR `text_predicate` verification tier as a last resort |
| `CORTEX_UIA_READ` | on | `0` = off | the UIA semantic read that populates `ui_elements` / focused-control text |

### Capture and images

| Variable | Default | Values / band | Purpose |
|---|---|---|---|
| `CORTEX_CAPTURE` | `blt` | `blt` \| `dxgi` (other → `blt`) | capture backend; `dxgi` is pure-ctypes Desktop Duplication and permanently falls back to the mss path if duplication cannot be created or fails |
| `CORTEX_INTERNAL_FRAME_REUSE` | on | `0` = off | the capture-time RGB frame stash + identical-pixel PNG payload dedupe (mechanical speed; digests/verification byte-identical) |
| `CORTEX_PNG_COMPRESS_LEVEL` | unset (PIL default) | int 0..9 | PNG encode/size trade for capture throughput |
| `CORTEX_PNG_OPTIMIZE` | off | `1` = on | PIL `optimize=True` (slower encode) |
| `CORTEX_TEXT_PNG` | off | `1` = on | keep real PNG payloads in text-delivery sessions (default: deterministic raw-pixel keys; digests unaffected) |

### Input engine

| Variable | Default | Values / band | Purpose |
|---|---|---|---|
| `CORTEX_INPUT_BACKEND` | `sendinput` | `sendinput` \| `pyautogui` | physical-input engine (both honor the same stop/failsafe contract) |
| `CORTEX_PYAUTOGUI_PAUSE` | 0.0 | seconds ≥ 0 | fallback-engine PAUSE |
| `CORTEX_TYPE_INTERVAL` | 0.0 | seconds ≥ 0 | per-chunk typing pacing |
| `CORTEX_DRAG_INTERPOLATE` | on | `0` = off | segmented drag strokes (button always released, incl. mid-stroke stop) |
| `CORTEX_DRAG_STEP_PAUSE` | 0.001 | seconds ≥ 0 | per-segment drag pacing (the SendInput factory's ~1 ms live default) |
| `CORTEX_DRAG_STEP_PIXELS` | 8 | int ≥ 1 | drag stroke granularity (the SendInput factory's ~8 px live default) |
| `CORTEX_KEY_DISPATCH_GAP` | 0.05 | seconds ≥ 0 (`0` disables) | terminal-key chords pace behind a prior keyboard dispatch (drops the final-Enter race) |
| `CORTEX_FOCUS_SETTLE_SECONDS` | 0.3 | seconds ≥ 0 | keyboard input paces behind a recent focus transition (first-keys-after-activation race) |
| `CORTEX_TYPE_INTEGRITY` | on | `0`/`off` = legacy | typed-text read-back verification (the `integrity=` message suffix; `0` restores the byte-identical legacy dispatch) |
| `CORTEX_TYPE_CHUNK_CHARS` | 64 | clamped 1..4096 | typing chunk size |
| `CORTEX_TYPE_VERIFY_SETTLE_SECONDS` | 0.05 | seconds ≥ 0 | settle before the post-dispatch read-back |
| `CORTEX_TYPE_VERIFY_CONFIRM_GAP_SECONDS` | 0.04 | seconds ≥ 0 | spacing between quick confirm reads |
| `CORTEX_TYPE_VERIFY_FAST_MAX_READS` | 4 | clamped 2..8 | quick reads the fast verify path may spend |
| `CORTEX_TYPE_VERIFY_STABILITY_SECONDS` | 2.5 | seconds ≥ 0 | landing-lag horizon before repair decisions (escalation only) |

### Motion (timed moves and settles)

| Variable | Default | Values / band | Purpose |
|---|---|---|---|
| `CORTEX_MOVE_DURATION_MS` | 0 (instant) | accepted 200..500; out-of-band → default | outside-plan timed-motion default (the outside-plan move is instant by policy) |
| `CORTEX_PLAN_MOVE_DURATION_MS` | 0 | accepted 0..500 | fail-safe in-plan transition duration (a submitted plan states its own timing) |
| `CORTEX_MOVE_SHAPE` | `linear` | `linear` \| `quadratic_bezier` | motion path shape |
| `CORTEX_MOVE_BEND_PX` | 0 | accepted -5000..5000 | signed path bend (screen envelope still applies) |
| `CORTEX_MOVE_EASING` | `linear` | `linear` \| `smoothstep` | timing-only easing |
| `CORTEX_PLAN_SAMPLE_INTERVAL_MS` | 12 | accepted 8..16 (fallback-not-clamp) | timed-motion sampling cadence |
| `CORTEX_MOVE_SETTLE_MS` | 0.0 | accepted 0..200 | cursor-reposition settle (0.7.6 default 0 — the unconditional 50 ms settle was removed) |

### Launching

| Variable | Default | Values / band | Purpose |
|---|---|---|---|
| `CORTEX_ATTACH_OR_LAUNCH` | `server` | only `driver` (case-insensitive) restores never-launch; anything else → `server` | default `attach_or_launch.launch` policy for `ensure_app` |
| `CORTEX_LAUNCH_PATHS` | unset | `needle=path;needle=path` (semicolons; quoted paths OK) | needle→executable mapping for GUI apps not on PATH; resolution order: mapped path → `PATH` → Microsoft Store alias → raw-name spawn; malformed entries skipped fail-safe. LOCAL TRUSTED CONFIGURATION (host `env` block) |

### Vision provider (the model-judge tier only)

The deterministic surface needs NO key. The provider is lazy: a session works
without any of these; a missing key raises a typed fail-closed error only if a
call actually reaches the judge tier.

| Variable | Default | Purpose |
|---|---|---|
| `VISION_API_KEY` | unset | API key for the vision provider (judge tier) |
| `OPENAI_API_KEY` | unset | fallback when `VISION_API_KEY` is unset |
| `VISION_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible base URL |
| `VISION_MODEL` | `gpt-4.1-mini` | multimodal judge model |

### Read internally, not knobs

`PYTHONPATH` is consulted only to discover the optional `cortex_text_ocr`
side package; `LOCALAPPDATA` only to resolve Microsoft Store execution aliases
during an authorized launch. They are host OS variables — Cortex reads them but
does not define them.
