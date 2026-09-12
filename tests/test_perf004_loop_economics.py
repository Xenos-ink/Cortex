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
  (unsafe item, digest surprise, approval, stop token, cap). W-2/057 update: an
  EXECUTED item's verification "failed" no longer stops the batch by default (the
  strict ``CORTEX_QUEUE_STRICT_VERIFY=1`` stop is still pinned here); a dispatch
  error still does.
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
    execute_payload,
    executed_summary,
    make_session,
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
    """C1 observe reuse SURVIVES the run_goal removal on the direct surface: a queued
    action's grounding reuses the previous item's post-action capture (no new fresh
    capture), and the reuse is audited, never silent.

    RETARGETED (run_goal removal): the loop's loop_top reuse died with the loop; the
    SAME reuse mechanism lives on in the follow_ups queue (agent._run_action_queue
    passes each post-action capture as the next item's source_observation)."""
    session_id, bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(
        session_id, "click", x=10, y=10, follow_ups=[{"action": "click", "x": 20, "y": 20}]
    )
    payload = execute_payload(response)
    assert payload["ok"] is True, payload
    assert len(executed_summary(backend)) == 2
    # Queued pipeline: primary direct_request + validate + post_action, then the
    # queued item's grounding REUSES that post-action capture (no new fresh capture):
    # reuse events are audited, never silent.
    events = audit_events(bundle, session_id)
    reused = [
        event
        for event in events
        if event["event_type"] == "observation" and event.get("metadata", {}).get("reused")
    ]
    assert reused, "the queue's observe reuse must be audited"
    counters = bundle.metrics.snapshot()["counters"]
    assert counters["observation_reuse"] >= 1


async def test_validation_audits_digest_staleness_proof(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every direct-action validation audits its staleness proof (digest match).

    RETARGETED (run_goal removal): the direct path drives the same digest-first
    staleness proof the loop used to exercise."""
    session_id, bundle, _backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(session_id, "click", x=15, y=15)
    response = execute_payload(response)
    assert response["ok"] is True
    events = audit_events(bundle, session_id)
    validations = [event for event in events if event["event_type"] == "validation"]
    assert validations
    proofs = {event["metadata"]["staleness_proof"] for event in validations}
    assert proofs <= {"digest_match", "digest_mismatch"}
    assert "digest_match" in proofs


async def test_run_single_audits_staleness_proof(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    response = await server.computer_execute(session_id, "click", x=15, y=15)
    response = execute_payload(response)
    assert response["ok"] is True
    bundle = server._get_bundle(session_id)
    events = audit_events(bundle, session_id)
    validations = [event for event in events if event["event_type"] == "validation"]
    assert any("staleness_proof" in event["metadata"] for event in validations)
    assert backend.executed


# --- C2: rate-gate semantics ---------------------------------------------------------------------
# REMOVED (run_goal removal): the two loop-driven C2 pins (gate-consults-only-fresh-
# observations, burst-captures-paced-through-the-loop) exercised the internal loop's
# loop_top capture cadence and died with the loop. The SURVIVING gate semantics on the
# direct surface are pinned by test_p5_redteam.rt3 (fresh-gate fail-closed + pacing)
# and test_controller_integration.test_screenshot_rate_limit_trips_cleanly (the
# host-driven observe path trips typed). Burst accounting on the direct path is
# pinned by test_p5_redteam.test_rt3_direct_execute_path_captures_are_burst_bounded.


# --- C3: verification ladder ----------------------------------------------------------------------


async def test_deterministic_tier_skips_model_judge(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C3 on the direct surface: a deterministic-verifiable action NEVER reaches the
    model judge.

    RETARGETED (run_goal removal): the direct path has no provider hint channel
    (verification_hint was a loop-only envelope field), so the model-judge LADDER
    itself is pinned below through the agent seam — same method, same tiers — and
    this test pins the SURVIVING equivalent: a deterministic-verifiable action
    (focus_window -> window_state) resolves verified with the judge never invoked."""
    backend = ScriptedBackend(
        active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="App Window")
    )
    provider = JudgingProvider([], verdict="verified")
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=backend, provider=provider, dry_run=False,
        require_approval=False, limits=FAST_LIMITS,
    )
    response = await server.computer_execute(session_id, "focus_window", target="App")
    response = execute_payload(response)
    assert response["ok"] is True, response
    assert response["verification"]["outcome"] == "verified"
    assert response["verification"]["verification_method"] == "window_state"
    assert provider.judge_calls == 0  # the deterministic tier decided; judge skipped


