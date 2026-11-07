#!/usr/bin/env python
"""WP-R3 equivalence checker (stdlib only; reuses tools/restructure/baseline.py).

Proves the restructure: BEFORE = a detached worktree at bf21ae0 (read-only),
AFTER = this tree at 9227c4b. Every check is run with the CURRENT baseline.py
(imported, never copied); the BEFORE tree is only ever read or run with
PYTHONDONTWRITEBYTECODE=1 / -p no:cacheprovider so nothing is written into it.

  python tools/restructure/equivalence.py run --before DIR --after DIR --out DIR
      [--only inventory,nodeids,counts,tools,probe,audit,launch,testdiff,devtree,perf,report]

Internal subcommands (spawns of this tool, one tree per interpreter):
  _stage-audit --tree DIR --out DIR   baseline.stage_audit against one tree
                                      (BEFORE: flat->layered sys.modules aliases)
  _fake-harness --repeats N           offline fake-backend captures/action +
                                      Metrics p50/p95 (flat OR layered imports)

Checks (each PASS/FAIL with evidence JSON in --out):
  1 inventory   symbol + top-level statement inventory, both trees, diffed with
                baseline.cmd_diff under a merged symbol map (r1 move table +
                r2a/r2b symbol maps). The ONLY allowed differences are the
                reviewed list: D2 logger statement, D1 server/__init__ shim,
                D8 win32 else: placeholders, the 8+2 if TYPE_CHECKING: blocks,
                empty package __init__ files (no diff entries). Anything else
                FAILS. Statement order within each destination module is
                checked by baseline.cmd_diff (order violations must be 0).
  2 nodeids     pytest --collect-only on both trees; before ids mapped through
                the git renames (git diff -M bf21ae0..HEAD --name-status);
                removed must be 0 outside renamed files (every one listed);
                added must live only in new guard/test files (listed per file).
  3 counts      full suite both trees, -W error::RuntimeWarning, tripwire
                armed (CUMCP_RUN_E2E unset): before-passing maps to
                after-passing; skip sets identical in identity.
  4 tools       baseline.stage_tools on both trees; raw tools/list sha256
                byte-identical and equal to the WP-R0 constant.
  5 probe       baseline.stage_probe on both trees: PROBE PASS both.
  6 audit       baseline.stage_audit on both trees; identical event-type set
                and identical per-type top-level/metadata key shapes.
  7 perf        INTERLEAVED A/B (before, after, before, after, ...): (a) fresh
                interpreter import time N>=40/side, cold and warm (per-side
                PYTHONPYCACHEPREFIX inside tempfile); (b) each tree's own
                r5_profile_mirror (path-bound: flat vs layered module names;
                identical stages; both carry the stage-E 3-tuple fix) and the
                fake-backend harness (captures/action + Metrics p50/p95),
                N>=15 rounds/side. Per metric: medians, AFTER/BEFORE ratio,
                bootstrap 95% CI (fixed seed, 2000 resamples). Verdict rule:
                PASS if CI hi <= 1.02; NOISE if median ratio <= 1.02 but CI hi
                > 1.02 (auto re-run once at 2xN, both passes reported);
                REGRESSION if CI lo > 1.02 -> STOP, nothing is tuned.
  8 testdiff    git diff -M bf21ae0..HEAD -- tests benchmarks; after removing
                import lines and module-path retargets, the residue must be
                confined to the reviewed allow-list (new guard/test files, the
                D3/D7 same-object lines, the 49 fixture yield/return-None
                sites, the source-text-test rewrites, the step-0 guard
                changes); residue is printed grouped by file.
  9 devtree     dev tree git status/diff sha256 unchanged (998f5816... /
                c495e5e8...).

On any FAIL or REGRESSION the tool reports STOP and changes nothing.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import json
import os
import random
import re
import statistics
import shutil
import subprocess
import sys
import tempfile
import time
import types
import venv
import zipfile
from collections import Counter
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
REPO = TOOL_DIR.parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import baseline  # tools/restructure/baseline.py (WP-R0 tool; reused, not copied)

# R: no absolute user paths baked in as defaults — the gate python defaults to
# sys.executable (override via CORTEX_GATE_PYTHON); the dev tree is a REQUIRED
# --dev-tree argument for check 9 (or CORTEX_DEV_TREE)
GATE_PY = Path(os.environ.get("CORTEX_GATE_PYTHON", sys.executable))
DEV_TREE = Path(os.environ["CORTEX_DEV_TREE"]) if os.environ.get("CORTEX_DEV_TREE") else None
# R: pinned input sha256s over LINE-ENDING-NORMALIZED bytes (inputs/ are
# tracked byte-identical copies; normalization keeps the pins valid on CRLF
# checkouts — see normalized_text_sha256)
PINNED_INPUT_SHA256 = {
    "inputs/move_table.json":
        "dd9452bacac83be1c310720826f88f1a55fc4e6c3282a815763b26b70c937b0a",
    "inputs/r2a_symbol_map.json":
        "f30918474f550ca6fb05d87f8de5057e4fded472555c1576ac74adcfe93695ed",
    "inputs/r2b_symbol_map.json":
        "ab0b75bd9ec3c258e68942ee28541c9e4220d82802a80f38626528f5b38fa5a0",
}
ART = REPO / ".orvex" / "mission" / "artifacts" / "restructure"

BEFORE_GIT = "bf21ae0"
AFTER_GIT = "9227c4b"
EXPECTED_TOOLS_SHA = "eda8b72457deff6d21660e640638009b8ea42eea91d345ff59a07e6deac6c0c5"
EXPECTED_DEV_STATUS_SHA = "998f5816"
EXPECTED_DEV_DIFF_SHA = "c495e5e8"

# m2: reviewed statement-delta hashes (WP-R2a DELTA-1 + WP-R2b, pinned from the
# reviewed diffs; the derivation must MATCH these, else the check fails)
PINNED_BEFORE_WIN32_IF = ("computer_use_mcp.backend",
                          "fc4bb3c1001f4bb690b8e082bfb4c0a7bea859d79ef32fa5c8e6c4e8321d6497")
PINNED_AFTER_WIN32_IF = ("computer_use_mcp.desktop.win32",
                         "36c01571c09949424102a6ca36ced82ceb79a590d4da6b8f3ed0b263b8c4f3f5")
PINNED_TYPE_CHECKING_HASH = "ba9db845e3758de48e312988ae15286ecd3387a2c8fefe3f894abbba4bd0c6fc"
WP_R3_FILES = ("tools/restructure/equivalence.py", "tools/restructure/baseline.py",
               "tools/restructure/r5_profile_mirror.py",
               "tools/restructure/testdiff_residue_pins.json",
               "tools/restructure/EQUIVALENCE.md",
               "tools/restructure/inputs/move_table.json",
               "tools/restructure/inputs/r2a_symbol_map.json",
               "tools/restructure/inputs/r2b_symbol_map.json",
               "tests/test_restructure_equivalence_tool.py")
# tracked, byte-identical copies of the gitignored .orvex inputs (reproducibility)
MOVE_TABLE = TOOL_DIR / "inputs" / "move_table.json"
SYMBOL_MAPS = (TOOL_DIR / "inputs" / "r2a_symbol_map.json",
               TOOL_DIR / "inputs" / "r2b_symbol_map.json")
TESTDIFF_PINS = TOOL_DIR / "testdiff_residue_pins.json"

CEILING = 1.02
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20261031
IMPORT_N = 40
PROFILE_ROUNDS = 15
MIRROR_ITERATIONS = 10
HARNESS_REPEATS = 8
EXPECTED_TYPE_CHECKING_BLOCKS = 10  # 8 (WP-R2a DELTA-1 F2) + 2 (WP-R2b DELTA-2 F3)

# Reviewed inventory allow-list (r2a DELTA-1, r2b notes §§2/12).
ALLOWED_MISSING = [{"module": "computer_use_mcp.server", "kind": "assign", "name": "logger"}]  # D2
ALLOWED_ADDED = [
    # D1 shim: server/__init__.py records as module "computer_use_mcp.server"
    {"module": "computer_use_mcp.server", "kind": "def", "name": "main"},
    {"module": "computer_use_mcp.server.instance", "kind": "assign", "name": "logger"},  # D2
]
ALLOWED_DUPLICATED = ALLOWED_ADDED
ALLOWED_SYMMAP_MISMATCH = [{"from_module": "computer_use_mcp.server", "name": "logger"}]  # D2
MODULE_DEFAULTS = {
    "computer_use_mcp.backend": "computer_use_mcp.desktop.backend",
    "computer_use_mcp.server": "computer_use_mcp.server.plan_surface",
}
STAY_PUT_MODULES = {"computer_use_mcp", "computer_use_mcp.cli"}

# Reviewed testdiff residue allow-list — every file cross-checked against the
# r1/r2a/r2b notes; TRIMMED per VAL-WP-R3-02 (files with no residue removed:
# test_controller_integration.py, test_p5_redteam.py,
# test_w2_queue_false_failure_repro.py, test_wp7a_plan_boundary.py).
ALLOWED_RESIDUE_FILES = {
    "tests/conftest.py",
    "tests/desktop_tripwire.py",
    "tests/fresh_backend_import.py",
    "tests/test_audit_compliance.py",
    "tests/test_batch7_failure_classification.py",
    "tests/test_batch8_classification_completeness.py",
    "tests/test_focus_guard.py",
    "tests/test_hotkey_guard.py",
    "tests/test_rem_e_live_gaps.py",
    "tests/test_restructure_baseline_tool.py",
    "tests/test_session_idle_timeout.py",
    "tests/test_session_isolation.py",
    "tests/test_w1_cancellable_wait.py",
    "tests/test_w2_evidence_store.py",
    "tests/test_w6_delta1_fixes.py",
    "tests/test_w6_n1_waiter_own_loop.py",
    "tests/test_w6_wait_blocking_segments.py",
    "tests/test_wp5_evidence_lifecycle.py",
    "tests/test_wp7b_plan_surface.py",
    "tests/test_wp_h_hermeticity.py",
    "benchmarks/runner.py",
}
WP_R3_TOOL_TEST_FILES = ("tests/test_restructure_equivalence_tool.py",)
# the restructure TOOLING's own test file: extended by the WP-R1/R2a deltas
# (diff-tool unit tests, recorded in r1/notes.md DELTA-1 and r2a step-1 §7)
RESTRUCTURE_OWNED_TEST_FILES = ("tests/test_restructure_baseline_tool.py",)

FLAT_ALIASES = {
    "computer_use_mcp.pipeline.agent": "computer_use_mcp.agent",
    "computer_use_mcp.security.audit": "computer_use_mcp.audit",
    "computer_use_mcp.security.safety": "computer_use_mcp.safety",
    "computer_use_mcp.desktop.fake_backend": "computer_use_mcp.backend",
    "computer_use_mcp.core.limits": "computer_use_mcp.limits",
    "computer_use_mcp.core.models": "computer_use_mcp.models",
    "computer_use_mcp.core.state": "computer_use_mcp.state",
    "computer_use_mcp.vision.observation": "computer_use_mcp.observation",
}
FAKE_PARENTS = (
    "computer_use_mcp.core", "computer_use_mcp.security", "computer_use_mcp.desktop",
    "computer_use_mcp.vision", "computer_use_mcp.pipeline", "computer_use_mcp.planning",
)


# ---------------------------------------------------------------------------
# small utils
# ---------------------------------------------------------------------------

def _base_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.pop("CUMCP_RUN_E2E", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra:
        env.update(extra)
    return env


def tree_env(tree: Path, extra: dict | None = None) -> dict:
    return _base_env({"PYTHONPATH": str(Path(tree) / "src"), **(extra or {})})


def _run(cmd: list[str], cwd: Path, env: dict, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return _run(["git"] + args, cwd=cwd, env=_base_env(), timeout=120)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@contextlib.contextmanager
def _tree_globals(tree: Path):
    """Point baseline's REPO/SRC/PKG at one tree (restored on exit). Also
    rebinds run()'s cwd DEFAULT — it was bound at import time, so a patched
    baseline.REPO alone would not move it (checks 2/5 would silently run both
    sides against the AFTER tree)."""
    saved = (baseline.REPO, baseline.SRC, baseline.PKG, baseline.run.__defaults__)
    src = Path(tree) / "src"
    baseline.REPO, baseline.SRC = Path(tree), src
    baseline.PKG = src / "computer_use_mcp"
    baseline.run.__defaults__ = (Path(tree),)
    try:
        yield
    finally:
        baseline.REPO, baseline.SRC, baseline.PKG = saved[:3]
        baseline.run.__defaults__ = saved[3]


# ---------------------------------------------------------------------------
# pure helpers (unit-tested in tests/test_restructure_equivalence_tool.py)
# ---------------------------------------------------------------------------

def parse_name_status(text: str) -> tuple[dict[str, str], set[str], set[str]]:
    """git diff --name-status output -> (renames old->new, added, modified)."""
    renames: dict[str, str] = {}
    added: set[str] = set()
    modified: set[str] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R"):
            renames[parts[1]] = parts[2]
        elif status.startswith("A"):
            added.add(parts[1])
        elif status.startswith("M"):
            modified.add(parts[1])
    return renames, added, modified


def map_node_ids(before_ids: list[str], after_ids: list[str],
                 renames: dict[str, str]) -> dict:
    """Map before node ids through file renames; report removed/added."""

    def remap(node_id: str) -> str:
        head = node_id.split("::", 1)[0]
        new_head = renames.get(head)
        return (new_head + node_id[len(head):]) if new_head else node_id

    after_set = set(after_ids)
    mapped = {i: remap(i) for i in before_ids}
    removed = sorted(i for i, m in mapped.items() if m not in after_set)
    mapped_values = set(mapped.values())
    added = sorted(i for i in after_ids if i not in mapped_values)
    return {"removed": removed, "added": added, "renamed": dict(renames)}


MODPATH_RE = re.compile(r"computer_use_mcp(?:\.\w+)+")


def normalize_modpath(line: str) -> str:
    """Collapse every computer_use_mcp... dotted path to its last component."""
    return MODPATH_RE.sub(lambda m: m.group(0).rsplit(".", 1)[-1], line.rstrip())


def _drop_imports(lines: list[str]) -> list[str]:
    out: list[str] = []
    depth = 0
    for ln in lines:
        s = ln.strip()
        if depth > 0:
            depth += s.count("(") - s.count(")")
            continue
        if re.match(r"^import\s", s) or re.match(r"^from\s[\w.]+\s+import\b", s):
            depth += s.count("(") - s.count(")")
            continue
        out.append(ln)
    return out


FIXTURE_NORMALIZE = {"return server": "return None", "yield server": "yield"}

ALIAS_TOKENS = re.compile(
    r"\b(srv_computer_use_mcp|srv_server_[a-z_]+|srv_core_[a-z_]+|srv_planning_[a-z_]+|"
    r"backend_module|windows_module|input_engine_module|clipboard_module|"
    r"text_readers_module|capture_module|observation_module|engine_module|srv|server|B)"
    r"\b\.?")


def normalize_diff_line(sign: str, content: str) -> str:
    s = content.strip()
    s = FIXTURE_NORMALIZE.get(s, s)
    s = normalize_modpath(s)
    # the r2a/r2b retargets also renamed the imported-module ALIASES
    # (server.* -> srv_server_<module>.*); strip the alias family so a pure
    # retarget pairs. Behaviour changes survive as residue.
    return ALIAS_TOKENS.sub("", s)


# the reviewed D3/D7 same-object patch lines (added-only, per r2a/r2b notes
# §4/§D3/D7): an added line is accepted ONLY if it matches the shape AND
# references one of these reviewed target names (closed set, r2b notes)
RETARGET_PATCH_TARGETS = frozenset({
    "_bundles", "_registry", "_stopped_sessions", "_plan_guard",
    "_plan_terminal_audit_lock", "_WAITS_IN_FLIGHT", "_WAIT_TOMBSTONES",
    "_wait_now", "EXPECTED_NOOP_PATCH_SITES", "_backend_factory",
    "_provider_factory", "_session_scoped", "_get_bundle"})
PATCH_EXTRA_RE = re.compile(r"monkeypatch\.(?:setattr|delenv)")
PATCH_ASSIGN_RE = re.compile(r"^\s*[\w.\[\]']+\s*(\[[^\]]*\]\s*)?=[^=]")


def _is_patch_extra(line: str) -> bool:
    if not any(t in line for t in RETARGET_PATCH_TARGETS):
        return False
    return bool(PATCH_EXTRA_RE.search(line) or PATCH_ASSIGN_RE.match(line))
# added comment lines documenting a reviewed retarget/rewrite decision
REVIEW_MARKER_RE = re.compile(
    r"WP-R\d|WP10|VAL-[A-Z0-9-]*|F\d+\b|C-[345]\b|D\d\b|D-\d|HOME|driven reader|"
    r"retarget|monolith")


def diff_residue(diff_text: str, added_files: set[str] = frozenset(),
                 whole_file_allowed: set[str] = frozenset()) -> dict:
    """Residue of a unified diff after dropping import lines, module-path and
    alias retargets and the reviewed fixture value-use rewrites. Leftover
    added lines that are blank or reviewed D3/D7 patch extras are allowed
    (counted); any other leftover fails unless its file is on the reviewed
    whole-file allow-list (step-0 guards, source-text rewrites)."""
    per_file: dict[str, dict[str, list[str]]] = {}
    current = None
    for raw in diff_text.splitlines():
        if raw.startswith("+++ b/"):
            current = raw[6:]
            per_file.setdefault(current, {"added": [], "removed": []})
            continue
        if raw.startswith("---") or raw.startswith("diff ") or raw.startswith("index "):
            continue
        if current is None:
            continue
        if raw.startswith("+"):
            per_file[current]["added"].append(raw[1:])
        elif raw.startswith("-"):
            per_file[current]["removed"].append(raw[1:])
    residue: dict[str, dict] = {}
    for path, sides in per_file.items():
        if path in added_files:
            continue  # new guard/test files from the restructure WPs
        rem = Counter(normalize_diff_line("-", ln) for ln in _drop_imports(sides["removed"]))
        add = Counter(normalize_diff_line("+", ln) for ln in _drop_imports(sides["added"]))
        common = rem & add
        leftover_added = add - common
        leftover_removed = rem - common
        allowed_patch: list[str] = []
        allowed_blank = 0
        allowed_comments: list[str] = []
        unexpected: list[str] = []
        for ln, n in leftover_added.items():
            for _ in range(n):
                stripped = ln.strip()
                if not stripped:
                    allowed_blank += 1
                elif stripped.startswith("#") and REVIEW_MARKER_RE.search(ln):
                    allowed_comments.append(ln)
                elif _is_patch_extra(ln):
                    allowed_patch.append(ln)
                else:
                    unexpected.append("+ " + ln)
        unexpected += sorted("- " + ln for ln, n in leftover_removed.items()
                             for _ in range(n))
        if allowed_patch or allowed_blank or allowed_comments or unexpected:
            # M2: record per-file residue for EVERY file, whitelisted included
            entry = {"patch_extra_lines": len(allowed_patch),
                     "blank_lines": allowed_blank,
                     "comment_lines": len(allowed_comments),
                     "unexpected": sorted(unexpected)}
            if path in whole_file_allowed:
                entry["whole_file_allowed"] = True
            entry["residue"] = sorted(unexpected)
            residue[path] = entry
    return residue


def bootstrap_ratio_ci(before: list[float], after: list[float],
                       resamples: int = BOOTSTRAP_RESAMPLES,
                       seed: int = BOOTSTRAP_SEED) -> tuple[float, float]:
    """95% CI of median(after)/median(before), paired-resample bootstrap."""
    rng = random.Random(seed)
    nb, na = len(before), len(after)
    if nb == 0 or na == 0:
        return (float("inf"), float("inf"))
    stats: list[float] = []
    for _ in range(resamples):
        b = statistics.median(before[rng.randrange(nb)] for _ in range(nb))
        a = statistics.median(after[rng.randrange(na)] for _ in range(na))
        stats.append(a / b if b else float("inf"))
    stats.sort()
    lo = stats[int(0.025 * (resamples - 1))]
    hi = stats[int(0.975 * (resamples - 1))]
    return (lo, hi)


def perf_verdict(ci_lo: float, ci_hi: float, median_ratio: float,
                 ceiling: float = CEILING) -> str:
    """Contract WP-R3.json check 7 verdict rule, verbatim: \"PASS if the CI
    upper bound <= 1.02; NOISE if the median ratio <= 1.02 but the CI upper
    bound > 1.02 (then re-run that metric once with 2xN and report both);
    REGRESSION if the CI lower bound > 1.02\". The median clause is applied:
    a CI that straddles the ceiling with median > ceiling is still NOISE
    (never PASS, never REGRESSION — the contract has no fourth verdict);
    after the 2xN rerun the caller downgrades NOISE to NOISE-UNRESOLVED."""
    if ci_lo > ceiling:
        return "REGRESSION"
    if ci_hi <= ceiling:
        return "PASS"
    return "NOISE"


def mirror_forbidden_calls(tree) -> list[str]:
    """B1 guard: names of any input/execute/clipboard/launch/focus-style call
    in the parsed mirror AST (screen-READ only). `type` is included, so
    `backend.type_text(...)` is caught."""
    forbidden_exact = {
        "execute", "release_modifiers", "release", "SendInput", "sendinput",
        "keybd_event", "SetCursorPos", "mouse_event", "typewrite", "position",
        "click", "rightClick", "dragTo", "scroll", "hotkey", "keyDown", "keyUp",
        "press", "write", "type",
    }
    forbidden_prefixes = ("focus", "launch", "clipboard", "sendinput", "click",
                          "drag", "scroll", "keypress", "hotkey", "type")
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            names = [func.attr]
        elif isinstance(func, ast.Name):
            names = [func.id]
        else:
            continue
        for n in names:
            low = n.lower()
            if n in forbidden_exact or any(low.startswith(p) for p in forbidden_prefixes) \
                    or "clipboard" in low or "sendinput" in low:
                hits.append(n)
    return hits


def warm_child_env(env: dict) -> dict:
    """M1: the warm import phase must be able to WRITE .pyc files — drop
    PYTHONDONTWRITEBYTECODE; the per-side PYTHONPYCACHEPREFIX stays."""
    out = dict(env)
    out.pop("PYTHONDONTWRITEBYTECODE", None)
    return out


def rerun_done_for_metric(metric: str, phases: dict,
                          import_n: int = IMPORT_N,
                          profile_rounds: int = PROFILE_ROUNDS) -> bool:
    """M5: track the contract's 2xN rerun PER METRIC GROUP, from the rerun-*
    phases ONLY, and require the rerun to be full-size (finding 10: the 2xN
    rerun means the SAME N again — import 40 samples, profile 15 rounds)."""
    if metric == "import_cold_ms":
        return int(phases.get("rerun-import-cold", 0)) >= import_n
    if metric == "import_warm_ms":
        return int(phases.get("rerun-import-warm", 0)) >= import_n
    return int(phases.get("rerun-profile", 0)) >= profile_rounds


def first_pass_records(series: dict, import_n: int = IMPORT_N,
                       profile_rounds: int = PROFILE_ROUNDS) -> dict:
    """Finding 1: derive the FIRST 1xN pass deterministically from the FIRST N
    samples of each series (contract N: 40 per import side, 15 profile
    rounds) — independent of how many phases ran or in what order."""
    out: dict[str, dict] = {}
    for metric, d in sorted(series.items()):
        n = import_n if metric.startswith("import_") else profile_rounds
        out[metric] = _metric_record(d["before"][:n], d["after"][:n])
    return out


def overall_verdict(verdicts: list) -> str:
    """M4: ranked overall — STOP > INCOMPLETE > NOISE > PASS; an unrecognised
    verdict is treated as STOP (never silently PASS)."""
    known = {"PASS", "FAIL", "REGRESSION", "NOISE", "NOISE-UNRESOLVED",
             "NOT_RUN", "DEFERRED", "INCOMPLETE"}
    if any(v not in known or v in ("FAIL", "REGRESSION", "NOISE-UNRESOLVED")
           for v in verdicts):
        return "STOP"
    if any(v in ("NOT_RUN", "DEFERRED", "INCOMPLETE") for v in verdicts):
        return "INCOMPLETE"
    if any(v == "NOISE" for v in verdicts):
        return "NOISE"
    return "PASS"


SPLIT_NAME_MAP = (
    ("fake_backend.py", "backend.py"), ("errors.py", "backend.py"),
    ("coordinates.py", "backend.py"), ("motion.py", "backend.py"),
    ("launch.py", "backend.py"), ("input_engine.py", "backend.py"),
    ("clipboard.py", "backend.py"), ("text_readers.py", "backend.py"),
    ("windows.py", "backend.py"), ("capture.py", "backend.py"),
    ("win32.py", "backend.py"),
    ("schemas.py", "server.py"), ("state.py", "server.py"),
    ("replies.py", "server.py"), ("wait_machine.py", "server.py"),
    ("plan_surface.py", "server.py"), ("lifecycle.py", "server.py"),
    ("wait.py", "server.py"), ("observe.py", "server.py"),
    ("execute.py", "server.py"), ("fetch.py", "server.py"),
    ("instance.py", "server.py"), ("app.py", "server.py"),
)


def signed_pct(ratio: float) -> str:
    """Finding 1: signed percentage of a ratio, formatted once, correct for
    both signs: 1.013 -> '+1.3%', 0.9406 -> '-5.9%'."""
    return f"{(ratio - 1.0) * 100.0:+.1f}%"


def import_time_section(perf: dict, d7: dict | None = None,
                        d3: dict | None = None) -> list[str]:
    """Import-time ruling text (VAL-WP-R3-04): the Commander's wording with
    every number filled from the artifacts."""
    lines = ["## Import time", ""]
    finals = perf.get("metrics_final", {})
    cold = finals.get("import_cold_ms")
    warm = finals.get("import_warm_ms")
    d7t = (d7 or {}).get("importtime_total", {})
    module_note = "module delta unavailable (D3 missing)"
    if d3:
        ps = d3.get("per_side", {})
        b_mod = ps.get("before", {}).get("cum_modules")
        a_mod = ps.get("after", {}).get("cum_modules")
        if b_mod is not None and a_mod is not None:
            module_note = f"AFTER loads {a_mod - b_mod} more package modules"
    if cold and warm and d7t:
        # finding 1: signed percentages via one helper, correct for both signs
        lean_slower = [name for name, rec in
                       (("cold", cold), ("warm", warm), ("D7 total", d7t))
                       if rec["ratio"] > 1.0]
        lean_faster = [name for name, rec in
                       (("cold", cold), ("warm", warm), ("D7 total", d7t))
                       if rec["ratio"] <= 1.0]
        lean_clause = (f"{', '.join(lean_slower)} lean slower"
                       if not lean_faster else
                       f"{', '.join(lean_slower) or 'none'} lean slower and "
                       f"{', '.join(lean_faster)} lean faster")
        sigma = (d7 or {}).get("sigma_self", {})
        recon = sigma.get("reconciliation", {})
        ci_width = (d7t["ci95"][1] - d7t["ci95"][0]) * 100.0
        lines.append(
            f"Start-up import leans: {lean_clause} (cold "
            f"{signed_pct(cold['ratio'])} CI "
            f"[{signed_pct(cold['ci95'][0])}, {signed_pct(cold['ci95'][1])}], "
            f"warm {signed_pct(warm['ratio'])} CI "
            f"[{signed_pct(warm['ci95'][0])}, {signed_pct(warm['ci95'][1])}], "
            f"D7 total {signed_pct(d7t['ratio'])} CI "
            f"[{signed_pct(d7t['ci95'][0])}, {signed_pct(d7t['ci95'][1])}]); "
            f"product modules' own import time "
            f"{sigma.get('computer_use_mcp_delta_us', 0) / 1000.0:+.1f} ms "
            f"({sigma.get('computer_use_mcp_before_us', 0) / 1000.0:.1f} -> "
            f"{sigma.get('computer_use_mcp_after_us', 0) / 1000.0:.1f} ms) vs "
            f"other modules "
            f"{sigma.get('other_modules_delta_us', 0) / 1000.0:+.1f} ms; the "
            f"reconciliation residual (measured total minus Sigma-self) is "
            f"{recon.get('residual_before_us', 0) / 1000.0:+.1f} ms BEFORE and "
            f"{recon.get('residual_after_us', 0) / 1000.0:+.1f} ms AFTER - "
            f"UNEXPLAINED, and D7 is noise-dominated; per-module median "
            f"|delta| {d7.get('per_module_median_abs_delta_us', 0):.0f} us "
            f"(per-module only, not comparable to sums) over "
            f"{d7.get('same_name_module_count', 0)} same-name modules; "
            f"the D7 totals are very noisy (n {d7t.get('n', ['?'])[0]} per "
            f"side, CI width {ci_width:.1f} percentage points) and do not by "
            f"themselves establish anything; {module_note}; a cost up to ~5% "
            f"is not excluded and none is statistically established. It is a "
            f"one-time cost per server start, not per action.")
        for metric, label in (("import_cold_ms", "Cold import"),
                              ("import_warm_ms", "Warm import")):
            rec = finals.get(metric)
            if rec:
                lines.append(
                    f"- {label}: ratio {rec['ratio']:.4f}, CI95 "
                    f"[{rec['ci95'][0]:.4f}, {rec['ci95'][1]:.4f}], "
                    f"n {rec['n']} ({rec['verdict']})")
    else:
        lines.append("- Import time: not measured in this generation")
    if d7:
        rec = d7.get("importtime_total", {})
        sigma = d7.get("sigma_self", {})
        oa = d7.get("only_in_after", {})
        ob = d7.get("only_in_before", {})
        lines.append(
            f"- D7 -X importtime breakdown (warm, interleaved, "
            f"{d7.get('rounds_per_side')} runs/side): median total import "
            f"{rec.get('before_median', 0):.0f} vs "
            f"{rec.get('after_median', 0):.0f} us (ratio "
            f"{rec.get('ratio', 0):.4f}); net product self-time "
            f"{sigma.get('computer_use_mcp_delta_us', 0):+.0f} us "
            f"(control/other modules "
            f"{sigma.get('other_modules_delta_us', 0):+.0f} us); noise floor "
            f"{d7.get('noise_floor_median_abs_delta_us', 0):.0f} us "
            f"(median |delta| of {d7.get('same_name_module_count', 0)} "
            f"same-name modules); re-homed files: {oa.get('count', 0)} "
            f"AFTER-only vs {ob.get('count', 0)} BEFORE-only (top product "
            f"self-time deltas in diag_D7.json)")
    return lines


def normalize_d5_func(func: str) -> str:
    """m3: normalize a D5 row name — strip 0x… addresses and r3-diag-…
    child paths, strip the layer segments (anchored), and map the WP-R2
    split destination files back to their origin module so split-move pairs
    pair by name."""
    f = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", func)
    f = re.sub(r"^[A-Za-z]:?/.*?/r3-diag-[^/]+/", "(diag_child)/", f)
    f = _ANCHORED_LAYER_RE.sub("", f.replace("\\", "/"))
    for dest, origin in SPLIT_NAME_MAP:
        f = re.sub(r"(?<!\w)" + re.escape(dest) + r"(?![\w.])", origin, f)
    return f


def ms_to_us(ms: float) -> float:
    """M6 unit fix: audit Metrics latencies are MILLISECONDS; report prose
    speaks microseconds (1 ms = 1000 us)."""
    return ms * 1000.0


def residue_pin_problems(digests: dict, pins: dict,
                         allowed: set = frozenset()) -> list[str]:
    """M2 (finding 10): enforce the residue pins for EVERY digested file —
    a pin mismatch or a pinned file whose residue changed/absent fails."""
    problems: list[str] = []
    for f, dig in sorted(digests.items()):
        if pins.get(f) != dig:
            problems.append(f"residue PIN mismatch for {f}: "
                            f"expected {pins.get(f)}, got {dig}")
    for f in sorted(pins):
        if f not in digests:
            problems.append(f"pinned file {f} no longer has any residue "
                            f"(expected {pins[f]})")
    return problems


def missing_module_problems(missing: list) -> list[str]:
    """M3: a wheel missing any module must fail the launch check."""
    if not missing:
        return []
    return [f"wheel is missing {len(missing)} module(s): "
            f"{[m['expected'] for m in missing[:10]]}"]


def _unwrap_symbol_map(raw: dict) -> dict:
    if isinstance(raw, dict) and isinstance(raw.get("symbol_map"), dict):
        raw = raw["symbol_map"]
    return {str(k): str(v) for k, v in raw.items()}


def merge_symbol_maps(move_table_raw: dict, symbol_maps: list[dict]) -> dict:
    """Merged old->new map: module-level move table + module defaults, then
    name-level keys (which win over module-level via cmd_diff's lookup).
    Metadata keys (no dot) are dropped."""
    merged: dict[str, str] = {}
    for k, v in move_table_raw.items():
        if "." in str(k):
            merged[str(k)] = str(v)
    merged.update(MODULE_DEFAULTS)
    for raw in symbol_maps:
        for k, v in _unwrap_symbol_map(raw).items():
            if "." in k:
                merged[k] = v
    return merged


def _entry_matches(actual: dict, allowed: dict) -> bool:
    return all(actual.get(k) == v for k, v in allowed.items())


def classify_inventory_report(report: dict, expected_stmt_missing: list[dict],
                              expected_stmt_added: list[dict]) -> dict:
    """Split a baseline.cmd_diff report into the reviewed allow-list and the
    UNEXPECTED residue (which fails the check)."""

    def split(actual: list, allowed: list[dict]) -> tuple[list, list]:
        ok, bad = [], []
        for entry in actual:
            (ok if any(_entry_matches(entry, a) for a in allowed) else bad).append(entry)
        return ok, bad

    out: dict = {}
    out["missing"], out["missing_unexpected"] = split(report.get("missing", []), ALLOWED_MISSING)
    out["added"], out["added_unexpected"] = split(report.get("added", []), ALLOWED_ADDED)
    out["duplicated"], out["duplicated_unexpected"] = split(
        report.get("symbol_duplicated", []), ALLOWED_DUPLICATED)
    out["symmap_mismatch"], out["symmap_mismatch_unexpected"] = split(
        report.get("symbol_map_mismatch", []), ALLOWED_SYMMAP_MISMATCH)
    out["stmt_missing"], out["stmt_missing_unexpected"] = split(
        report.get("statement_missing", []), expected_stmt_missing)
    out["stmt_added"], out["stmt_added_unexpected"] = split(
        report.get("statement_added", []), expected_stmt_added)
    out["changed"] = report.get("changed", [])
    out["moved_mismatch"] = report.get("moved_mismatch", [])
    out["symmap_unknown_keys"] = report.get("symbol_map_unknown_keys", [])
    out["statement_order_violations"] = report.get("statement_order_violations", [])
    out["unexpected_total"] = sum(
        len(out[k]) for k in ("missing_unexpected", "added_unexpected",
                              "duplicated_unexpected", "symmap_mismatch_unexpected",
                              "stmt_missing_unexpected", "stmt_added_unexpected")
    ) + len(out["changed"]) + len(out["moved_mismatch"]) + \
        len(out["symmap_unknown_keys"]) + len(out["statement_order_violations"])
    return out


# ---------------------------------------------------------------------------
# AST derivations for the reviewed statement allow-list
# ---------------------------------------------------------------------------

def _top_if_hashes(test_names: set[str]) -> dict[str, list[dict]]:
    """{module: [{name, hash, ordinal}]} of top-level If statements whose test
    is a Name in test_names, for the tree baseline currently points at."""
    module_ids = baseline.collect_module_ids()
    out: dict[str, list[dict]] = {}
    for path in sorted(baseline.PKG.rglob("*.py")):
        if path.name == "__init__.py":
            mod = baseline._dotted(path.parent.relative_to(baseline.SRC))
        else:
            mod = baseline._dotted(path.relative_to(baseline.SRC).with_suffix(""))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = baseline._alias_map(tree, module_ids, mod)
        for ordinal, node in enumerate(tree.body):
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name) \
                    and node.test.id in test_names:
                digest = baseline.normalized_hash(node, module_ids, aliases)
                out.setdefault(mod, []).append({
                    "name": f"stmt@{digest[:8]}", "hash": digest, "ordinal": ordinal})
    return out


def derive_expected_statement_deltas(before_tree: Path, after_tree: Path) -> dict:
    with _tree_globals(before_tree):
        before_if = _top_if_hashes({"IS_WINDOWS"})
    with _tree_globals(after_tree):
        after_tc = _top_if_hashes({"TYPE_CHECKING"})
        after_win = _top_if_hashes({"IS_WINDOWS"})
    before_win = [(mod, e) for mod, es in before_if.items() for e in es]
    after_win_entries = [(mod, e) for mod, es in after_win.items() for e in es]
    tc_entries = [(mod, e) for mod, es in after_tc.items() for e in es]
    expected_missing = [
        {"module": mod, "kind": "If", "name": e["name"], "hash": e["hash"]}
        for mod, e in before_win
    ]
    expected_added = [
        {"module": mod, "kind": "If", "name": e["name"], "hash": e["hash"]}
        for mod, e in after_win_entries + tc_entries
    ]
    return {
        "expected_stmt_missing": expected_missing,
        "expected_stmt_added": expected_added,
        "before_is_windows_blocks": [{"module": m, **e} for m, e in before_win],
        "after_is_windows_blocks": [{"module": m, **e} for m, e in after_win_entries],
        "type_checking_blocks": [{"module": m, **e} for m, e in tc_entries],
        "type_checking_block_count": len(tc_entries),
        "expected_type_checking_block_count": EXPECTED_TYPE_CHECKING_BLOCKS,
    }


def module_file_map(tree: Path) -> dict[str, str]:
    """{dotted module: wheel-relative .py path} for every module under a
    tree's src (paths carry the computer_use_mcp/ prefix, as in a wheel)."""
    src = Path(tree) / "src" / "computer_use_mcp"
    out = {}
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src.parent).as_posix()
        dotted = rel[:-3].replace("/", ".")
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        out[dotted] = rel
    return out


