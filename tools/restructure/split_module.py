"""Split a module into new modules by moving byte-exact line ranges (WP-R2a step 1).

Usage: python tools/restructure/split_module.py <spec.json>

Spec shape:
{
  "source": "src/computer_use_mcp/desktop/backend.py",
  "modules": {"<name>": {"path": "src/.../desktop/<name>.py",
                          "header": ["<line>", ...]}, ...},
  "moves": [{"target": "<name>", "start": 81, "end": 88}, ...]
}

Rules enforced (fail closed, no files written on any violation):
- `moves` must be sorted by start, non-overlapping, 1-based inclusive, within the file.
- Every source line is either moved exactly once or kept in the rewritten source.
- Target files are written as: header lines + the moved source lines BYTE-FOR-BYTE
  (keepends preserved, no reformatting), in source order.
- The source is rewritten with only the kept lines (order preserved).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def load_spec(spec_path: str) -> tuple[Path, dict[str, dict], list[dict]]:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    return Path(spec["source"]), spec["modules"], spec["moves"]


def validate_moves(moves: list[dict], line_count: int) -> None:
    if not moves:
        raise SystemExit("spec has no moves")
    previous_end = 0
    for move in moves:
        start, end = move["start"], move["end"]
        if not (1 <= start <= end <= line_count):
            raise SystemExit(f"move out of bounds: {move}")
        if start <= previous_end:
            raise SystemExit(f"move overlaps or is out of order: {move} (previous end {previous_end})")
        previous_end = end


def apply_split(source: Path, modules: dict[str, dict], moves: list[dict]) -> dict[str, int]:
    raw = source.read_bytes()
    lines = raw.splitlines(keepends=True)
    validate_moves(moves, len(lines))
    moved_by_target: dict[str, list[bytes]] = {name: [] for name in modules}
    for move in moves:
        target = move["target"]
        if target not in moved_by_target:
            raise SystemExit(f"move targets unknown module: {target}")
        moved_by_target[target].extend(lines[move["start"] - 1 : move["end"]])
    counts: dict[str, int] = {}
    for name, module in modules.items():
        header = "".join(line + "\n" for line in module["header"]).encode("utf-8")
        body = b"".join(moved_by_target[name])
        Path(module["path"]).write_bytes(header + body)
        counts[name] = len(module["header"]) + len(moved_by_target[name])
    kept = list(lines)
    for move in reversed(moves):
        del kept[move["start"] - 1 : move["end"]]
    source.write_bytes(b"".join(kept))
    moved_total = sum(len(v) for v in moved_by_target.values())
    if moved_total + len(kept) != len(lines):
        raise SystemExit("internal error: moved + kept != total")
    counts["<kept in source>"] = len(kept)
    return counts


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python tools/restructure/split_module.py <spec.json>", file=sys.stderr)
        return 2
    source, modules, moves = load_spec(argv[1])
    counts = apply_split(source, modules, moves)
    for name, count in counts.items():
        print(f"{name}: {count} lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
