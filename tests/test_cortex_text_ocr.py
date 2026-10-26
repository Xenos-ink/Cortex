"""cortex_text_ocr robustness — control-char-safe JSON parse (AVR-010 live bug).

Found in LIVE verification: on a real desktop (ZCode terminal window with Arabic
text + special characters), ocr.ps1's JSON output failed the Python-side strict
parse ("PowerShell output is not valid JSON (Invalid control character at: line
1 column 2385)"). The seam's fail-open held (substrate "uia" + honest
substrate_error) — that behavior is pinned elsewhere and must not change; these
tests pin the PACKAGE fix:

1. ocr.ps1 forces UTF-8 (no BOM) output and pre-escapes raw C0 control chars in
   string values (belt);
2. the Python parse tries ``json.loads(raw, strict=False)`` first and, on
   JSONDecodeError, escapes raw control chars (\\x00-\\x1f except \\t\\n\\r) and
   retries ONCE (braces) — stamping every served region dict with the honest
   ``"json_sanitized": True`` when (and ONLY when) the sanitize retry saved it.

PURE-PYTHON parse tests only: no PowerShell/WinRT execution, no OCR engine. The
transport boundary (``subprocess.run``) is faked in-memory; layout convention:
this file lives in the repo's tests/ next to every other suite. The package is
loaded DIRECTLY from its side-package source file — deliberately NOT installed
into this venv and NOT put on sys.path/sys.modules, because the seam's suite
(test_avr_text_substrates.py) pins that ``find_spec("cortex_text_ocr")`` must
miss here; this file must not leak it into those probes.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from PIL import Image

_SIDE_PKG_INIT = (
    Path(__file__).resolve().parents[1]
    / "sidepackages"
    / "cortex_text_ocr"
    / "src"
    / "cortex_text_ocr"
    / "__init__.py"
)


def _load_side_package():
    spec = importlib.util.spec_from_file_location("cortex_text_ocr", _SIDE_PKG_INIT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # NOT registered in sys.modules (see docstring)
    return module


cto = _load_side_package()


def _ocr_payload(inner_text: str) -> str:
    """One ocr.ps1 OCR line as RAW PowerShell stdout text (a str).

    Direct concatenation (NOT json.dumps) on purpose: the inner text must stay
    RAW in the payload — raw control char(s), raw backslash — exactly the
    bytes-as-decoded stdout the live bug produced. The fixtures contain no
    double quotes, so this stays well-formed apart from the intended control
    characters.
    """
    return (
        '{"ok":true,"mode":"ocr","lines":[{"text":"' + inner_text + '",'
        '"x":10.0,"y":20.0,"width":120.0,"height":16.0,"words":2}]}'
    )


def test_parse_accepts_raw_control_char_via_strict_false() -> None:
    """A raw ESC inside a string value (the live bug shape) now parses."""
    raw = _ocr_payload("a\x1bb")
    data = cto._parse_ps_output(raw)
    assert data["ok"] is True
    assert data["lines"][0]["text"] == "a\x1bb"
    # The FIRST (strict=False) attempt saved it: no sanitize happened.
    assert cto._last_parse_sanitized is False


def test_parse_sanitize_retry_saves_and_flags() -> None:
    """Belt-and-braces: a payload strict=False still rejects parses on retry #1.

    backslash + raw ESC inside a string ("Invalid \\escape" even with
    strict=False); escaping the ESC to \\u001b text makes it parse, the flag is
    honest True, and the text keeps a readable \\u001b representation.
    """
    raw = _ocr_payload("x\\\x1by")
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw, strict=False)  # attempt #1 genuinely fails
    data = cto._parse_ps_output(raw)
    assert data["ok"] is True
    assert data["lines"][0]["text"] == "x\\u001by"
    assert cto._last_parse_sanitized is True


def test_parse_fail_closed_message_preserved() -> None:
    """Still-unparsable output fails closed with the seam's documented message."""
    with pytest.raises(RuntimeError, match=r"not valid JSON") as exc_info:
        cto._parse_ps_output('{"ok":true, oops')
    assert str(exc_info.value).startswith(
        "cortex_text_ocr: PowerShell output is not valid JSON ("
    )
    assert cto._last_parse_sanitized is False


def test_sanitize_leaves_json_whitespace_alone() -> None:
    """\\t \\n \\r are exempt; every other C0 char becomes literal \\uXXXX text."""
    assert cto._sanitize_control_chars("a\tb\nc\rd") == "a\tb\nc\rd"
    assert cto._sanitize_control_chars("a\x00\x08\x0b\x0c\x0e\x1fz") == (
        "a\\u0000\\u0008\\u000b\\u000c\\u000e\\u001fz"
    )


def _fake_transport(raw: str):
    """A subprocess.run stand-in returning ocr.ps1 stdout WITHOUT running PS."""

    class _Completed:
        stdout = raw.encode("utf-8")
        stderr = b""

    return lambda cmd, **kwargs: _Completed()


def _frame() -> Image.Image:
    return Image.new("RGB", (16, 16), "white")


def test_regions_stamp_json_sanitized_only_when_sanitize_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The honest boolean rides each region dict ONLY on the sanitize path."""
    monkeypatch.setattr(cto, "available", lambda: True)

    # Clean strict=False parse (raw ESC accepted by attempt #1): NO stamp.
    monkeypatch.setattr(cto.subprocess, "run", _fake_transport(_ocr_payload("a\x1bb")))
    regions = cto.regions(None, None, 0.05, frame=_frame())
    assert [r["text"] for r in regions] == ["a\x1bb"]
    assert all("json_sanitized" not in r for r in regions)

    # Sanitize-retry parse: every served region carries json_sanitized: True.
    monkeypatch.setattr(cto.subprocess, "run", _fake_transport(_ocr_payload("x\\\x1by")))
    regions = cto.regions(None, None, 0.05, frame=_frame())
    assert [r["text"] for r in regions] == ["x\\u001by"]
    assert all(r.get("json_sanitized") is True for r in regions)
