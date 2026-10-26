"""cortex_text_ocr — the optional Windows.Media.Ocr text substrate for Cortex (A3).

The side package behind Cortex's pluggable text-substrate seam
(``computer_use_mcp.text_substrates``; design of record
``research/AVR009-substrate-design.md`` §3 — the documented stability contract).

Contract (exactly what the seam calls):

    regions(monitor, verdict, timeout_budget, *, frame=None) -> list[dict]

plus the documented module-level ``available() -> bool``. ZERO runtime
dependencies: Python stdlib + one bundled PowerShell script (``ocr.ps1``,
Windows PowerShell 5.1 + WinRT projections) driven through ``subprocess``.

Engine (Windows.Media.Ocr — WORKING invocation shape on this machine; supersedes
EXP-020.1's naive single-projection probe, which NullReferenceException'd):

1. load BOTH projections: ``[Windows.Media.Ocr.OcrEngine, Windows.Foundation,
   ContentType=WindowsRuntime]`` AND ``[Windows.Globalization.Language, ...]``
   (plus BitmapDecoder/StorageFile for the pixel path);
2. create via ``TryCreateFromUserProfileLanguages()`` (explicit
   ``TryCreateFromLanguage(Language("en-US"|"en-GB"|"en"))`` kept as fallback);
3. recognize via the WinRT-await pattern (the
   ``System.WindowsRuntimeSystemExtensions`` AsTask reflection helper) over
   ``StorageFile.GetFileFromPathAsync`` -> ``BitmapDecoder.GetSoftwareBitmapAsync``
   -> ``OcrEngine.RecognizeAsync``.

Honesty contract:

- Coordinates: the ``frame`` given IS the screenshot (screenshot-local by
  construction), so OCR rects (frame pixel coordinates) are returned AS-IS.
  ``verdict.scale_x/scale_y`` is deliberately NOT applied: it converts
  PHYSICAL-space rects (the UIA needle's case); a pixel substrate measures in
  screenshot space directly — applying the scale would double-convert.
- Confidence: Windows.Media.Ocr provides none — every region ships
  ``confidence: None`` (never an invented 0.0; A1.1 doctrine).
- Fail-closed: ANY failure (engine unavailable, PowerShell error, timeout,
  unparsable output, no frame) raises ``RuntimeError`` with a one-line reason —
  the seam converts that into the honest ``substrate_error`` record and serves
  the UIA regions (the observation never fails). Malformed per-line rects are
  DROPPED rather than served.
- Control-character-safe JSON parse (AVR-010 live bug): a real terminal's text
  can carry raw C0 control characters that break a strict JSON parse. The parse
  tries ``json.loads(raw, strict=False)`` first; on failure it escapes raw
  control chars (\\x00-\\x1f except \\t\\n\\r) and retries ONCE — and when that
  sanitize retry is what saved the parse, every served region dict is stamped
  with the honest ``"json_sanitized": True`` (absent on the clean path).
- Provenance: engine identifier :data:`SUBSTRATE_ID` (``"windows-media"``);
  the served block token is seam-owned (``ocr:cortex_text_ocr``).

Timeout contract (recorded deviation): ``timeout_budget`` is honored as the
subprocess kill deadline WHEN it is at least the PowerShell transport floor
(:data:`PS_OCR_TIMEOUT_FLOOR_SECONDS` — a cold powershell.exe + WinRT
projection load cannot complete inside the seam's 50 ms-class UIA budget);
below the floor the floor is enforced instead. The ACTUAL elapsed milliseconds
surface in the block's ``substrate_ms`` / ``spatial_text_ms`` — cost is
REPORTED, never gated (A3 §5; installing the package IS the latency
acceptance).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import Any

__version__ = "0.1.0"

#: Engine identifier (provenance). The block-level ``substrate`` token is
#: seam-owned ("ocr:cortex_text_ocr"); this names the ENGINE inside the package.
SUBSTRATE_ID = "windows-media"

#: Contract region cap — the same ``UIA_MAX_ELEMENTS`` class the seam enforces
#: (a larger result is a contract violation; the package self-caps in reading order).
MAX_REGIONS = 30

#: Stock text truncation convention (the UIA needle's).
_TEXT_TRUNCATE = 200

#: Hard kill deadline floor for the PowerShell transport, in seconds. The seam's
#: stock budget (``SIDE_SUBSTRATE_BUDGET_SECONDS = 0.05``) is the UIA needle's
#: 50 ms class — BELOW any PowerShell cold start; enforcing it verbatim would make
#: the package time out on every call. A budget >= the floor is honored verbatim;
#: a smaller budget is floored (recorded deviation, A3 §5: cost reported, never
#: gated). Measured transport cost on the dev machine: ~0.6 s per OCR call.
PS_OCR_TIMEOUT_FLOOR_SECONDS = 5.0

#: ``available()`` probe subprocess deadline (seconds; once per process).
PROBE_TIMEOUT_SECONDS = 15.0

_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr.ps1")

_available_cache: bool | None = None
_last_error: str | None = None

#: Raw C0 control characters that can break a JSON parse. \t \n \r are exempt
#: (JSON whitespace / self-escapable).
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

#: True when the MOST RECENT parse was saved by the sanitize retry (never reset
#: to True by anything else; every parse sets it honestly).
_last_parse_sanitized: bool = False


def _sanitize_control_chars(raw: str) -> str:
    """Escape raw C0 control chars (except \\t\\n\\r) as literal \\uXXXX text."""
    return _CTRL_CHARS_RE.sub(lambda m: f"\\u{ord(m.group(0)):04x}", raw)


def _parse_ps_output(raw: str) -> dict[str, Any]:
    """Parse one line of ocr.ps1 JSON, control-char-safe (raise RuntimeError).

    Belt-and-braces for the live-found bug (real terminal text carried a raw
    control character inside a string value, which broke the old strict parse;
    the seam's fail-open held but the observation lost its OCR):

    1. ``json.loads(raw, strict=False)`` first — accepts raw control characters
       inside string values;
    2. on JSONDecodeError, escape raw control chars (\\x00-\\x1f except
       \\t\\n\\r) and retry ONCE; when that retry saved the parse,
       :data:`_last_parse_sanitized` is True and :func:`regions` stamps each
       served region dict with ``"json_sanitized": True``.
    """
    global _last_parse_sanitized
    _last_parse_sanitized = False
    try:
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError as first_error:
        try:
            data = json.loads(_sanitize_control_chars(raw))
        except json.JSONDecodeError:
            raise RuntimeError(
                f"cortex_text_ocr: PowerShell output is not valid JSON ({first_error})"
            ) from first_error
        _last_parse_sanitized = True
    if not isinstance(data, dict):
        raise RuntimeError("cortex_text_ocr: PowerShell output is not a JSON object")
    return data


def last_error() -> str | None:
    """The most recent failure reason (None when the last operation succeeded)."""
    return _last_error


def _run_ps(image_path: str | None, timeout: float) -> dict[str, Any]:
    """Run the bundled ocr.ps1 once; return its parsed JSON (raise RuntimeError)."""
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-NoLogo",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        _SCRIPT_PATH,
    ]
    if image_path:
        cmd += ["-Image", image_path]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=creationflags,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "cortex_text_ocr: powershell.exe not found; Windows.Media.Ocr transport "
            "requires Windows PowerShell"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"cortex_text_ocr: PowerShell OCR timed out after {timeout:.1f}s "
            "(process killed); failing closed"
        ) from exc
    stdout = (completed.stdout or b"").decode("utf-8", errors="replace")
    stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
    start = stdout.find("{")
    if start < 0:
        tail = stderr.strip().splitlines()[-1] if stderr.strip() else "no output"
        raise RuntimeError(f"cortex_text_ocr: unparsable PowerShell output ({tail})")
    return _parse_ps_output(stdout[start:].strip())


def available() -> bool:
    """True only when a Windows.Media.Ocr engine TRULY creates (probe cached once).

    Runs the engine-creation probe (ocr.ps1, no image) exactly once per process and
    caches the verdict; the cached result is re-consulted cheaply on every call.
    False is the honest UNAVAILABLE-DEFEASIBLE answer (broken language-pack
    configuration, no powershell.exe, probe timeout) — the reason is kept in
    :func:`last_error`.
    """
    global _available_cache, _last_error
    if _available_cache is not None:
        return _available_cache
    try:
        data = _run_ps(None, PROBE_TIMEOUT_SECONDS)
    except RuntimeError as exc:
        _last_error = str(exc)
        _available_cache = False
        return False
    if data.get("ok"):
        _available_cache = True
        _last_error = None
    else:
        _available_cache = False
        _last_error = f"cortex_text_ocr: engine unavailable: {data.get('reason', 'unknown')}"
    return _available_cache


def regions(
    monitor: Any,
    verdict: Any,
    timeout_budget: float,
    *,
    frame: Any = None,
) -> list[dict[str, Any]]:
    """OCR the frame; return screenshot-local TextRegion-compatible dicts (fail-closed).

    ``frame`` is the observation's PIL Image — the screenshot itself, so OCR rects
    are already screenshot-local and are returned as-is (see module docstring).
    ``monitor``/``verdict`` are accepted per the contract and deliberately unused.
    Windows.Media.Ocr measures NO confidence: every entry ships ``confidence: None``.
    Any failure raises ``RuntimeError`` with a one-line reason (the seam records it
    as ``substrate_error`` and serves the UIA regions — the observation never fails).
    """
    global _last_error
    if frame is None:
        _last_error = (
            "cortex_text_ocr: no frame supplied; a pixel substrate requires the "
            "observation frame"
        )
        raise RuntimeError(_last_error)
    deadline = max(float(timeout_budget), PS_OCR_TIMEOUT_FLOOR_SECONDS)
    if not available():
        raise RuntimeError(_last_error or "cortex_text_ocr: engine unavailable")

    handle, png_path = tempfile.mkstemp(prefix="cortex_text_ocr_", suffix=".png")
    os.close(handle)
    try:
        rgb = frame if frame.mode == "RGB" else frame.convert("RGB")
        rgb.save(png_path, format="PNG")
        data = _run_ps(png_path, deadline)
    except RuntimeError as exc:
        _last_error = str(exc)
        raise
    except Exception as exc:  # noqa: BLE001 - any frame failure fails closed
        _last_error = f"cortex_text_ocr: frame handling failed: {type(exc).__name__}: {exc}"
        raise RuntimeError(_last_error) from exc
    finally:
        try:
            os.unlink(png_path)
        except OSError:
            pass

    if not data.get("ok"):
        _last_error = f"cortex_text_ocr: {data.get('reason', 'unknown')}"
        raise RuntimeError(_last_error)

    # Honest provenance (AVR-010 live bug): when the control-char sanitize retry
    # is what made THIS parse possible, stamp every served region with it.
    sanitized = _last_parse_sanitized

    out: list[dict[str, Any]] = []
    for line in data.get("lines") or []:
        try:
            text = str(line.get("text", "")).strip()
            x = int(round(float(line.get("x"))))
            y = int(round(float(line.get("y"))))
            width = int(round(float(line.get("width"))))
            height = int(round(float(line.get("height"))))
        except (TypeError, ValueError):
            continue  # malformed rect: dropped, never served
        if not text or x < 0 or y < 0 or width <= 0 or height <= 0:
            continue  # contract-violating entry: dropped, never served
        region: dict[str, Any] = {
            "text": text[:_TEXT_TRUNCATE],
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "confidence": None,
        }
        if sanitized:
            region["json_sanitized"] = True
        out.append(region)
        if len(out) >= MAX_REGIONS:
            break
    _last_error = None
    return out
