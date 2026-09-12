"""Independent red-team tests for ``computer_use_mcp.cli``.

Independent adversarial suite (author of cli.py did NOT write this file). Attacks the
stated contracts: no-clobber, backup-before-first-write, dry-run purity, ff-only update,
TOML append-only, probe exactness, registration shapes, stdlib-only imports.

Non-e2e: tmp_path fake homes/trees only; venv/pip/probe/subprocess are stubbed. No real
agent configs, no real venv, no pip, no network, no real git.

Fix status (R3-1 and R3-2 are FIXED in cli.py; the guards below are always-on regressions):
- R3-1 fixed: merge_json (src/computer_use_mcp/cli.py) no longer destroys a non-dict value
  found on the pointer path (e.g. ``mcp.servers`` holding a list): without ``--force`` it
  refuses (skipped-exists; the plan layer reports the loud ERROR reason "parent node has
  non-dict type; use --force to replace"), with ``--force`` it reports
  replaced-type-confused.
- R3-2 fixed: write_with_backup (src/computer_use_mcp/cli.py) never overwrites an existing
  backup: a taken second-resolution stamp is re-taken at microsecond precision, then
  numbered _2/_3... suffixes are used until a free name is found, so a second write within
  the same second keeps the first backup (original bytes) intact. The distinct-seconds
  control proves the rest of the backup chain works.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from computer_use_mcp import cli
from computer_use_mcp.cli import AgentSpec

VENV_REL = Path(".venv") / "Scripts" / "python.exe"
ARGS = ["-m", "computer_use_mcp.server"]


def _agents_for(home: Path) -> tuple[AgentSpec, ...]:
    """Full 5-agent table bound to a fake home (lambdas ignore the passed home)."""
    return (
        AgentSpec(
            name="zcode",
            paths=lambda _h: [home / ".zcode" / "cli" / "config.json"],
            fmt="zcode-json",
            pointer=("mcp", "servers", "cortex"),
        ),
        AgentSpec(
            name="claude",
            paths=lambda _h: [home / ".claude.json"],
            fmt="claude-json",
            pointer=("mcpServers", "cortex"),
        ),
        AgentSpec(
            name="cursor",
            paths=lambda _h: [home / ".cursor" / "mcp.json"],
            fmt="cursor-json",
            pointer=("mcpServers", "cortex"),
        ),
        AgentSpec(
            name="codex",
            paths=lambda _h: [home / ".codex" / "config.toml"],
            fmt="codex-toml",
            pointer=("mcp_servers", "cortex"),
        ),
        AgentSpec(
            name="kimi",
            paths=lambda _h: [home / ".kimi-code" / "config.json", home / ".kimi-code" / "mcp.json"],
            fmt="claude-json",
            pointer=("mcpServers", "cortex"),
        ),
    )


def _single_agent(home: Path, name: str) -> tuple[AgentSpec, ...]:
    return tuple(s for s in _agents_for(home) if s.name == name)


def _snapshot(root: Path) -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for p in sorted(root.rglob("*")):
        out[p.relative_to(root).as_posix()] = p.read_bytes() if p.is_file() else None
    return out


def _write(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _git_recorder(script: list[tuple[tuple[str, ...], dict]]):
    """Fake subprocess.run matching argv prefixes against ``script`` in order."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        argv = list(argv)
        calls.append(argv)
        for prefix, resp in script:
            if argv[: len(prefix)] == list(prefix):
                return _Completed(**resp)
        return _Completed()

    return fake_run, calls


def _stub_provision(monkeypatch, calls: list | None = None):
    calls = [] if calls is None else calls
    monkeypatch.setattr(cli, "_ensure_venv", lambda *a, **k: calls.append("venv"))
    monkeypatch.setattr(cli, "_pip_install", lambda *a, **k: calls.append("pip"))
    monkeypatch.setattr(cli, "probe_server", lambda *a, **k: calls.append("probe") or (True, "OK: stub"))
    return calls


