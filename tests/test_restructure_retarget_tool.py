"""Unit tests for the WP-R2b retarget tool (DELTA 1 rules R1-R8 + VAL scenarios)."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import sys
from pathlib import Path

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "restructure" / "retarget_refs.py"
REAL_MAP = Path(__file__).resolve().parents[1] / "tools" / "restructure" / "r2b_retarget_map.json"
REL = "tests/synthetic.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("retarget_refs_tool", _TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules["retarget_refs_tool"] = module
    spec.loader.exec_module(module)
    return module


def _real_map():
    tool = _load_tool()
    return tool.load_map(str(REAL_MAP))


def _rt(source: str, overrides_extra: dict | None = None):
    tool = _load_tool()
    names, bindings, overrides = _real_map()
    if overrides_extra:
        overrides = {**overrides, **overrides_extra}
    return tool.retarget_source(source, REL, names, bindings, overrides)


def _imports(new: str) -> list[str]:
    head = new.split("\n\n\n")[0]
    return [line for line in head.splitlines() if line.startswith(("from ", "import "))]


# --- R1: every reference to a readers-name targets readers[0] -------------------------------


def test_r1_read_and_call_go_to_the_primary_reader() -> None:
    source = (
        "from computer_use_mcp import server\n\n\n"
        "def test_x():\n"
        '    server.computer_execute({})\n'          # no readers -> home execute
        '    out = server.computer_wait("s")\n'      # no readers -> home wait
        '    assert 1 not in server._bundles\n'      # readers -> lifecycle
        '    server._registry.get("k") is None\n'    # readers -> lifecycle
    )
    new, _edits, sites = _rt(source)
    assert sites == []
    assert "srv_server_execute.computer_execute({})" in new
    assert 'srv_server_wait.computer_wait("s")' in new
    assert "srv_server_lifecycle._bundles" in new
    assert "srv_server_lifecycle._registry" in new
    assert "srv_state" not in new


def test_r1_single_reader_assignment_targets_the_reader_not_the_home() -> None:
    # VAL B1 scenario: `server._backend_factory = ...` patched nothing when it
    # landed on state (the home); R1 sends it to lifecycle (the only reader).
    source = (
        "from computer_use_mcp import server\n\n\n"
        "def test_x():\n"
        "    server._backend_factory = fake_factory\n"
    )
    new, _edits, sites = _rt(source)
    assert sites == []
    assert "srv_server_lifecycle._backend_factory = fake_factory" in new
    assert "srv_server_state" not in new


def test_r1_read_of_a_rebound_name_targets_the_same_reader() -> None:
    # VAL B2 scenario: a read of `_bundles` must see the patched lifecycle dict.
    source = (
        "from computer_use_mcp import server\n\n\n"
        "def test_x():\n"
        "    assert session_id not in server._bundles\n"
    )
    new, _edits, sites = _rt(source)
    assert sites == []
    assert "srv_server_lifecycle._bundles" in new


def test_r1_name_without_readers_goes_to_home() -> None:
    source = (
        "from computer_use_mcp import server\n\n\n"
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(server, "stop_session", spy)\n'
    )
    new, _edits, sites = _rt(source)
    assert sites == []
    assert 'monkeypatch.setattr(srv_server_lifecycle, "stop_session", spy)' in new
    assert not [e for e in _rt(source)[1] if e["form"] == "d3-extra"]


# --- R2: same-object D3 extras ---------------------------------------------------------------


def test_r2_monkeypatch_extras_reuse_the_installed_object_dict() -> None:
    # VAL B3 scenario: `{}` must be installed ONCE and shared, not evaluated 3x.
    source = (
        "from computer_use_mcp import server\n\n\n"
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(server, "_bundles", {})\n'
    )
    new, edits, sites = _rt(source)
    assert sites == []
    body = new.split("def test_x")[1]
    assert 'monkeypatch.setattr(srv_server_lifecycle, "_bundles", {})' in body
    assert 'monkeypatch.setattr(srv_server_wait_machine, "_bundles", srv_server_lifecycle._bundles)' in body
    assert 'monkeypatch.setattr(srv_server_wait, "_bundles", srv_server_lifecycle._bundles)' in body
    assert sum(1 for e in edits if e["form"] == "d3-extra") == 1


def test_r2_extras_reuse_a_constructed_object_not_its_source_text() -> None:
    # VAL B3 scenario: `server.PlanLeaseGuard()` must not be re-evaluated/copied.
    source = (
        "from computer_use_mcp import server\n\n\n"
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(server, "_plan_guard", server.PlanLeaseGuard())\n'
    )
    new, _edits, sites = _rt(source)
    assert sites == []
    body = new.split("def test_x")[1]
    assert 'monkeypatch.setattr(srv_server_plan_surface, "_plan_guard", srv_server_plan_surface.PlanLeaseGuard())' not in body
    assert 'monkeypatch.setattr(srv_server_plan_surface, "_plan_guard", server.PlanLeaseGuard())' not in body
    assert 'monkeypatch.setattr(srv_server_execute, "_plan_guard", srv_server_plan_surface._plan_guard)' in body
    assert 'monkeypatch.setattr(srv_server_lifecycle, "_plan_guard", srv_server_plan_surface._plan_guard)' in body


def test_r2_builtin_setattr_extras_use_builtin_setattr() -> None:
    source = (
        "from computer_use_mcp import server\n\n\n"
        "def test_x():\n"
        '    setattr(server, "_bundles", {})\n'
    )
    new, _edits, sites = _rt(source)
    assert sites == []
    body = new.split("def test_x")[1]
    assert 'setattr(srv_server_lifecycle, "_bundles", {})' in body
    assert 'setattr(srv_server_wait_machine, "_bundles", srv_server_lifecycle._bundles)' in body
    assert "monkeypatch" not in body


def test_r2_plain_assignment_extras_use_assignment() -> None:
    source = (
        "from computer_use_mcp import server\n\n\n"
        "def test_x():\n"
        "    server._bundles = make_bundles()\n"
    )
    new, _edits, sites = _rt(source)
    assert sites == []
    body = new.split("def test_x")[1]
    assert "srv_server_lifecycle._bundles = make_bundles()" in body
    assert "srv_server_wait_machine._bundles = srv_server_lifecycle._bundles" in body
    assert "srv_server_wait._bundles = srv_server_lifecycle._bundles" in body


# --- R3: rebind/D3 only for the BARE alias ----------------------------------------------------


def test_r3_attribute_patch_on_a_readers_name_gets_no_extra_lines() -> None:
    # VAL B4 scenario: setattr(server._plan_guard, "install", ...) must NOT
    # replace the guard object itself in execute/lifecycle.
    source = (
        "from computer_use_mcp import server\n\n\n"
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(server._plan_guard, "install", wrapped)\n'
    )
    new, edits, sites = _rt(source)
    assert sites == []
    assert "srv_server_plan_surface._plan_guard" in new
    assert not [e for e in edits if e["form"] == "d3-extra"]


# --- R4: unique full-path aliases + collision refusal -----------------------------------------


def test_r4_aliases_are_derived_from_the_full_dotted_path() -> None:
    source = (
        "from computer_use_mcp import server\n\n\n"
        "def test_x():\n"
        "    a = server._bundles\n"            # computer_use_mcp.server.state -> srv_server_lifecycle reader
        "    r = server.SessionRegistry()\n"   # computer_use_mcp.core.state -> srv_core_state
    )
    new, _edits, sites = _rt(source)
    assert sites == []
    assert "srv_server_lifecycle._bundles" in new
    assert "srv_core_state.SessionRegistry()" in new
    assert "import computer_use_mcp.server.lifecycle as srv_server_lifecycle" in new
    assert "import computer_use_mcp.core.state as srv_core_state" in new


def test_r4_generated_alias_collision_is_a_refusal(tmp_path: Path) -> None:
    tool = _load_tool()
    target = tmp_path / "t.py"
    target.write_text(
        "srv_server_lifecycle = 1\n"
        "from computer_use_mcp import server\n\n\n"
        "def test_x():\n"
        "    server._registry.get('k')\n",
        encoding="utf-8")
    before = target.read_bytes()
    rc = tool.main([str(REAL_MAP), str(target)])
    assert rc == 2
    assert target.read_bytes() == before


# --- R5: indentation + output re-parse --------------------------------------------------------


def test_r5_indented_import_inside_a_function_reparses() -> None:
    # VAL B6 scenario, strengthened (VAL-WP-R2b-0-02 MINOR 1): TWO modules in an
    # indented multi-line inserted import (the old unindented behaviour would
    # also pass a single-module pin).
    source = (
        "def make():\n"
        "    from computer_use_mcp import server\n"
        "\n"
        "    server.computer_wait('s')\n"
        "    assert 1 not in server._bundles\n"
        "    return None\n"
    )
    new, _edits, sites = _rt(source)
    assert sites == []
    assert "    import computer_use_mcp.server.wait as srv_server_wait\n" in new
    assert "    import computer_use_mcp.server.lifecycle as srv_server_lifecycle\n" in new
    assert _parse(new) is not None  # R5: the output parses


def _parse(text: str):
    import ast as ast_module

    try:
        return ast_module.parse(text)
    except SyntaxError:
        return None



# --- R6: bytes in / bytes out through the CLI --------------------------------------------------


def test_r6_crlf_survives_through_main_write_mode(tmp_path: Path) -> None:
    tool = _load_tool()
    target = tmp_path / "t.py"
    target.write_bytes(
        b"from computer_use_mcp import server\r\n"
        b"\r\n"
        b"\r\n"
        b"def test_x(monkeypatch):\r\n"
        b'    monkeypatch.setattr(server, "_bundles", {})\r\n')
    rc = tool.main([str(REAL_MAP), str(target)])
    assert rc == 0
    raw = target.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert b'monkeypatch.setattr(srv_server_lifecycle, "_bundles", {})\r\n' in raw
    assert b'monkeypatch.setattr(srv_server_wait_machine, "_bundles", srv_server_lifecycle._bundles)\r\n' in raw


def test_r6_non_ascii_line_is_converted_not_refused(tmp_path: Path) -> None:
    # VAL-WP-R2b-0-02 MINOR 2: the byte->character column conversion on a
    # non-ASCII line is CORRECT (the old test's '©' was an identifier, so it
    # refused on SyntaxError instead of proving the conversion).
    tool = _load_tool()
    target = tmp_path / "t.py"
    target.write_text(
        "from computer_use_mcp import server\n\n\n"
        "def test_x():\n"
        "    r\u00e9sultat = server.computer_wait('s')  # caf\u00e9 \u2014 na\u00efve\n",
        encoding="utf-8")
    rc = tool.main([str(REAL_MAP), str(target)])
    assert rc == 0
    text = target.read_text(encoding="utf-8")
    assert "srv_server_wait.computer_wait('s')  # café — naïve" in text
    assert _parse(text) is not None


# --- R7: all-or-nothing ------------------------------------------------------------------------


def test_r7_one_bad_file_writes_nothing(tmp_path: Path) -> None:
    tool = _load_tool()
    good = tmp_path / "good.py"
    bad = tmp_path / "bad.py"
    good.write_text(
        "from computer_use_mcp import server\n\n\n"
        "def test_x():\n"
        "    server.computer_wait('s')\n",
        encoding="utf-8")
    bad.write_text(
        "from computer_use_mcp import server\n\n\n"
        "def test_x():\n"
        "    server.not_in_the_map()\n",
        encoding="utf-8")
    good_before, bad_before = good.read_bytes(), bad.read_bytes()
    rc = tool.main([str(REAL_MAP), str(good), str(bad)])
    assert rc == 2
    assert good.read_bytes() == good_before  # R7: NOTHING written
    assert bad.read_bytes() == bad_before


def test_r7_unparsable_input_lists_a_problem_and_writes_nothing(tmp_path: Path) -> None:
    tool = _load_tool()
    good = tmp_path / "good.py"
    broken = tmp_path / "broken.py"
    good.write_text(
        "from computer_use_mcp import server\n\n\n"
        "def test_x():\n"
        "    server.computer_wait('s')\n",
        encoding="utf-8")
    broken.write_text("def f(:\n", encoding="utf-8")
    good_before = good.read_bytes()
    rc = tool.main([str(REAL_MAP), str(good), str(broken)])
    assert rc == 2
    assert good.read_bytes() == good_before


# --- R8: raw strings / builtin setattr / quoting ------------------------------------------------


def test_r8_raw_string_target_keeps_prefix_and_quotes() -> None:
    source = (
        "from computer_use_mcp import server\n\n\n"
        "def test_x(monkeypatch):\n"
        r'    monkeypatch.setattr(r"computer_use_mcp.server._bundles", {})' + "\n"
    )
    new, _edits, sites = _rt(source)
    assert [s for s in sites if "D3 string" not in s.reason] == []
    assert 'r"computer_use_mcp.server.lifecycle._bundles"' in new


# --- other forms ---------------------------------------------------------------------------------


def test_member_and_module_import_bindings_retarget() -> None:
    source = (
        "from computer_use_mcp import server\n\n\n"
        "def test_x():\n"
        "    registry = server.SessionRegistry()\n"
        "    lease = server.plan_layer.PLAN_GUARD\n"
        "    now = server.time.monotonic()\n"
        "    store = server.dispose_store\n"
        "    flds = server.dataclass_fields\n"  # member read: fields, not dataclass_fields
    )
    new, _edits, sites = _rt(source)
    assert sites == []
    assert "srv_core_state.SessionRegistry()" in new
    assert "srv_planning_plan.PLAN_GUARD" in new
    assert "srv_time.monotonic()" in new
    # M1: dispose_store has readers [lifecycle] -> the reference goes there
    assert "srv_server_lifecycle.dispose_store" in new
    # VAL-WP-R2b-0-02 MINOR 4: the map's `member` key is honored on READS
    assert "srv_dataclasses.fields" in new
    assert "srv_dataclasses.dataclass_fields" not in new
    assert "import dataclasses as srv_dataclasses" in new
    assert "import computer_use_mcp.planning.plan as srv_planning_plan" in new
    assert "import computer_use_mcp.core.state as srv_core_state" in new
    assert "import time as srv_time" in new


def test_dunder_file_and_doc_use_the_file_override() -> None:
    overrides = {"tests/synthetic.py": {
        "__file__": "computer_use_mcp.server.replies",
        "__doc__": "computer_use_mcp.server.replies",
    }}
    source = (
        "from computer_use_mcp import server\n\n\n"
        "def test_x():\n"
        "    path = Path(server.__file__)\n"
        "    doc = server.__doc__ or ''\n"
    )
    new, _edits, sites = _rt(source, overrides)
    assert sites == []
    assert "srv_server_replies.__file__" in new and "srv_server_replies.__doc__" in new


def test_m2_w1_package_root_override() -> None:
    tool = _load_tool()
    names, bindings, overrides = _real_map()
    source = (
        "from computer_use_mcp import server\n\n\n"
        "def test_x():\n"
        "    root = Path(server.__file__).parent\n"
    )
    new, _edits, sites = tool.retarget_source(
        source, "tests/test_w1_cancellable_wait.py", names, bindings, overrides)
    assert sites == []
    assert "srv_computer_use_mcp.__file__" in new
    assert "import computer_use_mcp as srv_computer_use_mcp" in new


def test_pkg_root_chain_form_is_retargeted() -> None:
    source = (
        "import computer_use_mcp.server\n\n\n"
        "def test_x():\n"
        "    computer_use_mcp.server.computer_wait('s')\n"
    )
    new, _edits, sites = _rt(source)
    assert sites == []
    assert "srv_server_wait.computer_wait('s')" in new


def test_from_import_lines_are_retargeted_per_home() -> None:
    source = (
        "from computer_use_mcp.server import stop_session, computer_wait\n\n\n"
        "def test_x():\n"
        "    return stop_session, computer_wait\n"
    )
    new, _edits, sites = _rt(source)
    assert sites == [], [s.reason for s in sites]
    assert "from computer_use_mcp.server.lifecycle import stop_session" in new
    assert "from computer_use_mcp.server.wait import computer_wait" in new
    assert "from computer_use_mcp.server.lifecycle import stop_session" in new
    assert "from computer_use_mcp.server.wait import computer_wait" in new


def test_bare_package_string_is_left_alone() -> None:
    source = "EXPECTED = ('computer_use_mcp.server', 'stop_session', 0)\n"
    new, edits, sites = _rt(source)
    assert sites == [] and edits == [] and new == source


def test_fixture_return_yield_and_dynamic_getattr_refuse(tmp_path: Path) -> None:
    tool = _load_tool()
    target = tmp_path / "t.py"
    target.write_text(
        "from computer_use_mcp import server\n\n\n"
        "def fx_a():\n    return server\n\n\n"
        "def fx_b():\n    yield server\n\n\n"
        "def fx_c(name):\n    return getattr(server, name)\n",
        encoding="utf-8")
    rc = tool.main(["--dry-run", str(REAL_MAP), str(target)])
    assert rc == 2


def test_rewrite_is_idempotent() -> None:
    source = (
        "from computer_use_mcp import server\n\n\n"
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(server, "_bundles", {})\n'
        "    server.computer_execute({})\n"
    )
    once, _e1, sites1 = _rt(source)
    assert sites1 == []
    twice, edits2, sites2 = _rt(once)
    assert sites2 == [] and edits2 == [] and twice == once


def test_rewrite_is_idempotent_on_the_w1_package_root_override() -> None:
    # VAL-WP-R2b-0-02 MINOR 3: the generated `import computer_use_mcp as
    # srv_computer_use_mcp` (w1 __file__ override) must NOT count as an old
    # server import — running the tool TWICE on the w1 file is a no-op.
    tool = _load_tool()
    names, bindings, overrides = _real_map()
    source = (
        "from computer_use_mcp import server\n\n\n"
        "def test_x():\n"
        "    root = Path(server.__file__).parent\n"
    )
    once, _e1, sites1 = tool.retarget_source(
        source, "tests/test_w1_cancellable_wait.py", names, bindings, overrides)
    assert sites1 == []
    assert "import computer_use_mcp as srv_computer_use_mcp" in once
    twice, edits2, sites2 = tool.retarget_source(
        once, "tests/test_w1_cancellable_wait.py", names, bindings, overrides)
    assert sites2 == [], [s.reason for s in sites2]
    assert edits2 == [] and twice == once


def test_untouched_lines_are_byte_identical() -> None:
    source = (
        "import os\n"
        "from computer_use_mcp import server\n"
        "CONSTANT = 42  # a comment that must survive verbatim\n\n\n"
        "def test_x():\n"
        "    server.computer_execute({})\n"
    )
    new, _edits, sites = _rt(source)
    assert sites == []
    old_lines = source.splitlines()
    new_lines = new.splitlines()
    for index in (0, 2, 4, 5):
        assert new_lines[index] == old_lines[index]


def test_real_map_homes_resolve_to_real_modules() -> None:
    """Post-split map↔reality check (the pre-split --audit against server.py
    ran clean at 492267b and that file is now the plan_surface git-mv target).
    FULL strength (F4, VAL-WP-R2b-1-01): every `names` entry must be a REAL
    top-level binding OF ITS HOME MODULE (per-module audit, not just "some file
    exists"), and every `import_bindings` home must be an importable module that
    actually binds the name."""
    tool = _load_tool()
    root = Path(__file__).resolve().parents[1]
    names, bindings, _overrides = tool.load_map(str(REAL_MAP))
    server_pkg = root / "src" / "computer_use_mcp" / "server"
    bindings_by_leaf: dict[str, set[str]] = {}
    for name, entry in names.items():
        leaf = entry["home"].rsplit(".", 1)[-1]
        assert (server_pkg / f"{leaf}.py").is_file(), (
            f"names[{name!r}] home {entry['home']} is not a server/ module"
        )
        if leaf not in bindings_by_leaf:
            module = ast.parse((server_pkg / f"{leaf}.py").read_text(encoding="utf-8"))
            bound: set[str] = set()
            for node in module.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bound.add(node.name)
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        for sub in ast.walk(target):
                            if isinstance(sub, ast.Name):
                                bound.add(sub.id)
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    bound.add(node.target.id)
                elif isinstance(node, ast.ImportFrom):
                    bound.update(a.asname or a.name for a in node.names)
                elif isinstance(node, ast.Import):
                    bound.update((a.asname or a.name).split(".")[0] for a in node.names)
            bindings_by_leaf[leaf] = bound
        assert name in bindings_by_leaf[leaf], (
            f"names[{name!r}] is NOT a top-level binding of {entry['home']}"
        )
    for binding, entry in bindings.items():
        if entry.get("kind") == "member":
            module = importlib.import_module(entry["home"])
            bound_name = entry.get("member", binding)
            assert hasattr(module, bound_name), (
                f"import_bindings[{binding!r}]: {entry['home']} does not bind "
                f"{bound_name!r}"
            )
        else:  # kind "module": the binding IS the imported module
            assert importlib.util.find_spec(entry["home"]) is not None, (
                f"import_bindings[{binding!r}]: {entry['home']} is not importable"
            )


def test_audit_flags_missing_and_extra_names(tmp_path: Path) -> None:
    tool = _load_tool()
    fake_server = tmp_path / "server.py"
    fake_server.write_text("import time\nA = 1\n\ndef f():\n    return A\n", encoding="utf-8")
    problems = tool.audit_map(
        str(fake_server),
        {"A": {"line": 2, "home": "x"}, "ghost": {"line": 9, "home": "y"}},
        {"time": {"line": 1, "kind": "module", "home": "time"},
         "os": {"line": 0, "kind": "module", "home": "os"}},
    )
    joined = "\n".join(problems)
    assert "MISSING from names: f" in joined
    assert "NOT a top-level binding of server.py: ghost" in joined
    assert "NOT a top-level import binding of server.py: os" in joined
    ok = tool.audit_map(str(fake_server), {"A": {"line": 2, "home": "x"}, "f": {
        "line": 4, "home": "x"}}, {"time": {"line": 1, "kind": "module", "home": "time"}})
    assert ok == []


def test_tool_never_uses_regex_on_source() -> None:
    import inspect

    module = inspect.getmodule(_load_tool().retarget_source)
    assert module is not None
    src = inspect.getsource(module)
    assert "import re\n" not in src
    assert "re.sub" not in src and "re.match" not in src and "re.search" not in src
