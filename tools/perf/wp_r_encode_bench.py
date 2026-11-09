"""WP-R (ORVEX-2026-109) encode bench: a synthetic 10 s x 5 fps 1280x720
recording through the REAL ``encode_video`` path, decoded back with av.

Run with the gate interpreter:
    PYTHONDONTWRITEBYTECODE=1 D:/Cortex/orvex-108-gate-venv/Scripts/python.exe tools/perf/wp_r_encode_bench.py

Prints the measured encode wall time, the produced MP4 size and the decoded
frame count/duration — the numbers reported in .orvex/mission/artifacts/gates/
wp-r-notes.md.  Nothing is written to disk; the MP4 lives in memory only.
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from PIL import Image, ImageDraw  # noqa: E402

from computer_use_mcp.vision.recorder import (  # noqa: E402
    RecordedFrame,
    Recording,
    _decode_video_info,
    encode_video,
)

WIDTH, HEIGHT, FPS, SECONDS = 1280, 720, 5, 10


def _frame(index: int) -> RecordedFrame:
    image = Image.new("RGB", (WIDTH, HEIGHT), (235, 235, 235))
    draw = ImageDraw.Draw(image)
    x = (index * 97) % (WIDTH - 320)
    y = (index * 53) % (HEIGHT - 160)
    draw.rectangle((x, y, x + 300, y + 140), fill=(40, 90, 160))
    draw.ellipse((WIDTH // 2 - 60, HEIGHT // 2 - 60, WIDTH // 2 + 60, HEIGHT // 2 + 60),
                 outline=(200, 60, 60), width=4)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=70)
    return RecordedFrame(
        t_ms=index * 1000.0 / FPS,
        jpeg=buffer.getvalue(),
        pointer_x=float(WIDTH // 2),
        pointer_y=float(HEIGHT // 2),
        pointer_state="pressed" if index % 5 == 0 else "normal",
        in_image=True,
        width=WIDTH,
        height=HEIGHT,
    )


def main() -> None:
    count = SECONDS * FPS
    recording = Recording(
        record_id="rec_bench00000000",
        session_id="sess_bench",
        duration_s=SECONDS,
        fps=FPS,
        requested_monotonic_start=0.0,
        region=[0, 0, WIDTH, HEIGHT],
        monitor_bounds=[0, 0, WIDTH, HEIGHT],
        canonical_origin=(0, 0),
        canonical_size=(WIDTH, HEIGHT),
    )
    started = time.perf_counter()
    recording.frames = [_frame(index) for index in range(count)]
    synth_seconds = time.perf_counter() - started
    encode_started = time.perf_counter()
    data, info = encode_video(recording, fps=FPS)
    encode_seconds = time.perf_counter() - encode_started
    decode_started = time.perf_counter()
    decoded = _decode_video_info(data) if data is not None else {}
    decode_seconds = time.perf_counter() - decode_started
    print(f"frames={count} size={WIDTH}x{HEIGHT} fps={FPS} seconds={SECONDS}")
    print(f"synthetic_frame_build_s={synth_seconds:.3f}")
    print(f"encode_s={encode_seconds:.3f}")
    print(f"video_bytes={len(data) if data else 0}")
    print(f"video_info={info}")
    print(f"decode_s={decode_seconds:.3f} decoded={decoded}")


if __name__ == "__main__":
    main()
