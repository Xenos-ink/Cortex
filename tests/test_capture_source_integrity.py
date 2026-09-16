"""v0.7.1 Defect A: capture-source integrity guard (provenance + identical-frame detector).

Field evidence (v0.7.0 live Blender session): a dead/frozen capture source returned
byte-identical frames across distinct actions, so the visual tier confidently reported
``Mean pixel difference 0.000000`` forever — verdict-shaped output with no evidential
value. The fix ships TWO additive diagnostics (verdicts are NEVER altered):

- ``VerificationResult.capture_provenance``: sha256 + byte size of BOTH frames the
  visual tier already diffed (computed in-hand; zero new captures);
- an agent-side counter of CONSECUTIVE byte-identical pre/post pairs across DISTINCT
  screen-affecting actions; at >= 3 it annotates the result and the verification audit
  with ``CAPTURE_SOURCE_SUSPECTED identical_pairs=<n>`` (same uppercase vocabulary
  family as TARGET_GONE / FOCUS_DRIFTED / STUCK_MODIFIER / NO_INSTANCE). Any pair whose
  hashes/bytes differ RESETS the counter; ``wait``/``done`` and probe results never
  feed it.

Pinned here: threshold counting + marker, reset-on-differ, exempt kinds, one-pair-per-
action dedup (expected-text fallback), provenance presence/stability, verdict values
unchanged, and the detector performs ZERO new captures (observe-call parity).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

import computer_use_mcp.verification as verification_module
from computer_use_mcp.agent import (
    CAPTURE_SOURCE_SUSPECTED,
    ComputerUseAgent,
)
from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.limits import Limits
from computer_use_mcp.models import (
    CaptureProvenance,
    ExecutionResult,
    GroundedAction,
    Observation,
    VerificationResult,
)
from computer_use_mcp.safety import SafetyPolicy
from computer_use_mcp.state import StopToken, TaskState
from computer_use_mcp.verification import (
    ScreenshotDiffStrategy,
    VerificationEngine,
    VerificationIntent,
    VerificationKind,
    _capture_provenance,
)

# =====================================================================================
# Helpers
# =====================================================================================

WHITE = (240, 240, 240)
BLACK = (15, 15, 15)


def _png_b64(color: tuple[int, int, int], width: int = 64, height: int = 48) -> str:
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _observation(png_b64: str, width: int = 64, height: int = 48) -> Observation:
    return Observation(image_base64=png_b64, width=width, height=height)


def _provenance(identical: bool, tag: bytes = b"frame") -> CaptureProvenance:
    same = hashlib.sha256(tag).hexdigest()
    other = hashlib.sha256(tag + b"-other").hexdigest()
    return CaptureProvenance(
        before_sha256=same,
        after_sha256=same if identical else other,
        before_bytes=len(tag),
        after_bytes=len(tag),
    )


def _result_with(provenance: CaptureProvenance | None) -> VerificationResult:
    return VerificationResult(
        outcome="uncertain",
        changed=False,
        note="pixel tier note",
        confidence=0.4,
        verification_method="screenshot_diff",
        capture_provenance=provenance,
    )


def _keypress(effect: str | None = "the value commits") -> GroundedAction:
    return GroundedAction(
        action="keypress", keys=["enter"], expected_effect=effect, confidence=1.0
    )


class _RecordingAuditor:
    """In-memory auditor capturing every emitted event for assertions."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event_type: str, session_id: str, **kwargs: Any) -> None:
        self.events.append({"event_type": event_type, "session_id": session_id, **kwargs})


class _CaptureCountingBackend(FakeComputerBackend):
    """Fake backend counting observations; optional per-execute pixel flip.

    ``flip_script`` pops one bool per completed execute: True toggles the screenshot
    color AFTER that execute (so that action's pre/post pair differs), False leaves it
    (byte-identical pair — the frozen-source shape). No script = always frozen.
    """

    def __init__(self, *, flip_script: list[bool] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.observe_calls = 0
        self.execute_calls = 0
        self.flip_script = list(flip_script) if flip_script is not None else None
        self._toggled = False

    def observe(self, monitor_index: int | None = None) -> Observation:
        self.observe_calls += 1
        observation = super().observe(monitor_index)
        observation.image_base64 = _png_b64(BLACK if self._toggled else WHITE)
        return observation

    def execute(self, action: GroundedAction, stop: Any = None, **kwargs: Any) -> str:
        message = super().execute(action, stop, **kwargs)
        self.execute_calls += 1
        if self.flip_script is not None:
            flip = self.flip_script.pop(0) if self.flip_script else False
            if flip:
                self._toggled = not self._toggled
        return message


def _agent(backend: FakeComputerBackend, **kwargs: Any) -> ComputerUseAgent:
    kwargs.setdefault("auditor", _RecordingAuditor())
    return ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        limits=Limits(min_screenshot_interval_ms=1, max_actions=100, max_task_seconds=600.0).validate(),
        **kwargs,
    )


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        dry_run=False,
        stopped=False,
        allowed_windows=[],
        min_confidence=0.0,
        max_steps=100,
        step_count=0,
        require_approval=False,
        max_retries_per_action=1,
    )


