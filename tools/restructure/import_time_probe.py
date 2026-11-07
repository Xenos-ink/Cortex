"""Server import-time probe, N=30 fresh interpreters (WP-R2a step-1 perf item).

Usage: python tools/restructure/import_time_probe.py <out.json>
Writes {median,p95,min,all_ms,baseline:{median,p95},delta_ms:{median,p95}}.
Baseline numbers are the WP-R0 capture (unsplit tree, same machine/python).
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CHILD = (
    "import time\n\n"
    "t0 = time.perf_counter()\n"
    "import computer_use_mcp.server.app  # noqa: E402,F401\n\n"
    "print(time.perf_counter() - t0)\n"
)
BASELINE_MEDIAN_MS = 1015.0
BASELINE_P95_MS = 1140.0
N = 30


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: import_time_probe.py <out.json>", file=sys.stderr)
        return 2
    py = Path(sys.executable)
    with tempfile.TemporaryDirectory() as td:
        child = Path(td) / "_import_time_child.py"
        child.write_text(CHILD, encoding="utf-8")
        samples: list[float] = []
        for _ in range(N):
            proc = subprocess.run([str(py), str(child)], capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                print(proc.stderr[-500:], file=sys.stderr)
                return 1
            samples.append(float(proc.stdout.strip().splitlines()[-1]) * 1000.0)
    xs = sorted(samples)
    p95 = xs[int(round(0.95 * (len(xs) + 1))) - 1]
    blob = {
        "n": N,
        "median_ms": round(statistics.median(xs), 1),
        "p95_ms": round(p95, 1),
        "min_ms": round(xs[0], 1),
        "all_ms": [round(x, 1) for x in xs],
        "baseline_unsplit": {"median_ms": BASELINE_MEDIAN_MS, "p95_ms": BASELINE_P95_MS},
        "delta_ms": {
            "median": round(statistics.median(xs) - BASELINE_MEDIAN_MS, 1),
            "p95": round(p95 - BASELINE_P95_MS, 1),
        },
        "note": "A/B judgement (vs the +5% hot-path ceiling) is WP-R3's; this records the measurement only.",
    }
    Path(argv[1]).write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: blob[k] for k in ("n", "median_ms", "p95_ms", "min_ms", "delta_ms")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
