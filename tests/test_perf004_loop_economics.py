"""PERF-004 loop-economics tests (A4): the 9 mission changes, mocked end-to-end.

Covers (each maps to an entry in ``evidence/perf-004/p2/change-log.md``):

- C1 observe reuse: the post-action capture becomes the next loop_top observation
  (capture count drops), digest-first staleness auditing, validator guarantees intact
  (STALE recovery itself stays covered by test_controller_integration).
- C2 rate-gate semantics: the interval gate consults FRESH observations only; intra-step
  verification captures are burst-exempt but still recorded (session-wide boundedness).
- C3 verification ladder: deterministic tiers -> pixel diff -> provider judge LAST;
  a deterministic verdict skips the judge entirely.
- C4 host-payload opt-out: ``include_screenshot_after=False`` omits the heavy image;
  omitted (legacy callers) keeps the payload unchanged.
- C5 dry-run default flip + unmistakable banner.
- C6 teach-in-text: invalid_action rejections carry the exact vocabulary + closest shape.
- C7 queued ``follow_ups``: full pipeline per item, zero bypass, adversarial stops
  (unsafe item, verification failure, digest surprise, approval, stop token, cap).
- C8 structured observation ``text_summary``.

Everything runs on FakeComputerBackend derivatives (no real input dispatch).
"""

from __future__ import annotations

import base64
import inspect
import io
import json
from typing import Any

import pytest
from PIL import Image
from test_controller_integration import (
    FAST_LIMITS,
    ScriptedBackend,
    ScriptedProvider,
    _png,
    audit_events,
    executed_summary,
    make_session,
)
from test_controller_integration import (
    click as make_click,
)

from computer_use_mcp import server
from computer_use_mcp.models import (
    MAX_FOLLOW_UPS,
    ActionSpec,
    AgentDecision,
    GroundedAction,
    WindowInfo,
)
from computer_use_mcp.observation import observation_text_summary
from computer_use_mcp.state import SessionRegistry
from computer_use_mcp.verification import VerificationIntent, VerificationKind


def done() -> AgentDecision:
    return AgentDecision(status="done", summary="done")


def with_hint(decision: AgentDecision, hint: str) -> Any:
    """Provider envelope carrying a verification_hint (pinned E4 decide_full shape)."""
    from types import SimpleNamespace

    return SimpleNamespace(
        decision=decision,
        verification_hint=hint,
        expected_effect=None,
        suspicious_content=None,
        redactions_applied=[],
    )


def focus_window(target: str, hint: str | None = None) -> Any:
    decision = AgentDecision(
        status="action",
        action=GroundedAction(
            action="focus_window",
            target=target,
            confidence=1.0,
            expected_effect=f"switch to {target}",
        ),
    )
    return with_hint(decision, hint) if hint is not None else decision


class JudgingProvider(ScriptedProvider):
    """ScriptedProvider whose judge_change returns a scripted (counted) verdict."""

    def __init__(self, script: Any = None, *, verdict: str = "verified", **kwargs: Any) -> None:
        super().__init__(script, **kwargs)
        self.verdict = verdict

    def judge_change(
        self, before_b64: str, after_b64: str, expected_effect: str, goal: str | None = None
    ) -> dict[str, Any]:
        self.judge_calls += 1
        return {"outcome": self.verdict, "confidence": 0.8, "reason": "judged"}