def _run_actions(
    agent: ComputerUseAgent, actions: list[GroundedAction]
) -> list[Any]:
    outcomes = []
    for action in actions:
        outcomes.append(
            asyncio.run(agent.run_single(_state(), action, approved=True))
        )
    return outcomes


def _verification_events(agent: ComputerUseAgent) -> list[dict[str, Any]]:
    return [event for event in agent.auditor.events if event["event_type"] == "verification"]


# =====================================================================================
# Provenance: computed from frames in hand, stable, honest sizes
# =====================================================================================


def test_provenance_is_stable_for_identical_frames_and_differs_for_changed() -> None:
    first = _observation(_png_b64(WHITE))
    second = _observation(_png_b64(WHITE))
    changed = _observation(_png_b64(BLACK))
    strategy = ScreenshotDiffStrategy()
    intent = VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=True)

    identical_result = strategy.verify(intent, first, second)
    again_result = strategy.verify(intent, first, second)
    changed_result = strategy.verify(intent, first, changed)

    provenance = identical_result.capture_provenance
    assert provenance is not None
    assert provenance.algorithm == "sha256"
    assert provenance.before_sha256 == provenance.after_sha256
    # stable: identical frames hash identically across independent verifications
    assert again_result.capture_provenance == provenance
    # the frame identity bytes are the decoded RGB pixels (w*h*3), not the PNG payload
    assert provenance.before_bytes == 64 * 48 * 3 == provenance.after_bytes
    assert changed_result.capture_provenance is not None
    assert changed_result.capture_provenance.before_sha256 != changed_result.capture_provenance.after_sha256


def test_provenance_hashes_the_raw_stash_when_present() -> None:
    image = Image.new("RGB", (8, 4))
    image._frame_raw = b"\x01\x02\x03\x04" * 4  # type: ignore[attr-defined]
    other = Image.new("RGB", (8, 4))
    provenance = _capture_provenance(image, other)
    assert provenance is not None
    assert provenance.before_sha256 == hashlib.sha256(b"\x01\x02\x03\x04" * 4).hexdigest()
    assert provenance.before_bytes == 16
    # the decoded side hashes its RGB bytes
    assert provenance.after_sha256 == hashlib.sha256(other.tobytes()).hexdigest()
    assert provenance.after_bytes == 8 * 4 * 3


def test_provenance_rides_every_visual_tier_verdict_including_combined_uncertain() -> None:
    white = _observation(_png_b64(WHITE))
    strategy_result = ScreenshotDiffStrategy().verify(
        VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=None), white, white
    )
    assert strategy_result.outcome == "uncertain"
    assert strategy_result.capture_provenance is not None
    # the engine's combined-uncertain result keeps the pixel tier's provenance
    combined = VerificationEngine().verify(
        VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=None), white, white
    )
    assert combined.outcome == "uncertain"
    assert combined.capture_provenance is not None
    assert any(
        item.startswith("capture sha256 before=") and item.endswith("bytes=9216/9216")
        for item in combined.evidence
    )


def test_provenance_survives_serialization_and_legacy_results_stay_none() -> None:
    legacy = VerificationResult(verified=True, changed=True, note="legacy shape")
    assert legacy.capture_provenance is None
    payload = legacy.model_dump()
    assert payload["capture_provenance"] is None
    fresh = VerificationResult(
        verified=False,
        changed=False,
        note="with provenance",
        capture_provenance=_provenance(identical=True),
    )
    restored = VerificationResult.model_validate_json(fresh.model_dump_json())
    assert restored.capture_provenance == fresh.capture_provenance


def test_provenance_degrades_to_none_instead_of_raising() -> None:
    # a non-image frame object (no raw stash, no mode) degrades to None, never raises
    assert _capture_provenance(object(), object()) is None
    # a non-bytes raw stash is ignored and the decoded RGB bytes hash gracefully
    image = Image.new("RGB", (4, 4))
    image._frame_raw = 12345  # type: ignore[attr-defined]
    provenance = _capture_provenance(image, Image.new("RGB", (4, 4)))
    assert provenance is not None
    assert provenance.before_sha256 == hashlib.sha256(image.tobytes()).hexdigest()


