"""WP6 (ORVEX-2026-108) — the cancellable background wait family, ported from the
dev tree's ORVEX-2026-102 §6 Phase 1 suite and ADAPTED to the blocking design.

The dev wait was fire-and-forget: ``computer_wait`` returned a handle immediately
and nothing ever told the agent when the wait ended (finding S-02 — exactly what
the user rejected in the Addendum 2 follow-up). In 0.7.6 the tools BLOCK, in
segments that never trip the MCP client's 60 s tool timeout, until the agent is
TOLD the wait is over:

* ``computer_wait(duration_seconds)`` blocks up to one segment (env
  ``CORTEX_WAIT_SEGMENT_SECONDS``, default 45, band 1..55, fallback-not-clamp) and
  returns ``completed`` (wait over) or ``running`` (+ ``wait_id``);
* ``computer_wait_status(session_id, wait_id)`` blocks the NEXT segment the same
  way (``block=false`` peeks without blocking) — decision D-1, the S-02 consumer;
* ``computer_cancel_wait`` ends a wait early and wakes a blocked caller in < 1 s.

Invariants carried over from dev, still defended here:

* **K-5 / C7 / A-1** — cancel flags set UNDER the module ``_lock``; joins OUTSIDE it
  (``test_the_join_must_never_happen_under_the_module_lock`` reproduces the v1
  hazard and pins the shipped path completing inside a hard deadline).
* **K-6 / A-3** — the bounded per-session tombstone ring makes ``completed`` and
  ``already_cancelled`` answerable after the live entry is gone.
* **R2 / A-6** — ``wait_id`` lookup is session-scoped; foreign and unknown are
  byte-identical.
* **K-2 / A-5 / OQ-5** — a background wait consumes ZERO ``max_actions`` slots.
* **K-1 / A-2 / C1** — ``max_task_seconds`` stays advertised-but-unenforced.
* **R17 / K-8** — the advertised tool surface cannot silently drift.
* **S-15 (fixed during the port)** — caps are checked BEFORE ``setdefault``, so a
  refused wait leaves no empty per-session dict.

RECORDS (behavior changes vs dev, all intentional):
* The wait tools are now ASYNC coroutines (dev pinned them sync — the blocking
  segments REQUIRE awaiting; a sync tool would block the event loop).
* Plan-lease refusals for wait/cancel (dev ``plan_lease``) are a **WP7 seam**: the
  plan machinery does not exist in the stable tree yet, so those two dev tests are
  NOT ported; WP7 must add the refusal (status stays allowed during a lease).
* A worker-fault reply is ``result:"failed"`` + ``error:"wait_failed"`` (dev used
  ``status:"error"`` with ``ok:true`` — not contract-conformant).
* Gate refusals are ``rejected`` replies with ``reasons`` (dev used bare typed
  errors); the message TEXT is byte-identical to dev wherever dev has it.
* ``computer_fetch_image`` is expected at 10 tools later (WP5); this suite pins 9.

Timing is proven deterministically with an injected monotonic clock
(``server._wait_now`` monkeypatched) and short REAL segments (the fixture patches
``_wait_segment_seconds`` to 0.2 s) — no long real sleeps anywhere.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import json
import threading
import time
from typing import Any, Callable

import pytest

from computer_use_mcp import cli as cli_module
from computer_use_mcp import server
from computer_use_mcp.limits import Limits
from computer_use_mcp.server import _parse_limits

from test_controller_integration import (  # noqa: F401 - pytest fixture import
    FAST_LIMITS,
    ScriptedBackend,
    fresh_server,  # noqa: F401 - re-exported fixture for the drift-guard test
    make_session,
)
from reply_contract import validate_reply

#: The test segment: short enough that every blocking call is fast, long enough
#: that a worker woken mid-segment beats it (0.1 s cancel slice).
WAIT_SEGMENT_TEST_SECONDS = 0.2
WAIT_CLOCK_DEADLINE_SECONDS = 5.0
HANG_PROBE_SECONDS = 1.0


# --- harness ------------------------------------------------------------------------------


class _FakeClock:
    """Injectable monotonic clock: the wait mechanism is proven without real time."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.calls = 0
        self.raise_from: int | None = None
        self.fired = False

    def __call__(self) -> float:
        self.calls += 1
        if self.raise_from is not None and not self.fired and self.calls >= self.raise_from:
            self.fired = True
            raise RuntimeError("injected wait-worker fault")
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _RecordingBundles(dict):
    """``_bundles`` subclass that records the wait-cancel state at every pop."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.order: list[tuple[str, Any]] = []
        self.cancel_states: list[bool] = []

    def pop(self, key: Any, *args: Any) -> Any:  # type: ignore[override]
        pending = [
            handle
            for registry in server._WAITS_IN_FLIGHT.values()
            for handle in registry.values()
        ]
        self.cancel_states = [handle.cancel.is_set() for handle in pending]
        self.order.append(("pop", key))
        return super().pop(key, *args)


@pytest.fixture
def wait_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """``fresh_server`` plus a pristine wait registry and a SHORT blocking segment,
    so no wait outlives a test and no blocking call runs longer than a blink."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", server.SessionRegistry(max_sessions=16))
    monkeypatch.setattr(server, "_WAITS_IN_FLIGHT", {})
    monkeypatch.setattr(server, "_WAIT_TOMBSTONES", {})
    monkeypatch.setattr(server, "_bundles", {})
    monkeypatch.setattr(server, "_stopped_sessions", {})
    monkeypatch.setattr(server, "_wait_segment_seconds", lambda: WAIT_SEGMENT_TEST_SECONDS)
    yield server
    # Test hygiene: cancel and join anything this test left behind so one failure
    # cannot cascade into a phantom orphan in the NEXT test's thread sweep.  The
    # leak invariants are asserted INSIDE the tests, not here.
    for registry in list(server._WAITS_IN_FLIGHT.values()):
        handles = list(registry.values())
        for handle in handles:
            handle.cancel.set()
        server._join_wait_threads(handles)


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Freeze the wait clock: waits never expire on their own; the tests cancel."""
    clock = _FakeClock()
    monkeypatch.setattr(server, "_wait_now", clock)
    return clock


def _open_session(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> tuple[str, Any]:
    limits = dict(FAST_LIMITS)
    limits.update(kwargs.pop("limits", {}))
    session_id, bundle, _backend, _provider = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=limits, **kwargs
    )
    return session_id, bundle


def _live_handles() -> list[Any]:
    return [handle for registry in server._WAITS_IN_FLIGHT.values() for handle in registry.values()]


def _handle(session_id: str, wait_id: str) -> Any:
    return server._WAITS_IN_FLIGHT[session_id][wait_id]


def _status(session_id: str, wait_id: str) -> dict[str, Any]:
    """Live status of a running wait, read straight from its handle."""
    handle = _handle(session_id, wait_id)
    now = server._wait_now()
    return {
        "status": "running",
        "elapsed_seconds": now - handle.started_monotonic,
        "remaining_seconds": handle.deadline_monotonic - now,
    }


def _await_retired(
    session_id: str, wait_id: str | None = None, timeout: float = WAIT_CLOCK_DEADLINE_SECONDS
) -> bool:
    """Wait (bounded) until the worker has left the live registry."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        registry = server._WAITS_IN_FLIGHT.get(session_id, {})
        if not registry or (wait_id is not None and wait_id not in registry):
            return True
        time.sleep(0.01)
    return False


