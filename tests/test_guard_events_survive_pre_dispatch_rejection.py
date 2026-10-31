"""ORVEX-2026-105 interference retention, agent side: a PRE-dispatch blocking guard
verdict publishes its event payload on the execute/queue reply.

The defect these tests pin: a blocking pre-dispatch verdict became
``ComputerUseAgent._guard_rejection_outcome``, which carried the guard event in
``reasons`` only. Post-action events reach the reply through the post-action
assignment (``interference_events=post_events``), but nothing is dispatched on a
pre-dispatch refusal — there is no post-action frame — so a pre-dispatch event could
never surface in the reply's ``interference_events`` on any path (nor, from WP7, on
the plan lease, whose cross-call carrier ``_PlanLease.interference_events`` reads
exactly this outcome field).

The pass criterion is VALUE IDENTITY — the exact event string in ``reasons[0]`` is
the exact string the reply reports under ``interference_events`` — not merely that
the key exists. Nothing here injects an event list or stubs the guard: the only
authored state is the backend's active window, the real input the focus guard reads.

The plan-lease side of the retention (events carried ACROSS stepwise-plan calls by
``_PlanLease.interference_events``) is the WP7 seam and is deliberately NOT pinned
here — the plan surface does not exist in this tree yet.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    execute_payload,
    executed_summary,
    make_session,
)

from computer_use_mcp import server
from computer_use_mcp.desktop.interference import FOCUS_TAKEN_BY
from computer_use_mcp.core.models import WindowInfo

TARGET = WindowInfo(
    hwnd=1, pid=100, process_name="EXCEL.EXE", window_class="XLMAIN", title="Book1 - Excel",
)
#: Foreign in EVERY identity field (hwnd, pid, process, class, title), so no allowlist,
#: owned-dialog or launcher rule can heal the steal back into the bound target.
FOREIGN = WindowInfo(
    hwnd=9, pid=900, process_name="notepad.exe", window_class="Notepad",
    title="user notes - Notepad",
)


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    from computer_use_mcp.core.state import SessionRegistry

    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    yield server


def _stolen_session(monkeypatch: pytest.MonkeyPatch) -> tuple[str, Any, ScriptedBackend]:
    """A session whose focus guard is ARMED on TARGET while the foreground was stolen
    by FOREIGN — both ordinary guard inputs (the public rebind + real backend state),
    the guard itself untouched."""
    backend = ScriptedBackend(flip=True, active_window=TARGET)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    bundle.agent.guard.rebind(TARGET)
    assert bundle.agent.guard.armed is True
    backend.set_active_window(FOREIGN)
    return session_id, bundle, backend


async def test_flat_reply_reports_the_pre_dispatch_guard_event(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wire-level pin: a click refused by the pre-dispatch focus guard carries the
    SAME event string as the first rejection reason, under ``interference_events``."""
    session_id, _bundle, backend = _stolen_session(monkeypatch)
    payload = execute_payload(
        await server.computer_execute(session_id, "click", x=50, y=50)
    )

    event = payload["reasons"][0]
    assert event.startswith(FOCUS_TAKEN_BY), payload
    assert "title='user notes - Notepad'" in event and "process=notepad.exe" in event
    # VALUE IDENTITY, not key presence: the reasons string and the event are the SAME.
    assert payload["interference_events"] == [event], payload
    # NEGATIVE CONTROL: the refused pre-dispatch verdict dispatched nothing.
    assert executed_summary(backend) == []


async def test_queue_stops_on_the_pre_dispatch_rejection_and_keeps_the_event(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queue path: the FIRST item's pre-dispatch rejection stops the batch with the
    named stop reason, the rejection entry keeps the refusal envelope, and the reply
    still publishes the retained event — a later speculative item never runs against a
    foreign window."""
    session_id, _bundle, backend = _stolen_session(monkeypatch)
    payload = execute_payload(
        await server.computer_execute(
            session_id, "click", x=10, y=10, include_screenshot_after=False,
            follow_ups=[{"action": "click", "x": 30, "y": 30}],
        )
    )

    event = payload["reasons"][0]
    assert event.startswith(FOCUS_TAKEN_BY), payload
    assert payload["interference_events"] == [event], payload
    assert payload["follow_ups_stopped_reason"] == "focus_taken_by", payload
    entries = payload["follow_up_results"]
    assert len(entries) == 1, entries  # the queued item NEVER ran
    assert entries[0]["kind"] == "rejected" and entries[0]["result"] == "rejected"
    assert entries[0]["reasons"][0].startswith(FOCUS_TAKEN_BY)
    assert executed_summary(backend) == []


async def test_a_clean_reply_still_carries_no_interference_events_key(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absence discipline: with the guard bound to the ACTUAL foreground (no steal),
    the same click executes and the reply carries NO ``interference_events`` key — the
    field is present only when events accumulated (additive, never an empty list)."""
    backend = ScriptedBackend(flip=True, active_window=TARGET)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    bundle.agent.guard.rebind(TARGET)
    payload = execute_payload(
        await server.computer_execute(session_id, "click", x=10, y=10)
    )
    assert payload["ok"] is True, payload
    assert "interference_events" not in payload, payload


async def test_queue_item_n_events_stay_on_the_items_own_outcome_until_wp7(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4 (VAL-WP3) — pins the CURRENT item-N≥1 behavior: a pre-dispatch refusal on a
    LATER queue item keeps its event on that item's own entry (kind=rejected, the event
    as reasons[0]), while the top-level reply's ``interference_events`` stays item 0's
    (a clean item 0 → the key is absent). The named stop reason still fires.

    WP7 SEAM: when the plan/lease aggregate lands, the reply should merge later items'
    retained events (deduped, bounded) into the aggregate — the per-item outcome field
    this test pins is exactly what that merge consumes. Do not "fix" the reply here."""
    backend = ScriptedBackend(flip=True, active_window=TARGET)
    session_id, bundle, backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    bundle.agent.guard.rebind(TARGET)
    assert bundle.agent.guard.armed is True

    stolen = {"n": 0}

    def steal_on_first_dispatch(action: Any) -> None:
        # item 0 runs clean against the bound target; the steal lands DURING item 0's
        # dispatch, so item 1's pre-dispatch guard check is the one that refuses
        if stolen["n"] == 0:
            stolen["n"] = 1
            backend.set_active_window(FOREIGN)

    backend.execute_hooks.append(steal_on_first_dispatch)

    payload = execute_payload(
        await server.computer_execute(
            session_id, "click", x=10, y=10, include_screenshot_after=False,
            follow_ups=[{"action": "click", "x": 30, "y": 30}],
        )
    )

    entries = payload["follow_up_results"]
    assert len(entries) == 2, entries  # the batch stopped at item 1
    assert entries[0]["kind"] == "executed" and entries[0]["ok"] is True
    assert entries[1]["kind"] == "rejected"
    event = entries[1]["reasons"][0]
    assert event.startswith(FOCUS_TAKEN_BY), entries[1]
    # CURRENT behavior: item 1's event rides the item's own entry; the reply aggregate
    # carries only item 0's (none) — the WP7 deferral recorded in the gate notes.
    assert "interference_events" not in payload, payload
    assert payload["follow_ups_stopped_reason"] == "focus_taken_by", payload
    # the foreign window never received item 1's click
    assert len(executed_summary(backend)) == 1