class _Stamp:
    def __init__(self, text: str):
        self.text = text

    def astimezone(self):
        return self

    def strftime(self, fmt):
        return self.text


def _fake_clock(monkeypatch, stamps: list[str]):
    """Force cli.write_with_backup onto a controlled clock (deterministic collision test)."""
    seq = iter(stamps)

    class _FakeDT:
        @staticmethod
        def now(tz=None):
            try:
                return _Stamp(next(seq))
            except StopIteration:
                return _Stamp(stamps[-1])

    monkeypatch.setattr(cli, "datetime", _FakeDT)


# ===========================================================================
# Attack 1 — clobber resistance
# ===========================================================================


def test_r3_a1_force_replacement_preserves_siblings_and_top_level(tmp_path, monkeypatch):
    """--force may replace the cortex entry itself, but NOTHING else in the file."""
    home = tmp_path / "home"
    cfg = _write(
        home / ".claude.json",
        {
            "user": {"id": 7, "prefs": {"theme": "dark"}},
            "mcpServers": {
                "other-server": {"command": "npx", "args": ["-y", "srv"], "env": {"K": "V"}},
                "cortex": {  # differing existing entry with extra keys
                    "type": "stdio",
                    "command": "C:\\old\\python.exe",
                    "args": ["-m", "old"],
                    "env": {"SECRET": "keep-or-drop-but-only-here"},
                },
            },
        },
    )
    repo = tmp_path / "repo"
    monkeypatch.setattr(cli, "KNOWN_AGENTS", _single_agent(home, "claude"))
    _stub_provision(monkeypatch)

    rc = cli.main(["install", "--repo", str(repo), "--agents", "claude", "--force"])

    assert rc == 0
    data = json.loads(cfg.read_text(encoding="utf-8"))
    # siblings + unrelated top-level keys survive byte-for-byte (semantic parity)
    assert data["user"] == {"id": 7, "prefs": {"theme": "dark"}}
    assert data["mcpServers"]["other-server"] == {
        "command": "npx",
        "args": ["-y", "srv"],
        "env": {"K": "V"},
    }
    # cortex entry was replaced with the target registration (no cwd: claude)
    assert data["mcpServers"]["cortex"] == {
        "type": "stdio",
        "command": str(repo.resolve() / VENV_REL),
        "args": ARGS,
    }
    assert set(data.keys()) == {"user", "mcpServers"}


def test_r3_a1_no_force_leaves_differing_entry_byte_identical(tmp_path, monkeypatch):
    home = tmp_path / "home"
    existing = {
        "type": "stdio",
        "command": "C:\\old\\python.exe",
        "args": ["-m", "old"],
        "env": {"SECRET": "x"},
    }
    cfg = _write(home / ".claude.json", {"mcpServers": {"cortex": existing}})
    raw_before = cfg.read_bytes()
    repo = tmp_path / "repo"
    monkeypatch.setattr(cli, "KNOWN_AGENTS", _single_agent(home, "claude"))
    _stub_provision(monkeypatch)

    rc = cli.main(["install", "--repo", str(repo), "--agents", "claude"])

    assert rc == 0  # skip-exists is a report, not an error
    assert cfg.read_bytes() == raw_before  # file untouched on disk
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcpServers"]["cortex"] == existing  # extra env keys intact
    assert not list(tmp_path.rglob("*cortex-backup*"))


def test_r3_a1_corrupted_json_fails_loud_no_write_no_backup(tmp_path, monkeypatch, capsys):
    """Truncated JSON on disk must be reported, never silently overwritten or backed up."""
    home = tmp_path / "home"
    cfg = home / ".zcode" / "cli" / "config.json"
    cfg.parent.mkdir(parents=True)
    broken = b'{"mcp": {"servers": {"cor'  # truncated mid-write simulation
    cfg.write_bytes(broken)
    monkeypatch.setattr(cli, "KNOWN_AGENTS", _single_agent(home, "zcode"))

    results = cli.register_agent(
        _single_agent(home, "zcode")[0],
        home,
        {"type": "stdio", "command": "p", "args": ARGS, "cwd": "r"},
        force=False,
        dry_run=False,
    )

    assert results == [(str(cfg), "ERROR")]
    out = capsys.readouterr().out
    assert "ERROR" in out and "does not parse as JSON" in out
    assert cfg.read_bytes() == broken  # loud refusal, zero bytes changed
    assert not list(home.rglob("*cortex-backup*"))


