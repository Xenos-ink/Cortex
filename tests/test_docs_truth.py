"""Docs truth tests (WP10b): the user-facing docs must state what the code does.

Every pinned value is READ FROM CODE AT TEST TIME — imported constants, pydantic
field metadata, the ``Limits.validate`` source, a grep of the sources for the
environment variables the code actually reads, and the live ``mcp.list_tools()``
surface — then asserted against the docs (README.md, docs/*.md, docs/history/).
Truth, not substring presence: each test FAILS when the code value changes or
the documented sentence is removed.

Files under test: README.md, docs/TOOLS.md, docs/REPLY-CONTRACT.md,
docs/CONFIGURATION.md, docs/ARCHITECTURE.md, docs/SAFETY.md, docs/INSTALL.md,
docs/history/*.md. (skills/cortex-skill/ has its own truth suite;
test_skill_truth.py.)
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import dataclasses
import inspect
import re
from pathlib import Path

import pytest

from computer_use_mcp import __version__
from computer_use_mcp.cli import EXPECTED_TOOLS, build_parser
from computer_use_mcp.core import limits as limits_mod
from computer_use_mcp.core.limits import Limits
from computer_use_mcp.core.models import (
    MAX_DURATION_MS,
    MAX_FOLLOW_UPS,
    MAX_HOLD_MS,
    MAX_PLAN_SETTLE_MS,
    RESULT_TOKENS,
    OK_TRUE_RESULTS,
    ActionSpec,
    Point,
    ok_from_result,
)
from computer_use_mcp.desktop import interference
from computer_use_mcp.planning.plan import (
    MAX_PLAN_ACTIONS_PER_POINT,
    MAX_PLAN_POINTS,
    MAX_PLAN_TIMEOUT_MS,
    PLAN_FAILURE_REASONS,
    TERMINAL_PLAN_RESULTS,
)
from computer_use_mcp.server import app as server_app
from computer_use_mcp.server.lifecycle import (
    SESSION_IDLE_TIMEOUT_DEFAULT_SECONDS,
    SESSION_IDLE_TIMEOUT_MAX_SECONDS,
    SESSION_IDLE_TIMEOUT_MIN_SECONDS,
)
from computer_use_mcp.server.replies import RESULT_IMAGE_MAX_KB_DEFAULT
from computer_use_mcp.server.wait_machine import (
    MAX_WAITS_GLOBAL,
    MAX_WAITS_PER_SESSION,
    WAIT_HARD_MAX_SECONDS,
    WAIT_SEGMENT_DEFAULT_SECONDS,
    WAIT_SEGMENT_MAX_SECONDS,
    WAIT_SEGMENT_MIN_SECONDS,
    WAIT_TOMBSTONES_PER_SESSION,
)
from computer_use_mcp.vision.evidence import (
    EVIDENCE_MAX_AGE_SECONDS,
    EVIDENCE_MAX_BYTES,
    EVIDENCE_MAX_FRAMES,
)

ROOT = Path(__file__).resolve().parents[1]

CURRENT_TRUTH_DOCS = [
    ROOT / "README.md",
    ROOT / "docs" / "INSTALL.md",
    ROOT / "docs" / "TOOLS.md",
    ROOT / "docs" / "REPLY-CONTRACT.md",
    ROOT / "docs" / "CONFIGURATION.md",
    ROOT / "docs" / "ARCHITECTURE.md",
    ROOT / "docs" / "SAFETY.md",
]

HISTORY_DOCS = sorted((ROOT / "docs" / "history").glob("*.md"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _norm(text: str) -> str:
    return " ".join(text.split())


def _section(text: str, heading: str) -> str:
    """The text under a `##`/`###` heading up to the next heading of any level."""
    match = re.search(rf"^#+\s*{re.escape(heading)}\s*$", text, re.M)
    assert match is not None, f"docs section not found: {heading}"
    rest = text[match.end():]
    nxt = re.search(r"^#+\s+", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


# --- the tool surface ------------------------------------------------------------------------


def _live_tool_names() -> set[str]:
    return {tool.name for tool in asyncio.run(server_app.mcp.list_tools())}


def _table_tool_names(text: str) -> set[str]:
    """First-column code spans of the pipe-table rows (the tool tables)."""
    return set(
        re.findall(r"^\| `([a-z][a-z_0-9]+)` \s*\|", text, re.M)
    ) | set(re.findall(r"^\| `([a-z][a-z_0-9]+)`\s+\|", text, re.M))


def test_the_served_surface_is_exactly_the_documented_ten_tools() -> None:
    live = _live_tool_names()
    assert live == EXPECTED_TOOLS  # the probe constant and the served surface agree
    for doc, heading in (
        (ROOT / "README.md", "The ten tools at a glance"),
        (ROOT / "docs" / "TOOLS.md", "The ten tools"),
    ):
        text = _read(doc)
        assert _table_tool_names(_section(text, heading)) == EXPECTED_TOOLS, doc
        for tool in EXPECTED_TOOLS:
            assert f"`{tool}`" in text, (doc, tool)


def test_no_extra_tool_is_named_in_the_tool_reference() -> None:
    tools_doc = _read(ROOT / "docs" / "TOOLS.md")
    spans = set(re.findall(r"`([a-z][a-z_0-9]+)`", tools_doc))
    extras = {
        span
        for span in spans
        if span.startswith("computer_") and span not in EXPECTED_TOOLS
        and span != "computer_use_mcp"
    }
    assert not extras, f"docs/TOOLS.md names tools the server does not serve: {extras}"


# --- the reply contract ----------------------------------------------------------------------


def test_the_reply_contract_documents_the_closed_result_token_set() -> None:
    text = _read(ROOT / "docs" / "REPLY-CONTRACT.md")
    table = _section(text, "The `result` token set (closed)")
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", table, re.M))
    assert documented == set(RESULT_TOKENS)
    # The ok column and the ok-rule sentence, both derived from the code rule:
    ok_rows = dict(re.findall(r"^\| `([a-z_]+)` \| [^|]+\| (true|false) \|", table, re.M))
    assert set(ok_rows) == set(RESULT_TOKENS)
    for token, cell in ok_rows.items():
        assert cell == ("true" if ok_from_result(token) else "false"), token
    stated = set(
        re.findall(
            r"`ok` is True iff `result` is `(\w+)` or `(\w+)`", text, re.M
        ).pop()
    )
    assert stated == set(OK_TRUE_RESULTS)
    assert ok_from_result(sorted(OK_TRUE_RESULTS)[0]) is True
    assert ok_from_result("dry_run") is False


def test_the_failure_reason_vocabulary_is_documented_closed() -> None:
    text = _read(ROOT / "docs" / "REPLY-CONTRACT.md")
    table = _section(text, "The `failure_reason` vocabulary (closed)")
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", table, re.M))
    assert documented == set(PLAN_FAILURE_REASONS)


def test_the_per_surface_result_subsets_are_consistent_with_the_code() -> None:
    text = _read(ROOT / "docs" / "REPLY-CONTRACT.md")
    start = text.find("Per-surface subsets:")
    end = text.find("## Terminal plan honesty")
    assert start != -1 and end != -1 and start < end
    body = text[start:end]
    bullets = re.findall(r"^- (.*)$", body, re.M)
    assert bullets, "per-surface subset bullets not found"
    flat = next(b for b in bullets if b.startswith("The flat execute"))
    terminal = next(b for b in bullets if b.startswith("The terminal plan"))
    terminal_tokens = set(re.findall(r"`(\w+)`", terminal)) & set(RESULT_TOKENS)
    assert terminal_tokens == set(TERMINAL_PLAN_RESULTS)
    flat_tokens = set(re.findall(r"`(\w+)`", flat)) & set(RESULT_TOKENS)
    assert "unknown" not in flat_tokens  # the terminal aggregate never rides a flat step
    for bullet in bullets:
        tokens = set(re.findall(r"`(\w+)`", bullet)) & set(RESULT_TOKENS)
        # the read-only observe bullet legitimately names NO result token
        if not bullet.startswith("`computer_observe`"):
            assert tokens, f"subset bullet names no result token: {bullet[:60]}"
    stray = {
        token
        for bullet in bullets
        for token in re.findall(r"`(\w+)`", bullet)
        if token not in RESULT_TOKENS
        and token != "result"
        and not token.startswith("computer_")
    }
    assert not stray, f"per-surface bullets name tokens outside the closed set: {stray}"


def test_the_readme_token_list_and_the_vocabulary_size_come_from_the_code() -> None:
    readme = _read(ROOT / "README.md")
    for token in RESULT_TOKENS:
        assert f"`{token}`" in readme, token
    contract = _read(ROOT / "docs" / "REPLY-CONTRACT.md")
    assert f"{len(PLAN_FAILURE_REASONS)}-token" in contract


def test_the_resume_reply_keys_and_flow_are_taught_from_code() -> None:
    lifecycle_src = _read(
        ROOT / "src" / "computer_use_mcp" / "server" / "lifecycle.py"
    )
    tools_doc = _read(ROOT / "docs" / "TOOLS.md")
    readme = _read(ROOT / "README.md")
    for key in ("resumed", "continuation_of", "resumed_from_checkpoint"):
        assert f'"{key}"' in lifecycle_src, key  # the reply really carries it
        assert f"`{key}" in tools_doc, key
        assert f"`{key}" in readme, key
    assert "checkpoint.json" in tools_doc


def test_the_stop_path_checkpoint_is_runtime_guarded_and_docs_do_not_overclaim() -> None:
    """F16: a FRESH session's stop writes NO checkpoint — the write is guarded by
    the long-running runtime (only a resumed session carries one). The docs must
    state that scope and never promise a fresh stop-then-resume flow."""
    lifecycle_src = _read(
        ROOT / "src" / "computer_use_mcp" / "server" / "lifecycle.py"
    )
    assert "isinstance(runtime, LongRunningRuntime)" in lifecycle_src
    assert "CheckpointTrigger.BEFORE_SESSION_END" in lifecycle_src
    overclaim_patterns = [
        r"stop[_ ]?session[^.\n]{0,80}writes?\s+(?:a|an|the)\s+[^.\n]{0,60}checkpoint",
        r"stop[_ ]?session[^.\n]{0,80}writes?\s+(?:a\s+)?`?before[_ -]?session[_ -]?end`?",
        r"stopped\s+session\s+can\s+be\s+resumed",
    ]
    for path in (
        ROOT / "README.md",
        ROOT / "docs" / "TOOLS.md",
        ROOT / "docs" / "ARCHITECTURE.md",
    ):
        text = _norm(_read(path))
        for pattern in overclaim_patterns:
            assert re.search(pattern, text, re.I) is None, (path, pattern)
    tools = _norm(_read(ROOT / "docs" / "TOOLS.md"))
    arch = _norm(_read(ROOT / "docs" / "ARCHITECTURE.md"))
    for honest in (tools, arch):
        assert re.search(
            r"only (?:by )?a session that already carries the long-running runtime",
            honest,
            re.I,
        ), "the honest runtime-guard scope is not stated"
    assert "NO checkpoint file" in tools and "NO checkpoint file" in arch


# --- the CLI surface (docs/INSTALL.md usage lines generated from cli.py's parser) --------------


def _cli_subcommand_options() -> dict[str, set[str]]:
    parser = build_parser()
    sub_action = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return {
        name: {
            opt
            for action in sub._actions
            for opt in action.option_strings
            if opt not in ("-h", "--help")
        }
        for name, sub in sub_action.choices.items()
    }


def test_install_md_usage_lines_match_the_argparse_parser_exactly() -> None:
    text = _read(ROOT / "docs" / "INSTALL.md")
    cli_section = _section(text, "The `cortex-mcp` CLI")
    fence = re.search(r"```bash\n(.*?)```", cli_section, re.S)
    assert fence is not None, "no usage fence in the CLI section"
    documented: dict[str, set[str]] = {}
    for line in fence.group(1).splitlines():
        match = re.match(r"cortex-mcp\s+(\S+)\s+(.*)$", line.strip())
        if not match:
            continue
        documented[match.group(1)] = set(re.findall(r"\[(--?[a-z-]+)", match.group(2)))
    actual = _cli_subcommand_options()
    assert set(documented) == set(actual), (set(documented), set(actual))
    for sub, flags in documented.items():
        assert flags == actual[sub], (
            f"{sub}: doc {sorted(flags)} vs parser {sorted(actual[sub])}"
        )


# --- limits (docs/CONFIGURATION.md limits table re-derived from the code) ---------------------


def _limits_bands_from_validate_source() -> dict[str, tuple[float, float]]:
    source = inspect.getsource(Limits.validate)
    bands: dict[str, tuple[float, float]] = {}
    for match in re.finditer(
        r"_clamp\(\s*self\.(\w+),\s*([^,]+?),\s*([^)]+?)\)", source
    ):
        field, lo, hi = match.group(1), match.group(2), match.group(3)
        bands[field] = (float(ast.literal_eval(lo)), float(ast.literal_eval(hi)))
    return bands


def test_the_limits_table_matches_the_limits_code() -> None:
    text = _read(ROOT / "docs" / "CONFIGURATION.md")
    table = _section(text, "Limits (`start_session(limits={...})`)")
    rows = {
        match.group(1): (
            float(match.group(2)),
            float(match.group(3)),
            float(match.group(4)),
        )
        for match in re.finditer(
            r"^\| `(\w+)` \| ([0-9.]+) \| ([0-9.]+)\.\.([0-9.]+) \|", table, re.M
        )
    }
    fields = {field.name for field in dataclasses.fields(Limits)}
    assert fields, "Limits lost its fields?"
    bands = _limits_bands_from_validate_source()
    assert set(bands) == fields  # every field is clamped -> every field needs a row
    assert set(rows) == fields, f"limits rows drifted: {rows.keys()} vs {fields}"
    defaults = {field.name: field.default for field in dataclasses.fields(Limits)}
    for name, (doc_default, doc_lo, doc_hi) in rows.items():
        assert doc_default == float(defaults[name]), name
        assert (doc_lo, doc_hi) == bands[name], name


# --- environment variables -------------------------------------------------------------------


def _env_names_read_by_the_code() -> set[str]:
    """Every env var name the code reads: os.environ/os.getenv call args, the
    ``*_ENV`` module constants, and the inline name literals passed to the
    ``_env_*`` knob helpers (desktop/backend.py)."""
    names: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for arg in re.findall(
            r"os\.(?:environ\.get|getenv)\(\s*(['\"]?[A-Za-z_][\w]*['\"]?)", source
        ):
            if arg.startswith(("'", '"')):
                names.add(ast.literal_eval(arg))
            else:
                assigned = re.search(
                    rf"^{arg}\s*(?::[^=]+)?=\s*[rbu]?['\"]([A-Z0-9_]+)['\"]",
                    source,
                    re.M,
                )
                if assigned:
                    names.add(assigned.group(1))
        for arg in re.findall(
            r"^[A-Z0-9_]+_ENV\s*(?::[^=]+)?=\s*['\"]([A-Z0-9_]+)['\"]",
            source,
            re.M,
        ):
            names.add(arg)
        for literal in re.findall(r"['\"]([A-Z][A-Z0-9_]{3,})['\"]", source):
            if literal.startswith(("CORTEX_", "VISION_", "COMPUTER_USE_MCP_")):
                names.add(literal)
        if 'os.getenv("LOG_LEVEL"' in source:
            names.add("LOG_LEVEL")
    return names


def test_every_env_var_the_code_reads_is_documented_and_none_is_invented() -> None:
    code_names = _env_names_read_by_the_code()
    assert code_names, "the env-name scan found nothing (scanner broken?)"
    doc = _read(ROOT / "docs" / "CONFIGURATION.md")
    spans = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})`", doc))
    documented = {
        span
        for span in spans
        if span.startswith(("CORTEX_", "VISION_", "COMPUTER_USE_MCP_"))
        or span in {"LOG_LEVEL", "OPENAI_API_KEY", "PYTHONPATH", "LOCALAPPDATA"}
    }
    missing = code_names - documented
    assert not missing, f"env vars the code reads but CONFIGURATION.md omits: {sorted(missing)}"
    invented = documented - code_names
    assert not invented, f"CONFIGURATION.md documents env vars the code never reads: {sorted(invented)}"


# --- versions ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", [ROOT / "README.md", ROOT / "docs" / "ARCHITECTURE.md", ROOT / "docs" / "SAFETY.md"]
)
def test_version_statements_in_current_docs_match_the_package_version(path: Path) -> None:
    text = _read(path)
    tokens = set(re.findall(r"(?<![\w.\-])v?0\.7\.\d+(?![\w])", text))
    assert tokens == {__version__}, f"{path} states versions {tokens} != {__version__}"


# --- links ------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [*CURRENT_TRUTH_DOCS, *HISTORY_DOCS])
def test_relative_markdown_links_resolve(path: Path) -> None:
    for target in re.findall(r"\]\(([^)#\s]+)\)", _read(path)):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        resolved = (path.parent / target).resolve()
        assert resolved.exists(), f"{path} links to missing file: {target}"


def test_every_history_file_is_linked_from_current_truth() -> None:
    assert HISTORY_DOCS, "docs/history/ is empty"
    linked = "\n".join(_read(path) for path in CURRENT_TRUTH_DOCS)
    for path in HISTORY_DOCS:
        assert path.name in linked, f"no current doc links {path.name}"


# --- negative pins ----------------------------------------------------------------------------


_BANNED_FALSEHOOD_PATTERNS = [
    r"never\s+launches\s+by\s+default",
    r"(five|six)\s+MCP\s+tools",
    r"exactly\s+(five|six)\s+tools",
    r"serves\s+exactly\s+(five|six)\b",
]


@pytest.mark.parametrize("path", CURRENT_TRUTH_DOCS)
def test_current_truth_docs_carry_no_superseded_falsehoods(path: Path) -> None:
    text = _norm(_read(path)).lower()
    for pattern in _BANNED_FALSEHOOD_PATTERNS:
        assert re.search(pattern, text) is None, (
            f"{path} carries the superseded falsehood /{pattern}/"
        )


@pytest.mark.parametrize(
    "name", ["README.md", "SAFETY.md", "TOOLS.md"]
)
def test_the_launch_truth_is_stated_without_the_allowlist_overclaim(name: str) -> None:
    path = ROOT / name if name == "README.md" else ROOT / "docs" / name
    text = _norm(_read(path))
    assert 'launch="server"' in text, path
    assert "NO process allowlist" in text, path
    assert "any resolvable target may" in text, path


def test_the_launch_policy_default_is_server_in_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(interference.ATTACH_OR_LAUNCH_ENV, raising=False)
    assert interference._default_launch_policy() == "server"
    policy = interference.AttachOrLaunchPolicy()
    assert policy.launch == "server"


def test_the_launch_default_statements_are_pinned_in_architecture_and_configuration() -> None:
    policy = interference.AttachOrLaunchPolicy()
    assert policy.launch == "server"
    arch = _norm(_read(ROOT / "docs" / "ARCHITECTURE.md"))
    assert f'DEFAULT launch policy is `"{policy.launch}"`' in arch
    cfg = _read(ROOT / "docs" / "CONFIGURATION.md")
    assert (
        f"| `{interference.ATTACH_OR_LAUNCH_ENV}` | `{policy.launch}` | "
        "only `driver` (case-insensitive) restores never-launch"
    ) in cfg


def test_history_files_carry_the_historical_header() -> None:
    for path in (ROOT / "docs" / "history").glob("*.md"):
        assert _read(path).startswith(
            "# HISTORICAL — describes"
        ), f"{path} is missing the HISTORICAL header"


# --- bounds taught in docs/TOOLS.md and docs/CONFIGURATION.md, derived from the code ----------


def _field_bounds(model: type, name: str) -> tuple[float, float]:
    ge = le = None
    for constraint in model.model_fields[name].metadata:
        ge = getattr(constraint, "ge", ge)
        le = getattr(constraint, "le", le)
    assert ge is not None and le is not None, (model.__name__, name)
    return int(ge), int(le)


def _field_max_length(model: type, name: str) -> int:
    for constraint in model.model_fields[name].metadata:
        value = getattr(constraint, "max_length", None)
        if value is not None:
            return int(value)
    raise AssertionError((model.__name__, name))


def test_tools_md_states_the_action_bounds_derived_from_the_models() -> None:
    doc = _norm(_read(ROOT / "docs" / "TOOLS.md"))
    lo, hi = _field_bounds(Point, "x")
    assert f"`{lo}..{hi}`" in doc, "coordinate bounds"
    lo, hi = _field_bounds(ActionSpec, "delta")
    assert f"band `{lo}..{hi}`" in doc, "delta band"
    assert f"`text` ≤ {_field_max_length(ActionSpec, 'text')} chars" in doc
    assert f"`expected_effect` ≤ {_field_max_length(ActionSpec, 'expected_effect')} chars" in doc
    assert f"`target` ≤ {_field_max_length(ActionSpec, 'target')} chars" in doc
    assert f"`keys` ≤ {_field_max_length(ActionSpec, 'keys')} names" in doc
    assert f"`hold_ms` 0..{MAX_HOLD_MS}" in doc
    assert f"`duration_ms` 0..{MAX_DURATION_MS}" in doc
    assert f"`settle_ms` 0..{MAX_PLAN_SETTLE_MS}" in doc
    assert f"(≤ {MAX_FOLLOW_UPS} items)" in doc
    assert f"at most {MAX_PLAN_POINTS} points" in doc
    assert f"at most {MAX_PLAN_ACTIONS_PER_POINT} actions per point" in doc
    assert f"`timeout_ms` 0..{MAX_PLAN_TIMEOUT_MS}" in doc


def test_tools_md_states_the_wait_and_evidence_bounds_derived_from_the_code() -> None:
    doc = _norm(_read(ROOT / "docs" / "TOOLS.md"))
    assert (
        f"default {int(WAIT_SEGMENT_DEFAULT_SECONDS)} s, "
        f"band {int(WAIT_SEGMENT_MIN_SECONDS)}..{int(WAIT_SEGMENT_MAX_SECONDS)}"
    ) in doc
    assert f"min(limits.max_wait_seconds, {int(WAIT_HARD_MAX_SECONDS)})" in doc
    assert f"{MAX_WAITS_PER_SESSION} waits per session, {MAX_WAITS_GLOBAL} process-wide" in doc
    assert f"the last {WAIT_TOMBSTONES_PER_SESSION} finished waits" in doc
    assert (
        f"{EVIDENCE_MAX_FRAMES} frames / {EVIDENCE_MAX_BYTES // (1024 * 1024)} MB"
        f" / {int(EVIDENCE_MAX_AGE_SECONDS)} s"
    ) in doc
    assert f"{int(SESSION_IDLE_TIMEOUT_DEFAULT_SECONDS // 60)} minutes" in doc
    assert (
        f"band {int(SESSION_IDLE_TIMEOUT_MIN_SECONDS)}..{int(SESSION_IDLE_TIMEOUT_MAX_SECONDS)}"
    ) in doc


def test_configuration_md_states_the_wait_and_idle_knob_rows_from_the_code() -> None:
    doc = _read(ROOT / "docs" / "CONFIGURATION.md")
    assert (
        f"| `CORTEX_WAIT_SEGMENT_SECONDS` | {int(WAIT_SEGMENT_DEFAULT_SECONDS)} | "
        f"accepted {int(WAIT_SEGMENT_MIN_SECONDS)}..{int(WAIT_SEGMENT_MAX_SECONDS)}"
    ) in doc
    assert (
        f"| `CORTEX_SESSION_IDLE_TIMEOUT_SECONDS` | {int(SESSION_IDLE_TIMEOUT_DEFAULT_SECONDS)} "
        f"(15 min) | accepted {int(SESSION_IDLE_TIMEOUT_MIN_SECONDS)}.."
        f"{int(SESSION_IDLE_TIMEOUT_MAX_SECONDS)}"
    ) in doc


def _env_literal(name_token: str, sources: list[str]) -> str:
    """Resolve an identifier argument to its string/int literal in the sources."""
    for source in sources:
        match = re.search(
            rf"^{name_token}\s*(?::[^=]+)?=\s*['\"]?([^#'\"]+?)['\"]?\s*(?:#.*)?$",
            source,
            re.M,
        )
        if match:
            return match.group(1).strip()
    raise AssertionError(f"cannot resolve env-name token {name_token}")


def _env_knob_specs() -> dict[str, tuple[str, str | None, str | None]]:
    """(env, default, lo, hi) parsed from the _env_* knob helper call sites.

    ``lo``/``hi`` are None for the unbounded helpers (nonnegative float / positive
    int), where the doc band is the ">= 0"-style wording instead.
    """
    files = [
        ROOT / "src" / "computer_use_mcp" / "desktop" / "backend.py",
        ROOT / "src" / "computer_use_mcp" / "desktop" / "input_engine.py",
    ]
    sources = [path.read_text(encoding="utf-8") for path in files]
    resolution_sources = [
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "src").rglob("*.py"))
    ]
    combined = "\n".join(sources)

    def env_name(token: str) -> str:
        return (
            token.strip("\"'")
            if token.startswith(("'", '"'))
            else _env_literal(token, resolution_sources)
        )

    specs: dict[str, tuple[str, str | None, str | None]] = {}
    for name_tok, default, lo, hi in re.findall(
        r"_env_(?:bounded_int|int_in_range|bounded_float)\(\s*([A-Za-z_]+|['\"][A-Z_]+['\"])\s*,\s*(-?[\w.]+)\s*,\s*(-?[\w.]+)\s*,\s*(-?[\w.]+)\s*,?\s*\)",
        combined,
    ):
        if default == "None":
            continue  # a None default is the "unset = engine default" case (PIL level)
        specs[env_name(name_tok)] = (default, lo, hi)
    for name_tok, default in re.findall(
        r"_env_(?:nonnegative_float|positive_int|nonnegative_int)\(\s*([A-Za-z_]+|['\"][A-Z_]+['\"])\s*,\s*(-?[\w.]+)\s*,?\s*\)",
        combined,
    ):
        if not default[0].isdigit() and default not in ("None",):
            default = _env_literal(default, resolution_sources)
        specs.setdefault(env_name(name_tok), (default, None, None))
    return specs


def test_configuration_md_knob_rows_match_the_helper_call_sites() -> None:
    specs = _env_knob_specs()
    assert specs, "the knob-call scan found nothing (scanner broken?)"
    doc = _read(ROOT / "docs" / "CONFIGURATION.md")
    rows = {
        match.group(1): (match.group(2), match.group(3))
        for match in re.finditer(
            r"^\| `([A-Z][A-Z0-9_]+)` \| ([^|]+) \| ([^|]+) \|", doc, re.M
        )
    }
    for env, (default, lo, hi) in specs.items():
        assert env in rows, f"CONFIGURATION.md has no row for {env}"
        default_cell, band_cell = rows[env]

        def _float(text: str) -> float:
            return float(text.replace("_", ""))

        assert any(
            abs(_float(token) - _float(default)) < 1e-9
            for token in re.findall(r"-?\d[\d_]*(?:\.\d+)?", default_cell)
        ), f"{env}: default {default} vs cell {default_cell!r}"
        if lo is not None:
            pair = re.search(r"(-?[\d_.]+)\.\.(-?[\d_.]+)", band_cell)
            assert pair is not None, f"{env}: band cell {band_cell!r} has no lo..hi"
            assert abs(_float(pair.group(1)) - _float(lo)) < 1e-9 and abs(
                _float(pair.group(2)) - _float(hi)
            ) < 1e-9, f"{env}: band {lo}..{hi} vs cell {band_cell!r}"
        else:
            assert "≥" in band_cell, f"{env}: unbounded knob band cell {band_cell!r}"


def test_configuration_md_pins_the_special_case_defaults() -> None:
    doc = _read(ROOT / "docs" / "CONFIGURATION.md")
    assert f"| `CORTEX_RESULT_IMAGE_MAX_KB` | {RESULT_IMAGE_MAX_KB_DEFAULT} |" in doc
    assert "| `CORTEX_VALIDATE_REUSE_MS` | 1500 |" in doc
