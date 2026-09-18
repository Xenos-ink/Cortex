"""C060 unit tests for the ``cortex-mcp probe`` subcommand.

Non-e2e: the probe subprocess is stubbed with an injectable fake ``Popen`` (same pattern
as the C059 cli tests) — no real server, no real venv, no network, no agent configs.
Covers the single-line PASS/FAIL output, exit codes via ``main()``, the failure modes
(4-tool surface, anyOf token, timeout kill, child exit without a tools/list response),
the stdin-open request discipline (the probe must NOT close the child's stdin before the
responses arrive — closing it early race-cancels the server's in-flight tools/list
handling and loses responses from a healthy server), subprocess argument forwarding,
and the default ``--python``/``--cwd`` resolution.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from computer_use_mcp import cli


# ---------------------------------------------------------------------------
# fakes (injectable Popen — no real subprocess)
# ---------------------------------------------------------------------------


class _FakeStdin:
    def __init__(self):
        self.written = ""
        self.closed = False

    def write(self, text):
        self.written += text

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _FakeProc:
    """Stream-based fake child: stdin records writes, stdout is a line iterator.

    There is deliberately NO ``communicate`` — the probe must not use it
    (``communicate(input=...)`` closes the child's stdin right after writing,
    which is exactly the race the streaming probe fixed).
    """

    def __init__(self, stdout="", hang=False, delay_before_tools=0.0):
        self.stdin = _FakeStdin()
        self.killed = False
        self._hang = hang
        self._delay = delay_before_tools
        self._lines = stdout.splitlines(keepends=True)
        self.stdout = self._iter_stdout()

    def _iter_stdout(self):
        tools_seen = False
        for line in self._lines:
            if self._delay and not tools_seen and '"id": 2' in line:
                time.sleep(self._delay)  # responses arrive late; stdin stays open
            tools_seen = tools_seen or '"id": 2' in line
            yield line
        while self._hang and not self.killed:
            time.sleep(0.01)  # child alive, stdout open, no further data

    def poll(self):
        return None  # appears alive so the probe must kill() it

    def kill(self):
        self.killed = True


def _stdout_for(tools: list[str], extra_schema: dict | None = None) -> str:
    init = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
    entries: list[dict] = [{"name": n} for n in tools]
    if extra_schema:
        entries[0]["inputSchema"] = extra_schema
    listing = {"jsonrpc": "2.0", "id": 2, "result": {"tools": entries}}
    return json.dumps(init) + "\n" + json.dumps(listing) + "\n"


def _all_tools() -> list[str]:
    return sorted(cli.EXPECTED_TOOLS)


def _make_repo_with_venv(root: Path) -> Path:
    """A fake repo whose .venv python exists (path per platform via cli._venv_python)."""
    repo = root / "repo"
    venv_python = cli._venv_python(repo / ".venv")
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"")
    return repo


# ---------------------------------------------------------------------------
# PASS path + exit codes via main()
# ---------------------------------------------------------------------------


def test_probe_pass_prints_one_line_and_exits_zero(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *a, **k: _FakeProc(_stdout_for(_all_tools()))
    )
    rc = cli.main(["probe", "--python", "python", "--cwd", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    lines = out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"PROBE PASS — {len(cli.EXPECTED_TOOLS)} tools: ")
    for name in cli.EXPECTED_TOOLS:  # every tool name appears in the pass line
        assert name in lines[0]


def test_probe_fail_four_tools_exits_one(monkeypatch, tmp_path, capsys):
    tools = _all_tools()[:-1]  # drop one tool -> 4-tool surface
    monkeypatch.setattr(
        cli.subprocess, "Popen", lambda *a, **k: _FakeProc(_stdout_for(tools))
    )
    rc = cli.main(["probe", "--python", "python", "--cwd", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    lines = out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("PROBE FAIL — ")
    assert "mismatch" in lines[0]


def test_probe_fail_anyof_token_exits_one(monkeypatch, tmp_path, capsys):
    stdout = _stdout_for(_all_tools(), extra_schema={"type": "object", "anyOf": []})
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: _FakeProc(stdout))
    rc = cli.main(["probe", "--python", "python", "--cwd", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.splitlines() == ["PROBE FAIL — forbidden schema token 'anyOf' present in tools payload"]


def test_probe_timeout_kills_child_and_exits_one(monkeypatch, tmp_path, capsys):
    proc = _FakeProc(hang=True)  # child alive, stdout open, never answers
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: proc)
    rc = cli.main(
        ["probe", "--python", "python", "--cwd", str(tmp_path), "--timeout", "1"]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert proc.killed is True
    assert out.splitlines() == ["PROBE FAIL — probe timed out after 1s"]


def test_probe_unstartable_python_prints_single_fail_line(monkeypatch, tmp_path, capsys):
    def boom(*a, **k):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(cli.subprocess, "Popen", boom)
    rc = cli.main(["probe", "--python", "C:/nonexistent/python.exe", "--cwd", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    lines = out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("PROBE FAIL — cannot start ")
    assert "nonexistent" in lines[0]


# ---------------------------------------------------------------------------
# subprocess argument forwarding
# ---------------------------------------------------------------------------


def test_probe_forwards_python_cwd_and_server_module(monkeypatch, tmp_path):
    calls: dict = {}

    def fake_popen(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return _FakeProc(_stdout_for(_all_tools()))

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    cwd = tmp_path / "some" / "repo"
    rc = cli.main(["probe", "--python", "C:/py/python.exe", "--cwd", str(cwd)])
    assert rc == 0
    assert calls["argv"] == [str(Path("C:/py/python.exe")), "-m", "computer_use_mcp.server"]
    assert calls["kwargs"]["cwd"] == str(cwd)


def test_probe_writes_all_requests_and_holds_stdin_open(monkeypatch, tmp_path):
    proc = _FakeProc(_stdout_for(_all_tools()))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: proc)
    rc = cli.main(["probe", "--python", "python", "--cwd", str(tmp_path)])
    assert rc == 0
    assert '"id": 1' in proc.stdin.written  # initialize
    assert "notifications/initialized" in proc.stdin.written
    assert '"id": 2' in proc.stdin.written  # tools/list
    assert proc.stdin.closed is True  # closed only during teardown, after the verdict


def test_probe_survives_delayed_tools_list_response(monkeypatch, tmp_path):
    # A healthy server whose tools/list response arrives late: the probe must keep
    # stdin open and keep reading until both responses land (the EOF race fix).
    proc = _FakeProc(_stdout_for(_all_tools()), delay_before_tools=0.3)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: proc)
    rc = cli.main(["probe", "--python", "python", "--cwd", str(tmp_path)])
    assert rc == 0


def test_probe_child_exit_without_tools_list_response_fails_typed(monkeypatch, tmp_path, capsys):
    # Child answered initialize, then exited: typed tools/list failure, no timeout.
    stdout = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "x"}}) + "\n"
    proc = _FakeProc(stdout)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: proc)
    rc = cli.main(["probe", "--python", "python", "--cwd", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.splitlines() == ["PROBE FAIL — no tools/list response from server"]


# ---------------------------------------------------------------------------
# default --python / --cwd resolution
# ---------------------------------------------------------------------------


def test_default_probe_python_prefers_repo_venv_python(tmp_path, monkeypatch):
    repo = _make_repo_with_venv(tmp_path)
    monkeypatch.setattr(cli, "default_repo", lambda: repo)
    assert cli.default_probe_python() == cli._venv_python(repo / ".venv")


def test_default_probe_python_falls_back_to_running_interpreter(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "default_repo", lambda: tmp_path)  # repo without a .venv
    assert cli.default_probe_python() == Path(sys.executable)


def test_probe_defaults_use_repo_venv_python_and_repo_root(monkeypatch, tmp_path, capsys):
    repo = _make_repo_with_venv(tmp_path)
    monkeypatch.setattr(cli, "default_repo", lambda: repo)
    calls: dict = {}

    def fake_popen(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return _FakeProc(_stdout_for(_all_tools()))

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    rc = cli.main(["probe"])  # no flags: everything resolved from the repo layout
    out = capsys.readouterr().out
    assert rc == 0
    assert calls["argv"][0] == str(cli._venv_python(repo / ".venv"))
    assert calls["kwargs"]["cwd"] == str(repo)
    assert "PROBE PASS" in out


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def test_probe_argparse_wiring_and_defaults():
    parser = cli.build_parser()
    args = parser.parse_args(
        ["probe", "--python", "P", "--cwd", "C", "--timeout", "7"]
    )
    assert args.command == "probe"
    assert args.python == "P"
    assert args.cwd == "C"
    assert args.timeout == 7.0
    assert args.func is cli.cmd_probe

    defaults = parser.parse_args(["probe"])
    assert defaults.python is None
    assert defaults.cwd is None
    assert defaults.timeout == 30.0
    assert defaults.func is cli.cmd_probe
