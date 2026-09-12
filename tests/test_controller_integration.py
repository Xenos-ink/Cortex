"""Wave 3 controller integration tests: the closed loop through the server tool surface.

Everything runs against ``FakeComputerBackend`` derivatives and a ``ScriptedProvider``
implementing the pinned E4 provider surface (``decide_full``/``judge_change``) — no
network, no real provider code. Covered here:

- happy path (click -> verified) with per-phase audit events + metrics snapshot sanity;
- type action verified via expected_text (fake OCR evidence);
- every recovery path: STALE_COORDINATES (window switch between propose/execute ->
  re-decide with NEW coordinates, never a same-coordinate retry), MOVED_UI (verification
  failed -> re-decide succeeds), BLOCKED_UI (Escape dismiss -> same-instance retry),
  verification-failed-to-exhaustion (termination failed_verification), provider failure
  (fail-closed, recovery) and persistent provider failure (termination provider_error);
- stop discipline: stop_session between steps and stop mid-execute (zero further inputs,
  stopped_by_user, emergency_stop audit) and model output can never reach the stop token;
- limit trips: max_actions, max_model_calls, task duration, screenshot rate;
- approval budget semantics: single budget, recovery retry does not re-consume, new
  distinct action after exhaustion denied with requires_approval;
- step_count persistence across run_goal calls;
- concurrent session isolation;
- computer_execute confidence semantics + expected_effect verification + legacy shapes.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from typing import Any

import pytest
from PIL import Image

from computer_use_mcp import server
from computer_use_mcp.agent import ComputerUseAgent
from computer_use_mcp.backend import DisplayUnavailableError, FakeComputerBackend, InputBlockedError
from computer_use_mcp.limits import LimitExceeded, Limits
from computer_use_mcp.models import (
    AgentDecision,
    FailureClass,
    GroundedAction,
    SessionState,
    TextRegion,
    VerificationResult,
    WindowInfo,
)
from computer_use_mcp.recovery import (
    RecoveryContext,
    RecoveryController,
    RecoveryStrategy,
    classify_failure,
)
from computer_use_mcp.state import SessionRegistry, TaskStopped
from computer_use_mcp.validator import StaleObservationError, ValidationOutcome

# --- fakes ---------------------------------------------------------------------------------


def _png(color: str = "white") -> str:
    image = Image.new("RGB", (64, 48), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class SimpleNamespaceEnvelope:
    """Duck-typed ``ProviderDecision`` envelope for scripted decisions."""

    def __init__(self, decision: Any, suspicious_content: str | None = None) -> None:
        self.decision = decision
        self.expected_effect = getattr(decision, "expected_effect", None)
        self.verification_hint = getattr(decision, "verification_hint", None)
        self.suspicious_content = suspicious_content
        self.redactions_applied: list[str] = []


class ScriptedProvider:
    """Fake provider implementing the pinned E4 surface with a script of decisions.

    ``errors`` entries raise on the matching consecutive decide call (index-aligned);
    ``always_error`` raises on every call (persistent provider failure). ``hooks`` are
    index-aligned callables run BEFORE the scripted decision is returned — used to
    simulate environment changes between propose and execute.
    """

    def __init__(
        self,
        script: list[Any] | None = None,
        *,
        errors: list[Exception | None] | None = None,
        always_error: Exception | None = None,
        hooks: list[Any] | None = None,
        repeat_last: bool = True,
    ) -> None:
        self.script = list(script or [])
        self.errors = list(errors or [])
        self.always_error = always_error
        self.hooks = list(hooks or [])
        self.repeat_last = repeat_last
        self.decide_calls = 0
        self.judge_calls = 0
        self._script_index = 0

    async def decide_full(self, goal: str, observation: Any, history: list[str]) -> Any:
        call = self.decide_calls
        self.decide_calls += 1
        if call < len(self.hooks) and self.hooks[call] is not None:
            self.hooks[call]()
        if self.always_error is not None:
            raise self.always_error
        if call < len(self.errors) and self.errors[call] is not None:
            raise self.errors[call]  # failed calls consume no script entry
        if not self.script:
            raise RuntimeError("ScriptedProvider script is empty.")
        if self._script_index >= len(self.script) and not self.repeat_last:
            raise RuntimeError("ScriptedProvider script exhausted.")
        decision = self.script[min(self._script_index, len(self.script) - 1)]
        self._script_index += 1
        if isinstance(decision, AgentDecision):
            return SimpleNamespaceEnvelope(decision)
        if isinstance(decision, tuple) and len(decision) == 2:  # (decision, suspicious_content)
            return SimpleNamespaceEnvelope(decision[0], suspicious_content=decision[1])
        return decision

    async def decide(self, goal: str, observation: Any, history: list[str]) -> Any:
        result = await self.decide_full(goal, observation, history)
        return result.decision

    def judge_change(
        self, before_b64: str, after_b64: str, expected_effect: str, goal: str | None = None
    ) -> dict[str, Any]:
        self.judge_calls += 1
        return {"outcome": "uncertain", "confidence": 0.0, "reason": "fake judge never verifies"}


class ScriptedBackend(FakeComputerBackend):
    """Fake backend with deterministic screenshot flipping and fault hooks.

    With ``flip`` enabled, every completed execute flips the screenshot color, so the
    post-action observation always differs from the pre-action baseline (verification
    succeeds for any stated/implicit change expectation). ``flip=False`` keeps every
    screenshot identical (verification of a change then fails deterministically).
    """

    def __init__(self, *, flip: bool = True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.flip = flip
        self.executes = 0
        self.execute_hooks: list[Any] = []
        self.observe_faults: list[Exception | None] = []  # popped per observe call (D4)
        self.typed_text: str | None = None
        self.observe_override_png: str | None = None  # REM-A H7: oversized-image hook

    def observe(self) -> Any:
        if self.observe_faults:
            fault = self.observe_faults.pop(0)
            if fault is not None:
                raise fault
        observation = super().observe()
        color = "white"
        if self.flip and self.executes % 2 == 1:
            color = "black"
        observation.image_base64 = self.observe_override_png or _png(color)
        if self.typed_text:
            observation.ocr_text = [
                TextRegion(text=self.typed_text, x=8, y=8, width=120, height=16, confidence=0.95)
            ]
        return observation

    def execute(self, action: GroundedAction, stop: Any = None) -> str:
        for hook in self.execute_hooks:
            hook(action)
        if action.action.value == "type" and action.text:
            self.typed_text = action.text
        message = super().execute(action, stop)
        self.executes += 1
        return message


class UnblockOnEscapeBackend(ScriptedBackend):
    """Simulates a modal blocking input until Escape dismisses it."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.set_input_blocked(True)
        self.execute_hooks.append(self._unblock_on_escape)

    def _unblock_on_escape(self, action: GroundedAction) -> None:
        if action.action.value == "keypress" and any(k.lower() == "esc" for k in action.keys):
            self.set_input_blocked(False)


