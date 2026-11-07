"""N1 (VAL-WP6-02 carry-over, fixed in WP4b) — each blocked waiter is woken on its
OWN loop.

The pre-fix code fired every waiter's ``asyncio.Event`` on the loop that CREATED the
wait (``handle.loop``). A ``computer_wait_status``/``computer_cancel_wait`` call
running on a DIFFERENT loop than the originating ``computer_wait`` then had its wake
delivered to the wrong loop — and when the original loop had already closed (the
normal case here: ``asyncio.run`` closes it when the first call returns), the wake was
swallowed entirely and the second call fell back to the full segment or hung. The fix
stores ``(own loop, event)`` per waiter (captured with ``asyncio.get_running_loop()``
at registration) and fires each event on its own loop — in ``_signal_loop_waiters``
and in the real-time segment Timer alike.

Production runs one stdio loop, so this was latent — exactly why it is pinned now,
while the reproduction is a two-thread unit test on stubbed backends.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest
from test_controller_integration import FAST_LIMITS, make_session

import computer_use_mcp.server.lifecycle as srv_server_lifecycle
import computer_use_mcp.server.wait_machine as srv_server_wait_machine
import computer_use_mcp.server.wait as srv_server_wait
import computer_use_mcp.server.state as srv_server_state
from computer_use_mcp.core.state import SessionRegistry

TEST_SEGMENT_SECONDS = 0.2


@pytest.fixture
def wait_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh registries + short blocking segment (same as the w6 suite)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(srv_server_lifecycle, "_registry", SessionRegistry(max_sessions=16))
    monkeypatch.setattr(srv_server_wait_machine, "_WAITS_IN_FLIGHT", {})
    monkeypatch.setattr(srv_server_wait, "_WAITS_IN_FLIGHT", srv_server_wait_machine._WAITS_IN_FLIGHT)
    monkeypatch.setattr(srv_server_wait_machine, "_WAIT_TOMBSTONES", {})
    monkeypatch.setattr(srv_server_wait, "_WAIT_TOMBSTONES", srv_server_wait_machine._WAIT_TOMBSTONES)
    monkeypatch.setattr(srv_server_lifecycle, "_bundles", {})
    monkeypatch.setattr(srv_server_wait_machine, "_bundles", srv_server_lifecycle._bundles)
    monkeypatch.setattr(srv_server_wait, "_bundles", srv_server_lifecycle._bundles)
    monkeypatch.setattr(srv_server_lifecycle, "_stopped_sessions", {})
    monkeypatch.setattr(srv_server_state, "_stopped_sessions", srv_server_lifecycle._stopped_sessions)
    monkeypatch.setattr(srv_server_wait, "_wait_segment_seconds", lambda: TEST_SEGMENT_SECONDS)
    yield
    for registry in list(srv_server_wait_machine._WAITS_IN_FLIGHT.values()):
        handles = list(registry.values())
        for handle in handles:
            handle.cancel.set()
        srv_server_wait_machine._join_wait_threads(handles)


def test_status_on_a_second_loop_is_woken_on_its_own_loop(
    wait_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reproduction from the review: start a wait on one ``asyncio.run`` loop
    (which CLOSES when that call returns its ``running`` segment reply), then block a
    ``computer_wait_status`` on a SECOND loop. The completion wake must reach the
    status call on its own loop — pre-fix it went to the closed first loop, was
    swallowed, and the status call never woke (it missed the segment Timer too, which
    also fired on the closed loop)."""
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )

    # Loop A: create the wait (0.5 s > one 0.2 s segment) and let its call RETURN —
    # the reply is ``running`` + wait_id, and loop A is closed by asyncio.run.
    first = asyncio.run(srv_server_wait.computer_wait(session_id, 0.5))
    assert first["status"] == "running", first
    wait_id = str(first["wait_id"])
    deadline_gap = 0.5 - TEST_SEGMENT_SECONDS  # ~0.3 s of wait left when B starts

    box: dict[str, Any] = {}

    def _status_on_own_loop() -> None:
        started = time.monotonic()
        reply = asyncio.run(srv_server_wait.computer_wait_status(session_id, wait_id))
        box["elapsed"] = time.monotonic() - started
        box["reply"] = reply

    thread = threading.Thread(target=_status_on_own_loop, name="n1-status-loop-b")
    thread.start()
    thread.join(timeout=5.0)
    assert not thread.is_alive(), (
        "the status call on its own loop was never woken — the completion wake went "
        "to the (closed) loop that created the wait"
    )
    reply = box["reply"]
    # The first blocked segment can legitimately return ``running`` (its real-time
    # Timer fired mid-wait); re-attach until the wait's own expiry completes it. The
    # N1 assertion is the WALL CLOCK: every segment round must wake on B's own loop
    # at the wait's expiry (≈0.3 s of wait left when B started), never the closed
    # loop-A swallow that used to leave the caller hanging.
    rounds = 0
    while reply["status"] == "running" and rounds < 4:
        started = time.monotonic()
        reply = asyncio.run(srv_server_wait.computer_wait_status(session_id, wait_id))
        rounds += 1
    assert reply["status"] == "completed", reply
    assert box["elapsed"] < deadline_gap + 0.15, box["elapsed"]
    validate = reply  # shape check stays on the §1 marker envelope
    assert validate["result"] == "marker"
    assert validate["ok"] is True
