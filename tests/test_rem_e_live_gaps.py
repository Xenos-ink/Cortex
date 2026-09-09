"""REM-E live-test gap tests (ORVEX-CORTEX-055, live-test recovery).

Pins the two Commander-proven defects from the LIVE machine run:

DEFECT 1 — ensure_app launch never spawns Store-app targets (mspaint.exe).

  On the live machine Paint is a Microsoft Store app whose real executable is
  reachable only through the execution-alias reparse point under
  ``%LOCALAPPDATA%\\Microsoft\\WindowsApps\\mspaint.exe``. REM-C's (correct)
  removal of ``shell=True`` left ``_launch_process`` resolving the needle via
  ``shutil.which`` (-> None for aliases) then falling back to a raw-name Popen
  (-> FileNotFoundError -> soft-None -> the NO_INSTANCE probe never launches).
  The fix resolves the needle as a Windows Store execution alias BETWEEN the
  ``which`` step and the raw-name fallback; the charset gate stays exactly as
  REM-C shipped it (validation runs before any path construction).

DEFECT 2 — computer_observe's ImageContent is not size-bounded.

  REM-A bounded only the EXECUTE path (``_bound_outbound_image`` via
  ``_execute_response_blocks``); ``computer_observe`` returned the raw PNG
  ImageContent (live probe: 1,827,424 base64 chars ≈ 1.37 MB — the exact
  provider-400 truncated/base64 symptom class this mission fixes). The fix
  wraps the observe image through the SAME helper and reports the truthful
  outbound mime in the ``image_format`` metadata; the INTERNAL Observation
  stays PNG (pixel-diff/checkpoints untouched). computer_screenshot
  delegates to observe, so it is covered by the same change.

Written RED first (pre-fix failures confirmed), then greened by the REM-E fixes.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
from typing import Any

import pytest
from PIL import Image

from computer_use_mcp import server
from computer_use_mcp.models import Observation

from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    _png,
    make_session,
)


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh bounded registry/bundles + per-test audit dir (same as controller suite)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", __import__(
        "computer_use_mcp.state", fromlist=["SessionRegistry"]
    ).SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    return server


# --- DEFECT 1: Store execution-alias resolution in _launch_process ----------------------------


def _alias_dir(tmp_path: Any) -> Any:
    """A fake %LOCALAPPDATA%\\Microsoft\\WindowsApps directory (alias parent)."""
    windows_apps = tmp_path / "Microsoft" / "WindowsApps"
    windows_apps.mkdir(parents=True, exist_ok=True)
    return tmp_path, windows_apps


def test_d1_alias_exists_which_none_popen_gets_alias_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Pin (a): which=None + the Store alias exists -> Popen receives the ALIAS
    PATH (plain one-element argv, no shell) — the mspaint.exe live-launch case."""
    from computer_use_mcp.backend import LocalComputerBackend

    localappdata, windows_apps = _alias_dir(tmp_path)
    alias_path = str(windows_apps / "mspaint.exe")
    alias_path.encode("ascii")  # sanity: pure-ASCII path, no charset surprises
    windows_apps.joinpath("mspaint.exe").write_bytes(b"")  # os.path.exists() -> True

    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    backend = LocalComputerBackend.__new__(LocalComputerBackend)
    with monkeypatch.context() as ctx:
        ctx.setattr(shutil, "which", lambda needle: None)
        recorded: dict[str, Any] = {}

        def fake_popen(argv, *args: Any, **kwargs: Any):
            recorded["argv"] = argv
            recorded["kwargs"] = kwargs

        ctx.setattr(subprocess, "Popen", fake_popen)
        result = backend._launch_process("mspaint.exe")
    assert result == "mspaint.exe"
    assert recorded["argv"] == [alias_path]  # the ALIAS PATH, not the bare name
    assert not recorded["kwargs"].get("shell")  # REM-C: still no shell


