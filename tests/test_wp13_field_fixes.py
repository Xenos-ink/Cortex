"""WP13 — field fixes from the first real ZCode session (offline pins).

Field evidence (sess_80396a19): (e1) computer_wait(30) hit the host's 30 s tool
timeout because the wait segment (45 s) assumed a 60 s client; (e2) a click with
a 2 s wait follow-up + screenshot timed out at 30 s; (e4) a direct ``hold_ms``
argument was silently dropped; (e5) ``plan_action='start'`` did not teach how to
start; (e6) serverInfo.version reported the mcp SDK version.

Pinned here, all offline:
- the client-timeout knob and every DERIVED bound (A): at a 30 s host the wait
  segment, the type deadline and the pacing budget are all <= 15 s; at the
  default 60 s they reproduce the shipped 45/36/45;
- the follow-up chain stops with ``time_budget`` instead of being killed (A);
- the e2 call shape's intrinsic cost is bounded (B reproduction + breakdown);
- unknown tool arguments are rejected fail-closed with a typed error (C);
- ``initialize`` reports Cortex's own version (F);
- ensure_app names the owning process of a matching visible window title as a
  HINT and never attaches by title (D).
"""

from __future__ import annotations

import json
import threading
import re
from typing import Any

import pytest
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    execute_payload,
    make_session,
)

import computer_use_mcp
import computer_use_mcp.desktop.backend as backend_module
import computer_use_mcp.pipeline.agent as agent_module
import computer_use_mcp.server.execute as srv_server_execute
from computer_use_mcp.desktop.client_budget import (
    CLIENT_TIMEOUT_MAX_SECONDS,
    CLIENT_TIMEOUT_MIN_SECONDS,
    client_timeout_seconds,
    pacing_budget_seconds,
    type_action_deadline_seconds,
    wait_segment_default_seconds,
)
from computer_use_mcp.core.state import SessionRegistry
from computer_use_mcp.server.instance import mcp
from computer_use_mcp.planning import plan as srv_planning_plan

from computer_use_mcp.server import wait_machine as srv_wait_machine
from computer_use_mcp.server import wait_machine as srv_server_wait_machine
from computer_use_mcp.server import lifecycle as srv_server_lifecycle
from computer_use_mcp.server import wait as srv_server_wait


# --- m14 (WP13 DELTA-3, Commander ruling): NO real COM, NO real capture ----------------------


class _OfflineSemanticReader:
    """The offline semantic-reader seam (m14): never touches real UIA COM or
    the desktop. Reads degrade honestly to None (blind/read_timeout)."""

    value_source = "uia"
    last_degraded_reason: str | None = None
    warm_state: str | None = "ok"

    @property
    def available(self) -> bool:
        return True

    def warm(self) -> str:
        return "ok"

    def read(self) -> dict[str, object] | None:
        return None


def _offline_grab_png(
    self: Any, left: int, top: int, width: int, height: int
) -> tuple[str, int, int, Any]:
    """The offline capture seam (m14): a synthetic PNG, zero desktop contact."""
    import base64
    import io

    from PIL import Image as PILImage

    image = PILImage.new("RGB", (max(width, 1), max(height, 1)), (250, 250, 250))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return encoded, width, height, image


@pytest.fixture(autouse=True)
def _wp13_offline_desktop_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """m14 guard + seams for EVERY WP13 test: no real capture, no real COM.

    - ``UiaSemanticReader`` and ``LocalComputerBackend._grab_png`` are patched
      to offline stubs for the duration of the test;
    - a sentinel on ``LocalComputerBackend.__init__`` FAILS the test if a
      backend is constructed while the REAL seams are still armed (i.e. any
      construction that would do a real UIA warm / real capture). The session
      ``real_backend`` fixture (conftest.py) arms its OWN offline seams before
      constructing — N4 (VAL-WP13-02) — so it passes this sentinel whatever the
      test order, and the WP13 module passes ALONE.
    """
    real_uia = backend_module.UiaSemanticReader
    monkeypatch.setattr(backend_module, "UiaSemanticReader", _OfflineSemanticReader)
    monkeypatch.setattr(
        backend_module.LocalComputerBackend, "_grab_png", _offline_grab_png
    )
    original_init = backend_module.LocalComputerBackend.__init__

    def _guarded_init(self: Any, *args: Any, **kwargs: Any) -> None:
        if backend_module.UiaSemanticReader is real_uia:
            raise AssertionError(
                "m14 guard: an UNPATCHED LocalComputerBackend construction in a "
                "WP13 test (real UIA COM warm + real capture are forbidden "
                "offline) — build it while the offline seams are armed"
            )
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(backend_module.LocalComputerBackend, "__init__", _guarded_init)
    yield

