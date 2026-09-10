# Real-Windows E2E suite (E7-owned; D6/R-8 gating & isolation)

Live-desktop end-to-end tests for the computer-use-mcp runtime. They drive REAL
applications (Notepad, classic Calculator, Microsoft Edge on a local page) through the
runtime's own MCP tool surface (`start_session` → `computer_observe` →
`computer_execute`) — no vision model, no network. RETARGETED (run_goal removal): the
internal decide/recovery loop is gone; the host drives every action as a direct call.
What is under test is the RUNTIME pipeline: observation → grounding →
validation (staleness/allowlists) → risk → approval → execution → re-observe →
semantic verification (the verification outcome — verified or failed — is returned to
the host; re-driving is the host's job).

## Real-input gating (D6 desktop-safety gate) — READ BEFORE RUNNING

**These tests type and click on the REAL desktop of whoever runs them.** A plain
`python -m pytest tests` never executes them: the gate is FAIL-CLOSED and they are
SKIPPED BY DEFAULT, with a loud skip reason printed under `-rs`:

```
D6 desktop-safety gate: this e2e test drives REAL keyboard/mouse input on the LIVE
desktop ... opt in per-run with: CUMCP_RUN_E2E=1 python -m pytest tests/e2e ...
```

- The ONE opt-in: exact variable `CUMCP_RUN_E2E` with exact value `1` (nothing else
  — not `true`/`yes`/`on`/`2`, not any `E2E_*`/`REAL*`/`DESKTOP*` variable — ever
  enables the gate; this is pinned in `tests/test_r8_pins.py`).
- WARNING when opting in: you are on the hook for the desktop. Close your own work
  first; opt in ONLY on a machine/desktop you control. The tests move real windows
  and type real text (searchable markers, see below).
- Mechanism: `pytest_collection_modifyitems` in `tests/e2e/conftest.py` adds a
  `pytest.mark.skip` to every `e2e`-marked item unless
  `e2e_real_input_enabled()` (pure predicate, pinned) returns True. The gate tests
  the marker itself (`get_closest_marker("e2e")`), NOT keyword membership (pytest
  adds path parts as keywords, so `"e2e" in item.keywords` would also match the
  directory name and over-skip/gate wrongly).
- `test_benchmark_harness.py` lives here but is deliberately NOT e2e-marked: it
  validates the benchmark runner in fake mode (fakes only), so it runs in the
  standard suite.

## Running

```bash
# standard suite (e2e tests are skipped automatically; deterministic, no desktop):
. .venv/Scripts/activate
python -m pytest tests/ -q

# real-Windows E2E (drives live applications; ~75s on this box):
CUMCP_RUN_E2E=1 python -m pytest tests/e2e/ -q
# PowerShell:  $env:CUMCP_RUN_E2E = "1"; python -m pytest tests/e2e/ -q
```

`pytest-timeout` is intentionally not installed; instead every e2e test receives a
`deadline` fixture and all wait helpers fail fast past `CUMCP_E2E_TEST_TIMEOUT`
(default 240 s). No dependencies were added.

## Skip gate and marker

- Every desktop test is marked `@pytest.mark.e2e`; the marker is registered in
  `conftest.py` (pyproject is not edited) and the gate skips marked tests unless
  `CUMCP_RUN_E2E=1` (see "Real-input gating" above for the full fail-closed
  doctrine).
- `test_benchmark_harness.py` lives here but is deliberately NOT e2e-marked: it
  validates the benchmark runner in fake mode (fakes only), so it runs in the
  standard suite.

## Unique-window isolation (D6 wrong-window defense)

The suite NEVER attaches to, focuses, types into, or closes a window it did not
itself launch:

- **Notepad** — every instance is launched with a scratch file whose name embeds a
  run-unique token (`w32.unique_window_token()` → `cumcp-e2e-<pid>-<n>-<ts>`), so
  the window TITLE carries the token. Attach goes through
  `w32.attach_window_by_unique_title`, which enumerates ONLY marker-carrying
  windows: the user's own open Notepad (same class, same exe, different title) is
  INVISIBLE to the attach path. Zero matches → loud `TimeoutError` (never a
  fallback attach); >1 match → loud `RuntimeError` (never a guess). At launch time
  the helper asserts the found hwnd belongs to OUR pid AND carries the token.
- **Calculator** — the classic `CalcFrame` title cannot be set, so attach is
  PID-scoped: the wait matches `CalcFrame` AND the pid of the process WE started;
  the user's own Calculator (different pid) can never match. Identity assertions
  in the tests pin `hwnd`/`pid` to the launched process.
- **Edge/browser** — the local page `<title>` embeds the run-unique token and the
  wait goes through the marker-only path (mandatory anyway: with an Edge singleton
  running, the launcher hands off and exits, so PID-scoping is impossible).
- **Teardown** closes exactly what was opened: `kill_process_tree(proc.pid)` (the
  process we started) for Notepad/Calculator; for Edge, `close_window(hwnd)` is
  exact-hwnd (posts WM_CLOSE to that hwnd and polls only that hwnd's liveness —
  never a class/title re-search that could touch the user's window), then kills the
  launcher tree if still alive.
- All of this is pinned WITHOUT real input in `tests/test_r8_pins.py` (stubbed
  window enumeration + stubbed PostMessageW): marker-less user windows are ignored
  by attach, ambiguous markers raise, teardown targets exactly the own hwnd/pid.

### Searchable markers (if a past run polluted your open apps)

If a previous (pre-D6-hardening) run typed into windows you had open, search your
open documents for these strings and delete the stray text:

- Notepad typing: `e2e-typed-7391 quick brown fox`
- Notepad moved-window recovery: `moved-window-recovered-7391`
- Notepad stale-observation: `stale-reject-marker-7391`
- Browser page title (a tab title, harmless, close the tab): `E2E Browser Verification Page`
  (post-hardening titles carry a run-unique `cumcp-e2e-...` token after this prefix)
- Calculator: no typed markers (click-only; any stray Calculator window is a
  `CalcFrame` that can be closed safely).

## Files

| file | purpose |
|---|---|
| `conftest.py` | marker + D6 fail-closed skip gate (`e2e_real_input_enabled`), per-test deadline, evidence fixture, session factory |
| `helpers_win32.py` | real-Win32 arrangement/assertion helpers (launch, identity, unique-marker attach, window text, calc grid, moves) |
| `helpers_runtime.py` | `E2EScriptedProvider` (pinned E4 surface) + injected verification strategies |
| `test_e2e_notepad.py` | identity (P0-G/I), semantic typing (P0-A), moved-window verification failure returned to the host (P0-B), foreground-switch staleness defense (P0-H companion) |
| `test_e2e_calculator.py` | grounded clicks + real display-predicate verification |
| `test_e2e_browser.py` | local page in Edge verified via window state |
| `test_benchmark_harness.py` | benchmark runner validation (fake mode) |
| `scratch/` | runtime-created scratch files (gitignored, cleaned per test) |

(Desktop-safety pins without real input live in `tests/test_r8_pins.py`, outside
this directory, so they run in every plain suite run.)

## Verification approach (honest, evidence-based)

- The runtime's default strategy chain alone cannot semantically verify several real
  desktop transitions, so the suite INJECTS application-specific deterministic
  strategies (the documented `VerificationStrategy` protocol) into the session's
  `VerificationEngine` via the `_get_bundle` seam:
  - `WindowTextPredicateStrategy` — reads the real Edit control (WM_GETTEXT) for
    `expected_text` intents (Notepad typing);
  - `CalcDisplayPredicateStrategy` — reads the real Calculator display Static for
    `calc_display_equals:<value>` effects (claimed for `predicate` AND `visual_change`
    intents carrying the marker).
  Both read REAL window state and decide verified/failed/uncertain from it; nothing is
  fabricated, and `uncertain` is never upgraded.
- **Measured environment finding:** a single digit change on the 1920x1080 screenshot
  is a mean pixel difference of ~0.2 — far below the legacy diff threshold (1.0) — so
  pixel diff alone cannot verify Calculator input. This is exactly the class of gap the
  strategy framework exists for; the E2E evidence records it.
- Every test ALSO asserts independently of the runtime (Win32 display reads, edit-control
  text, file contents) so a runtime bug cannot self-certify.

## Environment findings (documented, not worked around silently)

1. `calc.exe` on Server 2022 is a stub that exits immediately and re-launches the real
   `win32calc.exe` (`CalcFrame`) in a different process. The suite launches
   `win32calc.exe` directly so the tracked PID owns the window.
2. `LocalComputerBackend`'s DPI fallback ladder DOWNGRADES a pre-set per-monitor-v2
   process to "system" awareness (its `SetProcessDPIAware()` fallback "succeeds").
   Consequence: importing the helpers must have NO global DPI side effect (E6 finding
   D10) — the awareness call is lazy (`ensure_dpi_awareness`), invoked by an autouse
   conftest fixture for e2e-marked tests only and by rect-sensitive entry points. On
   this box the downgrade is otherwise harmless at runtime (single monitor, system DPI
   == monitor DPI ⇒ observations still classify `verified_passthrough`), but the
   standard suite's `dpi_estimated is False` assertion requires the backend to be the
   process's first awareness setter.
3. Window-bounds-only moves do NOT trip `STALE_OBSERVATION` (identity checks are
   hwnd/pid/process/monitor/dimensions/coordinate-space). The moved-window test shows
   the semantic verification layer catching the miss (WRONG_WINDOW → bounded recovery),
   while the window-switch test exercises the true staleness rejection.
4. Foreground can be stolen by the hosting console between launch and observation; tests
   focus the target window explicitly before asserting foreground-derived state.

## Evidence

Every e2e test writes `evidence/e2e/<test_name>/`: `observation_before/after.json`
(window identity incl. HWND/PID/exe), downscaled `screenshot_before/after.png`,
`audit_excerpt.jsonl` (structured audit events), `transcript.md`, `result.json`
(pytest outcome + recorded assertions), and appends a line to `evidence/e2e/runs.md`.
The committed artifacts are from the final green run (run id `20260905-205303`).