def test_r3_a1_corrupted_json_install_exit_nonzero(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    cfg = home / ".cursor" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_bytes(b'{"mcpServers": ')
    repo = tmp_path / "repo"
    monkeypatch.setattr(cli, "KNOWN_AGENTS", _single_agent(home, "cursor"))
    _stub_provision(monkeypatch)

    rc = cli.main(["install", "--repo", str(repo), "--agents", "cursor"])

    assert rc == 1  # error surfaced in the exit code
    assert "does not parse as JSON" in capsys.readouterr().out
    assert cfg.read_bytes() == b'{"mcpServers": '


def test_r3_a1_type_confused_parent_not_destroyed_without_force():
    data = {"mcp": {"servers": ["legacy-server-list"], "other": 1}}
    new, changed, reason = cli.merge_json(
        data, ("mcp", "servers", "cortex"), {"command": "x"}, force=False
    )
    assert changed is False, "must not rewrite when the pointer path holds non-dict data"
    assert reason in ("skipped-exists",)
    assert new["mcp"]["servers"] == ["legacy-server-list"]
    assert data["mcp"]["servers"] == ["legacy-server-list"]


# ===========================================================================
# Attack 2 — backup semantics
# ===========================================================================


def test_r3_a2_two_writes_distinct_seconds_two_backups_first_holds_original(tmp_path, monkeypatch):
    _fake_clock(monkeypatch, ["20260912-120000", "20260912-120001"])
    p = tmp_path / "config.json"
    p.write_bytes(b"ORIGINAL")
    b1 = cli.write_with_backup(p, b"V2")
    b2 = cli.write_with_backup(p, b"V3")
    assert b1 is not None and b2 is not None and b1 != b2
    assert b1.read_bytes() == b"ORIGINAL"  # first backup holds the original bytes
    assert b2.read_bytes() == b"V2"  # second backup holds the prior generation
    assert p.read_bytes() == b"V3"
    assert sorted(q.name for q in tmp_path.glob("*cortex-backup*")) == [b1.name, b2.name]


def test_r3_a2_force_write_backup_holds_pre_force_bytes(tmp_path, monkeypatch, capsys):
    """Backup-before-first-write through the real register_agent --force path."""
    home = tmp_path / "home"
    cfg = _write(
        home / ".zcode" / "cli" / "config.json",
        {"mcp": {"servers": {"cortex": {"command": "old"}, "mem": {"command": "m"}}}},
    )
    original = cfg.read_bytes()
    monkeypatch.setattr(cli, "KNOWN_AGENTS", _single_agent(home, "zcode"))
    spec = _single_agent(home, "zcode")[0]
    reg = {"type": "stdio", "command": "P", "args": ARGS, "cwd": "R"}

    results = cli.register_agent(spec, home, reg, force=True, dry_run=False)

    assert results == [(str(cfg), cli.REGISTERED)]
    backups = list(home.rglob("*cortex-backup*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original  # PRIOR content, not the new content
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcp"]["servers"]["cortex"] == reg
    assert data["mcp"]["servers"]["mem"] == {"command": "m"}
    assert "backup:" in capsys.readouterr().out


def test_r3_a2_same_second_collision_must_not_overwrite_first_backup(tmp_path, monkeypatch):
    _fake_clock(monkeypatch, ["20260912-120000", "20260912-120000"])  # forced collision
    p = tmp_path / "config.json"
    p.write_bytes(b"ORIGINAL")
    b1 = cli.write_with_backup(p, b"V2")
    b2 = cli.write_with_backup(p, b"V3")
    assert b1 != b2, "second same-second write must get a distinct backup name"
    assert len(list(tmp_path.glob("*cortex-backup*"))) == 2, "both backups must coexist"
    assert b1.read_bytes() == b"ORIGINAL", "first backup must still hold the original bytes"


# ===========================================================================
# Attack 3 — dry-run purity on a real-ish tree
# ===========================================================================


def test_r3_a3_dry_run_tree_byte_identical_realish_home(tmp_path, monkeypatch, capsys):
    root = tmp_path
    home = root / "home"
    repo = root / "repo"
    repo.mkdir()
    zcode = _write(
        home / ".zcode" / "cli" / "config.json",
        {"theme": "dark", "mcp": {"servers": {"memory": {"command": "npx", "args": ["-y", "m"]}}}},
    )
    claude = _write(
        home / ".claude.json",
        {"user": {"id": 1}, "mcpServers": {"cortex": {"command": "old-cortex"}}},
    )
    cursor = _write(home / ".cursor" / "mcp.json", {"mcpServers": {}})
    codex = home / ".codex" / "config.toml"
    codex.parent.mkdir(parents=True)
    codex.write_bytes(b'model = "x"\n[mcp_servers.other]\ncommand = "y"\n')
    venvpy = str(repo.resolve() / VENV_REL)
    kimi = _write(
        home / ".kimi-code" / "mcp.json",
        {"mcpServers": {"cortex": {"type": "stdio", "command": venvpy, "args": ARGS}}},
    )  # exactly equal -> UNCHANGED

    monkeypatch.setattr(cli, "KNOWN_AGENTS", _agents_for(home))
    calls = _stub_provision(monkeypatch)

    before = _snapshot(root)
    rc = cli.main(["install", "--dry-run", "--repo", str(repo)])
    after = _snapshot(root)

    assert rc == 0
    assert before == after, "dry-run must leave the whole tree byte-identical"
    assert calls == [], "no venv/pip/probe side effects in dry-run"
    assert not list(root.rglob("*cortex-backup*")), "no backup files anywhere"
    for f in (zcode, claude, cursor, codex, kimi):
        assert f.exists()
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "would write" in out
    assert "would back up to" in out
    assert cli.SKIPPED_EXISTS in out  # claude differing entry reported, not touched
    assert cli.REGISTERED in out  # zcode, cursor, codex, kimi? (kimi unchanged)
    assert cli.UNCHANGED in out  # kimi equal entry
    assert out.count(cli.SKIPPED_NOT_FOUND) == 0  # all five agents had a config


# ===========================================================================
# Attack 4 — update is ff-only, clean abort, no reset anywhere
# ===========================================================================


def test_r3_a4_update_nonff_aborts_clean_marker_untouched(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = repo / "marker.txt"
    marker.write_bytes(b"DO-NOT-RESET")
    before_tree = _snapshot(repo)
    fake_run, calls = _git_recorder(
        [
            (("git", "fetch"), {"returncode": 0}),
            (
                ("git", "merge", "--ff-only"),
                {"returncode": 1, "stderr": "fatal: Not possible to fast-forward, aborting."},
            ),
        ]
    )
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_package_version", lambda exe: "0.5.7")
    provisioned = _stub_provision(monkeypatch)

    rc = cli.cmd_update(
        argparse_namespace(repo=str(repo), dry_run=False)
    )

    assert rc == 2  # clean, non-zero abort
    out = capsys.readouterr().out
    assert "not fast-forward" in out
    assert "reset" not in out.lower()
    assert _snapshot(repo) == before_tree  # no reset/checkout touched the working tree
    assert marker.read_bytes() == b"DO-NOT-RESET"
    assert provisioned == [], "no reinstall after a failed merge"
    for argv in calls:
        assert argv[0] == "git"
        assert not any("reset" in a.lower() or "--hard" in a for a in argv)


def test_r3_a4_update_success_uses_exact_ff_only_argv_and_reports_versions(
    tmp_path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_run, calls = _git_recorder([(("git",), {"returncode": 0})])
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    versions = iter(["0.5.7", "0.5.8"])
    monkeypatch.setattr(cli, "_package_version", lambda exe: next(versions))
    provisioned = _stub_provision(monkeypatch)

    rc = cli.cmd_update(argparse_namespace(repo=str(repo), dry_run=False))

    assert rc == 0
    assert ["git", "fetch", "origin"] in calls
    assert ["git", "merge", "--ff-only", "origin/main"] in calls  # ff-only, nothing else
    for argv in calls:
        assert "reset" not in [a.lower() for a in argv]
        assert "--hard" not in argv
    out = capsys.readouterr().out
    assert "version before: 0.5.7" in out
    assert "version after:  0.5.8" in out
    assert provisioned == ["venv", "pip", "pip", "probe"]


def test_r3_a4_update_dry_run_runs_nothing(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_run, calls = _git_recorder([])
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(
        cli, "_package_version", lambda exe: pytest.fail("update dry-run must not run python")
    )

    rc = cli.cmd_update(argparse_namespace(repo=str(repo), dry_run=True))

    assert rc == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "--ff-only" in out and "DRY-RUN" in out


def test_r3_a4_no_reset_or_hard_anywhere_in_cli_source():
    """'reset'/'--hard' must appear nowhere outside docstrings; '--hard' nowhere at all."""
    src_path = Path(cli.__file__)
    src = src_path.read_text(encoding="utf-8")
    assert "--hard" not in src, "--hard must not appear anywhere in cli.py"
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    scrubbed = src
    for doc in docstrings:
        scrubbed = scrubbed.replace(doc, "")
    assert "reset" not in scrubbed, (
        "'reset' outside docstrings (the module docstring's 'no reset anywhere' promise "
        "is the only allowed occurrence)"
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in docstrings:
                continue
            assert "reset" not in node.value
            assert "--hard" not in node.value


# ===========================================================================
# Attack 5 — TOML append-only / refuse-malformed
# ===========================================================================


@pytest.mark.parametrize(
    "preamble",
    [
        b'model = "gpt-x"\n[mcp_servers.other]\ncommand = "y"\n',
        b'[mcp_servers]\nroot = 1\n[other]\nx = 1\n',
        b'no-newline-at-end = true',
    ],
    ids=["other-subtable", "empty-mcp_servers-table", "no-trailing-newline"],
)
def test_r3_a5_toml_append_keeps_other_sections_and_parses(tmp_path, preamble):
    p = tmp_path / "config.toml"
    p.write_bytes(preamble)
    reg = {"type": "stdio", "command": "P:\\py.exe", "args": ARGS}

    status, backup = cli.toml_register(p, reg, force=False)

    assert status == "registered"
    assert backup is not None and backup.read_bytes() == preamble
    data = tomllib.loads(p.read_text(encoding="utf-8"))  # file must parse after append
    assert data["mcp_servers"]["cortex"] == {"command": "P:\\py.exe", "args": ARGS}
    if b"other" in preamble:
        assert "other" in data["mcp_servers"] or "other" in data
    for key in ("model", "root", "no-newline-at-end"):
        if key.encode() in preamble:
            flattened = str(data)
            assert key in flattened


def test_r3_a5_toml_malformed_refuses_to_touch(tmp_path, capsys):
    for broken in (b"not [valid toml", b'mcp_servers = "string-not-table"'):
        p = tmp_path / "config.toml"
        p.write_bytes(broken)
        status, backup = cli.toml_register(p, {"command": "P", "args": ARGS}, force=False)
        assert status == "error"
        assert backup is None
        assert p.read_bytes() == broken  # refuses to touch the file
        assert "toml" in capsys.readouterr().out.lower()


def test_r3_a5_toml_existing_section_via_register_agent_force_untouched(tmp_path, capsys):
    home = tmp_path / "home"
    raw = b'[mcp_servers.cortex]\ncommand = "already-here"\nargs = []\n'
    cfg = home / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_bytes(raw)
    spec = _agents_for(home)[3]  # codex
    assert spec.fmt == "codex-toml"

    results = cli.register_agent(
        spec, home, {"type": "stdio", "command": "P", "args": ARGS}, force=True, dry_run=False
    )

    assert results == [(str(cfg), cli.SKIPPED_EXISTS)]
    assert cfg.read_bytes() == raw  # force never rewrites TOML
    assert not list(home.rglob("*cortex-backup*"))
    assert "--force does not rewrite TOML" in capsys.readouterr().out


# ===========================================================================
# Attack 6 — probe exactness
# ===========================================================================


class _FakeProc:
    def __init__(self, stdout="", raise_timeout=False, poll_value=0):
        self._stdout = stdout
        self._raise_timeout = raise_timeout
        self._poll_value = poll_value
        self.kill_count = 0
        self.drain_called = False  # set on any communicate() that RETURNS (i.e. post-kill drain)

    def communicate(self, input=None, timeout=None):
        if self._raise_timeout:
            self._raise_timeout = False
            raise subprocess.TimeoutExpired(cmd="probe", timeout=timeout)
        self.drain_called = True
        return self._stdout, ""

    def poll(self):
        return self._poll_value

    def kill(self):
        self.kill_count += 1


def _probe_stdout(tools):
    init = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
    listing = {"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}}
    return "\n".join([json.dumps(init), json.dumps(listing)]) + "\n"


def _run_probe(monkeypatch, tmp_path, proc):
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: proc)
    return cli.probe_server("python", tmp_path, timeout=5)


def test_r3_a6_probe_rejects_ref_token(monkeypatch, tmp_path):
    tools = [
        {"name": n, "inputSchema": {"$ref": "#/definitions/evil"}}
        for n in sorted(cli.EXPECTED_TOOLS)
    ]
    ok, detail = _run_probe(monkeypatch, tmp_path, _FakeProc(_probe_stdout(tools)))
    assert ok is False
    assert "$ref" in detail, f"precise reason required, got: {detail}"


def test_r3_a6_probe_rejects_four_tools(monkeypatch, tmp_path):
    tools = [{"name": n} for n in sorted(cli.EXPECTED_TOOLS)][:4]
    ok, detail = _run_probe(monkeypatch, tmp_path, _FakeProc(_probe_stdout(tools)))
    assert ok is False
    assert "mismatch" in detail
    assert "stop_session" not in detail  # the dropped tool is precisely why it failed


def test_r3_a6_probe_rejects_sixth_rogue_tool(monkeypatch, tmp_path):
    tools = [{"name": n} for n in sorted(cli.EXPECTED_TOOLS)] + [{"name": "rogue_tool"}]
    ok, detail = _run_probe(monkeypatch, tmp_path, _FakeProc(_probe_stdout(tools)))
    assert ok is False
    assert "mismatch" in detail and "rogue_tool" in detail


def test_r3_a6_probe_timeout_kills_child_even_if_unreaped(monkeypatch, tmp_path):
    proc = _FakeProc(raise_timeout=True, poll_value=None)  # stubborn child
    ok, detail = _run_probe(monkeypatch, tmp_path, proc)
    assert ok is False
    assert "timed out" in detail
    assert proc.kill_count >= 1, "child must be killed on timeout"
    assert proc.drain_called, "output pipe must be drained (communicate) after kill"


def test_r3_a6_probe_missing_initialize_reason(monkeypatch, tmp_path):
    listing = {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}
    ok, detail = _run_probe(
        monkeypatch, tmp_path, _FakeProc(json.dumps(listing) + "\n")
    )
    assert ok is False
    assert "initialize" in detail


def test_r3_a6_probe_duplicate_tool_names_set_semantics_documented(monkeypatch, tmp_path):
    """Documents current set-semantics: a duplicated name still passes (5 unique names).

    The 5-tool invariant is enforced as a SET of unique names; a duplicated tools/list
    entry is a protocol violation the real server never emits. Pinned so any tightening
    or loosening is a conscious decision.
    """
    names = sorted(cli.EXPECTED_TOOLS)
    tools = [{"name": n} for n in names] + [{"name": names[0]}]  # 6 entries, 5 unique
    ok, detail = _run_probe(monkeypatch, tmp_path, _FakeProc(_probe_stdout(tools)))
    assert ok is True
    assert "5 tools" in detail


# ===========================================================================
# Attack 7 — registration shapes (cwd zcode-only)
# ===========================================================================


def test_r3_a7_full_install_registration_shapes_cwd_zcode_only(tmp_path, monkeypatch, capsys):
    root = tmp_path
    home = root / "home"
    repo = root / "repo"
    repo.mkdir()
    _write(home / ".zcode" / "cli" / "config.json", {})
    _write(home / ".claude.json", {})
    _write(home / ".cursor" / "mcp.json", {})
    codex = home / ".codex" / "config.toml"
    codex.parent.mkdir(parents=True)
    codex.write_bytes(b'[mcp_servers.other]\ncommand = "y"\n')
    _write(home / ".kimi-code" / "mcp.json", {})
    monkeypatch.setattr(cli, "KNOWN_AGENTS", _agents_for(home))
    _stub_provision(monkeypatch)

    rc = cli.main(["install", "--repo", str(repo)])

    assert rc == 0
    expected_repo = repo.resolve()
    expected_python = str(expected_repo / VENV_REL)

    zcode = json.loads((home / ".zcode" / "cli" / "config.json").read_text(encoding="utf-8"))
    assert zcode["mcp"]["servers"]["cortex"] == {
        "type": "stdio",
        "command": expected_python,
        "args": ARGS,
        "cwd": str(expected_repo),
    }
    assert list(zcode["mcp"]["servers"]["cortex"]) == ["type", "command", "args", "cwd"]

    for rel, pointer in (
        (".claude.json", "mcpServers"),
        (".cursor/mcp.json", "mcpServers"),
        (".kimi-code/mcp.json", "mcpServers"),
    ):
        data = json.loads((home / rel).read_text(encoding="utf-8"))
        reg = data[pointer]["cortex"]
        assert "cwd" not in reg, f"{rel}: cwd must be zcode-only"
        assert list(reg) == ["type", "command", "args"]
        assert reg["command"] == expected_python and reg["args"] == ARGS

    toml_data = tomllib.loads(codex.read_text(encoding="utf-8"))
    assert set(toml_data["mcp_servers"]["cortex"]) == {"command", "args"}
    assert "cwd" not in toml_data["mcp_servers"]["cortex"]
    assert toml_data["mcp_servers"]["other"] == {"command": "y"}
    out = capsys.readouterr().out
    assert out.count(cli.REGISTERED) == 5


# ===========================================================================
# Attack 8 — stdlib-only imports
# ===========================================================================


def test_r3_a8_cli_imports_stdlib_only():
    src = Path(cli.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module)
    allowlist = {
        "__future__",
        "argparse",
        "copy",
        "json",
        "shutil",
        "subprocess",
        "sys",
        "tomllib",
        "collections.abc",
        "dataclasses",
        "datetime",
        "pathlib",
    }
    violations = imported - allowlist
    assert not violations, f"non-allowlisted imports in cli.py: {sorted(violations)}"
    for mod in imported:
        top = mod.split(".")[0]
        assert top in sys.stdlib_module_names, f"{mod} is not stdlib"


# ===========================================================================
# helpers
# ===========================================================================


def argparse_namespace(*, repo: str, dry_run: bool):
    import argparse as _argparse

    return _argparse.Namespace(repo=repo, dry_run=dry_run)
