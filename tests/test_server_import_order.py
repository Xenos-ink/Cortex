"""WP-R2b step-1 import-order proofs (plan §4, D4, A1/A2).

A1: the intra-package import DAG is ACYCLIC and strictly layered — EVERY
``computer_use_mcp.server`` submodule is importable FIRST, in a clean state that
removes ONLY the ``computer_use_mcp.server`` / ``computer_use_mcp.server.*``
sys.modules entries (never lower layers: re-executing desktop modules would
rebind DLL wrappers the tripwire never wrapped; monkeypatch restores everything).
Same-layer one-way edges (registration-order proof A2 is what makes them safe):
observe → fetch is GONE (D8b: the shared def's home is observe); execute →
observe (``_resolve_view_param``) remains — real-server registration is
unaffected because app.py imports observe BEFORE execute.

A2: the TWELVE tools are registered in exactly the monolith's decorator (source)
order with the WP-R recording tools LAST (server/record.py imported after
computer_fetch_image), proven through the PUBLIC ``list_tools()`` API on a clean-state import of
``computer_use_mcp.server.app`` (registration = decoration at def site; app.py
imports the tool modules in registration order; the installed SDK stores tools
in an insertion-ordered dict — mcp/server/fastmcp/tools/tool_manager.py:28/:72).

Plus the frozen launch paths (plan §2): ``computer_use_mcp.server:main`` (the
D1 console-script shim) and ``computer_use_mcp.server.__main__`` (the
``python -m`` entry) both resolve WITHOUT starting the server.
"""

from __future__ import annotations

import asyncio
import importlib
import runpy
import sys

import pytest

SERVER_PKG = "computer_use_mcp.server"

#: every server submodule, layered bottom-up (plan.md §3-A)
SUBMODULES = (
    "instance",
    "schemas",
    "state",
    "replies",
    "wait_machine",
    "plan_surface",
    "lifecycle",
    "wait",
    "observe",
    "execute",
    "fetch",
    "app",
)

#: the exact monolith registration order (plan.md §4) = tools/list order;
#: WP-R (ORVEX-2026-109): the recording tools register AFTER computer_fetch_image
#: (server/app.py imports ``record`` last).
EXPECTED_TOOL_ORDER = (
    "start_session",
    "stop_session",
    "computer_wait",
    "computer_wait_status",
    "computer_cancel_wait",
    "computer_observe",
    "computer_screenshot",
    "computer_zoom",
    "computer_execute",
    "computer_fetch_image",
    "computer_record",
    "computer_record_result",
)


def _clean_server_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ONLY the server package and its submodules from sys.modules
    (restored by monkeypatch). Lower cu layers stay cached on purpose.

    The CACHED parent package's ``server`` attribute is restored explicitly:
    a fresh import rebinds ``sys.modules['computer_use_mcp'].server`` to the
    throwaway package object, and sys.modules restoration alone would leave the
    cached parent pointing at it after the test."""
    parent = sys.modules.get("computer_use_mcp")
    if parent is not None and hasattr(parent, "server"):
        monkeypatch.setattr(parent, "server", parent.server)
    for key in sorted(list(sys.modules)):
        if key == SERVER_PKG or key.startswith(SERVER_PKG + "."):
            monkeypatch.delitem(sys.modules, key)


@pytest.mark.parametrize("submodule", SUBMODULES)
def test_a1_each_submodule_imports_first_in_a_clean_state(
    submodule: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DAG proof: no partially-initialised dependency — importing the submodule
    FIRST succeeds (each first-import runs on a THROWAWAY FastMCP instance, so
    the partial surface cannot pollute other tests)."""
    _clean_server_modules(monkeypatch)
    module = importlib.import_module(f"{SERVER_PKG}.{submodule}")
    assert module.__name__ == f"{SERVER_PKG}.{submodule}"


def test_a2_tool_registration_order_is_the_monolith_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean-state import of server.app, then the PUBLIC list_tools() must list
    exactly the twelve tools in the old decorator (source) order, with the WP-R
    recording tools registered after computer_fetch_image."""
    _clean_server_modules(monkeypatch)
    app_module = importlib.import_module(f"{SERVER_PKG}.app")
    tools = asyncio.run(app_module.mcp.list_tools())
    names = tuple(tool.name for tool in tools)
    assert names == EXPECTED_TOOL_ORDER, (
        "tools/list order must equal the monolith registration order; "
        f"got {names}"
    )


def test_both_launch_paths_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """``computer_use_mcp.server:main`` is the D1 shim (pyproject console
    scripts) and ``computer_use_mcp.server.__main__`` imports cleanly (the
    ``python -m`` entry written into installed MCP configs). Neither is RUN."""
    _clean_server_modules(monkeypatch)
    package = importlib.import_module(SERVER_PKG)
    assert callable(package.main)
    entry = importlib.import_module(f"{SERVER_PKG}.__main__")
    assert callable(entry.main)
    app_module = importlib.import_module(f"{SERVER_PKG}.app")
    assert callable(app_module.main)
    # the shim delegates to the SAME app entry object
    assert package.main.__module__ == SERVER_PKG


def test_f1_console_shim_delegates_to_app_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1 (VAL-WP-R2b-1-01): the shim must actually CALL app.main — a shim that
    delegates to the wrong function (or returns nothing) fails here. The stub
    replaces ``app.main`` BEFORE the shim module is imported, so the shim's
    ``from .app import main as _main`` binds the stub."""
    _clean_server_modules(monkeypatch)
    app_module = importlib.import_module(f"{SERVER_PKG}.app")
    calls: list[tuple[tuple, dict]] = []

    def sentinel(*args: object, **kwargs: object) -> str:
        calls.append((args, kwargs))
        return "sentinel-result"

    monkeypatch.setattr(app_module, "main", sentinel)
    package = importlib.import_module(SERVER_PKG)
    result = package.main()
    assert calls == [((), {})]
    assert result == "sentinel-result"


def test_f1_python_m_entry_calls_app_main_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1 (VAL-WP-R2b-1-01): ``python -m computer_use_mcp.server`` executes the
    package via ``runpy.run_module("computer_use_mcp.server", run_name="__main__")``
    (CPython runpy._run_module_as_main → for a package, the ``__main__``
    submodule; documented runpy behavior) — the ``if __name__ == "__main__":``
    guard must call ``main()`` EXACTLY ONCE. The stub replaces ``app.main``
    before the run; the freshly-executed ``__main__`` block binds it via
    ``from .app import main``. Deleting the guarded call (calls == []) or calling
    it twice fails here. The real server is never started."""
    _clean_server_modules(monkeypatch)
    app_module = importlib.import_module(f"{SERVER_PKG}.app")
    calls: list[int] = []
    monkeypatch.setattr(app_module, "main", lambda *a, **k: calls.append(1))
    runpy.run_module(SERVER_PKG, run_name="__main__")
    assert calls == [1]


def test_f1_dunder_main_module_form_calls_app_main_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1: the equivalent direct form — run_module on the ``__main__`` submodule
    itself — executes the SAME file and guard (runpy finds ``__main__.py`` either
    way); pinned so a rename/split of the entry module cannot dodge the pin."""
    _clean_server_modules(monkeypatch)
    app_module = importlib.import_module(f"{SERVER_PKG}.app")
    calls: list[int] = []
    monkeypatch.setattr(app_module, "main", lambda *a, **k: calls.append(1))
    runpy.run_module(f"{SERVER_PKG}.__main__", run_name="__main__")
    assert calls == [1]
