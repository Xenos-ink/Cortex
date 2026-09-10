#!/usr/bin/env python
"""R-5 SPEED PROFILE HARNESS (ORVEX-CORTEX-056-LIVEFIX).

Benign local benchmark: screen READING only (mss capture + encode + digest +
verification diff + outbound bounding). NO input injection, NO clicks, no app
launching. Real capture/encode/digest/verification code, real desktop.

Stages measured (mirroring the live audit stages):
  1. observe()  backend full observation  (mss grab + PNG encode + window
     identity + monitors + UIA semantic read)
     sub-stages isolated individually:
       a. mss.grab raw                     (BitBlt -> raw RGB bytes)
       b. Image.frombytes + PNG save       (PIL encode)
       c. base64.b64encode
       d. _refresh_monitors()
       e. query_foreground_window()
       f. UIA semantic read (reader.read())
  2. observation_digest (sha256 over base64 payload)
  3. verification: VerificationEngine.verify (screenshot_diff path) over
     before/after observations captured milliseconds apart on a live screen
  4. _bound_outbound_image (REM-E JPEG ladder) on the captured PNG
  5. rate-gate wait cost probe: how long _observe() semantics would wait when
     consecutive gated captures arrive back-to-back (min_screenshot_interval_ms=250)
  6. full single-action mechanical path (no OS input): observe(validate) +
     verify + observe(post_action) + verify — reproducing the audit stage
     composition of the live sessions.
Usage:
  python benchmarks/r5_speed_profile.py [--iterations N] [--json OUT]
Writes human-readable report to stdout; --json writes machine-readable dict.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

import base64


def _ms(samples: list[float]) -> dict[str, float]:
    xs = sorted(samples)
    if not xs:
        return {"n": 0, "p50": float("nan"), "p95": float("nan"), "mean": float("nan"), "max": float("nan")}
    p95 = xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]
    return {
        "n": len(xs),
        "p50": xs[len(xs) // 2],
        "p95": p95,
        "mean": sum(xs) / len(xs),
        "max": xs[-1],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=15, help="samples per stage")
    parser.add_argument("--json", dest="json_out", default=None)
    args = parser.parse_args()
    iters = max(3, args.iterations)

    # import server for _bound_outbound_image (importable; no server start)
    from computer_use_mcp.backend import LocalComputerBackend
    from computer_use_mcp.limits import LimitEnforcer, Limits
    from computer_use_mcp.observation import observation_digest
    from computer_use_mcp.verification import (
        ScreenshotDiffStrategy,
        VerificationEngine,
        VerificationIntent,
        VerificationKind,
    )

    report: dict[str, dict] = {}

    backend = LocalComputerBackend()
    print(f"# capture backend ready; png_optimize={backend.png_optimize} "
          f"uia_enabled={backend._uia_enabled} reader={type(backend._semantic_reader).__name__ if backend._semantic_reader else None}")

    # ---------------------------------------------------------------- stage A/B: observe full
    samples = {"observe_full": []}
    for _ in range(iters):
        t0 = time.perf_counter()
        obs = backend.observe()
        samples["observe_full"].append((time.perf_counter() - t0) * 1000.0)
    report["observe_full"] = _ms(samples["observe_full"])
    print(f"observe_full: {report['observe_full']}")

    # --- sub-stage isolation (fresh iterations each) --------------------------
    monitor = backend._monitors[0] if backend._monitors else None
    if monitor is not None:
        left, top, width, height = monitor.bounds

        # a. mss.grab only
        grab_ms = []
        for _ in range(iters):
            t0 = time.perf_counter()
            with backend._mss_factory() as capture:
                raw = capture.grab({"left": left, "top": top, "width": width, "height": height})
            grab_ms.append((time.perf_counter() - t0) * 1000.0)
        report["mss_grab"] = _ms(grab_ms)

        from PIL import Image

        # b. frombytes + PNG save
        png_ms = []
        raw_bytes = raw.rgb
        size = raw.size
        for _ in range(iters):
            t0 = time.perf_counter()
            image = Image.frombytes("RGB", size, raw_bytes)
            out = io.BytesIO()
            image.save(out, format="PNG", optimize=backend.png_optimize)
            png_ms.append((time.perf_counter() - t0) * 1000.0)
        report["pil_png_encode"] = _ms(png_ms)
        png_bytes = out.getvalue()

        # c. base64 encode
        b64_ms = []
        for _ in range(iters):
            t0 = time.perf_counter()
            _ = base64.b64encode(png_bytes).decode("ascii")
            b64_ms.append((time.perf_counter() - t0) * 1000.0)
        report["b64_encode"] = _ms(b64_ms)
        png_size_kb = len(png_bytes) / 1024.0
        report["png_size_kb"] = {"n": 1, "p50": png_size_kb, "p95": png_size_kb, "mean": png_size_kb, "max": png_size_kb}
        print(f"screen {size[0]}x{size[1]}, png {png_size_kb:.1f} KB")

    # d. _refresh_monitors
    mon_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        backend._refresh_monitors()
        mon_ms.append((time.perf_counter() - t0) * 1000.0)
    report["refresh_monitors"] = _ms(mon_ms)

    # e. query_foreground_window
    fgw_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        backend.query_foreground_window()
        fgw_ms.append((time.perf_counter() - t0) * 1000.0)
    report["query_foreground_window"] = _ms(fgw_ms)

    # f. UIA semantic read
    if backend._semantic_reader is not None:
        uia_ms = []
        for _ in range(iters):
            t0 = time.perf_counter()
            backend._uia_semantic_fields(monitor, backend._active_context.verdict if backend._active_context else None)
            uia_ms.append((time.perf_counter() - t0) * 1000.0)
        report["uia_semantic_read"] = _ms(uia_ms)
    else:
        report["uia_semantic_read"] = {"n": 0, "p50": 0.0, "p95": 0.0, "mean": 0.0, "max": 0.0}

    # ---------------------------------------------------------------- stage C: digest
    obs = backend.observe()
    dig_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        observation_digest(obs)
        dig_ms.append((time.perf_counter() - t0) * 1000.0)
    report["observation_digest"] = _ms(dig_ms)

    # ---------------------------------------------------------------- stage D: verification
    engine = VerificationEngine()  # default chain, judge None
    intent = VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=True)
    ver_ms = []
    before = backend.observe()
    for _ in range(iters):
        after = backend.observe()
        t0 = time.perf_counter()
        engine.verify(intent, before, after)
        ver_ms.append((time.perf_counter() - t0) * 1000.0)
    report["verify_screenshot_diff"] = _ms(ver_ms)

    # also isolate ScreenshotDiffStrategy alone
    strategy = ScreenshotDiffStrategy()
    ver_ms2 = []
    for _ in range(iters):
        t0 = time.perf_counter()
        strategy.verify(intent, before, after)
        ver_ms2.append((time.perf_counter() - t0) * 1000.0)
    report["verify_diff_only"] = _ms(ver_ms2)

    # decode isolation (Image.open(b64decode))
    import base64 as _b64

    from PIL import Image

    dec_ms = []
    payload = before.image_base64
    for _ in range(iters):
        t0 = time.perf_counter()
        img = Image.open(io.BytesIO(_b64.b64decode(payload))).convert("RGB")
        dec_ms.append((time.perf_counter() - t0) * 1000.0)
    report["diff_decode_one_side"] = _ms(dec_ms)
    del img

    # ---------------------------------------------------------------- stage E: outbound bound (REM-E)
    bound_ms = []
    enc_counts = []
    import computer_use_mcp.server as srv

    orig_save = Image.Image.save

    def counting_save(self, *a, **k):
        enc_counts.append(1)
        return orig_save(self, *a, **k)

    for _ in range(iters):
        enc_counts.clear()
        t0 = time.perf_counter()
        bounded, mime = srv._bound_outbound_image(before.image_base64)
        bound_ms.append((time.perf_counter() - t0) * 1000.0)
    report["bound_outbound_image"] = {**_ms(bound_ms), "jpeg_encodes_per_call": len(enc_counts)}
    report["bound_outbound_mime"] = {"n": 1, "p50": 1.0 if mime == "image/jpeg" else 0.0,
                                     "p95": 0.0, "mean": 0.0, "max": 0.0}

    # ladder depth probe: capture the PNG->JPEG path with save counting
    enc_counts.clear()
    Image.Image.save = counting_save
    try:
        _b, _m = srv._bound_outbound_image(before.image_base64)
    finally:
        Image.Image.save = orig_save
    report["bound_outbound_encodes"] = {"n": 1, "p50": float(len(enc_counts)), "p95": float(len(enc_counts)),
                                        "mean": float(len(enc_counts)), "max": float(len(enc_counts))}

    # ---------------------------------------------------------------- stage F: rate-gate wait probe
    limits = Limits(min_screenshot_interval_ms=250)
    enforcer = LimitEnforcer(limits)
    waits = []
    for _ in range(iters):
        enforcer.record_screenshot()
        t0 = time.perf_counter()
        waited = 0.0
        while not enforcer.can_screenshot():
            time.sleep(0.05)
            waited += 0.05
            if waited > 2.0:
                break
        waits.append((time.perf_counter() - t0) * 1000.0)
    report["rate_gate_wait_after_capture"] = _ms(waits)

    # ---------------------------------------------------------------- stage G: composite per-action
    # Mechanical composition of the live audit path per action:
    #   observe(validate) + [execution stub 0ms] + observe(post_action) + verify
    comp_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        src = backend.observe()
        after = backend.observe()
        engine.verify(intent, src, after)
        comp_ms.append((time.perf_counter() - t0) * 1000.0)
    report["composite_action_two_observes_plus_verify"] = _ms(comp_ms)

    # composite with digest comparisons (validator staleness + strict digest)
    from computer_use_mcp.observation import digest_matches

    comp2 = []
    for _ in range(iters):
        t0 = time.perf_counter()
        src = backend.observe()
        current = backend.observe()
        digest_matches(src, current)
        after = backend.observe()
        digest_matches(current, after)
        engine.verify(intent, src, after)
        comp2.append((time.perf_counter() - t0) * 1000.0)
    report["composite_three_observes_digest_verify"] = _ms(comp2)

    print("\n=== R-5 SPEED PROFILE (ms) ===")
    for key, stats in report.items():
        if key.endswith("_kb") or key.startswith("bound_outbound_mime"):
            print(f"  {key}: p50={stats['p50']:.1f}")
            continue
        print(f"  {key}: n={stats['n']} p50={stats['p50']:.1f} p95={stats['p95']:.1f} mean={stats['mean']:.1f} max={stats['max']:.1f}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"# json written: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
