# WP-R3 equivalence checker (tools/restructure/equivalence.py)

Purpose: prove the layered restructure — BEFORE tree at `bf21ae0` vs AFTER tree
at `9227c4b` (or HEAD with 9227c4b as ancestor and a WP-R3-only diff) — broke
nothing and slowed nothing (contract WP-R3.json, goal.md Addendum 3). Reuses
the WP-R0 capture tool `baseline.py` by import.

## Inputs (tracked, byte-identical to the gitignored .orvex sources)

| file | normalized sha256 (PINNED_INPUT_SHA256; CRLF→LF before hashing) |
|---|---|
| inputs/move_table.json | dd9452bacac83be1c310720826f88f1a55fc4e6c3282a815763b26b70c937b0a |
| inputs/r2a_symbol_map.json | f30918474f550ca6fb05d87f8de5057e4fded472555c1576ac74adcfe93695ed |
| inputs/r2b_symbol_map.json | ab0b75bd9ec3c258e68942ee28541c9e4220d82802a80f38626528f5b38fa5a0 |

Pins are computed over line-ending-normalized bytes because this repo has
core.autocrlf=true and no .gitattributes: a fresh checkout hands the tool
CRLF text files, and raw-byte pins would never match.

The tool verifies these pins at run time (`verify_tracked_inputs`), and
`tests/test_restructure_equivalence_tool.py::test_tracked_inputs_match_pinned_sha256`
compares the copies to the pinned values (never silently skips).

`testdiff_residue_pins.json` pins the sha256 of every residue entry; any
change fails check 8. Regenerate only with Commander approval.

## Environment (no absolute user paths as defaults)

- Gate interpreter: defaults to `sys.executable`; override with
  `CORTEX_GATE_PYTHON` (use the gate venv python that has the deps).
- Dev tree for check 9: REQUIRED via `--dev-tree PATH` or `CORTEX_DEV_TREE`
  (read-only; only its two hashes are taken).

## Worktree creation (once, BEFORE side; Commander action)

```
git -C /path/to/repo worktree add "C:/Users/HP/AppData/Local/Temp/orvex-108/wt-before" bf21ae0 --detach
```

## Command sequence (run from the AFTER tree root; one per foreground command)

```
GPY="C:/Users/HP/AppData/Local/Temp/orvex-108/gate-venv/Scripts/python.exe"
OUT=.orvex/mission/artifacts/restructure/r3
BEFORE="C:/Users/HP/AppData/Local/Temp/orvex-108/wt-before"
RUN="env -u CUMCP_RUN_E2E PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 $GPY tools/restructure/equivalence.py run --before $BEFORE --after . --out $OUT"

# preflight + check 1 (symbol/statement inventory vs pinned hashes)
$RUN --only inventory
# check 2 (node ids); check 3 (counts; --force re-runs both suites on the
# CURRENT test set — the first run takes ~3.5 min per side)
$RUN --only nodeids
$RUN --only counts --force
# checks 4+5 (tool surface, probe)
$RUN --only tools,probe
# check 6 (audit shapes) and 6b (launch paths: offline wheel + clean venv)
$RUN --only audit,launch
# check 8 (filtered test diff; enforced against testdiff_residue_pins.json)
$RUN --only testdiff
# check 9 (dev tree hashes; --dev-tree REQUIRED)
$RUN --only devtree --dev-tree "D:/Cortex/<dev-tree-path>"

# check 7 (perf, phased). A COMPLETE FRESH RUN starts with a new generation
# (archives perf_series.json -> perf_series.gen<N>.json; never mixes):
$RUN --only perf --perf-phase new-generation
$RUN --only perf --perf-phase import-cold
$RUN --only perf --perf-phase import-warm          # priming + .pyc assertion
$RUN --only perf --perf-phase profile --perf-rounds 5   # repeat to 15 rounds
# contract 2xN rerun of every NOISE metric (per metric group):
$RUN --only perf --perf-phase rerun-import-cold     # only if import_cold is NOISE
$RUN --only perf --perf-phase rerun-import-warm     # only if import_warm is NOISE
$RUN --only perf --perf-phase rerun-profile --perf-rounds 5  # to 15 rounds if mirror/harness NOISE
# verdict recompute from the CURRENT generation only (no re-measurement):
$RUN --only perf,report

# diagnosis of the perf finding (D1..D6, then merge; preflight runs here too)
"$GPY" tools/restructure/equivalence.py diagnose --before "$BEFORE" --after . --out "$OUT" --stage D1 --rounds 10
"$GPY" tools/restructure/equivalence.py diagnose --before "$BEFORE" --after . --out "$OUT" --stage D2-disable --rounds 10
"$GPY" tools/restructure/equivalence.py diagnose --before "$BEFORE" --after . --out "$OUT" --stage D2-freeze --rounds 10
"$GPY" tools/restructure/equivalence.py diagnose --before "$BEFORE" --after . --out "$OUT" --stage D3
"$GPY" tools/restructure/equivalence.py diagnose --before "$BEFORE" --after . --out "$OUT" --stage D4
"$GPY" tools/restructure/equivalence.py diagnose --before "$BEFORE" --after . --out "$OUT" --stage D5 --execs 300
"$GPY" tools/restructure/equivalence.py diagnose --before "$BEFORE" --after . --out "$OUT" --stage D6 --rounds 10   # repeat to 30
"$GPY" tools/restructure/equivalence.py diagnose --before "$BEFORE" --after . --out "$OUT" --stage final
```

## Expected outputs

One JSON per check in `--out` (inventory.json, nodeids.json, counts.json,
tools.json, probe.json, audit.json, launch.json, testdiff.json, devtree.json,
perf.json), plus `summary.json` and `report.md` (with `## Diagnosis` once a
diagnosis ran; regenerated reports preserve it). Perf series carry a
generation id; `perf_series.gen<N>.json` archives older generations and no
verdict ever mixes them.

## Reading summary.json

- `checks.<name>.result` — PASS / FAIL / NOISE / NOISE-UNRESOLVED /
  REGRESSION / NOT_RUN / DEFERRED.
- `overall` is the RANKED verdict (STOP > INCOMPLETE > NOISE > PASS; unknown
  verdicts are STOP). NOISE-UNRESOLVED metrics are listed in
  `summary.noise_unresolved` and in report.md with ratio + CI for an explicit
  Commander ruling (the contract check-7 rule is quoted in `perf_verdict`).

## Safety notes

- The BEFORE worktree is never written (PYTHONDONTWRITEBYTECODE=1, no cache
  provider; the only warm-phase writes go to a per-side PYTHONPYCACHEPREFIX
  under the out dir).
- The perf mirror reads the real desktop (mss/foreground/UIA) — READ only;
  Commander waiver B1; statically guarded by `test_mirror_reads_no_input`
  (which also catches `type*` calls via `mirror_forbidden_calls`).
- `uv build --offline` (n3) needs the build dependencies already in the local
  uv/build cache — they have been cached since WP-R0.