class ShiftingScreenBackend(ScriptedBackend):
    """Every observe() call returns a DIFFERENT image (async screen-churn simulator)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._observe_count = 0

    def observe(self) -> Any:
        self._observe_count += 1
        observation = super().observe()
        image = Image.new("RGB", (64, 48), ("white", "red", "blue")[self._observe_count % 3])
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        observation.image_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
        return observation


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """Fresh bounded registry/bundles + per-test audit dir (full session isolation)."""
    monkeypatch.setenv("COMPUTER_USE_MCP_LOG_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(server, "_registry", SessionRegistry(max_sessions=8))
    monkeypatch.setattr(server, "_bundles", {})
    monkeypatch.setattr(server, "_stopped_sessions", {})
    return server


def _observe_metadata(session_id: str) -> dict[str, Any]:
    response = server.computer_observe(session_id)
    return json.loads(response[0].text)


# --- C1: observe reuse + digest-first staleness -------------------------------------------------


async def test_run_loop_reuses_post_action_capture_as_next_loop_top(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([make_click(10, 10), make_click(20, 20), done()])
    session_id, bundle, backend, _ = make_session(
        monkeypatch, provider=provider, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.run_goal(session_id, "two clicks")

    assert response["termination_reason"] == "completed"
    assert len(executed_summary(backend)) == 2
    # Step 1: loop_top + validate probe + post_action = 3 captures.
    # Step 2: loop_top REUSED + validate probe + post_action = 2 captures.
    # Step 3: loop_top reused, decide -> done. Total = 5 (was 7 before PERF-004).
    counters = bundle.metrics.snapshot()["counters"]
    assert counters["screenshot_count"] == 5
    assert counters["observation_reuse"] == 2
    events = audit_events(bundle, session_id)
    reused = [
        event
        for event in events
        if event["event_type"] == "observation" and event.get("metadata", {}).get("reused")
    ]
    assert reused  # the reuse is audited, never silent


async def test_validation_audits_digest_staleness_proof(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([make_click(10, 10), done()])
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    await server.run_goal(session_id, "one click")
    events = audit_events(bundle, session_id)
    validations = [event for event in events if event["event_type"] == "validation"]
    assert validations
    proofs = {event["metadata"]["staleness_proof"] for event in validations}
    assert proofs <= {"digest_match", "digest_mismatch"}
    # Step 2 validates against the REUSED post-action capture; the flip backend's
    # validate probe is pixel-identical to it -> the digest PROVES screen identity.
    assert "digest_match" in proofs


async def test_run_single_audits_staleness_proof(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(session_id, "click", x=15, y=15)
    assert response["ok"] is True
    bundle = server._get_bundle(session_id)
    events = audit_events(bundle, session_id)
    validations = [event for event in events if event["event_type"] == "validation"]
    assert any("staleness_proof" in event["metadata"] for event in validations)
    assert backend.executed


# --- C2: rate-gate semantics ---------------------------------------------------------------------


async def test_gate_consults_only_fresh_observations(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([make_click(10, 10), make_click(20, 20), done()])
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, dry_run=False, require_approval=False,
        limits={"min_screenshot_interval_ms": 250},
    )
    enforcer = bundle.enforcer
    consultations = {"count": 0}
    original = enforcer.can_screenshot

    def counting() -> bool:
        consultations["count"] += 1
        return original()

    monkeypatch.setattr(enforcer, "can_screenshot", counting)
    response = await server.run_goal(session_id, "gate policy")

    assert response["termination_reason"] == "completed"
    # FRESH observations only: step 1's loop_top. Steps 2-3 loop_top are REUSED and the
    # intra-step validate/post_action probes are burst-exempt -> never consulted.
    assert consultations["count"] == 1


async def test_burst_captures_still_recorded_and_paced(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([make_click(10, 10), make_click(20, 20), done()])
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, dry_run=False, require_approval=False,
        limits={"min_screenshot_interval_ms": 250},
    )
    await server.run_goal(session_id, "burst accounting")
    snapshot = bundle.enforcer.snapshot()
    assert snapshot["screenshots"] == 5  # 1 gated loop_top + 4 intra-step captures
    assert snapshot["burst_screenshots"] == 4  # validate probe + post_action per step
    # Pacing truth: burst captures refresh the timestamp, so a FRESH capture right
    # after would be gated (session-wide protection stays enforced and bounded).
    assert bundle.enforcer.can_screenshot() is False


# --- C3: verification ladder ----------------------------------------------------------------------


async def test_deterministic_tier_skips_model_judge(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend(
        active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="App Window")
    )
    provider = JudgingProvider(
        [focus_window("App", hint="model_judge"), done()], verdict="verified"
    )
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, provider=provider, dry_run=False,
        require_approval=False, limits=FAST_LIMITS,
    )
    response = await server.run_goal(session_id, "focus deterministically")
    assert response["termination_reason"] == "completed"
    assert provider.judge_calls == 0  # the deterministic tier decided; judge skipped
    events = audit_events(bundle, session_id)
    verification = [event for event in events if event["event_type"] == "verification"]
    assert any(
        event["metadata"].get("ladder_tier") == "deterministic" and event["result"] == "verified"
        for event in verification
    )


async def test_pixel_diff_tier_fails_judge_intent_before_judge(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend(flip=False)  # identical pixels
    provider = JudgingProvider([], verdict="verified")
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, provider=provider, dry_run=False,
        require_approval=False, limits=FAST_LIMITS,
    )
    agent = server._get_bundle(session_id).agent
    before = backend.observe()
    after = backend.observe()
    intent = VerificationIntent(
        kind=VerificationKind.MODEL_JUDGE,
        expected_change=True,
        expected_effect="the screen must change",
    )
    result = await agent._verify(intent, before, after)
    assert result.outcome == "failed"  # the diff tier falsified the stated expectation...
    assert provider.judge_calls == 0  # ...so the judge never ran


async def test_judge_tier_runs_when_cheap_tiers_uncertain(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = JudgingProvider(
        [with_hint(make_click(10, 10, expected_change="changes"), "model_judge"), done()],
        verdict="verified",
    )
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.run_goal(session_id, "judge when cheap tiers abstain")
    assert response["termination_reason"] == "completed"
    assert provider.judge_calls == 1  # no deterministic criteria, pixels differ -> judge
    events = audit_events(bundle, session_id)
    verification = [event for event in events if event["event_type"] == "verification"]
    assert any(
        event["metadata"].get("ladder_tier") == "model_judge" and event["result"] == "verified"
        for event in verification
    )


# --- C4: host-payload opt-out ---------------------------------------------------------------------


async def test_include_screenshot_after_false_omits_image_keeps_legacy_default(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    legacy = await server.computer_execute(session_id, "click", x=15, y=15)
    assert legacy["ok"] is True
    assert isinstance(legacy.get("screenshot_after_base64"), str)  # legacy payload intact
    trimmed = await server.computer_execute(
        session_id, "click", x=25, y=25, include_screenshot_after=False
    )
    assert trimmed["ok"] is True
    assert "screenshot_after_base64" not in trimmed  # omitted entirely
    # Everything else survives the opt-out.
    assert trimmed["verification"]["outcome"] in {"verified", "uncertain", "failed"}
    assert len(executed_summary(backend)) == 2


def test_computer_execute_signature_additions_are_trailing_optional() -> None:
    parameters = list(inspect.signature(server.computer_execute).parameters.values())
    assert [p.name for p in parameters][-2:] == ["include_screenshot_after", "follow_ups"]
    assert parameters[-2].default is None
    assert parameters[-1].default is None


# --- C5: dry-run default flip + unmistakable banner ------------------------------------------------


def test_start_session_defaults_to_live_with_approval(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "_backend_factory", ScriptedBackend)
    monkeypatch.setattr(server, "_provider_factory", ScriptedProvider)
    response = server.start_session()
    assert response.get("session_id"), response
    assert response["dry_run"] is False  # the sanctioned PERF-004 C5 default flip
    assert response["require_approval"] is True  # untouched


async def test_dry_run_results_carry_unmistakable_banner(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([make_click(10, 10), done()])
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, provider=provider, dry_run=True, require_approval=False, limits=FAST_LIMITS
    )
    execute_response = await server.computer_execute(session_id, "wait", delta=1)
    assert execute_response["message"].startswith("DRY-RUN (no input dispatched):")
    goal_response = await server.run_goal(session_id, "banner check")
    results = goal_response["results"]
    assert results
    # The dry-run ACTION stub carries the banner (the provider "done" result is not a
    # dry-run result and keeps its legacy message).
    assert results[0]["message"].startswith("DRY-RUN (no input dispatched):")
    assert backend.executed == []  # still a no-op


# --- C6: teach-in-text -----------------------------------------------------------------------------


async def test_invalid_action_key_teaches_keypress(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    response = await server.computer_execute(session_id, "key", keys=["a"])
    assert response["ok"] is False
    assert response["error"] == "invalid_action"
    assert "keypress" in response["message"]
    assert "Valid actions" in response["message"]
    assert any("keypress" in hint for hint in response["reasons"])


async def test_invalid_action_triple_click_teaches_alternative(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    response = await server.computer_execute(session_id, "triple_click", x=5, y=5)
    assert response["error"] == "invalid_action"
    assert "double_click" in response["message"]
    assert any("triple_click" in hint for hint in response["reasons"])


async def test_hotkey_word_payload_teaches_key_names(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    # The Session-1 failure mode: a WORD passed as a hotkey payload (1-key hotkey).
    response = await server.computer_execute(session_id, "hotkey", keys=["hello"])
    assert response["ok"] is False
    assert response["error"] == "invalid_action"
    assert "keypress" in response["message"]  # the closest valid shape is taught
    assert any("KEY NAMES" in hint for hint in response["reasons"])
    assert server.ACTION_VOCABULARY.startswith("Valid actions")


# --- C7: queued follow_ups (full pipeline per item, zero bypass) ------------------------------------


async def test_follow_ups_run_full_pipeline_and_verify_each_item(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(
        session_id,
        "click",
        x=10,
        y=10,
        expected_effect="the click changes the screen",
        follow_ups=[
            {"action": "click", "x": 20, "y": 20, "expected_effect": "changes again"},
            {"action": "wait", "delta": 1},
        ],
    )
    assert response["ok"] is True
    assert response["follow_ups_stopped_reason"] is None
    assert len(response["follow_up_results"]) == 3
    assert all(item["ok"] for item in response["follow_up_results"])
    assert len(executed_summary(backend)) == 3
    events = audit_events(bundle, session_id)
    assert len([e for e in events if e["event_type"] == "execution"]) == 3
    assert len([e for e in events if e["event_type"] == "verification"]) == 3
    queue_events = [e for e in events if e["event_type"] == "queue"]
    assert queue_events and queue_events[-1]["result"] == "completed"


async def test_unsafe_follow_up_rejected_individually_and_stops_queue(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(
        session_id,
        "click",
        x=10,
        y=10,
        follow_ups=[
            {"action": "type", "text": "open powershell now", "expected_effect": "changes"},
            {"action": "click", "x": 30, "y": 30},
        ],
    )
    assert response["ok"] is True  # the FIRST action is the legacy payload and succeeded
    assert response["follow_ups_stopped_reason"] == "safety_denied"
    unsafe_entry = response["follow_up_results"][1]
    assert unsafe_entry["kind"] == "safety_denied" and unsafe_entry["ok"] is False
    assert len(executed_summary(backend)) == 1  # the unsafe item AND item 3 never ran


class FlipOnceBackend(ScriptedBackend):
    """The FIRST execute changes the screen (white -> black); later ones change nothing."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(flip=False, **kwargs)

    def observe(self) -> Any:
        observation = super().observe()
        if self.executes >= 1:
            observation.image_base64 = _png("black")  # settled after the first action
        return observation


