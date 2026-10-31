"""WP5 (ORVEX-2026-108 §2) — evidence-store LIFECYCLE: disposed exactly once, at
EVERY session end, and the fetch tool's relationship to the idle reaper.

New tests this work package pins (the ported store/fetch suite lives in
``test_w2_evidence_store.py``):

* the store is disposed **exactly once** on each of the three session-end paths —
  ``stop_session``, the WP-S idle reaper, and a ``start_session`` failure (a failed
  checkpoint resume discards an already-created bundle).  The instrument is a spy on
  ``server.dispose_store`` (the name the server actually calls), which records the
  store's session id per invocation;
* a fetch AFTER each end is refused, and the test documents WHICH refusal each path
  produces: stop → ``session_stopped``; idle reap → ``session_stopped`` (with the
  inactivity message); start failure → ``unknown_session`` (the session never
  became reachable).  ``store_disposed`` is the mid-teardown answer, pinned in the
  w2 suite.
* a ``computer_fetch_image`` call counts as SESSION ACTIVITY for the idle timeout
  (a fetch is activity, exactly like any other tool call — a polling agent cannot
  be reaped mid-conversation), and an in-flight fetch is never interrupted;
* the age-cap expiry is proven END-TO-END with a patched clock → ``image_expired``
  (the store-level version is in the w2 suite; this one goes through the tool).

All timing uses the patched monotonic clock (no real sleeps).
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest
from reply_contract import validate_reply
from test_controller_integration import (
    FAST_LIMITS,
    SessionRegistry,
    make_session,
)

from computer_use_mcp.vision import evidence as evidence_module
from computer_use_mcp import server


class FakeClock:
    """Controllable ``time.monotonic`` stand-in (no real sleeping)."""

    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


TEST_IDLE_TIMEOUT = "60"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Patch the monotonic clock EVERYWHERE (server + state + evidence share it)."""
    fake = FakeClock()
    monkeypatch.setattr(server.time, "monotonic", fake)
    return fake


@pytest.fixture
def fresh_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, clock: FakeClock
) -> Any:
    """Fresh registry/bundles/stopped-memory + short idle timeout + per-test audit dir."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv(server.SESSION_IDLE_TIMEOUT_ENV, TEST_IDLE_TIMEOUT)
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    monkeypatch.setattr(server, "_stopped_sessions", {})
    return server


def _metadata(result: Any) -> dict[str, Any]:
    if isinstance(result, list):
        return json.loads(result[0].text)
    return result


def _fetch(session_id: str, image_id: str) -> Any:
    """The fetch tool (ASYNC since DELTA 1 / VAL-WP5 F5) awaited from a sync test
    body via a private loop."""
    return asyncio.run(server.computer_fetch_image(session_id, image_id))


def _dispose_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Spy on ``dispose_store`` AS THE SERVER SEES IT (name-imported) and return the
    list of session ids it was called with — the exactly-once instrument."""
    calls: list[str] = []
    original = server.dispose_store

    def spy(extra: dict[str, Any]) -> int:
        store = extra.get(evidence_module.EVIDENCE_STORE_KEY)
        if store is not None:
            calls.append(str(store.session_id))
        return original(extra)

    monkeypatch.setattr(server, "dispose_store", spy)
    return calls


# --- exactly-once disposal: stop_session ------------------------------------------------------


