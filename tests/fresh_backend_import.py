"""Fresh in-process import of ``computer_use_mcp.backend`` under a private alias.

WP-H DELTA 1 (m2): the offline suite no longer spawns child interpreters — the
launch tripwire rejects ``python -c`` children — so the import-time env-parsing
proofs (motion bands, settle policy, ...) run the backend module FRESH in-process
instead. ``exec_module`` re-runs ``backend.py`` top-to-bottom exactly as a fresh
interpreter would (module-level constants parsed from the controlled environment),
without any subprocess.

Safety notes (for the WP-H audit):
- the probe module is NEVER inserted into ``sys.modules`` (private alias), so the
  installed ``computer_use_mcp.backend`` and every other test stay untouched;
- backend.py's module level only DEFINES things and binds ctypes prototypes — it
  performs no desktop effect, so the fresh exec cannot act on the desktop and the
  trip log stays empty;
- the controlled environment is applied to ``os.environ`` and restored in a
  ``finally`` (the child-probe env semantics, minus the process boundary).
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import computer_use_mcp.backend as _installed_backend

_BACKEND_PATH = Path(_installed_backend.__file__).resolve()
#: A submodule name INSIDE the real package so backend.py's relative imports
#: (``from .models import ...``) resolve against the installed package.
_PROBE_ALIAS = "computer_use_mcp._cortex_backend_fresh_probe"


class _PrivateDllLoader:
    """``ctypes.windll`` stand-in handing out PRIVATE WinDLL instances.

    The fresh exec re-runs backend.py's ctypes prototype bindings; they must land on
    private DLL instances, never on the SHARED ``ctypes.windll.user32`` the running
    backend uses — otherwise the new module's fresh ``POINTER(...)`` classes would
    replace the prototypes the installed backend's call sites depend on.
    """

    def __init__(self) -> None:
        self._cache: dict[str, ctypes.WinDLL] = {}

    def __getattr__(self, name: str) -> ctypes.WinDLL:
        cached = self.__dict__["_cache"]
        if name not in cached:
            cached[name] = ctypes.WinDLL(name)
        return cached[name]


class _PrivateWindll:
    """Swap ``ctypes.windll`` for private DLLs for exactly one exec (restored after)."""

    def __enter__(self) -> None:
        self._saved = ctypes.windll
        ctypes.windll = _PrivateDllLoader()  # type: ignore[assignment]

    def __exit__(self, *exc_info: object) -> None:
        ctypes.windll = self._saved  # type: ignore[assignment]


class _ControlledEnv:
    """``os.environ`` minus ambient ``CORTEX_*`` keys, plus explicit overrides."""

    def __init__(self, overrides: dict[str, str | None]) -> None:
        self._overrides = overrides
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> None:
        for key in [k for k in os.environ if k.startswith("CORTEX_")]:
            self._saved[key] = os.environ.pop(key)
        for key, value in self._overrides.items():
            # WP-H N1 (VAL-WP-H-02): an override key that is ALSO an ambient
            # ``CORTEX_*`` key was already saved by the strip above — re-saving
            # here overwrote the ambient value with ``None`` and the restore
            # DELETED it. ``setdefault`` keeps the first (ambient) save.
            self._saved.setdefault(key, os.environ.get(key))
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def __exit__(self, *exc_info: object) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def fresh_backend(env_overrides: dict[str, str | None] | None = None) -> ModuleType:
    """Exec ``backend.py`` fresh under a private alias with a controlled env.

    ``env_overrides`` maps ``CORTEX_*`` (or any) env name to its value; ``None``
    removes the key. Ambient ``CORTEX_*`` variables are stripped first (the
    dev-desktop hygiene the former child probes implemented via a clean child env).
    """
    with _ControlledEnv(env_overrides or {}):
        spec = importlib.util.spec_from_file_location(_PROBE_ALIAS, _BACKEND_PATH)
        if spec is None or spec.loader is None:  # pragma: no cover - never on a real tree
            raise RuntimeError(f"cannot load backend source at {_BACKEND_PATH}")
        module = importlib.util.module_from_spec(spec)
        # Required DURING exec: dataclasses.resolve_annots looks the executing module
        # up in sys.modules (backend.py:755). Registered under the private alias only
        # for the exec, removed afterwards so the installed package stays untouched.
        sys.modules[_PROBE_ALIAS] = module
        try:
            with _PrivateWindll():
                spec.loader.exec_module(module)
        finally:
            sys.modules.pop(_PROBE_ALIAS, None)
    _trap_fresh_module_user32(module)
    return module


def _tripwire_user32() -> Any:
    """The installed backend's ``_user32`` WHEN the desktop tripwire is armed."""
    installed = getattr(_installed_backend, "_user32", None)
    if getattr(installed, "_cortex_tripwire", False):
        return installed
    return None


def _trap_fresh_module_user32(module: ModuleType) -> None:
    """WP-H N2 (VAL-WP-H-02): the fresh module's PRIVATE ``_user32`` is untrapped.

    The exec binds its own ``WinDLL("user32")`` instances; the tripwire only
    patches the INSTALLED ``backend_module._user32``. While today's fresh-exec
    consumers only evaluate module-level CONSTANTS, any future expression that
    called an effect function through the fresh module would act untrapped.
    When the tripwire is armed, replace the fresh module's ``_user32`` with a
    tripwire wrapper over it (fresh trap instances, same node id).
    """
    armed = _tripwire_user32()
    if armed is None:
        return
    fresh_user32 = getattr(module, "_user32", None)
    if fresh_user32 is None:
        return
    from desktop_tripwire import _TripwireUser32

    module._user32 = _TripwireUser32(fresh_user32, str(getattr(armed, "_node_id", "fresh_backend_import")))