# =====================================================================================
# Unit: the identical-pair counter and the typed marker
# =====================================================================================


def test_counter_crosses_threshold_and_marker_carries_the_count() -> None:
    agent = _agent(_CaptureCountingBackend())
    actions = [_keypress() for _ in range(4)]
    markers: list[str | None] = []
    for action in actions:
        result, marker = agent._capture_source_guard(action, _result_with(_provenance(True)))
        markers.append(marker)
        if marker is not None:
            assert marker in result.note
            assert marker in result.evidence
            # fail-open on the verdict: outcome/confidence/note-prefix stay untouched
            assert result.outcome == "uncertain"
            assert result.confidence == 0.4
    assert agent._capture_identical_pairs == 4
    assert markers == [
        None,
        None,
        f"{CAPTURE_SOURCE_SUSPECTED} identical_pairs=3",
        f"{CAPTURE_SOURCE_SUSPECTED} identical_pairs=4",
    ]


def test_differing_pair_resets_the_counter() -> None:
    agent = _agent(_CaptureCountingBackend())
    pairs = [True, True, False, True, True, True]  # third pair differs -> reset
    markers: list[str | None] = []
    for identical in pairs:
        action = _keypress()
        _, marker = agent._capture_source_guard(action, _result_with(_provenance(identical)))
        markers.append(marker)
    # the count restarts after the reset: the marker fires on the 3rd consecutive
    # identical pair AFTER the differing one (not on the 5th pair overall)
    assert markers == [None, None, None, None, None, f"{CAPTURE_SOURCE_SUSPECTED} identical_pairs=3"]


def test_exempt_kinds_never_feed_the_counter() -> None:
    agent = _agent(_CaptureCountingBackend())
    for kind, fields in (("wait", {"delta": 1}), ("done", {})):
        action = GroundedAction(action=kind, confidence=1.0, **fields)
        result, marker = agent._capture_source_guard(action, _result_with(_provenance(True)))
        assert marker is None
        assert agent._capture_identical_pairs == 0
        assert result.note == "pixel tier note"  # untouched


def test_probe_shaped_result_without_provenance_never_counts_or_resets() -> None:
    agent = _agent(_CaptureCountingBackend())
    # probe outcomes (ensure_app NO_INSTANCE/AMBIGUOUS_INSTANCE) carry no provenance:
    # the counter must neither grow nor reset on them.
    first, marker = agent._capture_source_guard(_keypress(), _result_with(_provenance(True)))
    assert marker is None and agent._capture_identical_pairs == 1
    probe_result, marker = agent._capture_source_guard(_keypress(), _result_with(None))
    assert marker is None and probe_result is not None
    assert agent._capture_identical_pairs == 1  # untouched by the evidence-free result
    second, marker = agent._capture_source_guard(_keypress(), _result_with(_provenance(True)))
    assert marker is None and agent._capture_identical_pairs == 2
    assert first is not None and second is not None


def test_screen_affecting_action_with_differing_pair_resets_even_after_marker() -> None:
    agent = _agent(_CaptureCountingBackend())
    for _ in range(3):
        agent._capture_source_guard(_keypress(), _result_with(_provenance(True)))
    assert agent._capture_identical_pairs >= 3
    _, marker = agent._capture_source_guard(_keypress(), _result_with(_provenance(False)))
    assert marker is None
    assert agent._capture_identical_pairs == 0


def test_expected_text_fallback_replaces_its_own_contribution() -> None:
    """The pipeline re-verifies the SAME action (expected-text fallback to the visual
    tier); the pair must count exactly once, not twice."""
    agent = _agent(_CaptureCountingBackend())
    action = _keypress()
    agent._capture_source_guard(action, _result_with(_provenance(True)))
    assert agent._capture_identical_pairs == 1
    agent._capture_source_guard(action, _result_with(_provenance(True)))
    assert agent._capture_identical_pairs == 1  # replaced, not double-counted
    other = _keypress()
    agent._capture_source_guard(other, _result_with(_provenance(True)))
    assert agent._capture_identical_pairs == 2


def test_action_without_provenance_or_action_object_is_a_noop() -> None:
    agent = _agent(_CaptureCountingBackend())
    result = _result_with(_provenance(True))
    untouched, marker = agent._capture_source_guard(None, result)
    assert untouched is result and marker is None
    untouched, marker = agent._capture_source_guard(_keypress(), _result_with(None))
    assert untouched.note == "pixel tier note" and marker is None
    assert agent._capture_identical_pairs == 0