def test_stop_session_disposes_exactly_once_and_a_later_fetch_is_refused(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    store = bundle.extra[evidence_module.EVIDENCE_STORE_KEY]
    image_id = _metadata(server.computer_observe(session_id))["image_id"]
    calls = _dispose_calls(monkeypatch)

    asyncio.run(server.stop_session(session_id))
    # The dispose spy fired exactly once even though the teardown also runs through
    # _close_stopped_bundle internals; a re-stop finds no key and fires nothing.
    asyncio.run(server.stop_session(session_id))  # idempotent re-stop
    assert calls == [session_id], calls
    assert bundle.extra.get(evidence_module.EVIDENCE_STORE_KEY) is None
    assert store.disposed is True

    # DOCUMENTED: fetch after a stop → the session-lifecycle error ``session_stopped``
    # (the bundle is popped and remembered stopped; there is no store to consult).
    reply = _fetch(session_id, image_id)
    assert reply["ok"] is False and reply["error"] == "session_stopped"


# --- exactly-once disposal: the WP-S idle reaper ----------------------------------------------


def test_idle_reap_disposes_exactly_once_and_a_later_fetch_reports_the_inactivity_stop(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    store = bundle.extra[evidence_module.EVIDENCE_STORE_KEY]
    image_id = _metadata(server.computer_observe(session_id))["image_id"]
    calls = _dispose_calls(monkeypatch)

    clock.advance(61)  # past the 60 s test timeout, nothing in flight
    reaped = server._sweep_idle_sessions()
    assert reaped == [session_id]
    assert calls == [session_id], calls
    assert server._sweep_idle_sessions() == []  # nothing left to reap → no second dispose
    assert calls == [session_id], calls

    # DOCUMENTED: fetch after an idle reap → ``session_stopped``, and the message
    # names the inactivity stop (the reaper used the stop_session kill path).
    reply = _fetch(session_id, image_id)
    assert reply["ok"] is False and reply["error"] == "session_stopped"
    assert "inactivity" in reply["message"]
    # ... and the store object itself was really emptied, not just unlinked.
    assert store.disposed is True


# --- exactly-once disposal: start_session failure --------------------------------------------


def test_a_failed_resume_start_disposes_exactly_once_and_the_session_is_unreachable(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one start path that discards an ALREADY-CREATED bundle is the failed
    checkpoint resume: the store is created with the bundle, so the failure must
    dispose it exactly once before the bundle is dropped."""
    calls = _dispose_calls(monkeypatch)

    response = server.start_session(
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
        resume_from_checkpoint="no-such-checkpoint.json",
    )
    assert response["ok"] is False, response
    # The dispose spy is ALSO how the test learns the torn-down session's id (the
    # resume refusal does not echo it): exactly one store, exactly one disposal.
    assert len(calls) == 1, calls
    session_id = calls[0]
    assert session_id not in server._bundles

    # DOCUMENTED: fetch after a start failure → ``unknown_session`` (the session was
    # never reachable: not live, and not remembered stopped — it never served a call).
    reply = _fetch(session_id, "img_0000000000000000")
    assert reply["ok"] is False and reply["error"] == "unknown_session"


# --- a fetch is session activity --------------------------------------------------------------


def test_a_fetch_counts_as_session_activity_for_the_idle_timeout(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """A polling agent that only ever re-fetches stored frames is NEVER idle: the
    fetch touches the session's activity clock like any other tool call."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    image_id = _metadata(server.computer_observe(session_id))["image_id"]

    clock.advance(50)  # near the 60 s timeout
    fetched = _fetch(session_id, image_id)
    assert isinstance(fetched, list)  # served

    clock.advance(50)  # 100 s past the LAST observe — past the ORIGINAL deadline,
    # but only 50 s past the FETCH: the fetch re-based the idle clock.
    assert server._sweep_idle_sessions() == [], "the fetch must count as activity"

    clock.advance(11)  # 61 s past the fetch with nothing else in between
    assert server._sweep_idle_sessions() == [session_id]


def test_the_idle_reaper_never_reaps_a_fetch_in_flight(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """VAL-WP5 F6: the FETCH ITSELF holds the in-flight mark.  The fetch is blocked
    INSIDE ``_fetch_image_response`` (patched) while the idle clock runs far past the
    timeout and another thread sweeps — the session survives, because the in-flight
    mark is held by the tool call itself (``_session_scoped``), not by the test."""
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    image_id = _metadata(server.computer_observe(session_id))["image_id"]

    entered = threading.Event()
    release = threading.Event()
    real = server._fetch_image_response

    async def blocking_fetch_response(store: Any, img: str) -> Any:
        entered.set()  # we are INSIDE the tool body: the marks are already held
        await asyncio.to_thread(release.wait, 10)
        return await real(store, img)

    monkeypatch.setattr(server, "_fetch_image_response", blocking_fetch_response)

    clock.advance(120)  # far past the 60 s timeout before the fetch even starts
    result: dict[str, Any] = {}

    def runner() -> None:
        result["reply"] = _fetch(session_id, image_id)

    thread = threading.Thread(target=runner)
    thread.start()
    assert entered.wait(timeout=10), "the fetch never reached its response builder"
    assert bundle.context.in_flight_count() == 1, "the fetch itself must hold the mark"

    clock.advance(120)  # 240 s idle while the fetch is in flight
    assert server._sweep_idle_sessions() == [], "an in-flight fetch is never reaped"

    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert isinstance(result["reply"], list), "the released fetch serves the frame"

    assert server._sweep_idle_sessions() == []  # the completed call re-based the clock
    clock.advance(61)
    assert server._sweep_idle_sessions() == [session_id]


async def test_a_blocked_fetch_encode_leaves_the_event_loop_free(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VAL-WP5 F5: the fetch's PIL re-encode ladder runs OFF the loop thread
    (``asyncio.to_thread``) — a fetch whose encode is deliberately BLOCKED does not
    stop another fetch on the SAME loop from completing."""
    sid_a, _bundle_a, _backend_a, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    sid_b, _bundle_b, _backend_b, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    id_a = _metadata(server.computer_observe(sid_a))["image_id"]
    id_b = _metadata(server.computer_observe(sid_b))["image_id"]

    entered = threading.Event()
    release = threading.Event()
    real = server._bound_outbound_image
    calls = {"n": 0}

    def blocking_first_encode(data_b64: str, frame: Any = None) -> Any:
        if calls["n"] == 0:
            calls["n"] += 1
            entered.set()
            assert release.wait(timeout=10), "the blocked encode was never released"
        return real(data_b64, frame=frame)

    monkeypatch.setattr(server, "_bound_outbound_image", blocking_first_encode)

    async def scenario() -> None:
        task_a = asyncio.create_task(server.computer_fetch_image(sid_a, id_a))
        for _ in range(100):  # let A reach its (blocked) encode on the worker thread
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set(), "the first fetch never reached its encode"
        # B runs on the SAME loop while A's encode is still blocked: the loop is free.
        reply_b = await asyncio.wait_for(
            server.computer_fetch_image(sid_b, id_b), timeout=5.0
        )
        assert isinstance(reply_b, list)
        release.set()
        reply_a = await asyncio.wait_for(task_a, timeout=10.0)
        assert isinstance(reply_a, list)

    await scenario()
    assert calls["n"] == 1, "only the first fetch's encode was blocked"


# --- the age cap end-to-end through the tool ---------------------------------------------------


def test_the_age_cap_is_enforced_end_to_end_with_a_patched_clock(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """``computer_fetch_image`` after 3600 s (patched) answers the typed
    ``image_expired`` rejection retired by the age cap — validated against the §1
    fetch surface.  The evidence module shares the ``time`` module with the server,
    so the one patched clock drives BOTH the idle sweep and the frame age."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    image_id = _metadata(server.computer_observe(session_id))["image_id"]

    clock.advance(3599)
    fetched = _fetch(session_id, image_id)
    assert isinstance(fetched, list)  # still inside the age window

    clock.advance(2)  # 3601 s since capture
    reply = _fetch(session_id, image_id)
    validate_reply(reply, "fetch_image")
    assert reply["ok"] is False
    assert reply["result"] == "rejected"
    assert reply["reasons"] == ["image_expired"]
    assert reply["retired_by"] == "age_cap"