from computer_use_mcp.server import plan_surface as srv_server_plan_surface


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """The batch7 server-isolation fixture (registries + plan guard per test)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(srv_server_lifecycle, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(srv_server_lifecycle, "_bundles", {})
    monkeypatch.setattr(srv_server_wait_machine, "_bundles", srv_server_lifecycle._bundles)
    monkeypatch.setattr(srv_server_wait, "_bundles", srv_server_lifecycle._bundles)
    monkeypatch.setattr(srv_server_plan_surface, "_plan_guard", srv_planning_plan.PlanLeaseGuard())
    monkeypatch.setattr(srv_server_execute, "_plan_guard", srv_server_plan_surface._plan_guard)
    monkeypatch.setattr(srv_server_lifecycle, "_plan_guard", srv_server_plan_surface._plan_guard)
    yield



# --- A: the client-timeout knob and every derived bound -------------------------------------


def test_default_budget_reproduces_the_shipped_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CORTEX_CLIENT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("CORTEX_WAIT_SEGMENT_SECONDS", raising=False)
    assert client_timeout_seconds() == 60.0
    assert type_action_deadline_seconds() == 45.0
    assert pacing_budget_seconds() == 36.0
    assert wait_segment_default_seconds() == 45.0
    assert srv_wait_machine._wait_segment_seconds() == 45.0  # unchanged default behaviour


def test_a_30s_host_derives_every_bound_below_30s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORTEX_CLIENT_TIMEOUT_SECONDS", "30")
    monkeypatch.delenv("CORTEX_WAIT_SEGMENT_SECONDS", raising=False)
    timeout = client_timeout_seconds()
    assert timeout == 30.0
    # every derived bound is below 30 s minus the fixed 15 s margin
    assert type_action_deadline_seconds() <= 30.0 - 15.0
    assert pacing_budget_seconds() <= 30.0 - 15.0
    assert wait_segment_default_seconds() <= 30.0 - 15.0
    assert srv_wait_machine._wait_segment_seconds() <= 30.0 - 15.0


def test_env_band_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for bad in ("9", "601", "abc", "-3", "inf", ""):
        monkeypatch.setenv("CORTEX_CLIENT_TIMEOUT_SECONDS", bad)
        assert client_timeout_seconds() == 60.0, bad
    monkeypatch.setenv("CORTEX_CLIENT_TIMEOUT_SECONDS", "30")
    assert client_timeout_seconds() == 30.0
    assert CLIENT_TIMEOUT_MIN_SECONDS == 10.0 and CLIENT_TIMEOUT_MAX_SECONDS == 600.0


def test_explicit_wait_segment_override_wins_inside_the_band_falls_back_outside(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELTA 1: the valid band is [1, client timeout − 5]; an in-band override
    WINS; an out-of-band or unparsable value FALLS BACK to the derived default
    (fallback-not-clamp)."""
    monkeypatch.delenv("CORTEX_CLIENT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("CORTEX_WAIT_SEGMENT_SECONDS", "20")
    assert srv_wait_machine._wait_segment_seconds() == 20.0  # explicit wins
    monkeypatch.setenv("CORTEX_WAIT_SEGMENT_SECONDS", "55")
    assert srv_wait_machine._wait_segment_seconds() == 55.0  # at the 60 s ceiling
    monkeypatch.setenv("CORTEX_WAIT_SEGMENT_SECONDS", "56")
    assert srv_wait_machine._wait_segment_seconds() == 45.0  # out of band -> default
    monkeypatch.setenv("CORTEX_WAIT_SEGMENT_SECONDS", "100")
    assert srv_wait_machine._wait_segment_seconds() == 45.0  # fallback, not clamp
    monkeypatch.setenv("CORTEX_WAIT_SEGMENT_SECONDS", "0.5")
    assert srv_wait_machine._wait_segment_seconds() == 45.0  # below floor -> default
    monkeypatch.setenv("CORTEX_WAIT_SEGMENT_SECONDS", "abc")
    assert srv_wait_machine._wait_segment_seconds() == 45.0
    monkeypatch.setenv("CORTEX_CLIENT_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("CORTEX_WAIT_SEGMENT_SECONDS", "25")
    assert srv_wait_machine._wait_segment_seconds() == 25.0  # at the 30 s ceiling
    monkeypatch.setenv("CORTEX_WAIT_SEGMENT_SECONDS", "26")
    # out of the 30 s host's band -> falls back to the derived 15 s default
    assert srv_wait_machine._wait_segment_seconds() == 15.0


# --- DELTA 1 item 2: UIA COM calls are bounded by a worker-thread wall -----------------------


def test_semantic_read_timeout_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The UIA read wall derives from the client timeout: 2 s cap at the shipped
    defaults and at a 30 s host, 1 s at a 10 s host, 0.5 s floor. DELTA 2: the
    values are PINNED at 10/30/60 and the warm wall (full budget share capped
    at 10 s) is pinned too."""
    from computer_use_mcp.desktop.client_budget import (
        semantic_read_timeout_seconds,
        semantic_warm_timeout_seconds,
    )

    monkeypatch.delenv("CORTEX_CLIENT_TIMEOUT_SECONDS", raising=False)
    assert semantic_read_timeout_seconds() == 2.0  # min(2, 60/10)
    assert semantic_warm_timeout_seconds() == 10.0  # min(10, 45)
    monkeypatch.setenv("CORTEX_CLIENT_TIMEOUT_SECONDS", "30")
    assert semantic_read_timeout_seconds() == 2.0  # min(2, 30/10 = 3)
    assert semantic_warm_timeout_seconds() == 10.0  # min(10, 15)
    monkeypatch.setenv("CORTEX_CLIENT_TIMEOUT_SECONDS", "10")
    assert semantic_read_timeout_seconds() == 1.0  # min(2, 10/10 = 1)
    assert semantic_warm_timeout_seconds() == 5.0  # min(10, deadline 5)


def test_uia_read_survives_a_hung_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELTA-1 stage audit (field e2): one UIA COM call into a hung WebView2
    provider can block forever (the in-read 0.05 s budget is only checked
    BETWEEN calls). ``read()`` must return None within the derived wall."""
    import time as _time

    import computer_use_mcp.desktop.text_readers as text_readers_module

    reader = text_readers_module.UiaSemanticReader()
    release = threading.Event()

    def _hang() -> None:
        release.wait(5.0)  # simulates a COM call stuck in a hung provider

    monkeypatch.setattr(reader, "_read_unbounded", _hang)
    monkeypatch.setattr(text_readers_module, "semantic_read_timeout_seconds", lambda: 0.2)
    began = _time.perf_counter()
    assert reader.read() is None
    took = _time.perf_counter() - began
    release.set()
    assert took < 1.5, f"read() blocked {took:.3f}s past the 0.2 s wall"


def test_uia_warm_survives_a_hung_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same wall for ``warm()`` — ``start_session``'s backend init must never
    hang on a stuck provider either."""
    import time as _time

    import computer_use_mcp.desktop.text_readers as text_readers_module

    reader = text_readers_module.UiaSemanticReader()
    release = threading.Event()

    def _hang() -> bool:
        release.wait(5.0)
        return True

    monkeypatch.setattr(reader, "_warm_unbounded", _hang)
    monkeypatch.setattr(text_readers_module, "semantic_warm_timeout_seconds", lambda: 0.2)
    began = _time.perf_counter()
    assert reader.warm() == "timeout"
    took = _time.perf_counter() - began
    release.set()
    assert took < 1.5, f"warm() blocked {took:.3f}s past the 0.2 s wall"


# --- DELTA 2 #1: the single COM owner thread ---------------------------------------------------


def test_uia_calls_run_on_one_owner_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """DELTA 2 BLOCKER: EVERY UIA call (warm, read) runs on the reader's single
    long-lived OWNER thread — the thread that owns the COM apartment and the
    automation object — never on a fresh per-call thread and never on the
    caller's thread."""
    import threading as _threading

    import computer_use_mcp.desktop.text_readers as text_readers_module

    owner_name = "cortex-uia-com-owner"
    owners_before = [t for t in threading.enumerate() if t.name == owner_name]
    reader = text_readers_module.UiaSemanticReader()
    seen: list[int] = []
    snapshot = {"focused": {"value": "x"}, "elements": []}

    def _record() -> dict[str, object]:
        seen.append(_threading.get_ident())
        return snapshot

    monkeypatch.setattr(reader, "_read_unbounded", _record)
    monkeypatch.setattr(text_readers_module, "semantic_read_timeout_seconds", lambda: 0.5)
    assert reader.read() == snapshot
    assert reader.read() == snapshot
    assert len(seen) == 2
    assert seen == [reader.owner_thread_ident, reader.owner_thread_ident]
    assert seen[0] != _threading.get_ident()  # not the caller's thread
    # m13 (VAL-WP13-01): the EXACT owner-thread check — exactly ONE thread named
    # cortex-uia-com-owner exists beyond the pre-existing ones, and it IS the
    # thread every call ran on.
    owners_after = [t for t in threading.enumerate() if t.name == owner_name]
    assert len(owners_after) == len(owners_before) + 1
    assert owners_after[-1].ident == reader.owner_thread_ident


def test_uia_hang_busy_gate_and_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """DELTA 2: a hung call returns None within the wall; a SECOND call during
    the hang returns None AT ONCE (busy — no second worker, never queued behind
    the hang); after the hang ends the reader RECOVERS and serves new requests
    (the stale result is dropped, the busy gate clears)."""
    import time as _time

    import computer_use_mcp.desktop.text_readers as text_readers_module

    reader = text_readers_module.UiaSemanticReader()
    release = threading.Event()
    calls: list[int] = []

    def _hang() -> dict[str, object]:
        calls.append(_time.perf_counter())
        release.wait(5.0)
        return {"stale": True}  # would be a LATE result if a caller were waiting

    monkeypatch.setattr(reader, "_read_unbounded", _hang)
    monkeypatch.setattr(text_readers_module, "semantic_read_timeout_seconds", lambda: 0.2)
    began = _time.perf_counter()
    assert reader.read() is None
    assert reader.last_degraded_reason == "read_timeout"
    # busy gate: the second call during the hang answers AT ONCE
    busy_began = _time.perf_counter()
    assert reader.read() is None  # not queued behind the hang
    busy_took = _time.perf_counter() - busy_began
    assert busy_took < 0.1, f"busy call waited {busy_took:.3f}s (should be instant)"
    # recovery
    release.set()
    deadline = _time.perf_counter() + 2.0
    while reader._worker._busy_lock.locked() and _time.perf_counter() < deadline:
        _time.sleep(0.01)
    monkeypatch.setattr(reader, "_read_unbounded", lambda: {"fresh": True})
    assert reader.read() == {"fresh": True}
    assert reader.last_degraded_reason is None
    assert len(calls) == 1  # exactly ONE worker request ever ran


def test_typing_value_read_during_a_hang_is_read_timeout_never_repaired(
    real_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 2: the typing ladder's value read during a UIA owner-wall hang is
    the VISIBLE ``branch=read_timeout`` — never verified, never repaired (the
    dispatches are the typed chunks only)."""
    from test_wp12b_baseline_ladder import _dispatches, _type_action, _typing_backend

    E = "test"
    engine, _reader = _typing_backend(
        real_backend, monkeypatch, reader_values=[], control_type=None
    )

    def _hung_read() -> None:
        real_backend._last_read_degraded = "read_timeout"
        return None

    monkeypatch.setattr(real_backend, "read_focused_control_value", _hung_read)
    message = real_backend.execute(_type_action(E))
    assert "integrity=unverified(0/4)" in message and "branch=read_timeout" in message, message
    assert _dispatches(engine) == [E]  # chunks only — never repaired


# --- DELTA 1 item 3: per-item REMAINING budget -----------------------------------------------


class _QueueClock:
    """Controllable monotonic clock for the queue-budget checks (no real sleeping)."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _patch_agent_clock(monkeypatch: pytest.MonkeyPatch, budget: float) -> _QueueClock:
    """Freeze agent-module monotonic time; everything else keeps real time."""
    import time as _time
    from types import SimpleNamespace

    clock = _QueueClock()
    monkeypatch.setattr(agent_module, "time", SimpleNamespace(monotonic=clock, perf_counter=_time.perf_counter))
    monkeypatch.setattr(agent_module, "type_action_deadline_seconds", lambda: budget)
    return clock


async def test_wait_follow_up_exceeding_the_remaining_budget_is_not_dispatched(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 #3: item 0 consumes 14 s of a 15 s budget; a wait delta 2 item
    would overrun the remaining 1 s, so it is NOT dispatched (time_budget,
    exactly one dispatch) instead of running past the host timeout."""
    clock = _patch_agent_clock(monkeypatch, 15.0)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    seen_deadlines: list[Any] = []

    def _consume_14(action: Any) -> None:
        seen_deadlines.append(getattr(backend, "_queue_deadline_seconds", None))
        assert action.action.value == "done"
        clock.advance(14.0)

    backend.execute_hooks.append(_consume_14)
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            follow_ups=[{"action": "wait", "delta": 2}],
        )
    )
    assert payload["follow_ups_stopped_reason"] == "time_budget", payload
    assert len(payload["follow_up_results"]) == 1  # only item 0 ran
    assert len(backend.executed) == 1  # the wait NEVER dispatched
    assert seen_deadlines == [None]  # item 0 is not deadline-capped


async def test_type_follow_up_deadline_is_capped_to_the_remaining_budget(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 #3: item 0 consumes 14 s of a 15 s budget; a 2000-char type item
    dispatches but its deadline/pacing is capped to the remaining 1 s (never its
    own derived 45 s), and the override is restored after the item."""
    clock = _patch_agent_clock(monkeypatch, 15.0)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
        backend=ScriptedBackend(flip=False),
    )
    seen_deadlines: list[float] = []
    seen_item0_deadline: list[float] = []

    def _hook(action: Any) -> None:
        if action.action.value == "done":
            seen_item0_deadline.append(getattr(backend, "_queue_deadline_seconds", None))
            clock.advance(14.0)
        else:
            seen_deadlines.append(getattr(backend, "_queue_deadline_seconds", None))

    backend.execute_hooks.append(_hook)
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            follow_ups=[{"action": "type", "text": "x" * 2000}],
        )
    )
    assert payload.get("follow_ups_stopped_reason") in (None, "completed"), payload
    assert len(payload["follow_up_results"]) == 2
    assert seen_deadlines == [1.0]  # min(45, 15 - 14) — NOT the full 45 s
    assert seen_item0_deadline == [None]  # item 0 runs uncapped
    assert getattr(backend, "_queue_deadline_seconds", None) is None  # restored


async def test_plan_point_items_share_the_same_time_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELTA 1 #3 (plan path): a stepwise plan point enters the SAME queue loop
    (run_single plan_policies -> _run_action_queue), so an item that would
    overrun the budget stops with time_budget there too — one plan call cannot
    block past the host timeout."""
    from types import SimpleNamespace

    from computer_use_mcp.core.limits import Limits
    from computer_use_mcp.core.models import ActionSpec, GroundedAction, Point
    from computer_use_mcp.core.state import StopToken, TaskState
    from computer_use_mcp.desktop.fake_backend import FakeComputerBackend
    from computer_use_mcp.pipeline.agent import ComputerUseAgent
    from computer_use_mcp.security.safety import SafetyPolicy

    clock = _QueueClock()
    import time as _time

    monkeypatch.setattr(
        agent_module,
        "time",
        SimpleNamespace(monotonic=clock, perf_counter=_time.perf_counter),
    )
    monkeypatch.setattr(agent_module, "type_action_deadline_seconds", lambda: 1.0)
    backend = FakeComputerBackend()
    agent = ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        limits=Limits(min_screenshot_interval_ms=1, max_actions=100, max_task_seconds=600.0).validate(),
    )
    state = SimpleNamespace(
        dry_run=False,
        stopped=False,
        allowed_windows=[],
        min_confidence=0.0,
        max_steps=100,
        step_count=0,
        require_approval=False,
        max_retries_per_action=1,
    )
    outcome = await agent.run_single(
        state,
        GroundedAction(action="click", point=Point(x=100, y=100), confidence=1.0),
        approved=True,
        follow_ups=[ActionSpec(action="wait", delta=2)],
        # plan_policies entries of None: the settle markers stay unset and NO
        # point policy stops the batch — so the surfaced stop reason is the
        # budget gate itself (m13: the test asserts time_budget ONLY).
        plan_policies=[None, None],
    )
    assert outcome.follow_ups_stopped_reason == "time_budget"
    assert len(outcome.follow_up_results) == 1  # the wait never dispatched
    assert len(backend.executed) == 1


# --- A: the per-call time budget stops follow-up chains --------------------------------------


async def test_follow_up_chain_stops_on_the_time_budget(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A follow-up chain whose budget is spent stops with the typed
    ``time_budget`` reason — the call returns to the host instead of being
    killed by the host's tool timeout (field e1/e2)."""
    monkeypatch.setattr(agent_module, "type_action_deadline_seconds", lambda: 1e-6)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            follow_ups=[{"action": "wait", "delta": 1} for _ in range(5)],
        )
    )
    assert payload["follow_ups_stopped_reason"] == "time_budget", payload
    assert len(payload["follow_up_results"]) == 1  # only item 0 ran
    assert backend.executed and len(backend.executed) == 1


