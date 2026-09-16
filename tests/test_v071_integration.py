"""v0.7.1 CROSS-TRACK INTEGRATION: the three field-defect fixes on ONE action path.

Each fix is unit-pinned in its own file (test_v071_launch_resolution.py /
test_capture_source_integrity.py / test_v071_type_via_clipboard.py). This file pins
the SEAM the mission called out: a clipboard-transport type (Defect C) carrying a
stated ``expected_effect`` flows through the agent verification ladder and must
produce, on the SAME outcome, every diagnostic layer honestly and simultaneously:

- the execution message keeps the C-track payload: ``via=clipboard`` +
  ``integrity=unverified`` + ``TYPE_UNCONFIRMED no-readable-target`` (blind target);
- the visual-tier verification carries the A-track ``capture_provenance``
  (identical hashes on a frozen source, differing hashes on a live one);
- the A-track counter marks the 3rd consecutive identical pair with
  ``CAPTURE_SOURCE_SUSPECTED identical_pairs=<n>`` — and NEVER alters the verdict
  (stated effect + zero diff stays ``uncertain``; a real change stays ``verified``);
- the clipboard discipline (paste recorded, prior clipboard restored) holds while
  the detector watches, and a real screen change resets the counter through the
  clipboard transport exactly like through any other.

All mechanics run on FakeComputerBackend (hermetic; no real clipboard, no real input).
"""

from __future__ import annotations

import asyncio
import base64
import io
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from computer_use_mcp.agent import CAPTURE_SOURCE_SUSPECTED, ComputerUseAgent
from computer_use_mcp.backend import TYPE_UNCONFIRMED_MARKER, FakeComputerBackend
from computer_use_mcp.limits import Limits
from computer_use_mcp.models import GroundedAction
from computer_use_mcp.safety import SafetyPolicy
from computer_use_mcp.state import StopToken, TaskState

# =====================================================================================
# Helpers (same proven shapes as test_capture_source_integrity.py — self-contained)
# =====================================================================================

WHITE = (240, 240, 240)
BLACK = (15, 15, 15)


def _png_b64(color: tuple[int, int, int], width: int = 64, height: int = 48) -> str:
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class _FrozenOrFlippedBackend(FakeComputerBackend):
    """Fake backend with deterministic frames; optional per-execute pixel flip.

    ``flip_script`` pops one bool per completed execute: True toggles the screenshot
    color AFTER that execute (that action's pre/post pair DIFFERS — a real change),
    False leaves it (byte-identical pair — the frozen-source shape). No script =
    always frozen. ``observe_calls`` counts every observation (provenance must add none).
    """

    def __init__(self, *, flip_script: list[bool] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.observe_calls = 0
        self.execute_calls = 0
        self.flip_script = list(flip_script) if flip_script is not None else None
        self._toggled = False

    def observe(self, monitor_index: int | None = None) -> Any:
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


class _RecordingAuditor:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event_type: str, session_id: str, **kwargs: Any) -> None:
        self.events.append({"event_type": event_type, "session_id": session_id, **kwargs})


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
    return [asyncio.run(agent.run_single(_state(), action, approved=True)) for action in actions]


def _clipboard_type(text: str, effect: str) -> GroundedAction:
    """The field-shaped action: clipboard transport into a no-a11y app, stated effect."""
    return GroundedAction(
        action="type", text=text, via="clipboard", expected_effect=effect, confidence=1.0
    )


def _verification_events(agent: ComputerUseAgent) -> list[dict[str, Any]]:
    return [event for event in agent.auditor.events if event["event_type"] == "verification"]  # type: ignore[attr-defined]


# =====================================================================================
# THE seam: clipboard type + stated effect -> C payload + A provenance + A marker
# =====================================================================================