# ---------------------------------------------------------------------------
# m1 preflight: fail fast on mis-bound trees
# ---------------------------------------------------------------------------

def normalized_text_sha256(data: bytes) -> str:
    """BLOCKER 1 (core.autocrlf=true, no .gitattributes): a fresh checkout
    checks text files out with CRLF line endings, so RAW-byte hashes of the
    pinned inputs would never match. Hash LINE-ENDING-NORMALIZED bytes
    (CRLF -> LF, then UTF-8) instead — stable across checkout modes."""
    text = data.decode("utf-8")
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def verify_tracked_inputs() -> list[str]:
    """R: the tracked inputs must exist and match their PINNED sha256 over
    line-ending-normalized bytes (see normalized_text_sha256); the test
    compares to the .orvex sources the same way."""
    problems: list[str] = []
    for rel, expected in PINNED_INPUT_SHA256.items():
        f = TOOL_DIR / rel
        if not f.exists():
            problems.append(f"tracked input missing: {rel}")
            continue
        digest = normalized_text_sha256(f.read_bytes())
        if digest != expected:
            problems.append(f"tracked input {rel} normalized sha256 {digest} "
                            f"!= pinned {expected}")
    return problems


def preflight(before: Path, after: Path, git_fn=_git,
              before_git: str = BEFORE_GIT, after_git: str = AFTER_GIT,
              wp_files: tuple = WP_R3_FILES) -> list[str]:
    """Problems that must be empty before any check runs:
    before != after; BEFORE HEAD == bf21ae0 with `status --porcelain --ignored`
    EMPTY; AFTER HEAD == 9227c4b and its working tree differs only by the
    WP-R3 files."""
    problems: list[str] = []
    if before.resolve() == after.resolve():
        problems.append("preflight: --before and --after are the same directory")
    b_head = git_fn(["rev-parse", "--short=7", "HEAD"], before).stdout.strip()
    if b_head != before_git:
        problems.append(f"preflight: BEFORE HEAD {b_head!r} != {before_git!r}")
    b_status = git_fn(["status", "--porcelain", "--ignored"], before).stdout
    if b_status.strip():
        problems.append(f"preflight: BEFORE worktree not clean incl. ignored "
                        f"({len(b_status.splitlines())} lines)")
    a_head = git_fn(["rev-parse", "--short=7", "HEAD"], after).stdout.strip()
    if a_head != after_git:
        # R/m1: after landing, HEAD moves past 9227c4b — accept it as an
        # ANCESTOR of HEAD with the diff limited to the WP-R3 files
        anc = git_fn(["merge-base", "--is-ancestor", after_git, "HEAD"], after)
        if anc.returncode != 0:
            problems.append(f"preflight: AFTER HEAD {a_head!r} != {after_git!r} "
                            f"and {after_git} is not an ancestor of HEAD")
        else:
            names = git_fn(["diff", "--name-only", f"{after_git}..HEAD"], after)
            extra = [ln for ln in names.stdout.splitlines()
                     if ln and ln not in wp_files]
            if extra:
                problems.append(f"preflight: HEAD moved past {after_git} with "
                                f"non-WP-R3 changes: {extra[:10]}")
    a_status = git_fn(["status", "--porcelain", "-uall"], after).stdout
    # the WP's own files may be untracked (??) or modified ( M)
    allowed = {s + f for f in wp_files for s in ("?? ", " M ")}
    unexpected = [ln for ln in a_status.splitlines() if ln not in allowed]
    if unexpected:
        problems.append(f"preflight: AFTER working tree has non-WP-R3 changes: "
                        f"{unexpected[:10]}")
    return problems


