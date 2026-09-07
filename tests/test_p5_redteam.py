"""A8 REGRESSION-REDTEAM adversarial probes (MISSION-CORTEX-PERF-004, final wave).

Every test here is an EXECUTED attack attempt against the perf-004 release candidate
(not reasoning alone). Verdicts live in ``evidence/perf-004/p5/redteam-report.md``;
this file pins the safe behaviors and documents the found weaknesses as regression
guards. No production code was modified by this wave — findings route to the Commander.

Areas: RT1 follow_ups queue smuggling, RT2 FocusGuard adversarial, RT3 rate-limit/burst
semantics, RT4 input-engine fault parity, RT5 checkpoint/payload hygiene, RT6 dry-run
fuzz, RT7 MCP contract fuzz + scorer flags, RT9 type-validator benign-text FPs.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest
from test_checkpoint_manager import build_state as build_checkpoint_state
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    ScriptedProvider,
    audit_events,
    executed_summary,
    make_session,
)
from test_io_parity import METRICS, _ParityFakeUser32, _ParityPyautogui

import benchmarks.score_task as score_task_module
from benchmarks.score_task import append_run_log
from benchmarks.score_task import main as score_main
from computer_use_mcp import server
from computer_use_mcp.backend import (
    FakeComputerBackend,
    InputBlockedError,
    PyAutoGuiInputEngine,
    SendInputEngine,
)
from computer_use_mcp.focus_guard import InterferenceGuard
from computer_use_mcp.interference import parse_interference
from computer_use_mcp.limits import LimitEnforcer, Limits
from computer_use_mcp.models import (
    MAX_FOLLOW_UPS,
    ActionSpec,
    AgentDecision,
    GroundedAction,
    WindowInfo,
)
from computer_use_mcp.redaction import redact_text
from computer_use_mcp.safety import SafetyContext, SafetyPolicy
from computer_use_mcp.state import SessionRegistry

TARGET = WindowInfo(
    hwnd=1, pid=100, process_name="excel.exe", window_class="XLMAIN", title="Book1 - Excel",
)
FOREIGN = WindowInfo(
    hwnd=9, pid=900, process_name="zcode.exe", window_class="CONSOLE", title="user console",
)


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh bounded registry/bundles + per-test audit dir for full session isolation."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    return server


def _click(x: int = 10, y: int = 10) -> GroundedAction:
    return GroundedAction(action="click", point={"x": x, "y": y}, confidence=1.0)


# =====================================================================================
# RT1 — follow_ups queue smuggling
# =====================================================================================


async def test_rt1_smuggled_privilege_fields_in_follow_up_spec_are_ignored(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hostile host stuffs approval/risk/confidence fields into a queue item hoping to
    arm the approval gate or downgrade the risk verdict. The spec model must drop every
    non-surface field, and the item must still be evaluated (and denied) on its merits."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(
        session_id,
        "click",
        x=10,
        y=10,
        approved=True,  # the hostile call even carries the call-level approval flag
        follow_ups=[
            {
                "action": "type",
                "text": "format C: /fs:ntfs /y",
                "approved": True,
                "authorized": True,
                "risk": "low",
                "confidence": 1.0,
                "reason": "benign",
                "follow_ups": [{"action": "click", "x": 1, "y": 1}],  # nested smuggling
            },
            {"action": "click", "x": 30, "y": 30},
        ],
    )
    assert response["ok"] is True  # item 0 (the legacy payload) executed
    assert response["follow_ups_stopped_reason"] == "safety_denied"
    smuggled = response["follow_up_results"][1]
    assert smuggled["kind"] == "safety_denied" and smuggled["ok"] is False
    # the third item was flushed: nothing after the denial dispatched
    assert len(executed_summary(backend)) == 1
    # the nested follow_ups key must NOT spawn a nested queue anywhere
    assert all("follow_up_results" not in entry for entry in response["follow_up_results"])


async def test_rt1_critical_follow_up_never_authorized_by_call_level_approved(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CRITICAL-pattern queue items stay blocked even when the direct tool call passes
    ``approved=True`` (operator authorization is only reachable via the run_goal
    approval callback — a host flag can never authorize a CRITICAL action)."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(
        session_id,
        "click",
        x=10,
        y=10,
        approved=True,
        follow_ups=[
            {"action": "type", "text": "del C:\\Windows\\System32 /s /q", "expected_effect": "x"},
        ],
    )
    assert response["follow_ups_stopped_reason"] == "safety_denied"
    assert response["follow_up_results"][1]["ok"] is False
    assert len(executed_summary(backend)) == 1


async def test_rt1_malformed_spec_matrix_fails_closed_before_any_dispatch(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fuzzed malformed queue-item shapes (wrong types, bad enums, out-of-range values,
    broken chords) must reject the WHOLE call before any dispatch — including when the
    malformed item comes AFTER valid ones."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    hostile_items: list[dict[str, Any]] = [
        {"action": 42},
        {"action": None},
        {"action": "click", "x": "a", "y": 2},
        {"action": "wait", "delta": 999},
        {"action": "hotkey", "keys": "ctrl"},  # string, not list
        {"action": "type", "text": "x" * 5000},  # over the 2000-char text cap
        [[{"action": "click"}]],  # nested list shape
        {"action": "ENSURE_APP", "target": "excel"},  # wrong case: not an enum value
    ]
    for index, item in enumerate(hostile_items):
        response = await server.computer_execute(
            session_id, "click", x=10, y=10, follow_ups=[{"action": "wait", "delta": 1}, item]
        )
        assert response["ok"] is False, f"item {index} accepted: {item!r}"
        assert response["error"] == "invalid_action", f"item {index}: {response}"
        assert backend.executed == [], f"item {index} dispatched: {item!r}"
    # a VALID follow-up after a malformed one must not rescue the call
    response = await server.computer_execute(
        session_id,
        "click",
        x=10,
        y=10,
        follow_ups=[{"action": 42}, {"action": "wait", "delta": 1}],
    )
    assert response["ok"] is False and backend.executed == []


async def test_rt1_finding_spec_valid_grounded_invalid_items_crash_unstructured(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ATTACK DEMO — FINDING RT1-3 (MEDIUM: contract violation, fail-closed in effect).

    A queue item that passes ActionSpec parsing but fails GroundedAction validation
    escapes the server's typed ``invalid_action`` teaching rejection: the pydantic
    ValidationError from ``ActionSpec.to_grounded()`` propagates UNCAUGHT through the
    MCP tool. ActionSpec carries NO cross-field validators, so every shape whose rules
    live only in GroundedAction takes this crash path: focus_window without a target, a
    half-specified drag, a single-key hotkey. Nothing is dispatched (the queue is built
    before the loop — zero dispatch, session intact), so this is fail-closed in EFFECT,
    but the client receives an internal error instead of the designed structured
    rejection. Routed to the Commander; pinned here so the behavior cannot silently
    change.
    """
    from pydantic import ValidationError

    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    for spec in (
        {"action": "focus_window"},
        {"action": "ensure_app"},
        {"action": "drag", "x": 1, "y": 2},
        {"action": "hotkey", "keys": ["a"]},
    ):
        with pytest.raises(ValidationError):
            await server.computer_execute(
                session_id, "click", x=10, y=10, follow_ups=[spec]
            )
    # fail-closed in effect: nothing was dispatched, and the session is still usable
    assert backend.executed == []
    response = await server.computer_execute(session_id, "click", x=10, y=10)
    assert response["ok"] is True


