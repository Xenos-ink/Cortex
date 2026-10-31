"""Build the WP-R2b symbol_map.json from the retarget map (step-1 moves only).

Usage: python tools/restructure/make_r2b_symbol_map.py <before_symbols.json> <out.json>

Routes every `computer_use_mcp.server` symbol to its new server/ module using the
HOME column of tools/restructure/r2b_retarget_map.json (`names` section — the
import_bindings never produced inventory symbols: Import/ImportFrom nodes are
excluded from the inventory hashes). Fail closed: any before-symbol of the old
server module without a route, and any route naming a module that is not a file
of the new package, is a hard error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MAP = Path(__file__).resolve().parent / "r2b_retarget_map.json"
OLD_MODULE = "computer_use_mcp.server"

#: The retarget map's `home` is the REFERENCE target (where reads/calls land per
#: plan §5), not always the STATEMENT home. `mcp = FastMCP("Cortex")` is
#: CONSTRUCTED in instance.py (plan §7 R-f) while its references retarget to
#: app.mcp (plan §5) — the inventory symbol therefore routes to instance.
SYMBOL_HOME_OVERRIDES = {
    "mcp": "computer_use_mcp.server.instance",
}

#: Top-level statement routes (the before module's non-def/class/assign
#: statements; fail closed on any other): the basicConfig Expr (old 161) and
#: the `if __name__ == "__main__"` If (old 6000-6002). The module docstring is
#: not an inventory statement (docstrings are excluded by the inventory tool).
STATEMENT_ROUTES = {
    "stmt@a068be94": "computer_use_mcp.server.instance",
    "stmt@a390addd": "computer_use_mcp.server.__main__",
}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: make_r2b_symbol_map.py <before_symbols.json> <out.json>", file=sys.stderr)
        return 2
    retarget = json.loads(MAP.read_text(encoding="utf-8"))
    homes = {name: SYMBOL_HOME_OVERRIDES.get(name, entry["home"])
             for name, entry in retarget["names"].items()}
    before = json.loads(Path(argv[0]).resolve().read_text(encoding="utf-8"))
    server_symbols = {e["name"] for e in before["symbols"] if e["module"] == OLD_MODULE}
    missing = sorted(server_symbols - set(homes))
    if missing:
        raise SystemExit(f"no HOME route for before-symbols: {missing}")
    for stmt in before.get("statements", []):
        if stmt["module"] != OLD_MODULE:
            continue
        if stmt["name"] not in STATEMENT_ROUTES:
            raise SystemExit(f"no route for before-statement {stmt['name']} ({stmt['kind']})")
    new_pkg = Path(__file__).resolve().parents[2] / "src" / "computer_use_mcp" / "server"
    for home in sorted(set(homes.values()) | set(STATEMENT_ROUTES.values())):
        leaf = home.rsplit(".", 1)[-1]
        if not (new_pkg / f"{leaf}.py").is_file():
            raise SystemExit(f"route target is not a module of server/: {home}")
    blob = {f"{OLD_MODULE}:{name}": home for name, home in sorted(homes.items())}
    blob.update({f"{OLD_MODULE}:{name}": home for name, home in sorted(STATEMENT_ROUTES.items())})
    Path(argv[1]).resolve().write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")
    print(f"symbol_map: {len(blob)} routes -> {argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