async def test_verification_failure_stops_queue_before_later_items(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FlipOnceBackend()
    provider = ScriptedProvider([])
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, provider=provider, dry_run=False,
        require_approval=False, limits=FAST_LIMITS,
    )
    response = await server.computer_execute(
        session_id,
        "click",
        x=10,
        y=10,
        follow_ups=[
            {"action": "click", "x": 20, "y": 20, "expected_effect": "screen must change"},
            {"action": "click", "x": 30, "y": 30},
        ],
    )
    assert response["follow_ups_stopped_reason"] == "verification_failed"
    assert response["follow_up_results"][1]["verification_outcome"] == "failed"
    assert len(executed_summary(backend)) == 2  # items 1-2 ran; item 3 was flushed


async def test_post_action_digest_surprise_stops_queue(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ShiftingScreenBackend()
    provider = ScriptedProvider([])
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, provider=provider, dry_run=False,
        require_approval=False, limits=FAST_LIMITS,
    )
    response = await server.computer_execute(
        session_id,
        "click",
        x=10,
        y=10,
        follow_ups=[{"action": "click", "x": 20, "y": 20}],
    )
    # Item 2's staleness probe no longer matches its premise (every capture differs) ->
    # DIGEST SURPRISE: the speculative item is never executed against an unseen screen.
    assert response["follow_ups_stopped_reason"] == "digest_surprise"
    assert response["follow_up_results"][1]["kind"] == "digest_surprise"
    assert len(executed_summary(backend)) == 1


