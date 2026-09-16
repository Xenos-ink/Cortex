"""v0.7.1 Defect-B tests (ORVEX v07-008, field Blender case): CORTEX_LAUNCH_PATHS
mapping + typed NO_INSTANCE launch-status suffixes.

Field defect: ``ensure_app`` under ``launch=server`` for a GUI executable installed
outside PATH (Blender -> ``C:\\Program Files\\...``) resolved via ``shutil.which`` ->
``None``, missed the Store alias, failed the raw-name spawn, and degraded to a BARE
``NO_INSTANCE target=... launch=server`` payload — nothing spawned, zero explanation
(the tester verified with tasklist and bypassed Cortex).

The v0.7.1 fix (implementing the approved §7-B design):

- new env config ``CORTEX_LAUNCH_PATHS``: ``needle=path;needle=path`` (needle
  charset-validated exactly like ``validate_launch_needle``; path must EXIST as a
  FILE; malformed entries are skipped fail-safe — never fatal, never spawn);
  resolution order becomes **mapped path -> shutil.which -> Store alias -> raw-name
  fallback** (mapping FIRST, the later legs exactly as v0.7.0 shipped them);
- typed NO_INSTANCE payload suffixes for an AUTHORIZED launch that produced no
  process (never silent, never a false ``launched=`` claim):
  - ``launch_unresolved=path-lookup-missed (...)`` — valid needle, every resolution
    step missed (the raw-name spawn's FileNotFoundError);
  - ``launch_unresolved=spawn-failed (<ExcType>: <msg[:120]>)`` — a resolved target
    whose Popen raised (the OS error surfaced, bounded);
  - ``launched=<exe>`` / ``launch_rejected=LaunchTargetError`` unchanged;
  - ``launch=driver`` payloads byte-identical to v0.7.0 (never spawn, never suffix).
- the FakeComputerBackend mirror honors the same mapping env and emits the same
  typed suffixes (real-vs-fake payload parity is pinned below).

No GUI app is ever spawned here: every spawn is a stubbed Popen on a bare
``LocalComputerBackend`` (no ``__init__``) or the fake ledger, exactly like the
pre-existing launch suites (test_rem_e_live_gaps.py / test_r2_redteam_speed.py).
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from computer_use_mcp.backend import (
    FakeComputerBackend,
    LaunchTargetError,
    LocalComputerBackend,
    parse_launch_paths,
)
from computer_use_mcp.interference import NO_INSTANCE, format_no_instance

LAUNCH_PATHS_ENV = "CORTEX_LAUNCH_PATHS"

#: The EXACT v0.7.0 ``launch=driver`` NO_INSTANCE payload (byte-identical contract).
DRIVER_PAYLOAD = (
    "NO_INSTANCE target='blender' launch=driver "
    "(no existing window matched; with launch=driver the driver may launch through "
    "its normal flow)"
)

PATH_MISS_TOKEN = "launch_unresolved=path-lookup-missed"


def _bare_backend() -> LocalComputerBackend:
    """A LocalComputerBackend with no __init__ side effects (launch region only)."""
    return LocalComputerBackend.__new__(LocalComputerBackend)


def _no_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The NO_INSTANCE branch: discovery finds no instance of any needle."""
    monkeypatch.setattr(LocalComputerBackend, "enumerate_app_windows", lambda self, needle: [])


def _mapping(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, name: str = "blender.exe") -> str:
    """A real on-disk 'executable' (any existing file resolves a mapping on win32)."""
    exe = tmp_path / "tools" / name
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"")  # content irrelevant: mapping resolution only isfile()s it
    monkeypatch.setenv(LAUNCH_PATHS_ENV, f"blender={exe}")
    return str(exe)


