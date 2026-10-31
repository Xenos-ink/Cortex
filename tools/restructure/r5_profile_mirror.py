#!/usr/bin/env python
"""Signature-correct mirror of benchmarks/r5_speed_profile.py (WP-R0).

The upstream benchmark's stage E unpacks a stale 2-tuple from
``server._bound_outbound_image`` (it returns ``(base64, mimeType, appliedScale)``
since WP5's H7 change). ``benchmarks/`` is frozen during WP-R0, so this mirror
replicates the upstream stages and order EXACTLY, with only that unpack
adapted (3-tuple). Screen READING only: mss grab + PNG encode + digest +
verification diff + outbound bound + rate-gate wait probe. It injects NO input
and launches NO application.

Usage:
  python tools/restructure/r5_profile_mirror.py --iterations N --json OUT
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from PIL import Image


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
    parser.add_argument("--iterations", type=int, default=15)
    parser.add_argument("--json", dest="json_out", default=None)
    args = parser.parse_args()
    iters = max(3, args.iterations)

    from computer_use_mcp.backend import LocalComputerBackend
    from computer_use_mcp.limits import LimitEnforcer, Limits
    from computer_use_mcp.observation import digest_matches, observation_digest
    from computer_use_mcp.verification import (
        ScreenshotDiffStrategy,
        VerificationEngine,
        VerificationIntent,
        VerificationKind,
    )
    import computer_use_mcp.server as srv

    report: dict[str, dict] = {}
    backend = LocalComputerBackend()

    samples = {"observe_full": []}
    for _ in range(iters):
        t0 = time.perf_counter()
        obs = backend.observe()
        samples["observe_full"].append((time.perf_counter() - t0) * 1000.0)
    report["observe_full"] = _ms(samples["observe_full"])

    monitor = backend._monitors[0] if backend._monitors else None
    if monitor is not None:
        left, top, width, height = monitor.bounds

        grab_ms = []
        for _ in range(iters):
            t0 = time.perf_counter()
            with backend._mss_factory() as capture:
                raw = capture.grab({"left": left, "top": top, "width": width, "height": height})
            grab_ms.append((time.perf_counter() - t0) * 1000.0)
        report["mss_grab"] = _ms(grab_ms)

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

        b64_ms = []
        for _ in range(iters):
            t0 = time.perf_counter()
            _ = base64.b64encode(png_bytes).decode("ascii")
            b64_ms.append((time.perf_counter() - t0) * 1000.0)
        report["b64_encode"] = _ms(b64_ms)
        png_size_kb = len(png_bytes) / 1024.0
        report["png_size_kb"] = {"n": 1, "p50": png_size_kb, "p95": png_size_kb, "mean": png_size_kb, "max": png_size_kb}

    mon_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        backend._refresh_monitors()
        mon_ms.append((time.perf_counter() - t0) * 1000.0)
    report["refresh_monitors"] = _ms(mon_ms)

    fgw_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        backend.query_foreground_window()
        fgw_ms.append((time.perf_counter() - t0) * 1000.0)
    report["query_foreground_window"] = _ms(fgw_ms)

    if backend._semantic_reader is not None:
        uia_ms = []
        for _ in range(iters):
            t0 = time.perf_counter()
            backend._uia_semantic_fields(monitor, backend._active_context.verdict if backend._active_context else None)
            uia_ms.append((time.perf_counter() - t0) * 1000.0)
        report["uia_semantic_read"] = _ms(uia_ms)
    else:
        report["uia_semantic_read"] = {"n": 0, "p50": 0.0, "p95": 0.0, "mean": 0.0, "max": 0.0}

    obs = backend.observe()
    dig_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        observation_digest(obs)
        dig_ms.append((time.perf_counter() - t0) * 1000.0)
    report["observation_digest"] = _ms(dig_ms)

    engine = VerificationEngine()
    intent = VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=True)
    ver_ms = []
    before = backend.observe()
    for _ in range(iters):
        after = backend.observe()
        t0 = time.perf_counter()
        engine.verify(intent, before, after)
        ver_ms.append((time.perf_counter() - t0) * 1000.0)
    report["verify_screenshot_diff"] = _ms(ver_ms)

    strategy = ScreenshotDiffStrategy()
    ver_ms2 = []
    for _ in range(iters):
        t0 = time.perf_counter()
        strategy.verify(intent, before, after)
        ver_ms2.append((time.perf_counter() - t0) * 1000.0)
    report["verify_diff_only"] = _ms(ver_ms2)

    dec_ms = []
    payload = before.image_base64
    for _ in range(iters):
        t0 = time.perf_counter()
        img = Image.open(io.BytesIO(base64.b64decode(payload))).convert("RGB")
        dec_ms.append((time.perf_counter() - t0) * 1000.0)
    report["diff_decode_one_side"] = _ms(dec_ms)
    del img

    bound_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        bounded, mime, _scale = srv._bound_outbound_image(before.image_base64)
        bound_ms.append((time.perf_counter() - t0) * 1000.0)
    report["bound_outbound_image"] = _ms(bound_ms)
    report["bound_outbound_mime"] = {"n": 1, "p50": 1.0 if mime == "image/jpeg" else 0.0,
                                     "p95": 0.0, "mean": 0.0, "max": 0.0}

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

    comp_ms = []
    for _ in range(iters):
        t0 = time.perf_counter()
        src = backend.observe()
        after = backend.observe()
        engine.verify(intent, src, after)
        comp_ms.append((time.perf_counter() - t0) * 1000.0)
    report["composite_action_two_observes_plus_verify"] = _ms(comp_ms)

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

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    else:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
