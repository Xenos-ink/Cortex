#!/usr/bin/env python
"""R-6 DXGI cost breakdown (focus probe).

The duplication contract: AcquireNextFrame yields a frame ONLY when the desktop
composited a change; on an idle desktop it times out. Real Cortex captures
happen right after input (screen just changed) — so the realistic measurement
runs with a LIVE terminal (results written to a file, terminal scrolling keeps
the desktop compositing) and separates:

  T1 acquire-only           (wait for a fresh frame, timeout 8ms; no readback)
  T2 full readback          (acquire + QI + CopyResource + Map + row-copy + PIL)
  T3 readback ONLY          (CopyResource+Map+copy on the ALREADY-acquired frame;
                             i.e. acquire cost removed — the GPU-sync cost)
  T4 cached-frame grab      (acquire timeout when idle -> last frame, ms)
  T5 mss GDI grab           (the R-5 baseline)
"""
from __future__ import annotations

import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
sys.path.insert(0, os.path.join(REPO_ROOT, "benchmarks"))


def _ms(xs):
    xs = sorted(xs)
    return {"n": len(xs), "p50": xs[len(xs) // 2], "mean": sum(xs) / len(xs),
            "max": xs[-1]}


def fmt(name, s):
    print(f"  {name:44s} n={s['n']:>2} p50={s['p50']:7.1f}ms mean={s['mean']:7.1f}ms max={s['max']:7.1f}ms")


def main() -> int:
    import mss
    from PIL import Image
    from r6_dxgi_proto import Duplicator

    N = 14
    dup = Duplicator()
    W, H = dup.width, dup.height
    print(f"# duplication {W}x{H} rotation={dup.rotation}")

    # T4: cached grab when idle (fire while screen quiet: run FIRST, before prints)
    xs = []
    for _ in range(N):
        t0 = time.perf_counter()
        img = dup.grab(timeout_ms=1)
        xs.append((time.perf_counter() - t0) * 1000.0)
    fmt("T4 idle grab (timeout 1ms, cached/None)", _ms(xs))

    # T2: full readback grabs — the terminal printing THIS output keeps frames flowing
    xs = []
    kinds = []
    for i in range(N):
        t0 = time.perf_counter()
        img = dup.grab(timeout_ms=8)
        dt = (time.perf_counter() - t0) * 1000.0
        xs.append(dt)
        kinds.append("new" if img is not None else "timeout")
        print(f"    grab {i:2d}: {dt:6.1f}ms {kinds[-1]}", flush=True)
    fmt("T2 full readback grab (live screen)", _ms(xs))
    print(f"#    new={kinds.count('new')} timeout={kinds.count('timeout')}")

    # T3: readback-only on the last acquired frame (GPU sync + copy + PIL)
    frame_info = __import__("ctypes").create_string_buffer(64)
    res = __import__("ctypes").c_void_p()
    import ctypes
    hr = dup._dupv.call(8, ctypes.HRESULT,
        (ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
        8, ctypes.cast(frame_info, ctypes.c_void_p), ctypes.byref(res))
    got_frame = (hr == 0) and bool(res.value)
    xs = []
    if got_frame:
        for _ in range(N):
            t0 = time.perf_counter()
            dup._readback(res)
            xs.append((time.perf_counter() - t0) * 1000.0)
        dup._dupv.call(14, ctypes.HRESULT, ())
    fmt("T3 readback only (Copy+Map+copy, frame held)" if got_frame else "T3 skipped", _ms(xs) if xs else {"n": 0, "p50": 0, "mean": 0, "max": 0})

    # T5: mss baseline
    region = {"left": 0, "top": 0, "width": W, "height": H}
    with mss.MSS() as cap:
        cap.grab(region)
        xs = []
        for _ in range(N):
            t0 = time.perf_counter()
            shot = cap.grab(region)
            img = Image.frombuffer("RGB", shot.size, shot.raw, "raw", "BGRX", 0, 1)
            xs.append((time.perf_counter() - t0) * 1000.0)
    fmt("T5 mss grab + BGRX decode (baseline)", _ms(xs))

    dup.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