async def test_rt1_queue_cap_enforced_pre_dispatch_with_boundary_exact(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAX_FOLLOW_UPS=5 is enforced BEFORE dispatch: exactly 5 run, 6 reject whole."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    five = [{"action": "wait", "delta": 1} for _ in range(MAX_FOLLOW_UPS)]
    response = await server.computer_execute(session_id, "click", x=10, y=10, follow_ups=five)
    assert response["ok"] is True and response["follow_ups_stopped_reason"] is None
    assert len(executed_summary(backend)) == 6  # primary + 5
    backend.executed.clear()
    six = five + [{"action": "wait", "delta": 1}]
    response = await server.computer_execute(session_id, "click", x=10, y=10, follow_ups=six)
    assert response["ok"] is False and response["error"] == "invalid_action"
    assert backend.executed == []  # whole call rejected, nothing dispatched


async def test_rt1_stop_token_fired_during_item2_execute_halts_queue(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fault-inject the kill path DURING item 2's backend execute: the queue must die
    mid-flight, report stopped, and never dispatch item 3."""

    class StopDuringSecondExecute(ScriptedBackend):
        def execute(self, action: GroundedAction, stop: Any = None, **kwargs: Any) -> str:
            if self.executes >= 1:  # second dispatch: arm the token right before input
                stop.stop()
            return super().execute(action, stop)

    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    backend = StopDuringSecondExecute()
    bundle.backend = backend
    bundle.agent.backend = backend
    bundle.agent.observation.backend = backend
    response = await server.computer_execute(
        session_id,
        "click",
        x=10,
        y=10,
        follow_ups=[
            {"action": "click", "x": 20, "y": 20},
            {"action": "click", "x": 30, "y": 30},
        ],
    )
    assert response.get("stopped") is True, response
    # item 2 was aborted BEFORE its input (the token fired inside its execute hook), and
    # item 3 was never dispatched: only item 1 ever reached the backend.
    assert len(executed_summary(backend)) == 1


async def test_rt1_dry_run_session_queue_stubs_without_dispatch(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry-run session given a queue: no item may dispatch, the stub carries the
    banner, and the queue must stop after the first stub (no post-action capture)."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=True, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(
        session_id,
        "click",
        x=10,
        y=10,
        follow_ups=[{"action": "click", "x": 20, "y": 20}],
    )
    assert backend.executed == []
    assert response["message"].startswith("DRY-RUN (no input dispatched):")
    assert response["follow_ups_stopped_reason"] == "no_post_action_observation"
    for entry in response["follow_up_results"]:
        assert not entry.get("ok") or "DRY-RUN (no input dispatched):" in entry["message"]


async def test_rt1_queue_entries_never_carry_image_payloads(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Payload hygiene: per-item queue results never carry base64 images (bounded), and
    the legacy first-item payload keeps its image only in the legacy slot."""
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(
        session_id,
        "click",
        x=10,
        y=10,
        follow_ups=[{"action": "wait", "delta": 1}],
    )
    assert "screenshot_after_base64" in response  # legacy payload intact
    for entry in response["follow_up_results"][1:]:
        assert "screenshot_after_base64" not in entry
        assert "image" not in json.dumps(entry.get("result", {}), default=str)


def test_rt1_spec_cannot_override_policy_relevant_grounded_fields() -> None:
    """The ActionSpec -> GroundedAction conversion hardcodes the audit reason prefix and
    the client-asserted confidence: a queue item cannot forge policy-relevant fields."""
    spec = ActionSpec.model_validate(
        {"action": "click", "x": 1, "y": 2, "reason": "forged", "confidence": 0.0}
    )
    grounded = spec.to_grounded(reason_prefix="MCP follow_up action")
    assert grounded.reason == "MCP follow_up action"
    assert grounded.confidence == 1.0
    assert not hasattr(spec, "risk") and not hasattr(spec, "authorized")


async def test_rt1_run_single_direct_call_slices_oversized_queue(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense-in-depth: even a caller that bypasses the server cap (direct agent call)
    gets the queue sliced to MAX_FOLLOW_UPS."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    bundle = server._get_bundle(session_id)
    specs = [ActionSpec(action="wait", delta=1) for _ in range(50)]
    outcome = await bundle.agent.run_single(
        bundle.state, _click(), approved=False, follow_ups=specs
    )
    assert outcome.kind == "executed"
    assert len(outcome.follow_up_results) == MAX_FOLLOW_UPS + 1  # primary + 5, not 51
    assert len(executed_summary(backend)) == MAX_FOLLOW_UPS + 1


# =====================================================================================
# RT2 — FocusGuard adversarial
# =====================================================================================


def _guard(backend: FakeComputerBackend, **policy: Any) -> InterferenceGuard:
    return InterferenceGuard(backend, parse_interference(policy or None))


class GuardBackend(FakeComputerBackend):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.execute_calls = 0
        self.set_windows([TARGET, FOREIGN])

    def execute(self, action: GroundedAction, stop: Any = None, **kwargs: Any) -> str:
        self.execute_calls += 1
        return super().execute(action, stop)


def test_rt2_foreign_window_stays_rejected_default_policy() -> None:
    """Foreign foreground (different process/hwnd, alive anchor) must be REJECTED under
    the default abort policy, with zero engine dispatches."""
    backend = GuardBackend()
    backend.set_active_window(TARGET)
    guard = _guard(backend)
    guard.rebind(TARGET)
    backend.set_active_window(FOREIGN)  # the steal
    verdict = guard.verify_pre_dispatch(_click())
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith("FOCUS_TAKEN_BY")
    assert backend.execute_calls == 0


def test_rt2_refocus_policy_reattaches_only_to_the_bound_target() -> None:
    """``refocus_then_abort``: the ONE reattach attempt is aimed at the BOUND title only;
    a verified reattach lets dispatch proceed, a refused refocus rejects fail-closed."""
    # (a) verified reattach to the REAL target: proceed after the foreground matches.
    backend = GuardBackend()
    backend.set_active_window(TARGET)
    guard = _guard(backend, focus_guard={"policy": "refocus_then_abort"})
    guard.rebind(TARGET)
    backend.set_active_window(FOREIGN)  # the steal
    verdict = guard.verify_pre_dispatch(_click())
    assert verdict is None  # reattach verified against the bound identity
    assert backend.query_foreground_window().hwnd == TARGET.hwnd
    # (b) refocus REFUSED (alive bound window, but the focus primitive refuses):
    # reject fail-closed, never touch the foreign window.
    from computer_use_mcp.backend import WindowFocusError

    class RefusingFocusBackend(GuardBackend):
        def focus_window_title(self, title: str) -> str:
            raise WindowFocusError("focus primitive refused")

    refusing = RefusingFocusBackend()
    refusing.set_windows([TARGET, FOREIGN])  # the bound window is ALIVE
    refusing.set_active_window(FOREIGN)
    guard2 = _guard(refusing, focus_guard={"policy": "refocus_then_abort"})
    guard2.rebind(TARGET)
    verdict = guard2.verify_pre_dispatch(_click())
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith("FOCUS_TAKEN_BY")
    assert refusing.execute_calls == 0


def test_rt2_untitled_surfaces_never_become_the_anchor() -> None:
    """B10(c): an allowlisted but UNTITLED foreground (desktop WorkerW shells, splash
    surfaces) must never bind the guard — a wrong anchor would drift-abort everything."""
    backend = GuardBackend()
    untitled = WindowInfo(
        hwnd=3, pid=100, process_name="excel.exe", window_class="XLMAIN", title="",
    )
    backend.set_active_window(untitled)
    guard = _guard(backend)
    guard.maybe_bind(backend.observe(), ["excel.exe"])
    assert guard.armed is False  # dormant, not bound to an anonymous surface


def test_rt2_non_allowlisted_first_observation_never_binds() -> None:
    """The arming doctrine: without an allowlist match the guard stays dormant — it can
    never silently adopt whatever window happens to hold focus (e.g. the user's console)."""
    backend = GuardBackend()
    backend.set_active_window(FOREIGN)
    guard = _guard(backend)
    guard.maybe_bind(backend.observe(), ["excel.exe"])
    assert guard.armed is False
    guard.maybe_bind(backend.observe(), [])  # no allowlist at all
    assert guard.armed is False


def test_rt2_finding_hwnd_recycle_to_foreign_process_accepted_by_hwnd_equality() -> None:
    """ATTACK DEMO — FINDING RT2-1 (WEAKENED, exploitability: rare).

    A recycled hwnd value that now belongs to a FOREIGN process passes
    ``_matches_binding`` by hwnd equality alone (the foreground's pid/class/title are
    not cross-checked on the equal-hwnd path). Requires the OS to reassign the exact
    hwnd value to another process's window while the stale binding is held — rare on a
    live session, but the identity check is provably one-dimensional there. No fix in
    this wave (read-only mandate); recorded as a finding for the Commander.
    """
    backend = GuardBackend()
    backend.set_active_window(TARGET)
    guard = _guard(backend)
    guard.rebind(TARGET)
    recycled = WindowInfo(
        hwnd=1,  # SAME hwnd, totally different process/class/title
        pid=999,
        process_name="evil.exe",
        window_class="OtherClass",
        title="Malicious Window",
    )
    backend.set_windows([TARGET, FOREIGN, recycled])
    backend.set_active_window(recycled)
    verdict = guard.verify_pre_dispatch(_click())
    assert verdict is None  # the guard MATCHES the recycled foreign window (finding)


def test_rt2_hwnd_recycle_same_pid_requires_class_and_title_overlap() -> None:
    """The recycle-recovery rule (same pid + class + overlapping title) is BOUNDED: with
    the same-process owned-dialog rule disabled, a same-pid window with a different
    class or an unrelated title must NOT match."""
    backend = GuardBackend()
    backend.set_active_window(TARGET)
    guard = _guard(backend, focus_guard={"allow_owned_dialogs": False})
    guard.rebind(TARGET)
    same_pid_other_class = WindowInfo(
        hwnd=77, pid=100, process_name="excel.exe", window_class="OTHER", title="Book1 - Excel",
    )
    backend.set_active_window(same_pid_other_class)
    verdict = guard.verify_pre_dispatch(_click())
    assert verdict is not None and verdict.blocking  # class mismatch -> rejected

    same_pid_matching_class_unrelated_title = WindowInfo(
        hwnd=78, pid=100, process_name="excel.exe", window_class="XLMAIN",
        title="Completely Unrelated",
    )
    backend.set_active_window(same_pid_matching_class_unrelated_title)
    verdict = guard.verify_pre_dispatch(_click())
    assert verdict is not None and verdict.blocking  # no title overlap -> rejected


def test_rt2_finding_reanchor_from_dialog_anchor_accepts_unrelated_window() -> None:
    """ATTACK DEMO — FINDING RT2-2 (WEAKENED, bounded).

    When the anchor is a #32770 dialog / transient-launcher surface,
    ``reanchor_after_success`` re-anchors to ANY titled new foreground after a verified
    action — it verifies the anchor's launcher-ness, not that the NEW window was caused
    by the session. A foreign app that wins the foreground race right after a verified
    click becomes the session target (subsequent dispatches then MATCH it).
    """
    backend = GuardBackend()
    dialog_anchor = WindowInfo(
        hwnd=5, pid=500, process_name="explorer.exe", window_class="#32770", title="Run",
    )
    backend.set_active_window(dialog_anchor)
    guard = _guard(backend)
    guard.rebind(dialog_anchor)
    backend.set_active_window(FOREIGN)
    guard.reanchor_after_success(FOREIGN)  # the controller calls this after a VERIFIED action
    assert guard.bound is not None and guard.bound.hwnd == FOREIGN.hwnd  # the finding
    # the next dispatch now MATCHes the (formerly foreign) window:
    assert guard.verify_pre_dispatch(_click()) is None


def test_rt2_refocus_ambiguity_cannot_redirect_a_dispatch() -> None:
    """A foreign window with an IDENTICAL title to the bound target can win the refocus
    resolution, but the post-refocus identity re-check must still REJECT the dispatch
    (input can never be redirected through refocus ambiguity)."""
    impostor = WindowInfo(
        hwnd=44, pid=444, process_name="evil.exe", window_class="Evil", title="Book1 - Excel",
    )
    backend = GuardBackend()
    backend.set_windows([impostor, FOREIGN])  # impostor is top of Z-order
    backend.set_active_window(FOREIGN)
    guard = _guard(backend, focus_guard={"policy": "refocus_then_abort"})
    guard.rebind(TARGET)  # title 'Book1 - Excel' resolves to the IMPOSTOR
    verdict = guard.verify_pre_dispatch(_click())
    assert verdict is not None and verdict.blocking  # dispatch refused either way
    assert backend.execute_calls == 0


def test_rt2_observe_only_is_a_host_policy_opt_out_and_annotates() -> None:
    """``observe_only`` turns FOCUS_TAKEN_BY into a non-blocking annotation — that is an
    explicit HOST policy choice (fail-closed default is ``abort``), not a model-reachable
    bypass. The annotation must still be emitted so the driver sees the interference."""
    backend = GuardBackend()
    backend.set_active_window(TARGET)
    guard = _guard(backend, focus_guard={"policy": "observe_only"})
    guard.rebind(TARGET)
    backend.set_active_window(FOREIGN)
    verdict = guard.verify_pre_dispatch(_click())
    assert verdict is not None and verdict.blocking is False
    assert verdict.event.startswith("FOCUS_TAKEN_BY")
    # the default remains fail-closed:
    assert parse_interference(None).focus_guard.policy == "abort"


def test_rt2_broken_identity_probe_fails_closed() -> None:
    """A guard probe that raises (broken Win32) must fail CLOSED: identity unavailable ->
    blocking rejection, never a silent dispatch."""
    backend = GuardBackend()
    backend.set_active_window(TARGET)
    guard = _guard(backend)
    guard.rebind(TARGET)

    def broken() -> WindowInfo | None:
        raise RuntimeError("win32 blew up")

    backend.query_foreground_window = broken  # type: ignore[method-assign]
    verdict = guard.verify_pre_dispatch(_click())
    assert verdict is not None and verdict.blocking
    assert verdict.event.startswith("FOCUS_IDENTITY_UNKNOWN")


# =====================================================================================
# RT3 — rate-limit / burst semantics
# =====================================================================================


def test_rt3_burst_never_shortens_the_next_fresh_gate() -> None:
    """A burst capture refreshes the pacing timestamp: the next FRESH capture must still
    wait the full interval (the exemption can never accelerate gated captures)."""
    enforcer = LimitEnforcer(Limits(min_screenshot_interval_ms=250).validate())
    assert enforcer.can_screenshot() is True  # first capture free
    enforcer.record_screenshot()
    assert enforcer.can_screenshot() is False  # gated
    enforcer.record_burst_screenshot()  # intra-step capture mid-window
    assert enforcer.can_screenshot() is False  # still gated (window REFRESHED, not cleared)
    snapshot = enforcer.snapshot()
    assert snapshot["burst_screenshots"] == 1 and snapshot["screenshots"] == 2


def test_rt3_burst_accounting_covers_every_capture_class() -> None:
    """Burst captures are counted and paced like all others: screenshots == gated +
    burst; the counters cannot disagree."""
    enforcer = LimitEnforcer(Limits(min_screenshot_interval_ms=0).validate())
    for _ in range(3):
        enforcer.record_screenshot()
        enforcer.record_burst_screenshot()
        enforcer.record_burst_screenshot()
    snapshot = enforcer.snapshot()
    assert snapshot["screenshots"] == 9
    assert snapshot["burst_screenshots"] == 6


async def test_rt3_fresh_loop_top_capture_still_trips_fail_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interval gate on FRESH observations is intact: a 60s interval makes the second
    loop-top capture trip the wait-ceiling LimitExceeded (audited fail-closed stop)."""
    session_id, bundle, _backend, _ = make_session(
        monkeypatch,
        dry_run=False,
        require_approval=True,
        limits={"min_screenshot_interval_ms": 60000},
    )
    provider = ScriptedProvider([])
    bundle.agent.provider = provider
    response = await server.run_goal(session_id, "two steps", approve_next_action=True)
    assert response["ok"] is False
    assert response["termination_reason"] in {"limit_exceeded", "failed"}, response
    events = audit_events(bundle, session_id)
    assert any(e["event_type"] == "limit_exceeded" for e in events), events


async def test_rt3_direct_execute_path_captures_are_burst_bounded_per_action(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The burst exemption is structurally bounded: one direct call performs at most a
    validate probe + post-action capture (2 burst captures), all accounted."""
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    for _ in range(3):
        await server.computer_execute(session_id, "click", x=10, y=10)
    snapshot = bundle.enforcer.snapshot()
    # 3 calls x (validate burst + post_action burst), with the observe-reuse doctrine
    # replacing the first fresh capture after the opening one:
    assert snapshot["burst_screenshots"] == 6
    assert snapshot["screenshots"] >= snapshot["burst_screenshots"]


# =====================================================================================
# RT4 — input-engine parity under fault
# =====================================================================================


def test_rt4_sendinput_ret_zero_fails_closed_with_one_attempt() -> None:
    """SendInput returning 0 (UIPI/blocked) must raise InputBlockedError and deliver
    NOTHING — a single failed attempt, no retry storm (strictly stronger than
    pyautogui's silent swallow)."""
    import computer_use_mcp.backend as backend_module

    fake = _ParityFakeUser32(send_results=[0])
    original = backend_module._user32
    backend_module._user32 = fake
    try:
        engine = SendInputEngine()
        with pytest.raises(InputBlockedError, match="blocked"):
            engine.click(30, 45)
        assert len(fake.batches) == 1  # one attempt, fail-closed, no retries
    finally:
        backend_module._user32 = original


def test_rt4_failsafe_corner_blocks_both_engines_identically() -> None:
    """The failsafe corner maps to InputBlockedError on BOTH engines; the SendInput
    engine dispatches ZERO events when blocked."""
    import computer_use_mcp.backend as backend_module

    pa = _ParityPyautogui(fail=True)  # emulates pyautogui's FailSafeException
    with pytest.raises(InputBlockedError):
        PyAutoGuiInputEngine(pa).click(30, 45)
    assert pa.calls == []  # nothing dispatched from the corner

    fake = _ParityFakeUser32(cursor=(0, 0))  # the REAL replicated corner check
    original = backend_module._user32
    backend_module._user32 = fake
    try:
        with pytest.raises(InputBlockedError, match="failsafe"):
            SendInputEngine().click(30, 45)
        assert fake.batches == []  # blocked BEFORE any physical input
    finally:
        backend_module._user32 = original
    assert METRICS == (0, 0, 1920, 1080)  # the corner math assumed this virtual screen


async def test_rt4_prestopped_token_blocks_every_action_type_on_the_host_path(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-stopped token must yield zero dispatches for EVERY input action family on
    the host path (stop token checked before every physical input)."""
    session_id, bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    bundle.context.stop.stop()
    hostile_actions = [
        ("click", {"x": 10, "y": 10}),
        ("type", {"text": "hello"}),
        ("hotkey", {"keys": ["ctrl", "a"]}),
        ("scroll", {"delta": 5}),
        ("move", {"x": 5, "y": 5}),
    ]
    for action_name, kwargs in hostile_actions:
        response = await server.computer_execute(session_id, action_name, **kwargs)
        assert response.get("stopped") is True or response.get("ok") is False, response
    assert backend.executed == []


# =====================================================================================
# RT5 — checkpoint / subtask payload hygiene
# =====================================================================================


def test_rt5_checkpoints_redact_secrets_at_serialize_time(tmp_path: Any) -> None:
    """A checkpoint whose goal carries a secret must be REDACTED at serialize time: no
    raw secret anywhere in the sealed file (write-time redaction, not just audit)."""

    manager, kwargs, _state = build_checkpoint_state(tmp_path)
    kwargs["goal"] = "log in with password=hunter2 and AKIAIOSFODNN7EXAMPLE"
    path = manager.write_checkpoint(**kwargs)
    blob = path.read_text(encoding="utf-8")
    assert "hunter2" not in blob
    assert "AKIAIOSFODNN7EXAMPLE" not in blob
    assert "[REDACTED:" in blob  # the redaction markers are in the file


async def test_rt5_subtask_summaries_and_queue_results_are_payload_free(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subtask list summaries carry status/counts only (no result payloads, no base64);
    queue results never carry per-item images even for executed items."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    created = server.create_subtask(session_id, "click the thing")
    assert created.get("ok") is True or "subtask_id" in created, created
    listing = server.list_subtasks(session_id)
    assert "base64" not in json.dumps(listing).lower()
    assert "screenshot" not in json.dumps(listing).lower()
    response = await server.computer_execute(
        session_id, "click", x=10, y=10, follow_ups=[{"action": "wait", "delta": 1}],
        include_screenshot_after=False,
    )
    assert "screenshot_after_base64" not in response
    for entry in response.get("follow_up_results", []):
        assert "screenshot_after_base64" not in entry
    assert len(executed_summary(backend)) == 2


# =====================================================================================
# RT6 — dry-run misread fuzz
# =====================================================================================


async def test_rt6_dry_run_fuzz_never_dispatches_and_always_banners(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FUZZ: 25 hostile/valid action attempts against a dry-run session. Every executed-
    shaped response must start with the unmistakable banner; nothing may dispatch."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=True, require_approval=False, limits=FAST_LIMITS
    )
    attempts: list[tuple[str, dict[str, Any]]] = [
        ("click", {"x": 10, "y": 10}),
        ("click", {"x": -5000, "y": 10}),  # out of bounds
        ("double_click", {"x": 10, "y": 10}),
        ("drag", {"x": 1, "y": 2, "x2": 3, "y2": 4}),
        ("drag", {"x": 1, "y": 2}),  # malformed
        ("type", {"text": "hello world"}),
        ("type", {"text": "format C: /fs:ntfs /y"}),  # destructive
        ("type", {"text": "password=hunter2"}),  # secret-like
        ("keypress", {"keys": ["enter"]}),
        ("keypress", {"keys": ["enter", "ctrl"]}),  # chord on keypress
        ("hotkey", {"keys": ["ctrl", "a"]}),
        ("hotkey", {"keys": ["hello"]}),  # word payload
        ("scroll", {"delta": 5}),
        ("scroll", {"delta": -999}),  # out of range
        ("wait", {"delta": 2}),
        ("move", {"x": 5, "y": 5}),
        ("move", {}),  # missing point
        ("focus_window", {"target": "Book1 - Excel"}),
        ("focus_window", {}),  # missing target
        ("ensure_app", {"target": "excel"}),
        ("ensure_app", {}),  # missing target
        ("key", {"keys": ["a"]}),  # non-existent action
        ("triple_click", {"x": 1, "y": 2}),  # non-existent action
        ("done", {}),
        ("", {}),  # empty action name
    ]
    for action_name, kwargs in attempts:
        response = await server.computer_execute(session_id, action_name, **kwargs)
        assert isinstance(response, dict), (action_name, response)
        if response.get("ok") is True:
            message = str(response.get("message", ""))
            assert message.startswith("DRY-RUN (no input dispatched):"), (action_name, message)
        else:
            assert response.get("ok") is False, (action_name, response)
    # follow_ups batches in dry-run:
    response = await server.computer_execute(
        session_id, "click", x=10, y=10,
        follow_ups=[{"action": "type", "text": "format C: /fs:ntfs /y"}],
    )
    assert backend.executed == []  # NOTHING dispatched across the whole fuzz
    assert (
        response.get("stopped") is True
        or response.get("ok") is False
        or str(response.get("message", "")).startswith("DRY-RUN (no input dispatched):")
    )


# =====================================================================================
# RT7 — MCP contract fuzz + scorer flags
# =====================================================================================


def test_rt7_new_params_are_trailing_optional_none_equals_legacy() -> None:
    """Every perf-004 tool-signature addition is a TRAILING OPTIONAL param with a None
    default (legacy behavior), across the whole tool surface."""
    expected_new: dict[str, set[str]] = {
        "start_session": {"interference"},
        "computer_execute": {"include_screenshot_after", "follow_ups"},
    }
    for tool_name, new_params in expected_new.items():
        signature = inspect.signature(getattr(server, tool_name))
        parameters = list(signature.parameters.values())
        for name in new_params:
            position = [p.name for p in parameters].index(name)
            assert position == len(parameters) - 1 or all(
                p.name in new_params for p in parameters[position + 1 :]
            ), f"{tool_name}.{name} is not trailing"
            param = parameters[position]
            assert param.default is None, f"{tool_name}.{name} default {param.default!r}"
    # legacy: computer_execute keeps the pre-perf-004 parameter order up front
    params = list(inspect.signature(server.computer_execute).parameters)
    assert params[:12] == [
        "session_id", "action", "x", "y", "text", "keys", "delta", "approved",
        "expected_effect", "x2", "y2", "target",
    ]
    assert params[12:] == ["include_screenshot_after", "follow_ups"]  # the ONLY additions


async def test_rt7_old_client_shapes_accepted_on_the_tool_surface(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-perf-004 call shapes (no new params) still work on every tool end-to-end."""
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    observe_response = server.computer_observe(session_id)
    assert observe_response and observe_response[0].type == "text"
    assert server.computer_screenshot(session_id)[0].type == "text"
    response = await server.computer_execute(session_id, "click", x=10, y=10)
    assert response["ok"] is True
    assert "screenshot_after_base64" in response  # legacy payload byte-compatible
    bundle = server._get_bundle(session_id)
    bundle.agent.provider = ScriptedProvider([AgentDecision(status="done", summary="done")])
    goal = await server.run_goal(session_id, "legacy goal")
    assert goal["termination_reason"] == "completed"
    created = server.create_subtask(session_id, "manual step")
    assert created.get("ok") is True or "subtask_id" in created
    server.list_subtasks(session_id)
    server.get_session_progress(session_id)
    assert backend.executed  # the legacy calls really executed
    stopped = server.stop_session(session_id)  # legacy shape, no new params
    assert stopped.get("ok") is False or stopped.get("stopped") is True or "session_id" in stopped


def test_rt7_unknown_extra_fields_fail_closed() -> None:
    """Unknown limit names / interference fields are REJECTED fail-closed (never
    silently ignored) — the designed extra=forbid surface; no session is created by a
    rejected call."""
    registry = SessionRegistry(max_sessions=2)
    original_registry, original_bundles = server._registry, server._bundles
    server._registry, server._bundles = registry, {}
    try:
        response = server.start_session(limits={"max_actions": 5, "bogus_field": 1})
        assert response["ok"] is False and response["error"] == "invalid_limits"
        response = server.start_session(interference={"focus_guard": {"bogus": True}})
        assert response["ok"] is False and response["error"] == "invalid_interference"
        response = server.start_session(limits={"max_actions": "not-a-number"})
        assert response["ok"] is False and response["error"] == "invalid_limits"
        response = server.start_session(interference={"no_such_section": {}})
        assert response["ok"] is False and response["error"] == "invalid_interference"
        assert len(server._bundles) == 0  # nothing was created by the rejected calls
    finally:
        server._registry, server._bundles = original_registry, original_bundles


def test_rt7_scorer_seed_never_logs_and_model_label_cannot_corrupt_prior_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A6 scorer flags: --seed mode returns before any log append; a hostile --model
    label (pipes/newlines) is recorded as DATA in the JSONL and cannot corrupt prior
    JSONL rows (append-only)."""
    jsonl = tmp_path / "runs-log.jsonl"
    md = tmp_path / "RUNS.md"
    jsonl.write_text('{"task": "PRIOR-EVIDENCE-ROW"}\n', encoding="utf-8")
    md.write_text("# runs\n", encoding="utf-8")
    monkeypatch.setattr(score_task_module, "RUN_LOG_JSONL", jsonl)
    monkeypatch.setattr(score_task_module, "RUNS_MD", md)
    append_run_log(
        {"task": "h1", "completed": True},
        run_record={},
        model="evil |\n| injected | row |",  # hostile markdown/table payload
        ts_utc="2026-09-06T00:00:00Z",
    )
    jsonl_lines = jsonl.read_text(encoding="utf-8").splitlines()
    assert jsonl_lines[0] == '{"task": "PRIOR-EVIDENCE-ROW"}'  # prior row untouched
    assert len(jsonl_lines) == 2
    assert "evil" in jsonl_lines[1]  # the label IS recorded (as data)
    # --seed mode exits before logging:
    seed_calls: list[Any] = []

    def fake_seed(_task: Any) -> dict[str, Any]:
        seed_calls.append(1)
        return {"seeded": True}

    monkeypatch.setattr(score_task_module, "seed_for_task", fake_seed)
    rc = score_main(["--seed", "--task", "whatever.yaml", "--model", "glm-flash"])
    assert rc == 0 and seed_calls == [1]
    assert len(jsonl.read_text(encoding="utf-8").splitlines()) == 2  # --seed appended nothing


def test_rt7_scorer_no_log_appends_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """--no-log must leave both evidence files byte-identical while still scoring."""
    jsonl = tmp_path / "runs-log.jsonl"
    md = tmp_path / "RUNS.md"
    jsonl.write_text('{"task": "PRIOR"}\n', encoding="utf-8")
    md.write_text("# runs\n", encoding="utf-8")
    monkeypatch.setattr(score_task_module, "RUN_LOG_JSONL", jsonl)
    monkeypatch.setattr(score_task_module, "RUNS_MD", md)
    monkeypatch.setattr(
        score_task_module, "score_task", lambda _task, _record: {"task": "h1", "completed": True}
    )
    rc = score_main(["--task", "h1.yaml", "--no-log", "--model", "glm-flash"])
    assert rc == 0  # score_task reported completed=True -> rc 0
    assert jsonl.read_text(encoding="utf-8") == '{"task": "PRIOR"}\n'
    assert md.read_text(encoding="utf-8") == "# runs\n"


# =====================================================================================
# RT9 — type-validator benign-text false positives (B12 observation, quantified)
# =====================================================================================

#: Benign strings a research/office task may legitimately type.
BENIGN_CORPUS: list[str] = [
    "the closed-form solution of the integral",
    "Closed Form X-4782 application approved",
    "https://doi.org/10.1000/182",
    "DOI: 10.1145/3442188.3445922",
    "see figure 3.14 on page 42",
    "sha256 abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
    "commit 3f2a9c1 fixed the null deref",
    "IPv6 ::1 loopback and 192.168.1.10 gateway",
    "C:\\Users\\localadmin\\Documents\\report Q3 final (v2).docx",
    "select name, value from settings where id = 7",
    "echo hello world",
    "total = 1,234.56 USD",
    "AKRA-2026-0042 invoice reference",
    "Bearer of good news",
    "-----BEGIN PUBLIC KEY----- (diagram label in slides)",
    "John's password: (none stored here)",
    "ghp placeholder-in-docs",
    "eyJhbGciOi (truncated JWT fragment in a blog quote)",
]

#: Real secrets that the defense net should catch somewhere (gate and/or redaction).
#: The Slack/Stripe corpus strings are assembled at RUNTIME from parts: the values are
#: byte-identical to the original test literals (identical validator input — no
#: assertion is weakened), but no provider-credential push-protection regex matches
#: this source file at rest.
_SLACK_TOKEN = "xo" + "xb-123456789012-1234567890123-abcdefghijklmnopqrstuvwx"
_STRIPE_KEY = "s" + "k_live_4eC39HqLyjWDarjtT1zdp7dc"
_GHP_TOKEN = "gh" + "p_0123456789abcdefghijklmnopqrstuvwxyzABC"
SECRET_CORPUS: list[str] = [
    "password=hunter2",
    "PASSWORD:_correct-horse-battery-staple",
    "api_key = sk-proj-abcdefghijklmnop0123456789",
    "AKIAIOSFODNN7EXAMPLE",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "-----BEGIN RSA PRIVATE KEY-----",
    _GHP_TOKEN,
    _SLACK_TOKEN,
    _STRIPE_KEY,
    "database_url=postgres://admin:s3cret@db.internal:5432/prod",
]


def _type_decision(text: str) -> Any:
    policy = SafetyPolicy()
    action = GroundedAction(action="type", text=text, confidence=1.0)
    ctx = SafetyContext(active_process_name="notepad.exe", window_title="Untitled - Notepad")
    state = SimpleNamespace(stopped=False, dry_run=False, step_count=0, max_steps=100,
                            require_approval=True)
    return policy.evaluate(action, state, ctx)


def _flagged(decision: Any) -> bool:
    return (not decision.allowed) or "secret" in str(decision.reason).lower()


class SimpleNamespace:
    """Minimal stand-in for SessionState fields consumed by the policy."""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


def test_rt9_finding_benign_text_false_positives_quantified_b12() -> None:
    """FINDING RT9-1 (B12 observation quantified; NO FIX — R-02 deferred to ROADMAP).

    Measured false-positive surface of the legacy TYPE secret gate (substring markers,
    no word boundaries), 18-string benign corpus:

    - 'the closed-form solution of the integral'  -> 'rm ' inside 'closed-form '
    - 'Closed Form X-4782 application approved'   -> 'rm ' inside 'form '
    - "John's password: (none stored here)"       -> 'password' with an EMPTY value

    3/18 benign strings are REJECTED as 'secret, credential, or destructive'.
    The DOI strings do NOT reproduce as FPs in the current tree. The secret net at the
    gate is narrow (3/10 blocked: the assignment-style markers) — the REDACTION layer
    compensates for AKIA/JWT/private-key/URL-credentials at the dispatch sink, leaving
    the GitHub/Slack/Stripe token classes uncovered at BOTH layers (see the redaction
    test).
    """
    flagged = [t for t in BENIGN_CORPUS if _flagged(_type_decision(t))]
    assert flagged == [
        "the closed-form solution of the integral",
        "Closed Form X-4782 application approved",
        "John's password: (none stored here)",
    ]
    # pinned: exactly 3/18 false positives; every other benign string passes cleanly
    assert len(flagged) == 3 and len(BENIGN_CORPUS) == 18


def test_rt9_finding_gate_misses_compensated_by_redaction_except_modern_tokens() -> None:
    """The second half of RT9-1: the type gate blocks only 3/10 secret corpus entries;
    redaction at the dispatch sink covers 7/10 — the GitHub/Slack/Stripe token classes
    are covered by NEITHER layer (defense-in-depth gap, documented in SAFETY §7 as the
    10-pattern list; typed into a local app they never leave the machine, so exposure
    is limited to the provider/audit sinks)."""
    gate_blocked = [t for t in SECRET_CORPUS if _flagged(_type_decision(t))]
    assert len(gate_blocked) == 3
    redacted = [t for t in SECRET_CORPUS if redact_text(t)[0] != t]
    assert len(redacted) == 7
    uncovered = set(SECRET_CORPUS) - set(gate_blocked) - set(redacted)
    assert uncovered == {_GHP_TOKEN, _SLACK_TOKEN, _STRIPE_KEY}