# --- B: the e2 call shape's intrinsic cost ---------------------------------------------------


async def test_e2_shape_intrinsic_cost_is_bounded_and_timed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, real_backend: Any
) -> None:
    """B reproduction (field e2): click + expected_effect + a 2 s wait follow-up +
    include_screenshot_after. The recorded breakdown shows the intrinsic cost is
    ~2.2 s of sleeps (pacing 0.04 + settle 0.05 + verify gaps 0.08 + the wait's
    2.0 s) plus capture/encode — NO stage reaches 30 s. (Field-log RETRACTION,
    DELTA 1: e1's wait ran 25 minutes AFTER e2, so the old "queued behind e1"
    reading was false; the honest B status is NOT_REPRODUCED — the field stall is
    being hunted via the per-stage bounds of the DELTA-1 audit.)"""
    import time as _time

    recorded: list[float] = []
    real_sleep = _time.sleep

    def _record_sleep(seconds: float) -> None:
        recorded.append(float(seconds))

    monkeypatch.setattr(backend_module.time, "sleep", _record_sleep)

    class _FakeInputUser32:
        """SendInput seam stub (the click must not touch the real desktop)."""

        def SendInput(self, count: int, array: Any, cb_size: int) -> int:
            return count

        def GetCursorPos(self, point: Any) -> int:
            point.x, point.y = 960, 540  # center: NOT a failsafe corner
            return 1

        def GetSystemMetrics(self, index: int) -> int:
            return (1920, 1080)[index]

    import computer_use_mcp.desktop.input_engine as input_engine_module

    from recording_engine import RecordingEngine

    monkeypatch.setattr(input_engine_module, "_user32", _FakeInputUser32())
    # the click is dispatched through a RECORDING engine (no real input); the rest
    # of the real backend (capture, verification, follow-up waits) stays real.
    recording_engine = RecordingEngine()
    recording_engine.type_interval = 0.0  # the click is not paced (WP12a paces TYPE)
    monkeypatch.setattr(real_backend, "_engine", recording_engine)
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, backend=real_backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    started = _time.monotonic()
    # WP13 B: the call goes through the PROTOCOL path (FastMCP.call_tool) so the
    # per-call ` elapsed_ms=` suffix is applied by the server-layer wrapper.
    response = await mcp.call_tool(
        "computer_execute",
        {
            "session_id": session_id,
            "action": "click",
            "x": 771,
            "y": 838,
            "expected_effect": "the click landed",
            "follow_ups": [{"action": "wait", "delta": 2}],
            "include_screenshot_after": True,
            "approved": True,
        },
    )
    wall = _time.monotonic() - started
    # B breakdown (offline, click shape): the wait FOLLOW-UP dominates when it
    # dispatches — {"action": "wait", "delta": 2} sleeps 2.0 s server-side (the
    # stop-token slice loop, desktop/motion.py:46-54) — plus the click dispatch
    # (instant) and the post capture/JPEG. NO intrinsic stage reaches 30 s. The
    # offline run may refuse the zero-effect wait follow-up at the grounding gate
    # (offline grounding is stricter than the field run); EITHER way the whole
    # call is bounded far under the host timeout — that is what this pin proves.
    assert sum(recorded) <= 1.0, recorded
    assert wall < 5.0, f"the e2 shape took {wall:.2f}s wall (intrinsic bound)"
    # the executed reply carries the per-call timing inside the message
    payload = execute_payload(response)
    # the offline click is fast — the ` elapsed_ms=` suffix only appears on slow
    # calls (>= 3 s), pinned separately in test_slow_call_reply_carries_elapsed_ms
    assert "Executed click." in payload.get("message", ""), payload.get("message")
    observed = {
        "stopped_reason": payload.get("follow_ups_stopped_reason"),
        "follow_up_results": payload.get("follow_up_results"),
        "wall_s": round(wall, 3),
        "sleeps": recorded,
    }
    if observed["stopped_reason"] is None or observed["stopped_reason"] == "completed":
        # the whole queue ran: the wait follow-up's 2.0 s must show in the wall time
        assert wall >= 1.9, f"the 2 s wait follow-up did not sleep: {observed!r}"
    assert observed["stopped_reason"] in {None, "completed", "rejected"}, observed


async def test_slow_call_reply_carries_elapsed_ms(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch, real_backend: Any
) -> None:
    """WP13 B: a slow call (>= 3 s) carries `` elapsed_ms=<n>`` inside the reply
    message (contract-sanctioned key=value suffix), so the next field report
    shows real host-side timings."""
    import computer_use_mcp.desktop.input_engine as input_engine_module

    from recording_engine import RecordingEngine

    class _FakeInputUser32:
        def SendInput(self, count: int, array: Any, cb_size: int) -> int:
            return count

        def GetCursorPos(self, point: Any) -> int:
            point.x, point.y = 960, 540
            return 1

        def GetSystemMetrics(self, index: int) -> int:
            return (1920, 1080)[index]

    monkeypatch.setattr(input_engine_module, "_user32", _FakeInputUser32())

    import time as _time

    from computer_use_mcp.desktop.backend import LocalComputerBackend

    class _SlowDispatchBackend(LocalComputerBackend):
        """A real backend whose dispatch sleeps 3.2 s (the slow-call shape)."""

        def execute(self, action: Any, stop: Any = None, **kwargs: Any) -> str:
            _time.sleep(3.2)
            return super().execute(action, stop, **kwargs)

    slow_backend = _SlowDispatchBackend()
    recording_engine = RecordingEngine()
    recording_engine.type_interval = 0.0
    monkeypatch.setattr(slow_backend, "_engine", recording_engine)
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, backend=slow_backend, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await mcp.call_tool(
        "computer_execute",
        {
            "session_id": session_id,
            "action": "click",
            "x": 771,
            "y": 838,
            "approved": True,
        },
    )
    payload = execute_payload(response)
    match = re.search(r"elapsed_ms=(\d+)", payload.get("message", ""))
    assert match, f"no elapsed_ms in a slow (3.2 s dispatch) reply: {payload.get('message')!r}"
    assert int(match.group(1)) >= 3000, payload.get("message")


# --- C: unknown tool arguments are rejected fail-closed --------------------------------------


def _reply_body(payload: Any) -> dict[str, Any]:
    """Unwrap ``call_tool`` output (FastMCP tuple / content-block list / dict) to
    the reply dict — whatever content form that tool's normal replies use."""
    from reply_contract import assert_rejection

    if isinstance(payload, tuple) and len(payload) == 2:
        structured = payload[1]
        assert isinstance(structured, dict)
        return structured
    if isinstance(payload, dict):
        return payload
    assert isinstance(payload, list), type(payload)
    body = json.loads(payload[0].text)
    assert isinstance(body, dict)
    return body


def _assert_unknown_argument(body: dict[str, Any]) -> None:
    """M1 (VAL-WP13-01): the rejection is a CONTRACT rejection — run through
    ``assert_rejection`` (no ``error`` key) with the typed reason + teaching."""
    from reply_contract import assert_rejection

    assert_rejection(body)
    assert "unknown_argument" in body.get("reasons", []), body
    assert "unknown argument(s)" in body.get("message", ""), body


async def test_unknown_arguments_are_rejected_fail_closed() -> None:
    payload = await mcp.call_tool(
        "computer_execute",
        {"session_id": "s", "action": "click", "x": 1, "y": 2, "hold_ms": 250},
    )
    body = _reply_body(payload)
    _assert_unknown_argument(body)
    assert "hold_ms" in body["message"], body
    assert "follow_ups" in body["message"], body  # teaches where hold_ms belongs


async def test_unknown_arguments_are_rejected_on_every_tool() -> None:
    for name, args in (
        ("computer_wait", {"duration_seconds": 1.0, "hold_ms": 5}),
        ("start_session", {"foo": 1}),
        ("computer_observe", {"visual_view": "raw", "hold_ms": 5}),
    ):
        body = _reply_body(await mcp.call_tool(name, dict(args)))
        _assert_unknown_argument(body)


async def test_known_arguments_are_not_rejected() -> None:
    body = _reply_body(
        await mcp.call_tool(
            "computer_execute", {"session_id": "s", "action": "click", "x": 1, "y": 2}
        )
    )
    assert "unknown_argument" not in body.get("reasons", []), body


# --- DELTA 1 #4b/#5: the REAL protocol path (lowlevel CallToolRequest handler) ----------------


async def _protocol_call(name: str, arguments: dict[str, Any]) -> Any:
    """Run a tool call through the LOWLEVEL server's CallToolRequest handler —
    the same entry the wire protocol uses — and return the CallToolResult."""
    import mcp as mcp_sdk

    request = mcp_sdk.types.CallToolRequest(
        params=mcp_sdk.types.CallToolRequestParams(name=name, arguments=arguments)
    )
    handler = mcp._mcp_server.request_handlers[mcp_sdk.types.CallToolRequest]
    server_result = await handler(request)
    return server_result.root


