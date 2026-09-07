"""Independent scorer for the harsh benchmark tasks (benchmarks/tasks_hard/).

Evaluates a task YAML's ``predicates`` against the REAL machine/file/window state —
never driver self-report. Stdlib only, plus PIL for the mspaint pixel analysis
(PIL ships in the venv; no new dependencies are introduced).

CLI:
    # deterministic re-seed of the task's INPUT fixtures (never outputs):
    python benchmarks/score_task.py --seed --task benchmarks/tasks_hard/h1-....yaml

    # score a completed driver run:
    python benchmarks/score_task.py --task <yaml> --run-record run.json [--out scoring.json] \
        [--model "glm-flash"] [--no-log]

run record (written by the launcher; see tasks_hard/DRIVER-PROTOCOL.md):
    {"task": ..., "start_utc": iso8601, "end_utc": iso8601, "evidence_dir": path,
     "actions": int, "model_calls": int, "retries": int, "notes": str}

Output scoring JSON:
    {task, wall_time_s, human_reference_s, ratio, completed, precision,
     actions, model_calls, retries, notes, predicates: [{name, outcome, evidence}]}

Automatic run log (USER-COMMISSIONED: every scored run must leave a visible trace):
after every successful scoring invocation the scorer APPENDS one record to
    benchmarks/runs-log.jsonl  (machine-readable, append-only, one JSON line per run)
    benchmarks/RUNS.md         (human-readable table, newest first, capped at the last 200)
The record carries {ts_utc, task, run_id, model, wall_time_s, human_reference_s, ratio,
completed, precision, actions, retries, guard_events, anomalies_count, notes}.
``model`` is a free-text driver/model label passed via ``--model`` (e.g. "glm-flash",
"gpt-4o"); ``run_id`` / ``anomalies_count`` / ``guard_events`` come from the run record
when it carries them. Pass ``--no-log`` to opt out. Logging is additive: all existing
CLI invocations keep working unchanged.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import re
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

REPO_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURES = REPO_ROOT / "benchmarks" / "tasks_hard" / "fixtures"
GRACE_SECONDS = 1.0  # clock-skew allowance for the mtime >= start check


# --- small helpers -------------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _resolve_path(spec: str) -> Path:
    if spec.startswith("fixtures://"):
        return FIXTURES / spec[len("fixtures://"):]
    return Path(spec).expanduser()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _norm_alnum(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fixture(name: str) -> Path:
    path = FIXTURES / name
    if not path.exists():
        raise FileNotFoundError(f"canonical fixture missing: {path}")
    return path


# --- xlsx reading (stdlib zip + XML) --------------------------------------------------------------


class XlsxSheet:
    """Minimal read-only view of an Excel .xlsx (first worksheet)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.shared: list[str] = []
        self.cells: dict[str, dict[str, Any]] = {}
        self.formulas: list[str] = []
        self.fonts_bold: list[bool] = []
        self.cell_xfs_font_ids: list[int] = []
        with zipfile.ZipFile(path) as bundle:
            self._read_shared_strings(bundle)
            self._read_sheet(bundle)
            self._read_styles(bundle)

    @staticmethod
    def _sheet_member(bundle: zipfile.ZipFile) -> str:
        """Resolve the first sheet's part from workbook.xml (fallback sheet1.xml)."""
        names = set(bundle.namelist())
        fallback = "xl/worksheets/sheet1.xml"
        try:
            workbook = ET.fromstring(bundle.read("xl/workbook.xml"))
            rels = ET.fromstring(bundle.read("xl/_rels/workbook.xml.rels"))
            rel_target: dict[str, str] = {}
            for rel in rels:
                rel_target[rel.get("Id", "")] = rel.get("Target", "")
            for element in workbook.iter():
                if _local(element.tag) == "sheet":
                    rid = element.get(
                        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", ""
                    )
                    target = rel_target.get(rid, "")
                    if target:
                        normalized = target.lstrip("/")
                        if not normalized.startswith("xl/"):
                            normalized = "xl/" + normalized
                        if normalized in names:
                            return normalized
        except (KeyError, ET.ParseError):
            pass
        return fallback if fallback in names else next(
            (n for n in sorted(names) if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)), fallback
        )

    def _read_shared_strings(self, bundle: zipfile.ZipFile) -> None:
        if "xl/sharedStrings.xml" not in bundle.namelist():
            return
        root = ET.fromstring(bundle.read("xl/sharedStrings.xml"))
        for si in root:
            if _local(si.tag) != "si":
                continue
            self.shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))

    def _read_sheet(self, bundle: zipfile.ZipFile) -> None:
        root = ET.fromstring(bundle.read(self._sheet_member(bundle)))
        for element in root.iter():
            tag = _local(element.tag)
            if tag == "c":
                ref = element.get("r", "")
                cell: dict[str, Any] = {
                    "t": element.get("t", "n"),
                    "s": element.get("s"),
                    "v": None,
                    "text": "",
                    "formula": None,
                }
                for child in element:
                    child_tag = _local(child.tag)
                    if child_tag == "v":
                        cell["v"] = child.text or ""
                    elif child_tag == "f":
                        cell["formula"] = child.text or ""
                        self.formulas.append(child.text or "")
                    elif child_tag == "is":
                        cell["text"] = "".join(
                            t.text or "" for t in child.iter() if _local(t.tag) == "t"
                        )
                        cell["t"] = "inlineStr"
                if cell["t"] == "s" and cell["v"] is not None:
                    index = int(cell["v"])
                    cell["text"] = self.shared[index] if index < len(self.shared) else ""
                self.cells[ref] = cell
        self._root = root

    def _read_styles(self, bundle: zipfile.ZipFile) -> None:
        if "xl/styles.xml" not in bundle.namelist():
            return
        root = ET.fromstring(bundle.read("xl/styles.xml"))
        for section in root:
            name = _local(section.tag)
            if name == "fonts":
                for font in section:
                    bold = False
                    for part in font.iter():
                        if _local(part.tag) == "b" and part.get("val", "1") not in ("0", "false"):
                            bold = True
                    self.fonts_bold.append(bold)
            elif name == "cellXfs":
                for xf in section:
                    if _local(xf.tag) == "xf":
                        self.cell_xfs_font_ids.append(int(xf.get("fontId", "0")))

    # -- accessors --------------------------------------------------------------------------------

    def cell_text(self, ref: str) -> str:
        return str(self.cells.get(ref, {}).get("text", ""))

    def cell_number(self, ref: str) -> float | None:
        cell = self.cells.get(ref)
        if not cell or cell.get("v") in (None, ""):
            return None
        if cell["t"] in ("s", "inlineStr", "str"):
            try:
                return float(str(cell.get("v") if cell["t"] == "str" else cell.get("text", "")).strip())
            except ValueError:
                return None
        try:
            return float(str(cell["v"]))
        except ValueError:
            return None

    def cell_bold(self, ref: str) -> bool:
        cell = self.cells.get(ref)
        if not cell:
            return False
        style = cell.get("s")
        if style is None:
            return False
        index = int(style)
        if index >= len(self.cell_xfs_font_ids):
            return False
        font_id = self.cell_xfs_font_ids[index]
        return bool(font_id < len(self.fonts_bold) and self.fonts_bold[font_id])

    def all_strings(self) -> list[str]:
        return self.shared + [c["text"] for c in self.cells.values() if c.get("text")]

    def column_strings(self, column: str) -> list[str]:
        pattern = re.compile(rf"^{column}(\d+)$")
        return [
            c.get("text", "")
            for ref, c in sorted(self.cells.items(), key=lambda kv: _cell_sort_key(kv[0]))
            if pattern.match(ref)
        ]


