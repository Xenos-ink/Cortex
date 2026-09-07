"""Seed the Desktop\\cortex-bench fixture tree for the harsh benchmark tasks.

Deterministic re-seed: every run starts from byte-identical inputs. Run this
BEFORE a driver run (documented in benchmarks/tasks_hard/DRIVER-PROTOCOL.md and
available as ``python benchmarks/score_task.py --seed --task <yaml>``).

Design rules:
- INPUT files (sources the driver reads) are overwritten on every seed.
- OUTPUT paths (``*_out_*``) are NEVER created or deleted here: the scorer checks
  that the driver produced them, so pre-creating one would invalidate the task.
- t6_inbox is wiped and recreated flat (it is the task's INPUT state); the
  scorer hashes the CANONICAL copies under fixtures/t6_seed/ (never mutated),
  so a driver that edits a Desktop copy cannot change the expected answers.

Usage:
    python benchmarks/tasks_hard/setup_fixtures.py            # seed everything
    python benchmarks/tasks_hard/setup_fixtures.py t1 t6      # seed a subset
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BENCH_ROOT = Path.home() / "Desktop" / "cortex-bench"

COPIES = {
    "t1": ["t1_source.txt"],
    "t2": ["t2_facts.html"],
    "t4": ["t4_source.txt"],
    "t5": ["t5_roster.csv"],
    "t6": ["t6_seed"],  # copied as cortex-bench/t6_inbox/<files>
    "r1": ["r1_note.txt"],
    "r2": ["r2_note.txt"],
}


def seed(only: list[str] | None = None) -> dict[str, object]:
    wanted = only or list(COPIES)
    summary: dict[str, object] = {"bench_root": str(BENCH_ROOT), "seeded": {}}
    BENCH_ROOT.mkdir(parents=True, exist_ok=True)
    for task in wanted:
        if task == "t6":
            inbox = BENCH_ROOT / "t6_inbox"
            if inbox.exists():
                shutil.rmtree(inbox)
            inbox.mkdir(parents=True)
            count = 0
            for src in sorted((FIXTURES / "t6_seed").iterdir()):
                shutil.copy2(src, inbox / src.name)
                count += 1
            summary["seeded"]["t6_inbox"] = f"{count} files (wiped + recreated)"
            continue
        if task == "r1":
            # INPUT state for r1-dialog-navigation: the (empty) navigation target.
            # The driver's OUTPUT (r1_target/r1_archive/r1_out_note.txt) is never
            # created here; wiping the container removes any leftover from a
            # previous run so the task always starts from an empty r1_target.
            target = BENCH_ROOT / "r1_target"
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True)
            summary["seeded"]["r1_target"] = "empty dir (wiped + recreated)"
        for name in COPIES.get(task, []):
            src = FIXTURES / name
            if not src.exists():
                raise FileNotFoundError(f"canonical fixture missing: {src}")
            shutil.copy2(src, BENCH_ROOT / name)
            summary["seeded"].setdefault(task, []).append(str(BENCH_ROOT / name))  # type: ignore[union-attr]
    return summary


def main(argv: list[str]) -> int:
    only = [arg for arg in argv if not arg.startswith("-")]
    print(seed(only or None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