async def test_unknown_argument_reply_reaches_the_client_through_the_protocol_handler() -> None:
    """DELTA 1 #4b + DELTA-3 M1/m2: the rejection rides the REAL lowlevel
    handler and reaches the client in the SAME content shape as a normal reply
    of that tool (``convert_result``: Any-typed tool → TextContent only, no
    structuredContent — exactly like a normal reply; FastMCP serialization,
    Unicode raw; contract rejection, no ``error`` key). Rejection happens
    BEFORE argument validation (only the bogus key is sent)."""
    import pydantic_core

    result = await _protocol_call("computer_execute", {"hold_ms": 250})
    assert result.isError is False
    assert result.structuredContent is None  # Any-typed tool: unstructured only, like normal
    text = result.content[0].text
    body = json.loads(text)
    assert "unknown_argument" in body.get("reasons", []), body
    assert "hold_ms" in body["message"], body
    assert "error" not in body, body
    # FastMCP serialization form (Unicode raw), not json.dumps \uXXXX
    assert text == pydantic_core.to_json(body, fallback=str, indent=2).decode()


async def test_unknown_argument_reply_is_dict_tool_shaped_for_dict_tools() -> None:
    """DELTA-3 m2: for a dict-typed tool the rejection goes through THAT tool's
    ``convert_result`` — (content, structured) tuple, structuredContent present
    exactly like a normal reply of that tool — proven through the REAL
    protocol handler with a NON-ASCII argument name (raw Unicode, no \\uXXXX)."""
    import pydantic_core

    result = await _protocol_call("computer_wait", {"持続": 1})
    assert result.isError is False
    assert result.structuredContent is not None  # dict-typed tool: structured like normal
    body = json.loads(result.content[0].text)
    assert "unknown_argument" in body.get("reasons", []), body
    assert "持続" in body["message"], body  # raw Unicode preserved
    assert "持続" in result.content[0].text
    assert result.content[0].text == pydantic_core.to_json(
        result.structuredContent, fallback=str, indent=2
    ).decode()
    assert result.structuredContent.get("reasons") == body.get("reasons")


async def test_elapsed_ms_annotation_matches_fastmcp_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELTA 1 #4a: the annotated TextContent is re-serialized EXACTLY the way
    FastMCP serializes a dict tool result (pydantic_core.to_json, indent=2,
    raw Unicode — never json.dumps' \\uXXXX escapes), proven end-to-end with an
    Arabic message through mcp.call_tool."""
    import asyncio

    import mcp as mcp_sdk
    import pydantic_core
    from mcp.server.fastmcp import FastMCP as SdkFastMCP

    body = {"ok": True, "message": "تم الحفظ: حالة ناجحة"}
    text = pydantic_core.to_json(body, fallback=str, indent=2).decode()
    block = mcp_sdk.types.TextContent(type="text", text=text)

    async def fake_slow_call_tool(self: Any, name: str, arguments: dict[str, Any]) -> Any:
        await asyncio.sleep(3.2)
        return [block]

    monkeypatch.setattr(SdkFastMCP, "call_tool", fake_slow_call_tool)
    result = await mcp.call_tool("computer_observe", {"session_id": "sess-x"})
    annotated = next(b for b in result if getattr(b, "type", None) == "text")
    annotated_body = json.loads(annotated.text)
    assert "elapsed_ms=" in annotated_body["message"]
    assert "تم الحفظ" in annotated.text  # raw Unicode preserved (no \uXXXX)
    assert annotated.text == pydantic_core.to_json(annotated_body, fallback=str, indent=2).decode()


async def test_every_registered_tool_rejects_an_unknown_argument() -> None:
    """DELTA 1 #5 + DELTA-3 m13: EVERY registered tool — the EXACT set, from the
    full registration (importing ``server.app`` so the lazy fetch tool is in) —
    rejects an extra argument through the REAL protocol handler; rejection
    happens BEFORE argument validation, and no tool can silently drop an extra
    argument."""
    import computer_use_mcp.server.app as app_module  # noqa: F401 - registers ALL tools

    expected = {
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
    }
    tools = await mcp.list_tools()
    assert {tool.name for tool in tools} == expected, sorted(tool.name for tool in tools)
    for tool in tools:
        result = await _protocol_call(tool.name, {"__bogus_wp13_probe__": 1})
        assert result.isError is False, (tool.name, result)
        body = json.loads(result.content[0].text)
        assert "unknown_argument" in body.get("reasons", []), (tool.name, body)
        assert "__bogus_wp13_probe__" in body["message"], (tool.name, body)
        assert "error" not in body, (tool.name, body)


# --- DELTA 1 #7: start_session STATES the effective client budget ----------------------------


async def test_start_session_message_states_the_client_budget(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELTA 1 #7: start_session's reply carries ``client_timeout_s=<n>`` in its
    ``message`` (the contract's key=value suffix channel; ``message`` is already
    a field on every reply — no new top-level field)."""
    from test_controller_integration import ScriptedProvider

    import computer_use_mcp.server.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "_backend_factory", lambda: ScriptedBackend())
    monkeypatch.setattr(lifecycle, "_provider_factory", lambda: ScriptedProvider([]))
    monkeypatch.delenv("CORTEX_CLIENT_TIMEOUT_SECONDS", raising=False)
    response = lifecycle.start_session(dry_run=True, require_approval=False)
    assert "client_timeout_s=60" in response.get("message", ""), response.get("message")
    await lifecycle.stop_session(str(response["session_id"]))
    monkeypatch.setenv("CORTEX_CLIENT_TIMEOUT_SECONDS", "30")
    response = lifecycle.start_session(dry_run=True, require_approval=False)
    assert "client_timeout_s=30" in response.get("message", ""), response.get("message")
    await lifecycle.stop_session(str(response["session_id"]))


# --- DELTA 2 #2: the warm wall and the no-swap-on-timeout policy ------------------------------


def _construct_backend_with_reader(
    monkeypatch: pytest.MonkeyPatch, warm_state: str
) -> tuple[Any, list[Any], list[int]]:
    """Build a LocalComputerBackend with a STUB UIA reader whose warm() returns
    ``warm_state``; record the stub instances and warm-call count."""
    import computer_use_mcp.desktop.backend as backend_module

    instances: list[Any] = []
    warm_calls: list[int] = []

    class _StubUia:
        value_source = "uia"
        last_degraded_reason: str | None = None

        def __init__(self) -> None:
            instances.append(self)

        def warm(self) -> str:
            warm_calls.append(1)
            return warm_state

    monkeypatch.setattr(backend_module, "UiaSemanticReader", _StubUia)
    backend = backend_module.LocalComputerBackend(uia_read=True)
    return backend, instances, warm_calls


def test_warm_timeout_keeps_the_uia_reader_and_never_double_warms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELTA 2 #2: a warm that TIMES OUT leaves the UIA reader in place (the
    queued warm may still complete on the owner thread) — it must NOT silently
    swap the session to the Win32 reader; and warm runs EXACTLY ONCE (the
    DELTA-1 double warm is gone)."""
    backend, instances, warm_calls = _construct_backend_with_reader(monkeypatch, "timeout")
    assert len(warm_calls) == 1, warm_calls
    assert backend._semantic_reader is instances[0]


def test_warm_failure_swaps_to_the_win32_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """DELTA 2 #2 (the other side): a warm that RAN and genuinely FAILED swaps
    to the Win32 fallback reader — justified: the COM stack refused UIA in this
    process (the PERF-004 box), no later read can succeed."""
    from computer_use_mcp.desktop.text_readers import Win32TextReader

    backend, instances, _warm_calls = _construct_backend_with_reader(monkeypatch, "failed")
    assert backend._semantic_reader is not instances[0]
    assert isinstance(backend._semantic_reader, Win32TextReader)


# --- F: serverInfo reports Cortex's own version ----------------------------------------------


def test_server_info_reports_the_cortex_version() -> None:
    options = mcp._mcp_server.create_initialization_options()
    assert options.server_version == computer_use_mcp.__version__ == "0.7.6"


# --- D: ensure_app names the owning process of a matching visible window title ----------------


async def test_ensure_app_no_instance_hint_names_the_process_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from computer_use_mcp.core.models import WindowInfo

    backend = backend_module.LocalComputerBackend.__new__(
        backend_module.LocalComputerBackend
    )

    class _FakeWindows:
        @staticmethod
        def find_windows_by_title(needle: str, limit: int = 3):
            assert needle.casefold() == "whatsapp"
            assert limit == 3
            return [
                WindowInfo(
                    hwnd=4242,
                    pid=99,
                    process_name="WhatsApp.Root.exe",
                    exe_path="C:/Program Files/WindowsApps/5319275A.WhatsAppDesktop_.../WhatsApp.Root.exe",
                    window_class="Chrome_WidgetWin_1",
                    title="WhatsApp",
                    bounds=[0, 0, 800, 600],
                )
            ]

    monkeypatch.setattr(
        backend_module,
        "find_windows_by_title",
        _FakeWindows.find_windows_by_title,
        raising=False,
    )
    monkeypatch.setattr(
        backend,
        "enumerate_app_windows",
        lambda needle: [],  # no app-window candidate matches (the field case)
    )
    payload = backend.ensure_app("WhatsApp", allow_launch=False)
    assert "ensure_app_hint" in payload, payload
    assert "WhatsApp.Root.exe" in payload, payload
    # DELTA-3 m11b: the taught focus call uses a REAL argument (target, not title)
    assert "focus_window target='WhatsApp'" in payload, payload
    assert "spoofable" in payload or "hint only" in payload, payload
    # the taught arguments pass the unknown-argument gate
    body = _reply_body(
        await mcp.call_tool(
            "computer_execute",
            {"session_id": "s", "action": "focus_window", "target": "WhatsApp"},
        )
    )
    assert "unknown_argument" not in body.get("reasons", []), body


def test_ensure_app_hint_lists_multiple_process_basenames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELTA-3 m11c: the hint lists EVERY matching owning process basename
    (deduplicated, capped at 3) — two windows of different processes named
    'WhatsApp' both appear."""
    from computer_use_mcp.core.models import WindowInfo

    backend = backend_module.LocalComputerBackend.__new__(
        backend_module.LocalComputerBackend
    )

    def _fake_scan(needle: str, limit: int = 3) -> list[WindowInfo]:
        assert needle.casefold() == "whatsapp"
        return [
            WindowInfo(
                hwnd=1,
                pid=10,
                process_name="WhatsApp.Root.exe",
                exe_path="C:/x/WhatsApp.Root.exe",
                window_class="W",
                title="WhatsApp",
                bounds=[0, 0, 1, 1],
            ),
            WindowInfo(
                hwnd=2,
                pid=11,
                process_name="WhatsApp.Legacy.exe",
                exe_path="C:/x/WhatsApp.Legacy.exe",
                window_class="W",
                title="WhatsApp Web",
                bounds=[0, 0, 1, 1],
            ),
        ]

    monkeypatch.setattr(backend_module, "find_windows_by_title", _fake_scan, raising=False)
    monkeypatch.setattr(backend, "enumerate_app_windows", lambda needle: [])
    payload = backend.ensure_app("WhatsApp", allow_launch=False)
    assert "WhatsApp.Root.exe" in payload and "WhatsApp.Legacy.exe" in payload, payload
    assert "retry with target='WhatsApp.Root.exe'" in payload, payload


