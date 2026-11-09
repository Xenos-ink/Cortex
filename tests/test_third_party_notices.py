"""WP-L + WP-M (ORVEX-2026-109) truth test: THIRD_PARTY_NOTICES.md vs uv.lock.

Derives from ``uv.lock`` BOTH closures for Windows (no network, no venv
dependency): the default runtime set of ``computer-use-mcp`` (dev excluded)
and the ``pyautogui`` optional extra's closure minus the default. Asserts that
every locked package has a row in the right notices table at the locked
version, that every licence file the tables name exists, and that no file in
third_party/licenses/ (top level or spdx/) is unreferenced.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOCK = REPO / "uv.lock"
NOTICES = REPO / "THIRD_PARTY_NOTICES.md"
LICENSES_DIR = REPO / "third_party" / "licenses"

ROOT = "computer-use-mcp"


# ---------------------------------------------------------------- lock closure

def _include_marker(marker: str | None) -> bool:
    """Windows install closure: include unless the marker excludes win32."""
    if not marker:
        return True
    if "sys_platform != 'win32'" in marker or "sys_platform == 'emscripten'" in marker:
        return False
    return True


def _closure(lock_data: dict, extras: set[str]) -> dict[str, str]:
    """Locked Windows closure of the root package with the given extras on."""
    packages = {p["name"]: p for p in lock_data["package"]}
    closure: dict[str, str] = {}
    requested_extras: set[tuple[str, str]] = set()  # (pkg, extra) pairs
    root = packages[ROOT]
    queue: list[tuple[str, dict]] = [(d["name"], d) for d in root["dependencies"]]
    for extra in extras:
        queue.append((ROOT, {"name": ROOT, "extra": extra}))
    while queue:
        name, dep = queue.pop()
        if not _include_marker(dep.get("marker")):
            continue
        extra = dep.get("extra")
        extras_req = set(extra) if isinstance(extra, list) else ({extra} if extra else set())
        if extras_req and not {(name, e) for e in extras_req} <= requested_extras:
            requested_extras |= {(name, e) for e in extras_req}
            pkg = packages[name]
            for e in extras_req:
                for child in pkg.get("optional-dependencies", {}).get(e, []):
                    queue.append((child["name"], child))
        if name in closure:
            continue
        closure[name] = packages[name]["version"]
        for child in packages[name].get("dependencies", []):
            queue.append((child["name"], child))
    return closure


def locked_closures(lock_data: dict | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """(default closure, pyautogui-extra-only closure): name -> locked version."""
    if lock_data is None:
        lock_data = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    default = _closure(lock_data, set())
    with_extra = _closure(lock_data, {"pyautogui"})
    extra_only = {
        name: version
        for name, version in with_extra.items()
        if name not in default and name != ROOT
    }
    return default, extra_only


# ------------------------------------------------------------------- notices

def _table_rows(notices_text: str, section_header: str) -> dict[str, dict]:
    """Parse the markdown table following a section header: name -> row info."""
    lines = notices_text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == section_header), None
    )
    if start is None:
        return {}
    rows: dict[str, dict] = {}
    in_table = False
    for line in lines[start + 1:]:
        if line.startswith("#"):
            break
        if line.startswith("| Name | Version |"):
            in_table = True
            continue
        if in_table and not line.startswith("|"):
            in_table = False
            break
        if in_table and line.startswith("|") and not line.startswith("|---"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            rows[cells[0].lower()] = {
                "version": cells[1],
                "files_cell": cells[4],
            }
    return rows


def _referenced_files(notices_text: str, rows_by_section: dict[str, dict[str, dict]]) -> set[str]:
    files: set[str] = set()
    for rows in rows_by_section.values():
        for row in rows.values():
            if row["files_cell"] != "none shipped":
                for fname in row["files_cell"].split(","):
                    files.add(fname.strip())
    for match in re.finditer(r"spdx/([\w.-]+)", notices_text):
        files.add(f"spdx/{match.group(1)}")
    return files


def check_notices(
    lock_data: dict, notices_text: str, shipped_files: set[str]
) -> list[str]:
    """All WP-L/WP-M truth assertions on one lock/notices pair. Returns failures."""
    failures: list[str] = []
    default_rows = _table_rows(notices_text, "## Runtime dependencies (default install)")
    extra_rows = _table_rows(notices_text, "## Optional extra `pyautogui`")
    default, extra_only = locked_closures(lock_data)
    for closure, rows, table in (
        (default, default_rows, "Runtime dependencies (default install)"),
        (extra_only, extra_rows, "Optional extra `pyautogui`"),
    ):
        for name, version in sorted(closure.items()):
            row = rows.get(name)
            if row is None:
                failures.append(
                    f"locked package {name!r} has no row in the {table!r} table"
                )
                continue
            if row["version"] != version:
                failures.append(
                    f"{name}: notices version {row['version']!r} != locked version {version!r}"
                )
            if row["files_cell"] == "none shipped":
                continue
            if not row["files_cell"]:
                failures.append(f"{name}: empty licence-files cell in the {table!r} table")
                continue
            for fname in row["files_cell"].split(","):
                fname = fname.strip()
                if fname and fname not in shipped_files:
                    failures.append(
                        f"{name}: named licence file {fname!r} is missing from third_party/licenses/"
                    )
    # every row must belong to its table's closure (no stale/extra rows)
    for rows, closure, table in (
        (default_rows, default, "Runtime dependencies (default install)"),
        (extra_rows, extra_only, "Optional extra `pyautogui`"),
    ):
        for name in rows:
            if name == ROOT:
                continue
            if name not in closure:
                failures.append(
                    f"row {name!r} in the {table!r} table is not in that table's closure"
                )
    referenced = _referenced_files(
        notices_text, {"default": default_rows, "extra": extra_rows}
    )
    for fname in sorted(referenced):
        if fname.startswith("spdx/") and fname not in shipped_files:
            failures.append(f"named spdx licence file {fname!r} is missing from third_party/licenses/")
    for fname in sorted(shipped_files):
        if fname not in referenced:
            failures.append(f"unreferenced file in third_party/licenses/: {fname!r}")
    return failures


# ------------------------------------------------- F1 (DELTA 3): bare PyPI name

#: The bare PyPI name would fail (or install someone else's package) — it is
#: only legitimate when followed by " @ git+…".
_BARE_EXTRA_NAME = re.compile(r"computer-use-mcp\[pyautogui\](?! @ git\+)")


def _join_wrapped_literals(text: str) -> str:
    # the message literals may wrap across lines ("…[pyautogui] @ '\n    'git+…");
    # re-join adjacent string-literal fragments so the guard sees the full text
    return re.sub(r"['\"]\s*\n\s*['\"]", "", text)


def _bare_extra_name_lines(text: str) -> list[str]:
    joined = _join_wrapped_literals(text)
    return [
        f"line {i}: {line.strip()}"
        for i, line in enumerate(joined.splitlines(), 1)
        if _BARE_EXTRA_NAME.search(line)
    ]


#: DELTA 5 (MINOR-1): ONE module-level roots constant + ONE file walker shared
#: by the scanner and its controls — the controls exercise the real scanner.
_SCAN_ROOTS = (
    REPO / "README.md",
    REPO / "VERSIONS.md",
    REPO / "THIRD_PARTY_NOTICES.md",
    REPO / "docs",
    REPO / "src",
)

_GUARDED_NAME = re.compile(r"computer-use-mcp\[pyautogui\] @ git\+")


def _scan_files() -> list[Path]:
    files: list[Path] = []
    for root in _SCAN_ROOTS:
        if root.is_file():
            files.append(root)
        else:
            files.extend(
                path
                for path in sorted(root.rglob("*"))
                if path.is_file() and path.suffix in {".md", ".py"}
            )
    return files


def bare_extra_name_sites() -> tuple[list[str], int]:
    """(bare-name hits, guarded-occurrence count) over the scan roots."""
    hits: list[str] = []
    guarded = 0
    for path in _scan_files():
        joined = _join_wrapped_literals(path.read_text(encoding="utf-8"))
        for hit in _bare_extra_name_lines(joined):
            hits.append(f"{path}: {hit}")
        guarded += len(_GUARDED_NAME.findall(joined))
    return hits, guarded


def test_no_bare_pypi_extra_name_in_docs_or_src() -> None:
    # m-1 positive controls, on the SAME constant and the SAME walker the
    # scanner uses: the scan cannot pass vacuously.
    for root in _SCAN_ROOTS:
        assert root.exists(), f"scan root missing: {root}"
    backend = REPO / "src" / "computer_use_mcp" / "desktop" / "backend.py"
    assert "computer-use-mcp[pyautogui] @ git+" in _join_wrapped_literals(
        backend.read_text(encoding="utf-8")
    ), "the joiner no longer reconstructs backend.py's wrapped literal"
    hits, guarded = bare_extra_name_sites()
    assert guarded >= 1, "no guarded occurrence found — the scan may be blind"
    assert hits == [], (
        "bare `computer-use-mcp[pyautogui]` (would hit PyPI); it must be "
        "followed by ` @ git+`:\n" + "\n".join(hits)
    )


def test_negative_selftest_bare_extra_name_detector() -> None:
    assert _bare_extra_name_lines('pip install "computer-use-mcp[pyautogui]"') != []
    assert _bare_extra_name_lines('uv sync --extra pyautogui') == []
    assert (
        _bare_extra_name_lines(
            'pip install "computer-use-mcp[pyautogui] @ git+https://example/Cortex.git"'
        )
        == []
    )
    # a line-wrapped literal is also accepted (joined before matching)
    assert (
        _bare_extra_name_lines(
            'pip install "computer-use-mcp[pyautogui] @ \'\n    \'git+https://example/Cortex.git"'
        )
        == []
    )


# --------------------------------------------------------------------- tests

def test_notices_match_locked_windows_runtime() -> None:
    lock_data = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    shipped = {
        p.relative_to(LICENSES_DIR).as_posix()
        for p in LICENSES_DIR.rglob("*")
        if p.is_file()
    }
    failures = check_notices(lock_data, NOTICES.read_text(encoding="utf-8"), shipped)
    assert not failures, "\n".join(failures)


def test_default_closure_excludes_pyautogui_family_and_dev() -> None:
    default, extra_only = locked_closures()
    for pkg in (
        "pyautogui", "mouseinfo", "pymsgbox", "pyscreeze", "pygetwindow",
        "pytweening", "pyperclip", "pyrect",
        "pytest", "pytest-asyncio", "ruff", "pluggy", "iniconfig",
    ):
        assert pkg not in default, pkg
    assert set(extra_only) == {
        "pyautogui", "mouseinfo", "pymsgbox", "pyscreeze", "pygetwindow",
        "pytweening", "pyperclip", "pyrect",
    }
    assert "pyautogui" in default | extra_only
    assert "pywin32" in default  # win32 marker via mcp
    assert "cryptography" in default  # pulled via the pyjwt crypto extra


def test_negative_selftest_fails_on_bad_inputs() -> None:
    lock_data = {
        "package": [
            {
                "name": "computer-use-mcp",
                "version": "0.1.0",
                "dependencies": [{"name": "alpha", "marker": "sys_platform == 'win32'"}],
                "optional-dependencies": {"pyautogui": [{"name": "delta"}]},
            },
            {"name": "alpha", "version": "1.0", "dependencies": [{"name": "beta"}]},
            {"name": "beta", "version": "2.0", "dependencies": []},
            {"name": "delta", "version": "4.0", "dependencies": []},
            {"name": "pytest", "version": "8.0.0", "dependencies": []},
        ],
    }
    shipped = {"alpha-LICENSE", "spdx/GPL-2.0-or-later.txt"}

    default_table = (
        "## Runtime dependencies (default install)\n"
        "| Name | Version | Licence | Upstream | Licence files | Pulled in by |\n"
        "|---|---|---|---|---|---|\n"
        "| alpha | 1.0 | MIT | x | alpha-LICENSE | computer-use-mcp |\n"
        "| beta | 2.0 | MIT | x | none shipped | alpha |\n"
    )
    extra_table = (
        "\n"
        "## Optional extra `pyautogui`\n"
        "| Name | Version | Licence | Upstream | Licence files | Pulled in by |\n"
        "|---|---|---|---|---|---|\n"
        "| delta | 4.0 | MIT | x | none shipped | computer-use-mcp (extra) |\n"
    )
    good = default_table + extra_table + "\nEvidence: spdx/GPL-2.0-or-later.txt\n"
    assert check_notices(lock_data, good, shipped) == []

    missing_row = check_notices(lock_data, "", shipped)
    assert any("no row" in f for f in missing_row)
    missing_extra_row = check_notices(lock_data, default_table, shipped)
    assert any("delta" in f and "no row" in f for f in missing_extra_row)

    wrong_version = check_notices(
        lock_data, good.replace("| alpha | 1.0 |", "| alpha | 9.9 |"), shipped
    )
    assert any("locked version" in f for f in wrong_version)

    phantom_file = check_notices(
        lock_data, good.replace("alpha-LICENSE", "ghost-LICENSE"), shipped
    )
    assert any("missing from third_party/licenses/" in f for f in phantom_file)

    missing_spdx = check_notices(lock_data, good, {"alpha-LICENSE"})
    assert any("spdx/GPL-2.0-or-later.txt' is missing" in f for f in missing_spdx)

    empty_cell = check_notices(
        lock_data, good.replace("| alpha-LICENSE | computer-use-mcp |", "| | computer-use-mcp |"), shipped
    )
    assert any("empty licence-files cell" in f for f in empty_cell)

    wrong_table_row = check_notices(
        lock_data, default_table + extra_table.replace(
            "| delta | 4.0 | MIT | x | none shipped | computer-use-mcp (extra) |",
            "",
        ).rstrip() + "\n| alpha | 1.0 | MIT | x | alpha-LICENSE | misplaced |\n"
        "\nEvidence: spdx/GPL-2.0-or-later.txt\n",
        shipped,
    )
    assert any("not in that table's closure" in f for f in wrong_table_row)
    assert any("delta" in f and "no row" in f for f in wrong_table_row)

    orphan = check_notices(
        lock_data, good.replace("spdx/GPL-2.0-or-later.txt\n", "\n"), shipped
    )
    assert any("unreferenced file" in f for f in orphan)
