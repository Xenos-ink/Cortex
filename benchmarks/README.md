# computer-use-mcp — internal benchmark scaffolding

**Doctrine (Goal.md §26 / master-mission §8): this is a HARNESS only. No performance or
accuracy claims may be derived from anything in this directory until a real vision-model
provider is plugged in and a measured run is performed. Every generated artifact carries
the disclaimer `Harness validation output — NOT benchmark scores.`**

## Layout

```
benchmarks/
  tasks/*.yaml      task definitions (JSON-compatible subset of YAML 1.2)
  runner.py         the harness (loads tasks, runs them through the server tool surface,
                    collects metrics, writes results/<run_id>.json, prints a table)
  appwin.py         real-Windows app lifecycle for --mode env (Notepad / win32calc / Edge)
  fakeworld.py      faithful fake desktop (windows, edit buffers, calculator model,
                    modeled file saves) so the whole harness runs without a GUI
  results/          generated run artifacts (gitignored; one sample run is committed
                    under evidence/e2e/, labeled as harness validation)
```

## Task format

Task files use the **JSON-compatible subset of YAML 1.2** (YAML is a superset of JSON),
parsed with the stdlib `json` module. Rationale: the mission prohibits adding
dependencies and the venv has no PyYAML; JSON-in-`.yaml` keeps the `.yaml` contract with
zero parser risk. Any YAML 1.2 parser can read these files unchanged.

Schema (all fields required unless noted):

| field | type | notes |
|---|---|---|
| `id` | str | unique task id (`tNN-...`) |
| `category` | enum | one of OSWorld-2.0-aligned categories: `long_horizon_state_tracking`, `hidden_state`, `cross_source_reasoning`, `visual_spatial_precision`, `verification`, `safety_compliance` |
| `goal` | str | the goal handed to `run_goal` |
| `fake_runnable` | bool | false = needs a real application (skipped as `requires_env` in `--mode fake`) |
| `setup` | object | `app`: `notepad` \| `notepad_two` \| `calculator` \| `browser`, plus app params (`file_name`, `bounds`, `decoy_file`, `title_marker`, `title_marker_2`) |
| `provider.steps` | list | scripted provider steps (see below) |
| `verification` | object | `{"kind": K, "value": V}` or `{"all_of": [...]}`; kinds: `window_text_contains`, `window_title_contains`, `file_contains`, `calc_display_equals`, `runtime_blocked` |
| `max_actions` | int | action budget |
| `safety_notes` | str | why the task is safe to run |
| `allowed_processes` | list, optional | process allowlist enforced by the runtime (P0-G) |

Provider step types: `type_text`, `click_window_center` (optionally
`from_observation: true` to ground on the observation like a real pre-fault grounding),
`click_button` (label resolved from the live button grid / modeled grid), `click_point`,
`key`, `wait`, `done`; fault hooks `hook_move_window`, `hook_focus_window` run between
their neighbors. `{{page2_url}}` in a `type_text` step is substituted by the harness.

## Hint doctrine (env mode)

In `env` mode the runtime's DEFAULT verification chain applies: steps may use
`verification_hint` of `visual_change` (with a stated `expected_effect`) or
`window_state` (with the title needle as `expected_effect`). `expected_text` and
`predicate` hints require perception strategies that currently exist only in the E2E
suite; the task-level `verification` predicate (evaluated by the runner against real
window text / files / display values) carries the semantic check in every mode.

## Running

```
# fake mode (default; no GUI needed) — this is how the harness is validated:
. .venv/Scripts/activate
python -m benchmarks.runner --mode fake

# env mode (real applications on this box; conservative, harness-owned apps only):
python -m benchmarks.runner --mode env --run-id env-<label>

# options: --tasks <dir> --results <dir> --run-id <id>
```

Exit code is 0 when no runnable task failed. Results JSON contains per-task: grounding
strategies/confidence, per-action verification outcomes, recovery events + failure
classes, safety blocks (expected vs false), actions/task, model calls, and latency
summaries (observation/model/execution/verification/task) from the session metrics.

## Metric definitions

- **grounding outcome** — strategy + confidence recorded on each executed action.
- **action success** — executed actions whose runtime verification was `verified`.
- **verification accuracy** — verified / (verified+failed+uncertain) over executed actions.
- **recovery events** — audited `recovery` events, counted by failure class.
- **task completion** — `termination_reason == "completed"` plus the task-level predicate.
- **safety violation rate** — expected-blocked actions that nevertheless executed.
- **false safety blocks** — benign tasks that produced `safety_block` counters.
- **actions/task** — `step_count` per task. **latency** — session metrics per phase.
- **cost** — model calls (no monetary cost is measurable in harness runs).

## Current tasks (9)

| id | category | fake | env | notes |
|---|---|---|---|---|
| t01-notepad-type-verify | verification | yes | yes | marker typed; window-text predicate |
| t02-notepad-two-round-edit | long_horizon_state_tracking | yes | yes | two edit rounds + saves; file predicate |
| t03-notepad-moved-window-recovery | verification | yes | yes | moved-window fault; recovery exercised |
| t04-calculator-decimal-entry | visual_spatial_precision | yes | yes | precise small-target clicks |
| t05-calculator-keyboard-compute | verification | yes | yes | 7*6=42 via keyboard |
| t06-browser-open-local-page | verification | no | yes | window-state title verification |
| t07-browser-tab-switch-state | hidden_state | no | yes | background-tab state tracking |
| t08-notepad-save-cross-source | cross_source_reasoning | yes | yes | file + title sources |
| t09-notepad-block-destructive-text | safety_compliance | yes | yes | runtime must block; zero executions |
