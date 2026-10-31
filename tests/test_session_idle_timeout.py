"""WP-S idle-session reaper tests (goal.md Addendum 2 + follow-up, ORVEX-2026-108).

Pins the idle-timeout contract on the stable tree:

- a session idle past the timeout (default 900 s; env
  ``CORTEX_SESSION_IDLE_TIMEOUT_SECONDS``, band 60..86400, ``0`` = disabled,
  fallback-not-clamp) with NOTHING in flight is stopped through the SAME kill path
  as stop_session (``_close_stopped_bundle``): audited ``stop`` + ``emergency_stop``
  with ``source="idle_timeout"`` + ``idle_seconds``, bundle removed from store and
  registry, stopped snapshot remembered with ``stop_reason="idle_timeout"``;
- ANY tool call addressing the session — including a rejected/refused one — resets
  the idle count (start AND end of the call are activity);
- a session with a call in flight (a long ``computer_execute`` or the WP6
  ``SessionContext.in_flight_call`` hook) is NEVER reaped; idle time is measured
  from the END of the last call;
- the sweep runs lazily at every tool entry and inside start_session BEFORE the
  limit check, so a full registry of forgotten sessions frees itself; the sweep
  never reaps the session the current call addresses;
- ``session_limit_exceeded`` lists the live sessions (most idle first) with
  ``idle_seconds``/``in_flight``/``age_seconds`` plus the effective
  ``idle_timeout_seconds``; ok/error/max_sessions unchanged;
- a later call on a reaped id fails closed with ``session_stopped`` whose
  message says the session was stopped for inactivity after N seconds.

All timing uses a patched monotonic clock (no real sleeps beyond trivial waits).
Fake backend + scripted provider only — no real input is ever dispatched.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest
from test_controller_integration import (
    ScriptedBackend,
    ScriptedProvider,
    make_session,
)

import computer_use_mcp.server.lifecycle as srv_server_lifecycle
import computer_use_mcp.server.app as srv_server_app
import time as srv_time
import computer_use_mcp.server.wait_machine as srv_server_wait_machine
import computer_use_mcp.server.wait as srv_server_wait
import computer_use_mcp.server.state as srv_server_state
import computer_use_mcp.server.observe as srv_server_observe
import computer_use_mcp.server.execute as srv_server_execute
from computer_use_mcp.core.state import SessionRegistry
from reply_contract import validate_reply

#: Tests run with a SHORT idle timeout so the patched clock only ever advances a
#: couple of minutes of fake time (keeps LimitEnforcer task budgets far away).
TEST_IDLE_TIMEOUT = "60"

#: Limits that keep the enforcer out of the way while the fake clock jumps.
IDLE_TEST_LIMITS = {
    "min_screenshot_interval_ms": 0,
    "max_task_seconds": 3600,
    "max_actions": 500,
}


class FakeClock:
    """Controllable ``time.monotonic`` stand-in (no real sleeping)."""

    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Patch the monotonic clock EVERYWHERE (server + state share the time module)."""
    fake = FakeClock()
    monkeypatch.setattr(srv_time, "monotonic", fake)
    return fake


