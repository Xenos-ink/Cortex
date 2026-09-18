# cortex_text_ocr — the optional OCR text substrate for Cortex

An OPTIONAL side package that gives [Cortex](../../../) a real visual OCR text
substrate: **Windows.Media.Ocr** (the Windows built-in OCR engine) driven through a
bundled PowerShell/WinRT script. It is NOT installed by default and nothing imports it
unless it is present — installing it is the whole opt-in.

## Install

From the repo root:

```
<venv>/Scripts/python -m pip install ./sidepackages/cortex_text_ocr
```

or answer `y` at the installer prompt (`cortex-mcp install` / `cortex-mcp update` offer
it after their normal steps). Uninstall/reject any time — reversible, additive only:

```
<venv>/Scripts/python -m pip uninstall cortex_text_ocr
```

## How it is used (zero config)

Cortex auto-detects the package by its well-known name (`cortex_text_ocr`) on EVERY
spatial-text build — no config file, no env toggle, no server restart needed. When
present and available, its OCR regions are merged AFTER the stock UIA regions (UIA wins
overlaps; OCR adds coverage for pixel-only text). The spatial-text block names its
source: `"substrate": "ocr:cortex_text_ocr"` (default `"uia"`). Detection is per-build,
so a mid-session install takes effect on the next observation.

## Latency (the implicit-acceptance note)

Installing this package IS your acceptance of the added per-observation latency: each
spatial-text build with OCR runs a PowerShell subprocess (~0.6 s measured on the dev
machine; hard-killed at a 5 s floor — see the timeout note in the package docstring).
The cost is never silent: it is reported in the block (`substrate_ms`) and in the
server's `spatial_text_ms` metric. Cost is REPORTED, never gated; if the OCR call fails
or times out, the observation falls back to the stock UIA regions with an honest
`substrate_error` record — the observation itself never fails.

## Honest availability semantics

`available()` returns True only when a Windows.Media.Ocr engine TRULY creates (probed
once per process, then cached). On a machine with a broken/incomplete language-pack
configuration it returns False and Cortex silently keeps the UIA default. The engine
is created with the dual-projection + `TryCreateFromUserProfileLanguages()` + WinRT
await pattern (the naive single-projection probe of EXP-020.1 NullReferenceException'd;
the working shape supersedes it).

## What it returns

`regions(monitor, verdict, timeout_budget, *, frame=None)` — screenshot-local dict
regions (`text`, `x`, `y`, `width`, `height`, `confidence`), at most 30, reading order.
The frame given IS the screenshot, so OCR rects are screenshot-local as-is (the
verdict scale is deliberately NOT applied — that converts physical-space rects, and a
pixel substrate measures in screenshot space directly). Windows.Media.Ocr measures no
confidence: every region ships `confidence: None` — never an invented number.
