# Restructure baseline tool (WP-R0)

Stdlib-only measurement tool for the layered restructure (master-mission
Addendum 3). WP-R0 freezes the BEFORE picture; WP-R3 re-runs the same commands
on the restructured tree and diffs the two outputs. NO product code lives here.

## Commands

Run from the repo root with the gate venv (the same python every gate uses):

```
PY=C:/Users/HP/AppData/Local/Temp/orvex-108/gate-venv/Scripts/python.exe
PYTHONPATH=D:/Cortex/Cortex/src PYTHONDONTWRITEBYTECODE=1 \
  "$PY" tools/restructure/baseline.py capture --out .orvex/mission/artifacts/restructure/baseline
PYTHONPATH=D:/Cortex/Cortex/src PYTHONDONTWRITEBYTECODE=1 \
  "$PY" tools/restructure/baseline.py inventory --out .orvex/mission/artifacts/restructure/baseline/symbols.json
```

`capture` stages (run all, or `--only nodeids,counts,...`):
`nodeids counts tools probe audit perf wheel`. `--perf-runs N` (default 2)
controls how many full perf runs land in `perf.json`.

## What each stage writes

| stage | file | content |
|---|---|---|
| nodeids | `nodeids.txt` | sorted full collected node-id list (`pytest tests --collect-only -q`) |
| counts | `counts.json` | pass/skip/fail/errors/warnings from a full gate run (`-W error::RuntimeWarning`, tripwire armed, `CUMCP_RUN_E2E` unset) + exact command |
| tools | `tool_schemas.json` | the raw `tools/list` response line, byte for byte as the server emits it over stdio (handshake identical to `cli.probe_server`), its sha256, parsed result, tool names (must be exactly 10) |
| probe | `probe.txt` | stdout of `python -m computer_use_mcp.cli probe --python <gate> --cwd <repo>` |
| audit | `audit_shapes.json` | per audit event_type: sorted top-level keys + metadata keys |
| perf | `perf.json` | TWO runs, each: server import time (30 fresh child interpreters, median/p95/min), r5 profile (`tools/restructure/r5_profile_mirror.py --iterations 30 --json`, screen READING only — it injects no input), fake-backend harness (`.orvex/mission/artifacts/gates/perf_harness.py --repeats 30`: captures/action + Metrics p50/p95); plus computed run-to-run spread |
| wheel | `wheel_files.txt` | sorted file list of the wheel built into `DIR/wheel` (`uv build --wheel --out-dir`) + console-script entry points from the wheel metadata; build residue in the tree (`build/`, `src/*.egg-info`) is removed by the tool and the removal recorded |

Every artifact also carries machine / python / UTC timestamp / exact command
inside the JSON (or `capture_manifest.json` for the stage-level summary).

## Symbol inventory normalization (exact rules)

`inventory` hashes every top-level `def` / `class` / assignment in every module
under `src/computer_use_mcp/` (recursively; `__init__.py` counts as its package):

1. Only the symbol node's own AST subtree is hashed.
2. Every `Import` / `ImportFrom` node anywhere in the subtree is removed.
3. Docstrings are KEPT (ordinary `Expr(Constant str)`).
4. Line/column info is dropped: `ast.dump(..., include_attributes=False)`.
5. Module-qualified references are normalized: a dotted `Name/Attribute` chain
   that resolves to `computer_use_mcp`, any of its modules/subpackages, or an
   import alias bound to one (`import computer_use_mcp.server as srv`,
   `from computer_use_mcp import server`) is stripped of the module prefix:
   `computer_use_mcp.limits.Limits` and `srv._f` become `Limits` / `_f`. A bare
   module reference keeps the module's last component. Chains that do not
   resolve to a src module are untouched.
6. `sha256` (hex) of the dumped normalized subtree.

Consequences: moving a symbol between modules keeps the hash; import-line-only
edits keep it; any body or docstring edit changes it. Normalizer tests:
`tests/test_restructure_baseline_tool.py`.

## audit_shapes sources (documented per the contract)

- **AST scan (authoritative static source):** every `emit(...)` / `_audit(...)`
  call site under `src/computer_use_mcp/` — all product emissions pass through
  `AuditLogger.emit`, so the call sites are statically enumerable. Literal
  first args give event_type; keywords give top-level keys; `metadata={...}`
  dict literals give metadata keys. Emit sites fed by a loop variable over a
  literal tuple of event-type strings (the server stop/emergency_stop loop)
  are resolved too.
- **Dynamic fake-backend battery:** `AuditLogger.emit` is instrumented at
  runtime while a fake-backend action flow (win+r / type / enter / a failing
  action) runs fully offline. No desktop contact of any kind.

Union per event_type; event types never seen statically or dynamically are
listed with `"unseen_statically_and_dynamically": true` (vocabulary members
from `audit.AuditEventType` whose emission sites are driven only by paths the
offline battery cannot reach — their top-level keys still come from the
`AuditEvent` model, which fixes the top-level shape for every event).

## Design notes for WP-R3

- All artifacts are deterministic content (sorted lists, raw lines, exact
  commands) so a plain diff plus the spread table in the baseline README is the
  comparison input.
- Perf is compared against the run-to-run spread recorded in the baseline
  `README.md`, under the +2% ceiling from Addendum 3.
- The tool must be run with the gate venv python (the `audit` stage enforces
  this because it imports the product to instrument `AuditLogger.emit`).
- **r5 mirror:** `benchmarks/r5_speed_profile.py` has no fake/offline path (it
  is screen-READING-only by design) and its stage E crashes against the
  current 3-tuple `_bound_outbound_image` signature (benchmarks/ is frozen for
  WP-R0). `tools/restructure/r5_profile_mirror.py` replicates its stages and
  order exactly, adapting only that unpack. WP-R3 must use the same mirror.