def _alias_miss(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """An EMPTY Store-alias directory: the alias probe must miss."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    (tmp_path / "localappdata").mkdir(exist_ok=True)


def _popen_recorder(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    recorded: list[list[str]] = []

    def fake_popen(argv: list[str], *args: Any, **kwargs: Any) -> None:
        assert not kwargs.get("shell")  # REM-C invariant rides every launch leg
        recorded.append(argv)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return recorded


# --- CORTEX_LAUNCH_PATHS parser (fail-safe by construction) ----------------------------------


def test_parse_launch_paths_empty_and_garbage_inputs() -> None:
    assert parse_launch_paths(None) == {}
    assert parse_launch_paths("") == {}
    assert parse_launch_paths("   ") == {}
    assert parse_launch_paths(";;") == {}
    assert parse_launch_paths("no-equals-sign") == {}  # no '=' -> skipped
    assert parse_launch_paths("=C:\\only-a-path") == {}  # empty needle -> skipped
    assert parse_launch_paths("needle=") == {}  # empty path -> skipped


def test_parse_launch_paths_valid_entry_requires_existing_file(tmp_path: Any) -> None:
    real = tmp_path / "app.exe"
    real.write_bytes(b"")
    mapping = parse_launch_paths(f"my app={real}")
    assert mapping == {"my app": str(real)}
    # a path that does not exist as a FILE (missing or a directory) is skipped
    assert parse_launch_paths(f"ghost={tmp_path / 'missing.exe'}") == {}
    directory = tmp_path / "adir"
    directory.mkdir()
    assert parse_launch_paths(f"dir={directory}") == {}


def test_parse_launch_paths_needle_side_is_charset_validated(tmp_path: Any) -> None:
    real = tmp_path / "app.exe"
    real.write_bytes(b"")
    for needle in ("inv@lid", "C:\\evil.exe", "a|b", 'q"uote', "x y z=pad"):
        assert parse_launch_paths(f"{needle}={real}") == {}, needle
    assert parse_launch_paths(f"Ok.Needle_02-x={real}") == {"ok.needle_02-x": str(real)}
    # ';' is the ENTRY separator: "x;y=path" is entry "x" (skipped, no '=') plus a
    # VALID entry for needle "y" — a semicolon can never live inside a needle.
    assert parse_launch_paths(f"x;y={real}") == {"y": str(real)}


def test_parse_launch_paths_strips_whitespace_and_one_quote_pair(tmp_path: Any) -> None:
    real = tmp_path / "app.exe"
    real.write_bytes(b"")
    assert parse_launch_paths(f'  needle =  "{real}"  ') == {"needle": str(real)}
    assert parse_launch_paths(f"  needle =  '{real}'  ") == {"needle": str(real)}
    # a path containing '=' after the FIRST separator survives intact
    weird = tmp_path / "we=ird.exe"
    weird.write_bytes(b"")
    assert parse_launch_paths(f"needle={weird}") == {"needle": str(weird)}


def test_parse_launch_paths_later_duplicate_wins(tmp_path: Any) -> None:
    first = tmp_path / "first.exe"
    second = tmp_path / "second.exe"
    for path in (first, second):
        path.write_bytes(b"")
    assert parse_launch_paths(f"n={first};n={second}") == {"n": str(second)}


# --- real backend: mapping-first resolution --------------------------------------------------


def test_mapped_path_launches_when_needle_not_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The field Blender case, fixed: the needle is nowhere on PATH, but the
    configured mapping resolves it -> Popen receives the MAPPED path and the
    launch reports the real spawned basename."""
    monkeypatch.delenv(LAUNCH_PATHS_ENV, raising=False)
    exe = _mapping(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "computer_use_mcp.backend.shutil.which", lambda needle: None
    )  # needle NOT on PATH (the field condition)
    recorded = _popen_recorder(monkeypatch)
    result = _bare_backend()._launch_process("blender")
    assert result == "blender.exe"  # the mapped file's REAL basename
    assert recorded == [[exe]]  # the spawn used the mapped path


def test_mapped_needle_matches_case_insensitively_as_a_whole(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _mapping(monkeypatch, tmp_path)
    monkeypatch.setattr("computer_use_mcp.backend.shutil.which", lambda needle: None)
    recorded = _popen_recorder(monkeypatch)
    assert _bare_backend()._launch_process("BLENDER") == "blender.exe"
    assert len(recorded) == 1  # mapping hit — never reached the raw fallback


def test_mapping_goes_before_which(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Approved order: mapped path FIRST — a which-resolvable needle still uses the
    mapping when both exist."""
    exe = _mapping(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "computer_use_mcp.backend.shutil.which", lambda needle: r"C:\elsewhere\blender.exe"
    )
    recorded = _popen_recorder(monkeypatch)
    assert _bare_backend()._launch_process("blender") == "blender.exe"
    assert recorded == [[exe]]  # the mapping won


def test_mapping_for_another_needle_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A mapping for needle X never affects needle Y: the raw-name fallback is
    attempted unchanged (v0.7.0 order preserved behind the mapping)."""
    _mapping(monkeypatch, tmp_path)  # maps "blender" only
    monkeypatch.setattr("computer_use_mcp.backend.shutil.which", lambda needle: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir(exist_ok=True)
    recorded = _popen_recorder(monkeypatch)
    assert _bare_backend()._launch_process("otherapp") == "otherapp"
    assert recorded == [["otherapp"]]  # raw-name fallback, byte-identical to v0.7.0


# --- typed NO_INSTANCE payload suffixes (real backend ensure_app) ----------------------------


def test_path_lookup_missed_suffix_replaces_silent_degradation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """THE field defect, pinned: a charset-valid needle that misses mapping, PATH,
    the Store alias AND the raw-name spawn now carries the typed reason instead of
    a bare silent NO_INSTANCE."""
    monkeypatch.delenv(LAUNCH_PATHS_ENV, raising=False)
    _alias_miss(monkeypatch, tmp_path)
    monkeypatch.setattr("computer_use_mcp.backend.shutil.which", lambda needle: None)

    def _missing(argv: list[str], *args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError(f"[WinError 2] The system cannot find the file specified: {argv[0]}")

    monkeypatch.setattr(subprocess, "Popen", _missing)
    _no_windows(monkeypatch)
    payload = _bare_backend().ensure_app("blender", allow_launch=True)
    assert payload.startswith(NO_INSTANCE)
    assert "launch=server" in payload
    assert PATH_MISS_TOKEN in payload  # the honest typed reason
    assert "launched=" not in payload  # never a false launch claim
    # the static hint names the fix surface (no needle text — nothing injectable)
    assert "CORTEX_LAUNCH_PATHS" in payload


def test_spawn_failed_suffix_surfaces_the_os_error_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A RESOLVED target whose Popen raises reports ``spawn-failed`` with the
    exception type and a bounded message."""
    _mapping(monkeypatch, tmp_path)
    monkeypatch.setattr("computer_use_mcp.backend.shutil.which", lambda needle: None)
    long_message = "access denied: " + "x" * 500

    def _denied(argv: list[str], *args: Any, **kwargs: Any) -> None:
        raise PermissionError(long_message)

    monkeypatch.setattr(subprocess, "Popen", _denied)
    _no_windows(monkeypatch)
    payload = _bare_backend().ensure_app("blender", allow_launch=True)
    assert payload.startswith(NO_INSTANCE)
    assert "launch_unresolved=spawn-failed (PermissionError: " in payload
    suffix = payload.split("launch_unresolved=spawn-failed ", 1)[1]
    assert len(suffix) <= 160  # exception name + <=120-char message + delimiters
    assert "launched=" not in payload


def test_malformed_config_entries_are_skipped_fail_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Malformed CORTEX_LAUNCH_PATHS entries never crash and never spawn from the
    malformed entry; a VALID entry in the same string still resolves."""
    real = tmp_path / "good.exe"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"")
    monkeypatch.setenv(
        LAUNCH_PATHS_ENV,
        f"noequals; ={real};inv@lid={real};ghost={tmp_path / 'missing.exe'};good={real};;",
    )
    monkeypatch.setattr("computer_use_mcp.backend.shutil.which", lambda needle: None)
    recorded = _popen_recorder(monkeypatch)
    backend = _bare_backend()
    assert backend._launch_process("good") == "good.exe"  # the valid entry works
    assert recorded == [[str(real)]]
    with pytest.raises(LaunchTargetError):
        backend._launch_process("inv@lid")  # the invalid NEEDLE never reaches a spawn
    assert recorded == [[str(real)]]  # nothing extra spawned


def test_malformed_only_config_never_spawns_and_degrades_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """An entirely-garbage mapping string: no crash, NO spawn at all for a needle
    that never resolves — the typed path-lookup suffix rides the payload."""
    monkeypatch.setenv(LAUNCH_PATHS_ENV, "garbage;=;more=;@#$=nope")

    def _missing(argv: list[str], *args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "Popen", _missing)
    monkeypatch.setattr("computer_use_mcp.backend.shutil.which", lambda needle: None)
    _alias_miss(monkeypatch, tmp_path)
    _no_windows(monkeypatch)
    payload = _bare_backend().ensure_app("blender", allow_launch=True)
    assert PATH_MISS_TOKEN in payload


def test_directory_mapped_path_is_skipped_not_spawned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A mapping pointing at a DIRECTORY is skipped (the path must be a FILE) —
    the resolution falls through to the (missing) later legs, nothing spawns."""
    directory = tmp_path / "blender-dir"
    directory.mkdir()
    monkeypatch.setenv(LAUNCH_PATHS_ENV, f"blender={directory}")
    monkeypatch.setattr("computer_use_mcp.backend.shutil.which", lambda needle: None)

    def _missing(argv: list[str], *args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "Popen", _missing)
    _alias_miss(monkeypatch, tmp_path)
    payload = _bare_backend().ensure_app("blender", allow_launch=True)
    assert PATH_MISS_TOKEN in payload  # fell through; the directory was never spawned


def test_invalid_needle_still_typed_rejected_even_with_mapping_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """REM-C invariant: an invalid needle is a typed rejection BEFORE any spawn —
    a configured mapping must never bypass the charset gate."""
    _mapping(monkeypatch, tmp_path)
    recorded = _popen_recorder(monkeypatch)
    _no_windows(monkeypatch)
    payload = _bare_backend().ensure_app('x" & victim.bat', allow_launch=True)
    assert payload.startswith(NO_INSTANCE)
    assert "launch_rejected=LaunchTargetError" in payload
    assert recorded == []  # NOTHING spawned


def test_spawn_failure_via_alias_still_returns_none_from_launch_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Contract pin (v0.7.0 tests unchanged): ``_launch_process`` still returns
    ``None`` on any failure — the typed suffix travels out-of-band, so every
    pre-existing pin (``is None``) keeps holding."""
    _alias_miss(monkeypatch, tmp_path)
    monkeypatch.setattr("computer_use_mcp.backend.shutil.which", lambda needle: None)

    def _denied(argv: list[str], *args: Any, **kwargs: Any) -> None:
        raise PermissionError("access denied")

    monkeypatch.setattr(subprocess, "Popen", _denied)
    assert _bare_backend()._launch_process("blender") is None


# --- FakeComputerBackend mirror parity -------------------------------------------------------


def test_fake_mapping_launches_and_records_basename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Fake parity: the same mapping env launches through the fake and records the
    mapped file's basename — exactly what the real backend would report."""
    exe = _mapping(monkeypatch, tmp_path)
    backend = FakeComputerBackend()
    payload = backend.ensure_app("blender", allow_launch=True)
    assert payload.startswith(NO_INSTANCE)
    assert "launch=server" in payload
    assert "launched=blender.exe" in payload
    assert backend.launched_processes == ["blender.exe"]
    assert exe.endswith("blender.exe")


def test_fake_unresolvable_carries_the_same_typed_suffix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Fake parity for the field case: an unresolvable needle yields the SAME
    typed suffix the real backend emits (never the silent bare probe)."""
    monkeypatch.delenv(LAUNCH_PATHS_ENV, raising=False)
    backend = FakeComputerBackend()
    backend.launch_unresolvable.add("blender")
    payload = backend.ensure_app("blender", allow_launch=True)
    assert payload.startswith(NO_INSTANCE)
    assert PATH_MISS_TOKEN in payload
    assert "launched=" not in payload
    assert backend.launched_processes == []


def test_fake_mapping_beats_the_unresolvable_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Parity: mapping resolution goes FIRST on both backends — a needle listed as
    unresolvable still launches through a configured mapping."""
    _mapping(monkeypatch, tmp_path)
    backend = FakeComputerBackend()
    backend.launch_unresolvable.add("blender")
    payload = backend.ensure_app("blender", allow_launch=True)
    assert "launched=blender.exe" in payload
    assert backend.launched_processes == ["blender.exe"]


def test_fake_invalid_needle_still_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LAUNCH_PATHS_ENV, 'x" & victim.bat=C:\\ nowhere')
    backend = FakeComputerBackend()
    payload = backend.ensure_app('x" & victim.bat', allow_launch=True)
    assert "launch_rejected=LaunchTargetError" in payload
    assert backend.launched_processes == []


# --- launch=driver payloads are byte-identical (hard invariant) ------------------------------


def test_driver_payload_exact_string_never_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """``launch=driver`` payloads are EXACTLY the v0.7.0 string — no suffix, no
    spawn — even with a mapping configured for the needle."""
    _mapping(monkeypatch, tmp_path)
    monkeypatch.setattr("computer_use_mcp.backend.shutil.which", lambda needle: None)
    _no_windows(monkeypatch)

    def _must_not_spawn(argv: list[str], *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("launch=driver must never spawn")

    monkeypatch.setattr(subprocess, "Popen", _must_not_spawn)
    real_payload = _bare_backend().ensure_app("blender", allow_launch=False)
    assert real_payload == DRIVER_PAYLOAD
    fake_payload = FakeComputerBackend().ensure_app("blender", allow_launch=False)
    assert fake_payload == DRIVER_PAYLOAD
    assert fake_payload == format_no_instance("blender", "driver")


def test_baseline_server_payload_shapes_are_additive(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Every authorized-launch payload still STARTS with the exact v0.7.0
    NO_INSTANCE probe — the suffixes are strictly additive."""
    monkeypatch.delenv(LAUNCH_PATHS_ENV, raising=False)
    _alias_miss(monkeypatch, tmp_path)
    monkeypatch.setattr("computer_use_mcp.backend.shutil.which", lambda needle: None)

    def _missing(argv: list[str], *args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "Popen", _missing)
    _no_windows(monkeypatch)
    payload = _bare_backend().ensure_app("blender", allow_launch=True)
    baseline = format_no_instance("blender", "server")
    assert payload.startswith(baseline), payload