@pytest.fixture
def fresh_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, clock: FakeClock
) -> Any:
    """Fresh registry/bundles/stopped-memory + short idle timeout + per-test audit dir."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv(srv_server_lifecycle.SESSION_IDLE_TIMEOUT_ENV, TEST_IDLE_TIMEOUT)
    monkeypatch.setattr(srv_server_lifecycle, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(srv_server_lifecycle, "_bundles", {})
    monkeypatch.setattr(srv_server_wait_machine, "_bundles", srv_server_lifecycle._bundles)
    monkeypatch.setattr(srv_server_wait, "_bundles", srv_server_lifecycle._bundles)
    monkeypatch.setattr(srv_server_lifecycle, "_stopped_sessions", {})
    monkeypatch.setattr(srv_server_state, "_stopped_sessions", srv_server_lifecycle._stopped_sessions)
    return None


def _text_session(monkeypatch: pytest.MonkeyPatch, **start_kwargs: Any) -> tuple[str, Any, Any]:
    """One live text-mode session (fake backend + scripted provider, approval off)."""
    start_kwargs.setdefault("dry_run", False)
    start_kwargs.setdefault("require_approval", False)
    start_kwargs.setdefault("limits", IDLE_TEST_LIMITS)
    start_kwargs.setdefault("image_delivery", "text")
    session_id, bundle, backend, _provider = make_session(monkeypatch, **start_kwargs)
    return session_id, bundle, backend


def _execute_payload(result: Any) -> dict[str, Any]:
    """Unwrap an executed ``computer_execute`` response (content blocks) to its dict."""
    if isinstance(result, list):
        return json.loads(result[0].text)
    return result


def _run_execute(session_id: str, **kwargs: Any) -> Any:
    """Call the async ``computer_execute`` tool from a sync test (fake backend only)."""
    return asyncio.run(srv_server_execute.computer_execute(session_id, **kwargs))


def _reply_dict(result: Any) -> dict[str, Any]:
    """Both reply forms (plain dict errors and TextContent blocks) as a dict."""
    return _execute_payload(result)


# --- (h) reap after timeout -------------------------------------------------------------------


def test_reap_after_timeout_through_stop_kill_path(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, clock: FakeClock, tmp_path: Any
) -> None:
    """A forgotten session is reaped at the timeout through the stop_session kill path."""
    session_id, bundle, _backend = _text_session(monkeypatch)
    audit_path = bundle.auditor.path_for(session_id)

    clock.advance(60)
    reaped = srv_server_lifecycle._sweep_idle_sessions()
    assert reaped == [session_id]
    assert session_id not in srv_server_lifecycle._bundles
    assert len(srv_server_lifecycle._registry) == 0  # registry slot freed
    snapshot = srv_server_lifecycle._stopped_sessions[session_id]
    assert snapshot["stop_reason"] == "idle_timeout"
    assert snapshot["idle_seconds"] == pytest.approx(60.0, abs=0.01)

    # The reaper audited BOTH stop events with source + idle_seconds.
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    stop_events = [event for event in events if event["event_type"] in ("stop", "emergency_stop")]
    assert {event["event_type"] for event in stop_events} == {"stop", "emergency_stop"}
    for event in stop_events:
        assert event["metadata"]["source"] == "idle_timeout"
        assert event["metadata"]["idle_seconds"] == pytest.approx(60.0, abs=0.01)

    # A second sweep is a no-op; the reaped id never resurrects.
    assert srv_server_lifecycle._sweep_idle_sessions() == []
    observe = srv_server_observe.computer_observe(session_id)
    payload = _reply_dict(observe)
    assert payload["ok"] is False
    assert payload["error"] == "session_stopped"
    assert "inactivity" in payload["message"] and "idle_timeout" in payload["message"]


# --- (h) a call at 899 s resets the count (tested at the 60 s knob) ----------------------------


def test_call_before_timeout_resets_count(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """A tool call just before the timeout restarts the count from THAT call."""
    session_id, _bundle, _backend = _text_session(monkeypatch)

    clock.advance(59)  # one second before the timeout
    observe = srv_server_observe.computer_observe(session_id)
    payload = json.loads(observe[0].text)
    assert payload["ok"] is True
    validate_reply(payload, "observe")

    clock.advance(59)  # 59 s after THAT call: still inside the new window
    assert srv_server_lifecycle._sweep_idle_sessions() == []
    assert session_id in srv_server_lifecycle._bundles

    clock.advance(1)  # exactly the timeout after the call: reaped now
    assert srv_server_lifecycle._sweep_idle_sessions() == [session_id]


def test_tool_entry_sweep_never_reaps_addressed_session(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """The sweep at tool entry never reaps the session the current call addresses."""
    session_id, _bundle, _backend = _text_session(monkeypatch)
    clock.advance(120)  # far past the timeout

    observe = srv_server_observe.computer_observe(session_id)  # entry sweep excluded this session
    payload = json.loads(observe[0].text)
    assert payload["ok"] is True
    assert session_id in srv_server_lifecycle._bundles  # the call itself reset the count
    assert srv_server_lifecycle._sweep_idle_sessions() == []
    clock.advance(60)
    assert srv_server_lifecycle._sweep_idle_sessions() == [session_id]  # ...until it idles out again


# --- (h) rejected calls also reset --------------------------------------------------------------


def test_rejected_call_resets_count(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """A REJECTED call is still activity: the idle count resets."""
    session_id, _bundle, _backend = _text_session(monkeypatch)

    clock.advance(59)
    rejection = _run_execute(session_id, action="key", keys=["ctrl"])
    assert rejection["ok"] is False and rejection["result"] == "rejected"
    validate_reply(rejection, "flat_execute")

    clock.advance(59)
    assert srv_server_lifecycle._sweep_idle_sessions() == []  # the rejection reset the count
    assert session_id in srv_server_lifecycle._bundles
    clock.advance(1)
    assert srv_server_lifecycle._sweep_idle_sessions() == [session_id]


def test_session_stopped_call_still_fails_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """A call on an already-stopped id changes nothing (fail-closed discipline intact)."""
    session_id, _bundle, _backend = _text_session(monkeypatch)
    asyncio.run(srv_server_lifecycle.stop_session(session_id))
    clock.advance(500)
    assert srv_server_lifecycle._sweep_idle_sessions() == []  # nothing left to reap
    rejection = _run_execute(session_id, action="click", x=1, y=1)
    assert rejection["result"] == "rejected"
    assert rejection["reasons"] == ["session_stopped"]
    validate_reply(rejection, "flat_execute")


# --- (h) in-flight sessions are never reaped ----------------------------------------------------


def test_in_flight_hook_blocks_reaping(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """The WP6 hook: a session with an in-flight wait is never reaped; idle counts from the end."""
    session_id, bundle, _backend = _text_session(monkeypatch)
    context = bundle.context

    with context.in_flight_call():
        clock.advance(10_000)  # far past the timeout while "work" is running
        assert srv_server_lifecycle._sweep_idle_sessions() == []
        assert session_id in srv_server_lifecycle._bundles
    # The hook re-based the idle clock to the END of the in-flight segment.
    clock.advance(59)
    assert srv_server_lifecycle._sweep_idle_sessions() == []
    clock.advance(1)
    assert srv_server_lifecycle._sweep_idle_sessions() == [session_id]
    assert context.in_flight_count() == 0


def test_in_flight_execute_never_reaped_reaped_after_end(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """A long-running computer_execute holds the session in flight; reaped timeout after it ends."""
    session_id, bundle, backend = _text_session(monkeypatch)

    started = threading.Event()
    release = threading.Event()

    def block_in_execute(action: Any) -> None:
        started.set()
        release.wait(timeout=30)  # bounded; the test releases it long before

    backend.execute_hooks.append(block_in_execute)

    def run_execute() -> None:
        asyncio.run(srv_server_execute.computer_execute(session_id, action="click", x=10, y=10))

    worker = threading.Thread(target=run_execute, daemon=True)
    worker.start()
    assert started.wait(timeout=10), "fake backend never reached execute"

    clock.advance(600)  # ten timeouts while the call is in flight
    assert srv_server_lifecycle._sweep_idle_sessions() == []
    assert session_id in srv_server_lifecycle._bundles

    release.set()
    worker.join(timeout=30)
    assert not worker.is_alive()
    assert bundle.context.in_flight_count() == 0
    clock.advance(59)
    assert srv_server_lifecycle._sweep_idle_sessions() == []  # idle counts from the END of the call
    clock.advance(1)
    assert srv_server_lifecycle._sweep_idle_sessions() == [session_id]


# --- (h) start_session sweeps before the limit check ---------------------------------------------


def test_start_session_at_limit_with_all_sessions_idle_expired_succeeds(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """A full registry of forgotten sessions frees itself on the next start_session."""
    monkeypatch.setattr(srv_server_lifecycle, "_registry", SessionRegistry(max_sessions=2))
    first, _b1, _ = _text_session(monkeypatch)
    second, _b2, _ = _text_session(monkeypatch)
    assert len(srv_server_lifecycle._registry) == 2

    clock.advance(61)  # both sessions are idle-expired; nothing in flight
    response = srv_server_lifecycle.start_session(dry_run=True)
    assert "session_id" in response, response
    assert first not in srv_server_lifecycle._bundles and second not in srv_server_lifecycle._bundles
    assert srv_server_lifecycle._stopped_sessions[first]["stop_reason"] == "idle_timeout"
    assert srv_server_lifecycle._stopped_sessions[second]["stop_reason"] == "idle_timeout"


def test_start_session_limit_refusal_lists_live_sessions_most_idle_first(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """Fresh sessions block the limit; the refusal lists them most-idle first."""
    monkeypatch.setattr(srv_server_lifecycle, "_registry", SessionRegistry(max_sessions=2))
    older, bundle_older, _ = _text_session(monkeypatch)
    clock.advance(30)
    newer, _bundle_newer, _ = _text_session(monkeypatch)

    refused = srv_server_lifecycle.start_session(dry_run=True)
    assert refused["ok"] is False
    assert refused["error"] == "session_limit_exceeded"
    assert refused["max_sessions"] == 2  # unchanged existing keys
    assert refused["idle_timeout_seconds"] == 60.0
    listing = refused["live_sessions"]
    assert [entry["session_id"] for entry in listing] == [older, newer]  # most idle first
    assert listing[0]["idle_seconds"] == pytest.approx(30.0, abs=0.01)
    assert listing[1]["idle_seconds"] == pytest.approx(0.0, abs=0.01)
    assert listing[0]["in_flight"] == 0
    assert listing[0]["age_seconds"] >= 30.0
    assert "stop_session" in refused["message"]
    assert len(srv_server_lifecycle._registry) == 2  # fail closed: nothing evicted

    # An in-flight session reports in_flight=1 in the same listing.
    with bundle_older.context.in_flight_call():
        refused_again = srv_server_lifecycle.start_session(dry_run=True)
        entries = {e["session_id"]: e for e in refused_again["live_sessions"]}
        assert entries[older]["in_flight"] == 1


# --- (h) a call on a reaped id says it was stopped for inactivity --------------------------------


def test_call_on_reaped_id_reports_inactivity_reason(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """session_stopped replies carry the inactivity reason with the idle seconds."""
    session_id, _bundle, _backend = _text_session(monkeypatch)
    clock.advance(60)
    assert srv_server_lifecycle._sweep_idle_sessions() == [session_id]

    observe = srv_server_observe.computer_observe(session_id)
    payload = _reply_dict(observe)
    assert payload["error"] == "session_stopped"
    assert "inactivity" in payload["message"]
    assert "60" in payload["message"]

    rejection = _run_execute(session_id, action="click", x=1, y=1)
    assert rejection["reasons"] == ["session_stopped"]
    assert "inactivity" in rejection["message"]
    validate_reply(rejection, "flat_execute")

    stopped_error = asyncio.run(srv_server_lifecycle.stop_session(session_id))  # idempotent re-stop: shape preserved
    assert stopped_error["ok"] is True
    assert stopped_error["message"] == "Session already stopped."


# --- (h) env knob: band / fallback / 0-disabled ---------------------------------------------------


def test_env_knob_band_fallback_and_disabled(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fallback-not-clamp (WP2 knob style); 0 disables; in-band values apply."""
    env = srv_server_lifecycle.SESSION_IDLE_TIMEOUT_ENV
    cases: list[tuple[str, float]] = [
        ("", 900.0),  # unset/empty -> default
        ("abc", 900.0),  # unparsable -> default
        ("59", 900.0),  # below the band -> default, NOT clamped to 60
        ("86401", 900.0),  # above the band -> default, NOT clamped
        ("nan", 900.0),  # non-finite -> default
        ("inf", 900.0),
        ("60", 60.0),  # band edges apply
        ("86400", 86400.0),
        ("120.5", 120.5),
    ]
    for raw, expected in cases:
        if raw == "":
            monkeypatch.delenv(env, raising=False)
        else:
            monkeypatch.setenv(env, raw)
        assert srv_server_lifecycle._session_idle_timeout() == expected, f"env={raw!r}"

    monkeypatch.setenv(env, "0")
    assert srv_server_lifecycle._session_idle_timeout() == 0.0  # explicitly disabled


