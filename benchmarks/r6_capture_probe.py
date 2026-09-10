#!/usr/bin/env python
"""R-6 CAPTURE-FLOOR PROBE (ORVEX-CORTEX-056-LIVEFIX).

Attack the remaining mechanical floor left by R-5 (198.5ms p50 = 13.1x human; bar
~52ms). Screen READING only — no OS input. Measures, in isolation:

  C1  pure BitBlt capture cost today (persistent mss)  [the hardware floor, GDI]
  C2  raw-ctypes DXGI Desktop Duplication prototype: AcquireNextFrame path —
      is ~10-20ms/capture real on THIS box without any new dependency?
  C3  PNG compress-level sweep (1/3/6) on live frames — the encode half of the floor
  C4  full-frame diff vs numpy-free downsampled diff COST (equivalence judged separately)
  C5  composite "one action" floors: GDI pipeline today vs DXGI-swapped pipeline
      (identical downstream: PNG encode, verification fast path)

Everything here is READ-ONLY profiling; nothing is installed or mutated.
"""
from __future__ import annotations

import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))


def _ms(xs: list[float]) -> dict[str, float]:
    xs = sorted(xs)
    return {
        "n": len(xs),
        "p50": xs[len(xs) // 2],
        "p95": xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))],
        "mean": sum(xs) / len(xs),
        "max": xs[-1],
    }


def fmt(name: str, s: dict[str, float]) -> None:
    print(
        f"  {name:52s} n={s['n']:>2} p50={s['p50']:8.1f}ms p95={s['p95']:8.1f}ms "
        f"mean={s['mean']:8.1f}ms max={s['max']:8.1f}ms"
    )


def main() -> int:
    import base64
    import io

    from PIL import Image, ImageChops

    import mss

    from computer_use_mcp.backend import LocalComputerBackend

    iters = 12
    backend = LocalComputerBackend()
    monitor = backend._monitors[0]
    left, top, width, height = monitor.bounds
    region = {"left": left, "top": top, "width": width, "height": height}
    print(f"# region {width}x{height} (primary monitor)")

    # --- C1: persistent-mss BitBlt grab (the R-5 steady state) -----------------
    with mss.MSS() as cap:
        cap.grab(region)  # warm
        xs = []
        for _ in range(iters):
            t0 = time.perf_counter()
            shot = cap.grab(region)
            xs.append((time.perf_counter() - t0) * 1000.0)
    fmt("C1 mss persistent grab (BitBlt GDI)", _ms(xs))

    frame_ref = Image.frombuffer("RGB", shot.size, shot.raw, "raw", "BGRX", 0, 1)

    # --- C2: raw-ctypes DXGI Desktop Duplication prototype ---------------------
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from r6_dxgi_proto import Duplicator  # local prototype, pure ctypes

        dup = Duplicator()
        print(f"# DXGI: output {dup.width}x{dup.height} rotate={dup.rotation} "
              f"format={dup.fmt_name}")
        xs = []
        frames = []
        for i in range(iters):
            t0 = time.perf_counter()
            img = dup.grab(timeout_ms=0)
            dt = (time.perf_counter() - t0) * 1000.0
            xs.append(dt)
            frames.append(img)
        ok = all(f is not None and f.size == (width, height) for f in frames)
        fmt("C2 DXGI duplication grab (ctypes, no deps)", _ms(xs))
        print(f"#   frames ok={ok}; first size={frames[0].size if frames[0] else None}")
        if frames[-1] is not None:
            # pixel-parity spot check vs a GDI grab taken right after
            with mss.MSS() as cap:
                shot2 = cap.grab(region)
            gdi = Image.frombuffer("RGB", shot2.size, shot2.raw, "raw", "BGRX", 0, 1)
            # desktops animate (clock etc.) — compare mean delta, not identity
            from computer_use_mcp.verification import _diff_magnitude
            mean_d, strong = _diff_magnitude(frames[-1], gdi)
            print(f"#   DXGI-vs-GDI mean-diff on same instant: {mean_d:.3f} "
                  f"(strongly-changed {strong}) — animations make 0 impossible")
    except Exception as exc:  # noqa: BLE001
        print(f"# C2 DXGI prototype UNAVAILABLE on this box: {type(exc).__name__}: {exc}")

    # --- C3: PNG compress-level sweep on the live frame ------------------------
    for level in (1, 3, 6):
        out = io.BytesIO()
        xs = []
        for _ in range(iters):
            t0 = time.perf_counter()
            out = io.BytesIO()
            frame_ref.save(out, format="PNG", compress_level=level)
            xs.append((time.perf_counter() - t0) * 1000.0)
        b64len = len(base64.b64encode(out.getvalue()))
        fmt(f"C3 PNG encode level={level} ({b64len//1024}KB b64)", _ms(xs))

    # --- C4: diff cost — full frame vs downsampled ------------------------------
    before = frame_ref
    # synthesize an "after": same screen + a stroke-like patch (like a real action)
    after = before.copy()
    px = after.load()
    for y in range(300, 320):
        for x in range(400, 800):
            px[x, y] = (255, 255, 255)
    from computer_use_mcp.verification import _diff_magnitude as diff_full

    xs = []
    for _ in range(iters):
        t0 = time.perf_counter()
        diff_full(before, after)
        xs.append((time.perf_counter() - t0) * 1000.0)
    fmt("C4a diff full-frame (current)", _ms(xs))

    # downsampled (0.5x box) variant — COST ONLY (equivalence decided separately)
    def diff_half(b: Image.Image, a: Image.Image):
        size = (b.width // 2, b.height // 2)
        return diff_full(b.resize(size), a.resize(size))

    xs = []
    for _ in range(iters):
        t0 = time.perf_counter()
        diff_half(before, after)
        xs.append((time.perf_counter() - t0) * 1000.0)
    fmt("C4b diff via 0.5x downsample (cost only)", _ms(xs))

    # box-filter via PIL reduce (fast C path) instead of resize LANCZOS
    def diff_reduce(b: Image.Image, a: Image.Image):
        return diff_full(b.reduce(2), a.reduce(2))

    xs = []
    for _ in range(iters):
        t0 = time.perf_counter()
        diff_reduce(before, after)
        xs.append((time.perf_counter() - t0) * 1000.0)
    fmt("C4c diff via PIL reduce(2) (cost only)", _ms(xs))

    # --- C5: composite floors ---------------------------------------------------
    def composite_gdi() -> Image.Image:
        with mss.MSS() as cap:
            shot = cap.grab(region)
        return Image.frombuffer("RGB", shot.size, shot.raw, "raw", "BGRX", 0, 1)

    xs = []
    for _ in range(iters):
        t0 = time.perf_counter()
        f = composite_gdi()
        out = io.BytesIO()
        f.save(out, format="PNG", compress_level=6)
        b64 = base64.b64encode(out.getvalue()).decode("ascii")
        xs.append((time.perf_counter() - t0) * 1000.0)
    fmt("C5a capture+PNG6+b64 (GDI, per capture)", _ms(xs))

    try:
        xs = []
        for _ in range(iters):
            t0 = time.perf_counter()
            f = dup.grab(timeout_ms=0)
            out = io.BytesIO()
            f.save(out, format="PNG", compress_level=6)
            b64 = base64.b64encode(out.getvalue()).decode("ascii")
            xs.append((time.perf_counter() - t0) * 1000.0)
        fmt("C5b capture+PNG6+b64 (DXGI, per capture)", _ms(xs))
    except Exception as exc:  # noqa: BLE001
        print(f"# C5b skipped: {type(exc).__name__}: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