async def test_approval_required_mid_queue_stops_queue(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend(
        active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="App")
    )
    provider = ScriptedProvider([])
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, provider=provider, dry_run=False,
        require_approval=False, limits=FAST_LIMITS,
    )
    response = await server.computer_execute(
        session_id,
        "click",
        x=10,
        y=10,
        follow_ups=[
            {"action": "type", "text": "install the printer driver", "expected_effect": "changes"},
            {"action": "click", "x": 30, "y": 30},
        ],
    )
    assert response["follow_ups_stopped_reason"] == "approval_required"
    entry = response["follow_up_results"][1]
    assert entry["kind"] == "approval_required" and entry["requires_approval"] is True
    assert len(executed_summary(backend)) == 1  # nothing after the approval gate ran


async def test_stop_session_halts_a_running_queue(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SAFETY MANDATE (C7): the user kill path halts a queue between items.

    The stop token is armed right after item 1's post-action capture (simulating
    stop_session arriving mid-queue); item 2 must never start.
    """

    class StopAfterFirstActionBackend(ScriptedBackend):
        def __init__(self, token: Any, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._token = token

        def observe(self) -> Any:
            observation = super().observe()
            if self.executes >= 1:  # armed AFTER item 1's post-action capture
                self._token.stop()
            return observation

    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    stopping_backend = StopAfterFirstActionBackend(bundle.context.stop)
    bundle.backend = stopping_backend
    bundle.agent.backend = stopping_backend
    bundle.agent.observation.backend = stopping_backend  # the engine holds its own ref
    response = await server.computer_execute(
        session_id,
        "click",
        x=10,
        y=10,
        follow_ups=[{"action": "click", "x": 20, "y": 20}],
    )
    assert response["ok"] is False
    assert response.get("stopped") is True
    assert len(executed_summary(stopping_backend)) == 1  # item 1, then the kill path fired
    events = [e for e in audit_events(bundle, session_id) if e["event_type"] == "execution"]
    assert len(events) == 1  # item 2 never started


async def test_follow_ups_cap_and_malformed_items_fail_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    too_many = [{"action": "wait", "delta": 1} for _ in range(MAX_FOLLOW_UPS + 1)]
    response = await server.computer_execute(session_id, "click", x=10, y=10, follow_ups=too_many)
    assert response["ok"] is False
    assert response["error"] == "invalid_action"
    assert "5" in response["message"]
    malformed = await server.computer_execute(
        session_id, "click", x=10, y=10, follow_ups=[{"not_an_action": True}]
    )
    assert malformed["error"] == "invalid_action"
    non_list = await server.computer_execute(
        session_id, "click", x=10, y=10, follow_ups="click x=1"
    )
    assert non_list["error"] == "invalid_action"
    assert backend.executed == []  # fail-closed BEFORE anything was dispatched


def test_action_spec_round_trips_to_grounded_action() -> None:
    spec = ActionSpec.model_validate(
        {"action": "drag", "x": 1, "y": 2, "x2": 3, "y2": 4, "expected_effect": "moves"}
    )
    grounded = spec.to_grounded()
    assert grounded.action.value == "drag"
    assert (grounded.point.x, grounded.point.y) == (1, 2)
    assert (grounded.to_point.x, grounded.to_point.y) == (3, 4)
    assert grounded.expected_effect == "moves"
    assert grounded.confidence == 1.0


# --- C8: structured observation summary --------------------------------------------------------------


def test_text_summary_first_and_changed_note(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, _backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    first = _observe_metadata(session_id)
    assert "window:" in first["text_summary"]
    assert "first observation" in first["text_summary"]
    second = _observe_metadata(session_id)
    # The fake backend renders identical screenshots -> unchanged since previous.
    assert "unchanged since previous" in second["text_summary"]


def test_text_summary_changed_note_on_screen_change(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, backend, _ = make_session(monkeypatch, limits=FAST_LIMITS)
    first = _observe_metadata(session_id)
    assert "first observation" in first["text_summary"]
    backend.executes = 1  # the ScriptedBackend flip makes the next screenshot black
    second = _observe_metadata(session_id)
    assert "changed since previous" in second["text_summary"]


def test_text_summary_control_hint_and_graceful_omission() -> None:
    from computer_use_mcp.models import Observation

    base = {"image_base64": _png(), "width": 64, "height": 48}
    with_hint = Observation(
        **base, ui_elements=[{"name": "OK", "control_type": "button", "focused": True}]
    )
    summary = observation_text_summary(with_hint, previous_digest=None)
    assert "focused: button 'OK'" in summary
    without_hint = Observation(**base, ui_elements=None)
    assert "focused:" not in observation_text_summary(without_hint, previous_digest=None)
    # Window title/process and cursor render when present.
    windowed = Observation(
        **base,
        active_window_info=WindowInfo(hwnd=1, pid=2, process_name="notepad.exe", title="Untitled"),
        cursor_x=12,
        cursor_y=34,
    )
    summary = observation_text_summary(windowed)
    assert "process=notepad.exe" in summary
    assert "title='Untitled'" in summary
    assert "cursor: (12, 34)" in summary


# --- T8 anomaly-B1 regression: verification never depends on an omitted screenshot ------------
# The measurement bridge false-failed batches that used include_screenshot_after=false
# (a 0.0-diff on a starved ladder). Two guarantees are pinned here: (1) the response
# screenshot opt-out never feeds verification (the pipeline always captures its own
# fresh post-action observation), and (2) a starved ladder (the after-capture IS the
# grounding capture) routes past the pixel-diff tier instead of false-failing on a
# self-comparison 0.0 diff.


async def test_screenshot_opt_out_keeps_verification_fresh_and_diff_verified(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider(
        [
            AgentDecision(
                status="action",
                action=GroundedAction(action="click", point={"x": 25, "y": 25}, confidence=1.0),
            ),
            AgentDecision(status="done", summary="done"),
        ]
    )
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(
        session_id, "click", x=25, y=25, include_screenshot_after=False
    )
    # The opt-out strips the payload from the RESPONSE...
    assert "screenshot_after_base64" not in response
    # ...but verification ran the diff ladder on the internal fresh capture.
    assert response["verification"]["outcome"] == "verified"
    assert response["verification"]["verification_method"] == "screenshot_diff"


async def test_starved_after_capture_routes_past_the_diff_tier(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A judge intent with a starved (self-comparison) ladder is NOT false-failed at 0.0 diff."""

    class FrozenPostActionBackend(ScriptedBackend):
        """Returns the SAME capture object for the post-action observe (starved ladder)."""

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._starved = False
            self._frozen: Any = None

        def execute(self, action: Any, stop: Any = None, **kwargs: Any) -> str:
            self._starved = True  # from now on, the post-action capture is frozen
            return super().execute(action, stop)

        def observe(self) -> Any:
            if self._starved and self._frozen is not None:
                return self._frozen  # the SAME observation object as the last capture
            observation = super().observe()
            if self._starved:
                self._frozen = observation
            return observation

    provider = JudgingProvider(
        [
            # AgentDecision drops unknown kwargs (pydantic ignore): the hint must ride
            # the pinned E4 envelope (SimpleNamespace) like the other ladder tests.
            with_hint(
                AgentDecision(
                    status="action",
                    action=GroundedAction(action="click", point={"x": 25, "y": 25}, confidence=1.0),
                ),
                "model_judge",
            ),
            AgentDecision(status="done", summary="done"),
        ],
        verdict="verified",
    )
    session_id, _bundle, backend, provider_holder = make_session(
        monkeypatch, backend=FrozenPostActionBackend(), provider=provider,
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    _ = provider_holder
    response = await server.run_goal(session_id, "click with a judge")
    assert response["termination_reason"] == "completed"
    first = response["results"][0]
    # The judge tier decided (the diff tier was routed past on the self-comparison)...
    assert provider.judge_calls == 1
    # ...and the outcome is the judge's verdict — never a 0.0-diff false failure.
    assert first["verification"]["outcome"] == "verified"
    _ = backend
