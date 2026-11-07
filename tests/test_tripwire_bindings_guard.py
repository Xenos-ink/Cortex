"""D7 fail-closed guard (WP-R2a): no effect object may escape the tripwire.

The tripwire wraps every module-level binding named in
``desktop_tripwire.EFFECT_DLL_BINDINGS`` of every loaded
``computer_use_mcp.*`` module. Three independent arms keep that true no matter
how backend.py is split — each arm has a self-test proving it FAILS on a
synthetic violation:

1. RUNTIME ARM — after the autouse fixture arms the tripwire, every loaded
   ``computer_use_mcp.*`` module attribute in ``EFFECT_DLL_BINDINGS`` holding a
   real ``ctypes.WinDLL`` must carry the ``_cortex_tripwire`` marker. A future
   module that binds an effect object the tripwire did not wrap fails here.
2. STATIC ARM — every MODULE-LEVEL ``ctypes.windll.<x>`` / ``ctypes.WinDLL``,
   ``ctypes.oledll.<x>`` / ``ctypes.CDLL`` assignment under
   ``src/computer_use_mcp/`` must bind a name in ``EFFECT_DLL_BINDINGS`` (or a
   reviewed non-effect exception). A future ``_gdi32 = ctypes.windll.gdi32``
   fails the guard until the tripwire list (and its effect map) grows.
   Function-local binds are out of the runtime tripwire's reach by definition
   (they are not module attributes) and are documented separately (the dxgi
   capture path binds ``dxgi``/``d3d11`` locals inside ``_DxgiDuplicator``).
3. KERNEL32 ALLOW-LIST — every ``_kernel32.<Member>`` used anywhere in src
   must be inside a reviewed allow-list of non-effect members; any NEW member
   fails until reviewed (kernel32 is a read-only passthrough, so a new member
   would otherwise reach the OS untrapped and unreviewed).

OFFLINE-ONLY (like tests/test_wp_h_hermeticity.py): skipped under the live
gate, where the tripwire is legitimately disarmed.
"""

from __future__ import annotations

import ast
import ctypes
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import desktop_tripwire
from desktop_tripwire import (
    EFFECT_DLL_BINDINGS,
    EFFECT_USER32_NAMES,
    _TripwireDll,
    effect_binding_wrappers,
)

PKG = Path(__file__).resolve().parents[1] / "src" / "computer_use_mcp"

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("CUMCP_RUN_E2E") == "1",
        reason="offline-only tripwire-binding proofs (the live gate disarms the tripwire)",
    )
]

# --- pins ---------------------------------------------------------------------------------

_PINNED_EFFECT_DLL_BINDINGS = frozenset({"_user32", "_kernel32", "_ole32", "_oleaut32"})


def test_effect_dll_bindings_are_pinned() -> None:
    """The wrapped binding-name set is pinned HERE, as a literal (m3 style):
    growing it is a reviewed tripwire change, never an accident."""
    assert EFFECT_DLL_BINDINGS == _PINNED_EFFECT_DLL_BINDINGS
    assert desktop_tripwire._TRIPWIRE_DLL_EFFECTS["_user32"] == desktop_tripwire.EFFECT_USER32_NAMES
    for name in ("_kernel32", "_ole32", "_oleaut32"):
        assert desktop_tripwire._TRIPWIRE_DLL_EFFECTS[name] == frozenset()


# --- the arms (pure helpers so the self-tests can feed synthetic inputs) ------------------


def runtime_binding_violations(modules: list[tuple[str, Any]]) -> list[str]:
    """Arm 1: every EFFECT_DLL_BINDINGS attribute holding a real ctypes DLL
    (``ctypes.CDLL`` — covers WinDLL, OleDLL and CDLL, F11) must be a tripwire
    wrapper. None / test-fake / already-wrapped values pass."""
    violations: list[str] = []
    for dotted, module in modules:
        for name in sorted(EFFECT_DLL_BINDINGS):
            value = getattr(module, name, None)
            if value is None or isinstance(value, _TripwireDll):
                continue
            if not isinstance(value, ctypes.CDLL):
                continue  # a test's own stub (monkeypatch ordering) — not ours to wrap
            violations.append(
                f"{dotted}.{name}: UNTRAPPED real DLL bound at module level "
                "(the tripwire did not wrap this effect object)"
            )
    return violations


