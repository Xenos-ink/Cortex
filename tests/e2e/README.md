# Real-Windows E2E suite (E7-owned)

Live-desktop end-to-end tests for the computer-use-mcp runtime. They drive REAL
applications (Notepad, classic Calculator, Microsoft Edge on a local page) through the
runtime's own MCP tool surface (`start_session` → `computer_observe` → `run_goal` /
`computer_execute`) with **deterministic scripted providers** — no vision model, no
network. What is under test is the RUNTIME pipeline: observation → grounding →
validation (staleness/allowlists) → risk → approval → execution → re-observe →
semantic verification → bounded recovery.

## Running

```bash
# standard suite (e2e tests are skipped automatically; deterministic, no desktop):
. .venv/Scripts/activate
python -m pytest tests/ -q

# real-Windows E2E (drives live applications; ~75s on this box):
CUMCP_RUN_E2E=1 python -m pytest tests/e2e/ -q
```

`pytest-timeout` is intentionally not installed; instead every e2e test receives a
`deadline` fixture and all wait helpers fail fast past `CUMCP_E2E_TEST_TIMEOUT`
(default 240 s). No dependencies were added.

## Skip gate and marker

- Every desktop test is marked `@pytest.mark.e2e`; the marker is registered in
  `conftest.py` (pyproject is not edited) and the gate skips marked tests unless
  `CUMCP_RUN_E2E=1`.
- Implementation note: the gate uses `item.get_closest_marker("e2e")`, NOT keyword
  membership — pytest adds path components as keywords, so `"e2e" in item.keywords`
  also matches the directory name.
- `test_benchmark_harness.py` lives here but is deliberately NOT e2e-marked: it
  validates the benchmark runner in fake mode (fakes only), so it runs in the standard
  suite.

## Files

| file | purpose |
|---|---|
| `conftest.py` | marker + skip gate, per-test deadline, evidence fixture, session factory |
| `helpers_win32.py` | real-Win32 arrangement/assertion helpers (launch, identity, window text, calc grid, moves) |
| `helpers_runtime.py` | `E2EScriptedProvider` (pinned E4 surface) + injected verification strategies |
| `test_e2e_notepad.py` | identity (P0-G/I), semantic typing (P0-A), moved-window recovery (P0-B), window-switch staleness (P0-H) |
| `test_e2e_calculator.py` | grounded clicks + real display-predicate verification |
| `test_e2e_browser.py` | local page in Edge verified via window state |
| `test_benchmark_harness.py` | benchmark runner validation (fake mode) |
| `scratch/` | runtime-created scratch files (gitignored, cleaned per test) |

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