def test_d1_alias_missing_which_none_keeps_raw_name_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Pin (b): which=None + NO alias file -> the raw-name Popen fallback is
    unchanged (Windows CreateProcess PATH search, soft contract preserved)."""
    from computer_use_mcp.backend import LocalComputerBackend

    localappdata, windows_apps = _alias_dir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))

    backend = LocalComputerBackend.__new__(LocalComputerBackend)
    with monkeypatch.context() as ctx:
        ctx.setattr(shutil, "which", lambda needle: None)
        recorded: dict[str, Any] = {}

        def fake_popen(argv, *args: Any, **kwargs: Any):
            recorded["argv"] = argv
            recorded["kwargs"] = kwargs

        ctx.setattr(subprocess, "Popen", fake_popen)
        result = backend._launch_process("totally-unknown-app-xyz")
    assert result == "totally-unknown-app-xyz"
    assert recorded["argv"] == ["totally-unknown-app-xyz"]  # raw name, unchanged
    assert not recorded["kwargs"].get("shell")


def test_d1_which_resolves_beats_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Pin (c): when ``shutil.which`` resolves the needle, the which path wins —
    the alias dir is never consulted as a substitute for PATH resolution."""
    from computer_use_mcp.backend import LocalComputerBackend

    localappdata, windows_apps = _alias_dir(tmp_path)
    windows_apps.joinpath("notepad.exe").write_bytes(b"")  # alias ALSO exists
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))

    backend = LocalComputerBackend.__new__(LocalComputerBackend)
    resolved = r"C:\Windows\System32\notepad.exe"
    with monkeypatch.context() as ctx:
        ctx.setattr(shutil, "which", lambda needle: resolved)
        recorded: dict[str, Any] = {}

        def fake_popen(argv, *args: Any, **kwargs: Any):
            recorded["argv"] = argv
            recorded["kwargs"] = kwargs

        ctx.setattr(subprocess, "Popen", fake_popen)
        result = backend._launch_process("notepad.exe")
    assert result == "notepad.exe"
    assert recorded["argv"] == [resolved]  # which path wins over the alias


def test_d1_needle_validation_rejects_smuggle_before_alias_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Pin (d): the charset gate fires BEFORE any alias path is constructed — a
    smuggle needle (path separator / quote / metachar) raises the typed rejection
    and NOTHING is spawned, even with a hostile alias dir lying around."""
    from computer_use_mcp.backend import LaunchTargetError, LocalComputerBackend

    # A hostile LOCALAPPDATA whose WindowsApps "alias" is a smuggle name — the
    # gate must reject the NEEDLE before this dir is ever joined or read.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    backend = LocalComputerBackend.__new__(LocalComputerBackend)
    with monkeypatch.context() as ctx:
        ctx.setattr(shutil, "which", lambda needle: None)
        spawned: list[Any] = []

        def fake_popen(argv, *args: Any, **kwargs: Any):
            spawned.append(argv)

        ctx.setattr(subprocess, "Popen", fake_popen)
        for smuggle in (
            'x" & victim2.bat',
            "..\\..\\evil.exe",
            "C:\\Windows\\System32\\notepad.exe",
            "notepad|calc",
        ):
            with pytest.raises(LaunchTargetError):
                backend._launch_process(smuggle)
    assert spawned == []  # nothing was ever spawned for any smuggle form


# --- DEFECT 2: computer_observe bounds the outbound ImageContent -------------------------------


def _big_png(width: int = 400, height: int = 300) -> str:
    """A PNG sized to exceed the default outbound image budget (>= 180 KB decoded).

    REM-A's dot-grid helper crushes to tens of KB under PNG's filters; to
    honestly exceed 180 KB of DECODED bytes at small dimensions the image uses
    per-pixel random RGB texture (incompressible by PNG). Small dimensions keep
    the re-encode ladder fast (one quality step lands well under budget).
    """
    import random

    rng = random.Random(0x055)
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _observe_with(monkeypatch: pytest.MonkeyPatch, png_b64: str) -> Any:
    """Start a session whose next computer_observe returns ``png_b64``; return the
    raw observe content-block result."""
    backend = ScriptedBackend(flip=False)
    backend.observe_override_png = png_b64
    session_id, _bundle, _b, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    return server.computer_observe(session_id)


async def test_d2_oversized_observe_png_is_bounded_and_format_truthful(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin (e): a >180 KB PNG observation leaves as a bounded image block — the
    data decodes as JPEG (or at minimum fits the budget) and the mimeType matches
    the actual outbound bytes."""
    result = _observe_with(monkeypatch, _big_png())
    assert isinstance(result, list) and len(result) == 2, result
    text_block, image_block = result
    decoded = base64.b64decode(image_block.data, validate=True)
    budget = server._result_image_max_bytes()
    assert len(decoded) <= budget, (
        f"outbound observe image is {len(decoded)} bytes > budget {budget}"
    )
    if image_block.mimeType == "image/jpeg":
        assert decoded[:3] == b"\xff\xd8\xff"  # JPEG magic matches the claim
    else:  # in-budget passthrough must remain genuine PNG bytes
        assert image_block.mimeType == "image/png"
        assert decoded.startswith(b"\x89PNG\r\n\x1a\n")
    # no heavy blob rides the text channel
    assert "image_base64" not in text_block.text