# ---------------------------------------------------------------------------
# check 1: inventory
# ---------------------------------------------------------------------------

def check_inventory(before: Path, after: Path, out: Path) -> dict:
    # m5: record each side's binding in the result (inventory is in-process
    # AST work — cwd is never used, the tree enters via SRC/PKG)
    run_meta = {side: {"cwd": None, "pythonpath_used": str(Path(tree) / "src"),
                       "mode": "in-process AST (no cwd, no product import)"}
                for side, tree in (("before", before), ("after", after))}
    input_problems = verify_tracked_inputs()
    merged = merge_symbol_maps(_read_json(MOVE_TABLE), [_read_json(p) for p in SYMBOL_MAPS])
    map_file = out / "symbol_map_merged.json"
    _write_json(map_file, merged)

    b_inv, a_inv = out / "inventory_before.json", out / "inventory_after.json"
    with _tree_globals(before):
        baseline.cmd_inventory(argparse.Namespace(out=str(b_inv)))
    with _tree_globals(after):
        baseline.cmd_inventory(argparse.Namespace(out=str(a_inv)))

    coverage: list[str] = []
    b_blob = _read_json(b_inv)
    b_modules = {e["module"] for e in b_blob["symbols"]} | \
        {s["module"] for s in b_blob.get("statements", [])}
    for mod in sorted(b_modules):
        if mod in STAY_PUT_MODULES or mod in merged or \
                any(k.startswith(mod + ":") for k in merged):
            continue
        coverage.append(mod)

    derived = derive_expected_statement_deltas(before, after)
    with _tree_globals(after):
        rc = baseline.cmd_diff(argparse.Namespace(
            before=str(b_inv), after=str(a_inv), out=str(out / "inventory_diff.json"),
            move_table=None, symbol_map=str(map_file)))
    report = _read_json(out / "inventory_diff.json")
    cls = classify_inventory_report(
        report, derived["expected_stmt_missing"], derived["expected_stmt_added"])

    problems = []
    problems.extend(input_problems)
    # m2: the derived statement deltas must MATCH the pinned reviewed hashes
    derived_pairs = {(b["module"], b["hash"]) for b in derived["before_is_windows_blocks"]}
    if PINNED_BEFORE_WIN32_IF not in derived_pairs:
        problems.append(f"pinned BEFORE win32 If {PINNED_BEFORE_WIN32_IF} not derived")
    after_pairs = {(b["module"], b["hash"]) for b in derived["after_is_windows_blocks"]}
    if PINNED_AFTER_WIN32_IF not in after_pairs:
        problems.append(f"pinned AFTER win32 If {PINNED_AFTER_WIN32_IF} not derived")
    tc_hashes = {e["hash"] for e in derived["type_checking_blocks"]}
    if tc_hashes != {PINNED_TYPE_CHECKING_HASH} or \
            derived["type_checking_block_count"] != EXPECTED_TYPE_CHECKING_BLOCKS:
        problems.append(f"pinned TYPE_CHECKING hash/count mismatch: "
                        f"{tc_hashes} x {derived['type_checking_block_count']}")
    if coverage:
        problems.append(f"unmapped BEFORE modules: {coverage}")
    if cls["unexpected_total"]:
        problems.append(f"unexpected inventory residue ({cls['unexpected_total']} entries)")
    verdict = "PASS" if not problems else "FAIL"
    result = {
        "verdict": verdict, "problems": problems,
        "before_symbols": b_blob["symbol_count"], "after_symbols": _read_json(a_inv)["symbol_count"],
        "before_statements": b_blob["statement_count"],
        "after_statements": _read_json(a_inv)["statement_count"],
        "merged_map_entries": len(merged), "map_coverage_gaps": coverage,
        "diff_summary": {k: report.get(k) for k in (
            "missing", "changed", "added", "moved_count", "unchanged_count",
            "moved_verified_by_table", "symbol_map_verified", "statement_moved_per_map")},
        "classification": cls,
        "derived": derived,
        "run_metadata": run_meta,
        "evidence": ["inventory_before.json", "inventory_after.json",
                     "inventory_diff.json", "symbol_map_merged.json"],
    }
    _write_json(out / "inventory.json", result)
    return result


# ---------------------------------------------------------------------------
# check 2: node ids
# ---------------------------------------------------------------------------

def check_nodeids(before: Path, after: Path, out: Path) -> dict:
    ids = {}
    for side, tree in (("before", before), ("after", after)):
        sub = out / f"nodeids_{side}"
        sub.mkdir(parents=True, exist_ok=True)
        with _tree_globals(tree):
            baseline.stage_nodeids(GATE_PY, sub)
        ids[side] = [ln for ln in (sub / "nodeids.txt").read_text(encoding="utf-8").splitlines() if ln]

    diff = _git(["diff", "-M", "--name-status", f"{BEFORE_GIT}..{AFTER_GIT}", "--", "tests", "benchmarks"], after)
    renames, added_files, _modified = parse_name_status(diff.stdout)
    renames = {k: v for k, v in renames.items() if k.endswith(".py")}
    mapping = map_node_ids(ids["before"], ids["after"], renames)

    renamed_heads = set(renames)
    allowed_added_files = added_files | set(WP_R3_TOOL_TEST_FILES) | set(RESTRUCTURE_OWNED_TEST_FILES)
    problems = []
    removed_disallowed = [i for i in mapping["removed"]
                          if i.split("::", 1)[0] not in renamed_heads]
    added_by_file: dict[str, list[str]] = {}
    added_disallowed = []
    for i in mapping["added"]:
        head = i.split("::", 1)[0]
        added_by_file.setdefault(head, []).append(i)
        if head not in allowed_added_files:
            added_disallowed.append(i)
    if removed_disallowed:
        problems.append(f"{len(removed_disallowed)} removed ids outside renamed files")
    if added_disallowed:
        problems.append(f"{len(added_disallowed)} added ids outside new guard/test files")
    verdict = "PASS" if not problems else "FAIL"
    result = {
        "verdict": verdict, "problems": problems,
        "count_before": len(ids["before"]), "count_after": len(ids["after"]),
        "renames": renames, "added_files": sorted(added_files),
        "removed_ids": mapping["removed"],
        "removed_outside_renames": removed_disallowed,
        "added_by_file": {k: v for k, v in sorted(added_by_file.items())},
        "added_disallowed": added_disallowed,
        "wp_r3_tool_test_files": list(WP_R3_TOOL_TEST_FILES),
        "evidence": ["nodeids_before/nodeids.txt", "nodeids_after/nodeids.txt"],
    }
    _write_json(out / "nodeids.json", result)
    return result


# ---------------------------------------------------------------------------
# check 3: counts
# ---------------------------------------------------------------------------

def _collected_vs_junit(out: Path, a_stat: dict, b_stat: dict) -> dict:
    """m4: explain the collected-node-id vs junit-testcase gap (e.g. 3167 vs
    3169) by listing the junit keys that have no matching collected id."""
    explanation = {"method": "junit testcases minus collected ids, per side"}
    for side, stat in (("before", b_stat), ("after", a_stat)):
        nodeids_file = out / f"nodeids_{side}" / "nodeids.txt"
        junit_n = len(stat)
        if not nodeids_file.exists():
            explanation[side] = {"junit_testcases": junit_n,
                                 "note": "nodeids not collected yet"}
            continue
        collected = [ln for ln in nodeids_file.read_text(
            encoding="utf-8").splitlines() if ln]
        collected_keys = {_junit_key(i) for i in collected}
        unmatched = sorted({f"{k[0]}::{k[1]}" for k in stat if k not in collected_keys})
        explanation[side] = {
            "junit_testcases": junit_n,
            "collected_ids": len(collected),
            "junit_without_collected_id": unmatched[:10],
            "explanation": ("junit counts each executed test; the -q collection "
                            "list can differ where one collected id maps to "
                            "several executed cases or an id line is wrapped"),
        }
    return explanation


def _junit_key(nodeid: str) -> tuple[str, str]:
    path, *rest = nodeid.split("::")
    dotted = path[:-3].replace("/", ".") if path.endswith(".py") else path.replace("/", ".")
    if len(rest) >= 2:
        dotted = dotted + "." + ".".join(rest[:-1])
    return (dotted, rest[-1] if rest else "")


def _junit_statuses(xml_path: Path) -> dict[tuple[str, str], str]:
    import xml.etree.ElementTree as ET
    statuses: dict[tuple[str, str], str] = {}
    for tc in ET.parse(xml_path).getroot().iter("testcase"):
        key = (tc.get("classname", ""), tc.get("name", ""))
        status = "passed"
        if tc.find("skipped") is not None:
            status = "skipped"
        elif tc.find("failure") is not None or tc.find("error") is not None:
            status = "failed"
        statuses[key] = status
    return statuses


def _run_suite(tree: Path, out_xml: Path) -> tuple[dict, dict[tuple[str, str], str], dict]:
    cmd = [str(GATE_PY), "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider",
           "-rfEx", "-W", "error::RuntimeWarning", "--junitxml", str(out_xml)]
    proc = _run(cmd, cwd=tree, env=tree_env(tree), timeout=14400)
    counts = baseline.parse_pytest_counts(proc.stdout)
    if not counts or proc.returncode not in (0, 1):
        raise RuntimeError(f"suite run failed on {tree}: rc={proc.returncode}\n{proc.stdout[-2000:]}\n{proc.stderr[-1000:]}")
    meta = {"cwd": str(tree), "pythonpath_used": str(tree / "src"),
            "command": cmd, "returncode": proc.returncode,
            "summary_line": proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""}
    return counts, _junit_statuses(out_xml), meta


def cmd_suite(args: argparse.Namespace) -> int:
    """One side of check 3 (resumable: each suite is its own command)."""
    tree = Path(args.tree).resolve()
    xml = Path(args.junit).resolve()
    _counts, _stat, meta = _run_suite(tree, xml)
    _write_json(Path(args.out).resolve(), meta)
    return 0


def check_counts(before: Path, after: Path, out: Path,
                 force: bool = False) -> dict:
    b_stat = a_stat = None
    b_counts = a_counts = {}
    metas = {}
    for side, tree in (("before", before), ("after", after)):
        xml = out / f"counts_{side}.junit.xml"
        meta_f = out / f"counts_{side}_meta.json"
        if xml.exists() and meta_f.exists() and not force:
            meta = _read_json(meta_f)
            with _tree_globals(tree):
                counts = baseline.parse_pytest_counts(meta.get("summary_line", ""))
            stat = _junit_statuses(xml)
        else:
            with _tree_globals(tree):
                counts, stat, meta = _run_suite(tree, xml)
            _write_json(meta_f, meta)
        metas[side] = meta
        if side == "before":
            b_counts, b_stat = counts, stat
        else:
            a_counts, a_stat = counts, stat

    diff = _git(["diff", "-M", "--name-status", f"{BEFORE_GIT}..{AFTER_GIT}", "--", "tests"],
                after)
    renames, _added, _mod = parse_name_status(diff.stdout)
    renames = {k: v for k, v in renames.items() if k.endswith(".py")}
    by_dotted_old = {old[:-3].replace("/", "."): new for old, new in renames.items()}

    b_passed = {k for k, s in b_stat.items() if s == "passed"}
    b_skipped = {k for k, s in b_stat.items() if s == "skipped"}
    b_failed = {k for k, s in b_stat.items() if s == "failed"}
    a_passed = {k for k, s in a_stat.items() if s == "passed"}
    a_skipped = {k for k, s in a_stat.items() if s == "skipped"}
    a_failed = {k for k, s in a_stat.items() if s == "failed"}

    unmapped_passed = []
    for k in b_passed:
        mod, name = k
        new_mod = by_dotted_old.get(mod, mod)
        if (new_mod, name) not in a_passed:
            unmapped_passed.append({"before": k, "expected_after": (new_mod, name)})
    unmapped_skips = []
    for k in b_skipped:
        mod, name = k
        new_mod = by_dotted_old.get(mod, mod)
        if (new_mod, name) not in a_skipped:
            unmapped_skips.append({"before": k, "expected_after": (new_mod, name)})
    extra_skips = []
    for k in a_skipped:
        mod, name = k
        old_mod = next((o for o, n in by_dotted_old.items() if n == mod), mod)
        if (old_mod, name) not in b_skipped:
            extra_skips.append({"after": k})

    problems = []
    if b_failed or a_failed:
        problems.append(f"failures: before={sorted(b_failed)[:10]} after={sorted(a_failed)[:10]}")
    if unmapped_passed:
        problems.append(f"{len(unmapped_passed)} before-passing tests without an after-passing partner")
    if unmapped_skips or extra_skips:
        problems.append("skip identity mismatch")
    verdict = "PASS" if not problems else "FAIL"
    result = {
        "verdict": verdict, "problems": problems,
        "counts_before": b_counts, "counts_after": a_counts,
        "run_metadata": metas,
        "collected_vs_junit": _collected_vs_junit(out, a_stat, b_stat),
        "before_failed": sorted(b_failed), "after_failed": sorted(a_failed),
        "unmapped_passed": unmapped_passed,
        "skip_identity": {"unmapped_skips": unmapped_skips, "extra_skips": extra_skips},
        "renames": renames,
        "evidence": ["counts_before.junit.xml", "counts_after.junit.xml"],
    }
    _write_json(out / "counts.json", result)
    return result


# ---------------------------------------------------------------------------
# checks 4 + 5: tool surface / probe
# ---------------------------------------------------------------------------

