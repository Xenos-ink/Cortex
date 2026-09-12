"""C059 unit tests for ``computer_use_mcp.cli`` (the ``cortex-mcp`` installer).

Non-e2e: tmp_path fake homes, monkeypatched KNOWN_AGENTS paths. No real agent configs,
no venv creation, no pip, and no probe subprocess (the probe is covered via an
injectable fake ``Popen``).
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from computer_use_mcp import cli
from computer_use_mcp.cli import KNOWN_AGENTS, AgentSpec, build_registration, merge_json

REG = {
    "type": "stdio",
    "command": "C:\\repo\\.venv\\Scripts\\python.exe",
    "args": ["-m", "computer_use_mcp.server"],
    "cwd": "C:\\repo",
}
ZCODE_POINTER = ("mcp", "servers", "cortex")


def _agents_for(home: Path) -> tuple[AgentSpec, ...]:
    """KNOWN_AGENTS-shaped table bound to a fake home (lambdas ignore the passed home)."""
    return (
        AgentSpec(
            name="zcode",
            paths=lambda _home: [home / ".zcode" / "cli" / "config.json"],
            fmt="zcode-json",
            pointer=("mcp", "servers", "cortex"),
        ),
        AgentSpec(
            name="claude",
            paths=lambda _home: [home / ".claude.json"],
            fmt="claude-json",
            pointer=("mcpServers", "cortex"),
        ),
        AgentSpec(
            name="cursor",
            paths=lambda _home: [home / ".cursor" / "mcp.json"],
            fmt="cursor-json",
            pointer=("mcpServers", "cortex"),
        ),
        AgentSpec(
            name="codex",
            paths=lambda _home: [home / ".codex" / "config.toml"],
            fmt="codex-toml",
            pointer=("mcp_servers", "cortex"),
        ),
        AgentSpec(
            name="kimi",
            paths=lambda _home: [
                home / ".kimi-code" / "config.json",
                home / ".kimi-code" / "mcp.json",
            ],
            fmt="claude-json",
            pointer=("mcpServers", "cortex"),
        ),
    )


def _snapshot(root: Path) -> dict[str, bytes | None]:
    """Full recursive snapshot (files -> bytes, dirs -> None) for zero-write assertions."""
    out: dict[str, bytes | None] = {}
    for p in sorted(root.rglob("*")):
        key = p.relative_to(root).as_posix()
        out[key] = p.read_bytes() if p.is_file() else None
    return out


# ---------------------------------------------------------------------------
# merge_json
# ---------------------------------------------------------------------------


def test_merge_json_creates_nested_parents_on_empty_dict():
    new, changed, reason = merge_json({}, ZCODE_POINTER, REG, force=False)
    assert changed is True
    assert reason == "created"
    assert new["mcp"]["servers"]["cortex"] == REG


def test_merge_json_equal_existing_entry_unchanged():
    data = {"other": 1, "mcp": {"servers": {"cortex": dict(REG)}}}
    new, changed, reason = merge_json(data, ZCODE_POINTER, REG, force=False)
    assert changed is False
    assert reason == "unchanged"
    assert new["mcp"]["servers"]["cortex"] == REG


def test_merge_json_differing_entry_skipped_without_force():
    existing = dict(REG, command="C:\\old\\python.exe")
    data = {"mcp": {"servers": {"cortex": existing}}}
    new, changed, reason = merge_json(data, ZCODE_POINTER, REG, force=False)
    assert changed is False
    assert reason == "skipped-exists"
    assert new["mcp"]["servers"]["cortex"] == existing  # never clobbered


def test_merge_json_force_replaces_existing():
    existing = dict(REG, command="C:\\old\\python.exe")
    data = {"mcp": {"servers": {"cortex": existing}}}
    new, changed, reason = merge_json(data, ZCODE_POINTER, REG, force=True)
    assert changed is True
    assert reason == "replaced"
    assert new["mcp"]["servers"]["cortex"] == REG


def test_merge_json_preserves_unrelated_keys_exactly():
    data = {
        "top": {"keep": [1, 2, 3], "nested": {"x": None}},
        "mcp": {"servers": {"memory": {"command": "npx", "args": ["-y", "srv"]}, "z": 4}},
    }
    new, changed, _ = merge_json(data, ZCODE_POINTER, REG, force=False)
    assert changed is True
    assert new["top"] == {"keep": [1, 2, 3], "nested": {"x": None}}
    assert new["mcp"]["servers"]["memory"] == {"command": "npx", "args": ["-y", "srv"]}
    assert new["mcp"]["servers"]["z"] == 4
    assert set(new.keys()) == {"top", "mcp"}
    # input untouched (pure)
    assert "cortex" not in data["mcp"]["servers"]


# ---------------------------------------------------------------------------
# write_with_backup
# ---------------------------------------------------------------------------


def test_write_with_backup_creates_backup_with_prior_content(tmp_path):
    path = tmp_path / "config.json"
    prior = b'{"before": true}'
    path.write_bytes(prior)
    new_bytes = b'{"before": true, "after": true}'

    backup = cli.write_with_backup(path, new_bytes)

    assert backup is not None
    assert backup.parent == tmp_path
    assert backup.name.startswith("config.json.cortex-backup-")
    stamp = backup.name.removeprefix("config.json.cortex-backup-")
    assert len(stamp) == 15 and stamp[8] == "-"  # YYYYmmdd-HHMMSS
    assert backup.read_bytes() == prior
    assert path.read_bytes() == new_bytes


def test_write_with_backup_no_backup_when_file_absent(tmp_path):
    path = tmp_path / "fresh.json"
    backup = cli.write_with_backup(path, b"{}")
    assert backup is None
    assert path.read_bytes() == b"{}"
    assert list(tmp_path.iterdir()) == [path]


# ---------------------------------------------------------------------------
# toml_register
# ---------------------------------------------------------------------------


def test_toml_register_appends_parseable_section(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(b'model = "gpt-x"\n')
    reg = {"type": "stdio", "command": "C:\\repo\\.venv\\Scripts\\python.exe", "args": ["-m", "computer_use_mcp.server"]}

    status, backup = cli.toml_register(path, reg, force=False)

    assert status == "registered"
    assert backup is not None and backup.read_bytes() == b'model = "gpt-x"\n'
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["model"] == "gpt-x"
    cortex = data["mcp_servers"]["cortex"]
    assert cortex["command"] == reg["command"]
    assert cortex["args"] == reg["args"]


def test_toml_register_existing_section_left_untouched_even_with_force(tmp_path):
    path = tmp_path / "config.toml"
    raw = b'model = "gpt-x"\n\n[mcp_servers.cortex]\ncommand = "already-here"\n'
    path.write_bytes(raw)

    status, backup = cli.toml_register(path, dict(REG), force=True)

    assert status == "skipped-exists"
    assert backup is None
    assert path.read_bytes() == raw  # byte-identical: force never rewrites TOML
    assert not any("cortex-backup" in p.name for p in tmp_path.iterdir())


# ---------------------------------------------------------------------------
# discovery / planning
# ---------------------------------------------------------------------------


def test_discover_existing_path_is_planned(tmp_path, capsys):
    home = tmp_path / "home"
    cfg = home / ".zcode" / "cli" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcp": {"servers": {}}}), encoding="utf-8")
    spec = _agents_for(home)[0]
    assert spec.name == "zcode"

    results = cli.register_agent(spec, home, dict(REG), force=False, dry_run=True)

    out = capsys.readouterr().out
    assert results == [(str(cfg), cli.REGISTERED)]
    assert "would write" in out
    assert "would back up to" in out


def test_discover_missing_path_reports_not_found(tmp_path, capsys):
    home = tmp_path / "empty-home"
    spec = _agents_for(home)[0]

    results = cli.register_agent(spec, home, dict(REG), force=False, dry_run=True)

    out = capsys.readouterr().out
    assert results == [(cli.SKIPPED_NOT_FOUND, "")]
    assert cli.SKIPPED_NOT_FOUND in out
    assert ".zcode" in out  # reports the candidate paths it looked for


# ---------------------------------------------------------------------------
# cmd_install dry-run: zero writes + plan output
# ---------------------------------------------------------------------------


def test_install_dry_run_performs_zero_writes(tmp_path, monkeypatch, capsys):
    root = tmp_path
    home = root / "home"
    repo = root / "repo"

    # zcode config exists (no cortex yet) -> would register.
    zcode_cfg = home / ".zcode" / "cli" / "config.json"
    zcode_cfg.parent.mkdir(parents=True)
    zcode_cfg.write_text(
        json.dumps({"mcp": {"servers": {"memory": {"command": "npx"}}}}), encoding="utf-8"
    )
    # kimi mcp.json exists WITH a differing cortex entry -> skip-exists (never clobber).
    kimi_cfg = home / ".kimi-code" / "mcp.json"
    kimi_cfg.parent.mkdir(parents=True)
    kimi_cfg.write_text(
        json.dumps({"mcpServers": {"cortex": {"command": "uv", "args": ["run", "cortex"]}}}),
        encoding="utf-8",
    )
    # claude/cursor/codex absent -> SKIPPED-NOT-FOUND.

    monkeypatch.setattr(cli, "KNOWN_AGENTS", _agents_for(home))

    calls: list[tuple] = []
    monkeypatch.setattr(cli, "_ensure_venv", lambda *a, **k: calls.append(("venv", *a)))
    monkeypatch.setattr(cli, "_pip_install", lambda *a, **k: calls.append(("pip", *a)))
    monkeypatch.setattr(cli, "probe_server", lambda *a, **k: calls.append(("probe", *a)) or (True, "stub"))

    before = _snapshot(root)
    rc = cli.main(["install", "--dry-run", "--repo", str(repo)])
    after = _snapshot(root)

    assert rc == 0
    assert before == after  # zero writes anywhere under the fake root
    assert calls == []  # no venv/pip/probe side effects in dry-run
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "would ensure venv" in out
    assert "pip install -e" in out
    assert cli.SKIPPED_NOT_FOUND in out  # claude, cursor, codex
    assert cli.SKIPPED_EXISTS in out  # kimi (differing cortex entry preserved)
    assert cli.REGISTERED in out  # zcode planned
    assert "summary:" in out
    assert "Restart each agent to load the new registration." in out


# ---------------------------------------------------------------------------
# registration shape
# ---------------------------------------------------------------------------


def test_registration_shape_matches_zcode_structure_exactly():
    reg = build_registration(Path("C:\\repo"), Path("C:\\repo\\.venv\\Scripts\\python.exe"))
    assert reg == {
        "type": "stdio",
        "command": "C:\\repo\\.venv\\Scripts\\python.exe",
        "args": ["-m", "computer_use_mcp.server"],
        "cwd": "C:\\repo",
    }
    assert list(reg) == ["type", "command", "args", "cwd"]


def test_registration_has_no_cwd_for_claude_cursor_codex():
    reg = build_registration(
        Path("C:\\repo"), Path("C:\\repo\\.venv\\Scripts\\python.exe"), include_cwd=False
    )
    assert "cwd" not in reg
    assert reg == {
        "type": "stdio",
        "command": "C:\\repo\\.venv\\Scripts\\python.exe",
        "args": ["-m", "computer_use_mcp.server"],
    }
    for fmt in ("claude-json", "cursor-json", "codex-toml"):
        spec = next(s for s in KNOWN_AGENTS if s.fmt == fmt)
        reg = cli.build_registration(
            Path("r"), Path("p"), include_cwd=(spec.fmt == "zcode-json")
        )
        assert "cwd" not in reg


# ---------------------------------------------------------------------------
# probe verification logic (injectable fake Popen — no real subprocess)
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, stdout="", raise_timeout=False):
        self._stdout = stdout
        self._raise_timeout = raise_timeout
        self.killed = False

    def communicate(self, input=None, timeout=None):
        if self._raise_timeout:
            self._raise_timeout = False  # second call (post-kill drain) returns
            raise subprocess.TimeoutExpired(cmd="probe", timeout=timeout)
        return self._stdout, ""

    def poll(self):
        return 0

    def kill(self):
        self.killed = True


def _ok_stdout() -> str:
    init = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
    tools = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"tools": [{"name": n} for n in sorted(cli.EXPECTED_TOOLS)]},
    }
    return json.dumps(init) + "\n" + json.dumps(tools) + "\n"


def test_probe_stub_ok_on_exact_surface(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: _FakeProc(_ok_stdout()))
    ok, detail = cli.probe_server("python", tmp_path)
    assert ok is True
    assert "5 tools" in detail


def test_probe_stub_rejects_missing_tool(monkeypatch, tmp_path):
    tools = sorted(cli.EXPECTED_TOOLS)[:-1]  # drop one
    init = {"jsonrpc": "2.0", "id": 1, "result": {}}
    listing = {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": n} for n in tools]}}
    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *a, **k: _FakeProc(json.dumps(init) + "\n" + json.dumps(listing))
    )
    ok, detail = cli.probe_server("python", tmp_path)
    assert ok is False
    assert "mismatch" in detail


def test_probe_stub_rejects_anyof_token(monkeypatch, tmp_path):
    tools = [
        {"name": n, "inputSchema": {"type": "object"}}
        for n in sorted(cli.EXPECTED_TOOLS)
    ]
    tools[0]["inputSchema"]["anyOf"] = []  # banned token smuggled into a schema
    init = {"jsonrpc": "2.0", "id": 1, "result": {}}
    listing = {"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}}
    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *a, **k: _FakeProc(json.dumps(init) + "\n" + json.dumps(listing))
    )
    ok, detail = cli.probe_server("python", tmp_path)
    assert ok is False
    assert "anyOf" in detail


def test_probe_stub_kills_child_on_timeout(monkeypatch, tmp_path):
    proc = _FakeProc(raise_timeout=True)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: proc)
    ok, detail = cli.probe_server("python", tmp_path, timeout=5)
    assert ok is False
    assert "timed out" in detail
    assert proc.killed is True


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def test_cli_parse_smoke_install():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "install",
            "--repo", "R",
            "--venv", "V",
            "--agents", "zcode,kimi",
            "--force",
            "--dry-run",
        ]
    )
    assert args.command == "install"
    assert args.repo == "R"
    assert args.venv == "V"
    assert args.agents == "zcode,kimi"
    assert args.force is True
    assert args.dry_run is True
    assert callable(args.func) and args.func is cli.cmd_install


def test_cli_parse_smoke_defaults_and_update():
    parser = cli.build_parser()
    install = parser.parse_args(["install"])
    assert install.repo is None and install.venv is None
    assert install.agents == "all"
    assert install.force is False and install.dry_run is False

    update = parser.parse_args(["update", "--repo", "R", "--dry-run"])
    assert update.command == "update"
    assert update.repo == "R"
    assert update.dry_run is True
    assert update.func is cli.cmd_update


def test_known_agents_table_contract():
    assert [s.name for s in KNOWN_AGENTS] == ["zcode", "claude", "cursor", "codex", "kimi"]
    assert KNOWN_AGENTS[0].fmt == "zcode-json"
    assert KNOWN_AGENTS[0].pointer == ("mcp", "servers", "cortex")
    for spec in (KNOWN_AGENTS[1], KNOWN_AGENTS[2], KNOWN_AGENTS[4]):
        assert spec.pointer == ("mcpServers", "cortex")
    assert len(KNOWN_AGENTS[4].paths(Path("/h"))) == 2  # kimi: config.json + mcp.json candidates
    assert KNOWN_AGENTS[3].fmt == "codex-toml"


def test_select_agents_unknown_name_exits():
    with pytest.raises(SystemExit):
        cli.select_agents("zcode,nope")
    assert [s.name for s in cli.select_agents("kimi, zcode")] == ["zcode", "kimi"]