async def test_ladder_deterministic_tier_returns_before_the_judge(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C3 LADDER pin (agent seam, loop-free): a model-judge intent whose deterministic
    criteria already reach a verdict resolves at tier "deterministic" — the judge is
    never consulted.

    RETARGETED (run_goal removal): this is the old in-loop pin moved to the agent
    seam (``_verify_judge_ladder``), the exact method the loop used to call."""
    from computer_use_mcp.verification import VerificationIntent, VerificationKind

    backend = ScriptedBackend(
        active_window=WindowInfo(hwnd=1, pid=10, process_name="app.exe", title="App Window")
    )
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
        expected_window_title="App",
    )
    result, tier = await agent._verify_judge_ladder(intent, before, after)
    assert result.outcome == "verified"
    assert tier == "deterministic"
    assert provider.judge_calls == 0


async def test_zero_diff_stated_effect_defers_to_judge_bare_change_fails_before_judge(
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
    # RC-D11 (058): a STATED effect is no longer falsified by a zero diff — the
    # pixel tier defers (uncertain, absent pixels are not proof of absence) and the
    # judge adjudicates the defer. The pre-judge falsification contract survives for
    # a BARE change expectation (no described effect): there the pixel change is the
    # whole claim, so the diff tier still falsifies before the judge can run.
    stated = VerificationIntent(
        kind=VerificationKind.MODEL_JUDGE,
        expected_change=True,
        expected_effect="the screen must change",
    )
    result = await agent._verify(stated, before, after)
    assert result.outcome == "verified"  # the judge adjudicated the defer
    assert provider.judge_calls == 1  # the cheap-tier defer reached the judge exactly once
    provider2 = JudgingProvider([], verdict="verified")
    session_id2, _b, backend2, _ = make_session(
        monkeypatch, backend=ScriptedBackend(flip=False), provider=provider2, dry_run=False,
        require_approval=False, limits=FAST_LIMITS,
    )
    agent2 = server._get_bundle(session_id2).agent
    before2, after2 = backend2.observe(), backend2.observe()
    bare = VerificationIntent(kind=VerificationKind.MODEL_JUDGE, expected_change=True)
    bare_result = await agent2._verify(bare, before2, after2)
    assert bare_result.outcome == "failed"  # the diff tier falsified the bare expectation...
    assert provider2.judge_calls == 0  # ...so the judge never ran


async def test_judge_tier_runs_when_cheap_tiers_uncertain(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C3 LADDER pin (agent seam, loop-free): a model-judge intent no cheap tier can
    decide falls through to the judge tier and adopts its verdict.

    RETARGETED (run_goal removal): the old in-loop pin, moved to the exact method
    (``_verify_judge_ladder``) the loop used to call. Pixels differ (ShiftingScreen),
    no deterministic criteria -> the judge runs ONCE and its verdict wins."""
    from computer_use_mcp.verification import VerificationIntent, VerificationKind

    provider = JudgingProvider([], verdict="verified")
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, backend=ShiftingScreenBackend(), provider=provider,
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    agent = server._get_bundle(session_id).agent
    before = agent._observe("probe_before")
    after = agent._observe("probe_after")  # ShiftingScreenBackend: pixels differ
    intent = VerificationIntent(
        kind=VerificationKind.MODEL_JUDGE,
        expected_change=True,
        expected_effect="changes",
    )
    result, tier = await agent._verify_judge_ladder(intent, before, after)
    assert provider.judge_calls == 1  # no deterministic criteria, pixels differ -> judge
    assert tier == "model_judge"
    assert result.outcome == "verified"


# --- C4: host-payload opt-out ---------------------------------------------------------------------


async def test_include_screenshot_after_false_omits_image_default_is_half_res_jpeg(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RC-D10 (058): the DEFAULT executed response ships a HALF-RESOLUTION JPEG so a
    driving model's per-action context stops growing by 150-230KB PNG per action.
    ``include_screenshot_after=False`` still omits the image entirely (opt-out
    unchanged); the explicit ``true`` opt-in ships FULL resolution (its documented
    meaning); ``CORTEX_ACTION_IMAGE_FULL=1`` restores full-res defaults."""
    monkeypatch.delenv("CORTEX_ACTION_IMAGE_FULL", raising=False)
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    default = await server.computer_execute(session_id, "click", x=15, y=15)
    # REM-A H3: executed responses are content blocks (TextContent + ImageContent)
    # in parity with computer_observe - the image never rides the text channel.
    assert isinstance(default, list) and len(default) == 2
    assert default[0].type == "text" and default[1].type == "image"
    default_payload = json.loads(default[0].text)
    assert default_payload["ok"] is True
    assert "screenshot_after_base64" not in default_payload  # blob never rides text
    # Half-resolution JPEG by default (JPEG SOI marker) + the additive truth marker.
    assert base64.b64decode(default[1].data, validate=True)[:2] == bytes([0xFF, 0xD8])
    assert default[1].mimeType == "image/jpeg"
    assert default_payload["image_scale"] == 0.5
    trimmed = await server.computer_execute(
        session_id, "click", x=25, y=25, include_screenshot_after=False
    )
    # Opt-out keeps the plain-dict legacy shape minus the blob (no image at all).
    assert isinstance(trimmed, dict)
    assert trimmed["ok"] is True
    assert "screenshot_after_base64" not in trimmed  # omitted entirely
    # Everything else survives the opt-out.
    assert trimmed["verification"]["outcome"] in {"verified", "uncertain", "failed"}
    full = await server.computer_execute(
        session_id, "click", x=35, y=35, include_screenshot_after=True
    )
    # The explicit opt-in is the documented FULL-image path: in-budget bytes keep
    # the original PNG identity (REM-A H7 budget ladder untouched).
    assert isinstance(full, list) and len(full) == 2
    png_sig = bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A])
    assert base64.b64decode(full[1].data, validate=True)[:8] == png_sig
    assert full[1].mimeType == "image/png"
    assert len(executed_summary(backend)) == 3


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
    session_id, _bundle, backend, _ = make_session(
        monkeypatch, dry_run=True, require_approval=False, limits=FAST_LIMITS
    )
    execute_response = await server.computer_execute(session_id, "wait", delta=1)
    execute_response = execute_payload(execute_response)
    assert execute_response["message"].startswith("DRY-RUN (no input dispatched):")
    # AMENDED (run_goal removal): the loop's second banner check died with the loop;
    # the direct-surface banner above is the surviving pin (RT6 fuzz in
    # test_p5_redteam covers 25 hostile dry-run shapes).
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
    response = execute_payload(response)  # REM-A: executed -> content blocks
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
    response = execute_payload(response)  # REM-A: executed -> content blocks
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
    """W-2 (057) contract: with CORTEX_QUEUE_STRICT_VERIFY=1 the v0.5.5 stop-on-failed
    behavior is restored — an EXECUTED item with a DEFINITIVE failed verification
    flushes the remaining items. RC-D11 (058) update: a hotkey with a pixel-shaped
    stated effect can no longer produce a DEFINITIVE failed (absent pixels degrade
    to uncertain — the W-2 repro file pins that uncertain does not stop), so the
    failing item is a keypress whose launch-prefix effect ("open Calculator")
    promotes the intent to the deterministic window_state tier; the title never
    appears on the settled screen -> definitive failed."""
    monkeypatch.setenv("CORTEX_QUEUE_STRICT_VERIFY", "1")
    backend = FlipOnceBackend(
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
            {"action": "keypress", "keys": ["enter"], "expected_effect": "open Calculator"},
            {"action": "click", "x": 30, "y": 30},
        ],
    )
    response = execute_payload(response)  # REM-A: executed -> content blocks
    assert response["follow_ups_stopped_reason"] == "verification_failed"
    assert response["follow_up_results"][1]["verification_outcome"] == "failed"
    assert len(executed_summary(backend)) == 2  # items 1-2 ran; item 3 was flushed


async def test_executed_failed_verdict_no_longer_stops_queue_by_default(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W-2 (057) default: an EXECUTED item whose verification definitively failed
    no longer flushes the batch — the honest failed verdict rides the per-item
    entry and the remaining items still run. RC-D11 (058) update: the failing item
    is a keypress with a launch-prefix effect -> the deterministic window_state
    tier fails definitively on the settled screen (a pixel-shaped stated effect
    now degrades to uncertain)."""
    monkeypatch.delenv("CORTEX_QUEUE_STRICT_VERIFY", raising=False)
    backend = FlipOnceBackend(
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
            {"action": "keypress", "keys": ["enter"], "expected_effect": "open Calculator"},
            {"action": "click", "x": 30, "y": 30},
        ],
    )
    response = execute_payload(response)  # REM-A: executed -> content blocks
    assert response["follow_ups_stopped_reason"] is None  # the batch completed
    assert response["follow_up_results"][1]["verification_outcome"] == "failed"
    assert response["follow_up_results"][1]["ok"] is False  # the honest verdict rides
    assert len(executed_summary(backend)) == 3  # ALL items ran


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
    response = execute_payload(response)  # REM-A: executed -> content blocks
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
    response = execute_payload(response)  # REM-A: executed -> content blocks
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
    """A judge intent with a starved (self-comparison) ladder is NOT false-failed at 0.0 diff.

    RETARGETED (run_goal removal): the starved-ladder guarantee is pinned at the
    agent seam (``_verify`` with before == after, the SAME observation object) —
    the exact starved shape the loop used to produce via a frozen post-action
    capture. The diff tier is routed past; the judge tier decides."""

    provider = JudgingProvider([], verdict="verified")
    session_id, _bundle, _backend, _ = make_session(
        monkeypatch, provider=provider, dry_run=False, require_approval=False, limits=FAST_LIMITS
    )
    agent = server._get_bundle(session_id).agent
    backend = ScriptedBackend(flip=False)
    frozen = backend.observe()  # the SAME observation object: starved self-comparison
    from computer_use_mcp.verification import VerificationIntent, VerificationKind

    intent = VerificationIntent(
        kind=VerificationKind.MODEL_JUDGE,
        expected_change=True,
        expected_effect="changes",
    )
    result = await agent._verify(intent, frozen, frozen)
    # The judge tier decided (the diff tier was routed past on the self-comparison)...
    assert provider.judge_calls == 1
    # ...and the outcome is the judge's verdict — never a 0.0-diff false failure.
    assert result.outcome == "verified"
