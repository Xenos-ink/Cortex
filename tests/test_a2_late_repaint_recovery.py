"""A2 / F2-3 + E-02 — ONE bounded re-capture, conditioned, before declaring uncertain.

The pipeline took exactly ONE post-action capture, 16-32 ms after dispatch, and that
phase is exempt from the screenshot rate gate (``_INTRA_STEP_EXEMPT_PHASES``), so a UI
that repaints a millisecond later was missed forever with no retry at all.  The
measured cliff was 0 ms -> ``verified``, 1 ms -> ``uncertain``.

F2-3: when the verdict is ``uncertain`` **specifically because the two frames were
byte-identical** — decided from the capture-provenance hashes the pixel tier already
computed, never from a new capture — the agent settles briefly and takes **one** more
``post_action`` capture, then re-verifies against the NEW pair.  So the cliff moves
from "1 ms" to "past the settle".

E-02 (the WP4a fix of the upstream regression): the ONE re-capture is CONDITIONED —
it fires only when a fresh frame could plausibly carry NEW evidence, i.e. a stated
``expected_effect`` OR a repaint-prone action class (``type``/``scroll``). An
effect-less ``click`` on a static screen performs ZERO extra captures (the counting
regression lives in tests/test_e02_late_repaint_conditioning.py). Bounded to exactly
one re-capture, never a poll loop.

Non-negotiable guards, all pinned here:

* a world that NEVER changes stays ``uncertain`` — the re-capture finds the same
  evidence, so nothing is upgraded;
* an ``uncertain`` whose two frames already DIFFER (a sub-threshold change) is NOT
  re-captured at all — re-capturing on a non-identical uncertain would let noise
  upgrade a verdict, which the doctrine forbids;
* a stated effect is never verified by the recovered pixels (E-03): the recovery
  updates the evidence, the verdict stays the honest ``uncertain``.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

from PIL import Image, ImageDraw
import pytest

from computer_use_mcp.pipeline.agent import (
    _IDENTICAL_FRAME_RECHECK_LIMIT,
    _IDENTICAL_FRAME_RECHECK_SETTLE_SECONDS,
    ComputerUseAgent,
)
from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.core.limits import Limits
from computer_use_mcp.core.models import GroundedAction, Observation, WindowInfo
from computer_use_mcp.security.safety import SafetyPolicy
from computer_use_mcp.core.state import StopToken, TaskState

WIDTH = 1280
HEIGHT = 720
BOARD = WindowInfo(
    hwnd=4242,
    pid=90210,
    process_name="chess.exe",
    exe_path="C:\\Games\\chess.exe",
    window_class="ChessMainWnd",
    title="Chess - untitled",
    bounds=(0, 0, WIDTH, HEIGHT),
)


def _frame(*, changed: bool, tick: int = 0) -> str:
    import base64
    import io

    image = Image.new("RGB", (WIDTH, HEIGHT), (32, 32, 36))
    draw = ImageDraw.Draw(image)
    draw.rectangle([40, 40, 820, 600], outline=(120, 120, 130), width=3)
    if changed:
        # an unmistakable repaint: a 280x320 solid menu, nudged by the tick so two
        # "changed" frames are never byte-identical to each other
        draw.rectangle([900 + tick, 300, 1180 + tick, 620], fill=(238, 238, 240))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _backend_class(
    delay: float, *, never_changes: bool = False, paint_after_observes: int | None = None
) -> type[FakeComputerBackend]:
    class DelayedRepaintBackend(FakeComputerBackend):
        """World state changes at dispatch; the PIXELS follow ``delay`` seconds later.

        HERMETICITY (WP7b carry-over): ``paint_after_observes`` INJECTS the capture
        timing instead of the wall clock — the paint lands deterministically on the
        ``paint_after_observes``-th ``observe()`` after dispatch (0 = the first
        post-action capture already sees it; 1 = exactly the ONE bounded re-capture
        sees it; a large value = beyond the settle). Under a loaded full-suite run
        the real 1 ms delays could elapse BEFORE the first capture, flipping the
        capture-count assertions; the injected clock removes the race. The
        wall-clock ``delay`` arm is unchanged for every other node.
        """

        def __init__(self) -> None:
            super().__init__(width=WIDTH, height=HEIGHT, active_window=WindowInfo(**BOARD.model_dump()))
            self.ground_truth = False
            self._frozen = never_changes
            self._painted = False
            self._pending_since: float | None = None
            self._observes_since_dispatch = 0
            self.dispatch_ts: float | None = None
            self.capture_phases: list[str] = []

        def observe(self, monitor_index: int | None = None) -> Observation:
            if self._pending_since is not None and not self._painted and not self._frozen:
                if paint_after_observes is not None:
                    if self._observes_since_dispatch >= paint_after_observes:
                        self._painted = True
                elif time.monotonic() - self._pending_since >= delay:
                    self._painted = True
            observation = super().observe(monitor_index)
            observation.image_base64 = _frame(
                changed=self._painted, tick=len(self.capture_phases)
            )
            self.capture_phases.append("capture")
            if self._pending_since is not None:
                self._observes_since_dispatch += 1
            return observation

        def execute(self, action, stop=None, focus_hook=None, allow_launch=False):  # type: ignore[no-untyped-def]
            message = super().execute(action, stop, focus_hook=focus_hook, allow_launch=allow_launch)
            if action.action in {"click", "right_click", "keypress", "type"}:
                self.ground_truth = True
                self._pending_since = time.monotonic()
                self.dispatch_ts = time.monotonic()
            return message

    return DelayedRepaintBackend


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


def _run(backend: FakeComputerBackend, action: GroundedAction, **kwargs: Any) -> Any:
    agent = ComputerUseAgent(
        backend,
        provider=None,
        safety=SafetyPolicy(),
        task=TaskState(),
        stop=StopToken(),
        limits=Limits(min_screenshot_interval_ms=1, max_actions=100).validate(),
    )
    phases: list[str] = []
    original = agent._observe

    def traced(phase: str) -> Observation:
        phases.append(phase)
        return original(phase)

    agent._observe = traced  # type: ignore[method-assign]
    outcome = asyncio.run(agent.run_single(_state(), action, approved=True, **kwargs))
    assert outcome.result is not None, outcome.reasons
    return outcome.result, phases


def _click() -> GroundedAction:
    from computer_use_mcp.core.models import Point

    return GroundedAction(action="click", point=Point(x=400, y=300), confidence=1.0)


def _type(text: str = "castle move") -> GroundedAction:
    return GroundedAction(action="type", text=text, confidence=1.0)


# --- the conditioning itself (E-02 spec, pinned as constants) ------------------------------------


def test_the_recovery_is_bounded_to_one_and_conditioned_by_the_e02_spec() -> None:
    """The knobs are real: a non-zero settle, a hard limit of exactly ONE re-capture,
    and the repaint-prone classes are exactly type/scroll (E-02 spec)."""
    from computer_use_mcp.pipeline.agent import _INTRA_STEP_EXEMPT_PHASES, _REPAINT_PRONE_ACTIONS
    from computer_use_mcp.core.models import ActionType

    assert _IDENTICAL_FRAME_RECHECK_SETTLE_SECONDS > 0.0
    assert _IDENTICAL_FRAME_RECHECK_LIMIT == 1
    assert "post_action" in _INTRA_STEP_EXEMPT_PHASES
    assert _REPAINT_PRONE_ACTIONS == frozenset({ActionType.TYPE, ActionType.SCROLL})


# --- the cliff moves past the settle (repaint-prone class: type) ---------------------------------


def test_late_repaints_up_to_the_settle_no_longer_survive_the_cliff() -> None:
    """THE DELAY SWEEP, on a repaint-prone ``type`` action with NO stated effect.
    Before the fix, 0 ms verified and 1 ms did not — a cliff one millisecond wide.
    Now a repaint that lands inside the settle is caught by the one extra capture, so
    every delay in the sweep verifies (the recovered pixels ARE the bare-change
    claim on the T8 B2 pixel fallback).

    HERMETICITY (WP7b carry-over): the paint timing is INJECTED
    (``paint_after_observes``) instead of racing the real 1 ms delays against the
    dispatch→capture gap — under a loaded full-suite run the real clock let the
    repaint land inside the FIRST capture, flipping the capture-count assertions.
    The assertions are UNCHANGED (verdicts + the exact per-delay capture counts):
    delay 0 lands inside the FIRST capture (nothing to recover); every later delay
    needs exactly the ONE re-capture and no more."""
    settle_ms = _IDENTICAL_FRAME_RECHECK_SETTLE_SECONDS * 1000.0
    for delay_ms in (0, 1, 3, 10, 50):
        if delay_ms >= settle_ms:
            continue
        # 0 ms paints before the first capture; every nonzero in-settle delay paints
        # exactly on the ONE bounded re-capture (capture timing injected)
        paint_at = 0 if delay_ms == 0 else 1
        backend = _backend_class(delay_ms / 1000.0, paint_after_observes=paint_at)()
        result, phases = _run(backend, _type())
        assert backend.ground_truth is True
        assert result.verification is not None
        assert result.verification.outcome == "verified", (
            f"delay {delay_ms} ms -> {result.verification.outcome}: {result.verification.note}"
        )
        assert result.ok is True
        expected_captures = 1 if delay_ms == 0 else 2
        assert phases.count("post_action") == expected_captures, (delay_ms, phases)


def test_a_repaint_beyond_the_settle_stays_uncertain_and_bounded() -> None:
    """Beyond the settle the one re-capture is not enough — and that is the honest
    answer, not a regression: a single bounded retry cannot chase an unbounded
    repaint, so exactly ONE extra capture happens and no more."""
    backend = _backend_class(_IDENTICAL_FRAME_RECHECK_SETTLE_SECONDS + 0.25)()
    result, phases = _run(backend, _type())
    assert backend.ground_truth is True
    assert result.verification is not None
    assert result.verification.outcome == "uncertain"
    assert result.ok is False
    assert phases.count("post_action") == 2, phases


# --- the guards: never upgrade without new evidence ----------------------------------------------


def test_a_world_that_never_changes_stays_uncertain() -> None:
    """No re-capture can help when there is genuinely nothing new: the verdict is
    ``uncertain`` before and after, and the one extra capture did not launder it. A
    repaint-prone ``type`` earns the re-capture regardless of the screen."""
    backend = _backend_class(0.0, never_changes=True)()
    result, phases = _run(backend, _type())
    assert result.verification is not None
    assert result.verification.outcome == "uncertain", result.verification.note
    assert result.ok is False
    # the re-capture still happened (the conditioning was met) and changed nothing
    assert phases.count("post_action") == 2, phases


def test_a_stated_effect_click_earns_exactly_one_bounded_recapture() -> None:
    """A stated ``expected_effect`` conditions the re-capture on ANY action class: the
    static-screen click re-captures ONCE (a described transition may still be proven
    past the repaint), and — E-03 — the recovered pixels still cannot verify the
    stated effect, so the verdict stays the honest ``uncertain``."""
    backend = _backend_class(0.0, never_changes=True)()
    result, phases = _run(backend, _click(), expected_effect="the move is played")
    assert result.verification is not None
    assert result.verification.outcome == "uncertain", result.verification.note
    assert result.ok is False
    assert phases.count("post_action") == 2, phases  # exactly ONE re-capture


def test_a_non_identical_uncertain_is_not_re_captured() -> None:
    """An ``uncertain`` whose two frames already DIFFER (a sub-threshold change) is
    left alone: re-capturing there would let noise upgrade a verdict, which the
    doctrine forbids.  Only the byte-identical pair earns the one retry — pinned on
    BOTH an effect-less click (E-02 would skip it anyway) and a repaint-prone type
    (E-02 alone would fire; only the non-identical pair stops it)."""
    import base64
    import io

    class NoiseBackend(FakeComputerBackend):
        """Every capture is a DIFFERENT frame that never crosses the change threshold."""

        def __init__(self) -> None:
            super().__init__(width=WIDTH, height=HEIGHT, active_window=WindowInfo(**BOARD.model_dump()))
            self.counter = 0

        def observe(self, monitor_index: int | None = None) -> Observation:
            self.counter += 1
            observation = super().observe(monitor_index)
            image = Image.new("RGB", (WIDTH, HEIGHT), (32, 32, 36))
            ImageDraw.Draw(image).point((self.counter, 3), fill=(240, 240, 240))
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            observation.image_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
            return observation

    backend = NoiseBackend()
    result, phases = _run(backend, _click())
    assert result.verification is not None
    # one pixel per capture is below STRONG_CHANGE_MIN_PIXELS and the mean is far
    # below the threshold, so the tier abstains on a NON-identical pair
    assert result.verification.capture_provenance is not None
    assert (
        result.verification.capture_provenance.before_sha256
        != result.verification.capture_provenance.after_sha256
    )
    assert result.verification.outcome == "uncertain", result.verification.note
    assert phases.count("post_action") == 1, phases  # NO extra capture

    backend2 = NoiseBackend()
    result2, phases2 = _run(backend2, _type())
    assert result2.verification.capture_provenance is not None
    assert (
        result2.verification.capture_provenance.before_sha256
        != result2.verification.capture_provenance.after_sha256
    )
    assert phases2.count("post_action") == 1, phases2  # repaint-prone, but non-identical


def test_no_stated_effect_and_a_real_repaint_still_verifies_in_one_capture() -> None:
    """The effect-less click stays at the WP0 floor: a real transition verifies on the
    FIRST pair, so the conditioned re-capture never fires (zero extra captures)."""
    backend = _backend_class(0.0)()
    result, phases = _run(backend, _click())
    assert result.verification is not None
    assert result.verification.outcome == "verified"
    assert phases.count("post_action") == 1, phases