def _cell_sort_key(ref: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]+)(\d+)", ref)
    if not match:
        return (0, 0)
    letters, row = match.groups()
    column = 0
    for ch in letters:
        column = column * 26 + (ord(ch) - 64)
    return (int(row), column)


def _column_of(ref: str) -> str:
    match = re.fullmatch(r"([A-Z]+)\d+", ref)
    return match.group(1) if match else ""


# --- fixture-derived expectations -----------------------------------------------------------------


def _fixture_column_sum(spec: dict[str, Any]) -> float:
    path = _fixture(str(spec["file"]))
    column = int(spec.get("column", 3))
    total = 0.0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if spec.get("skip_header") and _looks_like_header(line):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= column:
            total += float(parts[column - 1])
    return total


def _looks_like_header(line: str) -> bool:
    return any(ch.isalpha() for ch in line.split(",")[-1])


def _fixture_totals(fixture: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for row in csv.reader(_fixture(fixture).read_text(encoding="utf-8").splitlines()):
        if len(row) >= 2 and row[0].strip() and row[0].strip().casefold() not in ("region", "category"):
            key = row[0].strip().casefold()
            totals[key] = totals.get(key, 0.0) + float(row[1])
    return totals


def _fixture_grid(fixture: str) -> list[list[str]]:
    return [row for row in csv.reader(_fixture(fixture).read_text(encoding="utf-8").splitlines()) if row]


# --- predicate evaluators ------------------------------------------------------------------------


def _pred_file_exists(spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    path = _resolve_path(spec["path"])
    return {"name": spec["name"], "outcome": path.is_file(),
            "evidence": {"path": str(path), "size": path.stat().st_size if path.is_file() else None}}


def _pred_file_fresh(spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    path = _resolve_path(spec["path"])
    if not path.is_file():
        return {"name": spec["name"], "outcome": False, "evidence": {"path": str(path), "error": "missing"}}
    mtime = path.stat().st_mtime
    start = ctx.get("start_dt")
    fresh = True if start is None else mtime >= start.timestamp() - GRACE_SECONDS
    return {"name": spec["name"], "outcome": fresh,
            "evidence": {"path": str(path), "mtime": mtime, "start_utc": ctx.get("start_utc")}}


def _pred_xlsx_contains_strings(spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    sheet = XlsxSheet(_resolve_path(spec["path"]))
    strings = sheet.all_strings()
    missing = [n for n in spec["needles"] if not any(n in s for s in strings)]
    return {"name": spec["name"], "outcome": not missing,
            "evidence": {"missing": missing, "strings_seen": len(strings)}}


def _pred_xlsx_min_string_hits(spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    sheet = XlsxSheet(_resolve_path(spec["path"]))
    strings = sheet.all_strings()
    hits = [n for n in spec["needles"] if any(n in s for s in strings)]
    minimum = int(spec.get("min", len(spec["needles"])))
    return {"name": spec["name"], "outcome": len(hits) >= minimum,
            "evidence": {"hits": len(hits), "required": minimum, "matched": hits}}


def _pred_xlsx_formula(spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    sheet = XlsxSheet(_resolve_path(spec["path"]))
    pattern = re.compile(spec["pattern"], re.IGNORECASE)
    matches = [f for f in sheet.formulas if pattern.search(f)]
    return {"name": spec["name"], "outcome": bool(matches),
            "evidence": {"formulas": sheet.formulas[:10], "matched": matches[:3]}}


def _pred_xlsx_cell_value(spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    sheet = XlsxSheet(_resolve_path(spec["path"]))
    ref = str(spec["ref"])
    tolerance = float(spec.get("tolerance", 0.01))
    if "expected_from_fixture" in spec:
        expected = _fixture_column_sum(spec["expected_from_fixture"])
        kind = "number"
    else:
        expected = spec["expected"]
        kind = str(spec.get("type", "number"))
    if kind == "number":
        actual = sheet.cell_number(ref)
        ok = actual is not None and abs(actual - float(expected)) <= tolerance
    else:
        actual = sheet.cell_text(ref).strip()
        ok = actual == str(expected).strip()
    cell = sheet.cells.get(ref, {})
    return {"name": spec["name"], "outcome": ok,
            "evidence": {"ref": ref, "expected": expected, "actual": actual,
                         "formula": cell.get("formula"), "raw": cell.get("v")}}


def _pred_xlsx_bold_cells(spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    sheet = XlsxSheet(_resolve_path(spec["path"]))
    results = {ref: sheet.cell_bold(ref) for ref in spec["cells"]}
    return {"name": spec["name"], "outcome": all(results.values()), "evidence": results}


def _pred_xlsx_rows_matching(spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    sheet = XlsxSheet(_resolve_path(spec["path"]))
    pattern = re.compile(spec["regex"])
    matches = [s for s in sheet.column_strings(spec.get("column", "A")) if pattern.match(s.strip())]
    return {"name": spec["name"], "outcome": len(matches) >= int(spec["min"]),
            "evidence": {"matches": len(matches), "required": int(spec["min"]), "samples": matches[:5]}}


def _pred_xlsx_spot_checks(spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    sheet = XlsxSheet(_resolve_path(spec["path"]))
    grid = _fixture_grid(spec["fixture"])
    numeric = re.compile(r"^-?\d+(\.\d+)?$")
    details: dict[str, Any] = {}
    ok = True
    for ref in spec["cells"]:
        match = re.fullmatch(r"([A-Z]+)(\d+)", ref)
        row, col_letter = int(match.group(2)), match.group(1)
        col_index = 0
        for ch in col_letter:
            col_index = col_index * 26 + (ord(ch) - 64)
        expected = grid[row - 1][col_index - 1].strip()
        if numeric.match(expected):
            actual = sheet.cell_number(ref)
            cell_ok = actual is not None and abs(actual - float(expected)) <= 0.01
            actual_repr = actual
        else:
            actual = sheet.cell_text(ref).strip()
            cell_ok = actual == expected
            actual_repr = actual
        details[ref] = {"expected": expected, "actual": actual_repr, "ok": cell_ok}
        ok = ok and cell_ok
    return {"name": spec["name"], "outcome": ok, "evidence": details}


def _pred_xlsx_computed_totals(spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    sheet = XlsxSheet(_resolve_path(spec["path"]))
    totals = _fixture_totals(spec["fixture"])
    tolerance = float(spec.get("tolerance", 0.01))
    details: dict[str, Any] = {}
    ok = True
    for offset, region in enumerate(spec["order"]):
        row = int(spec.get("start_row", 2)) + offset
        cat_ref = f"{spec.get('category_col', 'A')}{row}"
        val_ref = f"{spec.get('total_col', 'B')}{row}"
        cat_ok = sheet.cell_text(cat_ref).strip().casefold() == region.casefold()
        expected = totals.get(region.casefold())
        actual = sheet.cell_number(val_ref)
        val_ok = expected is not None and actual is not None and abs(actual - expected) <= tolerance
        details[region] = {"category_cell": cat_ref, "category_ok": cat_ok,
                           "value_cell": val_ref, "expected": expected, "actual": actual,
                           "value_ok": val_ok, "formula": sheet.cells.get(val_ref, {}).get("formula")}
        ok = ok and cat_ok and val_ok
    return {"name": spec["name"], "outcome": ok, "evidence": details}


def _pred_text_contains_normalized(spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    path = _resolve_path(spec["path"])
    content = _norm_alnum(path.read_text(encoding="utf-8", errors="replace")) if path.is_file() else ""
    missing = [n for n in spec["needles"] if _norm_alnum(n) not in content]
    return {"name": spec["name"], "outcome": path.is_file() and not missing,
            "evidence": {"missing": missing, "path": str(path)}}


def _pred_window_title_contains(spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from benchmarks.appwin import top_level_windows, window_class, window_text

    needle = str(spec["needle"]).casefold()
    klass = spec.get("class")
    titles = []
    found = False
    for hwnd in top_level_windows():
        title = window_text(hwnd)
        if not title:
            continue
        titles.append(title)
        if needle in title.casefold() and (klass is None or window_class(hwnd) == klass):
            found = True
    return {"name": spec["name"], "outcome": found,
            "evidence": {"needle": needle, "class": klass, "visible_titles": titles[:12]}}


def _pred_folder_layout(spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    root = _resolve_path(spec["root"])
    canonical_dir = _resolve_path(spec["canonical_dir"])
    prefix = str(spec.get("rename_prefix", ""))
    rule = {k.casefold(): v for k, v in spec["rule"].items()}
    allow_root = list(spec.get("allow_root", []))
    evidence: dict[str, Any] = {"root": str(root)}
    if not root.is_dir():
        return {"name": spec["name"], "outcome": False, "evidence": {**evidence, "error": "root missing"}}
    entries = sorted(p.name for p in root.iterdir())
    evidence["root_entries"] = entries
    layout_ok = entries == sorted(allow_root)
    evidence["root_exact"] = layout_ok
    files_ok = True
    details: dict[str, dict[str, Any]] = {}
    for src in sorted(canonical_dir.iterdir()):
        if not src.is_file():
            continue
        folder = rule.get(src.suffix.casefold())
        final = root / str(folder) / f"{prefix}{src.name}" if folder else None
        entry: dict[str, Any] = {"expected_folder": folder, "expected_name": f"{prefix}{src.name}"}
        if final is None or not final.is_file():
            entry["found"] = False
            files_ok = False
        else:
            entry["found"] = True
            entry["hash_ok"] = _sha256(final) == _sha256(src)
            files_ok = files_ok and entry["hash_ok"]
        details[src.name] = entry
    extras_ok = True
    for folder in allow_root:
        folder_path = root / folder
        if not folder_path.is_dir():
            extras_ok = False
            details[f"folder:{folder}"] = {"error": "folder missing"}
            continue
        actual = sorted(p.name for p in folder_path.iterdir())
        expected_names = sorted(
            f"{prefix}{src.name}" for src in canonical_dir.iterdir()
            if src.is_file() and rule.get(src.suffix.casefold(), "") == folder
        )
        if actual != expected_names:
            extras_ok = False
            details[f"folder:{folder}"] = {"expected": expected_names, "actual": actual}
    evidence["files"] = details
    evidence["folders_exact"] = extras_ok
    return {"name": spec["name"], "outcome": bool(layout_ok and files_ok and extras_ok), "evidence": evidence}


# --- mspaint pixel analysis (PIL; the only non-stdlib use) ----------------------------------------


def detect_canvas(pixels: Any, width: int, height: int) -> tuple[int, int, int, int] | None:
    """Locate the white mspaint canvas in a (maximized) window image.

    Bottom-anchored and shape-independent: scan UP from just above the bottom of the
    image — paint's gray-blue app background rows carry no long near-white run starting
    near the left edge; the first row that does is the canvas BOTTOM row. The canvas
    left/right come from a clean anchor row just above it, the top from walking a probe
    column (canvas_left + 5, always left of the task's target rectangle) upward while
    near-white. Returns (left, top, right, bottom) or None.
    """
    min_run = int(width * 0.2)

    def row_run(y: int) -> tuple[int, int] | None:
        x = 0
        best: tuple[int, int] | None = None
        while x < width:
            if _near_white(pixels[x, y]):
                x0 = x
                while x < width and _near_white(pixels[x, y]):
                    x += 1
                if x0 <= 45 and (best is None or x - x0 > best[1] - best[0]):
                    best = (x0, x - 1)
            else:
                x += 1
        return best if best and best[1] - best[0] >= min_run else None

    canvas_bottom = None
    for y in range(height - 60, 60, -1):
        if row_run(y) is not None:
            canvas_bottom = y
            break
    if canvas_bottom is None:
        return None
    anchor = row_run(canvas_bottom - 5) or row_run(canvas_bottom - 12)
    if anchor is None:
        return None
    canvas_left, canvas_right = anchor
    probe_x = min(canvas_left + 5, canvas_right - 5)
    canvas_top = canvas_bottom
    while canvas_top > 60 and _near_white(pixels[probe_x, canvas_top - 1]):
        canvas_top -= 1
    return (canvas_left, canvas_top, canvas_right, canvas_bottom)


def _pred_paint_rect_analysis(spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from benchmarks.appwin import (
        find_window,
        focus_window,
        is_visible,
        set_dpi_awareness,
        window_rect,
    )

    set_dpi_awareness()  # physical-pixel reads; scorer is its own process
    hwnd = find_window(class_name=str(spec.get("window_class", "MSPaintApp")))
    if hwnd is None or not is_visible(hwnd):
        return {"name": spec["name"], "outcome": False,
                "evidence": {"error": "no visible mspaint window (driver must leave it open)"}}

    from PIL import ImageGrab

    hwnd_int = int(hwnd)
    if ctypes.windll.user32.IsIconic(hwnd_int):
        ctypes.windll.user32.ShowWindow(hwnd_int, 9)  # SW_RESTORE
        time.sleep(0.6)
    focus_window(hwnd_int)  # the capture reads the screen region, so paint must be topmost
    time.sleep(0.4)
    left, top, width, height = window_rect(hwnd_int)
    screen_w = ctypes.windll.user32.GetSystemMetrics(0)
    screen_h = ctypes.windll.user32.GetSystemMetrics(1)
    right = min(left + width, screen_w)
    bottom = min(top + height, screen_h)
    left, top = max(left, 0), max(top, 0)
    width, height = right - left, bottom - top
    if width < 900 or height < 600:
        return {"name": spec["name"], "outcome": False,
                "evidence": {"error": "mspaint window too small; prompt requires a maximized window",
                             "rect": [left, top, width, height]}}
    screenshot = ImageGrab.grab(bbox=(left, top, left + width, top + height), all_screens=True)
    evidence_dir = ctx.get("evidence_dir")
    if evidence_dir:
        evidence_dir = Path(evidence_dir)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        screenshot.save(evidence_dir / "mspaint_window_after.png")
    pixels = screenshot.convert("RGB").load()

    # 1) canvas = the white sheet (bottom-anchored, shape-independent detection)
    canvas = detect_canvas(pixels, width, height)
    if canvas is None:
        return {"name": spec["name"], "outcome": False,
                "evidence": {"error": "canvas (large white region) not found", "rect": [left, top, width, height]}}
    canvas_left, canvas_top, canvas_right, canvas_bottom = canvas

    # 2) shape pixels: clearly-visible color (dark or saturated) inside the canvas crop
    candidates: list[tuple[int, int]] = []
    for y in range(canvas_top, canvas_bottom + 1):
        for x in range(canvas_left, canvas_right + 1):
            if _visible_ink(pixels[x, y]):
                candidates.append((x, y))
    if evidence_dir:
        crop = screenshot.convert("RGB").crop((canvas_left, canvas_top, canvas_right + 1, canvas_bottom + 1))
        crop.save(evidence_dir / "mspaint_canvas_after.png")
    if len(candidates) < 200:
        return {"name": spec["name"], "outcome": False,
                "evidence": {"error": "no drawn shape found on canvas", "canvas": [canvas_left, canvas_top, canvas_right, canvas_bottom]}}
    xs = [p[0] for p in candidates]
    ys = [p[1] for p in candidates]
    bbox = (min(xs), min(ys), max(xs), max(ys))
    bbox_area = (bbox[2] - bbox[0] + 1) * (bbox[3] - bbox[1] + 1)
    fill_fraction = len(candidates) / bbox_area
    target = spec["target"]
    tolerance = int(spec.get("tolerance_px", 12))
    deltas = {
        "left": bbox[0] - canvas_left - target[0],
        "top": bbox[1] - canvas_top - target[1],
        "right": bbox[2] - canvas_left - target[2],
        "bottom": bbox[3] - canvas_top - target[3],
    }
    within = all(abs(v) <= tolerance for v in deltas.values())
    filled_enough = fill_fraction >= float(spec.get("min_fill_fraction", 0.6))
    outcome = bool(within and filled_enough)
    evidence = {
        "canvas": [canvas_left, canvas_top, canvas_right, canvas_bottom],
        "shape_bbox_screen": list(bbox),
        "shape_bbox_canvas_relative": [
            bbox[0] - canvas_left, bbox[1] - canvas_top, bbox[2] - canvas_left, bbox[3] - canvas_top,
        ],
        "target": list(target), "deltas_px": deltas, "tolerance_px": tolerance,
        "fill_fraction": round(fill_fraction, 3), "min_fill_fraction": spec.get("min_fill_fraction", 0.6),
        "within_tolerance": within, "filled": filled_enough,
    }
    return {"name": spec["name"], "outcome": outcome, "evidence": evidence}


def _near_white(pixel: tuple[int, ...]) -> bool:
    r, g, b = pixel[:3]
    return r >= 250 and g >= 250 and b >= 250


def _visible_ink(pixel: tuple[int, ...]) -> bool:
    r, g, b = pixel[:3]
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    mx, mn = max(r, g, b), min(r, g, b)
    sat = (mx - mn) / mx if mx else 0.0
    return lum < 110 or (sat > 0.3 and mx > 60)


# --- live-window predicates (T8 realism: interference immunity) ------------------------------------


def _exe_basename(pid: int) -> str | None:
    """exe basename for a pid, or None when the process cannot be queried (fail-closed)."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        size = ctypes.wintypes.DWORD(260)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return os.path.basename(buffer.value)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _windows_of_process(process: str, title_token: str, klass: str | None) -> list[dict[str, Any]]:
    """Visible top-level windows of ONE process (exe basename, case-insensitive) whose
    title contains ``title_token`` (casefold). Empty token = any title."""
    from benchmarks.appwin import top_level_windows, window_class, window_pid, window_text

    process_cf = process.casefold()
    token_cf = title_token.casefold()
    matches: list[dict[str, Any]] = []
    for hwnd in top_level_windows():
        title = window_text(hwnd)
        if not title:
            continue
        if klass is not None and window_class(hwnd) != klass:
            continue
        if token_cf and token_cf not in title.casefold():
            continue
        if process_cf != (_exe_basename(window_pid(hwnd)) or "").casefold():
            continue
        matches.append({"hwnd": hwnd, "title": title, "class": window_class(hwnd)})
    return matches


def _pred_process_window_count(spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Exactly ``expected`` windows of ``process`` (exe basename) with ``title_token``
    in the title are visible. Proves "one task-relevant window, no stray doc/sheet"
    (a foreign window with a different document does not match the token)."""
    found = _windows_of_process(
        str(spec["process"]), str(spec.get("title_token", "")), spec.get("class")
    )
    expected = int(spec.get("expected", 1))
    return {
        "name": spec["name"],
        "outcome": len(found) == expected,
        "evidence": {
            "process": spec["process"], "title_token": spec.get("title_token", ""),
            "expected": expected, "actual": len(found),
            "matching_windows": [{"title": m["title"], "class": m["class"]} for m in found[:8]],
        },
    }


def _load_interrupt_snapshot(spec: dict[str, Any], ctx: dict[str, Any]) -> Path | None:
    raw = spec.get("snapshot")
    if not raw and ctx.get("run_record"):
        raw = ctx["run_record"].get(spec.get("snapshot_from_run_record", "interrupt_snapshot"))
    return Path(str(raw)) if raw else None


def _pred_window_state_unchanged(spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """The disturbance/decoy window recorded in a snapshot (written by the launcher's
    benchmarks/interrupt.py at injection time) is GONE (``allow_closed``) or left
    untouched: same class, same title token, rect within tolerance, edit-text hash
    unchanged (no keystroke landed in it)."""
    from benchmarks.appwin import (
        is_visible,
        read_edit_text,
        set_dpi_awareness,
        top_level_windows,
        window_class,
        window_text,
    )

    set_dpi_awareness()  # rect reads must be physical pixels, matching interrupt.py's snapshot
    tolerance = int(spec.get("tolerance_px", 4))
    allow_closed = bool(spec.get("allow_closed", True))
    snapshot_path = _load_interrupt_snapshot(spec, ctx)
    if snapshot_path is None or not Path(snapshot_path).is_file():
        return {"name": spec["name"], "outcome": False,
                "evidence": {"error": "interrupt snapshot missing", "snapshot": str(snapshot_path)}}
    snap = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    token_cf = str(snap.get("title_token", snap.get("title", ""))).casefold()
    want_class = snap.get("class")
    want_rect = snap.get("rect")
    want_text_sha = snap.get("text_sha256")
    live = None
    for hwnd in top_level_windows():
        title = window_text(hwnd)
        if not title or token_cf not in title.casefold():
            continue
        if want_class and window_class(hwnd) != want_class:
            continue
        live = hwnd
        break
    if live is None:
        evidence: dict[str, Any] = {"snapshot": str(snapshot_path), "window_state": "closed",
                                    "allow_closed": allow_closed}
        return {"name": spec["name"], "outcome": allow_closed, "evidence": evidence}
    title = window_text(live)
    rect = None
    if want_rect is not None:
        rect_struct = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(live), ctypes.byref(rect_struct))
        rect = [rect_struct.left, rect_struct.top, rect_struct.right - rect_struct.left,
                rect_struct.bottom - rect_struct.top]
    deltas = [abs(a - b) for a, b in zip(rect, want_rect)] if (rect and want_rect) else []
    text_sha = None
    if want_text_sha:
        text = read_edit_text(live) if is_visible(live) else ""
        text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    checks = {
        "visible": True,
        "title_token_present": token_cf in title.casefold(),
        "rect_within_tolerance": (not deltas) or max(deltas) <= tolerance,
        "text_hash_unchanged": (want_text_sha is None) or (text_sha == want_text_sha),
    }
    return {
        "name": spec["name"],
        "outcome": all(checks.values()),
        "evidence": {"snapshot": str(snapshot_path), "window_state": "open",
                     "title": title, "rect": rect, "rect_deltas_px": deltas,
                     "tolerance_px": tolerance, "text_sha256": text_sha, **checks},
    }


def _pred_focus_steal_recovered(spec: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """The interruption actually fired (launcher event log) AND the task's own window
    is the foreground window at scoring time. Artifact correctness is enforced by the
    remaining predicates; ``completed`` requires all of them."""
    from benchmarks.appwin import window_pid, window_text

    run_record = ctx.get("run_record") or {}
    log_path = spec.get("interrupt_log") or run_record.get(
        spec.get("interrupt_log_from_run_record", "interrupt_log")
    )
    fired, fired_evidence = False, {"interrupt_log": str(log_path) if log_path else None}
    if log_path and Path(str(log_path)).is_file():
        try:
            log = json.loads(Path(str(log_path)).read_text(encoding="utf-8"))
            fired = bool(log.get("fired"))
            fired_evidence.update({
                "fired": fired, "type": log.get("type"),
                "timestamp_utc": log.get("timestamp_utc"),
                "at_action_observed": log.get("at_action_observed"),
            })
        except (json.JSONDecodeError, OSError) as exc:
            fired_evidence["error"] = f"unreadable interrupt log: {exc}"
    require_event = bool(spec.get("require_event", True))

    process_cf = str(spec["process"]).casefold()
    token_cf = str(spec.get("title_token", "")).casefold()
    hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
    foreground: dict[str, Any] = {"hwnd": hwnd}
    task_focused = False
    if hwnd:
        title = window_text(hwnd)
        foreground.update({"title": title, "pid": window_pid(hwnd),
                           "exe": _exe_basename(window_pid(hwnd))})
    task_focused = (
        bool(title)
        and token_cf in title.casefold()
        and (foreground.get("exe") or "").casefold() == process_cf
    )
    event_ok = fired or not require_event
    return {
        "name": spec["name"],
        "outcome": bool(event_ok and task_focused),
        "evidence": {"interrupt": fired_evidence, "foreground": foreground,
                     "task_window_focused": task_focused,
                     "expected_process": spec["process"], "expected_title_token": token_cf},
    }


PREDICATE_EVALUATORS = {
    "file_exists": _pred_file_exists,
    "file_fresh": _pred_file_fresh,
    "xlsx_contains_strings": _pred_xlsx_contains_strings,
    "xlsx_min_string_hits": _pred_xlsx_min_string_hits,
    "xlsx_formula": _pred_xlsx_formula,
    "xlsx_cell_value": _pred_xlsx_cell_value,
    "xlsx_bold_cells": _pred_xlsx_bold_cells,
    "xlsx_rows_matching": _pred_xlsx_rows_matching,
    "xlsx_spot_checks": _pred_xlsx_spot_checks,
    "xlsx_computed_totals": _pred_xlsx_computed_totals,
    "text_contains_normalized": _pred_text_contains_normalized,
    "window_title_contains": _pred_window_title_contains,
    "folder_layout": _pred_folder_layout,
    "paint_rect_analysis": _pred_paint_rect_analysis,
    # T8 realism additions (interruption immunity; see DRIVER-PROTOCOL.md):
    "process_window_count": _pred_process_window_count,
    "window_state_unchanged": _pred_window_state_unchanged,
    "focus_steal_recovered": _pred_focus_steal_recovered,
}


# --- driver entry ---------------------------------------------------------------------------------


def _load_task(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("id", "predicates", "human_reference"):
        if key not in data:
            raise ValueError(f"{path.name}: missing required key {key!r}")
    return data


def seed_for_task(task_path: Path) -> dict[str, Any]:
    from benchmarks.tasks_hard.setup_fixtures import seed

    task = _load_task(task_path)
    match = re.match(r"h(\d+)-", str(task["id"]))
    if match:
        return seed(["t" + match.group(1)])
    match = re.match(r"r(\d+)-", str(task["id"]))
    if match:  # realism tasks (r1/r2): seed their own input fixtures
        return seed(["r" + match.group(1)])
    raise ValueError(f"cannot derive fixture prefix from task id {task['id']!r}")


def score_task(task_path: Path, run_record: dict[str, Any] | None) -> dict[str, Any]:
    task = _load_task(task_path)
    run_record = run_record or {}
    evidence_dir = run_record.get("evidence_dir")
    ctx: dict[str, Any] = {
        "start_utc": run_record.get("start_utc"),
        "start_dt": _parse_iso(run_record.get("start_utc")),
        "evidence_dir": str(evidence_dir) if evidence_dir else None,
        "run_record": run_record,
    }
    if evidence_dir:
        Path(evidence_dir).mkdir(parents=True, exist_ok=True)
    evaluated = []
    for spec in task["predicates"]:
        evaluator = PREDICATE_EVALUATORS.get(spec["kind"])
        if evaluator is None:
            evaluated.append({"name": spec["name"], "outcome": False,
                              "evidence": {"error": f"unknown predicate kind {spec['kind']!r}"}})
            continue
        evaluated.append(evaluator(spec, ctx))
    passed = sum(1 for item in evaluated if item["outcome"])
    total = len(evaluated)
    completed = total > 0 and passed == total
    start_dt = _parse_iso(run_record.get("start_utc"))
    end_dt = _parse_iso(run_record.get("end_utc"))
    wall_time = (end_dt - start_dt).total_seconds() if start_dt and end_dt else None
    reference = task.get("human_reference", {}).get("seconds")
    return {
        "task": task["id"],
        "wall_time_s": wall_time,
        "human_reference_s": reference,
        "ratio": (wall_time / reference) if wall_time and reference else None,
        "completed": completed,
        "precision": passed / total if total else 0.0,
        "actions": run_record.get("actions"),
        "model_calls": run_record.get("model_calls"),
        "retries": run_record.get("retries"),
        "notes": run_record.get("notes"),
        "predicates": evaluated,
    }


# --- automatic run log (benchmarks/runs-log.jsonl + benchmarks/RUNS.md) ----------------------------
#
# USER-COMMISSIONED doctrine: when testing Cortex, nothing about the test may go unrecorded.
# Every successful scoring invocation appends one record to the JSONL (append-only, never
# capped) and one row to the RUNS.md table (newest first, capped display). Both are written
# by this one code path so they can never disagree.


RUN_LOG_JSONL = REPO_ROOT / "benchmarks" / "runs-log.jsonl"
RUNS_MD = REPO_ROOT / "benchmarks" / "RUNS.md"
RUNS_MD_CAP = 200  # RUNS.md keeps the most recent rows; the JSONL keeps the full history

_RUNS_MD_COLUMNS = (
    "ts_utc (UTC)", "task", "run_id", "model", "wall_s", "ref_s", "ratio", "done",
    "precision", "actions", "retries", "guard", "anom", "notes",
)
_RUNS_MD_HEADER = (
    "# Benchmark run log (auto-generated)\n"
    "\n"
    "One row per scoring invocation of `benchmarks/score_task.py`, newest first — appended\n"
    "automatically after every scoring run. Do not edit by hand: the table is regenerated by\n"
    "the scorer and capped at the last 200 entries; the full append-only history lives in\n"
    "`benchmarks/runs-log.jsonl`. Deep per-run evidence (run records, scoring JSONs, bridge\n"
    "transcripts, screenshots): `evidence/perf-004/...`.\n"
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_run_log_record(
    payload: dict[str, Any],
    run_record: dict[str, Any] | None = None,
    model: str | None = None,
    ts_utc: str | None = None,
) -> dict[str, Any]:
    """Flat, JSON-serializable record of one scoring invocation (the JSONL line / MD row)."""
    run_record = run_record or {}
    guard = run_record.get("guard_events")
    if isinstance(guard, (list, tuple)):
        guard = len(guard)
    anomalies = run_record.get("anomalies")
    return {
        "ts_utc": ts_utc or _utc_now_iso(),
        "task": payload.get("task"),
        "run_id": run_record.get("run_id"),
        "model": model,
        "wall_time_s": payload.get("wall_time_s"),
        "human_reference_s": payload.get("human_reference_s"),
        "ratio": payload.get("ratio"),
        "completed": payload.get("completed"),
        "precision": payload.get("precision"),
        "actions": payload.get("actions"),
        "retries": payload.get("retries"),
        "guard_events": guard,
        "anomalies_count": len(anomalies) if isinstance(anomalies, list) else None,
        "notes": payload.get("notes"),
    }


def _md_cell(value: Any, limit: int = 0) -> str:
    if value is None:
        return ""
    text = str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _md_number(value: Any, digits: int) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return _md_cell(value)


def _md_row(record: dict[str, Any]) -> str:
    cells = [
        _md_cell(record.get("ts_utc")),
        _md_cell(record.get("task")),
        _md_cell(record.get("run_id")),
        _md_cell(record.get("model")),
        _md_number(record.get("wall_time_s"), 1),
        _md_number(record.get("human_reference_s"), 1),
        _md_number(record.get("ratio"), 2),
        "yes" if record.get("completed") else "no",
        _md_number(record.get("precision"), 3),
        _md_cell(record.get("actions")),
        _md_cell(record.get("retries")),
        _md_cell(record.get("guard_events")),
        _md_cell(record.get("anomalies_count")),
        _md_cell(record.get("notes"), 160),
    ]
    return "| " + " | ".join(cells) + " |"


def _existing_md_rows(text: str) -> list[str]:
    """Data rows of the generated table (everything after header + separator)."""
    rows = [line for line in text.splitlines() if line.startswith("|")]
    return rows[2:] if len(rows) > 2 else []


def append_run_log(
    payload: dict[str, Any],
    run_record: dict[str, Any] | None = None,
    model: str | None = None,
    ts_utc: str | None = None,
) -> dict[str, Any]:
    """Append one scoring invocation to runs-log.jsonl + RUNS.md. Returns the record."""
    record = build_run_log_record(payload, run_record, model, ts_utc)
    RUN_LOG_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG_JSONL.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    existing = _existing_md_rows(RUNS_MD.read_text(encoding="utf-8")) if RUNS_MD.is_file() else []
    rows = [_md_row(record)] + existing
    dropped = max(0, len(rows) - RUNS_MD_CAP)
    lines = _RUNS_MD_HEADER.splitlines()
    if dropped:
        lines.append("")
        lines.append(
            f"> Table capped: showing the {RUNS_MD_CAP} most recent of {len(rows) + dropped} "
            "logged runs — full history in `benchmarks/runs-log.jsonl`."
        )
        rows = rows[:RUNS_MD_CAP]
    lines.append("")
    lines.append("| " + " | ".join(_RUNS_MD_COLUMNS) + " |")
    lines.append("|" + "---|" * len(_RUNS_MD_COLUMNS))
    lines.extend(rows)
    RUNS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="harsh-task scorer (real machine state)")
    parser.add_argument("--task", type=Path, required=True, help="task YAML in benchmarks/tasks_hard/")
    parser.add_argument("--run-record", type=Path, help="run record JSON (see DRIVER-PROTOCOL.md)")
    parser.add_argument("--out", type=Path, help="scoring JSON output path (default: stdout)")
    parser.add_argument("--seed", action="store_true", help="re-seed the task's input fixtures and exit")
    parser.add_argument(
        "--model",
        metavar="LABEL",
        help="free-text driver/model label recorded in the run log (e.g. \"glm-flash\", \"gpt-4o\")",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="opt out of the automatic run-log append (benchmarks/RUNS.md + runs-log.jsonl)",
    )
    args = parser.parse_args(argv)

    if args.seed:
        print(json.dumps(seed_for_task(args.task), indent=2))
        return 0
    run_record = json.loads(args.run_record.read_text(encoding="utf-8")) if args.run_record else {}
    payload = score_task(args.task, run_record)
    rendered = json.dumps(payload, indent=2, default=str)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if not args.no_log:
        try:
            record = append_run_log(payload, run_record, args.model)
            print(
                f"[run log] {record['ts_utc']} {record['task']} appended to "
                f"benchmarks/RUNS.md + benchmarks/runs-log.jsonl"
            )
        except OSError as exc:
            print(f"WARNING: automatic run-log append failed: {exc}", file=sys.stderr)
    return 0 if payload["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