def test_ensure_app_without_a_matching_title_has_no_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = backend_module.LocalComputerBackend.__new__(
        backend_module.LocalComputerBackend
    )
    monkeypatch.setattr(
        backend_module,
        "find_windows_by_title",
        lambda needle, limit=3: [],
        raising=False,
    )
    monkeypatch.setattr(backend, "enumerate_app_windows", lambda needle: [])
    payload = backend.ensure_app("WhatsApp", allow_launch=False)
    assert "ensure_app_hint" not in payload, payload


# --- DELTA 1 #6: the hint's SUGGESTED target actually matches the app matcher -----------------


def _fake_windows_module(monkeypatch: pytest.MonkeyPatch, windows: list[dict[str, Any]]) -> Any:
    """A fake ``windows._user32`` enumerating a FIXED window population, so the
    REAL matcher in windows.enumerate_app_windows runs offline."""
    import ctypes

    import computer_use_mcp.desktop.windows as windows_module

    class _FakeUser32:
        def IsWindow(self, handle: Any) -> int:
            return 1

        def IsWindowVisible(self, handle: Any) -> int:
            return 1

        def GetAncestor(self, handle: Any, flag: int) -> int:
            return int(handle.value) if hasattr(handle, "value") else int(handle)

        def EnumWindows(self, callback: Any, lparam: int) -> int:
            for hwnd in windows:
                callback(ctypes.c_void_p(hwnd["hwnd"]), 0)
            return 1

    monkeypatch.setattr(windows_module, "_user32", _FakeUser32())
    monkeypatch.setattr(windows_module, "_window_pid", lambda hwnd: windows[0]["pid"])
    monkeypatch.setattr(
        windows_module,
        "_process_image_path",
        lambda pid: next(w["exe_path"] for w in windows if w["pid"] == pid),
    )
    monkeypatch.setattr(
        windows_module,
        "_window_text",
        lambda hwnd: next(w["title"] for w in windows if w["hwnd"] == int(hwnd)),
    )
    monkeypatch.setattr(windows_module, "_window_class_name", lambda hwnd: "Chrome_WidgetWin_1")
    monkeypatch.setattr(windows_module, "_window_rect", lambda hwnd: [0, 0, 800, 600])
    return windows_module


def test_the_hint_suggested_target_matches_ensure_apps_own_matcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELTA 1 #6 (field e3): the hint suggests target='WhatsApp.Root' — that
    suggestion must actually MATCH in ensure_app's own process matcher
    (windows.enumerate_app_windows: case-insensitive basename, .exe-tolerant),
    while the original 'WhatsApp' needle misses."""
    windows_module = _fake_windows_module(
        monkeypatch,
        [
            {
                "hwnd": 4242,
                "pid": 99,
                "exe_path": "C:/Program Files/WindowsApps/5319275A.WhatsAppDesktop_"
                "x64__xxxx/WhatsApp.Root.exe",
                "title": "WhatsApp",
            }
        ],
    )
    assert windows_module.enumerate_app_windows("WhatsApp") == []  # the field miss
    # .exe-tolerant: the full basename matches the same window (documented matcher)
    assert len(windows_module.enumerate_app_windows("whatsapp.root.exe")) == 1
    candidates = windows_module.enumerate_app_windows("WhatsApp.Root")  # the hint's suggestion
    assert len(candidates) == 1, candidates
    window = candidates[0].window
    assert window.process_name == "WhatsApp.Root.exe"
    assert window.pid == 99
    assert window.title == "WhatsApp"


# --- WP13 DELTA 3 (VAL-WP13-01 fixes) ---------------------------------------------------------


def test_no_wp13_test_constructs_an_unpatched_real_backend() -> None:
    """m14 guard (AST): this module must never call ``LocalComputerBackend(...)``
    — directly OR through a module attribute (``backend_module.LocalComputer
    Backend(...)``, N4 VAL-WP13-02) — every real-backend construction goes
    through the ``real_backend`` fixture or the ``_SlowDispatchBackend``
    subclass, both covered by the autouse offline-seams sentinel."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))

    def _enclosing_function(node: ast.Call) -> str | None:
        for wrapper in ast.walk(tree):
            if isinstance(wrapper, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                child is node for child in ast.walk(wrapper)
            ):
                return wrapper.name
        return None

    # constructions ALLOWED with the guard: both run only while the offline
    # seams are armed (autouse fixture / in-test seam re-patch) — the runtime
    # sentinel is the enforcement, this guard attributes the call sites.
    allowed_functions = {"_construct_backend_with_reader", "test_lifecycle_stop_stops_the_owner_thread_funnel"}
    direct_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "LocalComputerBackend")
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "LocalComputerBackend"
            )
        )
        and _enclosing_function(node) not in allowed_functions
    ]
    assert direct_calls == [], (
        "construct LocalComputerBackend only via the guarded seams "
        f"(unattributed call sites: {[_enclosing_function(n) for n in direct_calls]})"
    )


def test_warm_really_runs_the_throwaway_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """M2 (VAL-WP13-01): the warm's throwaway focused read REALLY runs — the
    fake COM seam counts exactly ONE GetFocusedElement call during warm (the
    DELTA-2 re-entrancy bug swallowed it behind the busy gate)."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    import ctypes
    from test_uia_reader import _FakeUiaWorld, _install_fake_com

    class _CountingWorld(_FakeUiaWorld):
        """Counts the GetFocusedElement (vtable slot 7) calls."""

        def __init__(self) -> None:
            super().__init__()
            self.get_focused_calls = 0
            keep = self.factory
            self.focused_ptr = self._make_element(
                {
                    30005: ("bstr", "Untitled - Notepad"),
                    30003: ("i4", 50004),
                    30011: ("bstr", "15"),
                    30045: ("bstr", "hello world"),
                    30022: ("bool", False),
                    30001: ("rect", (100.0, 50.0, 400.0, 30.0)),
                }
            )

            def counting_get_focused(_this: Any, out: Any) -> int:
                self.get_focused_calls += 1
                out[0] = self.focused_ptr
                return 0

            def counting_condition(_this: Any, out: Any) -> int:
                out[0] = self.array_ptr
                return 0

            self.automation_ptr = keep.make_object(
                {
                    7: ((ctypes.POINTER(ctypes.c_void_p),), counting_get_focused),
                    22: ((ctypes.POINTER(ctypes.c_void_p),), counting_condition),
                }
            )

    world = _CountingWorld()
    _install_fake_com(monkeypatch, world.automation_ptr)
    from computer_use_mcp.desktop.text_readers import UiaSemanticReader

    reader = UiaSemanticReader()
    assert reader.warm() == "ok"
    assert world.get_focused_calls == 1  # the throwaway focused read RAN
    assert reader.available is True


def test_reentrant_run_bounded_on_the_owner_thread_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M2 guard: ``run_bounded`` called FROM the owner thread raises RuntimeError
    (fail loudly); the worker converts the raising request body to a degraded
    None and stays alive, and production readers call the ``_*_unbounded``
    bodies directly (never re-enter)."""
    import computer_use_mcp.desktop.text_readers as text_readers_module

    reader = text_readers_module.UiaSemanticReader()
    monkeypatch.setattr(text_readers_module, "semantic_read_timeout_seconds", lambda: 0.5)
    errors: list[BaseException] = []

    def _reenter() -> None:
        try:
            reader._worker.run_bounded(lambda: 1, 0.5)
        except RuntimeError as error:
            errors.append(error)
        return None

    monkeypatch.setattr(reader, "_read_unbounded", _reenter)
    assert reader.read() is None  # the re-entrant read degraded, the worker survived
    assert len(errors) == 1 and "owner thread" in str(errors[0])
    # the worker still serves requests afterwards
    monkeypatch.setattr(reader, "_read_unbounded", lambda: {"ok": True})
    assert reader.read() == {"ok": True}