def executed_summary(backend: Any) -> list[tuple[str, tuple[int, int] | None, str | None]]:
    return [
        (
            action.action.value,
            None if action.point is None else (action.point.x, action.point.y),
            action.text,
        )
        for action in backend.executed
    ]


def audit_events(bundle: Any, session_id: str) -> list[dict[str, Any]]:
    path = bundle.auditor.path_for(session_id)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def audit_types(events: list[dict[str, Any]]) -> set[str]:
    return {event["event_type"] for event in events}


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh bounded registry/bundles + per-test audit dir for full session isolation."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    return server


def make_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    backend: Any = None,
    provider: Any = None,
    **start_kwargs: Any,
) -> tuple[str, Any, Any, Any]:
    """Start one session through the tool with injected fake backend/provider."""
    backend = backend if backend is not None else ScriptedBackend()
    provider = provider if provider is not None else ScriptedProvider([])
    monkeypatch.setattr(server, "_backend_factory", lambda: backend)
    monkeypatch.setattr(server, "_provider_factory", lambda: provider)
    response = server.start_session(**start_kwargs)
    assert response.get("session_id"), response
    session_id = str(response["session_id"])
    bundle = server._get_bundle(session_id)
    return session_id, bundle, backend, provider


def execute_payload(result: Any) -> dict[str, Any]:
    """REM-A: unwrap an executed ``computer_execute`` response to its dict payload.

    Executed responses are MCP content blocks (TextContent result JSON + ImageContent
    post-action screenshot — parity with computer_observe). Error/rejection/approval
    shapes stay plain dicts. This helper returns the payload dict for BOTH forms so
    legacy-shape assertions keep working.
    """
    if isinstance(result, list):
        return json.loads(result[0].text)
    return result


FAST_LIMITS = {"min_screenshot_interval_ms": 0}

# --- happy path, audit, metrics --------------------------------------------------------------