def check_tools(before: Path, after: Path, out: Path) -> dict:
    res = {}
    for side, tree in (("before", before), ("after", after)):
        sub = out / f"tools_{side}"
        sub.mkdir(parents=True, exist_ok=True)
        with _tree_globals(tree):
            res[side] = {"repo_used": str(baseline.REPO),
                         "pythonpath_used": str(baseline.SRC),
                         **baseline.stage_tools(GATE_PY, sub)}
    problems = []
    if res["before"]["sha256"] != res["after"]["sha256"]:
        problems.append("tools/list sha256 differs between trees")
    if res["after"]["sha256"] != EXPECTED_TOOLS_SHA:
        problems.append("tools/list sha256 != WP-R0 constant")
    if res["before"]["tool_count"] != 10 or res["after"]["tool_count"] != 10:
        problems.append("tool count != 10")
    result = {
        "verdict": "PASS" if not problems else "FAIL", "problems": problems,
        "expected_sha256": EXPECTED_TOOLS_SHA, **res,
        "evidence": ["tools_before/tool_schemas.json", "tools_after/tool_schemas.json"],
    }
    _write_json(out / "tools.json", result)
    return result


def check_probe(before: Path, after: Path, out: Path) -> dict:
    res = {}
    for side, tree in (("before", before), ("after", after)):
        sub = out / f"probe_{side}"
        sub.mkdir(parents=True, exist_ok=True)
        with _tree_globals(tree):
            res[side] = {"repo_used": str(baseline.REPO),
                         "pythonpath_used": str(baseline.SRC),
                         **baseline.stage_probe(GATE_PY, sub)}
    problems = [] if (res["before"]["ok"] and res["after"]["ok"]) else ["probe did not PASS on both trees"]
    result = {
        "verdict": "PASS" if not problems else "FAIL", "problems": problems, **res,
        "evidence": ["probe_before/probe.txt", "probe_after/probe.txt"],
    }
    _write_json(out / "probe.json", result)
    return result


# ---------------------------------------------------------------------------
# check 6: audit shapes
# ---------------------------------------------------------------------------

def _install_flat_aliases() -> None:
    for parent in FAKE_PARENTS:
        if parent not in sys.modules:
            mod = types.ModuleType(parent)
            mod.__path__ = []
            sys.modules[parent] = mod
    import importlib
    for layered, flat in FLAT_ALIASES.items():
        if layered not in sys.modules:
            sys.modules[layered] = importlib.import_module(flat)


def cmd_stage_audit(args: argparse.Namespace) -> int:
    tree = Path(args.tree).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.flat_aliases:
        _install_flat_aliases()
    with _tree_globals(tree):
        baseline.stage_audit(Path(sys.executable), out)
    return 0


def check_audit(before: Path, after: Path, out: Path) -> dict:
    shapes = {}
    for side, tree, flat in (("before", before, True), ("after", after, False)):
        sub = out / f"audit_{side}"
        proc = _run([str(GATE_PY), str(Path(__file__).resolve()), "_stage-audit",
                     "--tree", str(tree), "--out", str(sub)] +
                    (["--flat-aliases"] if flat else []),
                    cwd=tree, env=tree_env(tree), timeout=900)
        if proc.returncode != 0:
            raise RuntimeError(f"audit stage failed for {side}: {proc.stderr[-1500:]}")
        shapes[side] = _read_json(sub / "audit_shapes.json")["shapes"]

    types_b, types_a = set(shapes["before"]), set(shapes["after"])
    shape_diffs = []
    for et in sorted(types_b & types_a):
        for key in ("top_level_keys", "metadata_keys"):
            if shapes["before"][et][key] != shapes["after"][et][key]:
                shape_diffs.append({"event_type": et, "key": key,
                                    "before": shapes["before"][et][key],
                                    "after": shapes["after"][et][key]})
    problems = []
    if types_b != types_a:
        problems.append(f"event-type sets differ: only_before={sorted(types_b - types_a)} "
                        f"only_after={sorted(types_a - types_b)}")
    if shape_diffs:
        problems.append(f"{len(shape_diffs)} key-shape diffs")
    result = {
        "verdict": "PASS" if not problems else "FAIL", "problems": problems,
        "run_metadata": {side: {"cwd": str(tree), "pythonpath_used": str(Path(tree) / "src")}
                         for side, tree in (("before", before), ("after", after))},
        "event_type_count_before": len(types_b), "event_type_count_after": len(types_a),
        "shape_diffs": shape_diffs,
        "method_note": ("BEFORE runs baseline.stage_audit with flat->layered "
                        "sys.modules aliases so the identical current tool code "
                        "executes against the flat tree; shapes are compared "
                        "per event type."),
        "evidence": ["audit_before/audit_shapes.json", "audit_after/audit_shapes.json"],
    }
    _write_json(out / "audit.json", result)
    return result


# ---------------------------------------------------------------------------
# check 8: filtered test diff gate
# ---------------------------------------------------------------------------

def check_testdiff(after: Path, out: Path) -> dict:
    diff = _git(["diff", "-M", f"{BEFORE_GIT}..{AFTER_GIT}", "--", "tests", "benchmarks"], after)
    if diff.returncode != 0:
        raise RuntimeError(f"git diff failed: {diff.stderr[-500:]}")
    _renames, added_files, _mod = parse_name_status(
        _git(["diff", "-M", "--name-status", f"{BEFORE_GIT}..{AFTER_GIT}",
              "--", "tests", "benchmarks"], after).stdout)
    residue = diff_residue(diff.stdout, added_files=added_files,
                           whole_file_allowed=ALLOWED_RESIDUE_FILES)
    disallowed = {f: entry for f, entry in residue.items()
                  if f not in ALLOWED_RESIDUE_FILES and entry["unexpected"]}

    # M2: pin a sha256 of each whitelisted file's residue in a tracked JSON —
    # any change to the whitelisted files' residue fails the check
    def _digest(entry: dict) -> str:
        return hashlib.sha256(json.dumps(entry, sort_keys=True,
                                         ensure_ascii=False).encode("utf-8")).hexdigest()

    digests = {f: _digest(e) for f, e in sorted(residue.items())}
    problems = []
    if not TESTDIFF_PINS.exists():
        problems.append(f"residue pins missing ({TESTDIFF_PINS}): write the "
                        f"tracked pins from this run's residue_digests, then re-run")
    else:
        pins = _read_json(TESTDIFF_PINS)
        problems.extend(residue_pin_problems(digests, pins))
    if disallowed:
        problems.append(f"unlisted residue files: {sorted(disallowed)}")
    verdict = "PASS" if not problems else "FAIL"
    result = {
        "verdict": verdict, "problems": problems,
        "residue_digests": digests,
        "pins_file": str(TESTDIFF_PINS),
        "allow_list": sorted(ALLOWED_RESIDUE_FILES),
        "added_files_ignored": sorted(added_files),
        "residue_files": sorted(residue),
        "residue": residue,
        "disallowed_residue": disallowed,
        "residue_file_count": len(residue),
        "method_note": ("retarget pairing strips the mechanical alias family "
                        "(server / srv_server_* / *_module) and module paths; "
                        "added blank lines and the reviewed D3/D7 same-object "
                        "patch lines are allowed everywhere and counted; any "
                        "other leftover fails unless the file is on the "
                        "reviewed whole-file allow-list (step-0 guards, "
                        "source-text rewrites, tuple-rebind splits). The "
                        "behavioural proof itself is carried by checks 1-3."),
    }
    _write_json(out / "testdiff.json", result)
    return result


# ---------------------------------------------------------------------------
# check 9: dev tree hashes
# ---------------------------------------------------------------------------

def check_devtree(out: Path, dev_tree: Path | None = None) -> dict:
    tree = Path(dev_tree) if dev_tree else DEV_TREE
    if tree is None:
        result = {"verdict": "FAIL",
                  "problems": ["dev tree required: pass --dev-tree or set "
                               "CORTEX_DEV_TREE (read-only; only hashes)"]}
        _write_json(out / "devtree.json", result)
        return result
    if not tree.exists():
        result = {"verdict": "FAIL", "problems": [f"dev tree missing: {tree}"]}
        _write_json(out / "devtree.json", result)
        return result
    status = _git(["status", "--porcelain", "-uall"], tree)
    diff = _git(["diff"], tree)
    status_sha = hashlib.sha256(status.stdout.encode("utf-8")).hexdigest()
    diff_sha = hashlib.sha256(diff.stdout.encode("utf-8")).hexdigest()
    problems = []
    if not status_sha.startswith(EXPECTED_DEV_STATUS_SHA):
        problems.append(f"dev status sha256 {status_sha} != {EXPECTED_DEV_STATUS_SHA}...")
    if not diff_sha.startswith(EXPECTED_DEV_DIFF_SHA):
        problems.append(f"dev diff sha256 {diff_sha} != {EXPECTED_DEV_DIFF_SHA}...")
    result = {
        "verdict": "PASS" if not problems else "FAIL", "problems": problems,
        "dev_tree": str(tree),
        "status_sha256": status_sha, "diff_sha256": diff_sha,
        "expected_status_sha256_prefix": EXPECTED_DEV_STATUS_SHA,
        "expected_diff_sha256_prefix": EXPECTED_DEV_DIFF_SHA,
    }
    _write_json(out / "devtree.json", result)
    return result


# ---------------------------------------------------------------------------
# check 6b (contract check 6): launch paths on a clean venv from the AFTER wheel
# ---------------------------------------------------------------------------

LAUNCH_CHECKER = '''"""Written by tools/restructure/equivalence.py — launch-path checks."""
import importlib.metadata as md
import json

targets = {ep.name: ep.value for ep in md.entry_points(group="console_scripts")}
import computer_use_mcp.server as srv
import computer_use_mcp.cli as cli
print(json.dumps({
    "console_scripts": targets,
    "server_main_callable": callable(srv.main),
    "server_main_module": getattr(srv.main, "__module__", None),
    "cli_main_callable": callable(cli.main),
}))
'''


def check_launch(before: Path, after: Path, out: Path) -> dict:
    # The gate venv's site-packages provide ONLY third-party deps (the project
    # is never installed there); a venv built from a venv cannot inherit them
    # via system_site_packages (it would point at the BASE interpreter), so the
    # dep path is given explicitly and no PYTHONPATH can ever expose a tree.
    gate_site = GATE_PY.parent.parent / "Lib" / "site-packages"
    saved_env_pp = os.environ.pop("PYTHONPATH", None)
    try:
        # baseline.stage_wheel(out) builds into <out>/wheel/ itself
        with _tree_globals(after):
            wheel_res = baseline.stage_wheel(out)
        wheel_dir = out / "wheel"
        whl = wheel_dir / wheel_res["wheel"]
        names = set(zipfile.ZipFile(whl).namelist())

        problems: list[str] = []
        with tempfile.TemporaryDirectory(prefix="r3-venv-") as tmp:
            tmp = Path(tmp)
            vdir = tmp / "venv"
            venv.EnvBuilder(with_pip=True, clear=True).create(str(vdir))
            # a generated .pth in the venv's site-packages pulls in the gate
            # venv's third-party deps. Directory lines in a .pth do NOT recurse
            # (the gate site's own pywin32.pth would never run), so the pywin32
            # dirs and its bootstrap import are spelled out explicitly.
            pth = vdir / "Lib" / "site-packages" / "r3_gate_deps.pth"
            pth.parent.mkdir(parents=True, exist_ok=True)
            pth.write_text(
                "\n".join([
                    str(gate_site),
                    str(gate_site / "win32"),
                    str(gate_site / "win32" / "lib"),
                    "import pywin32_bootstrap",
                ]) + "\n",
                encoding="utf-8")
            vpy = vdir / "Scripts" / "python.exe"
            env = _base_env()
            env.pop("PYTHONPATH", None)
            proc = _run([str(vpy), "-m", "pip", "install", "--no-deps", "--no-index",
                         "--no-cache-dir", str(whl)], cwd=tmp, env=env, timeout=600)
            if proc.returncode != 0:
                raise RuntimeError(f"wheel install failed: {proc.stderr[-1500:]}")

            checker = tmp / "launch_check.py"
            checker.write_text(LAUNCH_CHECKER, encoding="utf-8")
            proc = _run([str(vpy), str(checker)], cwd=tmp, env=env, timeout=300)
            launch_info = {}
            if proc.returncode != 0:
                problems.append(f"launch checker failed: {proc.stderr[-800:]}")
            else:
                launch_info = json.loads(proc.stdout.strip().splitlines()[-1])
                if launch_info.get("server_main_module") != "computer_use_mcp.server":
                    problems.append("console-script target does not resolve to the "
                                    "server/__init__ shim")

            # python -m computer_use_mcp.server from the INSTALLED wheel: point
            # baseline's SRC at a dead path so the tree can never shadow the venv
            dead_src = tmp / "no-src"
            saved = (baseline.REPO, baseline.SRC)
            baseline.REPO, baseline.SRC = tmp, dead_src
            try:
                launch_tools = out / "launch_tools"
                launch_tools.mkdir(parents=True, exist_ok=True)
                m_server = baseline.stage_tools(vpy, launch_tools)
            except Exception as exc:
                # stage_tools DEVNULLs the server's stderr — capture it here so a
                # clean-venv failure is diagnosable instead of opaque
                handshake = "\n".join(json.dumps(m) for m in [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "r3-diag", "version": "0"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                ]) + "\n"
                diag = subprocess.run(
                    [str(vpy), "-m", "computer_use_mcp.server"], cwd=str(tmp), env=env,
                    input=handshake, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=180)
                launch_diag = {
                    "rc": diag.returncode,
                    "stderr_tail": diag.stderr[-1500:],
                    "stdout_head": diag.stdout[:400],
                }
                problems.append(f"-m server in the clean venv failed: {exc}; "
                                f"diag rc={diag.returncode} stderr_tail={diag.stderr[-400:]}")
                m_server = {"sha256": None}
            else:
                launch_diag = None
            finally:
                baseline.REPO, baseline.SRC = saved
            if m_server["sha256"] != EXPECTED_TOOLS_SHA:
                problems.append("-m server tools/list sha != WP-R0 constant in the clean venv")

            proc = _run([str(vpy), "-m", "computer_use_mcp.cli", "--help"],
                        cwd=tmp, env=env, timeout=300)
            cli_help_ok = proc.returncode == 0
            if not cli_help_ok:
                problems.append(f"python -m computer_use_mcp.cli --help failed: "
                                f"rc={proc.returncode} {proc.stderr[-300:]}")
            proc = _run([str(vpy), "-m", "computer_use_mcp.cli", "probe",
                         "--python", str(vpy), "--cwd", str(tmp)],
                        cwd=tmp, env=env, timeout=300)
            probe_ok = proc.returncode == 0 and "PROBE PASS" in proc.stdout
            if not probe_ok:
                problems.append(f"clean-venv probe failed: {proc.stdout[-400:]} "
                                f"{proc.stderr[-400:]}")

            # wheel module coverage: every module of BOTH trees, the BEFORE one
            # through the merged old->new module map (no module lost)
            merged = merge_symbol_maps(_read_json(MOVE_TABLE),
                                       [_read_json(p) for p in SYMBOL_MAPS])
            missing_modules = []
            for dotted, rel in module_file_map(after).items():
                if rel not in names:
                    missing_modules.append({"tree": "after", "module": dotted,
                                            "expected": rel})
            for dotted, rel in module_file_map(before).items():
                target_dotted = merged.get(dotted, dotted)
                suffix = "/__init__.py" if rel.endswith("__init__.py") else ".py"
                target = target_dotted.replace(".", "/") + suffix
                if target not in names:
                    missing_modules.append({"tree": "before", "module": dotted,
                                            "expected": target})
            if missing_modules:
                # M3: a lost module must fail the launch check, not just be
                # recorded
                problems.extend(missing_module_problems(missing_modules))

        result = {
            "verdict": "PASS" if not problems else "FAIL", "problems": problems,
            "missing_wheel_modules": missing_modules,
            "wheel": whl.name, "wheel_files": len(names),
            "entry_points": wheel_res["entry_points"],
            "launch_checker": launch_info,
            "m_server_sha256": m_server["sha256"],
            "m_server_diag": launch_diag,
            "cli_help_ok": cli_help_ok,
            "clean_venv_probe_ok": probe_ok,
            "venv_note": ("clean venv (no system site packages); third-party deps "
                          "resolve offline via a generated r3_gate_deps.pth "
                          "sitedir pointing at the gate venv's site-packages "
                          "(the project is never installed in the gate venv); "
                          "the project itself is installed ONLY from the "
                          "AFTER wheel; PYTHONPATH stripped for every venv "
                          "command."),
            "evidence": ["wheel/", "launch_tools/tool_schemas.json"],
        }
    finally:
        if saved_env_pp is not None:
            os.environ["PYTHONPATH"] = saved_env_pp
        else:
            os.environ.pop("PYTHONPATH", None)
    _write_json(out / "launch.json", result)
    return result


# ---------------------------------------------------------------------------
# check 7: interleaved performance A/B
# ---------------------------------------------------------------------------

IMPORT_CHILD_TEMPLATE = '''"""WP-R3 import-time child (written by equivalence.py into a tempfile dir)."""
import time

t0 = time.perf_counter()
import {module}  # noqa: E402,F401

print(time.perf_counter() - t0)
'''


def _import_once(child: Path, tree: Path, warm: bool, cache_dir: Path) -> float:
    extra: dict = {"PYTHONPATH": str(tree / "src")}
    if warm:
        # M1: warm must be able to WRITE .pyc — drop PYTHONDONTWRITEBYTECODE
        # (which _base_env always sets) and point PYTHONPYCACHEPREFIX at the
        # per-side cache dir (never inside either tree)
        extra["PYTHONPYCACHEPREFIX"] = str(cache_dir)
        env = warm_child_env(_base_env(extra))
    else:
        extra["PYTHONDONTWRITEBYTECODE"] = "1"
        env = _base_env(extra)
    proc = _run([str(GATE_PY), str(child)], cwd=child.parent, env=env, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"import child failed ({tree}, warm={warm}): {proc.stderr[-500:]}")
    return float(proc.stdout.strip().splitlines()[-1]) * 1000.0


def _pyc_written(cache_dir: Path) -> bool:
    return any(cache_dir.rglob("*.pyc"))