def test_clipboard_type_stated_effect_all_three_diagnostic_layers_coexist() -> None:
    backend = _FrozenOrFlippedBackend()  # every frame byte-identical: the field shape
    backend.clipboard_value = "prior-content"  # seed so restore discipline is observable
    agent = _agent(backend)
    texts = ["value one", "value two", "value three"]
    outcomes = _run_actions(
        agent, [_clipboard_type(text, "the console echoes the value") for text in texts]
    )

    assert backend.execute_calls == 3
    for text, outcome in zip(texts, outcomes):
        assert outcome.kind == "executed" and outcome.result is not None
        result = outcome.result
        # --- C layer: the execution payload stays honest about the blind paste ---
        assert result.message.startswith("Simulated type. via=clipboard ")
        assert "integrity=unverified" in result.message
        assert TYPE_UNCONFIRMED_MARKER in result.message
        # the clipboard-path hint recommends VISUAL confirmation (never re-recommends
        # the transport it is already on — cf. the sendinput hint, which does)
        assert "screenshot/observe" in result.message
        assert 'via="clipboard"' not in result.message
        # --- A layer: the visual tier provenance rides the SAME outcome ---
        verification = result.verification
        assert verification is not None
        assert verification.capture_provenance is not None
        assert verification.capture_provenance.before_bytes > 0
        # honest verdict: stated effect + zero diff -> uncertain, NEVER success
        assert verification.outcome == "uncertain"
        assert result.ok is False
        assert CAPTURE_SOURCE_SUSPECTED not in result.message

    # C discipline: every paste recorded, clipboard restored after every action
    assert backend.clipboard_pastes == texts
    assert backend.clipboard_value == "prior-content"

    # A marker: fires exactly on the 3rd consecutive identical pair, never earlier
    for index, outcome in enumerate(outcomes):
        verification = outcome.result.verification
        assert verification is not None
        provenance = verification.capture_provenance
        assert provenance is not None
        assert provenance.before_sha256 == provenance.after_sha256  # frozen source
        if index < 2:
            assert CAPTURE_SOURCE_SUSPECTED not in verification.note
            assert CAPTURE_SOURCE_SUSPECTED not in verification.evidence
        else:
            expected = f"{CAPTURE_SOURCE_SUSPECTED} identical_pairs=3"
            assert expected in verification.note
            assert expected in verification.evidence
            # coexistence pin: verdict value unchanged by the marker
            assert verification.outcome == "uncertain"

    # audit parity: a TYPE action's verification runs the pixel tier TWICE (once under
    # the expected-text intent's ladder, once under the explicit visual fallback), so
    # 6 provenance-bearing audit events for 3 actions. The guard's action-id dedup
    # counts ONE pair per action regardless (verdicts above), and the marker appears
    # on both events of the 3rd action only.
    events = _verification_events(agent)
    visual_events = [
        event for event in events if event.get("metadata", {}).get("capture_provenance")
    ]
    assert len(visual_events) == 6
    for event in visual_events:
        assert str(event.get("metadata", {}).get("capture_provenance")).startswith("before=")
    assert [event.get("metadata", {}).get("capture_source_suspected") for event in visual_events] == [
        None,
        None,
        None,
        None,
        f"{CAPTURE_SOURCE_SUSPECTED} identical_pairs=3",
        f"{CAPTURE_SOURCE_SUSPECTED} identical_pairs=3",
    ]


def test_clipboard_type_real_change_verifies_and_never_marks() -> None:
    # a genuinely LIVE source through the clipboard transport: every pre/post pair
    # differs, so the stated effect VERIFIES, the detector never marks, and the
    # execution message still honestly reports the blind paste (both layers coexist
    # without lying in either direction).
    backend = _FrozenOrFlippedBackend(flip_script=[True, True])
    agent = _agent(backend)
    outcomes = _run_actions(
        agent,
        [_clipboard_type("alpha", "the console echoes the value") for _ in range(2)],
    )

    for outcome in outcomes:
        result = outcome.result
        assert result is not None
        assert TYPE_UNCONFIRMED_MARKER in result.message  # blind target, honestly stated
        verification = result.verification
        assert verification is not None
        assert verification.outcome == "verified"  # the real pixel change is honored
        assert result.ok is True
        assert verification.capture_provenance is not None
        assert verification.capture_provenance.before_sha256 != (
            verification.capture_provenance.after_sha256
        )
        assert CAPTURE_SOURCE_SUSPECTED not in verification.note
        assert CAPTURE_SOURCE_SUSPECTED not in verification.evidence
    assert agent._capture_identical_pairs == 0


def test_clipboard_transport_feeds_the_detector_reset_discipline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # two identical pairs, a REAL change through a clipboard type (reset), then three
    # more identical pairs: the marker must fire on the 3rd pair AFTER the reset
    # (action 6), proving the clipboard transport neither feeds nor starves the
    # counter differently from any other action.
    backend = _FrozenOrFlippedBackend(flip_script=[False, False, True, False, False, False])
    agent = _agent(backend)
    outcomes = _run_actions(
        agent,
        [_clipboard_type(f"row {i}", "the console echoes the value") for i in range(6)],
    )
    markers = [
        CAPTURE_SOURCE_SUSPECTED in outcome.result.verification.note  # type: ignore[union-attr]
        for outcome in outcomes
    ]
    assert markers == [False, False, False, False, False, True]
    assert (
        f"{CAPTURE_SOURCE_SUSPECTED} identical_pairs=3"
        in outcomes[-1].result.verification.note  # type: ignore[union-attr]
    )
    # the reset action itself verified the real change
    assert outcomes[2].result.verification.outcome == "verified"  # type: ignore[union-attr]
    # and the provenance performed ZERO extra captures: the same six actions on an
    # identical backend with provenance stubbed out observe exactly as many times
    parity_backend = _FrozenOrFlippedBackend(flip_script=[False, False, True, False, False, False])
    import computer_use_mcp.verification as verification_module

    monkeypatch.setattr(
        verification_module, "_capture_provenance", lambda before, after: None
    )
    agent_without = _agent(parity_backend)
    _run_actions(
        agent_without,
        [_clipboard_type(f"row {i}", "the console echoes the value") for i in range(6)],
    )
    assert parity_backend.observe_calls == backend.observe_calls