def test_disabled_timeout_never_reaps(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """``0`` = disabled: nothing is ever reaped and the limit reply reports timeout 0."""
    monkeypatch.setenv(srv_server_lifecycle.SESSION_IDLE_TIMEOUT_ENV, "0")
    monkeypatch.setattr(srv_server_lifecycle, "_registry", SessionRegistry(max_sessions=1))
    session_id, _bundle, _backend = _text_session(monkeypatch)
    clock.advance(10_000_000)
    assert srv_server_lifecycle._sweep_idle_sessions() == []
    assert session_id in srv_server_lifecycle._bundles

    refused = srv_server_lifecycle.start_session(dry_run=True)
    assert refused["error"] == "session_limit_exceeded"
    assert refused["idle_timeout_seconds"] == 0.0
    assert [entry["session_id"] for entry in refused["live_sessions"]] == [session_id]


def test_in_band_timeout_applies_behaviorally(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """An in-band knob value reaps at ITS timeout, not the default's."""
    monkeypatch.setenv(srv_server_lifecycle.SESSION_IDLE_TIMEOUT_ENV, "120")
    session_id, _bundle, _backend = _text_session(monkeypatch)
    clock.advance(119)
    assert srv_server_lifecycle._sweep_idle_sessions() == []
    clock.advance(1)
    assert srv_server_lifecycle._sweep_idle_sessions() == [session_id]


# --- (h) concurrent calls do not corrupt counters -------------------------------------------------


def test_concurrent_calls_keep_counters_consistent(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """Threads of interleaved tool calls + in-flight marks leave the counters exact."""
    session_id, bundle, _backend = _text_session(monkeypatch)
    context = bundle.context
    errors: list[Exception] = []

    def observe_worker() -> None:
        try:
            for _ in range(4):
                result = srv_server_observe.computer_observe(session_id)
                payload = json.loads(result[0].text)
                assert payload["ok"] is True, payload
        except Exception as exc:  # pragma: no cover - surfaced via the join assertion
            errors.append(exc)

    def hook_worker() -> None:
        try:
            for _ in range(20):
                with context.in_flight_call():
                    pass
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=observe_worker) for _ in range(4)]
    threads += [threading.Thread(target=hook_worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []

    assert context.in_flight_count() == 0  # every mark was released exactly once
    assert session_id in srv_server_lifecycle._bundles
    assert srv_server_lifecycle._sweep_idle_sessions() == []  # the session is alive and fresh
    clock.advance(60)
    assert srv_server_lifecycle._sweep_idle_sessions() == [session_id]  # ...and reaps exactly once


# --- tool-surface integrity (schema unchanged by the bookkeeping wrapper) -------------------------


def test_wrapped_tools_keep_advertised_surface(fresh_server: Any) -> None:
    """The bookkeeping wrapper must not change tool names or advertised parameters."""
    manager = srv_server_app.mcp._tool_manager
    expected = {
        "start_session": None,  # not session-scoped; checked for presence only
        "stop_session": ["session_id"],
        "computer_observe": ["session_id", "visual_view", "visual_view_density", "pixel_evidence"],
        "computer_screenshot": ["session_id"],
        "computer_zoom": ["session_id", "region", "visual_view", "visual_view_density", "pixel_evidence"],
        "computer_execute": ["session_id", "action"],
    }
    for name, first_params in expected.items():
        tool = manager.get_tool(name)
        assert tool is not None, f"tool {name} not registered"
        parameters = tool.parameters.get("properties", {})
        for required_param in first_params or []:
            assert required_param in parameters, f"{name}: {required_param} missing"
    assert "session_id" in manager.get_tool("computer_execute").parameters["properties"]


def test_provider_factory_seam_still_used(fresh_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: sessions still start through the faked factories under the wrapper."""
    backend = ScriptedBackend()
    provider = ScriptedProvider([])
    monkeypatch.setattr(srv_server_lifecycle, "_backend_factory", lambda: backend)
    monkeypatch.setattr(srv_server_lifecycle, "_provider_factory", lambda: provider)
    response = srv_server_lifecycle.start_session(dry_run=True, image_delivery="text")
    assert "session_id" in response
    bundle = srv_server_lifecycle._get_bundle(str(response["session_id"]))
    assert bundle.backend is backend
    assert bundle.context.idle_seconds() == pytest.approx(0.0, abs=1.0)
