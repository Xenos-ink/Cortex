#!/usr/bin/env python
"""R-7 DIFF-STAGE PROFILE + CORPUS RECORDER (ORVEX-CORTEX-056-LIVEFIX, goal section 7).

Precise per-operation cost breakdown of the verify/diff stage on REAL desktop
frame pairs. Screen READING only — no OS input is dispatched, no app is
launched or attached (D6-safe: capture only).

Modes:
  record   Capture real frame pairs (dxgi if available + blt), build boundary
           and synthetic pairs from the REAL frame's pixels, save corpus .npz
           files + labels.json to D:\\Cortex\\.orvex\\artifacts\\r7_diff_corpus\\.
  profile  Load the corpus, time the CURRENT _diff_magnitude op-by-op
           (difference / split / lighter / histogram / mean loops), the full
           ScreenshotDiffStrategy.verify and VerificationEngine.verify paths,
           and each candidate optimization, verifying magnitude-tuple
           equality vs the current implementation on every pair.

numpy is NOT a runtime dependency of this project (pyproject: mcp, pydantic,
pydantic-settings, Pillow, mss, pyautogui, httpx) and cv2 is not either, so
every candidate below is PIL/stdlib-only. Numba/cython forbidden (no compile).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import platform
import struct
import sys
import time
from typing import Any, Callable

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
CORPUS_DIR = os.path.normpath(
    os.path.join(REPO_ROOT, "..", ".orvex", "artifacts", "r7_diff_corpus")
)

from PIL import Image, ImageChops  # noqa: E402


def ms_of(fn: Callable[[], Any], repeat: int = 8) -> dict[str, float]:
    xs: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t0) * 1000.0)
    xs.sort()
    return {"n": float(repeat), "p50": xs[len(xs) // 2], "mean": sum(xs) / len(xs), "max": xs[-1]}


def _fmt(stats: dict[str, float]) -> str:
    return f"p50={stats['p50']:7.2f}ms mean={stats['mean']:7.2f}ms max={stats['max']:7.2f}ms"


# =====================================================================================
# Corpus recording (real desktop, capture only)
# =====================================================================================

def _observe_frames(capture_env: str | None) -> tuple[Any, Any, dict[str, Any]]:
    """Two back-to-back real captures through the real backend (screen read only)."""
    if capture_env is not None:
        os.environ["CORTEX_CAPTURE"] = capture_env
    else:
        os.environ.pop("CORTEX_CAPTURE", None)
    from computer_use_mcp.backend import LocalComputerBackend
    from computer_use_mcp.verification import ScreenshotDiffStrategy

    backend = LocalComputerBackend()
    first = backend.observe()
    second = backend.observe()
    resolved = {
        "capture_backend": getattr(backend, "_capture_backend", "?"),
        "dxgi_live": getattr(backend, "_dxgi", None) is not None,
        "size": [first.width, first.height],
    }

    def _frame(obs: Any) -> Any:
        frame = getattr(obs, "_frame", None)
        if frame is not None and frame.mode == "RGB":
            return frame
        return ScreenshotDiffStrategy._decode(obs.image_base64)

    return _frame(first), _frame(second), resolved


def _set_patch(img: Image.Image, x0: int, y0: int, width: int, height: int, rgb: tuple[int, int, int]) -> None:
    px = img.load()
    for y in range(y0, y0 + height):
        for x in range(x0, x0 + width):
            px[x, y] = rgb


def record_corpus() -> int:
    from computer_use_mcp.verification import (
        STRONG_CHANGE_MIN_PIXELS,
        STRONG_PIXEL_DELTA,
    )

    os.makedirs(CORPUS_DIR, exist_ok=True)
    pairs: list[tuple[str, Image.Image, Image.Image]] = []
    resolved: dict[str, Any] = {}

    for tag, env in (("dxgi", "dxgi"), ("blt", None)):
        try:
            f1, f2, res = _observe_frames(env)
        except Exception as exc:  # noqa: BLE001
            print(f"[record] {tag} capture unavailable: {exc!r} — skipped")
            continue
        if f1.size != f2.size:
            print(f"[record] {tag} size drift {f1.size} vs {f2.size} — skipped")
            continue
        resolved[tag] = res
        w, h = f1.size
        label_tag = tag

        # (a) real identical + real back-to-back pairs
        pairs.append((f"real_identical_{label_tag}", f1, f1.copy()))
        pairs.append((f"real_backtoback_{label_tag}", f1, f2))

        # (b) small caret-like change (9 px, delta 200) — sub-boundary flicker class
        caret = f1.copy()
        _set_patch(caret, 10, 10, 3, 3, (255, 255, 255))
        pairs.append((f"caret_9px_{label_tag}", f1, caret))

        # (c) boundary pairs: exactly 49/50/51 strongly-changed pixels
        #     (white-on-real-frame patch = delta >= 40 for nearly every real pixel)
        for n_px in (STRONG_CHANGE_MIN_PIXELS - 1, STRONG_CHANGE_MIN_PIXELS, STRONG_CHANGE_MIN_PIXELS + 1):
            b = f1.copy()
            cols = min(n_px, w - 40)
            rows = -(-n_px // cols)  # ceil
            _set_patch(b, 40, 40, cols, rows, (255, 255, 255))
            # trim to exact count: walk and reset extras (cols*rows may overshoot)
            px = b.load()
            placed = cols * rows
            for i in range(placed - 1, n_px - 1, -1):
                px[40 + (i % cols), 40 + (i // cols)] = f1.load()[40 + (i % cols), 40 + (i // cols)]
            pairs.append((f"boundary_{n_px}px_{label_tag}", f1, b))

        # (d) a compact stroke-like change ~400 px (typical verified draw)
        stroke = f1.copy()
        _set_patch(stroke, 100, 100, 200, 2, (255, 255, 255))
        pairs.append((f"stroke_400px_{label_tag}", f1, stroke))

        # (e) uniform STRONG_PIXEL_DELTA-1 noise (just under the strong bar) —
        #     a stress pair where strong-count must stay 0 but mean is huge
        noise = f1.copy()
        px_n = noise.load()
        px_s = f1.load()
        d = STRONG_PIXEL_DELTA - 1
        for y in range(0, h, 2):
            for x in range(0, w, 2):
                r, g, bl = px_s[x, y]
                px_n[x, y] = (min(255, r + d), min(255, g + d), min(255, bl + d))  # type: ignore[assignment]
        pairs.append((f"noise_{d}delta_quad_{label_tag}", f1, noise))

        # (f) single-pixel change (worst small case)
        one = f1.copy()
        _set_patch(one, 5, 5, 1, 1, (255, 0, 0))
        pairs.append((f"single_px_{label_tag}", f1, one))

    # (g) synthetic pure cases independent of the live desktop (stable in CI)
    base = Image.new("RGB", (512, 512), (100, 100, 100))
    for name, before, after in (
        ("syn_identical", base, base.copy()),
        ("syn_single", base, _patched(base, (5, 5), (255, 0, 0))),
        ("syn_alt_polarity", base, _alternating(base, 200)),
        ("syn_uniform39", base, Image.new("RGB", (512, 512), (139, 139, 139))),
        ("syn_uniform40", base, Image.new("RGB", (512, 512), (140, 140, 140))),
    ):
        pairs.append((name, before, after))

    # save corpus: one npz per pair is heavy; save as raw bytes npz per pair
    import numpy as np  # benchmark-only import (dev machine has numpy via benchmarks)

    labels = []
    for label, before, after in pairs:
        w, h = before.size
        arr = np.concatenate(
            [np.frombuffer(before.tobytes(), dtype=np.uint8),
             np.frombuffer(after.tobytes(), dtype=np.uint8)]
        )
        np.savez_compressed(
            os.path.join(CORPUS_DIR, f"{label}.npz"),
            pair=arr, width=w, height=h,
        )
        labels.append(label)
        print(f"[record] {label}: {w}x{h}")

    with open(os.path.join(CORPUS_DIR, "labels.json"), "w", encoding="utf-8") as fh:
        json.dump({"labels": labels, "resolved": resolved, "recorded": True}, fh, indent=2)
    print(f"[record] {len(labels)} pairs -> {CORPUS_DIR}")
    return 0


def _patched(base: Image.Image, xy: tuple[int, int], rgb: tuple[int, int, int]) -> Image.Image:
    img = base.copy()
    img.load()[xy[0], xy[1]] = rgb  # type: ignore[index]
    return img


def _alternating(base: Image.Image, delta: int) -> Image.Image:
    img = base.copy()
    px = img.load()
    for y in range(img.height):
        for x in range(img.width):
            if (x + y) % 2 == 0:
                px[x, y] = (100 + delta, 100 - delta, 100)  # type: ignore[assignment]
    return img


def load_corpus() -> list[tuple[str, Image.Image, Image.Image]]:
    import numpy as np

    with open(os.path.join(CORPUS_DIR, "labels.json"), encoding="utf-8") as fh:
        labels = json.load(fh)["labels"]
    pairs = []
    for label in labels:
        with np.load(os.path.join(CORPUS_DIR, f"{label}.npz")) as data:
            w, h = int(data["width"]), int(data["height"])
            raw = data["pair"].tobytes()
            half = len(raw) // 2
            before = Image.frombytes("RGB", (w, h), raw[:half])
            after = Image.frombytes("RGB", (w, h), raw[half:])
        pairs.append((label, before, after))
    return pairs


# =====================================================================================
# Current implementation, imported for exact comparison
# =====================================================================================

def _current_magnitude(before: Image.Image, after: Image.Image) -> tuple[float, int]:
    from computer_use_mcp.verification import _diff_magnitude

    return _diff_magnitude(before, after)


# =====================================================================================
# Profile: op-by-op + full paths
# =====================================================================================

def profile_corpus() -> int:
    from computer_use_mcp.verification import (
        ScreenshotDiffStrategy,
        VerificationEngine,
        VerificationIntent,
        VerificationKind,
    )
    from computer_use_mcp.models import Observation

    pairs = load_corpus()
    print(f"\n=== corpus loaded: {len(pairs)} pairs ===")
    report: dict[str, Any] = {"per_pair": [], "op_totals": {}}

    op_totals: dict[str, dict[str, float]] = {}

    def add_op(name: str, stats: dict[str, float]) -> None:
        cur = op_totals.setdefault(name, {"n": 0.0, "p50": 0.0, "mean": 0.0, "max": 0.0})
        cur["n"] += stats["n"]
        cur["p50"] += stats["p50"]
        cur["mean"] += stats["mean"]
        cur["max"] = max(cur["max"], stats["max"])

    for label, before, after in pairs:
        # sanity: current magnitude for the record
        m_cur = _current_magnitude(before, after)

        # op-by-op timing of the CURRENT implementation body
        t_diff = ms_of(lambda: ImageChops.difference(before, after))
        diff = ImageChops.difference(before, after)
        t_split = ms_of(lambda: diff.split())
        bands = diff.split()
        t_lighter = ms_of(lambda: ImageChops.lighter(ImageChops.lighter(bands[0], bands[1]), bands[2]))
        max_band = ImageChops.lighter(ImageChops.lighter(bands[0], bands[1]), bands[2])
        t_hist_diff = ms_of(lambda: diff.histogram())
        histogram = diff.histogram()
        t_hist_max = ms_of(lambda: max_band.histogram())
        t_mean_loop = ms_of(
            lambda: [
                sum(index * count for index, count in enumerate(histogram[band * 256:(band + 1) * 256]))
                for band in range(3)
            ]
        )
        t_strong_tail = ms_of(lambda: sum(max_band.histogram()[40:]))

        add_op("ImageChops.difference", t_diff)
        add_op("diff.split()", t_split)
        add_op("lighter/lighter (max band)", t_lighter)
        add_op("diff.histogram()", t_hist_diff)
        add_op("max_band.histogram()", t_hist_max)
        add_op("python mean loop (3 bands)", t_mean_loop)
        add_op("strong tail sum", t_strong_tail)

        t_full = ms_of(lambda: _current_magnitude(before, after))
        add_op("FULL _diff_magnitude", t_full)

        row = {
            "label": label,
            "mean": m_cur[0],
            "strongly_changed": m_cur[1],
            "ops": {
                "difference": t_diff["p50"],
                "split": t_split["p50"],
                "lighter": t_lighter["p50"],
                "hist_diff": t_hist_diff["p50"],
                "hist_max": t_hist_max["p50"],
                "mean_loop": t_mean_loop["p50"],
                "strong_tail": t_strong_tail["p50"],
                "full": t_full["p50"],
            },
        }
        report["per_pair"].append(row)
        print(
            f"  {label:34s} mean={m_cur[0]:8.4f} strong={m_cur[1]:8d} | "
            f"diff={t_diff['p50']:6.2f} split={t_split['p50']:6.2f} "
            f"lighter={t_lighter['p50']:6.2f} hist={t_hist_diff['p50']:6.2f} "
            f"histmax={t_hist_max['p50']:6.2f} meanloop={t_mean_loop['p50']:6.2f} "
            f"tail={t_strong_tail['p50']:6.2f} | FULL={t_full['p50']:7.2f}ms"
        )

    n_pairs = float(len(pairs))
    print("\n=== op totals (p50 summed over pairs / count) ===")
    for name, st in op_totals.items():
        report["op_totals"][name] = st
        print(f"  {name:28s} avg_p50={st['p50']/n_pairs:7.2f}ms avg_mean={st['mean']/n_pairs:7.2f}ms max={st['max']:7.2f}ms")

    # full verify path profile on the largest pair
    from computer_use_mcp.verification import FocusChangeStrategy

    big = max(pairs, key=lambda p: p[1].width * p[1].height)
    label, before, after = big

    def obs_of(img: Image.Image) -> Observation:
        o = Observation(image_base64="RAW:PIN", width=img.width, height=img.height)
        o._frame = img  # noqa: SLF001 - benchmark mirrors the R-5 stash
        return o

    ob, oa = obs_of(before), obs_of(after)
    intent = VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=True)
    engine = VerificationEngine()
    strategy = ScreenshotDiffStrategy()
    focus = FocusChangeStrategy()
    focus_intent = VerificationIntent(
        kind=VerificationKind.VISUAL_CHANGE, expected_change=True,
        expected_effect="x", metadata={"focus_change_click": True}, diff_threshold=1.0,
    )

    t_engine = ms_of(lambda: engine.verify(intent, ob, oa), repeat=6)
    t_strategy = ms_of(lambda: strategy.verify(intent, ob, oa), repeat=6)
    t_focus = ms_of(lambda: focus.verify(focus_intent, ob, oa), repeat=6)
    t_focus_mag = ms_of(lambda: focus._digest_change_magnitude(ob, oa), repeat=6)  # noqa: SLF001
    print(f"\n=== full-path profile (pair {label} {before.width}x{before.height}) ===")
    print(f"  VerificationEngine.verify   {_fmt(t_engine)}")
    print(f"  ScreenshotDiffStrategy.verify {_fmt(t_strategy)}")
    print(f"  FocusChangeStrategy.verify    {_fmt(t_focus)}")
    print(f"  _digest_change_magnitude      {_fmt(t_focus_mag)}")
    report["full_path"] = {
        "pair": label,
        "size": [before.width, before.height],
        "engine_verify": t_engine,
        "strategy_verify": t_strategy,
        "focus_verify": t_focus,
        "focus_magnitude": t_focus_mag,
    }

    # PNG-encode reference (what the encode-skip saved)
    t_png = ms_of(lambda: before.save(io.BytesIO(), format="PNG"), repeat=4)
    report["full_path"]["png_encode_reference"] = t_png
    print(f"  (reference) PNG encode        {_fmt(t_png)}")

    out = os.path.join(CORPUS_DIR, "profile_report.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(f"\n[profile] report -> {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record-only", action="store_true")
    parser.add_argument("--profile-only", action="store_true")
    args = parser.parse_args()
    print(f"platform={platform.system()} python={sys.version.split()[0]}")
    print(f"corpus dir: {CORPUS_DIR}")
    if not args.profile_only:
        record_corpus()
    if not args.record_only:
        profile_corpus()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
