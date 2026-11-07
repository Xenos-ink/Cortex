"""Unit tests for the WP-R2a split tool (byte-exact statement moves)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = Path(__file__).resolve().parents[1] / "tools" / "restructure" / "split_module.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("split_module_tool", _SPEC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_tree(tmp_path: Path, source_lines: list[str]) -> tuple[Path, Path]:
    src_dir = tmp_path / "pkg"
    src_dir.mkdir()
    source = src_dir / "mod.py"
    source.write_text("".join(line + "\n" for line in source_lines), encoding="utf-8")
    return source, src_dir


def test_moves_lines_byte_exact_in_source_order(tmp_path):
    tool = _load_tool()
    source_lines = [
        "KEEP_DOC = 1",
        "A = 1  # comment stays",
        "",
        "def f():",
        "    return A",
        "B = 2",
        "C = 3",
    ]
    source, src_dir = _make_tree(tmp_path, source_lines)
    modules = {
        "part": {
            "path": str(src_dir / "part.py"),
            "header": ['"""Part."""', "", "import os", ""],
        }
    }
    moves = [{"target": "part", "start": 2, "end": 5}]
    counts = tool.apply_split(source, modules, moves)
    written = Path(modules["part"]["path"]).read_text(encoding="utf-8")
    assert written == '"""Part."""\n\nimport os\n\nA = 1  # comment stays\n\ndef f():\n    return A\n'
    assert source.read_text(encoding="utf-8") == "KEEP_DOC = 1\nB = 2\nC = 3\n"
    assert counts == {"part": 8, "<kept in source>": 3}


def test_overlapping_moves_are_refused_and_nothing_is_written(tmp_path):
    tool = _load_tool()
    source, src_dir = _make_tree(tmp_path, ["A = 1", "B = 2", "C = 3"])
    modules = {"part": {"path": str(src_dir / "part.py"), "header": []}}
    moves = [
        {"target": "part", "start": 1, "end": 2},
        {"target": "part", "start": 2, "end": 3},
    ]
    with pytest.raises(SystemExit, match="overlap"):
        tool.apply_split(source, modules, moves)
    assert not (src_dir / "part.py").exists()
    assert source.read_text(encoding="utf-8") == "A = 1\nB = 2\nC = 3\n"


def test_out_of_order_and_out_of_bounds_moves_are_refused(tmp_path):
    tool = _load_tool()
    source, src_dir = _make_tree(tmp_path, ["A = 1", "B = 2"])
    modules = {"part": {"path": str(src_dir / "part.py"), "header": []}}
    with pytest.raises(SystemExit, match="out of order"):
        tool.apply_split(source, modules, [{"target": "part", "start": 2, "end": 2}, {"target": "part", "start": 1, "end": 1}])
    with pytest.raises(SystemExit, match="out of bounds"):
        tool.apply_split(source, modules, [{"target": "part", "start": 1, "end": 3}])
    with pytest.raises(SystemExit, match="unknown module"):
        tool.apply_split(source, modules, [{"target": "other", "start": 1, "end": 1}])


def test_every_source_line_is_moved_or_kept_exactly_once(tmp_path):
    tool = _load_tool()
    source_lines = ["A = 1", "B = 2", "C = 3", "D = 4"]
    source, src_dir = _make_tree(tmp_path, source_lines)
    modules = {
        "one": {"path": str(src_dir / "one.py"), "header": []},
        "two": {"path": str(src_dir / "two.py"), "header": []},
    }
    moves = [
        {"target": "one", "start": 1, "end": 1},
        {"target": "two", "start": 3, "end": 4},
    ]
    counts = tool.apply_split(source, modules, moves)
    assert Path(modules["one"]["path"]).read_text(encoding="utf-8") == "A = 1\n"
    assert Path(modules["two"]["path"]).read_text(encoding="utf-8") == "C = 3\nD = 4\n"
    assert source.read_text(encoding="utf-8") == "B = 2\n"
    assert sum(counts.values()) == len(source_lines)


def test_crlf_sources_are_copied_byte_for_byte(tmp_path):
    tool = _load_tool()
    source_lines = ["A = 1\r\n", "# keep\r\n", "B = 2\r\n"]
    src_dir = tmp_path / "pkg"
    src_dir.mkdir()
    source = src_dir / "mod.py"
    source.write_bytes("".join(source_lines).encode("utf-8"))
    modules = {"part": {"path": str(src_dir / "part.py"), "header": ['"""H."""', ""]}}
    counts = tool.apply_split(source, modules, [{"target": "part", "start": 1, "end": 2}])
    part = Path(modules["part"]["path"]).read_bytes()
    assert part == b'"""H."""\n\nA = 1\r\n# keep\r\n'  # CR bytes preserved exactly
    assert source.read_bytes() == b"B = 2\r\n"
    assert counts == {"part": 4, "<kept in source>": 1}


def test_main_rejects_a_spec_without_moves(tmp_path):
    tool = _load_tool()
    source, src_dir = _make_tree(tmp_path, ["A = 1"])
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(
        json.dumps({"source": str(source), "modules": {}, "moves": []}), encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="no moves"):
        tool.main([str(_SPEC), str(spec_path)])