#: Module-level DLL bindings outside EFFECT_DLL_BINDINGS that were REVIEWED as
#: non-effect (read-only): any NEW name fails the static arm until it is added
#: here (or to EFFECT_DLL_BINDINGS, with an effect map) after review.
REVIEWED_NON_EFFECT_DLL_BINDINGS: dict[str, str] = {
    "_shcore": "read-only DPI awareness/monitor queries only "
    "(SetProcessDpiAwareness, GetDpiForMonitor); no input/clipboard/window effect",
}

#: Module-level cached member pointers (``_x = _user32.SendMessageW``) that were
#: REVIEWED — empty today; any capture fails the static arm until reviewed (F11).
REVIEWED_EFFECT_MEMBER_CAPTURES: dict[str, str] = {}


def _dotted_chain(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


#: ctypes DLL LOADER singletons/factories — any module-level access to one is a
#: DLL bind the tripwire may not see (F11: cdll/pydll/LibraryLoader/getattr/...)
_LOADER_CHAIN_ROOTS = ("ctypes.windll", "ctypes.oledll", "ctypes.cdll", "ctypes.pydll")
_LOADER_CALLS = ("ctypes.WinDLL", "ctypes.CDLL", "ctypes.OleDLL", "ctypes.LibraryLoader")
_LOADER_SINGLETONS = frozenset({"windll", "oledll", "cdll", "pydll"})


def _contains_dll_loader_access(node: ast.AST) -> bool:
    """True if the expression subtree reaches a ctypes DLL loader in ANY form:
    ``ctypes.windll.<x>`` / ``ctypes.oledll.<x>`` / ``ctypes.cdll`` /
    ``ctypes.pydll`` / ``ctypes.LibraryLoader(...)`` / ``ctypes.WinDLL(...)`` /
    ``ctypes.CDLL(...)`` / ``ctypes.OleDLL(...)`` / ``getattr(ctypes.windll, …)``
    — call-wrapped or conditional expressions included (the subtree walk)."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute):
            chain = _dotted_chain(sub)
            if chain is None:
                continue
            if any(chain == root or chain.startswith(root + ".")
                   for root in _LOADER_CHAIN_ROOTS):
                return True
            if chain == "ctypes.LibraryLoader":
                return True
        elif isinstance(sub, ast.Call):
            func = _dotted_chain(sub.func)
            if func in _LOADER_CALLS:
                return True
            if func == "getattr" and sub.args and _contains_dll_loader_access(sub.args[0]):
                return True
    return False


def _is_effect_member_capture(value: ast.AST) -> bool:
    """``<effect binding>.<member>`` (any depth) — a definition-time cached
    pointer that would bypass every module binding wrapper (F11)."""
    chain = _dotted_chain(value)
    if chain is None:
        return False
    root = chain.split(".", 1)[0]
    return root in EFFECT_DLL_BINDINGS and "." in chain


def _assignment_is_dll_binding(value: ast.AST | None) -> bool:
    """A value that BINDS a ctypes DLL (any loader form, call-wrapped or
    conditional — the subtree walk makes IfExp and wrapped calls visible)."""
    return value is not None and _contains_dll_loader_access(value)


def _module_level_statements(tree: ast.Module) -> list[ast.stmt]:
    """All statements at MODULE level, descending into module-level
    If/Try/With/For/While bodies (their statements still run at import) but
    NOT into function/class bodies."""
    out: list[ast.stmt] = []

    def visit_body(body: list[ast.stmt]) -> None:
        for stmt in body:
            out.append(stmt)
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for group in ("body", "orelse", "finalbody"):
                nested = getattr(stmt, group, None)
                if isinstance(nested, list):
                    visit_body(nested)
            for handler in getattr(stmt, "handlers", []) or []:
                visit_body(handler.body)

    visit_body(tree.body)
    return out


def static_binding_violations(sources: list[tuple[str, str]]) -> list[str]:
    """Arm 2: module-level ctypes DLL binds must use reviewed names, in
    reviewed forms (F11):

    - an assignment/AnnAssign whose value reaches a ctypes loader in any form
      must bind a name in EFFECT_DLL_BINDINGS or the reviewed allow-list;
    - a module-level cached member pointer (``_x = _user32.SendMessageW``)
      fails unless reviewed (REVIEWED_EFFECT_MEMBER_CAPTURES — empty today);
    - ``from <pkg> import _user32 as <alias>`` (an aliased effect binding)
      fails — the tripwire and the guard match the binding NAME;
    - ``from ctypes import windll`` (any loader singleton) fails — the module
      could re-bind DLLs at runtime unseen;
    - any other module-level statement (Expr/For/...) whose subtree reaches a
      ctypes loader (``ctypes.windll.LoadLibrary("x")``) fails.
    """
    violations: list[str] = []
    for dotted, source in sources:
        tree = ast.parse(source, filename=dotted)
        for stmt in _module_level_statements(tree):
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                violations.extend(_import_violations(dotted, stmt))
                continue
            if isinstance(stmt, ast.Assign):
                for target, value in ((t, stmt.value) for t in stmt.targets):
                    violations.extend(_assign_violations(dotted, target, value))
                continue
            if isinstance(stmt, ast.AnnAssign):
                if stmt.value is not None:
                    violations.extend(_assign_violations(dotted, stmt.target, stmt.value))
                continue
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(stmt, (ast.If, ast.Try, ast.With, ast.For, ast.While,
                                 ast.AsyncWith, ast.ExceptHandler)):
                continue  # compound: their nested statements are visited individually
            # every other module-level executable statement: no ctypes loader
            # access may appear outside a reviewed assignment (LoadLibrary, ...)
            if _contains_dll_loader_access(stmt):
                violations.append(
                    f"{dotted}: module-level statement at line {stmt.lineno} reaches a "
                    "ctypes DLL loader outside a reviewed binding assignment"
                )
    return violations


def _assign_violations(dotted: str, target: ast.expr, value: ast.expr) -> list[str]:
    if not _assignment_is_dll_binding(value):
        if _is_effect_member_capture(value) and isinstance(target, ast.Name) \
                and target.id not in REVIEWED_EFFECT_MEMBER_CAPTURES:
            return [
                f"{dotted}: module-level cached member pointer "
                f"'{target.id} = {ast.unparse(value)}' bypasses every binding "
                "wrapper — review and allow-list it or remove it"
            ]
        return []
    names: list[str] = []
    if isinstance(target, ast.Name):
        names = [target.id]
    elif isinstance(target, (ast.Tuple, ast.List)):
        names = [e.id for e in target.elts if isinstance(e, ast.Name)]
    else:
        return []  # a prototype write (_kernel32.OpenProcess.argtypes = [...])
    return [
        f"{dotted}: module-level DLL binding '{name}' is neither in "
        "EFFECT_DLL_BINDINGS nor in the reviewed non-effect allow-list "
        "(tests/test_tripwire_bindings_guard.py) — the tripwire would "
        "not wrap this effect object"
        for name in names
        if name not in EFFECT_DLL_BINDINGS and name not in REVIEWED_NON_EFFECT_DLL_BINDINGS
    ]


def _import_violations(dotted: str, stmt: ast.Import | ast.ImportFrom) -> list[str]:
    out: list[str] = []
    if isinstance(stmt, ast.ImportFrom) and stmt.module == "ctypes":
        for alias in stmt.names:
            if alias.name in _LOADER_SINGLETONS:
                out.append(
                    f"{dotted}: 'from ctypes import {alias.name}' (line "
                    f"{stmt.lineno}) — a loader singleton import lets the module "
                    "re-bind DLLs at runtime unseen; use explicit bindings"
                )
    if isinstance(stmt, ast.ImportFrom):
        for alias in stmt.names:
            if alias.name in EFFECT_DLL_BINDINGS and alias.asname:
                out.append(
                    f"{dotted}: 'from {stmt.module} import {alias.name} as "
                    f"{alias.asname}' (line {stmt.lineno}) — an ALIASED effect "
                    "binding escapes the tripwire and this guard, which match "
                    "the binding name"
                )
    return out


def _member_uses(sources: list[tuple[str, str]], binding: str) -> dict[str, set[str]]:
    """{member: {module files}} for every ``<binding>.<Member>`` attribute use
    in the given sources (prototype configs included, via the nested Attribute
    node), anywhere in the module."""
    used: dict[str, set[str]] = {}
    for dotted, source in sources:
        for node in ast.walk(ast.parse(source, filename=dotted)):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == binding
            ):
                used.setdefault(node.attr, set()).add(dotted)
    return used


def member_use_violations(
    sources: list[tuple[str, str]], binding: str, allowlist: dict[str, str]
) -> list[str]:
    """Arm 3 (kernel32) / arm 4 (user32, F12): every ``<binding>.<Member>`` use
    in src must be inside the member's reviewed allow-list; a NEW member fails
    until reviewed."""
    return [
        f"{binding}.{name}: NOT in the reviewed member allow-list (used in "
        f"{', '.join(sorted(files))})"
        for name, files in sorted(_member_uses(sources, binding).items())
        if name not in allowlist
    ]


def stale_member_entries(
    sources: list[tuple[str, str]], binding: str, allowlist: dict[str, str],
    pinned: frozenset[str] = frozenset(),
) -> list[str]:
    """Fail-closed bookkeeping (F12): every REVIEWED entry must still be used
    by the tree (the pinned effect members are exempt — they are the trap
    surface, not usage records). A stale entry is removed, not hoarded."""
    used = set(_member_uses(sources, binding))
    return sorted(
        f"{binding}.{name}: stale allow-list entry — no longer used anywhere in src"
        for name in allowlist
        if name not in pinned and name not in used
    )


#: Every ``_kernel32.<Member>`` reviewed as a NON-effect member (the tripwire
#: passes kernel32 through read-only: process-local memory/handle/query
#: plumbing; every desktop-reaching effect rides user32). One-line reason each.
#: Any member used in src but missing here FAILS the guard until reviewed.
KERNEL32_MEMBER_ALLOWLIST: dict[str, str] = {
    "OpenProcess": "opens a PROCESS_QUERY_LIMITED_INFORMATION handle for the "
    "process image path (a read-only query; no window/input/clipboard effect)",
    "QueryFullProcessImageNameW": "reads the image path of an opened process "
    "handle (read-only query)",
    "CloseHandle": "closes a kernel handle we opened (cleanup plumbing)",
    "GlobalAlloc": "allocates movable memory for a clipboard WRITE payload "
    "(process-local memory; the desktop effect is OpenClipboard/SetClipboardData, "
    "trapped on the user32 binding)",
    "GlobalLock": "locks GlobalAlloc memory to fill/read the clipboard payload "
    "(process-local memory)",
    "GlobalUnlock": "unlocks GlobalAlloc memory (process-local memory)",
    "GlobalFree": "frees GlobalAlloc memory (process-local memory)",
    "GetCurrentThreadId": "reads the current thread id for AttachThreadInput "
    "focus plumbing (read-only query; the AttachThreadInput effect itself is a "
    "trapped user32 member)",
}

#: The pinned 11 user32 EFFECT members (the trap surface) — the reviewed
#: read-only remainder of what src uses lives in
#: USER32_MEMBER_ALLOWLIST below (F12, VAL-WP-R2a-0-01).
_REVIEWED_READONLY_USER32_MEMBERS: dict[str, str] = {
    "GetForegroundWindow": "read-only query (foreground hwnd)",
    "GetAncestor": "read-only query (window ancestor chain)",
    "IsWindow": "read-only query (hwnd validity)",
    "GetWindowThreadProcessId": "read-only query (owning pid/thread)",
    "GetWindow": "read-only query (related window handle)",
    "IsWindowVisible": "read-only query (visibility style)",
    "GetGUIThreadInfo": "read-only query (GUI thread state / focus)",
    "CloseClipboard": "closes a clipboard WE opened (ownership pair of "
    "OpenClipboard — the effect is the open/empty/set trio, all trapped)",
    "IsClipboardFormatAvailable": "read-only clipboard FORMAT query (no data)",
    "GetClipboardData": "clipboard READ (F6a: read-only by review — content "
    "leaves the desktop, never mutates it; the write trio is trapped)",
    "GetWindowTextLengthW": "read-only query (title length)",
    "GetWindowRect": "read-only query (window rectangle)",
    "GetMonitorInfoW": "read-only query (monitor geometry)",
    "GetCursorPos": "read-only query (cursor position)",
    "GetAsyncKeyState": "read-only query (key state)",
    "EnumDisplayMonitors": "read-only enumeration (monitors)",
    "SetProcessDpiAwarenessContext": "sets THIS process's DPI awareness mode "
    "(process-local state; no window/input effect)",
    "SetProcessDPIAware": "sets THIS process's DPI awareness (process-local)",
    "GetWindowTextW": "read-only query (window title text)",
    "GetClassNameW": "read-only query (window class name)",
    "GetAwarenessFromDpiAwarenessContext": "read-only query (DPI awareness value)",
    "EnumWindows": "read-only enumeration (top-level windows)",
    "VkKeyScanW": "read-only keyboard-layout query (char -> virtual key)",
    "SendMessageTimeoutW": "sends a query message with SMTO_ABORTIFHUNG and a "
    "timeout (read-only text/class retrieval via WM_GETTEXT/WM_GETTEXTLENGTH — "
    "reviewed non-effect query form)",
    "IsIconic": "read-only query (minimized state)",
    "GetThreadDpiAwarenessContext": "read-only query (thread DPI context)",
    "GetDpiForSystem": "read-only query (system DPI)",
    "CountClipboardFormats": "read-only clipboard query (format count only)",
}

#: user32 member allow-list = the pinned effect members (the trap surface) ∪
#: the reviewed read-only members (one-line reason each, F12). Any
#: ``_user32.<Member>`` used in src outside this fails the guard.
USER32_MEMBER_ALLOWLIST: dict[str, str] = {
    name: f"pinned EFFECT member (trapped by the tripwire; trip label "
    f"_user32.{name})"
    for name in sorted(EFFECT_USER32_NAMES)
}
USER32_MEMBER_ALLOWLIST.update(_REVIEWED_READONLY_USER32_MEMBERS)


def _package_sources() -> list[tuple[str, str]]:
    return [
        (path.relative_to(PKG).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(PKG.rglob("*.py"))
    ]


# --- the live guards ----------------------------------------------------------------------


def _loaded_package_modules() -> list[tuple[str, ModuleType]]:
    return sorted(
        (name, module)
        for name, module in sys.modules.items()
        if name.startswith("computer_use_mcp") and isinstance(module, ModuleType)
    )


def test_runtime_arm_every_loaded_effect_binding_is_wrapped() -> None:
    """After the autouse fixture armed the tripwire, no loaded
    ``computer_use_mcp.*`` module may hold an untrapped real WinDLL under an
    EFFECT_DLL_BINDINGS name — the fresh-backend probe's trap proves the same
    helper covers freshly exec'd modules."""
    assert runtime_binding_violations(_loaded_package_modules()) == []


def test_fresh_module_bindings_are_all_wrapped() -> None:
    """N2 generalized: the fresh exec's PRIVATE effect-DLL bindings (_user32
    AND _kernel32/_ole32/_oleaut32) are wrapped when the tripwire is armed."""
    from fresh_backend_import import fresh_backend

    module = fresh_backend({})
    violations = runtime_binding_violations(
        [("computer_use_mcp._cortex_backend_fresh_probe", module)]
    )
    assert violations == []
    kernel32 = getattr(module, "_kernel32", None)
    assert isinstance(kernel32, _TripwireDll), (
        "the fresh module's _kernel32 is UNTRAPPED while the tripwire is armed"
    )


def test_static_arm_only_allows_reviewed_dll_bindings() -> None:
    sources = _package_sources()
    assert static_binding_violations(sources) == []


def test_kernel32_members_are_inside_the_reviewed_allowlist() -> None:
    assert member_use_violations(_package_sources(), "_kernel32", KERNEL32_MEMBER_ALLOWLIST) == []


def test_user32_members_are_inside_the_reviewed_allowlist() -> None:
    """F12 (VAL-WP-R2a-0-01): every ``_user32.<Member>`` used in src is the
    pinned effect surface or a reviewed read-only member — a new member
    (SetWindowPos, BlockInput, PostMessageW, ...) fails until reviewed."""
    assert member_use_violations(_package_sources(), "_user32", USER32_MEMBER_ALLOWLIST) == []


def test_reviewed_member_allowlists_have_no_stale_entries() -> None:
    """F12: stale-entry check — every reviewed read-only entry must still be
    used somewhere in src (the pinned effect members are the trap surface and
    are exempt)."""
    sources = _package_sources()
    assert stale_member_entries(sources, "_kernel32", KERNEL32_MEMBER_ALLOWLIST) == []
    assert stale_member_entries(
        sources, "_user32", USER32_MEMBER_ALLOWLIST, pinned=EFFECT_USER32_NAMES
    ) == []
    # self-check of the stale check itself: an unused entry is reported
    fake = dict(KERNEL32_MEMBER_ALLOWLIST)
    fake["Beep"] = "synthetic stale entry"
    assert "_kernel32.Beep" in stale_member_entries(sources, "_kernel32", fake)[0]


def test_the_reviewed_binding_allowlist_covers_what_the_tree_binds() -> None:
    """Fail-closed bookkeeping: the reviewed BINDING allow-list must stay
    MINIMAL — every entry must still be exercised by the tree (a stale entry
    is removed, not hoarded)."""
    sources = _package_sources()
    bound_names: set[str] = set()
    for _dotted, source in sources:
        tree = ast.parse(source)
        for stmt in _module_level_statements(tree):
            if isinstance(stmt, ast.Assign):
                pairs = [(t, stmt.value) for t in stmt.targets]
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                pairs = [(stmt.target, stmt.value)]
            else:
                continue
            for target, value in pairs:
                if isinstance(target, ast.Name) and _assignment_is_dll_binding(value):
                    bound_names.add(target.id)
    assert bound_names - EFFECT_DLL_BINDINGS == set(REVIEWED_NON_EFFECT_DLL_BINDINGS)


def test_selftest_shared_wrapper_is_one_object_per_real_dll() -> None:
    """F6a (VAL-WP-R2a-0-01): ONE wrapper per real DLL, installed on EVERY
    module binding of that DLL — a member patch through any binding is seen
    through all of them, and teardown restores all bindings. Uses an ISOLATED
    MonkeyPatch so the autouse fixture's own wrappers are untouched."""
    real = ctypes.WinDLL("user32")  # created; NOTHING called through it
    mod_a = _synthetic_module("computer_use_mcp._guard_shared_a", _user32=real)
    mod_b = _synthetic_module("computer_use_mcp._guard_shared_b", _user32=real)
    mp = pytest.MonkeyPatch()
    mp.setitem(sys.modules, "computer_use_mcp._guard_shared_a", mod_a)
    mp.setitem(sys.modules, "computer_use_mcp._guard_shared_b", mod_b)

    wrapped = desktop_tripwire.wrap_loaded_effect_bindings(mp, "shared-wrapper-node")
    assert "computer_use_mcp._guard_shared_a._user32" in wrapped
    assert "computer_use_mcp._guard_shared_b._user32" in wrapped
    # the identical wrapper object on both bindings:
    assert mod_a._user32 is mod_b._user32
    assert isinstance(mod_a._user32, _TripwireDll)
    # a member patch via ONE binding is visible via the OTHER:
    sentinel = object()
    mp.setattr(mod_a._user32, "GetForegroundWindow", sentinel, raising=False)
    assert mod_b._user32.GetForegroundWindow is sentinel
    # teardown (the isolated patch's undo) restores BOTH bindings to the real DLL:
    mp.undo()
    assert mod_a._user32 is real and mod_b._user32 is real


def test_selftest_teardown_rescan_fails_on_a_late_unwrapped_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F13: a module FIRST imported after install (bound to a real DLL mid-test)
    is caught by the teardown re-scan."""
    real = ctypes.WinDLL("kernel32")  # created; NOTHING called through it
    late = _synthetic_module("computer_use_mcp._guard_late_probe", _kernel32=real)
    monkeypatch.setitem(sys.modules, "computer_use_mcp._guard_late_probe", late)
    violations = desktop_tripwire.find_unwrapped_effect_bindings()
    assert any("_guard_late_probe._kernel32" in v and "UNWRAPPED" in v for v in violations)
    # once the shared helper wraps it, the re-scan is clean:
    from desktop_tripwire import effect_binding_wrappers as _ebw

    for name, wrapper in _ebw(late, "late-node"):
        setattr(late, name, wrapper)
    assert desktop_tripwire.find_unwrapped_effect_bindings() == []


# --- self-tests: each arm FAILS on a synthetic violation ---------------------------------


def _synthetic_module(name: str, **attrs: Any) -> ModuleType:
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def test_selftest_runtime_arm_fails_on_an_untrapped_binding() -> None:
    real = ctypes.WinDLL("kernel32")  # an instance is created; NOTHING is called
    bad = _synthetic_module("computer_use_mcp._guard_selftest_untrapped", _kernel32=real)
    violations = runtime_binding_violations([("computer_use_mcp._guard_selftest_untrapped", bad)])
    assert len(violations) == 1
    assert "_guard_selftest_untrapped._kernel32" in violations[0]
    assert "UNTRAPPED" in violations[0]
    # ... and the same module with the shared-helper wrapper applied is clean:
    module = _synthetic_module("computer_use_mcp._guard_selftest_wrapped", _kernel32=real)
    for name, wrapper in effect_binding_wrappers(module, "selftest-node"):
        setattr(module, name, wrapper)
    assert runtime_binding_violations([("computer_use_mcp._guard_selftest_wrapped", module)]) == []


def test_selftest_runtime_arm_ignores_none_and_fakes() -> None:
    none_module = _synthetic_module("computer_use_mcp._guard_selftest_none", _ole32=None)
    fake_module = _synthetic_module(
        "computer_use_mcp._guard_selftest_fake", _user32=object()
    )
    assert runtime_binding_violations(
        [("computer_use_mcp._guard_selftest_none", none_module),
         ("computer_use_mcp._guard_selftest_fake", fake_module)]
    ) == []


def test_selftest_static_arm_fails_on_a_new_binding_name() -> None:
    caught = [
        "_gdi32 = ctypes.windll.gdi32\n",
        "_gdi32 = ctypes.WinDLL('gdi32')\n",
        "_services = ctypes.oledll.oleaut32\n",
        "_libc = ctypes.CDLL('libc.so.6')\n",
        "if IS_WINDOWS:\n    _gdi32 = ctypes.windll.gdi32\n",  # nested in module-level If
    ]
    violations = static_binding_violations([("synthetic.py", src) for src in caught[:5]])
    assert len(violations) == 5
    assert all("'_gdi32'" in v or "'_services'" in v or "'_libc'" in v for v in violations)
    # the compliant forms stay clean: effect names + the reviewed _shcore, and a
    # FUNCTION-LOCAL bind (dxgi capture path) is out of the arm's scope:
    clean = (
        "_user32 = ctypes.windll.user32\n"
        "_kernel32 = ctypes.WinDLL('kernel32')\n"
        "_shcore = ctypes.windll.shcore\n"
        "def _probe():\n"
        "    dxgi = ctypes.windll.dxgi\n"
        "    return dxgi\n"
    )
    assert static_binding_violations([("synthetic.py", clean)]) == []


def test_selftest_static_arm_fails_on_the_F11_evasion_forms() -> None:
    cases: list[tuple[str, str]] = [
        ("aliased effect binding", "from computer_use_mcp import win32 as _w\n"
         "from computer_use_mcp.win32 import _user32 as u32\n"),
        ("from ctypes import windll", "from ctypes import windll\n"),
        ("from ctypes import oledll", "from ctypes import oledll as od\n"),
        ("module-level cached member pointer", "SendMessageW = _user32.SendMessageW\n"),
        ("getattr loader", "_gdi32 = getattr(ctypes.windll, 'gdi32')\n"),
        ("cdll loader", "_m = ctypes.cdll.msvcrt\n"),
        ("pydll loader", "_m = ctypes.pydll\n"),
        ("LibraryLoader", "_ll = ctypes.LibraryLoader(ctypes.WinDLL)\n"),
        ("windll.LoadLibrary statement", "ctypes.windll.LoadLibrary('gdi32.dll')\n"),
        ("IfExp-wrapped bind", "_dll = ctypes.windll.gdi32 if True else None\n"),
        ("OleDLL call", "_o = ctypes.OleDLL('oleaut32')\n"),
    ]
    for label, source in cases:
        violations = static_binding_violations([("synthetic.py", source)])
        assert violations, f"F11 evasion NOT caught ({label}): {source!r}"
    # the aliased-binding case must name the escape explicitly:
    assert any("as u32" in v for v in static_binding_violations(
        [("synthetic.py", cases[0][1])]))
    # CDLL-family runtime values are wrapped too (F11): an OleDLL instance is a
    # ctypes.CDLL, so the runtime arm flags it and the wrapper accepts it:
    ole = ctypes.OleDLL("oleaut32")  # created; NOTHING called through it
    bad = _synthetic_module("computer_use_mcp._guard_selftest_ole", _oleaut32=ole)
    assert runtime_binding_violations(
        [("computer_use_mcp._guard_selftest_ole", bad)]
    ), "the runtime arm must flag a non-WinDLL (OleDLL/CDLL-family) binding"
    for name, wrapper in effect_binding_wrappers(bad, "ole-node"):
        setattr(bad, name, wrapper)
    assert runtime_binding_violations([("computer_use_mcp._guard_selftest_ole", bad)]) == []


def test_selftest_member_allowlist_fails_on_an_unreviewed_member() -> None:
    # kernel32:
    violations = member_use_violations(
        [("synthetic.py", "_kernel32.Beep(440, 100)\n")], "_kernel32", KERNEL32_MEMBER_ALLOWLIST
    )
    assert len(violations) == 1 and "_kernel32.Beep" in violations[0]
    # user32 (F12): an unreviewed EFFECT-shaped member fails:
    violations = member_use_violations(
        [("synthetic.py", "_user32.PostMessageW(0, 0, 0, 0)\n")], "_user32", USER32_MEMBER_ALLOWLIST
    )
    assert len(violations) == 1 and "_user32.PostMessageW" in violations[0]
    # every reviewed member stays clean (both bindings):
    for binding, allowlist in (("_kernel32", KERNEL32_MEMBER_ALLOWLIST),
                               ("_user32", USER32_MEMBER_ALLOWLIST)):
        reviewed_calls = "\n".join(
            f"{binding}.{member}(1, 2)\n" for member in sorted(allowlist)
        )
        assert member_use_violations([("synthetic.py", reviewed_calls)], binding, allowlist) == []
