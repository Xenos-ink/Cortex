<h1 align="center">Cortex</h1>

<p align="center"><img src="Cortex.png" alt="Cortex" width="340"></p>

<p align="center">A closed-loop computer-use MCP server for Windows — agents observe the screen, propose actions, and Cortex proves that each action did what it was supposed to do.</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/Python-3.11%2B-blue.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/platform-Windows-blue.svg" alt="Platform: Windows">
</p>

## What is Cortex

Cortex is a Model Context Protocol (MCP) server that gives AI agents a safe, verifiable way to operate a Windows desktop. An agent connects over stdio and gets six core tools — start a guarded session, capture the screen, execute a single action (click, double-click, drag, move, type, keypress, hotkey, scroll, wait, focus_window), hand a goal to the autonomous loop, stop everything at any moment — plus four long-running session tools ([Long-Running Sessions](#long-running-sessions)). Between the proposal and the physical input, Cortex runs a fixed pipeline: it grounds the action against a fresh observation, validates it (staleness, allowlists, coordinate integrity), classifies its risk, requests approval when policy requires it, and only then executes through a stop-checked backend.

After execution, Cortex re-observes the screen and verifies the action **semantically**: did the intended state transition actually occur? Every verification ends in an explicit outcome — `verified`, `failed`, or `uncertain` — and `uncertain` is never treated as success. Failures route into a bounded recovery layer that classifies the fault (moved window, unexpected dialog, stale screen, blocked input) and re-plans instead of retrying blindly.

The design principle: the goal is not to click the right pixel — it is to reach the intended state and prove that it happened. Every exit from the loop carries an explicit, audited termination reason, and every session writes a redacted JSONL audit trail of every pipeline phase.

## How it works

```text
 start_session
      |
      v
 OBSERVATION --> GROUNDING --> VALIDATION --> RISK/AUTHORIZATION --> EXECUTION
 (screenshot +   (bind the     (staleness,     (contextual risk,      (stop-checked
  identity:       target to     allowlists,    approval gates,       physical input,
  monitor/DPI,    the screen)   coordinate     CRITICAL block)       100 ms wait
  window/process)               integrity)                           slices)
                                                                      |
      +---------------------------------------------------------------+
      |
      v
 RE-OBSERVE --> SEMANTIC VERIFICATION --(verified)--> next action --+
                    |                                               |
                    +--(failed / uncertain)--> RECOVERY/REPLANNING -+
                                                (bounded: 2/action, 6/task;
                                                 back to OBSERVATION)

 stop token / resource limits / approval exhaustion --> FAIL SAFELY
 (every exit carries an explicit termination_reason)
```

**Observation.** The backend captures the screen with `mss` and reads the foreground window's identity through Win32: `hwnd`, `pid`, process name, executable path, window class, title, and bounds. Each observation also carries per-monitor DPI, the cursor position, a fresh `observation_id`, a UTC timestamp, and a measured coordinate-space classification — `verified_passthrough`, `scaled`, or `unverifiable`. An `unverifiable` space refuses coordinate input outright; coordinates are never sent to the OS on a guess.

**State.** Each session owns a bounded `TaskState` (histories are capped deques), a thread-safe `StopToken`, a `LimitEnforcer` carrying nine hard limits, an audit logger, and a metrics registry. A registry caps concurrent sessions (default 4) and refuses new sessions fail-closed at capacity instead of evicting live ones.

**Grounding.** The grounding router binds the action's target to the screen. Coordinate grounding bounds-checks the point in screenshot space — both endpoints for a drag — and records the verified screenshot-to-input scale without ever rewriting the coordinate; the single scale transform is applied exactly once, in the backend, at execution. Region, text-anchor (OCR), and accessibility (UIA) strategies can derive a target when the data exists; OCR and UIA are currently extension points that refuse fail-closed when absent, and the pipeline degrades gracefully to coordinate grounding. Non-spatial actions (type, keypress, hotkey, scroll, wait, done, focus_window) ground trivially — they carry text, key names, or a window title instead of coordinates.

**Action validation.** Before anything executes, a second fresh observation is captured and the action is validated against it: screen bounds, confidence floor, window and process allowlists (process identity is authoritative; unknown identity while an allowlist is configured is a fail-closed rejection), and staleness. Coordinate actions bind to the observation they were grounded from; if the HWND, process, monitor, screenshot dimensions, or coordinate space drifted, the action is rejected (`STALE_OBSERVATION`) and the loop re-observes instead of executing stale coordinates.

**Risk / authorization.** A contextual engine classifies every action `LOW` / `MEDIUM` / `HIGH` / `CRITICAL` from the action type, its text, the active window/process identity, and the goal. `HIGH` always requires approval; `CRITICAL` is blocked pending explicit authorization that nothing on screen and nothing the model says can provide; a high-risk action with unknown context escalates to `CRITICAL` (fail closed). Approval messages state the action, the target identity, the reason, the consequence, and how to approve.

**Execution.** The backend performs the physical input through PyAutoGUI. The stop token is checked before every input and between typed characters; waits are sliced at 100 ms so a stop lands within one slice; drags interpolate the stroke in segments and always release the mouse button, including on a mid-stroke stop. `focus_window` is the deliberate exception to PyAutoGUI: it drives the Win32 foreground switch directly (restore when minimized, then the standard `AttachThreadInput` foreground switch with an ALT-key nudge) and verifies that the foreground actually moved — a refusal is a typed `WindowFocusError`, never a silent success. Dry-run sessions validate the full pipeline but short-circuit before any input.

**Re-observe.** A fresh post-action capture is taken, rate-gated inside the loop by `min_screenshot_interval_ms` (default 250 ms).

**Semantic verification.** A chain of six strategies answers "did the intended transition happen?" with `verified` / `failed` / `uncertain`. The first definitive outcome wins; an all-uncertain chain combines into `uncertain`; no code path upgrades `uncertain` to success (asserted by a dedicated test). The comparison baseline is always the observation the action was grounded from.

**Recovery / replanning.** Failures classify into a 12-value taxonomy. Recovery is bounded at 2 attempts per action and 6 per task; the same coordinates are never retried blindly — the loop re-observes, re-grounds, and re-decides. Authentication prompts always terminate safely; credentials are never auto-typed.

**Complete / fail safely.** Every exit carries one of eight termination reasons (`completed`, `failed_verification`, `blocked_safety`, `approval_exhausted`, `limit_exceeded`, `stopped_by_user`, `unrecoverable`, `provider_error`). A model-declared "done" ends the task but is labeled `completion_evidence="model_declared"` — a model assertion, never presented as an evidenced check.

## Features

| | |
|---|---|
| **Closed-loop execution** | Every action is observed, grounded, validated, risk-checked, executed, re-observed, and semantically verified. |
| **Verifiable outcomes** | `verified` / `failed` / `uncertain` on every action; `uncertain` is never success. |
| **Staleness protection** | Coordinate actions bind to the observation they were grounded from; screen-identity drift refuses the action. |
| **Coordinate integrity** | Measured screenshot-vs-input classification (DPI-aware); unverifiable spaces refuse coordinate input. |
| **Contextual risk engine** | `LOW`/`MEDIUM`/`HIGH`/`CRITICAL` from action + context; approval gates and `CRITICAL` blocking. |
| **Bounded recovery** | 12-class failure taxonomy, 2 attempts/action, 6/task, no blind coordinate retries, no auto-typed credentials. |
| **Kill switch** | Thread-safe stop token checked before every physical input; `stop_session` closes the session fail-closed. |
| **Audit trail** | Per-session JSONL with every phase event, redaction enforced at write time, 19 counters + 5 latency metrics. |
| **Secret redaction** | 10 detection patterns enforced on audit, provider payloads, and tool responses; secret-like typed text is blocked. |
| **Prompt-injection containment** | Five-channel prompt doctrine; screen text is untrusted data, never instructions or authorization. |
| **Hard resource limits** | 9 enforceable limits (task time, actions, model calls, screenshot rate, sessions) with fail-closed termination. |
| **Long-running sessions** | Subtask decomposition with deterministic plan validation, dependency-ordered sequential execution, atomic checkpoints/resume, approval epochs, and shared session budgets — all through the same closed-loop executor. |
| **Benchmark scaffolding** | OSWorld-2.0-aligned task format plus a fake/env harness runner — harness only, no published scores. |

## Requirements

- **Windows 10, Windows 11, or Windows Server** with an active, interactive display session. Observation and input go through the real desktop; there is no headless mode.
- **Python 3.11 or newer.**
- **An OpenAI-compatible vision endpoint** — required only for the autonomous `run_goal` mode (the model that decides actions and judges screenshots). Deterministic direct control through `computer_observe` + `computer_execute` calls no model at all.

## Installation

From git:

```bash
pip install git+https://github.com/Xenos-ink/Cortex.git
```

Or from a clone, editable with the dev tools:

```bash
git clone https://github.com/Xenos-ink/Cortex.git
cd Cortex
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1        # Git Bash / WSL: source .venv/Scripts/activate
python -m pip install -e ".[dev]"
```

Verify the install:

```bash
python -c "import computer_use_mcp; print(computer_use_mcp.__version__)"
```

The package installs two console scripts, `cortex` and `computer-use-mcp`, which start the stdio MCP server. Running one blocks while it waits for MCP traffic on stdin — that is what a stdio server does; configure it in an MCP client instead of running it interactively (next section).

**Updating**

```bash
pip install --upgrade git+https://github.com/Xenos-ink/Cortex.git
```

or, from a clone:

```bash
git pull && python -m pip install -e ".[dev]"
```

## Running the server

```bash
python -m computer_use_mcp.server     # or just: cortex
```

Both entry points speak MCP over stdio: stdout carries the protocol, logs go to stderr, and per-session audit logs are written under `%TEMP%\cortex\logs` (override with `COMPUTER_USE_MCP_LOG_DIR`).

### MCP client configuration

A generic `mcpServers` block; the same shape works in Claude Code, ZCode, Cursor-style client configs. Adjust paths to your machine:

```json
{
  "mcpServers": {
    "cortex": {
      "command": "C:\\path\\to\\.venv\\Scripts\\python.exe",
      "args": ["-m", "computer_use_mcp.server"],
      "cwd": "C:\\path\\to\\Cortex",
      "env": {
        "VISION_BASE_URL": "https://api.openai.com/v1",
        "VISION_MODEL": "your-vision-model",
        "VISION_API_KEY": "${VISION_API_KEY}"
      }
    }
  }
}
```

The `env` block is only needed if you plan to use `run_goal`; deterministic direct control needs no API key.

### Environment variables

| Variable | Purpose |
|---|---|
| `VISION_API_KEY` | API key for the vision provider. **Lazy:** the server starts and `start_session` works without it; it is required only when `run_goal` makes its first model call (a missing key raises a typed, fail-closed error at that point). |
| `OPENAI_API_KEY` | Fallback used when `VISION_API_KEY` is unset. |
| `VISION_BASE_URL` | OpenAI-compatible API base URL (default `https://api.openai.com/v1`). |
| `VISION_MODEL` | Multimodal model name (default `gpt-4.1-mini`). |
| `COMPUTER_USE_MCP_LOG_DIR` | Root directory for per-session audit logs (default `%TEMP%\cortex\logs`). |
| `COMPUTER_USE_MCP_CHECKPOINT_DIR` | Root directory for per-session long-running checkpoints (default `%TEMP%\computer-use-mcp\checkpoints`; one `checkpoint.json` per session). |
| `LOG_LEVEL` | Server log level (default `INFO`). |

## Usage

### Two integration patterns

**Deterministic direct control.** The driving agent (or a human-supervised script) calls `computer_observe` and `computer_execute` itself: observe, decide what to do, execute one action, read the verification result, repeat. You keep full control of every step and no vision API is needed — but every action still passes through grounding, staleness validation, the risk engine, the approval gate, and semantic verification. Prefer this pattern when the steps are known in advance, when you want the calling agent to reason over raw observations, or when you are testing and want deterministic behavior.

**Autonomous goal mode.** `run_goal` hands a goal to a configured vision model. The model observes the screen and proposes actions; Cortex grounds, validates, risk-checks, executes, verifies, and recovers with bounded autonomy — an approval budget of exactly one action per call, recovery bounded at 2 attempts per action and 6 per task, and the nine hard limits above everything. Prefer this pattern when the UI path is not known in advance and you want the loop to handle verification and failure recovery itself. It requires an OpenAI-compatible vision endpoint.

### Tool reference

Cortex exposes six core MCP tools; the four long-running session tools are documented under [Long-Running Sessions](#long-running-sessions).

**`start_session(dry_run=True, require_approval=True, max_steps=30, max_retries_per_action=1, min_confidence=0.70, allowed_windows=None, allowed_processes=None, limits=None, resume_from_checkpoint=None)`**

Creates a guarded session and returns the session state plus `allowed_processes`, the effective `limits`, and `task_id`. Dry-run and per-action approval are on by default — flip them deliberately. `allowed_windows` is a window-title allowlist, `allowed_processes` a process allowlist (authoritative when the OS reports process identity; fail-closed when it does not; `focus_window` targets are checked against both allowlists before any foregrounding call). `limits` accepts any `Limits` field as a dict; unknown field names or non-numeric values are rejected. No API key is required. The trailing optional `resume_from_checkpoint` resumes a previous long-running session from a checkpoint file as a continuation — see [Resume](#resume).

```json
{
  "dry_run": false,
  "require_approval": false,
  "max_steps": 30,
  "min_confidence": 0.7,
  "allowed_windows": ["Notepad"],
  "allowed_processes": ["notepad.exe"],
  "limits": { "max_actions": 50, "max_task_seconds": 300 }
}
```

**`computer_observe(session_id)`**

Captures the current screen and returns MCP content blocks: one `ImageContent` block carrying the screenshot itself (`image/png`) — so vision-capable client models receive a real image, not text — plus one `TextContent` block with the JSON metadata `{ "observation": ..., "digest": <sha256 of the screenshot payload>, "observation_id": ..., "active_app": ..., "image_format": "image/png" }`. The `observation` object carries dimensions, cursor position, coordinate-space classification, monitor identity (bounds, primary flag, DPI scales), and the full window/process identity; the raw base64 never travels as text. Error paths still return the structured error dict. This explicit tool is not rate-gated — every capture writes an audit row, so hammering clients generate audit volume at their own discretion; the `run_goal` loop is the rate-gated path. `computer_screenshot(session_id)` is a compatibility alias returning the same blocks.

**`computer_execute(session_id, action, x=None, y=None, text=None, keys=None, delta=0, approved=False, expected_effect=None, x2=None, y2=None, target=None)`**

Validates and executes one action through the full pipeline. Supported actions:

| Action | Parameters | Notes |
|---|---|---|
| `click` | `x`, `y` | screenshot coordinates |
| `double_click` | `x`, `y` | screenshot coordinates |
| `drag` | `x`, `y`, `x2`, `y2` | start and end point; both required, both screenshot coordinates; grounded and bounds-checked at both endpoints |
| `move` | `x`, `y` | hover the mouse — repositions the cursor, no click |
| `type` | `text` | up to 2000 chars, typed character-by-character with stop checks between characters |
| `keypress` | `keys` | list of up to 12 key names, e.g. `["ctrl", "s"]` |
| `hotkey` | `keys` | compound shortcut chord of 2-12 key names, e.g. `["ctrl", "s"]`; single-key presses stay on `keypress` |
| `scroll` | `delta` | -20..20 |
| `wait` | `delta` (seconds) | capped at 10 s, sliced at 100 ms so a stop lands immediately |
| `focus_window` | `target` | window title (max 200 chars); brings the matching window to the foreground |
| `done` | — | completion marker |

When either allowlist is configured (`start_session(allowed_processes=…)` / `allowed_windows=…`), a `focus_window` target is resolved and checked against **both** before any foregrounding call — the backend never runs on a disallowed target. Process allowlist: a target whose process is outside the list is refused (`process_not_allowed`), and so is a target that cannot be resolved (`process_identity_unavailable`). Window-title allowlist: a target whose title is outside the list is refused (`window_not_allowed`), and a target that cannot be resolved is refused there too (`window_identity_unavailable`). All four rejections are fail-closed.

Approval semantics: `approved=true` authorizes this one call when the policy requires approval (always for `HIGH` risk; for interactive actions when the session was started with `require_approval=true`). It never clears a `CRITICAL` block. `expected_effect` opts into semantic verification: the stated effect must be observed, or the result reports `failed`/`uncertain` — a stated effect turns "the screen changed somehow" into "the screen changed the way I claimed it would".

Possible outcomes: an executed result (`ok`, `action`, `message`, `verification`, plus `model_confidence`, `grounding_confidence`, `verification_confidence`), a grounding rejection (`ok: false` with `reasons`), a safety denial (`ok: false` with the policy message), or an approval request (`ok: false`, `requires_approval: true`, with the full approval message).

**`run_goal(session_id, goal, approve_next_action=False, auto_subtasks=False)`**

Runs the autonomous closed loop and returns `{ "ok", "approval_budget_remaining", "results", "session_id", "task_id", "termination_reason", "stopped", "requires_approval", "step_count", "metrics" }`. `results` is a list of per-action results (each with the action, message, verification outcome and evidence, and the post-action screenshot); each may carry `suspicious_content` (the model's own report of instruction-like screen text) and `completion_evidence` (`"model_declared"` when the model asserted completion without independent verification evidence). `metrics` is the full snapshot of counters and latency summaries. Approval budget: `approve_next_action=true` grants exactly **one** action per call. The grant binds to the approved action instance, so bounded recovery retries of that same instance do not re-consume it; a new, different action after exhaustion is denied fail-closed and surfaces as `requires_approval: true`. The trailing optional `auto_subtasks=true` switches to the Multi-Subtask mode (see [Long-Running Sessions](#long-running-sessions)); omitting it keeps byte-identical single-goal behavior.

**`stop_session(session_id)`**

The kill switch. Arms the thread-safe `StopToken` — checked before every physical input, at every loop checkpoint, and inside waits — closes the session, and audits `stop` + `emergency_stop`. Every later tool call on that session id fails closed with `session_stopped`. Idempotent: stopping an already-stopped session returns a normal stop result. Nothing a model outputs can reach the stop setter; decisions are data, not control.

### How an agent benefits

**Verifiable state transitions instead of hoping.** A direct call returns the verification outcome and its evidence, not just "no exception". With `expected_effect`, the caller states what should change and the result answers whether it did. `ok: true` means evidence supported the claimed transition — and `ok: false` with `outcome: "uncertain"` means the runtime refuses to claim success it cannot back.

**Staleness protection.** The screen changed since the observation your coordinates came from — another window took focus, a dialog popped up? The action is refused (`STALE_OBSERVATION`) and the pipeline re-observes and re-grounds instead of clicking into whatever is now under those coordinates. Direct calls retry the re-observation once automatically before reporting a rejection.

**Focus before act.** Desktop input lands wherever the foreground is, and a stolen foreground is a real failure class in long web-application tasks (D365/ERP-style flows): a dialog or popup takes focus and the next click or keystroke silently hits the wrong window. `focus_window` brings a named window to the foreground before acting, so focus is established deliberately instead of assumed; `hotkey` covers compound native shortcuts (`ctrl+s` save, `ctrl+z` undo, and their dialog-driven kin) that are awkward or impossible to express as mouse sequences; `move` hovers without clicking, enabling hover affordances and tooltip reveal as deliberate, verifiable steps.

**Safety rails.** Every action is classified contextually before execution:

| Risk | Examples | Policy |
|---|---|---|
| `LOW` | typing plain text into an identified window; clicking an identified application; cursor moves (`move`, no click); scroll; wait | executes under the session's approval policy |
| `MEDIUM` | navigating to a URL; save-as / rename / file moves; state-changing keypresses (`ctrl`, `alt`, `win`, `delete`, `backspace`); compound hotkeys containing those same keys; focusing a window (`focus_window`, category `window_focus_change` — subsequent input could land in an unintended application); interacting with an unidentified target application | approval when the session requires it; window-identity drift demotes actions to at least MEDIUM |
| `HIGH` | installers; killing processes; system settings; elevation; network configuration; pasting credentials | **always** requires approval |
| `CRITICAL` | shell/command execution; registry writes; disk formatting; file deletion; destructive SQL; credential changes; disabling security controls; purchases and money transfers; sending email | **blocked** pending explicit authorization (no MCP tool grants it today — fail safe, and honest about it); a high-risk action with unknown window/process context escalates here |

**Audit evidence.** Every phase of every action — observation, grounding, validation, safety decision, approval, execution, verification, recovery — is appended to a per-session JSONL file with redaction enforced at write time. `run_goal` additionally returns a metrics snapshot: 19 counters (actions, verifications by outcome, safety blocks, approvals, retries, recoveries, model calls, screenshots) and 5 latency summaries (observation, model, execution, verification, task) with count/avg/p50/p95/max.

**Recovery without blind retries.** A moved window, an unexpected dialog, a blocked input, an app crash — each classifies into a named failure class with a bounded plan: re-observe and re-decide with fresh grounding, one bounded Escape-key dismiss for blocked input, or a single same-instance retry with fresh grounding. The same coordinates are never fired twice hoping the second click lands. Authentication prompts terminate the task safely; credentials are never typed.

**Secret redaction.** Ten detection patterns (AWS keys, JWTs, private-key blocks, bearer tokens, basic-auth URLs, password/token assignments, Luhn-validated card numbers) are enforced on the audit sink, the provider payload, and the tool responses. A `type` action whose text resembles a secret, credential, or destructive command is blocked before it reaches the backend.

**Prompt-injection containment.** The model prompt is five labeled channels: `USER INTENT`, `SYSTEM POLICY`, `TASK STATE` (authoritative), `MODEL SUGGESTION` (advisory), and `ENVIRONMENT CONTENT` (untrusted data). Screen-derived text enters only under `ENVIRONMENT CONTENT` — it is data about the screen, never instructions, never authorization. The policy instructs the model to report instruction-like screen content in a `suspicious_content` field instead of complying, and provider output is parsed into a fixed schema where every field is data: nothing in it can change policy, grant approval, or reach the stop token.

### Verification semantics

Every verification produces an outcome in `{verified, failed, uncertain}`:

- **`verified`** — a strategy found affirmative evidence that the intended transition occurred.
- **`failed`** — a strategy found affirmative evidence that it did not (expected text absent, screen pixel-identical when a change was required, window/process state mismatched).
- **`uncertain`** — no strategy could decide from the available data. `uncertain` is **never** success anywhere in the codebase — no code path maps it to `verified`, and this is asserted by a dedicated test. Failed and uncertain verifications route to bounded recovery. The single documented carve-out: a `wait` action continues on `uncertain` with an audited note, because a wait makes no semantic claim about state.

Without a stated expectation, pixels alone stay ambiguous: an identical screen or a sub-threshold change yields `uncertain`, not a free success.

The six strategies in the default chain (first definitive outcome wins):

| Strategy | `verified` means | Without data |
|---|---|---|
| `deterministic_predicate` | a caller-supplied predicate returned `True` over (before, after) | no predicate → `uncertain` |
| `window_state` | the active window title (and/or bounds change) matches the stated expectation | no window identity → `uncertain` |
| `process_state` | the foreground process name (`.exe`-tolerant) and/or pid matches | no process identity → `uncertain` |
| `text_predicate` | the expected text appears in the OCR regions of the after-observation | no OCR → `uncertain` |
| `screenshot_diff` | pixels changed consistently with the stated expectation — mean pixel difference ≥ 1.0, **or** ≥ 50 strongly-changed pixels (per-channel delta ≥ 40), so compact changes such as thin strokes and small controls verify even when the screen-wide mean barely moves | decode failure → `uncertain` |
| `model_visual` | a configured vision judge (the OpenAI-compatible provider) confirmed the transition | no judge → `uncertain` |

The newer actions get deterministic intent defaults: `move` verifies through a deterministic cursor-position predicate — the after-observation's cursor must sit within ±2 px of the requested point on both axes; missing cursor data yields `uncertain`, never success. `focus_window` verifies through a deterministic window-state check against the requested target title (case-insensitive contains), never through pixels. `hotkey` verifies like `keypress`: `visual_change` by default, promoted to `window_state` when the stated effect opens/launches/switches to something ("open Notepad", "switch to Settings", …).

### Worked example

A realistic direct-control session: Notepad is open on the desktop; the agent starts a session, types a line, and stops. Field values are illustrative; field names and shapes are exact. Trailing `…` marks omitted content.

**1. Start a session — dry-run off, approval off, locked to Notepad:**

```json
// start_session
{
  "dry_run": false,
  "require_approval": false,
  "allowed_processes": ["notepad.exe"],
  "limits": { "max_actions": 50 }
}
```

```json
// response
{
  "session_id": "0f4ac21e5d8b4f0a9c3e77b2d1a6f5c8",
  "step_count": 0,
  "max_steps": 30,
  "max_retries_per_action": 1,
  "min_confidence": 0.7,
  "dry_run": false,
  "require_approval": false,
  "stopped": false,
  "allowed_windows": [],
  "pending_approval_token": null,
  "allowed_processes": ["notepad.exe"],
  "limits": "Limits(max_task_seconds=900.0, max_actions=50, max_retries_per_action=1, max_recovery_per_action=2, max_recovery_per_task=6, max_model_calls=60, min_screenshot_interval_ms=250, max_context_items=50, max_sessions=4)",
  "task_id": "9b1d…"
}
```

**2. Observe — check what is on screen and get the grounding identity:**

```json
// computer_observe { "session_id": "0f4a…" }  (response, trimmed — content blocks)
[
  { "type": "text", "text": "{
      \"observation\": {
        \"width\": 1920,
        \"height\": 1080,
        \"active_window\": \"Untitled - Notepad\",
        \"cursor_x\": 960,
        \"cursor_y\": 540,
        \"coordinate_scale_x\": 1.0,
        \"coordinate_space\": \"verified_passthrough\",
        \"observation_id\": \"c4d8e2a1…\",
        \"timestamp\": \"2026-09-05T12:00:00.123456Z\",
        \"monitor\": { \"id\": \"monitor-0\", \"index\": 0, \"bounds\": [0, 0, 1920, 1080], \"is_primary\": true, \"dpi_scale_x\": 1.0, \"dpi_scale_y\": 1.0 },
        \"active_window_info\": {
          \"hwnd\": 197216,
          \"pid\": 8123,
          \"process_name\": \"notepad.exe\",
          \"exe_path\": \"C:\\\\Windows\\\\System32\\\\notepad.exe\",
          \"window_class\": \"Notepad\",
          \"title\": \"Untitled - Notepad\",
          \"bounds\": [4, 4, 1024, 768]
        },
        \"ocr_text\": null,
        \"ui_elements\": null
      },
      \"digest\": \"3f7a…\",
      \"observation_id\": \"c4d8e2a1…\",
      \"active_app\": \"notepad.exe\",
      \"image_format\": \"image/png\"
    }"
  },
  { "type": "image", "data": "iVBORw0KGgoAAAANS…", "mimeType": "image/png" }
]
```

The screenshot arrives as a real `image` content block (visible to vision-capable client models); the text block carries only the grounding metadata — the raw base64 never travels as text. `ocr_text` and `ui_elements` are extension-point fields; without an OCR/UIA integration they stay `null` and the runtime grounds by coordinates.

**3. Type a line and state the expected effect — this is what drives semantic verification:**

```json
// computer_execute
{
  "session_id": "0f4a…",
  "action": "type",
  "text": "Hello from Cortex",
  "expected_effect": "the text 'Hello from Cortex' appears in the editor"
}
```

```json
// response
{
  "ok": true,
  "action": {
    "action": "type",
    "point": null,
    "to_point": null,
    "text": "Hello from Cortex",
    "keys": [],
    "delta": 0,
    "reason": "Explicit MCP action",
    "confidence": 1.0,
    "source_observation_id": null,
    "expected_effect": "the text 'Hello from Cortex' appears in the editor",
    "target": null,
    "action_id": "e5f2…",
    "risk": null,
    "grounding": {
      "strategy": "none",
      "confidence": 1.0,
      "evidence": ["Action type is non-spatial; grounding not required."],
      "normalized": false,
      "notes": "Trivial grounding for a non-spatial action; confidence is the decision's stated value."
    }
  },
  "message": "Executed type.",
  "verification": {
    "outcome": "verified",
    "changed": true,
    "note": "Visual state changed.",
    "confidence": 1.0,
    "evidence": [
      "Mean pixel difference 1.042500 (threshold 1).",
      "Strongly-changed pixels (delta >= 40): 1180 (change threshold 50)."
    ],
    "verification_method": "screenshot_diff",
    "observation_id": "7a11…",
    "verified": true
  },
  "screenshot_after_base64": "iVBORw0KGgoAAAANS…",
  "retry_count": 0,
  "model_confidence": 1.0,
  "grounding_confidence": 1.0,
  "verification_confidence": 1.0
}
```

What happened inside: the typed text was checked against OCR evidence first — none exists without an OCR integration, so the check honestly degraded to `uncertain` — and the documented fallback for direct calls then verified the deterministic visual change. The change was real, so the outcome is `verified` with the pixel evidence attached. Had nothing been typed (a blocked window, a swallowed keystroke), the screen would have stayed identical and the outcome would have been `failed`: "Expected change was not observed." The `risk` field on the action record stays `null`; the enforced risk decision (`low` — plain text into an identified target) lives in the safety audit event.

**4. Stop the session:**

```json
// stop_session { "session_id": "0f4a…" }
{
  "ok": true,
  "session_id": "0f4a…",
  "message": "Session stopped before the next action.",
  "task_id": "9b1d…",
  "termination_reason": null
}
```

From this point every tool call on that session id returns `{ "ok": false, "error": "session_stopped" }`. The session's full audit trail is on disk at `%TEMP%\cortex\logs\<session_id>\audit_<session_id>.jsonl`.

The same session works in autonomous mode: skip step 3 and call `run_goal` with `{"session_id": "0f4a…", "goal": "Type 'Hello from Cortex' into the Notepad window", "approve_next_action": true}` — the vision model proposes the actions, and the same grounding, validation, risk, approval, verification, and recovery machinery wraps each one.

## Long-Running Sessions

Some goals are too big for one `run_goal` call. Long-Running Sessions add an orchestration layer **above** the closed-loop executor: a goal is decomposed into subtasks, and the subtasks execute sequentially — each through the exact same observe → ground → validate → risk → approve → execute → verify → recover pipeline as before. The orchestrator decides *when* each subtask runs; it never performs an action itself, and there is no second execution loop. Session state (subtasks, dependency graph, shared resource counters, bounded context, checkpoints) persists server-side between MCP calls, so no MCP connection needs to stay open for hours — every tool call performs one bounded unit of work and returns.

Use it when the goal is naturally multi-stage (a report assembled from several sources, a multi-application workflow) or may legitimately run for hours. Skip it when one `run_goal` call is enough — the default behavior is unchanged.

### Subtasks

A subtask is a standalone entity: `subtask_id` (stable, serializable, at most 128 characters — preserved verbatim by checkpoint round-trips so dependency references never dangle), `description` (at most 2000 characters), `status`, `depends_on`, `created_at`, `started_at`, `completed_at`, bounded `results` (at most 20 per subtask, heavy screenshot payloads stripped before storage), and failure/recovery info (`failure_class`, `error`, `recovery_attempts`, `last_known_state`).

Statuses: `pending`, `running`, `completed`, `failed`, `blocked`, `paused` — moved along a fixed transition table (terminal states move nowhere). Hard ceiling: 50 subtasks per session.

Subtasks come from two sources: an LLM plan (`run_goal(auto_subtasks=True)`) or manual creation (`create_subtask`, which needs no planner key). An LLM plan is treated as an untrusted suggestion: a deterministic validator rejects the whole plan on any violation (duplicate ids, unknown/self/cyclic dependencies, oversize plans, invalid or non-`pending` statuses, unsafe content) before anything is created, and creation re-checks every rule again.

### Dependencies

Execution is strictly sequential. A subtask starts only when **all** of its dependencies are `completed` (the ready set is computed deterministically, in creation order). When a subtask fails, its transitive dependents are moved to `blocked` automatically — a failed prerequisite can never complete, so that branch is permanently unrunnable. The runtime may consult a bounded replan (at most 3 attempts per session) for replacement work; replacement entries never depend on dead ids. When only dead work remains and replanning cannot replace it, the session ends `unrecoverable`.

### Progress

`get_session_progress` returns a structured, bounded report: session status, the progress percentage, the current subtask, per-status counts, elapsed time, shared resource counters and limits, approval-epoch state, checkpoint status, and the replan budget. The percentage is **deterministic** — computed as completed/total from subtask-manager state (rounded to two decimals), never invented by a model.

### Checkpoints

Long-running state persists to disk so a session can be stopped and resumed:

- **Periodic cadence**: a checkpoint is written every 50 steps or every 30 minutes, whichever comes first.
- **Lifecycle triggers**: subtask completed, subtask failed, transition into a new subtask, before session end (including `stop_session`), and before-resume transitions.
- **Atomic**: the payload is serialized fully, written to a temp file, fsynced, then moved into place with `os.replace` — a reader never sees a partial file, and a failed write leaves the previous checkpoint intact.
- **Redacted**: every string value passes the redaction engine; if any value still looks secret-like after redaction, the write is **refused** (fail-closed). Checkpoints store no secrets and no screenshots.
- **Versioned**: `schema_version` gates loading; unknown/newer versions are rejected — never loaded best-effort. A corrupt checkpoint is rejected with a typed error; it is never deleted, repaired, or partially loaded.
- **Sealed**: every checkpoint carries an HMAC-SHA256 integrity seal over the tamper-sensitive state (session identity, goal, current subtask, budget counters, limits, subtask states), keyed by a per-installation random key file (`.integrity_key`) under the checkpoint directory. Load verifies the seal before anything restores: tampered or unsigned checkpoints are refused (`invalid_checkpoint`), and a missing or replaced key refuses the resume — the load path never recreates the key. Checkpoints written before the seal existed (the pre-fix format) are refused by design, fail-closed.
- **Bounded, single file per session**: one `checkpoint.json` under the checkpoint directory (`COMPUTER_USE_MCP_CHECKPOINT_DIR`, default `%TEMP%\computer-use-mcp\checkpoints`), capped at 8 MB serialized.

A checkpoint carries the session state, the goal, the subtask states and dependency graph, the current subtask, bounded necessary results, resource counters and limits, the context summary and recent history, termination state, the schema version, and a UTC timestamp.

### Resume

`start_session(resume_from_checkpoint="<path to checkpoint.json>")` resumes a previous session as a **continuation**, not a new session:

- The checkpoint is loaded and fully validated (schema/version, structure, integrity seal); checkpoints without a seal (the pre-fix format) are refused by design, fail-closed; any defect is a typed refusal (`invalid_checkpoint` / `resume_refused`) — never a partial restore.
- Counters are **restored, never reset**: actions / model calls / steps / subtasks continue from the checkpoint values, and the elapsed-time anchor is re-based so wall-clock gaps cannot refill the duration budget.
- Limits are the checkpoint's own (re-clamped) limits — a resume can never enlarge the budget.
- Subtasks, the dependency graph, and the bounded context are restored under their stable ids.
- The **current environment is re-verified** against the checkpoint's recorded expectations (foreground process and window, allowlists) before continuing; a mismatch — or missing current environment data — is a fail-closed refusal, and the freshly created session is discarded.
- Approval is **fresh**: an approval epoch is never resurrected from checkpoint data.

The start response carries `resumed: true` and `continuation_of` (the original session id).

### Session resource limits

The same `limits` dict on `start_session` carries the long-running fields (validated and clamped fail-closed, like the nine per-task limits):

| Limit | Default | Clamp range | Meaning |
|---|---|---|---|
| `max_session_seconds` | 14400 (4 h) | 1..86400 | total wall-clock session duration (24 h max configurable) |
| `max_session_actions` | 2000 | 1..2000 | interactive actions across all subtasks |
| `max_session_model_calls` | 500 | 1..500 | model calls across all subtasks |
| `max_session_steps` | 500 | 1..500 | loop steps across all subtasks |
| `max_subtasks` | 50 | 1..50 | subtask ceiling |
| `context_summarize_every` | 25 | 1..500 | steps between context compressions |
| `approval_epoch_seconds` | 1800 (30 min) | 60..86400 | wall-clock half of the approval epoch |
| `approval_epoch_actions` | 50 | 1..1000 | interactive-action half of the approval epoch |
| `health_check_interval` | 600 (10 min) | 60..3600 | minimum spacing between boundary health checks |

These are **shared session budgets**: every subtask also gets its own fresh per-task limit scope, but everything it consumes is mirrored onto the session counters, which only ever grow. On resume the counters are restored from the checkpoint — never zeroed.

### New MCP tools

Long-running sessions add four tools (the six existing tools are unchanged):

**`create_subtask(session_id, description, depends_on=None)`**

Creates one manual subtask; fail-closed on invalid graphs (unknown dependency, self-dependency, cycle, over the 50-subtask ceiling) and on a non-iterable or string `depends_on` (typed `invalid_subtask` error at the tool boundary — never an escaping `TypeError`). Works without any planner/LLM key. Returns `{ok, session_id, subtask: {subtask_id, description, status, depends_on, created_at}, total_subtasks}`.

**`list_subtasks(session_id)`**

All subtasks with statuses as a structured, bounded response: `{ok, session_id, total, counts: {<status>: n}, subtasks: [...]}`. Each summary carries bounded scalar fields plus result/recovery **counts** — never result payloads, never history.

**`run_subtask(session_id, subtask_id, approve_next_action=False)`**

Executes exactly ONE ready subtask through the existing closed-loop executor — one bounded subtask per call; state and checkpoints persist server-side between calls. Only `pending` or `paused` subtasks can run; unmet dependencies fail with `subtask_not_ready` and the list of unmet dependencies. `approve_next_action=true` authorizes at most one interactive action in this call (the `run_goal` budget semantics). Returns `{ok, session_id, subtask_id, status, termination_reason, requires_approval, stopped, results, executed_subtasks, approval_budget_remaining, detail, progress, metrics}`. A subtask paused awaiting a fresh approval epoch resumes through this tool.

**`get_session_progress(session_id)`**

The deterministic progress report described above.

Two existing tools gained trailing optional parameters: `run_goal(..., auto_subtasks=False)` and `start_session(..., resume_from_checkpoint=None)`.

### Example: a long-running session end to end

**1. Start a session with a long-running budget** (a vision API key is needed only for planning, summarization, and model decisions):

```json
// start_session
{
  "dry_run": false,
  "require_approval": false,
  "allowed_processes": ["notepad.exe", "explorer.exe"],
  "limits": { "max_session_seconds": 7200, "max_session_steps": 300, "max_subtasks": 10 }
}
```

**2. Decompose and run the goal** — `auto_subtasks=true` plans (LLM proposal → deterministic validation) and then executes ready subtasks sequentially, all inside this one call:

```json
// run_goal
{ "session_id": "0f4a…", "goal": "Prepare the monthly report from the data on screen and file it", "auto_subtasks": true }
```

The response keeps the `run_goal` shape and adds `planned_subtasks`, `executed_subtasks`, `replan_attempts`, `detail`, and `progress`. When the approval epoch (30 min / 50 interactive actions by default) expires or a health check stops the run, `requires_approval: true` or `termination_reason: "blocked_safety"` with `detail` explains why — and a lifecycle checkpoint has already been written.

**3. Check progress at any time:**

```json
// get_session_progress { "session_id": "0f4a…" }  (response, trimmed)
{
  "ok": true,
  "status": "idle",
  "total_subtasks": 5,
  "completed_subtasks": 2,
  "progress_percent": 40.0,
  "counts": { "pending": 1, "running": 0, "completed": 2, "failed": 0, "blocked": 1, "paused": 1 },
  "resource_counters": { "actions": 87, "model_calls": 40, "steps": 121, "subtasks": 3 },
  "checkpoint": { "has_checkpoint": true, "last_trigger": "subtask_completed", "last_checkpoint_at": "2026-09-06T10:14:00.000000Z" }
}
```

**4. Stop** — a `before_session_end` checkpoint is written before the kill path closes the session:

```json
// stop_session { "session_id": "0f4a…" }
```

**5. Resume later as a continuation** (counters and subtask states return exactly as they were; the current environment must still match the checkpoint's expectations):

```json
// start_session
{ "resume_from_checkpoint": "C:\\…\\checkpoints\\0f4a…\\checkpoint.json" }
// → { "…", "resumed": true, "continuation_of": "0f4a…" }
```

Then finish the remaining work one subtask at a time (`list_subtasks` first, then `run_subtask` — a paused subtask resumes with `approve_next_action: true`):

```json
// run_subtask
{ "session_id": "<new session id>", "subtask_id": "<id from list_subtasks>", "approve_next_action": true }
```

### Backward compatibility

- The six existing tools (`start_session`, `stop_session`, `computer_screenshot`, `computer_observe`, `computer_execute`, `run_goal`) keep their names, parameter positions, and response shapes; every long-running addition is a trailing optional parameter or an additive response field.
- `run_goal` without `auto_subtasks` runs the exact same single-goal loop as before — byte-identical default behavior; subtask mode is strictly opt-in.
- No existing limit was removed or weakened; the session-level limits above are additive and clamped like the rest. A subtask can never reset or enlarge a session counter.

## Testing

```bash
python -m pytest tests/ -q          # standard suite (E2E desktop tests auto-skip)
python -m ruff check src tests benchmarks

# Real-Windows E2E — drives live Notepad, Calculator, and Edge:
# PowerShell:  $env:CUMCP_RUN_E2E = "1"; python -m pytest tests/e2e/ -q
CUMCP_RUN_E2E=1 pytest tests/e2e/
```

Measured at HEAD on the development machine (Windows Server 2022, Python 3.12): **857 passed, 7 skipped** for the standard suite, and ruff reports **all checks passed**. The 7 skips are the gated real-Windows E2E desktop tests. `tests/e2e/` collects 10 tests: the 7 gated desktop tests (window identity, semantic typing verification, moved-window recovery, staleness on window switch, grounded Calculator clicks, display-value verification, browser window-state verification) plus 3 benchmark-harness tests that run unconditionally in the standard suite. The desktop tests use deterministic scripted providers — no vision model, no network — and cross-check runtime assertions against real Win32 state so the runtime cannot self-certify.

## Benchmarks

`benchmarks/` contains measurement scaffolding only: 9 task definitions in an OSWorld-2.0-aligned task format (`benchmarks/tasks/*.yaml`) and a runner (`python -m benchmarks.runner`) with `--mode fake` (default; an in-memory desktop, no GUI needed) and `--mode env` (real applications on the local machine). **No scores are published.** Every harness artifact carries the disclaimer "Harness validation output — NOT benchmark scores.", and no benchmark number should be inferred from anything in the repository until a measured run with a real vision provider exists.

## Safety model

The threat model assumes three things: the model controls a desktop and is treated as a fallible, potentially manipulable proposer — never an authority; screen content is untrusted and can never grant permission; the vision provider sits outside the trust boundary and receives redacted payloads with no capabilities. On top of that: fail-closed defaults everywhere (unknown risk escalates to `CRITICAL`, unverifiable coordinate spaces refuse input, `uncertain` verification routes to recovery, malformed provider output is bounded recovery instead of a crash), bounded autonomy (approval budgets, recovery budgets, nine hard limits), and a redaction-enforced audit trail of every phase.

Full details — risk taxonomy with all pattern categories, fail-closed rules table, approval semantics, kill-path discipline, and the honest residual-risk list: **[docs/SAFETY.md](docs/SAFETY.md)**.

## Architecture

The module map with verified import graph, the pipeline-to-module mapping, the pinned data contracts (`Observation`, `GroundedAction` lineage, `VerificationResult`), the coordinate-transform invariant, the recovery taxonomy table, the stop-token enforcement story, the limits table, and the audit schema: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## Limitations

- **Windows-first.** The execution backend targets Windows (PyAutoGUI + Win32). Non-Windows machines can import the package and run the test suite against in-memory fakes, but cannot drive a real desktop.
- **OCR and UIA are extension-point stubs.** No OCR engine or UIA integration ships. The text-anchor and accessibility grounding strategies refuse (fail-closed), and `text_predicate` verification degrades to `uncertain` — the pipeline degrades gracefully to coordinate grounding, but text-level grounding and verification need that integration built.
- **No OS-level sandboxing.** Policy, approval, and cancellation are enforced in-process. There is no VM, job object, or AppContainer isolation; if the process itself is compromised, these controls do not contain it, and the only physical backstop is the PyAutoGUI failsafe screen corner.
- **An interactive desktop session is required.** No headless mode; capture and input go through the real desktop.
- **Model-based verification is only as good as the configured provider.** A weak vision judge can produce wrong `verified` verdicts. Deterministic strategies run first in the chain, and a judge's `uncertain` is passed through, never upgraded.
- **Risk classification is pattern + context matching** (English patterns plus a small Arabic term set), not semantic understanding. Novel destructive phrasing in other languages may under-classify; the compensating controls are approval-by-default for interactive actions, allowlists, bounded recovery, and verification.
- **Screenshot secret redaction is partial.** Text-pattern redaction and explicit-region blur only; secrets visible purely as pixels are not detected.
- **Multi-monitor logic is unit-tested with fake monitor sets** (100/125/150% DPI); the reference E2E box is single-monitor, so real multi-monitor behavior is not E2E-verified.
- **The kill switch is cooperative and in-process.** `stop_session` guarantees no further input from this runtime; it is not an OS-level kill switch.

## Credits and references

### Research that informed the engineering design

Cortex does not include or redistribute code from any of these works. They informed the design thinking on verification, safety, and evaluation:

- OpenAI — [Computer-Using Agent](https://openai.com/index/computer-using-agent/)
- OpenAI — [Operator system card](https://openai.com/index/operator-system-card/)
- OSWorld: Benchmarking Computer Agents on Real Operating Systems ([arXiv:2404.07972](https://arxiv.org/abs/2404.07972))
- OSWorld 2.0 ([arXiv:2606.29537](https://arxiv.org/abs/2606.29537))
- ScreenSpot-Pro: GUI Grounding for Professional High-Resolution Computer Use ([arXiv:2504.07981](https://arxiv.org/abs/2504.07981))
- Agent S2: A Compositional Generalist-Specialist Framework for Computer Use Agents ([arXiv:2504.00906](https://arxiv.org/abs/2504.00906))
- UI-TARS: Pioneering Automated GUI Interaction with Native Agents ([arXiv:2501.12326](https://arxiv.org/abs/2501.12326))
- Agent S: An Open Agentic Framework that Uses Computers Like a Human ([arXiv:2410.08164](https://arxiv.org/abs/2410.08164))
- SeeClick: Harnessing GUI Grounding for Advanced Visual GUI Agents ([arXiv:2401.10946](https://arxiv.org/abs/2401.10946))
- CogAgent: A Visual Language Model for GUI Agents ([arXiv:2312.08914](https://arxiv.org/abs/2312.08914))

### Open-source libraries Cortex builds on

- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (FastMCP server API)
- [pydantic](https://docs.pydantic.dev/)
- [Pillow](https://python-pillow.org/)
- [mss](https://github.com/BoboTiG/python-mss)
- [PyAutoGUI](https://pyautogui.readthedocs.io/)
- [httpx](https://www.python-httpx.org/)

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Xenos-ink.
