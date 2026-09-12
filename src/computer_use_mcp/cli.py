"""``cortex-mcp`` — install/update the Cortex MCP registration into recognized agent configs.

Stdlib only (argparse/json/subprocess/pathlib/shutil/tomllib). Two subcommands:

- ``install``: provisions ``<repo>/.venv`` (editable install), refreshes the system-python
  console script (best-effort: without an elevated terminal this only warns), registers the
  Cortex MCP server into every recognized agent config found on the machine
  (zcode/claude/cursor/codex/kimi), backs up every file before the first write
  (collision-proof: an existing backup is never overwritten), never clobbers an existing
  cortex entry — nor a non-dict value sitting on the registration path — unless ``--force``,
  and verifies the built server with a zero-input stdio probe (exactly the 5 tools, zero
  anyOf/$ref).
- ``update``: fast-forwards the repo to ``origin/main`` (clean abort when not
  fast-forward — no reset anywhere), refreshes both editable installs, verifies the
  package version before/after, and re-probes the server.

The manual registration method stays intact: existing entries that already match are left
untouched (UNCHANGED); existing entries that differ are reported (SKIPPED-EXISTS-USE-FORCE)
and their files are never rewritten without ``--force``.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Statuses reported per agent config.
REGISTERED = "REGISTERED"
UNCHANGED = "UNCHANGED"
SKIPPED_EXISTS = "SKIPPED-EXISTS-USE-FORCE"
SKIPPED_NOT_FOUND = "SKIPPED-NOT-FOUND"

#: The exact 5-tool surface the probe must observe (server invariant).
EXPECTED_TOOLS = {
    "start_session",
    "stop_session",
    "computer_observe",
    "computer_screenshot",
    "computer_execute",
}

SERVER_MODULE = "computer_use_mcp.server"


@dataclass(frozen=True)
class AgentSpec:
    """One recognized agent: candidate config paths, format, and the cortex pointer."""

    name: str
    paths: Callable[[Path], list[Path]]
    fmt: str  # zcode-json | claude-json | cursor-json | codex-toml
    pointer: tuple[str, ...]


def _default_known_agents() -> tuple[AgentSpec, ...]:
    """Build the ordered KNOWN_AGENTS table for a given home (kept as a function for tests)."""
    return (
        AgentSpec(
            name="zcode",
            paths=lambda home: [home / ".zcode" / "cli" / "config.json"],
            fmt="zcode-json",
            pointer=("mcp", "servers", "cortex"),
        ),
        AgentSpec(
            name="claude",
            paths=lambda home: [home / ".claude.json"],
            fmt="claude-json",
            pointer=("mcpServers", "cortex"),
        ),
        AgentSpec(
            name="cursor",
            paths=lambda home: [home / ".cursor" / "mcp.json"],
            fmt="cursor-json",
            pointer=("mcpServers", "cortex"),
        ),
        AgentSpec(
            name="codex",
            paths=lambda home: [home / ".codex" / "config.toml"],
            fmt="codex-toml",
            pointer=("mcp_servers", "cortex"),
        ),
        AgentSpec(
            name="kimi",
            # Two candidate layouts; register only into a file that actually exists
            # (never guess an unknown inner layout).
            paths=lambda home: [
                home / ".kimi-code" / "config.json",
                home / ".kimi-code" / "mcp.json",
            ],
            fmt="claude-json",
            pointer=("mcpServers", "cortex"),
        ),
    )


KNOWN_AGENTS: tuple[AgentSpec, ...] = _default_known_agents()


def default_repo() -> Path:
    """Repo root = two parents up from this file (src/computer_use_mcp/cli.py)."""
    return Path(__file__).resolve().parents[2]


def build_registration(repo: Path, venv_python: Path, include_cwd: bool = True) -> dict:
    """The registration dict for the cortex entry (cwd only where the format supports it)."""
    reg: dict = {
        "type": "stdio",
        "command": str(venv_python),
        "args": ["-m", "computer_use_mcp.server"],
    }
    if include_cwd:
        reg["cwd"] = str(repo)
    return reg


def merge_json(
    data: dict, pointer_parts: tuple[str, ...], registration: dict, force: bool
) -> tuple[dict, bool, str]:
    """Merge the cortex ``registration`` at ``pointer_parts`` into parsed JSON ``data``.

    Returns ``(new_data, changed, reason)`` with reason one of ``created``/``replaced``/
    ``replaced-type-confused``/``unchanged``/``skipped-exists``. Missing parent dicts are
    created; every unrelated key is preserved; an existing differing entry is only replaced
    when ``force``. A non-dict value already sitting on the pointer path is never destroyed:
    without ``force`` the merge refuses (``skipped-exists``; the plan layer reports the loud
    reason), with ``force`` it is replaced and reported as ``replaced-type-confused``.
    """
    new_data = copy.deepcopy(data) if data else {}
    if not isinstance(new_data, dict):
        return data, False, "skipped-exists"  # unsupported root shape: never clobber
    node = new_data
    type_confused = False
    for part in pointer_parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            if child is not None:
                if not force:
                    # pre-existing non-dict data on the pointer path: never clobber silently
                    return new_data, False, "skipped-exists"
                type_confused = True
            child = {}
            node[part] = child
        node = child
    leaf = pointer_parts[-1]
    existing = node.get(leaf)
    if existing == registration:
        return new_data, False, "unchanged"
    if existing is not None and not force:
        return new_data, False, "skipped-exists"
    node[leaf] = copy.deepcopy(registration)
    if type_confused:
        return new_data, True, "replaced-type-confused"
    return new_data, True, ("replaced" if existing is not None else "created")


def write_with_backup(path: Path, new_bytes: bytes) -> Path | None:
    """Back up an existing ``path`` to ``<path>.cortex-backup-<YYYYmmdd-HHMMSS>``, then write.

    Collision-proof: an existing backup is NEVER overwritten. When the stamped name is
    already taken (e.g. a second write within the same second), the stamp is re-taken at
    microsecond precision and, if even that name is taken, ``_2``/``_3``… suffixes are
    appended until a free name is found — so every generation of the file (original bytes
    included) stays recoverable.

    Returns the backup path, or None when the file did not exist yet (no backup needed).
    """
    backup: Path | None = None
    if path.exists():
        now = datetime.now().astimezone()
        stamp = now.strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.cortex-backup-{stamp}")
        if backup.exists():
            # same-second collision: re-stamp with microsecond precision
            stamp = now.strftime("%Y%m%d-%H%M%S%f")
            backup = path.with_name(f"{path.name}.cortex-backup-{stamp}")
        n = 2
        while backup.exists():
            # still taken: numbered suffix until free (never overwrite a backup)
            backup = path.with_name(f"{path.name}.cortex-backup-{stamp}_{n}")
            n += 1
        shutil.copy2(path, backup)
    path.write_bytes(new_bytes)
    return backup


def _toml_block(registration: dict) -> str:
    """Stdlib-only TOML text for the cortex section (command string + args array)."""
    args_items = ", ".join(json.dumps(a) for a in registration.get("args", []))
    return (
        "\n[mcp_servers.cortex]\n"
        f"command = {json.dumps(registration['command'])}\n"
        f"args = [{args_items}]\n"
    )


def toml_plan(
    path: Path, registration: dict, force: bool
) -> tuple[str, bytes | None, str]:
    """Plan a TOML registration (pure read; no writes).

    Returns ``(action, new_bytes_or_None, detail)`` with action one of
    ``register``/``skip-exists``/``error``. An existing ``[mcp_servers.cortex]`` section
    is left alone even under ``force`` (reported, never rewritten).
    """
    raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return "error", None, f"existing config does not parse as TOML: {exc}"
    section = data.get("mcp_servers", {})
    if isinstance(section, dict) and "cortex" in section:
        return (
            "skip-exists",
            None,
            "existing [mcp_servers.cortex] left untouched (--force does not rewrite TOML)",
        )
    tail = raw if raw.endswith(b"\n") or not raw else raw + b"\n"
    new_bytes = tail + _toml_block(registration).encode("utf-8")
    try:
        check = tomllib.loads(new_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return "error", None, f"planned append does not parse as TOML: {exc}"
    if "cortex" not in check.get("mcp_servers", {}):
        return "error", None, "planned append did not produce [mcp_servers.cortex]"
    return "register", new_bytes, "would append [mcp_servers.cortex]"


def toml_register(path: Path, registration: dict, force: bool) -> tuple[str, Path | None]:
    """Append ``[mcp_servers.cortex]`` when absent (backup first); existing sections stay.

    Returns ``(status, backup_path_or_None)`` with status ``registered``/``skipped-exists``.
    """
    action, new_bytes, detail = toml_plan(path, registration, force)
    if action != "register":
        print(f"  toml: {action} — {detail}")
        return ("skipped-exists" if action == "skip-exists" else "error"), None
    backup = write_with_backup(path, new_bytes)
    return "registered", backup


# ---------------------------------------------------------------------------
# Server probe (stdio JSON-RPC over the real server module).
# ---------------------------------------------------------------------------


def probe_server(python_exe: Path | str, cwd: Path, timeout: float = 30.0) -> tuple[bool, str]:
    """Start the MCP server over stdio and verify the exposed tool surface.

    Sends initialize + initialized + tools/list; requires exactly
    ``EXPECTED_TOOLS`` and zero ``anyOf``/``$ref`` tokens in the payload.
    Returns ``(ok, detail)``; the child process is always killed.
    """
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "cortex-mcp-probe", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    stdin_payload = "\n".join(json.dumps(m) for m in messages) + "\n"
    proc = subprocess.Popen(
        [str(python_exe), "-m", SERVER_MODULE],
        cwd=str(cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        try:
            out, _ = proc.communicate(input=stdin_payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return False, f"probe timed out after {timeout:.0f}s"
    finally:
        if proc.poll() is None:
            proc.kill()
    payload = out or ""
    init_ok = False
    tools: list[str] | None = None
    for line in payload.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("id") == 1 and "result" in msg:
            init_ok = True
        if msg.get("id") == 2 and isinstance(msg.get("result"), dict):
            tools = [
                t.get("name", "") for t in msg["result"].get("tools", []) if isinstance(t, dict)
            ]
    if not init_ok:
        return False, "no initialize response from server"
    if tools is None:
        return False, "no tools/list response from server"
    if set(tools) != EXPECTED_TOOLS:
        return False, f"tool surface mismatch: got {sorted(tools)}"
    for banned in ("anyOf", "$ref"):
        if banned in payload:
            return False, f"forbidden schema token {banned!r} present in tools payload"
    return True, "OK: exactly 5 tools, 0 anyOf/$ref"


# ---------------------------------------------------------------------------
# Provisioning helpers (subprocess; monkeypatched in unit tests).
# ---------------------------------------------------------------------------


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _ensure_venv(venv_dir: Path, venv_python: Path) -> None:
    if venv_python.exists():
        return
    print(f"  creating venv at {venv_dir}")
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
    )
    if not venv_python.exists():
        raise RuntimeError(f"venv creation did not produce {venv_python}")


def _pip_install(python_exe: Path | str, repo: Path, deps: bool) -> None:
    """``pip install -e <repo>`` (full deps) or ``--no-deps`` (console-script refresh)."""
    try:
        subprocess.run(
            [str(python_exe), "-m", "pip", "--version"],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        if Path(sys.executable).resolve() != Path(python_exe).resolve():
            print("  pip missing in venv; running ensurepip")
            subprocess.run(
                [str(python_exe), "-m", "ensurepip"], check=True, capture_output=True
            )
        else:
            raise RuntimeError(f"pip unavailable on {python_exe}: {exc}") from exc
    cmd = [str(python_exe), "-m", "pip", "install", "-e", str(repo)]
    if not deps:
        cmd.append("--no-deps")
    cmd.append("--quiet")
    print(f"  $ {' '.join(cmd[:-1])}")
    subprocess.run(cmd, check=True, capture_output=True)


def _package_version(python_exe: Path | str) -> str:
    result = subprocess.run(
        [str(python_exe), "-c", "import computer_use_mcp; print(computer_use_mcp.__version__)"],
        capture_output=True,
        text=True,
        check=False,  # returncode inspected below
    )
    if result.returncode != 0:
        raise RuntimeError(f"import computer_use_mcp failed: {result.stderr.strip()}")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Planning (pure) + application of per-agent registrations.
# ---------------------------------------------------------------------------


def _parent_type_conflict(data: dict, pointer_parts: tuple[str, ...]) -> bool:
    """True when an intermediate node on ``pointer_parts`` exists and is not a dict."""
    node = data
    for part in pointer_parts[:-1]:
        child = node.get(part) if isinstance(node, dict) else None
        if child is None:
            return False
        if not isinstance(child, dict):
            return True
        node = child
    return False


def plan_json_target(
    path: Path, pointer: tuple[str, ...], registration: dict, force: bool
) -> tuple[str, bytes | None, str]:
    """Plan a JSON registration. Action: register | replace | unchanged | skip-exists | error."""
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return "error", None, f"existing config does not parse as JSON: {exc}"
    if not isinstance(data, dict):
        return "error", None, "existing config root is not a JSON object"
    new_data, changed, reason = merge_json(data, pointer, registration, force)
    if not changed:
        if reason == "skipped-exists" and not force and _parent_type_conflict(data, pointer):
            return "error", None, "parent node has non-dict type; use --force to replace"
        detail = {
            "unchanged": "cortex entry already matches the target",
            "skipped-exists": "cortex entry exists and differs; use --force to replace",
        }[reason]
        return reason, None, detail
    new_bytes = (json.dumps(new_data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if reason == "replaced-type-confused":
        return reason, new_bytes, "replaced-type-confused: parent node was not a dict (--force)"
    return reason, new_bytes, f"cortex entry {'created' if reason == 'created' else 'replaced (--force)'}"


_ACTION_STATUS = {
    "register": REGISTERED,
    "created": REGISTERED,
    "replace": REGISTERED,
    "replaced": REGISTERED,
    "replaced-type-confused": REGISTERED,
    "unchanged": UNCHANGED,
    "skip-exists": SKIPPED_EXISTS,
    "skipped-exists": SKIPPED_EXISTS,
    "not-found": SKIPPED_NOT_FOUND,
    "error": "ERROR",
}


def select_agents(names: str) -> tuple[AgentSpec, ...]:
    """Comma list from KNOWN_AGENTS names, or ``all``."""
    wanted = [n.strip() for n in names.split(",") if n.strip()]
    if wanted == ["all"]:
        return KNOWN_AGENTS
    known = {spec.name for spec in KNOWN_AGENTS}
    unknown = [n for n in wanted if n not in known]
    if unknown:
        raise SystemExit(
            f"unknown agent(s): {', '.join(unknown)}; known: {', '.join(s.name for s in KNOWN_AGENTS)}"
        )
    return tuple(spec for spec in KNOWN_AGENTS if spec.name in wanted)


def register_agent(
    spec: AgentSpec, home: Path, registration: dict, force: bool, dry_run: bool
) -> list[tuple[str, str]]:
    """Register one agent; returns [(label, status)] plus printed detail. Never clobbers."""
    results: list[tuple[str, str]] = []
    candidates = spec.paths(home)
    found = [p for p in candidates if p.exists()]
    if not found:
        names = ", ".join(str(p) for p in candidates)
        print(f"[{spec.name}] {SKIPPED_NOT_FOUND} (no recognizable config: {names})")
        return [(SKIPPED_NOT_FOUND, "")]
    for path in found:
        if spec.fmt == "codex-toml":
            action, new_bytes, detail = toml_plan(path, registration, force)
        else:
            action, new_bytes, detail = plan_json_target(path, spec.pointer, registration, force)
        status = _ACTION_STATUS.get(action, "ERROR")
        prefix = "[DRY-RUN] " if dry_run else ""
        if action in ("register", "replace", "created", "replaced", "replaced-type-confused"):
            verb = "would write" if dry_run else "writing"
            print(f"[{spec.name}] {path}")
            print(f"  {prefix}{verb}: {detail}")
            backup: Path | None = None
            if dry_run:
                print(f"  {prefix}would back up to {path}.cortex-backup-<timestamp>")
            else:
                if new_bytes is None:  # defensive: plan said write but produced nothing
                    status = "ERROR"
                else:
                    backup = write_with_backup(path, new_bytes)
            if backup is not None:
                print(f"  backup: {backup}")
        elif action == "unchanged":
            print(f"[{spec.name}] {path}: {UNCHANGED} ({detail})")
        elif action == "skip-exists":
            print(f"[{spec.name}] {path}: {SKIPPED_EXISTS} ({detail})")
        else:
            print(f"[{spec.name}] {path}: ERROR ({detail})")
        results.append((str(path), status))
    return results


# ---------------------------------------------------------------------------
# Subcommands.
# ---------------------------------------------------------------------------


def cmd_install(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve() if args.repo else default_repo()
    venv_dir = Path(args.venv).resolve() if args.venv else repo / ".venv"
    venv_python = _venv_python(venv_dir)
    home = Path.home()
    force = bool(args.force)
    dry_run = bool(args.dry_run)

    print(f"cortex-mcp install{' [DRY-RUN]' if dry_run else ''}")
    print(f"  repo: {repo}")
    print(f"  venv: {venv_dir} (python: {venv_python})")

    try:
        specs = select_agents(args.agents)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if dry_run:
        print("[DRY-RUN] would ensure venv (python -m venv) if missing")
        print(f"[DRY-RUN] would run: {venv_python} -m pip install -e {repo} (full deps)")
        print(f"[DRY-RUN] would run: {sys.executable} -m pip install -e {repo} --no-deps")
    else:
        _ensure_venv(venv_dir, venv_python)
        _pip_install(venv_python, repo, deps=True)
        # Refresh the global console script on the system interpreter (cli.py is stdlib-only,
        # so --no-deps is safe and fast). Best-effort: without an elevated terminal this can
        # fail writing into the system Python dir — that must NOT abort the run, so the exit
        # code keeps reflecting only registration + probe results.
        try:
            _pip_install(sys.executable, repo, deps=False)
        except subprocess.CalledProcessError:
            print(
                "  warning: global `cortex-mcp` command was not installed (needs an elevated "
                "terminal) — meanwhile `python -m computer_use_mcp.cli <args>` works from the venv"
            )

    all_results: dict[str, list[tuple[str, str]]] = {}
    for spec in specs:
        reg = build_registration(repo, venv_python, include_cwd=(spec.fmt == "zcode-json"))
        all_results[spec.name] = register_agent(spec, home, reg, force, dry_run)

    print()
    print("summary:")
    any_error = False
    for spec in specs:
        entries = all_results.get(spec.name) or [(SKIPPED_NOT_FOUND, "")]
        for target, status in entries:
            print(f"  {spec.name:<7} {status:<26} {target}")
            if status in (SKIPPED_NOT_FOUND, "ERROR"):
                any_error = any_error or status == "ERROR"

    if dry_run:
        print("[DRY-RUN] probe skipped (no writes performed)")
        print("Restart each agent to load the new registration.")
        # the plan is the contract: ERROR entries in it mean the real install would fail
        return 1 if any_error else 0

    ok, detail = probe_server(venv_python, repo)
    print(f"probe: {detail}")
    print("Restart each agent to load the new registration.")
    return 0 if ok and not any_error else 1


def cmd_update(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve() if args.repo else default_repo()
    venv_python = _venv_python(repo / ".venv")
    dry_run = bool(args.dry_run)

    print(f"cortex-mcp update{' [DRY-RUN]' if dry_run else ''}")
    print(f"  repo: {repo}")

    if dry_run:
        print(f"[DRY-RUN] would run: git fetch origin (cwd {repo})")
        print("[DRY-RUN] would run: git merge --ff-only origin/main")
        print(f"[DRY-RUN] would run: {venv_python} -m pip install -e {repo} (full deps)")
        print(f"[DRY-RUN] would run: {sys.executable} -m pip install -e {repo} --no-deps")
        print("[DRY-RUN] would verify package version before/after and probe the server")
        return 0

    version_before = _package_version(venv_python)
    print(f"version before: {version_before}")

    def _git(*git_args: str) -> tuple[bool, str]:
        result = subprocess.run(
            ["git", *git_args], cwd=str(repo), capture_output=True, text=True, check=False
        )
        return result.returncode == 0, (result.stdout + result.stderr).strip()

    ok, output = _git("fetch", "origin")
    if not ok:
        print(f"git fetch origin failed:\n{output}")
        return 2
    ok, output = _git("merge", "--ff-only", "origin/main")
    if not ok:
        print("not fast-forward; resolve manually")
        if output:
            print(output)
        return 2

    _ensure_venv(repo / ".venv", venv_python)
    _pip_install(venv_python, repo, deps=True)
    _pip_install(sys.executable, repo, deps=False)

    version_after = _package_version(venv_python)
    print(f"version after:  {version_after}")

    ok, detail = probe_server(venv_python, repo)
    print(f"probe: {detail}")
    print("Restart each agent to load the new registration.")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cortex-mcp",
        description=(
            "Install or update the Cortex MCP server registration across recognized AI agent configs."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser(
        "install",
        help="register the Cortex MCP into found agent configs",
        epilog=(
            "Exit code: 0 on success; 1 when any registration reports an ERROR entry or the "
            "probe fails. With --dry-run the plan is still checked: any ERROR entry in the "
            "plan (e.g. an unparseable config or a non-dict parent node) exits 1. The global "
            "`cortex-mcp` console-script refresh is best-effort — without an elevated "
            "terminal it only prints a warning and never changes the exit code."
        ),
    )
    p_install.add_argument("--repo", default=None, help="repo root (default: this package's repo)")
    p_install.add_argument("--venv", default=None, help="venv dir (default: <repo>/.venv)")
    p_install.add_argument(
        "--agents",
        default="all",
        help=f"comma list of agents or 'all' (default: all; known: {', '.join(s.name for s in KNOWN_AGENTS)})",
    )
    p_install.add_argument(
        "--force",
        action="store_true",
        help="replace existing differing cortex entries (default: skip and report)",
    )
    p_install.add_argument(
        "--dry-run", action="store_true", help="print the plan; perform zero writes"
    )
    p_install.set_defaults(func=cmd_install)

    p_update = sub.add_parser("update", help="fast-forward the repo and refresh the install")
    p_update.add_argument("--repo", default=None, help="repo root (default: this package's repo)")
    p_update.add_argument(
        "--dry-run", action="store_true", help="print the plan; run nothing"
    )
    p_update.set_defaults(func=cmd_update)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