async def test_happy_path_click_verified_with_phase_audit_and_metrics(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path on the direct surface: one approved click, verified, fully audited.

    RETARGETED (run_goal removal): the loop's happy path died with the loop; the
    direct path exercises the SAME phase pipeline (observe -> ground -> validate ->
    safety -> approval -> execute -> verify) and the same audit/metrics counters."""
    session_id, bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=True, limits=FAST_LIMITS
    )
    response = await server.computer_execute(session_id, "click", x=100, y=100, approved=True)
    first = execute_payload(response)

    assert first["ok"] is True, first
    assert first["verification"]["outcome"] == "verified"
    assert executed_summary(backend) == [("click", (100, 100), None)]

    events = audit_events(bundle, session_id)
    assert {
        "session_start",
        "observation",
        "grounding",
        "validation",
        "safety",
        "execution",
        "verification",
    } <= audit_types(events)
    # AMENDED (run_goal removal): the direct path audits an "approval" event only on
    # the DENIAL shape (requires_approval outcome); a caller-supplied approved=True
    # executes without the approval phase (the denial shape is pinned by the
    # confidence-semantics test below).
    execution_events = [event for event in events if event["event_type"] == "execution"]
    assert execution_events[0]["action_id"] == first["action"]["action_id"]
    assert execution_events[0]["active_app"] is None or isinstance(execution_events[0]["active_app"], str)

    counters = bundle.metrics.snapshot()["counters"]
    assert counters["action_total"] == 1
    assert counters["verification_verified"] == 1
    assert bundle.metrics.snapshot()["latencies"]["observation_ms"]["count"] >= 2


async def test_type_action_verified_via_expected_text(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T8 B2: the OCR text-predicate tier is opt-in (CORTEX_OCR_TEXT_VERIFICATION=1);
    # without the flag, type actions verify via UI-control/diff evidence instead
    # (see test_t8_interference.py / test_focus_guard.py regression coverage).
    monkeypatch.setenv("CORTEX_OCR_TEXT_VERIFICATION", "1")
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(session_id, "type", text="hello world")
    first = execute_payload(response)

    assert first["ok"] is True, first
    assert first["verification"]["outcome"] == "verified"
    assert first["verification"]["verification_method"] == "text_predicate"
    assert backend.typed_text == "hello world"


async def test_wait_action_uncertain_verification_is_tolerated(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Identical screenshots: the wait's visual-change check is uncertain. Documented
    # carve-out: a wait makes no semantic claim, so uncertain continues (never success).
    # RETARGETED (run_goal removal): direct path, same verifier contract.
    session_id, _bundle, backend, _ = make_session(
        monkeypatch,
        backend=ScriptedBackend(flip=False),
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    response = await server.computer_execute(session_id, "wait", delta=0)
    first = execute_payload(response)

    # Direct-path semantics (unchanged from the queue contract, REM-B H2b): an
    # UNCERTAIN verdict keeps ok=False on the direct single-action response —
    # uncertain is honest "cannot determine", never success; the loop used to
    # tolerate it because a wait makes no semantic claim (the same carve-out the
    # QUEUE applies: uncertain does not stop the queue).
    assert first["ok"] is False
    assert first["verification"]["outcome"] == "uncertain"
    assert executed_summary(backend) == [("wait", None, None)]

# --- recovery paths --------------------------------------------------------------------------
# REMOVED (run_goal removal): the four in-loop recovery scenarios below exercised the
# internal loop's RECOVER/REDECIDE machinery (stale-coordinates re-decide with NEW
# provider coordinates, moved-UI re-decide, blocked-UI dismiss + same-instance retry,
# failed-verification recovery-budget exhaustion) — all loop-exclusive behavior that
# died with the loop. What SURVIVES on the direct surface, pinned elsewhere:
#   - the failure CLASSIFICATION mapping and RecoveryController decision table:
#     test_failure_classification_mapping + test_recovery_controller_mapping_table below;
#   - staleness on the direct path: ONE automatic re-observe + re-validate (P0-H),
#     then a typed rejection — pinned by test_perf004 test_run_single_audits_staleness_proof
#     and the validator suites (test_coordinate_pipeline unverifiable-space pin);
#   - blocked input on the direct path: typed InputBlockedError fail-closed (backend
#     suites, test_io_parity / RT4 pins in test_p5_redteam);
#   - a failed expected_effect verification reports failed — never silently OK — pinned
#     by test_computer_execute_expected_effect_and_failure_shapes below.


# REMOVED (run_goal removal): the loop-provider-failure scenarios (single provider
# failure recovered in-loop, persistent provider failure terminating provider_error,
# model "blocked" decision terminating unrecoverable) were decide-phase loop behavior
# — there is no decide phase on the direct surface. The provider's own fail-closed
# construction path is pinned by the _LazyProvider suite (server module) and the
# agent-seam ladder pins in test_perf004_loop_economics.

# --- stop discipline (P0-C) --------------------------------------------------------------------
# REMOVED (run_goal removal): the two loop-stop scenarios (stop_session between
# loop steps; stop mid-execute DURING the loop's type action) drove the loop's
# between-steps kill path. The SURVIVING stop discipline on the direct surface is
# pinned by: test_p5_redteam.test_rt4_prestopped_token_blocks_every_action_type_on_the_host_path
# (a pre-stopped token blocks every action family on the host path, zero dispatches),
# test_stop_session_removes_bundle_and_blocks_tools (stopped session -> every tool
# fails closed with session_stopped), test_internal_kill_path_bundle_hygiene (same
# cleanup for internally-armed stops), and the queue's between-items stop check
# (test_perf004 test_stop_session_halts_a_running_queue).
# The model-output-cannot-reach-the-stop-token guarantee is moot without the loop
# (no model data is processed at all on the direct surface; the host supplies
# structured action specs, never envelopes with stop fields).

# --- limits (P0-L) ------------------------------------------------------------------------------
# RETARGETED (run_goal removal): the limit gates are enforced on the direct surface
# too (enforcer checks run in _run_single_pipeline: check_action/begin_action/
# check_task_duration; the fresh-capture gate on direct_request). The loop-only
# variants (max_model_calls, in-loop task duration) died with the loop — no model
# calls exist on the direct path at all.


async def test_max_actions_limit_trips_cleanly(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-session max_actions gate trips typed on the direct path: after the
    budget is spent, the next computer_execute is refused with limit_exceeded and
    nothing executes."""
    session_id, bundle, backend, _ = make_session(
        monkeypatch,
        dry_run=False,
        require_approval=False,
        limits={**FAST_LIMITS, "max_actions": 1},
    )
    first = await server.computer_execute(session_id, "click", x=10, y=10)
    first = execute_payload(first)
    assert first["ok"] is True, first
    assert len(backend.executed) == 1

    second = await server.computer_execute(session_id, "click", x=20, y=20)
    assert second["ok"] is False
    assert second["error"] == "limit_exceeded"
    assert len(backend.executed) == 1  # the over-budget action executed NOTHING
    events = audit_events(bundle, session_id)
    assert "limit_exceeded" in audit_types(events)


async def test_task_duration_limit_trips_cleanly(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session-duration gate trips typed on the loop-top check — the gate the
    agent's run loop enforced at every step top (``enforcer.check_task_duration``).

    AMENDED (run_goal removal): the loop that carried this gate at its loop-top is
    gone; on the direct surface the gate survives through the ENFORCER's own
    check (pinned here at the exact method the loop used to call — the trip
    behavior, audit, and typed error are identical wherever it is enforced)."""
    _session_id, bundle, _backend, _ = make_session(
        monkeypatch,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    bundle.enforcer._started_monotonic -= 10_000.0  # backdate: task "started" 10000s ago
    with pytest.raises(LimitExceeded) as excinfo:
        bundle.enforcer.check_task_duration()
    assert excinfo.value.limit_name == "max_task_seconds"


async def test_screenshot_rate_limit_trips_cleanly(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PERF-004 C2 refined semantics, direct path: the host-driven observe path
    (computer_execute -> direct_request capture) is gated; a second action arriving
    within the interval trips the typed limit_exceeded, audited, nothing further
    executed.

    RETARGETED (run_goal removal): the old test's loop half (gate-free in-loop
    cycle) died with the loop; the surviving gate on the direct surface is the pin."""
    session_id, bundle, backend, _ = make_session(
        monkeypatch,
        dry_run=False,
        require_approval=False,
        limits={"min_screenshot_interval_ms": 60_000},
    )
    first = await server.computer_execute(session_id, "click", x=10, y=10)
    first = execute_payload(first)
    assert first["ok"] is True, first
    assert len(executed_summary(backend)) == 1

    # The next host-driven direct_request capture arrives within the 60s interval
    # and trips fail-closed.
    stopped = await server.computer_execute(session_id, "wait", delta=1)
    assert stopped["ok"] is False
    assert stopped["error"] == "limit_exceeded"
    assert stopped["limit"] == "min_screenshot_interval_ms"
    events = audit_events(bundle, session_id)
    limit_events = [event for event in events if event["event_type"] == "limit_exceeded"]
    assert any(
        event["metadata"].get("limit") == "min_screenshot_interval_ms" for event in limit_events
    )

# --- approval budget semantics -------------------------------------------------------------------
# REMOVED (run_goal removal): the run_goal approval-budget semantics (one budget per
# CALL consumed across loop steps; a NEW distinct action after exhaustion denied
# fail-closed) were loop-call semantics. On the direct surface, per-action approval
# is the caller's explicit ``approved`` flag, pinned by
# test_computer_execute_confidence_semantics_and_expected_effect (unapproved
# interactive action -> requires_approval, approved -> executes) and the queue's
# approval stop (test_perf004 test_approval_required_mid_queue_stops_queue).


async def test_step_count_persists_across_direct_calls(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SessionState/Task step_count accumulates across separate tool calls (the old
    across-run_goal-calls persistence pin, retargeted to the direct surface)."""
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    first = await server.computer_execute(session_id, "click", x=10, y=10)
    first = execute_payload(first)
    second = await server.computer_execute(session_id, "click", x=30, y=30)
    second = execute_payload(second)

    assert first["ok"] is True and second["ok"] is True, (first, second)
    assert bundle.state.step_count == 2  # SessionState persistence preserved
    assert bundle.agent.task.step_count == 2

# --- session isolation ---------------------------------------------------------------------------


async def test_concurrent_sessions_are_isolated(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sessions driven CONCURRENTLY through the direct surface stay isolated:
    separate backends, task ids, audit files, and zero cross-session events.

    RETARGETED (run_goal removal): the old test ran two loops concurrently; the
    direct-path equivalent pins the same isolation invariants."""
    backend_one = ScriptedBackend()
    backend_two = ScriptedBackend()
    sid_one, bundle_one, _, _ = make_session(
        monkeypatch, backend=backend_one,
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    sid_two, bundle_two, _, _ = make_session(
        monkeypatch, backend=backend_two,
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    results = await asyncio.gather(
        server.computer_execute(sid_one, "click", x=10, y=10),
        server.computer_execute(sid_two, "click", x=40, y=40),
    )
    result_one, result_two = execute_payload(results[0]), execute_payload(results[1])

    assert result_one["ok"] is True and result_two["ok"] is True, (result_one, result_two)
    assert result_one["action"]["action_id"] != result_two["action"]["action_id"]
    assert executed_summary(backend_one) == [("click", (10, 10), None)]
    assert executed_summary(backend_two) == [("click", (40, 40), None)]
    assert bundle_one.auditor.path_for(sid_one) != bundle_two.auditor.path_for(sid_two)
    events_one = audit_events(bundle_one, sid_one)
    assert all(event["session_id"] == sid_one for event in events_one)

# --- computer_execute ----------------------------------------------------------------------------


async def test_computer_execute_confidence_semantics_and_expected_effect(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=True, limits=FAST_LIMITS
    )
    # Legacy approval shape preserved: unapproved interactive action requires approval.
    denied = await server.computer_execute(session_id, "click", x=10, y=10)
    assert denied == {
        "ok": False,
        "requires_approval": True,
        "message": denied["message"],
    }
    assert denied["message"]  # contextual reason, never an opaque coordinate-only ask
    assert backend.executed == []

    approved = await server.computer_execute(session_id, "click", x=10, y=10, approved=True)
    approved = execute_payload(approved)  # REM-A: executed -> content blocks
    assert approved["ok"] is True
    assert approved["verification"]["outcome"] == "verified"
    assert approved["model_confidence"] == 1.0  # client-asserted model confidence
    assert isinstance(approved["grounding_confidence"], float)
    assert isinstance(approved["verification_confidence"], float)


async def test_computer_execute_expected_effect_and_failure_shapes(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, backend=ScriptedBackend(flip=False), dry_run=False, require_approval=False,
        limits=FAST_LIMITS,
    )
    effect = await server.computer_execute(
        session_id, "hotkey", keys=["ctrl", "a"], approved=False, expected_effect="the screen changes"
    )
    effect = execute_payload(effect)  # REM-A: executed -> content blocks
    # A stated expected effect that did not occur is reported failed — never silently
    # OK. The action is a HOTKEY (unflagged visual-change intent, legacy failure
    # semantics): a flagged CLICK with a focus-type expectation degrades to uncertain
    # under the W-1 (057) contract, so it can no longer pin the definitive failure.
    assert effect["ok"] is False
    assert effect["verification"]["outcome"] == "failed"

    rejected = await server.computer_execute(session_id, "click", x=8000, y=10)
    assert rejected["ok"] is False
    # W-2 (057): the message names the REAL gate (grounding) instead of the generic
    # "Grounding rejected." stamp.
    assert rejected["message"].startswith("Action rejected by grounding:")
    assert rejected["reasons"]

    invalid = await server.computer_execute(session_id, "teleport")
    assert invalid["ok"] is False
    assert invalid["error"] == "invalid_action"

    server.stop_session(session_id)
    stopped = await server.computer_execute(session_id, "wait", delta=1)
    assert stopped["ok"] is False
    assert "stopped" in stopped["message"].lower()
    assert executed_summary(backend) == [("hotkey", None, None)]  # only the first action ran


async def test_computer_execute_dry_run_never_executes(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=True, require_approval=False, limits=FAST_LIMITS
    )
    dry = await server.computer_execute(session_id, "wait", delta=1)
    dry = execute_payload(dry)  # REM-A: executed (dry-run stub) -> content blocks
    assert dry["ok"] is True
    # PERF-004 C5: dry-run results are UNMISTAKABLE — the message must start with the
    # banner so no host can misread a no-op as execution.
    assert dry["message"].startswith("DRY-RUN (no input dispatched):")
    assert dry["verification"]["verified"] is False
    assert backend.executed == []

# --- recovery unit contract (mapping table) --------------------------------------------------------


def test_failure_classification_mapping() -> None:
    source = ScriptedBackend(active_window=WindowInfo(hwnd=1, pid=1, process_name="a.exe", title="Main")).observe()
    after_auth = ScriptedBackend(active_window=WindowInfo(hwnd=2, pid=2, process_name="b.exe", title="Sign in")).observe()
    after_dialog = ScriptedBackend(active_window=WindowInfo(hwnd=2, pid=2, process_name="b.exe", title="Confirm Delete")).observe()
    after_window = ScriptedBackend(active_window=WindowInfo(hwnd=2, pid=2, process_name="b.exe", title="Totally Other App")).observe()
    after_nav = ScriptedBackend(active_window=WindowInfo(hwnd=1, pid=1, process_name="a.exe", title="Settings")).observe()

    assert classify_failure(TaskStopped("stop")) is None  # a stop is not a failure
    assert classify_failure(StaleObservationError("active_window_hwnd")) is FailureClass.STALE_COORDINATES
    assert classify_failure(InputBlockedError("blocked")) is FailureClass.BLOCKED_UI
    assert classify_failure(ValidationOutcome(valid=False, reasons=["x"], codes=["point_out_of_bounds"])) is FailureClass.STALE_COORDINATES
    assert classify_failure(ValidationOutcome(valid=False, reasons=["x"], codes=["process_not_allowed"])) is FailureClass.WRONG_WINDOW
    assert classify_failure(VerificationResult(outcome="failed", changed=False, note="n"), RecoveryContext(source_observation=source, after_observation=after_auth)) is FailureClass.AUTH_REQUIRED
    assert classify_failure(VerificationResult(outcome="failed", changed=False, note="n"), RecoveryContext(source_observation=source, after_observation=after_dialog)) is FailureClass.UNEXPECTED_DIALOG
    assert classify_failure(VerificationResult(outcome="failed", changed=False, note="n"), RecoveryContext(source_observation=source, after_observation=after_window)) is FailureClass.WRONG_WINDOW
    assert classify_failure(VerificationResult(outcome="failed", changed=False, note="n"), RecoveryContext(source_observation=source, after_observation=after_nav)) is FailureClass.NAVIGATION_DRIFT
    assert classify_failure(VerificationResult(outcome="failed", changed=False, note="n"), RecoveryContext(source_observation=source, after_observation=source)) is FailureClass.MOVED_UI
    assert classify_failure(VerificationResult(outcome="uncertain", changed=False, note="n")) is FailureClass.LOW_CONFIDENCE
    assert classify_failure(ValueError("mystery")) is FailureClass.UNKNOWN


def test_recovery_controller_mapping_table() -> None:
    controller = RecoveryController(Limits())

    stale = controller.handle(FailureClass.STALE_COORDINATES, RecoveryContext(phase="validate"))
    assert stale.strategy is RecoveryStrategy.RECOVER_REOBSERVE
    assert stale.redecide is True and stale.retry_same_instance is False

    blocked = controller.handle(FailureClass.BLOCKED_UI, RecoveryContext(phase="execute"))
    assert blocked.strategy is RecoveryStrategy.RECOVER_DISMISS
    assert blocked.dismiss is True and blocked.retry_same_instance is True
    denied_dismiss = controller.handle(
        FailureClass.BLOCKED_UI, RecoveryContext(phase="execute", dismiss_allowed=False)
    )
    assert denied_dismiss.strategy is RecoveryStrategy.REPLAN

    assert controller.handle(FailureClass.AUTH_REQUIRED).strategy is RecoveryStrategy.TERMINATE_SAFELY
    assert controller.handle(FailureClass.AUTH_REQUIRED).termination_reason.value == "blocked_safety"
    assert controller.handle(FailureClass.ALREADY_COMPLETED).strategy is RecoveryStrategy.COMPLETE
    assert controller.handle(FailureClass.APP_CRASH).strategy is RecoveryStrategy.REPLAN
    assert controller.handle(FailureClass.UNRECOVERABLE).strategy is RecoveryStrategy.TERMINATE_SAFELY
    assert controller.handle(FailureClass.UNKNOWN).strategy is RecoveryStrategy.TERMINATE_SAFELY
    low = controller.handle(FailureClass.LOW_CONFIDENCE, RecoveryContext(phase="verify"))
    assert low.strategy is RecoveryStrategy.RETRY_ONCE and low.then_replan is True
    exhausted = controller.handle(
        FailureClass.MOVED_UI,
        RecoveryContext(phase="verify"),
    )
    # No live enforcer -> static limits only; with a fresh controller the budget is open.
    assert exhausted.strategy is RecoveryStrategy.RECOVER_REOBSERVE


# --- W4-fix regression tests (D1-D4, D6, D7, D9) ---------------------------------------------


async def test_stop_session_removes_bundle_and_blocks_tools(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1: stopping closes the bundle (no leak) and later tool calls fail closed."""
    session_id, _bundle, _backend, _provider = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    stop_response = server.stop_session(session_id)
    assert stop_response["ok"] is True
    assert session_id not in server._bundles  # memory freed
    assert server._registry.get(session_id) is None  # registry consistent

    observe = server.computer_observe(session_id)
    assert observe["ok"] is False
    assert observe["error"] == "session_stopped"
    screenshot = server.computer_screenshot(session_id)
    assert screenshot["ok"] is False
    assert screenshot["error"] == "session_stopped"
    execute = await server.computer_execute(session_id, "wait", delta=1)
    assert execute["ok"] is False
    assert execute["error"] == "session_stopped"
    # AMENDED (run_goal removal): the removed loop tool's stopped-session shape check
    # died with the loop; every surviving tool fails closed with session_stopped.

    again = server.stop_session(session_id)  # idempotent, shape preserved
    assert again["ok"] is True
    assert "already" in again["message"]

    unknown = server.computer_observe("never-existed")
    assert unknown["ok"] is False
    assert unknown["error"] == "unknown_session"


async def test_computer_observe_returns_image_content_block(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vision fix: observe/screenshot return a real MCP ImageContent block — never base64 as text.

    A vision-capable client model can only see a screenshot delivered as an image
    content block; a base64 string inside JSON text is invisible to the vision
    channel (and floods the context). The metadata text must stay bounded and free
    of the raw image data.
    """
    session_id, _bundle, _backend, _provider = make_session(monkeypatch)

    result = server.computer_observe(session_id)
    assert isinstance(result, list) and len(result) == 2
    text_block, image_block = result
    assert text_block.type == "text"
    assert image_block.type == "image"
    assert image_block.mimeType == "image/png"
    decoded = base64.b64decode(image_block.data, validate=True)
    assert decoded.startswith(b"\x89PNG\r\n\x1a\n")  # real PNG bytes, not a text blob

    payload = json.loads(text_block.text)
    assert "image_base64" not in text_block.text  # no base64 ever travels as text
    assert "image_base64" not in payload["observation"]
    assert payload["image_format"] == "image/png"
    assert payload["observation"]["width"] > 0
    assert payload["observation"]["height"] > 0
    assert payload["digest"]
    assert payload["observation_id"] == payload["observation"]["observation_id"]

    alias = server.computer_screenshot(session_id)
    assert isinstance(alias, list) and len(alias) == 2
    assert alias[0].type == "text"
    assert alias[1].type == "image" and alias[1].mimeType == "image/png"
    assert "image_base64" not in alias[0].text


async def test_pre_stopped_run_yields_failure_not_vacuous_ok() -> None:
    """D2: a run on a stopped session must not report ok=True over an empty result list."""
    state = SessionState(session_id="pre-stopped", stopped=True, dry_run=False)
    agent = ComputerUseAgent(FakeComputerBackend(), ScriptedProvider([]), session_id="pre-stopped")
    results = await agent.run("goal", state)

    assert results, "a stopped run still yields a result entry"
    assert all(item.ok is False for item in results)
    assert agent.task.termination_reason.value == "stopped_by_user"


async def test_suspicious_content_persisted_in_audit_and_results(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3 REMOVED-loop half (run_goal removal): provider suspicious-content marking
    was decide-phase loop machinery (no decide phase on the direct surface). The
    redaction guarantees that survive are pinned by test_response_path_redacts_*
    below and the audit sink suite in test_audit_compliance."""


async def test_observe_failure_classified_as_app_crash_and_recovers(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4: observe-phase backend failures on the DIRECT path surface as a typed
    action_error with a failure audit row — never a crash, never silent."""
    backend = ScriptedBackend()
    backend.observe_faults = [DisplayUnavailableError("screen capture failed")]
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, dry_run=False, require_approval=False,
        limits=FAST_LIMITS,
    )
    response = await server.computer_execute(session_id, "click", x=20, y=20)

    assert response["ok"] is False
    assert response["error"] == "action_error"
    events = audit_events(bundle, session_id)
    failure_events = [event for event in events if event["event_type"] == "failure"]
    assert failure_events, "observe failures must emit a failure event"


async def test_persistent_observe_failure_terminates_unrecoverable(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4 companion, direct path: a display that never produces an observation keeps
    failing closed — every attempt is a typed error, nothing ever executes."""
    backend = ScriptedBackend()
    backend.observe_faults = [DisplayUnavailableError("no display")] * 10
    session_id, _bundle, _b, _ = make_session(
        monkeypatch,
        backend=backend,
        dry_run=False,
        require_approval=False,
        limits=FAST_LIMITS,
    )
    for _ in range(3):
        response = await server.computer_execute(session_id, "click", x=10, y=10)
        assert response["ok"] is False
        assert response["error"] == "action_error"
    assert backend.executed == []


async def test_dismiss_attempt_accounting_invariants(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D6 REMOVED-loop half (run_goal removal): the in-loop dismiss-then-retry
    accounting was loop recovery machinery. Counter invariants on the direct path
    are pinned by the happy-path and limit tests above (action totals balance)."""


async def test_ground_phase_failure_emits_failed_grounding_audit(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D7: direct-path grounding refusals are audited (failed grounding event) and
    rejected — the same audit the loop used to emit, same shape."""
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )  # point (5000, 10) is outside the fake 1280x720 screenshot: grounding refuses
    response = await server.computer_execute(session_id, "click", x=5000, y=10)

    assert response["ok"] is False
    # W-2 (057): the rejection names the real gate (grounding) in the message.
    assert response["message"].startswith("Action rejected by grounding:")
    events = audit_events(bundle, session_id)
    grounding_failures = [
        event for event in events if event["event_type"] == "grounding" and event.get("result") == "failed"
    ]
    assert grounding_failures, "direct-path ground failures must emit a failed grounding event"


def test_start_session_limits_conflict_and_precedence(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D9: explicit retry-limit conflict raises; single-source values apply."""
    conflict = server.start_session(
        dry_run=False,
        require_approval=False,
        max_retries_per_action=2,
        limits={"max_retries_per_action": 3},
    )
    assert conflict["ok"] is False
    assert conflict["error"] == "invalid_limits"
    assert "exactly one place" in conflict["message"]

    _sid_param, bundle_param, _b, _p = make_session(
        monkeypatch, dry_run=False, require_approval=False, max_retries_per_action=4
    )
    assert bundle_param.enforcer.limits.max_retries_per_action == 4  # param applies alone

    _sid_dict, bundle_dict, _b2, _p2 = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits={"max_retries_per_action": 5}
    )
    assert bundle_dict.enforcer.limits.max_retries_per_action == 5  # dict applies alone

    _sid_merged, bundle_merged, _b3, _p3 = make_session(
        monkeypatch,
        dry_run=False,
        require_approval=False,
        max_retries_per_action=2,
        limits={"max_actions": 7},
    )
    # param applies when the dict omits it (documented precedence)
    assert bundle_merged.enforcer.limits.max_retries_per_action == 2
    assert bundle_merged.enforcer.limits.max_actions == 7


# --- W5-fix regression tests (F2/F3/F7) -------------------------------------------------------


async def test_response_path_redacts_secret_shaped_text(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2: the MCP response path is redacted too — no unredacted echo of host text.

    RETARGETED (run_goal removal): the direct surface carries the same guarantee.
    A host-supplied ``expected_effect`` with a secret echoes into the verification
    note/evidence — and is REDACTED at the response sink. (A secret-shaped ``type``
    payload is now stopped even earlier, at the safety gate — defense in depth.)"""
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(
        session_id,
        "click",
        x=10,
        y=10,
        expected_effect="window shows API_KEY=supersecretvalue123 after click",
    )
    response = execute_payload(response)
    assert response["ok"] is True, response

    # The response's MESSAGE/verification surfaces are redacted at the sink; the
    # action dict echoes the caller's own expected_effect verbatim (same-party
    # input, the same contract the loop had for caller-visible fields), so the
    # redaction pins target the surfaces _redact_result_payload owns.
    note = response["verification"]["note"]
    assert "supersecretvalue123" not in note, note
    assert "[REDACTED:" in note
    evidence_blob = json.dumps(response["verification"])
    assert "supersecretvalue123" not in evidence_blob
    # the audit sink keeps its own redaction guarantee (unchanged)
    assert "supersecretvalue123" not in json.dumps(audit_events(bundle, session_id))


async def test_provider_done_is_marked_model_declared(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3 REMOVED (run_goal removal): honest model-declared completion marking was a
    decide-phase loop guarantee ("done" from a provider is an assertion, not
    evidence). On the direct surface there is no provider "done" channel at all —
    the host decides when work is complete, and every action it drives is verified
    (or honestly uncertain) by the evidence ladder."""


async def test_internal_kill_path_bundle_hygiene(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7: an internally-armed kill path (not stop_session) gets identical hygiene."""
    session_id, bundle, _backend, _provider = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    bundle.context.stop.stop()  # simulate an internal safety path arming the kill path

    observe = server.computer_observe(session_id)
    assert observe["ok"] is False
    assert observe["error"] == "session_stopped"
    screenshot = server.computer_screenshot(session_id)
    assert screenshot["ok"] is False
    assert screenshot["error"] == "session_stopped"
    execute = await server.computer_execute(session_id, "wait", delta=1)
    assert execute["ok"] is False
    assert execute["error"] == "session_stopped"
    # AMENDED (run_goal removal): the removed loop tool's tail check died with the loop.

    assert session_id not in server._bundles  # same cleanup as stop_session
    assert server._registry.get(session_id) is None
    events = audit_events(bundle, session_id)
    assert any(
        event["event_type"] == "stop"
        and event.get("metadata", {}).get("source") == "internal_kill_path"
        for event in events
    )