# =====================================================================================
# Integration: frozen capture source through the real pipeline (fake backend)
# =====================================================================================


def test_frozen_source_marks_third_distinct_action_and_verdicts_stay_honest() -> None:
    backend = _CaptureCountingBackend()  # every frame byte-identical: the field shape
    auditor = _RecordingAuditor()
    agent = _agent(backend, auditor=auditor)
    outcomes = _run_actions(agent, [_keypress() for _ in range(4)])

    assert backend.execute_calls == 4
    for index, outcome in enumerate(outcomes):
        assert outcome.kind == "executed" and outcome.result is not None
        verification = outcome.result.verification
        assert verification is not None
        # honest verdicts: stated effect + zero diff -> uncertain, NEVER success
        assert verification.outcome == "uncertain"
        assert outcome.result.ok is False
        assert verification.capture_provenance is not None
        assert verification.capture_provenance.before_sha256 == (
            verification.capture_provenance.after_sha256
        )
        if index < 2:
            assert CAPTURE_SOURCE_SUSPECTED not in verification.note
        else:
            expected = f"{CAPTURE_SOURCE_SUSPECTED} identical_pairs={index + 1}"
            assert expected in verification.note
            assert expected in verification.evidence

    audit_markers = [
        event.get("metadata", {}).get("capture_source_suspected")
        for event in _verification_events(agent)
    ]
    assert audit_markers == [None, None, f"{CAPTURE_SOURCE_SUSPECTED} identical_pairs=3",
                             f"{CAPTURE_SOURCE_SUSPECTED} identical_pairs=4"]
    # every verification audit carries the provenance summary (honest diagnostics)
    for event in _verification_events(agent):
        provenance_meta = event.get("metadata", {}).get("capture_provenance")
        assert provenance_meta is not None
        assert provenance_meta.startswith("before=") and "after=" in provenance_meta


def test_live_capture_never_marks_and_changed_pairs_verify() -> None:
    # a genuinely LIVE source: every action's post-capture differs from its premise,
    # so no identical pair ever accumulates and every stated change verifies.
    backend = _CaptureCountingBackend(flip_script=[True, True, True, True])
    agent = _agent(backend)
    outcomes = _run_actions(agent, [_keypress() for _ in range(4)])

    for outcome in outcomes:
        verification = outcome.result.verification
        assert verification is not None and verification.outcome == "verified"
        assert verification.capture_provenance is not None
        assert verification.capture_provenance.before_sha256 != (
            verification.capture_provenance.after_sha256
        )
        assert CAPTURE_SOURCE_SUSPECTED not in verification.note
        assert CAPTURE_SOURCE_SUSPECTED not in verification.evidence
    assert agent._capture_identical_pairs == 0
    assert all(
        event.get("metadata", {}).get("capture_source_suspected") is None
        for event in _verification_events(agent)
    )


def test_reset_after_real_change_restarts_the_count_at_three() -> None:
    # two identical pairs, a REAL change (reset), then three more identical pairs:
    # the marker must fire on the 3rd pair after the reset (action 6), not earlier.
    backend = _CaptureCountingBackend(flip_script=[False, False, True])
    agent = _agent(backend)
    outcomes = _run_actions(agent, [_keypress() for _ in range(6)])
    markers = [
        outcome.result.verification.note for outcome in outcomes  # type: ignore[union-attr]
    ]
    assert [CAPTURE_SOURCE_SUSPECTED in note for note in markers] == [
        False, False, False, False, False, True,
    ]
    assert f"{CAPTURE_SOURCE_SUSPECTED} identical_pairs=3" in markers[-1]
    # the reset action itself verified the real change
    assert outcomes[2].result.verification.outcome == "verified"  # type: ignore[union-attr]