def test_stop_cycles_leave_no_extra_owner_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    """m3 + N5 (VAL-WP13-02): N start/stop cycles leave NO extra
    cortex-uia-com-owner threads, the fake CUIAutomation object is released ON
    the owner thread, and the fake CoUninitialize runs EXACTLY ONCE per cycle.

    Exact Release arithmetic per cycle on the MINIMAL world below (focused
    element + empty children array): the warm's throwaway read releases the
    focused element; the stop sentinel releases the automation object; the
    fake CoUninitialize runs once — each asserted EXACTLY via dedicated
    counters (the generic factory count also covers the condition/array
    releases; the full _FakeUiaWorld world releases more, which is why this
    test builds its own minimal world)."""
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).parent))
    import ctypes
    from test_uia_reader import _bstr, _FakeComFactory, _rect_safearray
    from test_uia_reader import _VARIANT, _VT_BSTR, _VT_I4, _VT_BOOL, _VT_ARRAY_R8

    class _MinimalWorld:
        """Automation + focused element (hello world) + true condition + an
        EMPTY children array — the smallest warmable fake-COM world."""

        def __init__(self) -> None:
            self.factory = _FakeComFactory()
            self.couninitialize_calls: list[int] = []
            self.automation_releases: list[int] = []
            self.focused_releases: list[int] = []
            self.array_ptr = self._make_array([])
            focused_ptr = self._make_element(
                {
                    30005: ("bstr", "hello"),
                    30003: ("i4", 50004),
                    30011: ("bstr", "15"),
                    30045: ("bstr", "x"),
                    30022: ("bool", False),
                    30001: ("rect", (1.0, 1.0, 2.0, 2.0)),
                },
                release_log=self.focused_releases,
            )
            condition_ptr = self.factory.make_object({})

            def get_focused(_this: Any, out: Any) -> int:
                out[0] = focused_ptr
                return 0

            def condition(_this: Any, out: Any) -> int:
                out[0] = condition_ptr
                return 0

            self.automation_ptr = self.factory.make_object(
                {
                    2: ((), lambda _this: self.automation_releases.append(1) or 1),
                    7: ((ctypes.POINTER(ctypes.c_void_p),), get_focused),
                    22: ((ctypes.POINTER(ctypes.c_void_p),), condition),
                }
            )

        def _make_element(
            self, props: dict[int, tuple[str, Any]], release_log: list[int] | None = None
        ) -> int:
            def get_property_value(_this: Any, prop_id: Any, out: Any) -> int:
                variant = out[0]
                kind, value = props.get(int(prop_id), ("empty", None))
                if kind == "bstr":
                    variant.vt = _VT_BSTR
                    variant.union.bstrVal = ctypes.c_void_p(_bstr(str(value)))
                elif kind == "i4":
                    variant.vt = _VT_I4
                    variant.union.lVal = int(value)  # type: ignore[arg-type]
                elif kind == "bool":
                    variant.vt = _VT_BOOL
                    variant.union.boolVal = bool(value)  # type: ignore[assignment]
                elif kind == "rect":
                    variant.vt = _VT_ARRAY_R8
                    variant.union.parray = ctypes.c_void_p(_rect_safearray(value))  # type: ignore[arg-type]
                else:
                    variant.vt = 0
                return 0

            def find_all(_this: Any, _scope: Any, _condition: Any, out: Any) -> int:
                out[0] = self.array_ptr
                return 0

            methods: dict[int, tuple[tuple[type, ...], Any]] = {
                6: (
                    (ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
                    find_all,
                ),
                10: ((ctypes.c_long, ctypes.POINTER(_VARIANT)), get_property_value),
            }
            if release_log is not None:
                methods[2] = ((), lambda _this: release_log.append(1) or 1)
            return self.factory.make_object(methods)

        def _make_array(self, element_ptrs: list[int]) -> int:
            def get_length(_this: Any, out: Any) -> int:
                out[0] = len(element_ptrs)
                return 0

            def get_element(_this: Any, index: Any, out: Any) -> int:
                out[0] = element_ptrs[int(index)]
                return 0

            return self.factory.make_object(
                {
                    3: ((ctypes.POINTER(ctypes.c_long),), get_length),
                    4: ((ctypes.POINTER(ctypes.c_void_p),), get_element),
                }
            )

    world = _MinimalWorld()

    def fake_co_create(_clsid: Any, _outer: Any, _ctx: Any, _iid: Any, out_ref: Any) -> int:
        out_ref._obj.value = world.automation_ptr
        return 0

    import computer_use_mcp.desktop.text_readers as text_readers_module

    monkeypatch.setattr(
        text_readers_module,
        "_ole32",
        SimpleNamespace(
            CoInitializeEx=lambda _pv, _mode: 0,
            CoCreateInstance=fake_co_create,
            CoUninitialize=lambda: world.couninitialize_calls.append(1),
        ),
    )
    owner_name = "cortex-uia-com-owner"
    before = [t for t in threading.enumerate() if t.name == owner_name]
    threads: list[Any] = []
    for _cycle in range(3):
        reader = text_readers_module.UiaSemanticReader()
        assert reader.warm() == "ok"
        threads.append(reader._worker._thread)
        reader.stop_owner_thread()
    for thread in threads:
        thread.join(3.0)
        assert not thread.is_alive(), "the owner thread did not exit on stop"
    after = [t for t in threading.enumerate() if t.name == owner_name]
    assert len(after) == len(before)
    # EXACT arithmetic (N5): the fake CoUninitialize ran EXACTLY once per owner
    # thread; the AUTOMATION pointer was released EXACTLY once per cycle (the
    # stop sentinel, on the owner thread); the FOCUSED element was released
    # EXACTLY once per cycle (the warm's throwaway read).
    assert len(world.couninitialize_calls) == 3, world.couninitialize_calls
    assert len(world.automation_releases) == 3, world.automation_releases
    assert len(world.focused_releases) == 3, world.focused_releases


def test_reads_before_the_warm_resolves_are_read_timeout(
    real_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m4: reads that arrive while the warm is still pending (warm_state None or
    'timeout') are READ TIMEOUT — the backend records the degradation, they are
    never plain blind reads."""
    backend = backend_module.LocalComputerBackend.__new__(backend_module.LocalComputerBackend)
    backend._semantic_reader = None

    class _PendingReader:
        value_source = "uia"
        available = False
        warm_state = "timeout"
        last_degraded_reason = None

    backend._semantic_reader = _PendingReader()
    assert backend.read_focused_control_value() is None
    assert backend._last_read_degraded == "read_timeout", backend._last_read_degraded


def _install_slow_fake_com(
    monkeypatch: pytest.MonkeyPatch, automation_ptr: int, delay: float, succeed: bool
) -> None:
    """Fake COM whose CoCreateInstance sleeps ``delay`` (ON the owner thread)
    then succeeds with ``automation_ptr`` or fails with E_NOINTERFACE."""
    import time as _time
    from types import SimpleNamespace

    import computer_use_mcp.desktop.text_readers as text_readers_module

    def slow_create(_clsid: Any, _outer: Any, _ctx: Any, _iid: Any, out_ref: Any) -> int:
        _time.sleep(delay)
        if succeed:
            out_ref._obj.value = automation_ptr
            return 0
        return -2147467262  # E_NOINTERFACE (observed on hardened boxes)

    monkeypatch.setattr(
        text_readers_module,
        "_ole32",
        SimpleNamespace(
            CoInitializeEx=lambda _pv, _mode: 0,
            CoCreateInstance=slow_create,
        ),
    )


def test_warm_slow_then_failed_resolves_failed_and_swaps_lazily_end_to_end(
    real_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1 (VAL-WP13-02), END TO END with NO hand-set state: a fake
    CoCreateInstance that is SLOWER than the warm wall and then FAILS —
    (a) warm() returns "timeout"; (b) a read during the pending warm reports
    read_timeout with the target marker KEPT (N2); (c) after the owner
    finishes and records "failed" ON the owner thread, the next read swaps
    lazily to the Win32TextReader (its read stubbed — no desktop contact)."""
    import sys
    import time as _time
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from test_uia_reader import _FakeUiaWorld

    import computer_use_mcp.desktop.text_readers as text_readers_module
    from computer_use_mcp.desktop.text_readers import UiaSemanticReader, Win32TextReader

    world = _FakeUiaWorld()  # ptr unused: CoCreateInstance FAILS after the delay
    _install_slow_fake_com(monkeypatch, world.automation_ptr, delay=0.4, succeed=False)
    monkeypatch.setattr(text_readers_module, "semantic_warm_timeout_seconds", lambda: 0.15)
    monkeypatch.setattr(text_readers_module, "semantic_read_timeout_seconds", lambda: 0.15)
    reader = UiaSemanticReader()
    # (a) the caller observes the wall expiry
    assert reader.warm() == "timeout"
    monkeypatch.setattr(real_backend, "_semantic_reader", reader)  # N11: restored after the test
    # (b) a read during the pending warm: read_timeout, target marker KEPT
    assert real_backend.read_focused_control_value() is None
    assert real_backend._last_read_degraded == "read_timeout"
    assert real_backend._last_read_had_target is True
    # the owner finishes the slow warm and records "failed" ITSELF
    deadline = _time.perf_counter() + 3.0
    while reader.warm_state != "failed" and _time.perf_counter() < deadline:
        _time.sleep(0.02)
    assert reader.warm_state == "failed"
    # (c) the lazy swap fires on the next read
    monkeypatch.setattr(Win32TextReader, "read", lambda self: None)
    assert real_backend.read_focused_control_value() is None
    assert isinstance(real_backend._semantic_reader, Win32TextReader)
    assert real_backend._semantic_reader is not reader


def test_warm_slow_then_succeeded_keeps_the_uia_reader_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N1 second case: a slow warm that then SUCCEEDS — warm() returns
    "timeout", the owner later records "ok", and the UIA reader stays in place
    and serves real (fake-COM) reads."""
    import sys
    import time as _time
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from test_uia_reader import _FakeUiaWorld

    import computer_use_mcp.desktop.text_readers as text_readers_module
    from computer_use_mcp.desktop.text_readers import UiaSemanticReader

    world = _FakeUiaWorld()
    _install_slow_fake_com(monkeypatch, world.automation_ptr, delay=0.4, succeed=True)
    monkeypatch.setattr(text_readers_module, "semantic_warm_timeout_seconds", lambda: 0.15)
    monkeypatch.setattr(text_readers_module, "semantic_read_timeout_seconds", lambda: 0.15)
    reader = UiaSemanticReader()
    assert reader.warm() == "timeout"
    deadline = _time.perf_counter() + 3.0
    while reader.warm_state == "timeout" and _time.perf_counter() < deadline:
        _time.sleep(0.02)
    assert reader.warm_state == "ok"
    snapshot = reader.read()
    assert isinstance(snapshot, dict) and snapshot is not None
    focused = snapshot["focused"]
    assert focused["value"] == "hello world"


def test_degraded_baseline_never_verifies_via_tail_match(
    real_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m5: a BASELINE read that timed out proves nothing for the whole action —
    a later healthy read that ends with the typed text is unverified with the
    visible branch=read_timeout (never verified, never repaired)."""
    from test_wp12b_baseline_ladder import _dispatches, _type_action, _typing_backend

    raws: list[Any] = [{"baseline_degraded": True}, "test", "test", "test", "test"]

    def _scripted_raw() -> str | None:
        item = raws.pop(0) if len(raws) > 1 else raws[-1]
        if isinstance(item, dict):
            real_backend._last_read_degraded = "read_timeout"
            return None
        real_backend._last_read_degraded = None
        return str(item)

    engine, _reader = _typing_backend(
        real_backend, monkeypatch, reader_values=[], control_type=None
    )
    monkeypatch.setattr(real_backend, "read_focused_control_value", _scripted_raw)
    message = real_backend.execute(_type_action("test"))
    assert "integrity=unverified" in message and "branch=read_timeout" in message, message
    assert _dispatches(engine) == ["test"]  # never repaired


def test_read_timeout_hides_no_target_marker_and_never_repairs(
    real_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m6: on read_timeout the TYPE_UNCONFIRMED no-readable-target marker and
    the clipboard hint must NOT fire (the target exists; the provider was
    slow) — driven through the REAL read_focused_control_value with a stub
    reader whose read times out."""
    from test_wp12b_baseline_ladder import _dispatches, _type_action, _typing_backend

    class _SlowReader:
        value_source = "uia"
        available = True
        warm_state = "ok"
        last_degraded_reason: str | None = None

        def read(self) -> None:
            self.last_degraded_reason = "read_timeout"
            return None

    engine, _reader = _typing_backend(
        real_backend, monkeypatch, reader_values=[], control_type=None
    )
    monkeypatch.setattr(real_backend, "_semantic_reader", _SlowReader())  # N11: restored
    message = real_backend.execute(_type_action("test"))
    assert "branch=read_timeout" in message, message
    assert "TYPE_UNCONFIRMED" not in message, message
    assert "clipboard" not in message, message
    assert _dispatches(engine) == ["test"]  # never repaired


def test_verify_budget_gate_fires_before_the_first_read_slow_provider(
    real_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m7 (part 1): with a SLOW (not hung) provider the whole type action fits
    deadline + one read wall. The derived deadline is SHRUNK to 0.3 s for a
    fast offline run (a 30 s host derives 15 s — same arithmetic, scaled); the
    slow read exceeds one wall, so the gate BEFORE the first verification read
    fires. Fake-clock-free: real sleeps, all under a second."""
    import time as _time

    from test_wp12b_baseline_ladder import _type_action, _typing_backend

    monkeypatch.setattr(backend_module, "type_action_deadline_seconds", lambda: 0.3)
    monkeypatch.setattr(backend_module, "semantic_read_timeout_seconds", lambda: 0.2)

    class _SlowButHonestReader:
        value_source = "uia"
        available = True
        warm_state = "ok"
        last_degraded_reason: str | None = None

        def read(self) -> dict[str, object]:
            # the REAL read shape: a snapshot dict with a focused value
            _time.sleep(0.25)  # slow: over the 0.2 s wall
            self.last_degraded_reason = None
            return {"focused": {"value": "x", "control_type": "Edit"}, "elements": []}

    engine, _reader = _typing_backend(
        real_backend, monkeypatch, reader_values=[], control_type=None
    )
    monkeypatch.setattr(real_backend, "_semantic_reader", _SlowButHonestReader())  # N11: restored
    began = _time.perf_counter()
    message = real_backend.execute(_type_action("test"))
    took = _time.perf_counter() - began
    # deadline 0.3 + one read wall 0.2 (+ the 0.05 settle + scheduling slack)
    assert took <= 0.3 + 0.2 + 0.3, f"the type action took {took:.3f}s"
    assert "branch=verify_over_budget" in message, message
    assert "integrity=verified" not in message, message
    del engine


def test_verify_budget_gate_covers_the_quick_and_horizon_reads(
    real_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m7 + N5 (VAL-WP13-02, part 2): the budget gate fires on the QUICK reads
    and on the HORIZON read — not only before the first read. The stub reader
    returns the REAL read shape (a snapshot dict) with controlled per-read
    delays; the derived deadline is shrunk to 0.5 s (30 s host ⇒ 15 s, scaled)
    and the read wall to 0.1 s. Both scenarios end unverified, never repaired."""
    from test_wp12b_baseline_ladder import _dispatches, _type_action, _typing_backend

    monkeypatch.setattr(backend_module, "type_action_deadline_seconds", lambda: 0.5)
    monkeypatch.setattr(backend_module, "semantic_read_timeout_seconds", lambda: 0.1)

    def _snapshot(value: str) -> dict[str, object]:
        return {"focused": {"value": value, "control_type": "Edit"}, "elements": []}

    class _ScriptedReader:
        value_source = "uia"
        available = True
        warm_state = "ok"
        last_degraded_reason: str | None = None

        def __init__(self, values: list[str], delays: list[float]) -> None:
            self.values = list(values)
            self.delays = list(delays)

        def read(self) -> dict[str, object]:
            import time as _time

            delay = self.delays.pop(0) if len(self.delays) > 1 else self.delays[-1]
            _time.sleep(delay)
            value = self.values.pop(0) if len(self.values) > 1 else self.values[-1]
            return _snapshot(value)

    # Scenario A — the QUICK (confirm) gate: the first read already ends with
    # the typed text and consumed most of the budget (0.45 s of the 0.5 s
    # deadline), so the single confirming quick read is skipped for budget and
    # reports verify_over_budget (N7 token, not unstable_confirm). The 0.45 s
    # margin keeps the gate deterministic even with Windows timer jitter.
    engine_a, _ = _typing_backend(
        real_backend, monkeypatch, reader_values=[], control_type=None
    )
    monkeypatch.setattr(  # N11: restored after the test
        real_backend,
        "_semantic_reader",
        _ScriptedReader(values=["", "test", "test"], delays=[0.0, 0.45, 0.45]),
    )
    message_a = real_backend.execute(_type_action("test"))
    assert "branch=verify_over_budget" in message_a, message_a
    assert "unstable_confirm" not in message_a, message_a
    assert "integrity=verified" not in message_a, message_a
    assert _dispatches(engine_a) == ["test"]

    # Scenario B — the post-HORIZON confirm gate: two equal non-matching reads
    # stall the fast path into escalation; the horizon read itself is
    # affordable, but the CONFIRM read after it is skipped for budget —
    # verify_over_budget (not repair_unconfirmed / horizon_unconfirmed), never
    # a repair, never verified.
    engine_b, _ = _typing_backend(
        real_backend, monkeypatch, reader_values=[], control_type=None
    )
    monkeypatch.setattr(  # N11: restored after the test
        real_backend,
        "_semantic_reader",
        _ScriptedReader(values=["pre", "pre", "pre"], delays=[0.0, 0.0, 0.0, 0.6]),
    )
    message_b = real_backend.execute(_type_action("test"))
    assert "branch=verify_over_budget" in message_b, message_b
    assert "integrity=verified" not in message_b, message_b
    assert "repair" not in message_b, message_b
    assert _dispatches(engine_b) == ["test"]


async def test_queued_type_item_that_cannot_run_paced_is_not_dispatched(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m8: a queued type item whose remaining budget cannot cover the paced
    dispatch at the proven-safe interval is NOT dispatched (time_budget) —
    never squeezed into an unpaced burst."""
    from types import SimpleNamespace as _NS

    clock = _patch_agent_clock(monkeypatch, 15.0)
    backend = ScriptedBackend(flip=False)
    backend._engine = _NS(type_interval=0.02)  # the paced real-engine shape
    session_id, _bundle, backend, _ = make_session(
        monkeypatch,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
        backend=backend,
    )

    def _consume_13_9(action: Any) -> None:
        clock.advance(13.9)

    backend.execute_hooks.append(_consume_13_9)
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            follow_ups=[{"action": "type", "text": "x" * 100}],  # 2.0 s paced + reserve
        )
    )
    assert payload["follow_ups_stopped_reason"] == "time_budget", payload
    assert len(payload["follow_up_results"]) == 1  # the type item never dispatched
    assert len(backend.executed) == 1


async def test_wait_gate_uses_the_clamped_wait_duration(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """n4: the wait gate compares the CLAMPED duration (backend cap 10 s) — a
    delta 12 wait with 10.5 s remaining DISPATCHES (raw-delta gating would
    have stopped it)."""
    from test_controller_integration import ScriptedBackend

    clock = _patch_agent_clock(monkeypatch, 15.0)
    backend = ScriptedBackend(flip=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
        backend=backend,
    )
    backend.execute_hooks.append(lambda action: clock.advance(4.5))
    payload = execute_payload(
        await srv_server_execute.computer_execute(
            session_id,
            "done",
            follow_ups=[{"action": "wait", "delta": 12}],  # clamped to 10 <= 10.5
        )
    )
    assert payload.get("follow_ups_stopped_reason") in (None, "completed"), payload
    assert len(payload["follow_up_results"]) == 2  # the wait DISPATCHED


async def test_elapsed_ms_annotates_dict_typed_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """m1 (VAL-WP13-01): dict-annotated tools (computer_wait & co.) come back as
    FastMCP (content, structured) tuples — the annotation lands on the
    STRUCTURED dict and the TextContent is re-serialized from it the FastMCP
    way, so the two never diverge."""
    import asyncio

    import mcp as mcp_sdk
    import pydantic_core
    from mcp.server.fastmcp import FastMCP as SdkFastMCP

    structured = {"ok": True, "message": "Completed wait.", "status": "completed"}
    text = pydantic_core.to_json(structured, fallback=str, indent=2).decode()
    block = mcp_sdk.types.TextContent(type="text", text=text)

    async def fake_slow_call_tool(self: Any, name: str, arguments: dict[str, Any]) -> Any:
        await asyncio.sleep(3.2)
        return ([block], structured)

    monkeypatch.setattr(SdkFastMCP, "call_tool", fake_slow_call_tool)
    result = await mcp.call_tool(
        "computer_wait", {"session_id": "sess-x", "duration_seconds": 1.0}
    )
    assert isinstance(result, tuple) and len(result) == 2
    content, annotated_structured = result
    assert "elapsed_ms=" in annotated_structured["message"]
    assert content[0].text == pydantic_core.to_json(
        annotated_structured, fallback=str, indent=2
    ).decode()
    assert "elapsed_ms=" in json.loads(content[0].text)["message"]


async def test_hang_to_typing_through_the_real_reader_and_backend(
    real_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m13: the REAL reader→backend chain under a hung provider — fake COM seam,
    real UiaSemanticReader, real backend ladder: the type dispatch lands, the
    verification reads time out, the reply is the visible read_timeout, the
    marker is suppressed and there is no repair."""
    import sys
    import time as _time
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    import ctypes
    from test_uia_reader import _FakeUiaWorld, _install_fake_com
    from test_wp12b_baseline_ladder import _dispatches, _type_action, _typing_backend

    release = threading.Event()
    world = _FakeUiaWorld()
    keep = world.factory

    def hung_get_focused(_this: Any, out: Any) -> int:
        release.wait(5.0)  # the provider hangs BEFORE answering
        out[0] = world._make_element(
            {
                30005: ("bstr", "Untitled - Notepad"),
                30003: ("i4", 50004),
                30011: ("bstr", "15"),
                30045: ("bstr", "hello world"),
                30022: ("bool", False),
                30001: ("rect", (100.0, 50.0, 400.0, 30.0)),
            }
        )
        return 0

    def condition(_this: Any, out: Any) -> int:
        out[0] = world.array_ptr
        return 0

    world.automation_ptr = keep.make_object(
        {
            7: ((ctypes.POINTER(ctypes.c_void_p),), hung_get_focused),
            22: ((ctypes.POINTER(ctypes.c_void_p),), condition),
        }
    )
    _install_fake_com(monkeypatch, world.automation_ptr)
    import computer_use_mcp.desktop.text_readers as text_readers_module
    from computer_use_mcp.desktop.text_readers import UiaSemanticReader

    monkeypatch.setattr(text_readers_module, "semantic_read_timeout_seconds", lambda: 0.2)
    monkeypatch.setattr(text_readers_module, "semantic_warm_timeout_seconds", lambda: 0.2)
    real_reader = UiaSemanticReader()
    engine, _stub = _typing_backend(
        real_backend, monkeypatch, reader_values=[], control_type=None
    )
    # the backend warm runs at construction; this session's reader warms HERE —
    # under the hung provider it times out but the automation object EXISTS
    # (CoCreateInstance completed before the hung read), so reads hit the busy gate
    assert real_reader.warm() == "timeout"
    monkeypatch.setattr(  # N11: restored after the test
        real_backend, "_semantic_reader", real_reader
    )
    began = _time.perf_counter()
    message = real_backend.execute(_type_action("test"))
    took = _time.perf_counter() - began
    release.set()
    assert "branch=read_timeout" in message, message
    assert "integrity=unverified" in message, message
    assert "TYPE_UNCONFIRMED" not in message, message  # m6 through the REAL chain
    assert _dispatches(engine) == ["test"]  # never repaired
    assert took < 5.0, f"the hung-provider type action took {took:.3f}s"


# --- WP13 DELTA 4 (VAL-WP13-02 fixes) ----------------------------------------------------------


async def test_lifecycle_stop_stops_the_owner_thread_funnel(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N3 (VAL-WP13-02): the session-end funnel REALLY stops the backend's UIA
    COM owner thread — start_session then stop_session through the REAL
    lifecycle with a fake-COM reader; the owner thread has EXITED (join with a
    short timeout) and on_stop ran (the cached automation pointer is None)."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from test_uia_reader import _FakeUiaWorld, _install_fake_com

    import computer_use_mcp.desktop.backend as backend_module
    import computer_use_mcp.server.lifecycle as lifecycle
    from computer_use_mcp.desktop.text_readers import UiaSemanticReader
    from test_controller_integration import ScriptedProvider

    world = _FakeUiaWorld()
    _install_fake_com(monkeypatch, world.automation_ptr)

    recorded: list[Any] = []

    class _RecordingReader(UiaSemanticReader):
        def __init__(self) -> None:
            super().__init__()
            recorded.append(self)

    monkeypatch.setattr(backend_module, "UiaSemanticReader", _RecordingReader)
    monkeypatch.setattr(
        lifecycle,
        "_backend_factory",
        lambda: backend_module.LocalComputerBackend(uia_read=True),
    )
    monkeypatch.setattr(lifecycle, "_provider_factory", lambda: ScriptedProvider([]))
    response = lifecycle.start_session(dry_run=True, require_approval=False)
    session_id = str(response["session_id"])
    assert recorded, "the lifecycle did not build a UIA reader"
    reader = recorded[0]
    thread = reader._worker._thread
    assert thread.is_alive()
    await lifecycle.stop_session(session_id)
    thread.join(2.0)
    assert not thread.is_alive(), "the owner thread did not exit on stop_session"
    assert reader._automation is None  # on_stop ran: released ON the owner thread
    assert reader.available is False


def test_pending_warm_read_timeout_keeps_the_target_marker_off(
    real_backend: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N2 (VAL-WP13-02): on the pending-warm read_timeout path the
    TYPE_UNCONFIRMED no-readable-target marker and the clipboard hint do NOT
    fire — through the REAL read_focused_control_value with a reader whose
    warm is still pending."""
    from test_wp12b_baseline_ladder import _type_action, _typing_backend

    class _PendingReader:
        value_source = "uia"
        available = False
        warm_state = "timeout"
        last_degraded_reason = None

        def read(self) -> None:
            return None

    engine, _reader = _typing_backend(
        real_backend, monkeypatch, reader_values=[], control_type=None
    )
    monkeypatch.setattr(  # N11: restored after the test
        real_backend, "_semantic_reader", _PendingReader()
    )
    message = real_backend.execute(_type_action("test"))
    assert "branch=read_timeout" in message, message
    assert "TYPE_UNCONFIRMED" not in message, message
    assert "clipboard" not in message, message
    assert real_backend._last_read_had_target is True


def test_ensure_app_hint_scan_raising_leaves_the_reply_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """n6 (VAL-WP13-02): a HINT-scan exception can never turn the NO_INSTANCE
    reply into an error — the payload stays the plain NO_INSTANCE without a
    hint."""
    backend = backend_module.LocalComputerBackend.__new__(
        backend_module.LocalComputerBackend
    )

    def _exploding_scan(needle: str, limit: int = 3) -> list[Any]:
        raise RuntimeError("injected enumeration fault")

    monkeypatch.setattr(backend_module, "find_windows_by_title", _exploding_scan, raising=False)
    monkeypatch.setattr(backend, "enumerate_app_windows", lambda needle: [])
    payload = backend.ensure_app("WhatsApp", allow_launch=False)
    assert "NO_INSTANCE" in payload, payload
    assert "ensure_app_hint" not in payload, payload


def test_find_windows_by_title_dedupes_process_basenames_before_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N8 (VAL-WP13-02): three windows of ONE process do not hide another
    process — the scan deduplicates by owning-process basename BEFORE cutting
    to the limit."""
    windows_module = _fake_windows_module(
        monkeypatch,
        [
            {"hwnd": 1, "pid": 10, "exe_path": "C:/x/WhatsApp.Root.exe", "title": "WhatsApp"},
            {"hwnd": 2, "pid": 10, "exe_path": "C:/x/WhatsApp.Root.exe", "title": "WhatsApp (2)"},
            {"hwnd": 3, "pid": 10, "exe_path": "C:/x/WhatsApp.Root.exe", "title": "WhatsApp (3)"},
            {"hwnd": 4, "pid": 11, "exe_path": "C:/x/WhatsApp.Legacy.exe", "title": "WhatsApp Web"},
        ],
    )
    # _fake_windows_module returns the REAL windows module with the fakes
    # patched in; extend the per-hwnd pid/exe resolution it cannot know.
    population = [
        {"hwnd": 1, "pid": 10, "exe_path": "C:/x/WhatsApp.Root.exe", "title": "WhatsApp"},
        {"hwnd": 2, "pid": 10, "exe_path": "C:/x/WhatsApp.Root.exe", "title": "WhatsApp (2)"},
        {"hwnd": 3, "pid": 10, "exe_path": "C:/x/WhatsApp.Root.exe", "title": "WhatsApp (3)"},
        {"hwnd": 4, "pid": 11, "exe_path": "C:/x/WhatsApp.Legacy.exe", "title": "WhatsApp Web"},
    ]
    monkeypatch.setattr(windows_module, "_window_pid", lambda hwnd: next(w["pid"] for w in population if w["hwnd"] == int(hwnd)))
    monkeypatch.setattr(
        windows_module,
        "_process_image_path",
        lambda pid: next(w["exe_path"] for w in population if w["pid"] == pid),
    )
    matches = windows_module.find_windows_by_title("whatsapp", limit=3)
    basenames = [w.process_name for w in matches]
    assert basenames == ["WhatsApp.Root.exe", "WhatsApp.Legacy.exe"], basenames


# --- WP13 DELTA 5 (VAL-WP13-03 fixes) ----------------------------------------------------------


async def test_invalid_start_sessions_return_typed_errors_and_free_the_registry(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N10 (VAL-WP13-03 BLOCKER): 5 invalid start_session calls each return the
    TYPED error (never an unstructured UnboundLocalError), leave the registry
    EMPTY (no orphan slots toward max_sessions), never reach the backend
    factory for argument-validation failures, survive a RAISING backend
    factory, and a valid start_session then succeeds."""
    import computer_use_mcp.server.lifecycle as lifecycle
    from test_controller_integration import ScriptedBackend, ScriptedProvider

    factory_calls: list[int] = []

    def _counting_factory() -> Any:
        factory_calls.append(1)
        return ScriptedBackend()

    monkeypatch.setattr(lifecycle, "_backend_factory", _counting_factory)
    monkeypatch.setattr(lifecycle, "_provider_factory", lambda: ScriptedProvider([]))

    invalid_calls: list[dict[str, Any]] = [
        {"max_steps": 0},
        {"max_steps": 1000},
        {"min_confidence": 1.5},
        {"max_steps": 0, "min_confidence": 80},
        {"max_retries_per_action": 9},
    ]
    for kwargs in invalid_calls:
        response = lifecycle.start_session(dry_run=True, require_approval=False, **kwargs)
        # the TYPED _error_response shape (the established error-test pattern)
        assert response.get("ok") is False, (kwargs, response)
        assert response.get("error"), (kwargs, response)  # a typed error code
        assert "message" in response, (kwargs, response)
    assert factory_calls == []  # argument validation fails BEFORE the factory
    assert len(lifecycle._registry) == 0  # no orphan slots (N10's lock-out)

    # a RAISING backend factory: same typed error, registry still empty
    def _exploding_factory() -> Any:
        raise RuntimeError("injected backend factory fault")

    monkeypatch.setattr(lifecycle, "_backend_factory", _exploding_factory)
    response = lifecycle.start_session(dry_run=True, require_approval=False)
    assert response.get("ok") is False and response.get("error"), response
    assert len(lifecycle._registry) == 0

    # a VALID start still succeeds afterwards
    monkeypatch.setattr(lifecycle, "_backend_factory", _counting_factory)
    response = lifecycle.start_session(dry_run=True, require_approval=False)
    # the success payload carries session_id and NO error (no "ok" key on
    # SessionState model dumps — the session itself is the success shape)
    assert response.get("session_id") and "error" not in response, response
    await lifecycle.stop_session(str(response["session_id"]))
