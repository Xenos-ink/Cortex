"""E2E suite configuration: marker gate, evidence capture, per-test deadlines.

Skip doctrine (charter): every test marked ``e2e`` runs REAL applications on the live
desktop and is therefore skipped unless ``CUMCP_RUN_E2E=1``. The standard suite
(``pytest tests/ -q``) stays green and deterministic without a desktop. The marker is
registered here (pyproject is not edited) via ``pytest_configure``.

Evidence doctrine (charter / master-mission 9-J): every e2e test produces, under
``evidence/e2e/<test_name>/``:

- ``observation_before.json`` / ``observation_after.json`` — full observation dumps
  (window identity incl. HWND/PID/exe = P0-G proof; screenshots saved separately);
- ``screenshot_before.png`` / ``screenshot_after.png`` — downscaled visual proof;
- ``audit_excerpt.jsonl`` — the session's structured audit events;
- ``transcript.md`` — human-readable run record (actions, verification outcomes,
  assertions, environment notes) written by the test itself;
- ``result.json`` — machine record incl. the pytest outcome, captured via the
  ``pytest_runtest_makereport`` hook below.

``runs.md`` at the evidence root gets one appended summary block per run.

Import note: pytest imports this directory's modules root-less (no ``__init__.py`` in
``tests/``), so the helpers are imported as top-level modules after an explicit
``sys.path`` insertion of this directory.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

E2E_DIR = Path(__file__).resolve().parent
REPO_ROOT = E2E_DIR.parent.parent
EVIDENCE_ROOT = REPO_ROOT / "evidence" / "e2e"
SCRATCH_ROOT = E2E_DIR / "scratch"
RUN_ID = os.getenv("CUMCP_E2E_RUN_ID", datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
TEST_TIMEOUT_S = float(os.getenv("CUMCP_E2E_TEST_TIMEOUT", "240"))

if str(E2E_DIR) not in sys.path:
    sys.path.insert(0, str(E2E_DIR))

import helpers_win32 as w32


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "e2e: runs real Windows applications on the live desktop (needs CUMCP_RUN_E2E=1)"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("CUMCP_RUN_E2E") == "1":
        return
    skip = pytest.mark.skip(
        reason="e2e test drives real Windows applications; set CUMCP_RUN_E2E=1 to enable"
    )
    for item in items:
        # NOTE: keyword membership would also match the PATH component "e2e" (pytest adds
        # path parts as keywords); the gate must test the registered marker itself.
        if item.get_closest_marker("e2e") is not None:
            item.add_marker(skip)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]):
    """Attach the report to the item so the evidence fixture can record the outcome."""
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


@pytest.fixture
def deadline() -> w32.Deadline:
    """Per-test watchdog: every wait helper fails fast past this deadline (no hangs)."""
    return w32.Deadline(TEST_TIMEOUT_S)


@pytest.fixture(autouse=True)
def e2e_dpi_awareness(request: pytest.FixtureRequest) -> Any:
    """Lazily opt e2e-marked tests into per-monitor-v2 awareness (E6 finding D10).

    Importing helpers_win32 must stay side-effect-free: a process-global awareness set
    at collection time downgrades ``LocalComputerBackend``'s own awareness resolution
    and breaks the standard suite's ``dpi_estimated is False`` assertion in mixed runs.
    Only e2e-marked tests (which drive real desktop windows) opt in, here at fixture
    setup — before any rect read. The call is an idempotent no-op when the runtime
    backend already set awareness.
    """
    if request.node.get_closest_marker("e2e") is not None:
        w32.ensure_dpi_awareness()
    yield


@pytest.fixture(autouse=True)
def e2e_scratch(request: pytest.FixtureRequest) -> Any:
    """Provide tests/e2e/scratch/<test>/ for files created by the test (cleaned up)."""
    if request.node.get_closest_marker("e2e") is None:
        yield None
        return
    test_dir = SCRATCH_ROOT / request.node.name
    test_dir.mkdir(parents=True, exist_ok=True)
    yield test_dir
    if test_dir.exists():
        shutil.rmtree(test_dir, ignore_errors=True)


class Evidence:
    """Per-test evidence recorder (see module docstring for the artifact layout)."""

    def __init__(self, test_name: str) -> None:
        self.test_name = test_name
        self.dir = EVIDENCE_ROOT / test_name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events: list[dict[str, Any]] = []
        self.assertions: list[str] = []
        self.notes: list[str] = []
        self.extra: dict[str, Any] = {}
        self.started = time.perf_counter()

    def note(self, message: str) -> None:
        self.notes.append(message)

    def assert_that(self, description: str, ok: bool, detail: Any = None) -> bool:
        """Record an assertion with its outcome (the test still asserts normally)."""
        suffix = f" — {detail}" if detail is not None else ""
        self.assertions.append(f"[{'PASS' if ok else 'FAIL'}] {description}{suffix}")
        return ok

    def record(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, "at": datetime.now(UTC).isoformat(), **fields})

    def add_extra(self, key: str, value: Any) -> None:
        self.extra[key] = value

    def save_observation(self, when: str, observation: dict[str, Any]) -> None:
        """Save an observation dump (no image) + downscaled screenshot next to it."""
        dump = {key: value for key, value in observation.items() if key != "image_base64"}
        (self.dir / f"observation_{when}.json").write_text(
            json.dumps(dump, indent=2, default=str), encoding="utf-8"
        )
        encoded = observation.get("image_base64")
        if not encoded:
            return
        try:
            from PIL import Image

            image = Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGB")
            scale = 800 / max(image.width, 1)
            small = image.resize((800, max(round(image.height * scale), 1)))
            small.save(self.dir / f"screenshot_{when}.png")
        except Exception as exc:  # noqa: BLE001 - screenshots are best-effort evidence
            self.note(f"screenshot_{when} could not be saved: {type(exc).__name__}: {exc}")

    def save_audit(self, bundle: Any, session_id: str, max_lines: int = 400) -> None:
        path = bundle.auditor.path_for(session_id)
        if not path.exists():
            self.note(f"audit log missing for session {session_id}")
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        (self.dir / "audit_excerpt.jsonl").write_text(
            "\n".join(lines[:max_lines]) + "\n", encoding="utf-8"
        )
        self.add_extra("audit_event_count", len(lines))

    def audit_events(self, bundle: Any, session_id: str) -> list[dict[str, Any]]:
        path = bundle.auditor.path_for(session_id)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def write(self, outcome: str, error: str | None = None) -> None:
        elapsed_ms = (time.perf_counter() - self.started) * 1000.0
        result = {
            "test": self.test_name,
            "run_id": RUN_ID,
            "outcome": outcome,
            "elapsed_ms": round(elapsed_ms, 1),
            "error": error,
            "assertions": self.assertions,
            "notes": self.notes,
            "recorded_events": self.events,
            "extra": self.extra,
            "finished_at": datetime.now(UTC).isoformat(),
        }
        (self.dir / "result.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        assertion_lines = [f"- {item}" for item in self.assertions] or ["- (none recorded)"]
        note_lines = [f"- {item}" for item in self.notes] or ["- (none)"]
        event_lines = [f"- `{json.dumps(event, default=str)}`" for event in self.events] or ["- (none)"]
        lines = [
            f"# E2E transcript — {self.test_name}",
            "",
            f"- run_id: `{RUN_ID}`",
            f"- outcome: **{outcome}**" + (f" — `{error}`" if error else ""),
            f"- elapsed: {elapsed_ms:.0f} ms",
            "",
            "## Assertions",
            *assertion_lines,
            "",
            "## Notes",
            *note_lines,
            "",
            "## Recorded events",
            *event_lines,
            "",
        ]
        (self.dir / "transcript.md").write_text("\n".join(lines), encoding="utf-8")
        with (EVIDENCE_ROOT / "runs.md").open("a", encoding="utf-8") as sink:
            suffix = f" | error: {error}" if error else ""
            sink.write(f"- {RUN_ID} | {self.test_name} | {outcome}{suffix} | {elapsed_ms:.0f} ms\n")


@pytest.fixture
def evidence(request: pytest.FixtureRequest) -> Any:
    """Evidence recorder bound to the current test; writes artifacts at teardown."""
    if request.node.get_closest_marker("e2e") is None:
        yield None
        return
    writer = Evidence(request.node.name)
    yield writer
    report = getattr(request.node, "rep_call", None) or getattr(request.node, "rep_setup", None)
    if report is None:
        writer.write("unknown")
    elif report.passed:
        writer.write("passed")
    elif report.skipped:
        writer.write("skipped", report.longreprtext[:500] if report.longrepr else None)
    else:
        writer.write("failed", (report.longreprtext if report.longrepr else "")[-2000:])


# --- server-surface session helper -----------------------------------------------------------


@pytest.fixture
def make_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh isolated server session wiring per test, with the REAL backend default.

    Returns a factory ``make_session(backend=None, provider=None, **start_kwargs)`` that
    injects the given backend/provider through the documented test seam
    (``server._backend_factory`` / ``server._provider_factory``), starts the session via
    the ``start_session`` tool, and returns ``(session_id, bundle)``.
    """
    from computer_use_mcp import server
    from computer_use_mcp.backend import LocalComputerBackend
    from computer_use_mcp.state import SessionRegistry

    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})

    def factory(backend: Any = None, provider: Any = None, **start_kwargs: Any) -> tuple[str, Any]:
        monkeypatch.setattr(server, "_backend_factory", lambda: backend or LocalComputerBackend())
        monkeypatch.setattr(server, "_provider_factory", lambda: provider)
        response = server.start_session(**start_kwargs)
        assert response.get("session_id"), response
        session_id = str(response["session_id"])
        bundle = server._get_bundle(session_id)
        return session_id, bundle

    return factory


@pytest.fixture
def with_verifier():
    """Return a callable that injects extra verification strategies into a session agent.

    The strategies extend (not replace) the default chain: real-window strategies come
    first so their definitive outcomes win over pixel-diff ambiguity.
    """

    def inject(session_id: str, bundle: Any, *strategies: Any) -> None:
        from computer_use_mcp.verification import VerificationEngine, default_strategy_chain

        bundle.agent.verifier = VerificationEngine(
            strategies=[*strategies, *default_strategy_chain(None)]
        )

    return inject
