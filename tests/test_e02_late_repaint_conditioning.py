"""E-02 regression — the late-repaint re-capture costs nothing on effect-less static
actions (ORVEX-2026-108 WP4a).

The upstream late-repaint recovery (F2-3) re-captured on EVERY identical-frame
uncertain, which added a capture per action on quiet screens (the measured +33-40%
captures regression, ORVEX-2026-106 E-02). The WP4a conditioning: the ONE re-capture
fires IFF a stated ``expected_effect`` exists OR the action class is repaint-prone
(``type``/``scroll``); it is bounded to exactly ONE re-capture; and an effect-less
``click``/``move``/``keypress`` on a static screen performs ZERO extra captures —
captures/action on the fake stays at the WP0 baseline (3.0 captures/action,
harness ``.orvex/mission/artifacts/gates/perf_harness.py``).

Counted on a counting fake backend (observe calls traced through the real agent
pipeline), never on the live desktop.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from computer_use_mcp.pipeline.agent import ComputerUseAgent
from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.core.limits import Limits
from computer_use_mcp.core.models import GroundedAction, Observation, Point
from computer_use_mcp.security.safety import SafetyPolicy
from computer_use_mcp.core.state import StopToken, TaskState

WIDTH = 1280
HEIGHT = 720


class StaticCountingBackend(FakeComputerBackend):
    """A static-screen fake that counts every capture (observe call)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(width=WIDTH, height=HEIGHT, **kwargs)
        self.capture_count = 0

    def observe(self, monitor_index: int | None = None) -> Observation:
        self.capture_count += 1
        return super().observe(monitor_index)


def _agent(backend: StaticCountingBackend) -> ComputerUseAgent:
    return ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        limits=Limits(min_screenshot_interval_ms=1, max_actions=100, max_task_seconds=600.0).validate(),
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


def _run(backend: StaticCountingBackend, action: GroundedAction, **kwargs: Any) -> Any:
    agent = _agent(backend)
    outcome = asyncio.run(agent.run_single(_state(), action, approved=True, **kwargs))
    assert outcome.result is not None, outcome.reasons
    return outcome


# --- E-02: effect-less static actions cost ZERO extra captures -----------------------------------


@pytest.mark.parametrize(
    "action",
    [
        GroundedAction(action="click", point=Point(x=100, y=100), confidence=1.0),
        GroundedAction(action="move", point=Point(x=120, y=110), confidence=1.0),
        GroundedAction(action="keypress", keys=["enter"], confidence=1.0),
    ],
    ids=["click", "move", "keypress"],
)
def test_effectless_static_actions_perform_zero_extra_captures(action: GroundedAction) -> None:
    """No stated effect + not a repaint-prone class ⇒ NO late-repaint re-capture, ever:
    exactly the WP0 floor of 3 captures (premise + validate + post_action)."""
    backend = StaticCountingBackend()
    outcome = _run(backend, action)
    assert backend.capture_count == 3, backend.capture_count
    assert outcome.result.verification is not None
    # F10 (VAL-WP4a): the verdict is exact per action — the click and the keypress on
    # a static screen are honestly ``uncertain``; the move verifies through its
    # deterministic cursor-at-target predicate (which never reads a stated effect).
    if action.action.value == "move":
        assert outcome.result.verification.outcome == "verified"
    else:
        assert outcome.result.verification.outcome == "uncertain"
        assert outcome.result.ok is False


# --- E-02: the conditioned classes earn AT MOST the ONE re-capture --------------------------------


def test_a_stated_effect_click_earns_at_most_one_extra_capture() -> None:
    """A stated ``expected_effect`` conditions the re-capture on any action class:
    the static screen earns exactly ONE extra capture (bounded) and the verdict stays
    the honest ``uncertain`` — pixels recovered later still cannot prove the effect."""
    backend = StaticCountingBackend()
    outcome = _run(backend, GroundedAction(action="click", point=Point(x=100, y=100), confidence=1.0),
                   expected_effect="the record is deleted")
    assert backend.capture_count == 4, backend.capture_count  # 3 + exactly one re-capture
    assert outcome.result.verification is not None
    assert outcome.result.verification.outcome == "uncertain"
    assert outcome.result.ok is False


@pytest.mark.parametrize("action", ["type", "scroll"], ids=["type", "scroll"])
def test_repaint_prone_classes_earn_at_most_one_extra_capture(action: str) -> None:
    """``type``/``scroll`` are repaint-prone by nature: on a static screen each earns
    exactly ONE bounded re-capture (never a poll loop), no stated effect required."""
    backend = StaticCountingBackend()
    if action == "type":
        grounded = GroundedAction(action="type", text="quiet", confidence=1.0)
    else:
        grounded = GroundedAction(action="scroll", delta=-1, confidence=1.0)
    _run(backend, grounded)
    assert backend.capture_count == 4, backend.capture_count  # 3 + exactly one re-capture


def test_the_recovery_never_repeats_even_when_the_screen_stays_identical() -> None:
    """Two stated-effect clicks in a row on the SAME static screen: each action is
    bounded to its own single re-capture — 4 captures per action, never growth."""
    backend = StaticCountingBackend()
    click = GroundedAction(action="click", point=Point(x=100, y=100), confidence=1.0)
    _run(backend, click, expected_effect="first effect")
    _run(backend, GroundedAction(action="click", point=Point(x=140, y=140), confidence=1.0),
         expected_effect="second effect")
    assert backend.capture_count == 8, backend.capture_count  # 4 + 4, never 4 + 5


def test_the_recapture_rides_the_rate_exempt_post_action_phase() -> None:
    """The re-capture is a ``post_action`` capture (rate-exempt intra-step phase), not
    a rate-gated fresh observation — pinned through the agent's own phase tracing."""
    backend = StaticCountingBackend()
    agent = _agent(backend)
    phases: list[str] = []
    original = agent._observe

    def traced(phase: str) -> Observation:
        phases.append(phase)
        return original(phase)

    agent._observe = traced  # type: ignore[method-assign]
    outcome = asyncio.run(
        agent.run_single(
            _state(),
            GroundedAction(action="click", point=Point(x=100, y=100), confidence=1.0),
            approved=True,
            expected_effect="the record is deleted",
        )
    )
    assert outcome.result is not None
    assert phases == ["direct_request", "validate", "post_action", "post_action"], phases


def test_an_effect_riding_only_on_the_action_still_conditions_the_recapture() -> None:
    """F6 (VAL-WP4a DELTA 2): the recapture gate reads the INTENT's effect —
    ``_build_intent`` merges the ``expected_effect`` PARAMETER and
    ``action.expected_effect`` — so an effect stated only on the action object
    conditions the ONE bounded re-capture exactly like a parameter-stated one."""
    backend = StaticCountingBackend()
    outcome = _run(
        backend,
        GroundedAction(
            action="click",
            point=Point(x=100, y=100),
            expected_effect="the record is deleted",
            confidence=1.0,
        ),
    )
    assert backend.capture_count == 4, backend.capture_count  # 3 + exactly one re-capture
    assert outcome.result.verification is not None
    assert outcome.result.verification.outcome == "uncertain"
    assert outcome.result.ok is False