def test_detector_and_provenance_perform_zero_new_captures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard consumes frames ALREADY in hand: observe-call counts must be byte-for-
    byte equal with the detector active vs fully disabled (provenance stubbed to None)."""
    frozen_backend = _CaptureCountingBackend()
    agent_with = _agent(frozen_backend)
    _run_actions(agent_with, [_keypress() for _ in range(4)])
    with_markers = frozen_backend.observe_calls

    disabled_backend = _CaptureCountingBackend()
    agent_without = _agent(disabled_backend)
    monkeypatch.setattr(verification_module, "_capture_provenance", lambda before, after: None)
    _run_actions(agent_without, [_keypress() for _ in range(4)])
    assert disabled_backend.observe_calls == with_markers
    # and the disabled run produced no marker (the counter saw no provenance at all)
    assert all(
        event.get("metadata", {}).get("capture_source_suspected") is None
        for event in _verification_events(agent_without)
    )


def test_diagnostics_are_additive_verdicts_identical_without_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below the marker threshold the diagnostics are pure additive fields: outcome,
    confidence, note, and ok are IDENTICAL to a run with provenance fully disabled."""
    outcomes_by_mode: dict[str, list[Any]] = {}
    for mode in ("with", "without"):
        backend = _CaptureCountingBackend()
        agent = _agent(backend)
        if mode == "without":
            monkeypatch.setattr(verification_module, "_capture_provenance", lambda before, after: None)
        outcomes_by_mode[mode] = _run_actions(agent, [_keypress() for _ in range(2)])

    for with_outcome, without_outcome in zip(outcomes_by_mode["with"], outcomes_by_mode["without"]):
        with_result = with_outcome.result
        without_result = without_outcome.result
        assert with_result is not None and without_result is not None
        assert with_outcome.kind == without_outcome.kind
        assert with_result.ok == without_result.ok
        assert with_result.message == without_result.message
        assert with_result.verification is not None and without_result.verification is not None
        assert with_result.verification.outcome == without_result.verification.outcome
        assert with_result.verification.confidence == without_result.verification.confidence
        assert with_result.verification.note == without_result.verification.note
        assert with_result.verification.changed == without_result.verification.changed
        # the only deltas are the additive diagnostics themselves
        assert with_result.verification.capture_provenance is not None
        assert without_result.verification.capture_provenance is None
        assert len(with_result.verification.evidence) == len(without_result.verification.evidence) + 1
        assert with_result.verification.evidence[:-1] == without_result.verification.evidence


# =====================================================================================
# Verdict vocabulary unchanged (engine-level pins: provenance never moves a verdict)
# =====================================================================================


@pytest.mark.parametrize(
    ("expected_change", "expected_effect", "identical", "outcome", "confidence"),
    [
        (True, None, True, "failed", 0.85),  # bare change expectation: legacy failure kept
        (True, "the value commits", True, "uncertain", 0.4),  # stated effect: never false-failed
        (None, None, True, "uncertain", 0.9),  # no expectation: identity proves nothing
        (True, None, False, "verified", None),  # change detected
        (False, None, False, "failed", None),  # stability expectation broken
        (False, None, True, "verified", 0.95),  # stability held
    ],
)
def test_visual_change_verdicts_unchanged_with_provenance_attached(
    expected_change: bool | None,
    expected_effect: str | None,
    identical: bool,
    outcome: str,
    confidence: float | None,
) -> None:
    before = _observation(_png_b64(WHITE))
    after = _observation(_png_b64(WHITE if identical else BLACK))
    intent = VerificationIntent(
        kind=VerificationKind.VISUAL_CHANGE,
        expected_change=expected_change,
        expected_effect=expected_effect,
    )
    result = ScreenshotDiffStrategy().verify(intent, before, after)
    assert result.outcome == outcome
    if confidence is not None:
        assert result.confidence == pytest.approx(confidence)
    assert result.capture_provenance is not None  # diagnostics ride; verdict unmoved
    provenance_line = result.evidence[-1]
    assert provenance_line.startswith("capture sha256 before=")
    assert "after=" in provenance_line and "bytes=" in provenance_line
    # a 0.000000 diff is ALWAYS accompanied by frame identity evidence
    zero_diff_evidence = [item for item in result.evidence if item.startswith("Mean pixel difference 0.000000")]
    if zero_diff_evidence:
        assert any(item.startswith("capture sha256 before=") for item in result.evidence)


def test_execution_result_serializes_provenance_for_outbound_payloads() -> None:
    """The provenance reaches the outbound ExecutionResult payload (what the driver sees)."""
    backend = _CaptureCountingBackend()
    agent = _agent(backend)
    (outcome,) = _run_actions(agent, [_keypress()])
    assert outcome.result is not None and outcome.result.verification is not None
    verification = outcome.result.verification
    assert verification.capture_provenance is not None
    dumped = outcome.result.model_dump()
    payload_provenance = dumped["verification"]["capture_provenance"]
    assert payload_provenance["algorithm"] == "sha256"
    assert payload_provenance["before_sha256"] == payload_provenance["after_sha256"]
    assert payload_provenance["before_bytes"] == payload_provenance["after_bytes"] == 64 * 48 * 3


def test_execution_result_helper_importable_for_typing() -> None:
    """ExecutionResult keeps carrying VerificationResult (additive field inside it)."""
    result = ExecutionResult(
        ok=False,
        action=_keypress(),
        message="msg",
        verification=_result_with(_provenance(True)),
    )
    assert result.verification is not None
    assert result.verification.capture_provenance is not None
