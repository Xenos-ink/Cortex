#!/usr/bin/env python
"""R-5 optimization-candidate probe: measure each proposed lever in isolation.

Screen READING only; no input. Measures:
  P1 mss new-instance-per-grab vs persistent instance grab
  P2 mss .rgb property reorder vs PIL frombuffer BGRX decode
  P3 PNG encode compress_level sweep (1/3/6 default) on the live screen
  P4 verification diff restructure (histogram-derived mean vs ImageStat.Stat)
     on raw frames (no PNG decode) vs current decode-then-diff
  P5 outbound JPEG encode from raw frame vs current decode(PNG)+encode
  P6 composite "optimized observe" prototype cost
"""
from __future__ import annotations

import base64
import io
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
    }


def fmt(name: str, s: dict[str, float]) -> None:
    print(f"  {name:48s} n={s['n']:>2} p50={s['p50']:8.1f}ms p95={s['p95']:8.1f}ms mean={s['mean']:8.1f}ms")


def main() -> int:
    iters = 12
    from PIL import Image, ImageChops, ImageStat

    from computer_use_mcp.backend import LocalComputerBackend

    backend = LocalComputerBackend()
    monitor = backend._monitors[0]
    left, top, width, height = monitor.bounds
    region = {"left": left, "top": top, "width": width, "height": height}
    import mss

    print(f"# region {width}x{height}")

    # --- P1: new instance per grab vs persistent ------------------------------
    new_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        with mss.MSS() as cap:
            cap.grab(region)
        new_ms.append((time.perf_counter() - t0) * 1000.0)
    fmt("P1 new-instance grab (mss.MSS ctx)", _ms(new_ms))

    with mss.MSS() as cap:
        persist_ms = []
        for _ in range(iters):
            t0 = time.perf_counter()
            cap.grab(region)
            persist_ms.append((time.perf_counter() - t0) * 1000.0)
    fmt("P1 persistent-instance grab", _ms(persist_ms))

    with mss.MSS() as cap:
        shot = cap.grab(region)

    # --- P2: .rgb property vs frombuffer BGRX --------------------------------
    rgb_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        _ = shot.rgb
        rgb_ms.append((time.perf_counter() - t0) * 1000.0)
    fmt("P2 mss .rgb property reorder", _ms(rgb_ms))

    bgrx_ms = []
    bgra = shot.bgra
    for _ in range(iters):
        t0 = time.perf_counter()
        img = Image.frombuffer("RGB", shot.size, bgra, "raw", "BGRX", 0, 1)
        bgrx_ms.append((time.perf_counter() - t0) * 1000.0)
    fmt("P2 PIL frombuffer BGRX (zero-copy view)", _ms(bgrx_ms))
    del img

    frombytes_ms = []
    rgb_bytes = shot.rgb
    for _ in range(iters):
        t0 = time.perf_counter()
        _img2 = Image.frombytes("RGB", shot.size, rgb_bytes)
        frombytes_ms.append((time.perf_counter() - t0) * 1000.0)
    fmt("P2 PIL frombytes RGB (copy)", _ms(frombytes_ms))
    del _img2

    # --- P3: PNG compress_level sweep -----------------------------------------
    frame = Image.frombytes("RGB", shot.size, rgb_bytes)
    png_sizes = {}
    for level in (1, 3, 6):
        xs = []
        for _ in range(iters):
            out = io.BytesIO()
            t0 = time.perf_counter()
            frame.save(out, format="PNG", compress_level=level)
            xs.append((time.perf_counter() - t0) * 1000.0)
            png_sizes[level] = out.tell() / 1024.0
        fmt(f"P3 PNG encode compress_level={level}", _ms(xs))
    print("  PNG sizes KB: " + ", ".join(f"level{k}={v:.0f}" for k, v in png_sizes.items()))

    # --- P4: diff restructure on raw frames ------------------------------------
    a = Image.frombytes("RGB", shot.size, rgb_bytes)
    b = Image.frombytes("RGB", shot.size, rgb_bytes)
    # make a small change like a drawn stroke
    b_draw = b.copy()
    from PIL import ImageDraw

    draw = ImageDraw.Draw(b_draw)
    draw.line((100, 100, 400, 300), fill=(255, 0, 0), width=3)

    cur_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        diff = ImageChops.difference(a, b_draw)
        mean_difference = sum(ImageStat.Stat(diff).mean) / 3.0
        bands = diff.split()
        max_band = ImageChops.lighter(ImageChops.lighter(bands[0], bands[1]), bands[2])
        strongly_changed = sum(max_band.histogram()[40:])
        cur_ms.append((time.perf_counter() - t0) * 1000.0)
    fmt("P4 current diff math (Stat+split+lighter+hist)", _ms(cur_ms))

    hist_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        diff = ImageChops.difference(a, b_draw)
        hist = diff.histogram()  # 768 bins, one pass
        n = diff.width * diff.height
        means = []
        for band in range(3):
            h = hist[band * 256:(band + 1) * 256]
            means.append(sum(i * c for i, c in enumerate(h)) / n)
        mean_difference2 = sum(means) / 3.0
        bands = diff.split()
        max_band = ImageChops.lighter(ImageChops.lighter(bands[0], bands[1]), bands[2])
        strongly_changed2 = sum(max_band.histogram()[40:])
        hist_ms.append((time.perf_counter() - t0) * 1000.0)
    fmt("P4 histogram-mean diff math", _ms(hist_ms))
    assert abs(mean_difference - mean_difference2) < 1e-9, (mean_difference, mean_difference2)
    assert strongly_changed == strongly_changed2, (strongly_changed, strongly_changed2)

    # verify equivalence on a real different frame pair (two live captures)
    with mss.MSS() as cap:
        s1 = cap.grab(region)
        s2 = cap.grab(region)
    f1 = Image.frombytes("RGB", s1.size, s1.rgb)
    f2 = Image.frombytes("RGB", s2.size, s2.rgb)
    d1 = ImageChops.difference(f1, f2)
    m1 = sum(ImageStat.Stat(d1).mean) / 3.0
    h1 = d1.histogram()
    n1 = d1.width * d1.height
    m2v = sum(
        sum(i * c for i, c in enumerate(h1[band * 256:(band + 1) * 256])) / n1
        for band in range(3)
    ) / 3.0
    print(f"  equivalence on live pair: Stat mean={m1:.9f} hist mean={m2v:.9f} equal={abs(m1-m2v)<1e-9}")

    # --- P5: outbound JPEG from raw vs decode-PNG-then-JPEG ---------------------
    png_buf = io.BytesIO()
    frame.save(png_buf, format="PNG")
    png_bytes = png_buf.getvalue()
    png_b64 = base64.b64encode(png_bytes).decode("ascii")

    cur_bound_ms = []
    from computer_use_mcp import server as srv

    for _ in range(iters):
        t0 = time.perf_counter()
        srv._bound_outbound_image(png_b64)
        cur_bound_ms.append((time.perf_counter() - t0) * 1000.0)
    fmt("P5 current outbound (b64dec+PNGdec+JPEG)", _ms(cur_bound_ms))

    raw_jpeg_ms = []
    budget = srv._result_image_max_bytes()
    for _ in range(iters):
        t0 = time.perf_counter()
        img = Image.frombytes("RGB", shot.size, rgb_bytes)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        data = buf.getvalue()
        # downscale ladder if needed
        q = 85
        steps = 0
        while len(data) > budget and steps < 12:
            img = img.resize((max(1, int(img.width * 0.85)), max(1, int(img.height * 0.85))), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=q)
            data = buf.getvalue()
            steps += 1
        out_b64 = base64.b64encode(data).decode("ascii")
        raw_jpeg_ms.append((time.perf_counter() - t0) * 1000.0)
    fmt("P5 outbound from raw frame (JPEG direct)", _ms(raw_jpeg_ms))
    print(f"  jpeg p50 size: {len(data)/1024:.0f} KB, budget {budget/1024:.0f} KB, ladder steps {steps}")

    # --- P6: composite optimized observe prototype -----------------------------
    with mss.MSS() as cap:
        proto_ms = []
        for _ in range(iters):
            t0 = time.perf_counter()
            shot = cap.grab(region)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
            out = io.BytesIO()
            img.save(out, format="PNG", compress_level=1)
            payload = base64.b64encode(out.getvalue()).decode("ascii")
            proto_ms.append((time.perf_counter() - t0) * 1000.0)
        fmt("P6 prototype observe capture+encode(compress=1)", _ms(proto_ms))

        proto_ms2 = []
        for _ in range(iters):
            t0 = time.perf_counter()
            shot = cap.grab(region)
            img = Image.frombuffer("RGB", shot.size, shot.bgra, "raw", "BGRX", 0, 1)
            out = io.BytesIO()
            img.save(out, format="PNG", compress_level=1)
            payload = base64.b64encode(out.getvalue()).decode("ascii")
            proto_ms2.append((time.perf_counter() - t0) * 1000.0)
        fmt("P6 prototype observe frombuffer+BGRX", _ms(proto_ms2))

    # PNG decode cost (verification currently pays 2x this)
    dec_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        Image.open(io.BytesIO(png_bytes)).convert("RGB")
        dec_ms.append((time.perf_counter() - t0) * 1000.0)
    fmt("P4 PNG decode one frame (per side today)", _ms(dec_ms))

    # JPEG decode for comparison (if internal format were JPEG)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