def _run_with_deadline(fn: Callable[[], Any], seconds: float, what: str) -> Any:
    """Run ``fn`` on a worker thread; raise if it has not returned in ``seconds``."""
    box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            box["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise AssertionError(
            f"{what} did not return within {seconds:.1f}s "
            f"(deadlock: an operation that never completes)"
        )
    if "error" in box:
        raise box["error"]
    return box.get("value")


# --- normal: start -> cancel, and start -> expire ---------------------------------------


async def test_a_wait_returns_running_and_cancels_mid_flight(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    session_id, _bundle = _open_session(monkeypatch)

    started = time.monotonic()
    reply = await server.computer_wait(session_id, duration_seconds=600.0, label="model bookkeeping")
    call_seconds = time.monotonic() - started

    assert reply["ok"] is True
    assert reply["result"] == "marker"  # unified contract §1.3
    assert reply["status"] == "running"
    assert reply["wait_id"].startswith("wt_")
    assert reply["requested_seconds"] == 600.0
    assert reply["max_wait_seconds"] == 10800.0
    assert reply["started_at"].endswith("Z")
    assert "computer_wait_status" in reply["message"]
    validate_reply(reply, "wait")
    # The blocking segment (patched to 0.2 s) is the longest the call may run.
    assert call_seconds < 2.0, f"computer_wait blocked for {call_seconds:.3f}s"

    frozen_clock.advance(250.0)
    cancelled = await server.computer_cancel_wait(session_id, reply["wait_id"])

    assert cancelled["ok"] is True
    assert cancelled["result"] == "marker"
    assert cancelled["status"] == "cancelled"
    assert cancelled["elapsed_seconds"] == pytest.approx(250.0, abs=0.5)
    assert cancelled["remaining_seconds"] == pytest.approx(350.0, abs=0.5)
    validate_reply(cancelled, "cancel_wait")
    assert _await_retired(session_id, reply["wait_id"])


async def test_a_wait_runs_to_its_natural_completion(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    session_id, _bundle = _open_session(monkeypatch)

    reply = await server.computer_wait(session_id, duration_seconds=30.0)
    assert reply["status"] == "running"
    frozen_clock.advance(30.0)

    assert _await_retired(session_id, reply["wait_id"])
    status = await server.computer_wait_status(session_id, reply["wait_id"])
    assert status["ok"] is True
    assert status["status"] == "completed"
    validate_reply(status, "wait_status")
    # The tombstone answers a late cancel honestly, too.
    assert (await server.computer_cancel_wait(session_id, reply["wait_id"]))["status"] == "completed"


async def test_the_mandatory_scenario_1800s_requested_cancelled_after_1200s(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    """SPEC 1.2 / AC-4 with an INJECTED clock: the 30-minute / 20-minute case,
    run in milliseconds."""
    session_id, _bundle = _open_session(monkeypatch)

    handle = await server.computer_wait(session_id, duration_seconds=1800.0)
    assert handle["requested_seconds"] == 1800.0
    assert handle["max_wait_seconds"] == 10800.0

    frozen_clock.advance(1200.0)
    cancelled = await server.computer_cancel_wait(session_id, handle["wait_id"])

    assert cancelled["ok"] is True
    assert cancelled["status"] == "cancelled"
    assert cancelled["elapsed_seconds"] == pytest.approx(1200.0, abs=0.5)
    assert cancelled["remaining_seconds"] == pytest.approx(600.0, abs=0.5)


async def test_the_model_can_act_again_immediately_while_a_wait_runs(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    """SPEC 1.2 / AC-3: the segment returns and the session is immediately usable."""
    backend = ScriptedBackend(flip=True)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )

    handle = await server.computer_wait(session_id, duration_seconds=900.0)
    response = await server.computer_execute(
        session_id, "click", x=10, y=20, approved=True, include_screenshot_after=False
    )
    payload = json.loads(response[0].text) if isinstance(response, list) else response

    assert payload["ok"] is True, payload
    assert _status(session_id, handle["wait_id"])["status"] == "running"
    cancelled = await server.computer_cancel_wait(session_id, handle["wait_id"])
    assert cancelled["status"] == "cancelled"


# --- boundaries ---------------------------------------------------------------------------


async def test_a_zero_duration_is_refused(wait_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id, _bundle = _open_session(monkeypatch)

    reply = await server.computer_wait(session_id, duration_seconds=0.0)

    assert reply["ok"] is False
    assert reply["result"] == "rejected"
    assert reply["reasons"] == ["invalid_wait_duration"]
    assert "10800.0" in reply["message"]
    assert server._WAITS_IN_FLIGHT == {}  # S-15: not even an empty per-session dict
    validate_reply(reply, "wait")


async def test_a_negative_duration_is_refused(wait_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id, _bundle = _open_session(monkeypatch)

    reply = await server.computer_wait(session_id, duration_seconds=-1.0)

    assert reply["ok"] is False
    assert reply["reasons"] == ["invalid_wait_duration"]
    assert server._WAITS_IN_FLIGHT == {}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
async def test_a_non_finite_duration_is_refused(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, bad: float
) -> None:
    session_id, _bundle = _open_session(monkeypatch)

    reply = await server.computer_wait(session_id, duration_seconds=bad)

    assert reply["ok"] is False
    assert reply["reasons"] == ["invalid_wait_duration"]
    assert "finite" in reply["message"]
    assert server._WAITS_IN_FLIGHT == {}


async def test_exactly_the_cap_is_accepted(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    """AC-1: 10 800.0 s is INSIDE the bound — the cap is inclusive."""
    session_id, _bundle = _open_session(monkeypatch)

    reply = await server.computer_wait(session_id, duration_seconds=10800.0)

    assert reply["ok"] is True
    assert reply["requested_seconds"] == 10800.0
    await server.stop_session(session_id)


async def test_one_millisecond_over_the_cap_is_refused_and_starts_nothing(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: 10 800.001 s is REFUSED, names the cap and the request, starts nothing."""
    session_id, _bundle = _open_session(monkeypatch)

    reply = await server.computer_wait(session_id, duration_seconds=10800.001)

    assert reply["ok"] is False
    assert reply["reasons"] == ["wait_duration_exceeds_max"]
    assert "10800.001" in reply["message"]
    assert "10800.0" in reply["message"]
    assert "10,800,000 ms" in reply["message"]
    # Nothing started: no registry entry (not even an empty one), no thread, no handle.
    assert server._WAITS_IN_FLIGHT == {}
    assert server._WAIT_TOMBSTONES == {}


async def test_refusal_names_the_effective_cap_not_the_hard_cap(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """K-19 / B-18: a session configured to 60 s must be told 60, not 10 800."""
    session_id, _bundle = _open_session(monkeypatch, limits={"max_wait_seconds": 60.0})

    reply = await server.computer_wait(session_id, duration_seconds=1800.0)

    assert reply["ok"] is False
    assert reply["reasons"] == ["wait_duration_exceeds_max"]
    assert "60.0" in reply["message"]
    assert "1800.0" in reply["message"]
    assert "10800" not in reply["message"]
    assert server._WAITS_IN_FLIGHT == {}


# --- errors -------------------------------------------------------------------------------


async def test_an_unknown_wait_id_is_refused_on_status_and_cancel(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle = _open_session(monkeypatch)

    cancel_reply = await server.computer_cancel_wait(session_id, "wt_" + "0" * 16)
    status_reply = await server.computer_wait_status(session_id, "wt_" + "0" * 16)

    for reply in (cancel_reply, status_reply):
        assert reply["ok"] is False
        assert reply["result"] == "rejected"
        assert reply["reasons"] == ["unknown_wait"]
        assert "wait_id" in reply["message"]
        validate_reply(reply, "cancel_wait" if reply is cancel_reply else "wait_status")


async def test_a_foreign_session_wait_id_is_byte_identical_to_unknown(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    """R2 / SPEC 1.2: a wrong id must not probe — and must not stop — another wait."""
    first, _b1 = _open_session(monkeypatch)
    second, _b2 = _open_session(monkeypatch)

    live = await server.computer_wait(first, duration_seconds=600.0)
    foreign = await server.computer_wait(second, duration_seconds=600.0)

    unknown_reply = await server.computer_cancel_wait(first, "wt_" + "f" * 16)
    foreign_reply = await server.computer_cancel_wait(first, foreign["wait_id"])

    assert json.dumps(foreign_reply, sort_keys=True) == json.dumps(unknown_reply, sort_keys=True)
    assert foreign_reply["ok"] is False
    assert foreign_reply["reasons"] == ["unknown_wait"]
    # The foreign wait is untouched — still live and still cancellable by its owner.
    assert _status(second, foreign["wait_id"])["status"] == "running"
    assert (await server.computer_cancel_wait(second, foreign["wait_id"]))["status"] == "cancelled"
    assert (await server.computer_cancel_wait(first, live["wait_id"]))["status"] == "cancelled"


async def test_a_duplicate_cancel_is_idempotent(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    """K-6 / A-3: the tombstone is what makes ``already_cancelled`` reachable."""
    session_id, _bundle = _open_session(monkeypatch)

    handle = await server.computer_wait(session_id, duration_seconds=300.0)
    frozen_clock.advance(100.0)
    first = await server.computer_cancel_wait(session_id, handle["wait_id"])
    second = await server.computer_cancel_wait(session_id, handle["wait_id"])
    third = await server.computer_cancel_wait(session_id, handle["wait_id"])

    assert first["status"] == "cancelled"
    assert "already_cancelled" not in first
    assert second["status"] == "cancelled"
    assert second["already_cancelled"] is True
    assert third["status"] == "cancelled"
    assert third["already_cancelled"] is True
    # Idempotent AND honest: the same elapsed/remaining on every repeat.
    assert second["elapsed_seconds"] == pytest.approx(first["elapsed_seconds"])
    assert second["remaining_seconds"] == pytest.approx(first["remaining_seconds"])
    assert third["elapsed_seconds"] == pytest.approx(first["elapsed_seconds"])


async def test_cancelling_a_completed_wait_reports_completed_not_failure(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    """SPEC 1.2 / §8.2: a finished wait answers ``completed`` — honest, not an error."""
    session_id, _bundle = _open_session(monkeypatch)

    handle = await server.computer_wait(session_id, duration_seconds=45.0)
    frozen_clock.advance(45.0)
    assert _await_retired(session_id, handle["wait_id"])

    reply = await server.computer_cancel_wait(session_id, handle["wait_id"])

    assert reply["ok"] is True
    assert reply["status"] == "completed"
    assert reply["remaining_seconds"] == pytest.approx(0.0, abs=0.5)
    assert reply["elapsed_seconds"] == pytest.approx(45.0, abs=0.5)


async def test_a_wait_cancelled_by_session_stop_cannot_be_cancelled_again(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    session_id, _bundle = _open_session(monkeypatch)

    handle = await server.computer_wait(session_id, duration_seconds=600.0)
    await server.stop_session(session_id)

    reply = await server.computer_cancel_wait(session_id, handle["wait_id"])
    assert reply["ok"] is False
    assert reply["error"] == "session_stopped"


async def test_an_exception_inside_the_wait_is_reported_as_wait_failed(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that raises is a typed, traceback-free outcome — never a silent leak."""
    clock = _FakeClock()
    # call 1 = the tool's start stamp; call 2 = the worker's first slice check;
    # call 3 = the worker's second slice check -> raises INSIDE the worker.
    clock.raise_from = 3
    monkeypatch.setattr(server, "_wait_now", clock)
    session_id, _bundle = _open_session(monkeypatch)

    handle = await server.computer_wait(session_id, duration_seconds=600.0)
    assert _await_retired(session_id, handle["wait_id"])

    reply = await server.computer_wait_status(session_id, handle["wait_id"])
    assert reply["ok"] is False
    assert reply["result"] == "failed"
    assert reply["error"] == "wait_failed"
    assert "injected wait-worker fault" in reply["message"]
    assert "Traceback" not in reply["message"]
    validate_reply(reply, "wait_status")
    assert server._WAITS_IN_FLIGHT == {}


async def test_wait_gate_rejections_are_rejected_replies_naming_the_gate(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-17 / K-19 under the unified contract: gate refusals are ``rejected``
    replies with ``reasons``; the message text stays dev-identical."""
    session_id, _bundle = _open_session(monkeypatch)

    over = await server.computer_wait(session_id, duration_seconds=20000.0)
    assert over["result"] == "rejected" and over["reasons"] == ["wait_duration_exceeds_max"]
    assert "requested 20000.0" in over["message"]

    under = await server.computer_wait(session_id, duration_seconds=-2.0)
    assert under["result"] == "rejected" and under["reasons"] == ["invalid_wait_duration"]

    unknown = await server.computer_wait_status(session_id, "wt_" + "1" * 16)
    assert unknown["result"] == "rejected" and unknown["reasons"] == ["unknown_wait"]


async def test_a_wait_on_an_unknown_session_is_refused(wait_server: Any) -> None:
    reply = await server.computer_wait("sess-does-not-exist", duration_seconds=5.0)

    assert reply["ok"] is False
    assert reply["error"] == "unknown_session"
    # An unknown-id call creates NO bookkeeping anywhere.
    assert server._WAITS_IN_FLIGHT == {}
    assert server._WAIT_TOMBSTONES == {}


async def test_a_wait_on_a_stopped_session_is_refused(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    session_id, _bundle = _open_session(monkeypatch)
    await server.stop_session(session_id)

    reply = await server.computer_wait(session_id, duration_seconds=5.0)

    assert reply["ok"] is False
    assert reply["error"] == "session_stopped"
    assert server._WAITS_IN_FLIGHT == {}


# --- concurrency --------------------------------------------------------------------------


async def test_two_waits_in_one_session_stay_independent(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    """AC-5: B's elapsed/remaining are continuous across A's cancel."""
    session_id, _bundle = _open_session(monkeypatch)

    first = await server.computer_wait(session_id, duration_seconds=600.0)
    frozen_clock.advance(60.0)
    second = await server.computer_wait(session_id, duration_seconds=300.0)

    frozen_clock.advance(60.0)
    cancelled = await server.computer_cancel_wait(session_id, first["wait_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["elapsed_seconds"] == pytest.approx(120.0, abs=0.5)
    assert cancelled["remaining_seconds"] == pytest.approx(480.0, abs=0.5)

    probe = _status(session_id, second["wait_id"])
    assert probe["status"] == "running"
    assert probe["elapsed_seconds"] == pytest.approx(60.0, abs=0.5)
    assert probe["remaining_seconds"] == pytest.approx(240.0, abs=0.5)

    final = await server.computer_cancel_wait(session_id, second["wait_id"])
    assert final["status"] == "cancelled"
    assert final["elapsed_seconds"] == pytest.approx(60.0, abs=0.5)
    assert final["remaining_seconds"] == pytest.approx(240.0, abs=0.5)


async def test_a_cancel_racing_natural_expiry_never_lies(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    """AC: the reply is one of the DEFINED outcomes, never a third, never a lie."""
    session_id, _bundle = _open_session(monkeypatch)
    requested = 0.2

    for _ in range(12):
        handle = await server.computer_wait(session_id, duration_seconds=requested)
        frozen_clock.advance(requested)
        reply = await server.computer_cancel_wait(session_id, handle["wait_id"])
        assert reply["ok"] is True
        assert reply["status"] in {"cancelled", "completed"}
        # The never-a-lie invariant: elapsed and remaining are two halves of the
        # request, so a caller can always reconstruct how far the wait really got.
        assert reply["elapsed_seconds"] >= 0.0
        assert reply["remaining_seconds"] >= 0.0
        assert reply["elapsed_seconds"] + reply["remaining_seconds"] == pytest.approx(
            requested, abs=1.0
        )
        assert reply["elapsed_seconds"] <= requested + 0.5
        if reply["status"] == "completed":
            assert reply["remaining_seconds"] == pytest.approx(0.0, abs=0.5)


async def test_ten_sequential_wait_cancel_cycles_leave_the_registry_empty(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    session_id, _bundle = _open_session(monkeypatch)

    for index in range(10):
        handle = await server.computer_wait(session_id, duration_seconds=120.0)
        frozen_clock.advance(5.0)
        reply = await server.computer_cancel_wait(session_id, handle["wait_id"])
        assert reply["status"] == "cancelled", (index, reply)
        assert _await_retired(session_id, handle["wait_id"])

    assert server._WAITS_IN_FLIGHT == {}
    assert sum(len(ring) for ring in server._WAIT_TOMBSTONES.values()) <= 8


async def test_a_second_wait_on_the_same_session_is_legal(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    """A-7: up to MAX_WAITS_PER_SESSION concurrent waits per session are legal."""
    session_id, _bundle = _open_session(monkeypatch)

    handles = [
        await server.computer_wait(session_id, duration_seconds=600.0)
        for _ in range(server.MAX_WAITS_PER_SESSION)
    ]
    assert all(handle["ok"] is True for handle in handles)
    assert len({handle["wait_id"] for handle in handles}) == server.MAX_WAITS_PER_SESSION

    overflow = await server.computer_wait(session_id, duration_seconds=600.0)
    assert overflow["ok"] is False
    assert overflow["result"] == "rejected"
    assert overflow["reasons"] == ["too_many_waits"]
    assert "MAX_WAITS_PER_SESSION" in overflow["message"]
    assert str(server.MAX_WAITS_PER_SESSION) in overflow["message"]
    # S-15 (the fix this port carries): the refusal added NO empty dict and did not
    # disturb the four live entries.
    assert sorted(server._WAITS_IN_FLIGHT) == [session_id]
    assert len(server._WAITS_IN_FLIGHT[session_id]) == server.MAX_WAITS_PER_SESSION

    await server.stop_session(session_id)


async def test_the_global_cap_is_reachable_and_binds(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    """A-7: 4 sessions x 4 waits fills MAX_WAITS_GLOBAL; a FIFTH session's first
    wait is the one that trips the process-wide cap — and S-15 means the refusal
    leaves no empty per-session dict for the refused session."""
    assert server.MAX_WAITS_GLOBAL == server.MAX_WAITS_PER_SESSION * 4
    sessions = [_open_session(monkeypatch)[0] for _ in range(4)]
    for session_id in sessions:
        for _ in range(server.MAX_WAITS_PER_SESSION):
            assert (await server.computer_wait(session_id, duration_seconds=600.0))["ok"] is True
    assert sum(len(reg) for reg in server._WAITS_IN_FLIGHT.values()) == server.MAX_WAITS_GLOBAL

    fresh_session, _bundle = _open_session(monkeypatch)
    overflow = await server.computer_wait(fresh_session, duration_seconds=600.0)
    assert overflow["ok"] is False
    assert overflow["result"] == "rejected"
    assert overflow["reasons"] == ["too_many_waits"]
    assert "MAX_WAITS_GLOBAL" in overflow["message"]
    assert "MAX_WAITS_PER_SESSION" not in overflow["message"]
    assert fresh_session not in server._WAITS_IN_FLIGHT  # the S-15 fix, precisely

    for session_id in [*sessions, fresh_session]:
        await server.stop_session(session_id)
    assert server._WAITS_IN_FLIGHT == {}


async def test_the_concurrency_soak_leaves_the_registry_empty(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MASTER MISSION §7.3: 8 concurrent waits across 4 sessions, cancellations
    interleaved, registry bounded and back to empty.  Real threads, real (short) waits."""
    sessions = [_open_session(monkeypatch)[0] for _ in range(4)]
    handles: list[tuple[str, str]] = []
    for index, session_id in enumerate(sessions):
        for _ in range(2):
            # ADAPTED vs dev (recorded): durations must outlive the whole soak now
            # that starting a wait consumes a real 0.2 s segment per call — dev's
            # 0.4 s waits expired mid-soak because dev's start returned instantly.
            handle = await server.computer_wait(session_id, duration_seconds=20.0 + 0.05 * index)
            assert handle["ok"] is True, handle
            handles.append((session_id, handle["wait_id"]))

    assert sum(len(reg) for reg in server._WAITS_IN_FLIGHT.values()) == 8

    for session_id, wait_id in handles[::2]:
        assert (await server.computer_cancel_wait(session_id, wait_id))["status"] == "cancelled"
    for session_id in sessions:
        await server.stop_session(session_id)

    assert server._WAITS_IN_FLIGHT == {}
    assert server._WAIT_TOMBSTONES == {}
    survivors = [
        thread.name for thread in threading.enumerate() if thread.name.startswith("cortex-wait-")
    ]
    assert survivors == []


# --- leaks: the K-5 deadlock invariant ----------------------------------------------------


async def test_stop_session_during_a_wait_terminates_the_thread(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 / A-1: the headline leak test. A running wait is cancelled by
    ``stop_session`` and no thread survives."""
    session_id, _bundle = _open_session(monkeypatch)
    reply = await server.computer_wait(session_id, duration_seconds=3000.0)
    handle = _handle(session_id, reply["wait_id"])
    thread = handle.thread
    assert thread is not None and thread.is_alive()

    await server.stop_session(session_id)

    thread.join(timeout=WAIT_CLOCK_DEADLINE_SECONDS)
    assert not thread.is_alive(), "the wait thread survived stop_session"
    assert handle.cancel.is_set()
    assert server._WAITS_IN_FLIGHT == {}
    assert server._WAIT_TOMBSTONES.get(session_id, {}) == {}


async def test_the_join_must_never_happen_under_the_module_lock(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """K-5 / C7 / A-1 — the reproduced deadlock, both halves.

    (a) THE HAZARD, reproduced with the product's OWN worker: a canceller that
        holds ``_lock`` and joins a worker whose ``finally`` needs ``_lock`` never
        completes.  This is the v1 design and it hangs.
    (b) THE SHIPPED PATH: the real ``stop_session`` finishes inside a hard
        deadline.  If the join is ever moved back inside the lock, (b) never
        returns and THIS TEST FAILS BY TIMEOUT.
    """
    # --- (a) the hazard, reproduced (bounded, and it always unwinds) -------------
    hazard_session, _b = _open_session(monkeypatch)
    hazard_reply = await server.computer_wait(hazard_session, duration_seconds=3000.0)
    hazard_handle = _handle(hazard_session, hazard_reply["wait_id"])
    measured: dict[str, Any] = {}

    def canceller_under_the_lock() -> None:
        with server._lock:
            hazard_handle.cancel.set()
            began = time.monotonic()
            hazard_handle.thread.join(timeout=HANG_PROBE_SECONDS)
            measured["elapsed"] = time.monotonic() - began
            measured["alive_during_join"] = hazard_handle.thread.is_alive()

    hazard_thread = threading.Thread(target=canceller_under_the_lock, daemon=True)
    hazard_thread.start()
    hazard_thread.join(HANG_PROBE_SECONDS * 4)
    assert not hazard_thread.is_alive(), "the hazard repro itself must always unwind"
    assert measured["alive_during_join"] is True
    assert measured["elapsed"] >= HANG_PROBE_SECONDS * 0.9
    hazard_handle.thread.join(timeout=WAIT_CLOCK_DEADLINE_SECONDS)

    # --- (b) the shipped path, inside a hard deadline ---------------------------
    live_session, _b2 = _open_session(monkeypatch)
    await server.computer_wait(live_session, duration_seconds=3000.0)
    reply = _run_with_deadline(
        lambda: asyncio.run(server.stop_session(live_session)), 3.0, "stop_session with a live wait"
    )
    assert reply["ok"] is True
    assert server._WAITS_IN_FLIGHT == {}
    assert [
        thread.name for thread in threading.enumerate() if thread.name.startswith("cortex-wait-")
    ] == []


async def test_the_join_guard_refuses_to_join_under_the_module_lock(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: the join helper itself rejects a locked call, so a
    future refactor fails loudly instead of hanging the product."""
    session_id, _bundle = _open_session(monkeypatch)
    handle = _handle(session_id, (await server.computer_wait(session_id, duration_seconds=60.0))["wait_id"])

    with server._lock:
        with pytest.raises(RuntimeError, match="module lock"):
            server._join_wait_threads([handle])

    await server.stop_session(session_id)


async def test_the_teardown_cancels_waits_before_the_bundle_is_popped(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """K-5 / dev anchor: ``_cancel_all_waits`` runs INSIDE the locked teardown and
    BEFORE the pop."""
    recording = _RecordingBundles()
    monkeypatch.setattr(server, "_bundles", recording)
    seen: list[dict[str, Any]] = []
    real_cancel_all_waits = server._cancel_all_waits

    def spy_cancel_all_waits(session_id: str) -> Any:
        handles = list(server._WAITS_IN_FLIGHT.get(session_id, {}).values())
        seen.append(
            {
                "bundle_live": session_id in server._bundles,
                "live_handles": len(handles),
                "before": [handle.cancel.is_set() for handle in handles],
            }
        )
        recording.order.append(("cancel_all_waits", session_id))
        result = real_cancel_all_waits(session_id)
        seen[0]["after"] = [handle.cancel.is_set() for handle in handles]
        return result

    monkeypatch.setattr(server, "_cancel_all_waits", spy_cancel_all_waits)
    session_id, _bundle = _open_session(monkeypatch)
    await server.computer_wait(session_id, duration_seconds=3000.0)

    await server.stop_session(session_id)

    assert seen, "_cancel_all_waits never ran"
    assert seen[0]["bundle_live"] is True, "waits were cancelled after the bundle was gone"
    assert seen[0]["live_handles"] == 1
    assert seen[0]["before"] == [False], "the flag was already set before teardown"
    assert seen[0]["after"] == [True], "the cancel flag was NOT set under the lock"
    assert recording.order.index(("cancel_all_waits", session_id)) < recording.order.index(
        ("pop", session_id)
    )


async def test_every_wait_thread_is_a_daemon(wait_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id, _bundle = _open_session(monkeypatch)

    handle = await server.computer_wait(session_id, duration_seconds=300.0)
    thread = _handle(session_id, handle["wait_id"]).thread

    assert thread is not None
    assert thread.daemon is True
    assert thread.name.startswith("cortex-wait-")
    await server.stop_session(session_id)


async def test_a_wait_completes_and_frees_its_slot_without_any_further_call(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A-9: the worker is bounded SOLELY by its deadline and its cancel event —
    never by anything client-derived.  No further call is needed to free the slot."""
    session_id, _bundle = _open_session(monkeypatch)

    reply = await server.computer_wait(session_id, duration_seconds=0.1)
    assert _await_retired(session_id, reply["wait_id"])
    assert server._WAITS_IN_FLIGHT == {}
    ring = server._WAIT_TOMBSTONES[session_id]
    assert len(ring) == 1
    assert next(iter(ring.values())).outcome == "completed"


async def test_a_wait_handle_holds_no_bundle_reference(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    """§8.2 invariant 3 (adapted): a handle may not keep a session's BUNDLE (its
    backend, agent, auditor, metrics) alive for 3 h. The handle now holds a WEAK
    reference to the SessionContext only — the WP-S in-flight mark needs the
    context, never the bundle.

    ADAPTATION vs dev (recorded): the traversal must skip FUNCTION and MODULE-DICT
    hops — the live worker thread reaches ``_wait_worker`` → the server module
    globals → EVERYTHING (including ``_bundles``), which is a traversal artifact of
    any module-level code, not retention by the handle. The dev suite never ran
    (R2), so the dev-original walk would have failed there for the same reason.
    """
    import types

    session_id, bundle = _open_session(monkeypatch)
    handle = _handle(
        session_id, (await server.computer_wait(session_id, duration_seconds=3000.0))["wait_id"]
    )

    reachable: set[int] = set()
    frontier: list[Any] = [handle]
    while frontier:
        current = frontier.pop()
        if isinstance(current, type) or isinstance(current, types.ModuleType):
            continue
        if isinstance(current, (types.FunctionType, types.MethodType)):
            continue  # functions drag their module globals in — not retention
        if id(current) in reachable:
            continue
        reachable.add(id(current))
        frontier.extend(gc.get_referents(current))

    assert id(bundle) not in reachable
    assert id(server._bundles) not in reachable
    # The weakref target (the context) IS reachable — by design — but the bundle
    # never is, so a stopped session's backend/agent/audit memory is not kept.
    await server.stop_session(session_id)


async def test_the_registry_entry_exists_before_the_thread_starts(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    """§8.2 invariant 3: the entry is inserted BEFORE ``start()``, so a cancel can
    never race ahead of the registry."""
    snapshots: list[dict[str, list[str]]] = []
    real_start = threading.Thread.start

    def spy_start(self: threading.Thread, *args: Any, **kwargs: Any) -> Any:
        snapshots.append(
            {
                session_id: sorted(registry)
                for session_id, registry in server._WAITS_IN_FLIGHT.items()
            }
        )
        return real_start(self, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", spy_start)
    session_id, _bundle = _open_session(monkeypatch)

    handle = await server.computer_wait(session_id, duration_seconds=300.0)

    assert snapshots, "the worker thread was never started"
    assert snapshots[-1][session_id] == [handle["wait_id"]]
    await server.stop_session(session_id)


@pytest.mark.parametrize("terminal", ["natural", "cancel", "stop", "exception"])
async def test_every_terminal_path_ends_in_the_same_finally(
    wait_server: Any,
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:
    """§8.2 invariant 3: natural expiry, cancel, session stop and a worker
    exception ALL end in the same ``finally`` — the entry is gone either way."""
    clock = _FakeClock()
    if terminal == "exception":
        clock.raise_from = 3
    monkeypatch.setattr(server, "_wait_now", clock)
    session_id, _bundle = _open_session(monkeypatch)

    handle = await server.computer_wait(session_id, duration_seconds=60.0)
    if terminal == "natural":
        clock.advance(60.0)
    elif terminal == "cancel":
        clock.advance(10.0)
        await server.computer_cancel_wait(session_id, handle["wait_id"])
    elif terminal == "stop":
        await server.stop_session(session_id)
    else:
        pass  # the injected fault fires on the worker's second slice check

    assert _await_retired(session_id, handle["wait_id"])
    assert server._WAITS_IN_FLIGHT == {}
    if terminal == "exception":
        ring = server._WAIT_TOMBSTONES.get(session_id, {})
        assert ring and next(iter(ring.values())).outcome == "wait_failed"


async def test_the_tombstone_ring_is_bounded(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    """K-6: cap 8 per session; the oldest tombstone is evicted, never unbounded."""
    session_id, _bundle = _open_session(monkeypatch)

    for _ in range(server.WAIT_TOMBSTONES_PER_SESSION + 4):
        handle = await server.computer_wait(session_id, duration_seconds=30.0)
        frozen_clock.advance(30.0)
        assert _await_retired(session_id, handle["wait_id"])

    ring = server._WAIT_TOMBSTONES[session_id]
    assert len(ring) == server.WAIT_TOMBSTONES_PER_SESSION
    # The evicted id is honestly unknown again, not silently answered.
    assert (
        await server.computer_cancel_wait(session_id, "wt_" + "9" * 16)
    )["reasons"] == ["unknown_wait"]


# --- budget, docstrings, teaching ---------------------------------------------------------


async def test_background_wait_consumes_zero_action_budget(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch, frozen_clock: _FakeClock
) -> None:
    """K-2 / A-5 / OQ-5: the wait never enters the action pipeline, so
    ``check_action``/``record_action`` never run.  Intended: the budget bounds
    INPUT volume and a wait dispatches none."""
    session_id, bundle = _open_session(monkeypatch)
    before = bundle.enforcer.snapshot()

    for _ in range(3):
        handle = await server.computer_wait(session_id, duration_seconds=600.0)
        await server.computer_cancel_wait(session_id, handle["wait_id"])

    after = bundle.enforcer.snapshot()
    assert after["actions"] == before["actions"] == 0


def test_the_wait_tools_are_async_coroutines() -> None:
    """CHANGED vs dev (recorded): the blocking segments REQUIRE awaiting — a sync
    tool would block the event loop (the exact defect the design forbids)."""
    for tool in (server.computer_wait, server.computer_cancel_wait, server.computer_wait_status):
        assert inspect.iscoroutinefunction(tool), tool.__name__


def test_the_wait_tools_return_plain_dicts_not_content_blocks() -> None:
    """K-9: these return DICT payloads (unlike the block family)."""
    for tool in (server.computer_wait, server.computer_cancel_wait, server.computer_wait_status):
        annotation = inspect.signature(tool).return_annotation
        assert annotation in (dict[str, object], "dict[str, object]"), (tool.__name__, annotation)
        meta = server.mcp._tool_manager.get_tool(tool.__name__).fn_metadata
        assert meta.output_model is not None, tool.__name__


def test_the_wait_docstrings_teach_the_blocking_loop() -> None:
    """AC-7b / AC-7c / C1 / S-02: the served text must teach the wait LOOP (call ->
    running -> status until completed -> act), the zero action budget, the REAL
    ending conditions, the client-cancellation semantics, and must NOT claim a
    900 s task ceiling (K-1)."""
    doc = server.computer_wait.__doc__ or ""
    assert "zero" in doc and "max_actions" in doc
    assert "stop_session" in doc
    assert "max_wait_seconds" in doc
    assert "computer_cancel_wait" in doc
    assert "computer_wait_status" in doc
    assert "completed" in doc and "running" in doc
    assert "60 s tool timeout" in doc  # the segment bound is taught
    assert "computer_wait" in server.ACTION_VOCABULARY
    assert "not a wall-clock ceiling that ends a session" in doc
    assert "task ceiling" not in doc
    assert "900-second" not in doc
    # client-side cancellation only abandons the call; the wait keeps running
    assert "KEEPS RUNNING" in doc
    # waiting time is tool-running time, never idle time (Addendum 2 follow-up)
    assert "never idle time" in doc
    # the flat in-queue action stays capped at 10 s
    assert "capped at 10 seconds" in doc

    status_doc = server.computer_wait_status.__doc__ or ""
    assert "completed" in status_doc and "running" in status_doc
    assert "block=false" in status_doc  # the non-blocking peek is taught


def test_max_task_seconds_is_not_newly_enforced() -> None:
    """K-1 / A-2 / R21: ``check_task_duration`` still has ZERO production call
    sites.  WP6 documents the gap; it does not close it."""
    import pathlib

    src = pathlib.Path(server.__file__).parent
    call_sites = [
        path.name
        for path in sorted(src.glob("*.py"))
        if path.name != "limits.py" and ".check_task_duration(" in path.read_text(encoding="utf-8")
    ]
    assert call_sites == []
    assert Limits().max_task_seconds == 900.0  # unchanged in VALUE too (C1)


# --- max_wait_seconds (the limit) --------------------------------------------------------


def test_max_wait_seconds_default_and_clamp() -> None:
    assert Limits().max_wait_seconds == 10800.0
    assert Limits(max_wait_seconds=1e9).validate().max_wait_seconds == 10800.0
    assert Limits(max_wait_seconds=-5.0).validate().max_wait_seconds == 0.0
    assert Limits(max_wait_seconds=60.0).validate().max_wait_seconds == 60.0
    assert Limits(max_wait_seconds=10800.0).validate().max_wait_seconds == 10800.0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_max_wait_seconds_rejects_non_finite_values(bad: float) -> None:
    """The F3/B·N5 guard, ported with WP6: the parser rejects a non-finite value
    rather than silently substituting the ceiling."""
    with pytest.raises(ValueError) as excinfo:
        _parse_limits({"max_wait_seconds": bad})
    assert "max_wait_seconds" in str(excinfo.value)
    assert "finite" in str(excinfo.value)


async def test_max_wait_seconds_is_advertised_in_the_start_session_limits_string(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap self-documents: ``start_session`` serves ``str(limits_obj)``."""
    response = server.start_session(limits=FAST_LIMITS)

    assert "max_wait_seconds=10800.0" in str(response["limits"])


async def test_a_session_with_max_wait_seconds_zero_refuses_every_wait(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle = _open_session(monkeypatch, limits={"max_wait_seconds": 0.0})

    reply = await server.computer_wait(session_id, duration_seconds=1.0)

    assert reply["ok"] is False
    assert reply["reasons"] == ["wait_duration_exceeds_max"]
    assert "0.0" in reply["message"]


# --- the advertised tool surface (R17 / K-8) --------------------------------------------


async def test_expected_tools_matches_the_registered_surface(fresh_server: Any) -> None:
    """R17 / K-8 / B-2b: the drift guard.  6 -> 9 with the WP6 wait family
    (decision D-1); ``computer_fetch_image`` is WP5's tenth and is NOT here yet."""
    registered = {tool.name for tool in await server.mcp.list_tools()}
    assert cli_module.EXPECTED_TOOLS == registered
    assert {"computer_wait", "computer_cancel_wait", "computer_wait_status"} <= registered
    assert len(registered) == 9
    assert "computer_fetch_image" not in registered


async def test_the_new_tools_advertise_no_anyof_and_no_ref() -> None:
    """B-1 / B-16b: the zero-``anyOf`` wire contract the wait family must satisfy."""
    for name in ("computer_wait", "computer_cancel_wait", "computer_wait_status"):
        meta = server.mcp._tool_manager.get_tool(name)
        schema_text = json.dumps(meta.fn_metadata.arg_model.model_json_schema())
        assert "anyOf" not in schema_text, name
        assert "$ref" not in schema_text and "$defs" not in schema_text, name


def test_duration_seconds_is_required_and_the_optionals_are_not() -> None:
    """B-16b: a required non-nullable float is what keeps the schema free of
    ``anyOf``/``$ref``; a widened alias here would be a new wire class."""
    fields = server.mcp._tool_manager.get_tool("computer_wait").fn_metadata.arg_model.model_fields
    assert fields["duration_seconds"].is_required()
    assert fields["label"].is_required() is False
    status_fields = server.mcp._tool_manager.get_tool(
        "computer_wait_status"
    ).fn_metadata.arg_model.model_fields
    assert status_fields["wait_id"].is_required()
    assert status_fields["block"].is_required() is False  # default true