async def test_d2_under_budget_observe_png_passes_through_untouched(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin (e, complement): an under-budget PNG observation passes through as PNG,
    byte-identical to the internal capture."""
    small_png = _png("white")
    result = _observe_with(monkeypatch, small_png)
    assert isinstance(result, list) and len(result) == 2, result
    _text_block, image_block = result
    assert image_block.mimeType == "image/png"
    assert image_block.data == small_png  # untouched passthrough


async def test_d2_observe_image_format_metadata_is_truthful(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin (f): the ``image_format`` metadata equals the ACTUAL outbound mime —
    not the hardcoded pre-REM-E 'image/png' (an oversized PNG must report
    image/jpeg once re-encoded)."""
    result = _observe_with(monkeypatch, _big_png())
    assert isinstance(result, list) and len(result) == 2, result
    text_block, image_block = result
    payload = json.loads(text_block.text)
    assert payload["image_format"] == image_block.mimeType, (
        f"metadata image_format={payload['image_format']!r} != "
        f"actual outbound mimeType={image_block.mimeType!r}"
    )
    assert image_block.mimeType == "image/jpeg"  # the big PNG really re-encoded


async def test_d2_internal_observation_stays_original_png(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin (g): the INTERNAL Observation.image_base64 keeps the ORIGINAL PNG —
    bounding is outbound-only; pixel-diff/checkpoints are unaffected."""
    big_png = _big_png()
    backend = ScriptedBackend(flip=False)
    backend.observe_override_png = big_png
    session_id, bundle, _b, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    result = server.computer_observe(session_id)
    assert isinstance(result, list) and len(result) == 2, result
    _text_block, image_block = result
    assert image_block.mimeType == "image/jpeg"  # outbound was re-encoded...
    # ...but the engine's own capture still holds the ORIGINAL PNG bytes.
    observation = bundle.agent.observation.capture()
    assert observation.image_base64 == big_png
    assert base64.b64decode(big_png).startswith(b"\x89PNG\r\n\x1a\n")
    # and the next internal capture is still PNG (pipeline unchanged).
    assert base64.b64decode(bundle.agent.observation.capture().image_base64)[:8] == (
        b"\x89PNG\r\n\x1a\n"
    )


def test_d2_observe_and_execute_docstrings_mention_both_paths(
    fresh_server: Any,
) -> None:
    """Docstring truthfulness: the REM-A knob comment bounds observe AND execute."""
    source = server.__doc__ or ""
    with open(server.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert "bounds the OUTBOUND image only" in source  # REM-A invariant kept
    assert "CORTEX_RESULT_IMAGE_MAX_KB" in source
    # REM-E: observe is explicitly named alongside execute where the budget applies
    assert "observe and execute" in source
