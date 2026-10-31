"""Deterministically retarget references to the server module (WP-R2b, A3+DELTA1).

Usage:
  python tools/restructure/retarget_refs.py <map.json> [--dry-run] [--out F] <file>...
  python tools/restructure/retarget_refs.py <map.json> --audit <server.py>

Rules (Commander DELTA 1, R1-R8):
- R1 For every map name WITH ``readers``: EVERY reference — read, call, Store,
  del, monkeypatch.setattr / builtin setattr / patch.object, dotted string —
  targets ``readers[0]`` (the primary reader: the module whose code is driven).
  HOME is used only for names WITHOUT readers. A Store is never a read.
- R2 Same object, always: each D3 extra line reuses the installed object —
  ``monkeypatch.setattr(srv_other, "X", srv_primary.X)`` right after the first
  patch (builtin-setattr and plain-assignment sources use their own form);
  the value's source text is never copied.
- R3 Rebind + D3 logic applies ONLY when the patched object is the BARE server
  alias; ``setattr(server._plan_guard, "install", ...)`` is an attribute patch
  on the object — only the reference is retargeted, no extra lines.
- R4 Aliases are unique and derived from the full dotted path
  (``srv_server_state``, ``srv_core_state``); a generated alias that collides
  with any existing name in the file is a refusal.
- R5 Inserted multi-line imports are indented to the replaced statement's
  indentation; every rewritten output is ``ast.parse``d and a parse failure is
  a refusal.
- R6 Files are read and written as BYTES (CRLF/LF preserved exactly).
- R7 All-or-nothing: the CLI rewrites every input in memory first; if ANY file
  refuses, has a generated-alias collision, or does not parse (including an
  input that is already unparsable), NOTHING is written and the CLI exits
  non-zero listing every problem.
- R8 Builtin ``setattr`` extras use builtin setattr; raw/quoted string targets
  keep their original quote; ast byte-offset columns are used for slicing only
  on pure-ASCII lines (a non-ASCII edited line is a refusal).

Also rewrites dotted string patch targets and ``from computer_use_mcp.server
import X`` lines; inserts the needed ``import <module> as srv_<dotted_path>``
lines. Idempotent: after a run no server alias remains. Untouched lines are
byte-identical. A bare ``"computer_use_mcp.server"`` string (package id) is
left alone: the package stays valid post-split.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

PKG = "computer_use_mcp"
SERVER = PKG + ".server"
ALIAS_PREFIX = "srv_"
DUNDERS = ("__file__", "__doc__")


class Site:
    """One refusal or hand-insert note."""

    def __init__(self, path: str, line: int, col: int, reason: str) -> None:
        self.path, self.line, self.col, self.reason = path, line, col, reason

    def __str__(self) -> str:
        return f"{self.path}:{self.line}:{self.col}: {self.reason}"


def load_map(map_path: str) -> tuple[dict, dict, dict]:
    data = json.loads(Path(map_path).read_text(encoding="utf-8"))
    return data["names"], data["import_bindings"], data.get("file_overrides", {})


def _alias_of(module: str) -> str:
    """R4: unique alias derived from the FULL dotted path."""
    if module == PKG:
        path = PKG
    elif module.startswith(PKG + "."):
        path = module[len(PKG) + 1:]
    else:
        path = module
    return ALIAS_PREFIX + path.replace(".", "_")


def _import_line_for(module: str) -> str:
    return f"import {module} as {_alias_of(module)}"


def _patch_calls(tree: ast.AST) -> tuple[dict[int, ast.Call], set[int]]:
    """({id(arg0): patch call} for object-form targets, {id(const)} for
    string-form targets) of monkeypatch.setattr / builtin setattr /
    patch.object / mock.patch calls."""

    def patch_form(func: ast.AST) -> str | None:
        if isinstance(func, ast.Attribute):
            if func.attr == "setattr" and isinstance(func.value, ast.Name):
                return "setattr"
            if func.attr == "object" and (
                (isinstance(func.value, ast.Attribute) and func.value.attr == "patch")
                or (isinstance(func.value, ast.Name) and func.value.id in ("patch", "patch_object"))
            ):
                return "patch.object"
            if func.attr == "patch":
                return "patch"
        elif isinstance(func, ast.Name):
            if func.id == "setattr":
                return "setattr"
            if func.id in ("patch", "patch_object"):
                return "patch"
        return None

    obj_targets: dict[int, ast.Call] = {}
    string_targets: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or patch_form(node.func) is None:
            continue
        target = node.args[0] if node.args else next(
            (kw.value for kw in node.keywords if kw.arg == "target"), None)
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            string_targets.add(id(target))
        elif target is not None:
            obj_targets[id(target)] = node
    return obj_targets, string_targets


def _server_roots(tree: ast.AST) -> dict[str, str]:
    """{local name: 'direct'|'pkg'} for every alias bound to the server module
    ('direct': the alias IS the server module; 'pkg': the alias is the
    computer_use_mcp package and the chain continues through ``.server``)."""
    roots: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == SERVER:
                    roots[alias.asname or PKG] = "direct" if alias.asname else "pkg"
                elif alias.name == PKG:
                    roots[alias.asname or PKG] = "pkg"
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == PKG:
            for alias in node.names:
                if alias.name == "server":
                    roots[alias.asname or "server"] = "direct"
    return roots


def _args_of_call(call: ast.Call, index: int, keyword: str) -> ast.AST | None:
    if len(call.args) > index:
        return call.args[index]
    return next((kw.value for kw in call.keywords if kw.arg == keyword), None)


def retarget_source(
    source: str, relpath: str, names: dict, bindings: dict, overrides: dict
) -> tuple[str, list[dict], list[Site]]:
    """Rewrite one file's server references: (new_source, dry-run edit table,
    refusals/hand-notes). With no refusals the new source differs only at the
    reported edits, parses, and re-running is a no-op."""
    tree = ast.parse(source, filename=relpath)
    kept_lines = source.splitlines(keepends=True)
    newline = "\r\n" if kept_lines and kept_lines[0].endswith("\r\n") else "\n"
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    obj_patches, string_patches = _patch_calls(tree)
    roots = _server_roots(tree)
    taken: set[str] = set()
    for node in ast.walk(tree):  # R4: every identifier already in the file
        if isinstance(node, ast.Name):
            taken.add(node.id)
        elif isinstance(node, ast.arg):
            taken.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            taken.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            taken.add(node.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                taken.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                taken.add(a.asname or a.name)
    import_stmts: list[ast.stmt] = [
        node for node in ast.walk(tree)
        if (isinstance(node, ast.Import)
            and any(a.name == SERVER or (a.name == PKG and a.asname)
                    for a in node.names))
        or (isinstance(node, ast.ImportFrom) and node.level == 0
            and node.module == PKG and any(a.name == "server" for a in node.names))
    ]
    # NOTE: a plain `import computer_use_mcp` (no asname — e.g. an importability
    # probe) is NOT rewritten; it stays valid post-split.

    edits: list[dict] = []
    sites: list[Site] = []
    needed: list[str] = []

    def require_module(module: str, node: ast.AST | None = None) -> str:
        alias = _alias_of(module)
        if alias in taken:
            if node is not None:
                sites.append(Site(relpath, node.lineno, node.col_offset,
                                  f"generated alias '{alias}' collides with an "
                                  "existing name in this file (R4)"))
            return alias
        line = _import_line_for(module)
        if line not in needed:
            needed.append(line)
        return alias

    def refuse(node: ast.AST, reason: str) -> None:
        sites.append(Site(relpath, node.lineno, node.col_offset, reason))

    def replace(node: ast.AST, new_text: str, form: str) -> None:
        old = (ast.get_source_segment(source, node) or "").replace("\n", " ").strip()
        shown = new_text.replace("\n", " ").replace("\r", " ").strip()
        edits.append({
            "line": node.lineno, "col": node.col_offset, "form": form,
            "old": old, "new": shown, "new_text": new_text,
            "start": (node.lineno, node.col_offset),
            "end": (node.end_lineno or node.lineno,
                    node.end_col_offset or node.col_offset),
        })

    def enclosing_stmt(node: ast.AST) -> ast.stmt:
        while not isinstance(node, ast.stmt):
            node = parents[id(node)]
        return node

    def add_after(stmt: ast.stmt, lines: list[str], form: str = "d3-extra") -> None:
        indent = " " * stmt.col_offset
        block = newline + newline.join(indent + text for text in lines)
        edits.append({
            "line": stmt.end_lineno, "col": stmt.end_col_offset, "form": form,
            "old": "",
            "new": "; ".join(text.strip() for text in lines),
            "new_text": block,
            "start": (stmt.end_lineno, stmt.end_col_offset),
            "end": (stmt.end_lineno, stmt.end_col_offset),
        })

    def resolve(name: str, node: ast.AST) -> tuple[dict, str] | None:
        if name in DUNDERS:
            target = overrides.get(relpath, {}).get(name)
            if target is None:
                refuse(node, f"unhandled dunder '{name}': no file_overrides entry for {relpath}")
                return None
            return {"home": target}, "name"
        if name in names:
            return names[name], "name"
        if name in bindings:
            return bindings[name], bindings[name]["kind"]
        refuse(node, f"unmapped name 'server.{name}' (not in the retarget map)")
        return None

    def primary_of(entry: dict) -> str:
        return (entry.get("readers") or [entry["home"]])[0]  # R1

    def d3_lines(form: str, patcher: str | None, primary_alias: str,
                 targets: list[str], name: str) -> list[str] | None:
        """R2: extras reuse the installed object; None => hand-insert."""
        if form == "monkeypatch":
            return [f"{patcher}.setattr({_alias_of(t)}, {json.dumps(name)}, "
                    f"{primary_alias}.{name})" for t in targets[1:]]
        if form == "setattr":
            return [f"setattr({_alias_of(t)}, {json.dumps(name)}, "
                    f"{primary_alias}.{name})" for t in targets[1:]]
        if form == "assign":
            return [f"{_alias_of(t)}.{name} = {primary_alias}.{name}"
                    for t in targets[1:]]
        return None

    def emit_d3(stmt: ast.stmt, form: str, patcher: str | None, primary: str,
                targets: list[str], name: str, node: ast.AST) -> None:
        lines = d3_lines(form, patcher, primary, targets, name)
        if lines is None:
            sites.append(Site(relpath, node.lineno, node.col_offset,
                              f"D3 rebind of '{name}' via {form}: insert same-object "
                              f"lines for {targets[1:]} by hand"))
            return
        add_after(stmt, lines)

    def rewrite_chain(node: ast.Attribute, root_name: str, mode: str) -> None:
        attrs: list[str] = [node.attr]
        cur: ast.AST = node.value
        while isinstance(cur, ast.Attribute):
            attrs.append(cur.attr)
            cur = cur.value
        if mode == "pkg":
            if not attrs or attrs[-1] != "server":
                return  # computer_use_mcp.<other submodule path> — untouched
            attrs = attrs[:-1]
            if not attrs:
                return  # bare computer_use_mcp.server — the Name branch handles it
        name = attrs[-1]
        tail = attrs[:-1][::-1]
        got = resolve(name, node)
        if got is None:
            return
        entry, kind = got
        suffix = "." + ".".join(tail) if tail else ""
        alias = require_module(primary_of(entry), node)  # R1/R3: references only
        if kind == "module":
            replace(node, alias + suffix, "read")
        else:
            replace(node, alias + "." + name + suffix, "read")
        readers = entry.get("readers") or []
        if isinstance(node.ctx, ast.Store) and len(readers) > 1:
            # plain-assignment rebind: R2 same-object extras (assign form)
            assign = parents.get(id(node))
            value_src = ast.get_source_segment(source, assign.value) \
                if isinstance(assign, ast.Assign) and assign.value is not None else None
            if value_src is None:
                sites.append(Site(relpath, node.lineno, node.col_offset,
                                  f"D3 assign rebind of '{name}': insert same-object "
                                  f"assignments for {readers[1:]} by hand"))
            else:
                primary_alias = _alias_of(readers[0])
                for other in readers[1:]:
                    require_module(other, node)
                emit_d3(enclosing_stmt(node), "assign", None, primary_alias,
                        readers, name, node)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            root = node.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in roots:
                top: ast.AST = node
                while isinstance(parents.get(id(top)), ast.Attribute):
                    top = parents[id(top)]
                if top is node:
                    rewrite_chain(node, root.id, roots[root.id])
        elif isinstance(node, ast.Name) and node.id in roots \
                and not isinstance(parents.get(id(node)), ast.Attribute):
            call = parents.get(id(node))
            if id(node) in obj_patches and isinstance(call, ast.Call):
                # R3: rebind only when the patched object is the BARE alias
                name_node = _args_of_call(call, 1, "name")
                if not (isinstance(name_node, ast.Constant)
                        and isinstance(name_node.value, str)):
                    refuse(node, "patch call with a non-constant attribute name")
                    continue
                got = resolve(name_node.value, node)
                if got is None:
                    continue
                entry, _kind = got
                targets = entry.get("readers") or [entry["home"]]
                primary = require_module(targets[0], node)
                for other in targets[1:]:
                    require_module(other, node)
                replace(node, primary, "rebind")
                if len(targets) > 1:
                    func_src = ast.get_source_segment(source, call.func) or ""
                    if func_src == "setattr":
                        form, patcher = "setattr", None
                    elif func_src.endswith(".setattr"):
                        form, patcher = "monkeypatch", func_src[: -len(".setattr")]
                    else:
                        form, patcher = func_src, None
                    emit_d3(enclosing_stmt(call), form, patcher, primary,
                            targets, name_node.value, node)
                continue
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) \
                    and call.func.id == "getattr" and call.args and call.args[0] is node:
                name_node = _args_of_call(call, 1, "name")
                if isinstance(name_node, ast.Constant) and isinstance(name_node.value, str):
                    got = resolve(name_node.value, node)
                    if got is not None:
                        entry, kind = got
                        alias = require_module(primary_of(entry), node)
                        if kind == "module":
                            replace(node, alias, "read")
                        else:
                            replace(node, f"{alias}.{name_node.value}", "read")
                    continue
                refuse(node, "getattr on the server module with a non-constant name")
                continue
            if isinstance(call, ast.Return):
                refuse(node, "unhandled form: a return of the server module "
                             "(fixture returning the module) — handle by hand")
                continue
            if isinstance(parents.get(id(node)), ast.Yield):
                refuse(node, "unhandled form: a yield of the server module "
                             "(fixture yielding the module) — handle by hand")
                continue
            refuse(node, "unhandled form: bare server module used as a value")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) in string_patches:
            value = node.value
            if value != SERVER and not value.startswith(SERVER + "."):
                continue
            rest = value[len(SERVER):].lstrip(".")
            head, _, deeper = rest.partition(".")
            if not head:
                continue  # bare package id stays valid post-split
            got = resolve(head, node)
            if got is None:
                continue
            entry, kind = got
            targets = entry.get("readers") or [entry["home"]]
            segment = ast.get_source_segment(source, node) or repr(value)
            stripped = segment.lstrip("rbuRBUfF")
            prefix = segment[:len(segment) - len(stripped)]
            quote = stripped[:1] or '"'
            replace(node, f"{prefix}{quote}{targets[0]}.{rest}{quote}", "string")
            if len(targets) > 1:
                sites.append(Site(relpath, node.lineno, node.col_offset,
                                  f"D3 string rebind of '{head}': add "
                                  f"{targets[1:]} targets by hand"))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == SERVER:
            if any(a.name == "*" for a in node.names):
                refuse(node, "unhandled form: star-import from the server module")
                continue
            by_home: dict[str, list[str]] = {}
            for alias in node.names:
                got = resolve(alias.name, node)
                if got is None:
                    continue
                entry, _kind = got
                if entry.get("drop"):
                    continue  # D5: dropped from the receiving import line
                bound = alias.asname or alias.name
                imported = entry.get("member", alias.name)
                by_home.setdefault(entry["home"], []).append(
                    imported if imported == bound else f"{imported} as {bound}")
            indent = " " * node.col_offset
            lines = [f"from {home} import " + ", ".join(items)
                     for home, items in sorted(by_home.items())]
            replace(node, lines[0] + "".join(newline + indent + text for text in lines[1:])
                    if lines else "", "from-import")

    for stmt in import_stmts:
        replace(stmt, "", "old-import")

    if not edits and not sites:
        return source, edits, sites
    offset_of: dict[int, int] = {}
    pos = 0
    line_texts = source.splitlines()
    for i, line in enumerate(kept_lines, start=1):
        offset_of[i] = pos
        pos += len(line)

    def abs_pos(p: tuple[int, int]) -> int:
        # R8: ast col offsets are UTF-8 BYTE offsets — convert to char offsets
        line = line_texts[p[0] - 1] if 0 < p[0] <= len(line_texts) else ""
        byte_col = min(p[1], len(line.encode("utf-8")))
        return offset_of.get(p[0], 0) + len(line.encode("utf-8")[:byte_col].decode("utf-8"))

    if needed and import_stmts:
        first = import_stmts[0]
        indent = " " * first.col_offset
        for edit in edits:
            if edit["start"] == (first.lineno, first.col_offset) \
                    and edit["form"] == "old-import":
                # R5: inserted imports inherit the replaced statement's indent
                edit["new_text"] = "".join(
                    [needed[0]] + [newline + indent + line for line in needed[1:]])
                edit["new"] = " ; ".join(needed)
                edit["form"] = "new-imports"
                break
    elif needed:
        # no server-import statement to replace (pkg-alias chains only): insert
        # the block at the top of the file
        edits.append({
            "line": 1, "col": 0, "form": "new-imports", "old": "",
            "new": " ; ".join(needed),
            "new_text": "".join([needed[0]] + [newline + line for line in needed[1:]]),
            "start": (1, 0), "end": (1, 0),
        })
    pieces: list[tuple[int, int, str]] = []
    for edit in edits:
        s, e = abs_pos(edit["start"]), abs_pos(edit["end"])
        # deletions swallow the statement's trailing newline so no blank line
        # (and no empty suite) is left behind
        if edit["form"] in ("old-import", "from-import", "drop-import") \
                and not edit.get("new_text", edit["new"]):
            if source[s:s + 2] == "\r\n":
                e += 2
            elif s < len(source) and source[s] == "\n":
                e += 1
        pieces.append((s, e, edit.get("new_text", edit["new"])))
    out = source
    for s, e, text in sorted(pieces, key=lambda p: (p[0], p[1]), reverse=True):
        out = out[:s] + text + out[e:]
    try:
        ast.parse(out, filename=relpath)  # R5: the output must parse
    except SyntaxError as exc:
        sites.append(Site(relpath, exc.lineno or 0, exc.offset or 0,
                          f"rewritten output does not parse (refusing): {exc.msg}"))
    return out, edits, sites


def audit_map(server_path: str, names: dict, bindings: dict) -> list[str]:
    """Cross-check the map against server.py's actual top-level bindings:
    names ∪ import_bindings must equal them exactly (no overlap, no gaps)."""
    tree = ast.parse(Path(server_path).read_text(encoding="utf-8"))
    bound: dict[str, int] = {}
    imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound[node.name] = node.lineno
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        bound[sub.id] = node.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound[node.target.id] = node.lineno
        elif isinstance(node, ast.Import):
            for a in node.names:
                imports.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue  # binds no runtime name
            for a in node.names:
                imports.add(a.asname or a.name)
    problems: list[str] = []
    for name in sorted(set(bound) - set(names)):
        problems.append(f"MISSING from names: {name} (line {bound[name]})")
    for name in sorted(set(names) - set(bound)):
        problems.append(f"NOT a top-level binding of server.py: {name}")
    for name in sorted(set(imports) - set(bindings)):
        problems.append(f"MISSING from import_bindings: {name}")
    for name in sorted(set(bindings) - set(imports)):
        problems.append(f"NOT a top-level import binding of server.py: {name}")
    overlap = sorted(set(names) & set(bindings))
    if overlap:
        problems.append(f"name in BOTH sections: {overlap}")
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("map")
    parser.add_argument("files", nargs="*")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", metavar="REPORT",
                        help="write the dry-run report to this file")
    parser.add_argument("--audit", metavar="SERVER_PY")
    args = parser.parse_intermixed_args(argv)
    names, bindings, overrides = load_map(args.map)
    if args.audit:
        problems = audit_map(args.audit, names, bindings)
        for problem in problems:
            print(problem)
        return 1 if problems else 0
    if not args.files:
        parser.error("no files given")
    report: list[str] = []
    problems: list[str] = []
    outputs: list[tuple[Path, str, str]] = []

    def emit(text: str) -> None:
        report.append(text)
        if not args.out:
            print(text)

    for file_path in args.files:
        path = Path(file_path)
        relpath = path.as_posix()
        try:
            source = path.read_bytes().decode("utf-8")  # R6: bytes in
        except UnicodeDecodeError as exc:
            problems.append(f"{relpath}: not valid UTF-8: {exc}")
            continue
        try:
            new_source, edits, file_sites = retarget_source(
                source, relpath, names, bindings, overrides)
        except SyntaxError as exc:
            problems.append(f"{relpath}: input does not parse: {exc}")
            continue
        problems.extend(str(site) for site in file_sites)
        outputs.append((path, relpath, new_source))
        emit(f"== {relpath}: {len(edits)} edit(s), {len(file_sites)} site(s)")
        for edit in edits:
            emit(f"  L{edit['line']}:C{edit['col']} [{edit['form']}] "
                 f"{edit['old']!r} -> {edit['new']!r}")
    if problems:
        emit(f"\nREFUSED/HAND-INSERT SITES ({len(problems)}) — NOTHING was written:")
        for problem in problems:
            emit(f"  {problem}")
    if args.out:
        Path(args.out).write_text("\n".join(report) + "\n", encoding="utf-8")
    if problems:
        return 2  # R7: all-or-nothing
    if not args.dry_run:
        for path, relpath, new_source in outputs:
            current = path.read_bytes().decode("utf-8")
            if new_source != current:
                path.write_bytes(new_source.encode("utf-8"))  # R6: bytes out
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