def clear_dir(path: Path) -> None:
    """Finding 5: clear a directory (the warm phase clears its per-side
    pycache prefix before priming, so the .pyc assertion can only pass on
    files written by THIS generation's priming)."""
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _mirror_round(tree: Path, json_out: Path) -> dict:
    mirror = tree / "tools" / "restructure" / "r5_profile_mirror.py"
    if not mirror.exists():
        raise RuntimeError(f"r5 mirror missing in {tree} (each tree runs its own "
                           "path-bound copy; identical stages, both carry the "
                           "stage-E 3-tuple fix)")
    json_out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(GATE_PY), str(mirror), "--iterations", str(MIRROR_ITERATIONS),
           "--json", str(json_out)]
    proc = _run(cmd, cwd=tree, env=tree_env(tree), timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError(f"r5 mirror failed for {tree}: {proc.stderr[-800:]}")
    return _read_json(json_out)


def _harness_round(tree: Path, repeats: int) -> dict:
    cmd = [str(GATE_PY), str(Path(__file__).resolve()), "_fake-harness", "--repeats", str(repeats)]
    proc = _run(cmd, cwd=tree, env=tree_env(tree), timeout=1200)
    if proc.returncode != 0:
        raise RuntimeError(f"fake harness failed for {tree}: {proc.stderr[-800:]}")
    text = proc.stdout
    return json.loads(text[text.index("{"):text.rindex("}") + 1])


def _metric_record(before: list[float], after: list[float]) -> dict:
    mb = statistics.median(before) if before else float("nan")
    ma = statistics.median(after) if after else float("nan")
    if mb == 0 and ma == 0:
        # degenerate stage (identically zero on BOTH trees — no ratio exists
        # and nothing can have regressed)
        return {"n": [len(before), len(after)], "before_median": 0.0,
                "after_median": 0.0, "ratio": 1.0, "ci95": [1.0, 1.0],
                "verdict": "PASS", "degenerate": True}
    ratio = (ma / mb) if mb else float("inf")
    lo, hi = bootstrap_ratio_ci(before, after)
    return {
        "n": [len(before), len(after)],
        "before_median": mb, "after_median": ma, "ratio": ratio,
        "ci95": [lo, hi], "verdict": perf_verdict(lo, hi, ratio),
    }


def _load_series(out: Path) -> dict:
    f = out / "perf_series.json"
    return _read_json(f) if f.exists() else {"series": {}, "rounds_done": 0, "phases": {}}


def check_perf(before: Path, after: Path, out: Path,
               phase: str | None = None, rounds: int | None = None) -> dict:
    """Interleaved A/B. Split into FOREGROUND-sized phases; each phase appends
    to perf_series.json (before/after alternating). `run --only perf` without a
    phase computes the statistics + verdict from the accumulated series."""
    rounds_dir = out / "perf_rounds"
    rounds_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out / "perf_pycache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    blob = _load_series(out)
    series: dict[str, dict[str, list[float]]] = {
        m: {"before": list(d["before"]), "after": list(d["after"])}
        for m, d in blob.get("series", {}).items()}
    trees = (("before", before), ("after", after))

    def push(metric: str, side: str, value: float) -> None:
        series.setdefault(metric, {"before": [], "after": []})[side].append(value)

    def save() -> None:
        blob["series"] = series
        _write_json(out / "perf_series.json", blob)

    if phase == "new-generation":
        # finding 1: never pool run generations — archive the old series
        # (kept, never deleted) and start a fresh blob
        gen = int(blob.get("generation", 1))
        archive = out / f"perf_series.gen{gen}.json"
        if not archive.exists():
            archive.write_text(json.dumps(blob, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")
        old_perf = out / "perf.json"
        if old_perf.exists():
            perf_archive = out / f"perf.gen{gen}.json"
            if not perf_archive.exists():
                perf_archive.write_text(old_perf.read_text(encoding="utf-8"),
                                        encoding="utf-8")
        fresh = {"generation": gen + 1, "series": {}, "rounds_done": 0, "phases": {}}
        _write_json(out / "perf_series.json", fresh)
        return {"verdict": "DEFERRED", "phase": phase, "generation": gen + 1,
                "archived": archive.name,
                "note": "generation archived; run the perf phases again"}

    if phase in ("import-cold", "import-warm", "rerun-import-cold", "rerun-import-warm"):
        warm = "warm" in phase
        with tempfile.TemporaryDirectory(prefix="r3-perf-") as tmp:
            tmp = Path(tmp)
            for side, module in (("before", "computer_use_mcp.server"),
                                 ("after", "computer_use_mcp.server.app")):
                child = tmp / f"_import_child_{side}.py"
                child.write_text(IMPORT_CHILD_TEMPLATE.format(module=module),
                                 encoding="utf-8")
            key = "import_warm_ms" if warm else "import_cold_ms"
            if warm:
                # finding 5: clear the per-side prefixes first, so the .pyc
                # assertion can only pass on files written by THIS run's
                # priming (stale gen-N-1 leftovers cannot satisfy it)
                for side in ("before", "after"):
                    clear_dir(cache_dir / f"pycache-{side}")
                # M1: one priming import per side first, then assert the cache
                # actually holds .pyc files before any sample is taken
                for side, tree in trees:
                    _import_once(tmp / f"_import_child_{side}.py", tree, True,
                                 cache_dir / f"pycache-{side}")
                for side in ("before", "after"):
                    if not _pyc_written(cache_dir / f"pycache-{side}"):
                        raise RuntimeError(
                            f"warm-import metric invalid: no .pyc written under "
                            f"the {side} pycache prefix (PYTHONDONTWRITEBYTECODE "
                            f"leaked into the warm env)")
            for _ in range(IMPORT_N):
                for side, tree in trees:
                    ms = _import_once(tmp / f"_import_child_{side}.py", tree, warm,
                                      cache_dir / f"pycache-{side}")
                    push(key, side, ms)
        blob["phases"][phase] = blob["phases"].get(phase, 0) + IMPORT_N
        save()
        return {"verdict": "DEFERRED", "phase": phase, "samples_added": IMPORT_N,
                "note": f"run `--only perf` again to compute; phase appended to perf_series.json"}

    if phase in ("profile", "rerun-profile"):
        k = rounds or PROFILE_ROUNDS
        offset = int(blob.get("rounds_done", 0))
        for r in range(k):
            for side, tree in trees:
                mirror = _mirror_round(
                    tree, rounds_dir / f"gen{blob.get('generation', 1)}_r{offset + r + 1:02d}_{side}_mirror.json")
                for stage, st in mirror.items():
                    if isinstance(st, dict) and st.get("n"):
                        for key in ("p50", "p95"):
                            if key in st:
                                push(f"mirror.{stage}.{key}", side, float(st[key]))
                harness = _harness_round(tree, HARNESS_REPEATS)
                push("harness.captures_per_action", side,
                     float(harness["captures_per_action"]))
                lat = harness.get("metrics_snapshot", {}).get("latencies", {})
                for key, val in lat.items():
                    if isinstance(val, dict) and val.get("p50_ms"):
                        push(f"harness.{key}.p50", side, float(val["p50_ms"]))
                        push(f"harness.{key}.p95", side, float(val.get("p95_ms") or 0.0))
        blob["rounds_done"] = offset + k
        blob["phases"][phase] = blob["phases"].get(phase, 0) + k
        save()
        return {"verdict": "DEFERRED", "phase": phase, "rounds_added": k,
                "note": "run `--only perf` again to compute"}

    if phase == "repro-profile":
        k = rounds or PROFILE_ROUNDS
        repro = blob.setdefault("repro", {"series": {}, "rounds_done": 0})
        rseries = repro["series"]
        for _ in range(k):
            for side, tree in trees:
                harness = _harness_round(tree, HARNESS_REPEATS)
                rseries.setdefault("harness.captures_per_action",
                                   {"before": [], "after": []})[side].append(
                    float(harness["captures_per_action"]))
                lat = harness.get("metrics_snapshot", {}).get("latencies", {})
                for key, val in lat.items():
                    if isinstance(val, dict) and val.get("p50_ms"):
                        rseries.setdefault(f"harness.{key}.p50",
                                           {"before": [], "after": []})[side].append(
                            float(val["p50_ms"]))
                        rseries.setdefault(f"harness.{key}.p95",
                                           {"before": [], "after": []})[side].append(
                            float(val.get("p95_ms") or 0.0))
        repro["rounds_done"] = repro.get("rounds_done", 0) + k
        blob["series"] = series
        _write_json(out / "perf_series.json", blob)
        return {"verdict": "DEFERRED", "phase": phase, "rounds_added": k,
                "note": "harness-only repro series; run diagnose D4/final to evaluate"}

    # ---- final computation from the accumulated series ----
    if not series:
        result = {"verdict": "NOT_RUN", "problems": ["no perf series collected"],
                  "evidence": ["perf_series.json"]}
        _write_json(out / "perf.json", result)
        return result

    records = {m: _metric_record(d["before"], d["after"]) for m, d in sorted(series.items())}
    phases = blob.get("phases", {})
    noise_unresolved = []
    for m, rec in records.items():
        if rec["verdict"] == "NOISE" and rerun_done_for_metric(m, phases):
            # M5: the contract's 2xN rerun for THIS metric group is done; the
            # metric still does not meet the PASS rule -> NOISE-UNRESOLVED
            rec["verdict"] = "NOISE-UNRESOLVED"
            noise_unresolved.append(m)
    # finding 1: the first pass is DERIVED deterministically from the first N
    # samples of each series (contract N) — independent of compute order
    first_pass = first_pass_records(series)
    noise = [m for m, rec in records.items() if rec["verdict"] == "NOISE"]
    regression = [m for m, rec in records.items() if rec["verdict"] == "REGRESSION"]
    pending_rerun = [m for m, rec in records.items()
                     if rec["verdict"] == "NOISE"
                     and not rerun_done_for_metric(m, phases)]
    if regression:
        verdict = "REGRESSION"
    elif noise_unresolved:
        verdict = "NOISE-UNRESOLVED"
    elif pending_rerun:
        verdict = "NOISE"
    else:
        verdict = "PASS"
    result = {
        "verdict": verdict,
        "generation": blob.get("generation", 1),
        "problems": ([f"REGRESSION metrics: {regression}"] if regression else []) +
                    ([f"NOISE-UNRESOLVED metrics (Commander ruling required): "
                      f"{noise_unresolved}"] if noise_unresolved else []),
        "rerun_required": sorted(pending_rerun),
        "method": {
            "interleaved": "BEFORE, AFTER, BEFORE, AFTER ... per sample / per round",
            "import_child": {"before": "import computer_use_mcp.server",
                             "after": "import computer_use_mcp.server.app"},
            "import_n_per_side": IMPORT_N,
            "profile_rounds_per_side": blob.get("rounds_done", 0),
            "mirror": ("each tree's OWN tools/restructure/r5_profile_mirror.py — the "
                       "mirror is path-bound (flat vs layered module names), the two "
                       "copies replicate the same stages in the same order and both "
                       "carry the stage-E 3-tuple fix; benchmarks/r5_speed_profile.py "
                       "itself is NOT used (stale stage-E unpack is WP11's to fix)"),
            "harness": "in-tool _fake-harness (flat OR layered imports), captures/action + Metrics p50/p95",
            "mirror_iterations": MIRROR_ITERATIONS, "harness_repeats": HARNESS_REPEATS,
            "bootstrap": {"resamples": BOOTSTRAP_RESAMPLES, "seed": BOOTSTRAP_SEED},
            "verdict_rule": "PASS ci_hi<=1.02; NOISE median<=1.02<ci_hi (re-run once at 2xN); REGRESSION ci_lo>1.02 -> STOP",
            "ceiling": CEILING,
        },
        "metrics_first_pass": first_pass,
        "metrics_final": records,
        "regression_metrics": regression,
        "noise_unresolved": [
            {"metric": m, "ratio": records[m]["ratio"],
             "ci95": records[m]["ci95"],
             "n": records[m]["n"]}
            for m in noise_unresolved],
        "noise_metrics_after_rerun": noise_unresolved,
        "evidence": ["perf_series.json", "perf_rounds/", "perf_pycache/"],
    }
    _write_json(out / "perf.json", result)
    return result


# ---------------------------------------------------------------------------
# diagnosis of the perf STOP finding (measurement only; nothing tuned)
# ---------------------------------------------------------------------------

DIAG_CALLS = 20000
DIAG_ROUNDS = 20
DIAG_WARMUP = 500
DIAG_PROFILE_EXECS = 600

DIAG_ENV_CHILD = '''"""_diag-env: import the harness's exact import set, report env size."""
import gc
import json
import sys

sys.path.insert(0, r"{tools_dir}")

def imp(flat, layered):
    try:
        return __import__(flat, fromlist=["*"])
    except ImportError:
        return __import__(layered, fromlist=["*"])

agent_mod = imp("computer_use_mcp.agent", "computer_use_mcp.pipeline.agent")
audit_mod = imp("computer_use_mcp.audit", "computer_use_mcp.security.audit")
backend_mod = imp("computer_use_mcp.backend", "computer_use_mcp.desktop.fake_backend")
limits_mod = imp("computer_use_mcp.limits", "computer_use_mcp.core.limits")
models_mod = imp("computer_use_mcp.models", "computer_use_mcp.core.models")
obs_mod = imp("computer_use_mcp.observation", "computer_use_mcp.vision.observation")
safety_mod = imp("computer_use_mcp.safety", "computer_use_mcp.security.safety")
state_mod = imp("computer_use_mcp.state", "computer_use_mcp.core.state")

gc.collect()
rss_mb = None
try:
    import psutil  # only used if already installed
    rss_mb = psutil.Process().memory_info().rss / 1048576.0
except ImportError:
    pass
print(json.dumps({{
    "sys_modules": len(sys.modules),
    "cum_modules": sum(1 for m in sys.modules if m.startswith("computer_use_mcp")),
    "gc_objects": len(gc.get_objects()),
    "gc_count": list(gc.get_count()),
    "rss_mb": rss_mb,
}}))
'''

DIAG_DIRECT_CHILD = '''"""_diag-direct: per-call cost of the harness's timed bracket
(agent._backend_execute on the perf_harness CountingFlowBackend world)."""
import gc
import json
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, r"{tools_dir}")

def imp(flat, layered):
    try:
        return __import__(flat, fromlist=["*"])
    except ImportError:
        return __import__(layered, fromlist=["*"])

import base64
import io

from PIL import Image

agent_mod = imp("computer_use_mcp.agent", "computer_use_mcp.pipeline.agent")
audit_mod = imp("computer_use_mcp.audit", "computer_use_mcp.security.audit")
backend_mod = imp("computer_use_mcp.backend", "computer_use_mcp.desktop.fake_backend")
limits_mod = imp("computer_use_mcp.limits", "computer_use_mcp.core.limits")
models_mod = imp("computer_use_mcp.models", "computer_use_mcp.core.models")
obs_mod = imp("computer_use_mcp.observation", "computer_use_mcp.vision.observation")
safety_mod = imp("computer_use_mcp.safety", "computer_use_mcp.security.safety")
state_mod = imp("computer_use_mcp.state", "computer_use_mcp.core.state")

notepad = models_mod.WindowInfo(hwnd=12, pid=500, process_name="notepad.exe",
                                window_class="Notepad", title="Untitled - Notepad")
actions = [
    models_mod.GroundedAction(action="hotkey", keys=["win", "r"], confidence=1.0),
    models_mod.GroundedAction(action="type", text="notepad", confidence=1.0),
    models_mod.GroundedAction(action="keypress", keys=["enter"], confidence=1.0),
    models_mod.GroundedAction(action="type", text="hello from the launched app",
                              confidence=1.0),
]


class CountingFlowBackend(backend_mod.FakeComputerBackend):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.capture_count = 0

    def observe(self, monitor_index=None):
        self.capture_count += 1
        observation = super().observe(monitor_index)
        observation.active_window = notepad.title
        observation.active_window_info = notepad.model_copy()
        color = "white" if len(self.executed) % 2 == 0 else "black"
        image = Image.new("RGB", (self.width, self.height), color)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        observation.image_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
        return observation


backend = CountingFlowBackend()
backend.set_windows([notepad])
stop = state_mod.StopToken()
agent = agent_mod.ComputerUseAgent(
    backend, provider=None, safety=safety_mod.SafetyPolicy(),
    task=state_mod.TaskState(), stop=stop,
    limits=limits_mod.Limits(max_actions={calls} + 10,
                             max_task_seconds=3600.0).validate(),
    metrics=audit_mod.Metrics(),
)
agent.observation = obs_mod.ObservationEngine(backend)

for i in range({warmup}):
    agent._backend_execute(actions[i % len(actions)], stop)

gc.collect()
if "{gc_mode}" == "disable":
    gc.disable()
elif "{gc_mode}" == "freeze":
    gc.freeze()
    gc.disable()

ns_tight = []
pc = time.perf_counter_ns
for i in range({calls}):
    a = actions[i % len(actions)]
    t0 = pc()
    agent._backend_execute(a, stop)
    ns_tight.append(pc() - t0)

# pressured loop: per-iteration garbage mimicking the harness's observe payload
# (the PNG encode allocates many small objects + ~200KB buffers between
# brackets), so the bracket is timed under the harness's GC/allocation context
ns_pressured = []
for i in range({calls}):
    a = actions[i % len(actions)]
    junk = [dict(x=i, y=j, tag="observe-payload") for j in range(64)]
    blob = bytes(200_000)
    t0 = pc()
    agent._backend_execute(a, stop)
    ns_pressured.append(pc() - t0)
    del junk, blob

def _ms(ns_list):
    us = sorted(x / 1000.0 for x in ns_list)
    n = len(us)
    return {{"calls": n, "p50_us": us[n // 2], "p95_us": us[int(0.95 * (n - 1))],
             "mean_us": sum(us) / n}}

print(json.dumps({{
    "world": "CountingFlowBackend(FakeComputerBackend) + agent._backend_execute "
             "(the exact perf_harness bracket)",
    "gc_mode": "{gc_mode}",
    "tight": _ms(ns_tight),
    "pressured": _ms(ns_pressured),
}}))
'''

DIAG_PROFILE_CHILD = '''"""_diag-profile: cProfile of the harness flow, top functions + call counts."""
import cProfile
import io
import json
import pstats
import sys
from types import SimpleNamespace

sys.path.insert(0, r"{tools_dir}")

def imp(flat, layered):
    try:
        return __import__(flat, fromlist=["*"])
    except ImportError:
        return __import__(layered, fromlist=["*"])

import asyncio
import base64

agent_mod = imp("computer_use_mcp.agent", "computer_use_mcp.pipeline.agent")
audit_mod = imp("computer_use_mcp.audit", "computer_use_mcp.security.audit")
backend_mod = imp("computer_use_mcp.backend", "computer_use_mcp.desktop.fake_backend")
limits_mod = imp("computer_use_mcp.limits", "computer_use_mcp.core.limits")
models_mod = imp("computer_use_mcp.models", "computer_use_mcp.core.models")
obs_mod = imp("computer_use_mcp.observation", "computer_use_mcp.vision.observation")
safety_mod = imp("computer_use_mcp.safety", "computer_use_mcp.security.safety")
state_mod = imp("computer_use_mcp.state", "computer_use_mcp.core.state")

from PIL import Image

notepad = models_mod.WindowInfo(hwnd=12, pid=500, process_name="notepad.exe",
                                window_class="Notepad", title="Untitled - Notepad")
flows = [
    models_mod.GroundedAction(action="hotkey", keys=["win", "r"], confidence=1.0),
    models_mod.GroundedAction(action="type", text="notepad", confidence=1.0),
    models_mod.GroundedAction(action="keypress", keys=["enter"], confidence=1.0),
    models_mod.GroundedAction(action="type", text="hello from the launched app",
                              confidence=1.0),
]


class CountingFlowBackend(backend_mod.FakeComputerBackend):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.capture_count = 0

    def observe(self, monitor_index=None):
        self.capture_count += 1
        observation = super().observe(monitor_index)
        observation.active_window = notepad.title
        observation.active_window_info = notepad.model_copy()
        color = "white" if len(self.executed) % 2 == 0 else "black"
        image = Image.new("RGB", (self.width, self.height), color)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        observation.image_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
        return observation


backend = CountingFlowBackend()
backend.set_windows([notepad])
stop = state_mod.StopToken()


def make_agent():
    return agent_mod.ComputerUseAgent(
        backend, provider=None, safety=safety_mod.SafetyPolicy(),
        task=state_mod.TaskState(), stop=stop,
        limits=limits_mod.Limits(max_actions=500,
                                 max_task_seconds=3600.0).validate(),
        metrics=audit_mod.Metrics(),
    )


agent = make_agent()
agent.observation = obs_mod.ObservationEngine(backend)
state = SimpleNamespace(
    dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
    max_steps={execs} + 10, step_count=0, require_approval=False,
    max_retries_per_action=1,
)

profiler = cProfile.Profile()
profiler.enable()
n = 0
# max_actions clamps to 500 -> a fresh agent per 500-action segment; the
# profiler accumulates across segments (call counts stay comparable)
while n < {execs}:
    agent = make_agent()
    agent.observation = obs_mod.ObservationEngine(backend)
    state = SimpleNamespace(
        dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
        max_steps=500, step_count=0, require_approval=False,
        max_retries_per_action=1,
    )
    for _ in range(500 // len(flows)):
        if n >= {execs}:
            break
        for action in flows:
            asyncio.run(agent.run_single(state, action))
            n += 1
profiler.disable()
stats = pstats.Stats(profiler, stream=io.StringIO())

import os as _os
import sys as _sys
_equivalence_dir = r"{tools_dir}"
if _equivalence_dir not in _sys.path:
    _sys.path.insert(0, _equivalence_dir)
import equivalence as _eq
_SRC_PREFIX = "{src_prefix}"
_LAYERS = ("core/", "security/", "desktop/", "vision/", "pipeline/", "planning/", "server/")


def _key(k):
    f, lineno, fn = k
    p = _eq.normalize_code_file(f.replace("\\\\", "/"), _SRC_PREFIX)
    if "r3-diag-" in p:
        return f"(diag_child):{{lineno}}({{fn}})"
    return f"{{p}}:{{lineno}}({{fn}})"


rows = sorted(stats.stats.items(), key=lambda kv: kv[1][3], reverse=True)[:25]
top = [
    {{"func": _key(k), "cum_ms": round(v[3] * 1000.0, 3),
      "tot_ms": round(v[2] * 1000.0, 3), "calls": v[1], "primitive": v[0]}}
    for k, v in rows
]
counts = _eq.raw_counts_from_entries(profiler.getstats(), _SRC_PREFIX)
string_qual_counts = {{k: v for k, v in counts.items() if k.startswith("<string>:")}}
import gc as _gc
import types as _types
string_code_objects = [
    {{"firstlineno": c.__code__.co_firstlineno, "name": c.__code__.co_name,
      "qualname": getattr(c, "__qualname__", ""), "module": c.__module__}}
    for c in _gc.get_objects()
    if isinstance(c, _types.FunctionType) and c.__code__.co_filename == "<string>"
]
print(json.dumps({{"executions": n, "top_by_cumtime": top, "call_counts": counts,
                   "string_qual_counts": string_qual_counts,
                   "string_code_objects": string_code_objects}}))
'''


def normalize_code_file(filename: str, src_prefix: str) -> str:
    """Tree-relative, layer-stripped module file for a code object. The layer
    strip is SEGMENT-ANCHORED (no substring replace, so `server.py` never
    matches the `server/` layer)."""
    p = filename.replace("\\", "/")
    if src_prefix and p.startswith(src_prefix):
        p = "computer_use_mcp/" + p[len(src_prefix):]
    return _ANCHORED_LAYER_RE.sub("", p)


LAYER_DIRS = ("core/", "security/", "desktop/", "vision/", "pipeline/",
              "planning/", "server/")
_ANCHORED_LAYER_RE = re.compile(
    r"(?:^|/)(?:core|security|desktop|vision|pipeline|planning|server)(?=/)")


def raw_code_key(code, src_prefix: str) -> str:
    """(normalized file, firstlineno, qualname) label from a RAW code object —
    bypasses pstats, whose label dict ASSIGNS per (filename, lineno, name) and
    silently drops all-but-one of the distinct dataclass `<string>:2(__init__)`
    code objects before this tool ever sees them. Built-in entries carry a
    plain string label instead of a code object — passed through as-is."""
    if isinstance(code, str):
        return code
    qual = getattr(code, "co_qualname", None) or code.co_name
    return (f"{normalize_code_file(code.co_filename, src_prefix)}:"
            f"{code.co_firstlineno}({qual})")


def raw_counts_from_entries(entries, src_prefix: str) -> dict[str, int]:
    """Call counts straight from profiler.getstats() entries (each entry has
    .code and .callcount); colliding normalized keys are summed defensively."""
    out: dict[str, int] = {}
    for entry in entries:
        key = raw_code_key(entry.code, src_prefix)
        out[key] = out.get(key, 0) + entry.callcount
    return out


def diag_round_contaminated(p50_us: float, p95_us: float) -> bool:
    """A D1/D2 round is contaminated if a dispatch-gap/settle sleep leaked into
    the timed loop (p50 above 200 us or p95 above 2 ms on the gap-free mix)."""
    return p50_us > 200.0 or p95_us > 2000.0


def _diag_child(script: str, tree: Path, json_out: Path, env_extra: dict | None = None,
                **fmt) -> dict:
    json_out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="r3-diag-") as tmp:
        child = Path(tmp) / "_diag_child.py"
        child.write_text(script.format(
            tools_dir=str(TOOL_DIR),
            benchmarks_dir=str(Path(tree) / "benchmarks"),
            src_prefix=(Path(tree) / "src" / "computer_use_mcp").as_posix() + "/",
            **fmt), encoding="utf-8")
        env = tree_env(tree, dict(env_extra or {}))
        proc = _run([str(GATE_PY), str(child)], cwd=tree, env=env, timeout=900)
    if proc.returncode != 0:
        raise RuntimeError(f"diag child failed ({tree.name}): {proc.stderr[-1200:]}")
    text = proc.stdout
    payload = json.loads(text[text.index("{"):text.rindex("}") + 1])
    payload["_tree"] = str(tree)
    return payload


#: the fake backend sleeps KEY_DISPATCH_GAP_SECONDS (0.05 s) on every keypress
#: and a focus settle after hotkeys; both are REAL sleeps that would dominate
#: the per-call timing. Zeroed identically on BOTH sides (env only, no code
#: change); the timed mix stays the harness's.
DIAG_GAP_FREE_ENV = {"CORTEX_KEY_DISPATCH_GAP": "0",
                     "CORTEX_FOCUS_SETTLE_SECONDS": "0"}


def _diag_stage_rounds(before: Path, after: Path, out: Path, stage: str,
                       gc_mode: str, rounds: int) -> dict:
    """Interleaved per-round fresh-interpreter children; ratio + CI across rounds.
    Re-invocations with the same stage APPEND rounds to the stored series.
    Each child reports a TIGHT loop (pure bracket) and a PRESSURED loop (the
    bracket under the harness's allocation/GC context)."""
    path = out / f"diag_{stage}.json"
    if path.exists():
        prev = _read_json(path)
        series = prev["series"]
        done = prev.get("rounds_done", 0)
    else:
        series = {f"{loop}.{stat}_us": {"before": [], "after": []}
                  for loop in ("tight", "pressured")
                  for stat in ("p50", "p95")}
        done = 0
    worlds = set()
    for r in range(rounds):
        for side, tree in (("before", before), ("after", after)):
            payload = _diag_child(DIAG_DIRECT_CHILD, tree,
                                  out / f"diag_tmp_{stage}.json",
                                  env_extra=DIAG_GAP_FREE_ENV,
                                  calls=DIAG_CALLS, warmup=DIAG_WARMUP,
                                  gc_mode=gc_mode)
            worlds.add(payload["world"])
            if diag_round_contaminated(payload["tight"]["p50_us"],
                                       payload["tight"]["p95_us"]):
                raise RuntimeError(
                    f"{stage} round {done + r + 1} ({side}) CONTAMINATED: "
                    f"tight p50={payload['tight']['p50_us']}us "
                    f"p95={payload['tight']['p95_us']}us — a dispatch-gap/"
                    f"focus-settle sleep leaked into the timed loop "
                    f"(env: {DIAG_GAP_FREE_ENV})")
            for loop in ("tight", "pressured"):
                for stat in ("p50", "p95"):
                    series[f"{loop}.{stat}_us"][side].append(
                        payload[loop][f"{stat}_us"])
    records = {m: _metric_record(d["before"], d["after"]) for m, d in series.items()}
    result = {"stage": stage, "gc_mode": gc_mode,
              "rounds_done": done + rounds, "rounds_per_side": done + rounds,
              "calls_per_round": DIAG_CALLS,
              "loops": ["tight (pure bracket)", "pressured (harness GC/allocation context)"],
              "worlds": sorted(worlds),
              "method": "BEFORE/AFTER interleaved, fresh interpreter per round, "
                        "time.perf_counter_ns around agent._backend_execute (the "
                        "exact perf_harness bracket) on the harness 4-action mix; "
                        "CORTEX_KEY_DISPATCH_GAP=0 and CORTEX_FOCUS_SETTLE_SECONDS=0 "
                        "set in BOTH children's env (env only, no code change) so "
                        "no real sleep is timed; rounds contaminated above "
                        "tight p50>200us or p95>2ms fail the stage loudly",
              "metrics": records,
              "series": series}
    _write_json(path, result)
    return result


def _diag_stage_env(before: Path, after: Path, out: Path) -> dict:
    per_side = {}
    for side, tree in (("before", before), ("after", after)):
        per_side[side] = _diag_child(DIAG_ENV_CHILD, tree, out / "diag_tmp_env.json")
    diffs = {k: (per_side["after"][k] if not isinstance(per_side["after"][k], list)
                 else None) for k in per_side["after"] if k != "_tree"}
    result = {"stage": "D3", "per_side": per_side,
              "absolute_deltas_after_minus_before": diffs,
              "rss_note": "psutil not installed in the gate venv -> RSS skipped"}
    _write_json(out / "diag_D3.json", result)
    return result


def _diag_stage_profile(before: Path, after: Path, out: Path,
                        execs: int = DIAG_PROFILE_EXECS) -> dict:
    per_side = {}
    for side, tree in (("before", before), ("after", after)):
        per_side[side] = _diag_child(DIAG_PROFILE_CHILD, tree,
                                     out / "diag_tmp_prof.json",
                                     execs=execs)
    counts_b = per_side["before"]["call_counts"]
    counts_a = per_side["after"]["call_counts"]
    qual_b = per_side["before"].get("string_qual_counts", {})
    qual_a = per_side["after"].get("string_qual_counts", {})

    def name_key(k: str) -> tuple[str, str]:
        # "path:lineno(qualname)" -> (path, qualname): line numbers shift with
        # the import-line rewrites; built-in string labels (no colon) pass
        # through whole so they pair by identity
        if ":" not in k:
            return (k, "")
        path_part, rest = k.rsplit(":", 1)
        qual = rest.split("(", 1)[1].rstrip(")") if "(" in rest else rest
        return (path_part, qual)

    def totals(counts: dict) -> dict:
        out: dict[tuple[str, str], int] = {}
        for k, v in counts.items():
            key = name_key(k)
            out[key] = out.get(key, 0) + v
        return out

    tb, ta = totals(counts_b), totals(counts_a)
    all_funcs = set(tb) | set(ta)
    name_diffs = [{"func": f"{p}({fn})", "before": tb.get((p, fn), 0),
                   "after": ta.get((p, fn), 0),
                   "delta": ta.get((p, fn), 0) - tb.get((p, fn), 0)}
                  for (p, fn) in sorted(all_funcs)
                  if tb.get((p, fn), 0) != ta.get((p, fn), 0)]
    name_diffs.sort(key=lambda d: -abs(d["delta"]))
    # per-qualname comparison of every <string> code object (raw getstats
    # counts: distinct dataclass __init__ code objects stay distinct)
    all_quals = sorted(set(qual_b) | set(qual_a))
    qual_rows = [{"key": k, "before": qual_b.get(k, 0), "after": qual_a.get(k, 0)}
                 for k in all_quals]
    result = {"stage": "D5", "executions_per_side": execs,
              "top_by_cumtime": {s: per_side[s]["top_by_cumtime"] for s in per_side},
              "string_code_objects": {s: per_side[s].get("string_code_objects")
                                      for s in per_side},
              "string_qual_counts": qual_rows,
              "string_qual_diffs": [r for r in qual_rows
                                    if r["before"] != r["after"]],
              "name_keyed_diff_nonzero_total": len(name_diffs),
              "name_keyed_diffs_nonzero": name_diffs[:50],
              "note": ("counts come from RAW profiler.getstats() entries keyed by "
                       "(normalized co_filename, co_firstlineno, co_qualname) — "
                       "NOT from pstats, whose label dict assigns per (file, "
                       "line, name) and drops all-but-one of the distinct "
                       "dataclass <string>:2(__init__) code objects; the "
                       "name-keyed comparison ignores line-number offsets"),
              "call_count_diff_nonzero_total": len(name_diffs),
              "call_count_diffs_nonzero": name_diffs[:50]}
    _write_json(out / "diag_D5.json", result)
    return result


def _diag_stage_d6(before: Path, after: Path, out: Path, rounds: int) -> dict:
    """D6: mirror-only repro under the ORIGINAL perf protocol — each tree's
    own r5_profile_mirror.py, same iterations, BEFORE/AFTER interleaved, fresh
    interpreter per round, rounds appended across invocations (B1 waiver
    applies: screen-READ only)."""
    path = out / "diag_D6.json"
    if path.exists():
        prev = _read_json(path)
        series = prev["series"]
        done = prev.get("rounds_done", 0)
    else:
        series = {}
        done = 0
    for r in range(rounds):
        for side, tree in (("before", before), ("after", after)):
            mirror = _mirror_round(tree, out / "perf_rounds" /
                                   f"gen_d6_r{done + r + 1:02d}_{side}_mirror.json")
            for stage, st in mirror.items():
                if isinstance(st, dict) and st.get("n"):
                    for key in ("p50", "p95"):
                        if key in st:
                            series.setdefault(f"mirror.{stage}.{key}",
                                              {"before": [], "after": []})[side].append(
                                float(st[key]))
    records = {m: _metric_record(d["before"], d["after"])
               for m, d in sorted(series.items())}
    focus = {}
    for m in ("mirror.verify_diff_only.p50", "mirror.verify_diff_only.p95",
              "mirror.composite_action_two_observes_plus_verify.p95",
              "mirror.composite_three_observes_digest_verify.p95"):
        if m in records:
            rec = records[m]
            focus[m] = {"ratio": rec["ratio"], "ci95": rec["ci95"],
                        "verdict": rec["verdict"],
                        "reproduces_ci_lo_gt_1": rec["ci95"][0] > 1.0}
    result = {"stage": "D6", "rounds_done": done + rounds,
              "rounds_per_side": done + rounds,
              "method": ("mirror-only repro under the ORIGINAL perf protocol "
                         "(same mirror, same iterations, BEFORE/AFTER "
                         "interleaved, fresh interpreter per round) with ONE "
                         "deviation: the harness round that follows each "
                         "mirror round in the perf protocol is OMITTED here "
                         "(it is covered separately by D4); screen-READ only "
                         "under the recorded B1 waiver"),
              "focus": focus, "metrics": records, "series": series}
    _write_json(path, result)
    return result


def parse_importtime_stderr(text: str) -> dict:
    """D7: parse `-X importtime` stderr lines
    ("import time: self[us] | cumulative[us] | name"; the unit token is
    tolerated) into {total_us, modules: {name: {self_us, cum_us}}}.
    total_us is the largest cumulative (the entry import)."""
    modules: dict[str, dict] = {}
    for m in re.finditer(r"import time:\s+(\d+)(?:\s*us)?\s*\|\s*(\d+)"
                         r"(?:\s*us)?\s*\|\s*(\S.+)", text):
        self_us, cum_us, name = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        modules[name] = {"self_us": self_us, "cum_us": cum_us}
    total = max((v["cum_us"] for v in modules.values()), default=0)
    return {"total_us": total, "modules": modules}


IMPORTTIME_CHILD = '''"""D7 child: import the server entry once (parent passes -X importtime)."""
import {module}  # noqa: E402,F401
'''


def _diag_stage_d7(before: Path, after: Path, out: Path,
                   rounds: int = 10) -> dict:
    """D7: import-time breakdown. Warm (.pyc primed per side under the side's
    PYTHONPYCACHEPREFIX — cleared first, priming return code checked, .pyc
    presence asserted), 10 runs per side, BEFORE/AFTER INTERLEAVED per round,
    fresh interpreter per run with `-X importtime`. Deterministic and
    read-only; no desktop contact."""
    cache_dir = out / "perf_pycache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    trees = (("before", "computer_use_mcp.server", before),
             ("after", "computer_use_mcp.server.app", after))
    totals: dict[str, list[float]] = {"before": [], "after": []}
    module_self: dict[str, dict[str, list[float]]] = {
        "before": {}, "after": {}}
    module_cum: dict[str, dict[str, list[float]]] = {
        "before": {}, "after": {}}
    with tempfile.TemporaryDirectory(prefix="r3-d7-") as tmp:
        tmp = Path(tmp)
        for _side, module, _tree in trees:
            child = tmp / f"_importtime_child_{_side}.py"
            child.write_text(IMPORTTIME_CHILD.format(module=module),
                             encoding="utf-8")
        prime_envs: dict[str, dict] = {}
        for side, _module, tree in trees:
            prefix = cache_dir / f"pycache-{side}"
            clear_dir(prefix)
            child = tmp / f"_importtime_child_{side}.py"
            prime_env = warm_child_env(_base_env({
                "PYTHONPATH": str(tree / "src"),
                "PYTHONPYCACHEPREFIX": str(prefix)}))
            prime_envs[side] = prime_env
            # priming: one warm-env run so the .pyc cache is genuine; check
            # the return code AND assert .pyc presence (VAL-WP-R3-04)
            prime = _run([str(GATE_PY), str(child)], cwd=tmp, env=prime_env,
                         timeout=180)
            if prime.returncode != 0:
                raise RuntimeError(f"D7 priming failed ({side}): "
                                   f"{prime.stderr[-500:]}")
            if not _pyc_written(prefix):
                raise RuntimeError(f"D7 warm guarantee invalid ({side}): no "
                                   f".pyc written under the pycache prefix")
        # interleaved per round: BEFORE, AFTER, BEFORE, AFTER ...
        for _ in range(rounds):
            for side, _module, tree in trees:
                proc = _run([str(GATE_PY), "-X", "importtime",
                             str(tmp / f"_importtime_child_{side}.py")],
                            cwd=tmp, env=prime_envs[side], timeout=180)
                if proc.returncode != 0:
                    raise RuntimeError(f"D7 child failed ({side}): "
                                       f"{proc.stderr[-500:]}")
                parsed = parse_importtime_stderr(proc.stderr)
                totals[side].append(float(parsed["total_us"]))
                for name, vals in parsed["modules"].items():
                    module_self[side].setdefault(name, []).append(
                        float(vals["self_us"]))
                    module_cum[side].setdefault(name, []).append(
                        float(vals["cum_us"]))

    def med(d: dict[str, list[float]]) -> dict[str, float]:
        return {k: statistics.median(v) for k, v in d.items()}

    self_b, self_a = med(module_self["before"]), med(module_self["after"])
    only_after = sorted(set(self_a) - set(self_b))
    only_before = sorted(set(self_b) - set(self_a))
    same_name = sorted(set(self_b) & set(self_a))
    # net Sigma-self over computer_use_mcp.* per side, and the CONTROL: all
    # other modules; plus the reconciliation against the measured total
    prod = re.compile(r"^computer_use_mcp\b")
    prod_b = sum(v for k, v in self_b.items() if prod.match(k))
    prod_a = sum(v for k, v in self_a.items() if prod.match(k))
    other_b = sum(v for k, v in self_b.items() if not prod.match(k))
    other_a = sum(v for k, v in self_a.items() if not prod.match(k))
    total_b, total_a = med(totals)["before"], med(totals)["after"]
    deltas = sorted(
        [{"module": m, "self_delta_us": self_a.get(m, 0.0) - self_b.get(m, 0.0),
          "before_self_us": self_b.get(m, 0.0), "after_self_us": self_a.get(m, 0.0)}
         for m in set(self_b) | set(self_a) if prod.match(m)],
        key=lambda r: -abs(r["self_delta_us"]))
    # noise floor: median |delta| of same-name modules present in BOTH trees
    same_deltas = sorted(abs(self_a.get(m, 0.0) - self_b.get(m, 0.0))
                         for m in same_name)
    noise_floor_us = (same_deltas[len(same_deltas) // 2]
                      if same_deltas else 0.0)
    total_rec = _metric_record(totals["before"], totals["after"])
    result = {
        "stage": "D7",
        "rounds_per_side": rounds,
        "warm": True,
        "interleaved": True,
        "note": ("deterministic -X importtime breakdown; module NAMES differ "
                 "between trees by design (flat vs layered), so only_in_* "
                 "lists re-homed names — net them against each other; "
                 "cumulative times double-count imported children and are "
                 "NOT a cost measure (kept in only_in_after/only_in_before "
                 "as raw data only)"),
        "importtime_total": {"metric": "importtime.total_us",
                             **total_rec},
        "totals_us": totals,
        "sigma_self": {
            "computer_use_mcp_before_us": prod_b,
            "computer_use_mcp_after_us": prod_a,
            "computer_use_mcp_delta_us": prod_a - prod_b,
            "other_modules_before_us": other_b,
            "other_modules_after_us": other_a,
            "other_modules_delta_us": other_a - other_b,
            "reconciliation": {
                "sum_self_before_us": prod_b + other_b,
                "sum_self_after_us": prod_a + other_a,
                "measured_total_before_us": total_b,
                "measured_total_after_us": total_a,
                "residual_before_us": total_b - (prod_b + other_b),
                "residual_after_us": total_a - (prod_a + other_a),
                "note": ("UNEXPLAINED residual per side (measured total minus "
                         "Sigma-self); it differs by side and goes the "
                         "opposite way from the Sigma-self ratio; D7 is "
                         "noise-dominated and this residual is not attributed "
                         "to any module")},
        },
        "only_in_after": {"count": len(only_after), "modules": only_after,
                          "sum_self_us": sum(self_a.get(m, 0.0)
                                             for m in only_after)},
        "only_in_before": {"count": len(only_before), "modules": only_before,
                           "sum_self_us": sum(self_b.get(m, 0.0)
                                              for m in only_before)},
        "per_module_median_abs_delta_us": noise_floor_us,
        "per_module_note": ("per-module median |delta| over same-name modules "
                            "— NOT comparable to the sums above"),
        "same_name_module_count": len(same_name),
        "top_self_time_deltas_product_only": deltas[:10],
    }
    _write_json(out / "diag_D7.json", result)
    return result


def _diag_stage_repro(before: Path, after: Path, out: Path) -> dict:
    check_perf(before, after, out, phase="repro-profile", rounds=PROFILE_ROUNDS)
    blob = _load_series(out)
    repro = blob.get("repro", {"series": {}})
    records = {m: _metric_record(d["before"], d["after"])
               for m, d in sorted(repro.get("series", {}).items())}
    result = {"stage": "D4", "method": "fresh 15 interleaved harness rounds "
                                       "(2xN relative to the first 15), harness-only",
              "metrics": records}
    _write_json(out / "diag_D4.json", result)
    return result


def cmd_diagnose(args: argparse.Namespace) -> int:
    before = Path(args.before).resolve()
    after = Path(args.after).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    problems = preflight(before, after)
    if problems:
        for p in problems:
            print(f"[r3] PREFLIGHT FAIL: {p}")
        raise SystemExit("preflight failed: fix the tree binding before diagnosing")
    stage = args.stage
    rounds = int(getattr(args, "rounds", None) or DIAG_ROUNDS)
    if stage in ("D1", "D2-disable", "D2-freeze"):
        gc_mode = {"D1": "off", "D2-disable": "disable", "D2-freeze": "freeze"}[stage]
        res = _diag_stage_rounds(before, after, out, stage, gc_mode, rounds)
    elif stage == "D3":
        res = _diag_stage_env(before, after, out)
    elif stage == "D4":
        res = _diag_stage_repro(before, after, out)
    elif stage == "D5":
        res = _diag_stage_profile(before, after, out,
                                  execs=int(getattr(args, "execs", None)
                                            or DIAG_PROFILE_EXECS))
    elif stage == "D6":
        res = _diag_stage_d6(before, after, out, rounds=rounds)
    elif stage == "D7":
        res = _diag_stage_d7(before, after, out, rounds=rounds)
    elif stage == "final":
        res = _diag_final(before, after, out)
    else:
        raise SystemExit(f"unknown diagnose stage: {stage}")
    print(json.dumps({k: res[k] for k in res if k in
                      ("stage", "metrics", "verdict", "conclusion", "per_side",
                       "regressed_at_2x", "call_count_diff_nonzero_total")},
                     default=str)[:1200])
    return 0


def classify_d5_rows(rows: list[dict]) -> dict:
    """Classify D5 nonzero rows from the DATA: names are NORMALIZED first
    (0x… addresses -> 0xADDR, r3-diag-.../_diag_child.py -> (diag_child)), a
    row pairs with its mirrored delta on the normalized name, |delta| <= 8
    thread/lock/monotonic rows are timing-dependent lock/poll counts, and
    anything else is UNCLASSIFIED (named in the report)."""
    normalized = [{**r, "nfunc": normalize_d5_func(r["func"])} for r in rows]
    remaining = list(normalized)
    pairs: list[dict] = []
    used: set[int] = set()
    for i, r in enumerate(remaining):
        if i in used:
            continue
        # a real split-move pair carries a delta above the poll threshold;
        # small mirrored deltas are timing noise even on identical names
        if abs(r["delta"]) <= 8:
            continue
        for j in range(i + 1, len(remaining)):
            if j in used:
                continue
            q = remaining[j]
            if r["delta"] == -q["delta"] and r["nfunc"] == q["nfunc"]:
                pairs.extend([r, q])
                used.update({i, j})
                break
    polls = [r for i, r in enumerate(remaining) if i not in used
             and abs(r["delta"]) <= 8
             and re.search(r"thread|lock|monotonic|deque|Event|wait\b|"
                           r"can_screenshot|_OverlappedFuture|socket\.close",
                           r["nfunc"], re.IGNORECASE)]
    used_ids = {id(r) for r in pairs}
    polls = [r for r in polls if id(r) not in used_ids]
    classified_ids = used_ids | {id(r) for r in polls}
    unclassified = [r for r in remaining if id(r) not in classified_ids]
    return {"split_move_pairs": len(pairs) // 2, "pair_rows": pairs,
            "timing_lock_poll": len(polls), "poll_rows": polls,
            "unclassified": unclassified,
            "unclassified_names": [r["nfunc"] for r in unclassified]}


def _diag_final(before: Path, after: Path, out: Path) -> dict:
    """Merge the stage results into diagnose.json + the report Diagnosis section."""
    def load(name):
        f = out / f"diag_{name}.json"
        return _read_json(f) if f.exists() else None

    d1, d2d, d2f = load("D1"), load("D2-disable"), load("D2-freeze")

    def excess(stage):
        if not stage:
            return None
        rec = stage["metrics"]["tight.p50_us"]
        rec95 = stage["metrics"]["tight.p95_us"]
        recp = stage["metrics"]["pressured.p50_us"]
        recp95 = stage["metrics"]["pressured.p95_us"]
        return {"tight_p50_ratio": rec["ratio"], "tight_p50_ci": rec["ci95"],
                "tight_p95_ratio": rec95["ratio"],
                "pressured_p50_ratio": recp["ratio"], "pressured_p50_ci": recp["ci95"],
                "pressured_p95_ratio": recp95["ratio"]}

    # decision (M6: supporting evidence only, no causal claims, numbers computed)
    conclusion = "inconclusive (missing stages)"
    try:
        t1 = d1["metrics"]["tight.p50_us"]["ratio"]
        p1 = d1["metrics"]["pressured.p50_us"]["ratio"]
        d4d = load("D4") or {}
        d4rec = d4d.get("metrics", {}).get("harness.execution_ms.p95")
        d4p50 = d4d.get("metrics", {}).get("harness.execution_ms.p50")
        d5d = load("D5") or {}
        cls = classify_d5_rows(d5d.get("name_keyed_diffs_nonzero", []))
        d5_summary = (f"D5 {d5d.get('call_count_diff_nonzero_total', '?')} nonzero "
                      f"rows: {cls['split_move_pairs']} split-move +/- pairs, "
                      f"{cls['timing_lock_poll']} timing-dependent lock/poll "
                      f"counts, {len(cls['unclassified'])} unclassified"
                      + (f" ({', '.join(cls['unclassified_names'])})"
                         if cls["unclassified_names"] else ""))
        d4_excl = (d4rec["ci95"][1] - 1.0) * 100.0
        d1_tight_ci_hi = max(
            d1["metrics"][k]["ci95"][1]
            for k in ("tight.p50_us", "tight.p95_us"))
        d1_resolves = (d1_tight_ci_hi - 1.0) * 100.0
        if d1 is not None and d4rec is not None:
            conclusion = (
                f"NOT REPRODUCED; no code-path difference found. Supporting "
                f"evidence only (it cannot resolve <= 2%): D1/D2 direct per-call "
                f"bracket ratios {t1:.3f}/{p1:.3f} ({DIAG_CALLS} calls x rounds, "
                f"unchanged under gc.disable/gc.freeze); the recorded STOP "
                f"metric did NOT reproduce on a fresh interleaved 2xN harness "
                f"run WITHOUT the mirror (D4 harness.execution_ms.p95 ratio "
                f"{d4rec['ratio']:.3f}, CI {d4rec['ci95'][0]:.3f}-"
                f"{d4rec['ci95'][1]:.3f} — the upper bound does not exclude "
                f"+{d4_excl:.1f}%); {d5_summary}. The gap between the direct "
                f"bracket (~{d1['metrics']['tight.p50_us']['before_median']:.1f} "
                f"us) and the harness p50 "
                f"(~{ms_to_us(d4p50['before_median']):.1f} us computed from "
                f"D4 ms) is NOT "
                f"explained by these measurements; the timer resolution "
                f"cannot resolve below +{d1_resolves:.1f}% on the tight loop; "
                f"no GC/heap causation is established.")
    except (KeyError, TypeError):
        pass

    result = {
        "verdict": "DIAGNOSIS",
        "conclusion": conclusion,
        "D1_direct": excess(d1), "D1_full": d1,
        "D2_disable": excess(d2d), "D2_disable_full": d2d,
        "D2_freeze": excess(d2f), "D2_freeze_full": d2f,
        "D3_env": load("D3"), "D4_repro": load("D4"), "D5_profile": load("D5"),
        "D6_mirror_repro": load("D6"),
    }
    _write_json(out / "diagnose.json", result)

    lines = ["", "## Diagnosis", ""]
    lines.append(f"Conclusion: {conclusion}")
    for name, stage in (("D1 direct per-call (tight / pressured)", d1),
                        ("D2 gc.disable (tight / pressured)", d2d),
                        ("D2 gc.freeze (tight / pressured)", d2f)):
        if not stage:
            continue
        parts = []
        for loop in ("tight", "pressured"):
            p50 = stage["metrics"][f"{loop}.p50_us"]
            p95 = stage["metrics"][f"{loop}.p95_us"]
            parts.append(
                f"{loop}: p50 {p50['before_median']:.2f} -> {p50['after_median']:.2f} us "
                f"(ratio {p50['ratio']:.3f}, CI {p50['ci95'][0]:.3f}-{p50['ci95'][1]:.3f}, "
                f"{p50['verdict']}), p95 ratio {p95['ratio']:.3f} ({p95['verdict']})")
        lines.append(f"- {name}: " + "; ".join(parts))
    if d1 is not None:
        t = d1["metrics"]["tight.p50_us"]
        hp50 = (load("D4") or {}).get("metrics", {}).get("harness.execution_ms.p50")
        h_val = (f"~{ms_to_us(hp50['before_median']):.1f} us (computed from D4 ms)"
                 if hp50 else "n/a")
        tight_ci_hi = max(d1["metrics"][k]["ci95"][1]
                          for k in ("tight.p50_us", "tight.p95_us"))
        lines.append(f"- Sanity + resolution limits (supporting evidence only): the "
                     f"D1 bracket p50 (~{t['before_median']:.1f} us, computed) FAILED "
                     f"the same-order sanity check against the harness p50 "
                     f"({h_val}); D1/D2 tight-loop CIs reach "
                     f"{(tight_ci_hi - 1.0) * 100.0:.1f}% above 1.0, so they cannot "
                     f"resolve <= 2%; this gap is NOT explained by D1-D5.")
    d3 = load("D3")
    if d3:
        b, a = d3["per_side"]["before"], d3["per_side"]["after"]
        lines.append(f"- D3 env: sys.modules {b['sys_modules']} vs {a['sys_modules']}; "
                     f"computer_use_mcp modules {b['cum_modules']} vs {a['cum_modules']}; "
                     f"gc_objects {b['gc_objects']} vs {a['gc_objects']}; "
                     f"gc_count {b['gc_count']} vs {a['gc_count']} ({d3['rss_note']})")
    d4 = load("D4")
    if d4 and "harness.execution_ms.p95" in d4.get("metrics", {}):
        rec = d4["metrics"]["harness.execution_ms.p95"]
        lines.append(f"- D4 repro (harness ONLY, no mirror — protocol deviation "
                     f"from the original mirror+harness rounds): "
                     f"harness.execution_ms.p95 ratio "
                     f"{rec['ratio']:.3f} CI {rec['ci95'][0]:.3f}-{rec['ci95'][1]:.3f} "
                     f"({rec['verdict']}); CI hi {rec['ci95'][1]:.3f} does NOT "
                     f"exclude +{(rec['ci95'][1] - 1.0) * 100.0:.1f}%")
    d5 = load("D5")
    if d5:
        cls = classify_d5_rows(d5.get("name_keyed_diffs_nonzero", []))
        lines.append(f"- D5 cProfile: functions with changed call counts between "
                     f"sides: {d5['call_count_diff_nonzero_total']} — computed "
                     f"classification: {cls['split_move_pairs']} split-move +/- "
                     f"pairs, {cls['timing_lock_poll']} timing-dependent lock/poll "
                     f"counts, {len(cls['unclassified'])} unclassified "
                     f"(top diffs in diag_D5.json)")
        for u in cls["unclassified_names"]:
            lines.append(f"    - UNCLASSIFIED row: {u}")
        lines.append("    - Explanation (post-F1b): counts now come from RAW "
                     "profiler.getstats() entries keyed by (normalized file, "
                     "firstlineno, qualname) — pstats's label dict ASSIGNS per "
                     "(file, line, name) and had collapsed the distinct "
                     "dataclass <string> __init__ code objects last-wins, "
                     "which is what produced the collapsed <string>(__init__) "
                     "row.")
        qual_diffs = d5.get("string_qual_diffs", [])
        if not qual_diffs:
            lines.append("    - <string> code objects: EVERY dataclass "
                         "__init__ qualname has an EQUAL count on both sides "
                         "(see string_qual_counts) — the 900/300 was the "
                         "cProfile label collision, not a real difference.")
        else:
            lines.append("    - <string> code objects with differing counts:")
            for r in qual_diffs:
                lines.append(f"        - {r['key']}: before {r['before']} -> "
                             f"after {r['after']}")
        # finding 6: print NORMALIZED names with the classifier's OWN label
        labeled = ([(r, "split-move pair") for r in cls["pair_rows"]]
                   + [(r, "timing-dependent lock/poll count") for r in cls["poll_rows"]]
                   + [(r, "UNCLASSIFIED") for r in cls["unclassified"]])
        for r, mech in labeled[:10]:
            if "(diag_child)" in r["nfunc"] and abs(r["delta"]) <= 1:
                # NIT: each tree's own diag child defines make_agent once, so
                # a +/-1 delta on a (diag_child) row is a harmless per-child
                # artifact, not a call-graph difference
                mech = "harmless per-child artifact (each child defines its own copy)"
            lines.append(f"    - {r['nfunc']}: before {r['before']} -> after "
                         f"{r['after']} (delta {r['delta']}; {mech})")
        if not cls["pair_rows"] and not cls["poll_rows"] and not cls["unclassified"]:
            lines.append("    - none: identical call graph between the trees")
    d6 = load("D6")
    if d6:
        recs = d6.get("metrics", {})
        lines.append(f"- D6 mirror-only repro (ORIGINAL perf protocol: each "
                     f"tree's own r5_profile_mirror.py, same iterations, "
                     f"BEFORE/AFTER interleaved, fresh interpreter per round, "
                     f"{d6.get('rounds_done', '?')} rounds/side; screen-READ "
                     f"only under B1 waiver): {len(recs)} mirror metrics")
        focus = d6.get("focus", {})
        for m, f in focus.items():
            lines.append(f"    - {m}: ratio {f['ratio']:.3f} CI "
                         f"{f['ci95'][0]:.4f}-{f['ci95'][1]:.4f} "
                         f"({f['verdict']}); reproduces CI lo > 1: "
                         f"{f['reproduces_ci_lo_gt_1']}")
        for m, rec in sorted(recs.items()):
            lines.append(f"    - {m}: ratio {rec['ratio']:.3f} CI "
                         f"{rec['ci95'][0]:.4f}-{rec['ci95'][1]:.4f} "
                         f"({rec['verdict']})")
    lines.append("")
    lines.append(f"Diagnosis verdict: {conclusion} (see ## Diagnosis); "
                 "Commander ruling pending")
    lines.append("")
    report = out / "report.md"
    if report.exists():
        text = report.read_text(encoding="utf-8")
        section = "\n".join(lines) + "\n"
        idx = text.find("## Diagnosis")
        if idx != -1:
            # idempotent refresh: replace the existing section in place
            report.write_text(text[:idx] + section, encoding="utf-8")
        else:
            report.write_text(text + section, encoding="utf-8")
    return result


def cmd_fake_harness(args: argparse.Namespace) -> int:
    """Offline fake-backend flow: captures/action + Metrics p50/p95. Imports
    resolve against the FLAT layout first, then the layered layout, so the same
    code runs against both trees (perf_harness.py itself is flat-import-only)."""
    import asyncio
    import base64
    import io
    from types import SimpleNamespace

    from PIL import Image

    def imp(flat: str, layered: str):
        try:
            return __import__(flat, fromlist=["*"])
        except ImportError:
            return __import__(layered, fromlist=["*"])

    agent_mod = imp("computer_use_mcp.agent", "computer_use_mcp.pipeline.agent")
    audit_mod = imp("computer_use_mcp.audit", "computer_use_mcp.security.audit")
    backend_mod = imp("computer_use_mcp.backend", "computer_use_mcp.desktop.fake_backend")
    limits_mod = imp("computer_use_mcp.limits", "computer_use_mcp.core.limits")
    models_mod = imp("computer_use_mcp.models", "computer_use_mcp.core.models")
    obs_mod = imp("computer_use_mcp.observation", "computer_use_mcp.vision.observation")
    safety_mod = imp("computer_use_mcp.safety", "computer_use_mcp.security.safety")
    state_mod = imp("computer_use_mcp.state", "computer_use_mcp.core.state")

    notepad = models_mod.WindowInfo(hwnd=12, pid=500, process_name="notepad.exe",
                                    window_class="Notepad", title="Untitled - Notepad")
    flows = [
        models_mod.GroundedAction(action="hotkey", keys=["win", "r"], confidence=1.0),
        models_mod.GroundedAction(action="type", text="notepad", confidence=1.0),
        models_mod.GroundedAction(action="keypress", keys=["enter"], confidence=1.0),
        models_mod.GroundedAction(action="type", text="hello from the launched app",
                                  confidence=1.0),
    ]

    fake = backend_mod.FakeComputerBackend

    class CountingFlowBackend(fake):  # type: ignore[name-defined]
        def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
            super().__init__(**kwargs)
            self.capture_count = 0

        def observe(self, monitor_index=None):  # type: ignore[no-untyped-def]
            self.capture_count += 1
            observation = super().observe(monitor_index)
            observation.active_window = notepad.title
            observation.active_window_info = notepad.model_copy()
            color = "white" if len(self.executed) % 2 == 0 else "black"
            image = Image.new("RGB", (self.width, self.height), color)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            observation.image_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
            return observation

    repeats = max(1, args.repeats)
    backend = CountingFlowBackend()
    backend.set_windows([notepad])
    metrics = audit_mod.Metrics()
    agent = agent_mod.ComputerUseAgent(
        backend, provider=None, safety=safety_mod.SafetyPolicy(),
        task=state_mod.TaskState(), stop=state_mod.StopToken(),
        limits=limits_mod.Limits(max_actions=repeats * len(flows) + 10,
                                 max_task_seconds=3600.0).validate(),
        metrics=metrics,
    )
    agent.observation = obs_mod.ObservationEngine(backend)
    state = SimpleNamespace(
        dry_run=False, stopped=False, allowed_windows=[], min_confidence=0.0,
        max_steps=repeats * len(flows) + 10, step_count=0, require_approval=False,
        max_retries_per_action=1,
    )
    actions_run = 0
    for _ in range(repeats):
        for action in flows:
            outcome = asyncio.run(agent.run_single(state, action))
            assert outcome.kind == "executed", outcome
            actions_run += 1
    print(json.dumps({
        "mode": "fake backend, offline, serial",
        "repeats": repeats, "actions_run": actions_run,
        "captures_per_action": backend.capture_count / actions_run,
        "captures_total": backend.capture_count,
        "metrics_snapshot": metrics.snapshot(),
    }))
    return 0


# ---------------------------------------------------------------------------
# summary + report
# ---------------------------------------------------------------------------

CHECK_ORDER = ("inventory", "nodeids", "counts", "tools", "probe", "audit",
               "launch", "testdiff", "devtree", "perf")

# B1 (Commander waiver, recorded in report.md): the perf mirror instantiates
# LocalComputerBackend for screen-READING only (observe/mss/foreground/UIA —
# no execute, no input, no clipboard, no launch, no focus), the same
# sanctioned WP-R0 read-only protocol on both sides. Guarded statically by
# tests/test_restructure_equivalence_tool.py::test_mirror_reads_no_input.
B1_WAIVER_NOTE = ("B1 waiver (Commander): tools/restructure/r5_profile_mirror.py "
                  "reads the real desktop (mss grab, foreground window, UIA) — "
                  "READ only, no input/clipboard/launch/focus; sanctioned as the "
                  "WP-R0 baseline protocol, run identically on both sides; "
                  "statically guarded by test_mirror_reads_no_input.")
CHECK_FILES = {
    "inventory": "inventory.json", "nodeids": "nodeids.json", "counts": "counts.json",
    "tools": "tools.json", "probe": "probe.json", "audit": "audit.json",
    "launch": "launch.json", "testdiff": "testdiff.json", "devtree": "devtree.json",
    "perf": "perf.json",
}


def write_summary_and_report(out: Path, results: dict, meta: dict) -> dict:
    # M4: overall never defaults to PASS. INCOMPLETE if any check is missing /
    # NOT_RUN / DEFERRED; STOP on FAIL/REGRESSION/NOISE-UNRESOLVED (M5 lists
    # NOISE-UNRESOLVED metrics for an explicit Commander ruling); PASS only if
    # every check ran and every verdict is PASS.
    summary = {"meta": meta, "checks": {}, "overall": "PASS"}
    rows = ["# WP-R3 equivalence report", "",
            f"BEFORE `{meta['before']}` @ {BEFORE_GIT} · AFTER `{meta['after']}` @ {AFTER_GIT}",
            "", "| check | result | evidence |", "|---|---|---|"]
    existing = out / "report.md"
    diagnosis = ""
    if existing.exists():
        text = existing.read_text(encoding="utf-8")
        idx = text.find("## Diagnosis")
        if idx != -1:
            diagnosis = text[idx:]  # n2: regeneration preserves "## Diagnosis"
    verdicts: list[str] = []
    for name in CHECK_ORDER:
        res = results.get(name)
        if res is None:
            verdicts.append("NOT_RUN")
            summary["checks"][name] = {"result": "NOT_RUN"}
            rows.append(f"| {name} | NOT RUN | — |")
            continue
        verdict = res.get("verdict", "FAIL")
        verdicts.append(verdict)
        summary["checks"][name] = {"result": verdict, "problems": res.get("problems", [])}
        evidence = "; ".join(res.get("evidence", []))
        rows.append(f"| {name} | {verdict} | {evidence} |")
    # M4: ranked overall (STOP > INCOMPLETE > NOISE > PASS; unknown -> STOP)
    summary["overall"] = overall_verdict(verdicts)
    if results.get("perf", {}).get("noise_unresolved"):
        summary["overall"] = "STOP"
        summary["noise_unresolved"] = results["perf"]["noise_unresolved"]
    summary["checks"]["report"] = {"result": "PASS"}
    _write_json(out / "summary.json", summary)
    rows += ["", f"**Overall: {summary['overall']}**", ""]
    if summary.get("noise_unresolved"):
        rows.append("NOISE-UNRESOLVED metrics (Commander ruling required):")
        for r in summary["noise_unresolved"]:
            rows.append(f"- {r['metric']}: ratio {r['ratio']:.4f}, "
                        f"CI95 [{r['ci95'][0]:.4f}, {r['ci95'][1]:.4f}], "
                        f"n {r['n']}")
        rows.append("")
    perf_file = out / "perf.json"
    if perf_file.exists():
        pf = _read_json(perf_file)
        fp = pf.get("metrics_first_pass", {})
        fin = pf.get("metrics_final", {})
        rows += ["", "### First pass (first N per series) vs final", "",
                 "| metric | first n | first ratio (CI) | final n | final ratio (CI) | verdict |",
                 "|---|---|---|---|---|---|"]
        for m in sorted(fin):
            f_rec = fp.get(m, {})
            n_rec = fin[m]
            rows.append(
                f"| {m} | {f_rec.get('n', ['-'])[0]} | {f_rec.get('ratio', 0):.4f} "
                f"([{f_rec.get('ci95', [0, 0])[0]:.4f}, "
                f"{f_rec.get('ci95', [0, 0])[1]:.4f}]) | {n_rec['n'][0]} | "
                f"{n_rec['ratio']:.4f} ([{n_rec['ci95'][0]:.4f}, "
                f"{n_rec['ci95'][1]:.4f}]) | {n_rec['verdict']} |")
        rows.append("")
    rows.append(B1_WAIVER_NOTE)
    rows.append("")
    current_perf = out / "perf.json"
    if current_perf.exists():
        d7_file = out / "diag_D7.json"
        d3_diag = out / "diagnose.json"
        rows.extend(import_time_section(
            _read_json(current_perf),
            _read_json(d7_file) if d7_file.exists() else None,
            _read_json(d3_diag).get("D3_env") if d3_diag.exists() else None))
        rows.append("")
    # generation history: keep earlier generations' verdicts on record
    # (finding 7: generation from the filename fallback, gen-1 STOP ratio+CI,
    # and the contamination note — gen-1's warm series pooled no-.pyc samples)
    archives = sorted(out.glob("perf.gen*.json"))
    if archives:
        rows.append("## Generation history")
        rows.append("")
        for f in archives:
            try:
                d = _read_json(f)
                gen = d.get("generation")
                if gen is None:
                    m = re.search(r"gen(\d+)", f.name)
                    gen = int(m.group(1)) if m else "?"
                stop_line = ""
                stop_metric = (d.get("metrics_final", {})
                               .get("harness.execution_ms.p95"))
                if d.get("regression_metrics"):
                    if stop_metric:
                        stop_line = (f"; STOP metric harness.execution_ms.p95 "
                                     f"ratio {stop_metric['ratio']:.3f}, CI "
                                     f"{stop_metric['ci95'][0]:.3f}-"
                                     f"{stop_metric['ci95'][1]:.3f}")
                    # VAL finding: the contamination note is specific to
                    # generation 1 (its warm series pooled no-.pyc and
                    # genuine samples) — never attach it to a later
                    # regressing generation
                    if gen == 1:
                        stop_line += (" — NOTE: this generation's warm series "
                                      "was CONTAMINATED (pooled no-.pyc and "
                                      "genuine samples; superseded by "
                                      "generation 2)")
                rows.append(
                    f"- {f.name}: generation {gen}, verdict "
                    f"{d.get('verdict')}, regressions "
                    f"{d.get('regression_metrics', [])}, noise-unresolved "
                    f"{len(d.get('noise_unresolved', []))} metrics{stop_line}")
            except (OSError, ValueError):
                rows.append(f"- {f.name}: unreadable")
        rows.append("")
    if diagnosis:
        rows.append(diagnosis)
    (out / "report.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return summary


def cmd_run(args: argparse.Namespace) -> int:
    before, after, out = Path(args.before).resolve(), Path(args.after).resolve(), Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    problems = preflight(before, after)
    if problems:
        for p in problems:
            print(f"[r3] PREFLIGHT FAIL: {p}")
        raise SystemExit("preflight failed: fix the tree binding before running checks")
    wanted = set(CHECK_ORDER) | {"report"}
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
    results: dict[str, dict] = {}
    t0 = time.perf_counter()
    if "inventory" in wanted:
        print("[r3] check 1 inventory ...", flush=True)
        results["inventory"] = check_inventory(before, after, out)
    if "nodeids" in wanted:
        print("[r3] check 2 nodeids ...", flush=True)
        results["nodeids"] = check_nodeids(before, after, out)
    if "counts" in wanted:
        print("[r3] check 3 counts (two full suites) ...", flush=True)
        results["counts"] = check_counts(before, after, out,
                                         force=bool(args.force))
    if "tools" in wanted:
        print("[r3] check 4 tool surface ...", flush=True)
        results["tools"] = check_tools(before, after, out)
    if "probe" in wanted:
        print("[r3] check 5 probe ...", flush=True)
        results["probe"] = check_probe(before, after, out)
    if "audit" in wanted:
        print("[r3] check 6 audit shapes ...", flush=True)
        results["audit"] = check_audit(before, after, out)
    if "launch" in wanted:
        print("[r3] check 6b launch paths (wheel + clean venv) ...", flush=True)
        results["launch"] = check_launch(before, after, out)
    if "testdiff" in wanted:
        print("[r3] check 8 filtered test diff ...", flush=True)
        results["testdiff"] = check_testdiff(after, out)
    if "devtree" in wanted:
        print("[r3] check 9 dev tree ...", flush=True)
        results["devtree"] = check_devtree(out, dev_tree=args.dev_tree)
    if "perf" in wanted:
        print("[r3] check 7 interleaved perf A/B ...", flush=True)
        results["perf"] = check_perf(before, after, out,
                                     phase=args.perf_phase, rounds=args.perf_rounds)
    if "report" in wanted:
        merged: dict[str, dict] = {}
        for name in CHECK_ORDER:
            f = out / CHECK_FILES[name]
            if f.exists():
                merged[name] = _read_json(f)
            elif name in results:
                merged[name] = results[name]
        summary = write_summary_and_report(out, merged, {
            "before": str(before), "after": str(after),
            "seconds": round(time.perf_counter() - t0, 1)})
        print(f"[r3] overall: {summary['overall']} -> {out / 'report.md'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="equivalence")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="run the equivalence checks")
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--only", default=None,
                   help="comma list of: " + ",".join(CHECK_ORDER + ("report",)))
    p.add_argument("--perf-phase", default=None, dest="perf_phase",
                   help="run one perf phase only: import-cold | import-warm | "
                        "profile | rerun-import-cold | rerun-import-warm | "
                        "rerun-profile; appends to perf_series.json")
    p.add_argument("--perf-rounds", type=int, default=None, dest="perf_rounds",
                   help="rounds for the (rerun-)profile phase (default all)")
    p.add_argument("--force", action="store_true",
                   help="re-run cached suites (check 3) on the CURRENT test set")
    p.add_argument("--dev-tree", default=os.environ.get("CORTEX_DEV_TREE"),
                   dest="dev_tree",
                   help="read-only dev tree for check 9 (or set CORTEX_DEV_TREE)")
    p.set_defaults(func=cmd_run)
    q = sub.add_parser("_stage-audit")
    q.add_argument("--tree", required=True)
    q.add_argument("--out", required=True)
    q.add_argument("--flat-aliases", action="store_true")
    q.set_defaults(func=cmd_stage_audit)
    d = sub.add_parser("diagnose", help="diagnose the perf STOP finding")
    d.add_argument("--before", required=True)
    d.add_argument("--after", required=True)
    d.add_argument("--out", required=True)
    d.add_argument("--stage", required=True,
                   help="D1 | D2-disable | D2-freeze | D3 | D4 | D5 | D6 | "
                        "D7 | final")
    d.add_argument("--rounds", type=int, default=None,
                   help="rounds for D1/D2 stages (default 20; invocations append)")
    d.add_argument("--execs", type=int, default=None,
                   help="profiled executions per side for D5 (default 600)")
    d.set_defaults(func=cmd_diagnose)
    r = sub.add_parser("_fake-harness")
    r.add_argument("--repeats", type=int, default=8)
    r.set_defaults(func=cmd_fake_harness)
    s = sub.add_parser("_suite", help="one side of check 3 (resumable)")
    s.add_argument("--tree", required=True)
    s.add_argument("--junit", required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_suite)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
