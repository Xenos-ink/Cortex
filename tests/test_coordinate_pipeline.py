"""Composed coordinate-pipeline tests: exactly ONE screenshot-to-physical transform (F1).

E9 red-team probe 7 (HIGH finding F1) measured that in a verified ``scaled`` coordinate
space the DPI scale was applied TWICE: ``CoordinateGroundingStrategy`` rewrote the action
point into input space (screenshot * scale) and the backend then scaled again, so the
executed position was ``origin + screenshot * scale**2`` (e.g. a click at screenshot
(100, 200) @125% executed at (156, 312) instead of (125, 250)).

The fix establishes the single-transform invariant: grounding validates bounds in
screenshot space and RECORDS the verified scale (``normalized=True`` means "validated in
a verified scaled space; scale recorded" — the point is never rewritten), and the backend
(``_map_to_physical``) applies the ONLY screenshot-to-physical transform, exactly once at
execution. No test previously composed grounding with backend execution, which is why the
defect survived each layer's local tests; these tests compose the full
grounding -> validation -> execution path end to end.

Rounding policy (pinned consistently in every scaled test here): the backend rounds
``origin + screenshot * scale`` to whole pixels with Python round-half-even
(``CoordinateTransform.to_physical`` uses ``round(...)``). Assertions require BOTH exact
equality with that policy AND a <= 0.5 px tolerance against the ideal real-valued
position ``origin + screenshot * scale``.

Patterns mirror tests/test_controller_integration.py: the server tool surface with
monkeypatched ``_backend_factory``/``_provider_factory`` seams and a scripted provider
that decides a click; ``FakeComputerBackend`` derivatives record the EXECUTED physical
input position (``_cursor_physical``, the GetCursorPos convention) which is asserted
against ``origin + screenshot * scale`` at 125%, 150%, and on a negative-origin secondary
monitor. Fail-closed and passthrough cases round out the matrix.
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import pytest
from PIL import Image

from computer_use_mcp import server
from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.grounding import GroundingRouter
from computer_use_mcp.models import (
    AgentDecision,
    CoordinateSpace,
    GroundedAction,
    MonitorInfo,
    Observation,
    SessionState,
)
from computer_use_mcp.state import SessionRegistry
from computer_use_mcp.validator import GroundingValidator

FAST_LIMITS = {"min_screenshot_interval_ms": 0}


# --- fakes (mirroring the test_controller_integration.py patterns) ---------------------------


def _png(color: str, width: int, height: int) -> str:
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _click(x: int, y: int, expected_change: str | None = None) -> AgentDecision:
    return AgentDecision(
        status="action",
        action=GroundedAction(
            action="click",
            point={"x": x, "y": y},
            confidence=1.0,
            expected_effect=expected_change,
        ),
    )


class _ScriptedProvider:
    """Minimal provider implementing the pinned ``decide_full`` surface with a script.

    Decisions are returned verbatim (the agent accepts a bare ``AgentDecision``); when
    the script is exhausted the LAST entry repeats, so recovery paths always reach a
    terminal ``done`` decision.
    """

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.decide_calls = 0
        self._index = 0

    async def decide_full(self, goal: str, observation: Any, history: list[str]) -> Any:
        self.decide_calls += 1
        if self._index >= len(self.script):
            decision = self.script[-1]
        else:
            decision = self.script[self._index]
            self._index += 1
        return decision


class _FlippingScaledBackend(FakeComputerBackend):
    """FakeComputerBackend whose screenshot flips color on every completed execute.

    Mirrors ``ScriptedBackend`` from test_controller_integration.py so the post-action
    observation differs from the pre-action baseline and the composed click verifies
    cleanly (visual-change intent) without polluting the executed-action count.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._executes = 0

    def observe(self) -> Observation:
        observation = super().observe()
        if self._executes % 2 == 1:
            observation.image_base64 = _png("black", self.width, self.height)
        return observation

    def execute(self, action: GroundedAction, stop: Any = None) -> str:
        message = super().execute(action, stop)
        self._executes += 1
        return message


# --- session helpers ---------------------------------------------------------------------------


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
    backend: Any,
    provider: Any,
    **start_kwargs: Any,
) -> tuple[str, Any, Any, Any]:
    """Start one session through the server tool with injected fake backend/provider."""
    monkeypatch.setattr(server, "_backend_factory", lambda: backend)
    monkeypatch.setattr(server, "_provider_factory", lambda: provider)
    response = server.start_session(**start_kwargs)
    assert response.get("session_id"), response
    session_id = str(response["session_id"])
    bundle = server._get_bundle(session_id)
    return session_id, bundle, backend, provider


def assert_single_transform(
    backend: FakeComputerBackend,
    screenshot_point: tuple[int, int],
    monitor_origin: tuple[int, int],
    scale: float,
) -> None:
    """Assert the EXECUTED physical position is origin + screenshot * scale, once.

    Rounding policy (see module docstring): the backend rounds ``origin + screenshot *
    scale`` with Python round-half-even; the executed position must equal that rounded
    value exactly AND stay within 0.5 px of the ideal real-valued position.
    """
    executed = backend._cursor_physical
    assert executed is not None, "the fake backend recorded no executed physical input"
    ideal_x = monitor_origin[0] + screenshot_point[0] * scale
    ideal_y = monitor_origin[1] + screenshot_point[1] * scale
    assert executed == (round(ideal_x), round(ideal_y))
    assert abs(executed[0] - ideal_x) <= 0.5
    assert abs(executed[1] - ideal_y) <= 0.5


def executed_points(backend: FakeComputerBackend) -> list[tuple[str, tuple[int, int] | None]]:
    return [
        (
            action.action.value,
            None if action.point is None else (action.point.x, action.point.y),
        )
        for action in backend.executed
        if action.action.value in {"click", "double_click"}
    ]


def audit_events(bundle: Any, session_id: str) -> list[dict[str, Any]]:
    path = bundle.auditor.path_for(session_id)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --- composed scaled-space executions (the gap E9 called out) --------------------------------


async def test_scaled_125_percent_executes_origin_plus_screenshot_times_scale_once(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """125% DPI (screenshot 1536x864, input 1920x1080): a click decided at screenshot
    (100, 200) must execute at (125, 250) — origin + screenshot * scale applied ONCE.
    Before the F1 fix this executed at (156, 312) (scale squared)."""
    monitor = MonitorInfo(
        id="m", index=0, bounds=(0, 0, 1920, 1080), is_primary=True,
        dpi_scale_x=1.25, dpi_scale_y=1.25,
    )
    backend = _FlippingScaledBackend(width=1536, height=864, monitors=[monitor])
    provider = _ScriptedProvider(
        [_click(100, 200, expected_change="the screen changes"), AgentDecision(status="done")]
    )
    session_id, _bundle, backend, _provider = make_session(
        monkeypatch, backend=backend, provider=provider,
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    response = await server.run_goal(session_id, "click the target")

    assert response["ok"] is True
    assert response["termination_reason"] == "completed"
    # The recorded action point stays in SCREENSHOT space end-to-end (grounding records
    # the scale; it never rewrites the point).
    assert executed_points(backend) == [("click", (100, 200))]
    assert_single_transform(backend, screenshot_point=(100, 200), monitor_origin=(0, 0), scale=1.25)
    assert backend._cursor_physical == (125, 250)


async def test_scaled_150_percent_executes_origin_plus_screenshot_times_scale_once(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """150% DPI (screenshot 1280x720, input 1920x1080): center click at screenshot
    (640, 360) executes at (960, 540) — NOT (1440, 810) as the scale-squared bug did."""
    monitor = MonitorInfo(
        id="m", index=0, bounds=(0, 0, 1920, 1080), is_primary=True,
        dpi_scale_x=1.5, dpi_scale_y=1.5,
    )
    backend = _FlippingScaledBackend(width=1280, height=720, monitors=[monitor])
    provider = _ScriptedProvider(
        [_click(640, 360, expected_change="the screen changes"), AgentDecision(status="done")]
    )
    session_id, _bundle, backend, _provider = make_session(
        monkeypatch, backend=backend, provider=provider,
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    response = await server.run_goal(session_id, "click the center")

    assert response["ok"] is True
    assert response["termination_reason"] == "completed"
    assert executed_points(backend) == [("click", (640, 360))]
    assert_single_transform(backend, screenshot_point=(640, 360), monitor_origin=(0, 0), scale=1.5)
    assert backend._cursor_physical == (960, 540)


async def test_negative_origin_secondary_monitor_executes_origin_plus_screenshot_times_scale_once(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative-origin secondary monitor at 125%: the single transform must apply the
    monitor origin exactly once — screenshot (960, 540) executes at (-720, -405),
    matching origin + screenshot * scale including both negative offsets."""
    monitor = MonitorInfo(
        id="m", index=0, bounds=(-1920, -1080, 1920, 1080), is_primary=True,
        dpi_scale_x=1.25, dpi_scale_y=1.25,
    )
    backend = _FlippingScaledBackend(width=1536, height=864, monitors=[monitor])
    provider = _ScriptedProvider(
        [_click(960, 540, expected_change="the screen changes"), AgentDecision(status="done")]
    )
    session_id, _bundle, backend, _provider = make_session(
        monkeypatch, backend=backend, provider=provider,
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    response = await server.run_goal(session_id, "click the secondary monitor")

    assert response["ok"] is True
    assert response["termination_reason"] == "completed"
    assert executed_points(backend) == [("click", (960, 540))]
    assert_single_transform(backend, screenshot_point=(960, 540), monitor_origin=(-1920, -1080), scale=1.25)
    assert backend._cursor_physical == (-720, -405)


# --- fail-closed and passthrough ends of the matrix -------------------------------------------


async def test_unverifiable_coordinate_space_refuses_end_to_end_fail_closed(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E9 probe 7b composed end to end: a screenshot/input ratio that matches no known
    DPI (1920/1000 = 1.92 vs 1.5) is UNVERIFIABLE, grounding refuses fail-closed, and
    NOTHING executes — zero backend inputs, no cursor movement."""
    monitor = MonitorInfo(
        id="m", index=0, bounds=(0, 0, 1920, 1080), is_primary=True,
        dpi_scale_x=1.25, dpi_scale_y=1.25,
    )
    backend = _FlippingScaledBackend(width=1000, height=720, monitors=[monitor])
    provider = _ScriptedProvider(
        [_click(100, 200, expected_change="the screen changes"), AgentDecision(status="done")]
    )
    session_id, bundle, backend, _provider = make_session(
        monkeypatch, backend=backend, provider=provider,
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    response = await server.run_goal(session_id, "click on an unverifiable screen")

    assert backend.executed == []  # fail closed: zero physical inputs
    assert backend._cursor_physical is None  # the cursor never moved
    assert response["termination_reason"] == "completed"  # refusal recovered -> scripted done
    grounding_failures = [
        event for event in audit_events(bundle, session_id)
        if event["event_type"] == "grounding" and event.get("result") == "failed"
    ]
    assert grounding_failures, "the unverifiable-space refusal must be audited"
    assert "unverifiable" in json.dumps(grounding_failures[0].get("metadata", {}))


async def test_passthrough_scale_one_applies_no_transform(
    fresh_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verified passthrough space (scale = 1): the executed physical position equals the
    screenshot point exactly — no origin offset beyond (0, 0), no scaling."""
    backend = _FlippingScaledBackend(width=1280, height=720)  # default monitor matches
    observation_probe = backend.observe()
    assert observation_probe.coordinate_space is CoordinateSpace.VERIFIED_PASSTHROUGH
    provider = _ScriptedProvider(
        [_click(300, 400, expected_change="the screen changes"), AgentDecision(status="done")]
    )
    session_id, _bundle, backend, _provider = make_session(
        monkeypatch, backend=backend, provider=provider,
        dry_run=False, require_approval=False, limits=FAST_LIMITS,
    )
    response = await server.run_goal(session_id, "click passthrough")

    assert response["ok"] is True
    assert response["termination_reason"] == "completed"
    assert executed_points(backend) == [("click", (300, 400))]
    assert backend._cursor_physical == (300, 400)


# --- direct composed unit: grounding -> validation -> execute ---------------------------------


def test_ground_validate_execute_composes_into_a_single_transform() -> None:
    """Unit-level composition of the exact pipeline phases, without the server: ground a
    scaled-space click, validate it against a fresh observation, execute it, and confirm
    the executed physical position is origin + screenshot * scale applied exactly once
    while the recorded point remains the screenshot-space decision point."""
    monitor = MonitorInfo(
        id="m", index=0, bounds=(0, 0, 1920, 1080), is_primary=True,
        dpi_scale_x=1.25, dpi_scale_y=1.25,
    )
    backend = FakeComputerBackend(width=1536, height=864, monitors=[monitor])
    observation = backend.observe()
    assert observation.coordinate_space is CoordinateSpace.SCALED
    assert observation.coordinate_scale_x == pytest.approx(1.25)

    action = GroundedAction(
        action="click",
        point={"x": 100, "y": 200},
        confidence=1.0,
        source_observation_id=observation.observation_id,
    )
    grounding = GroundingRouter().route(action, observation)
    action.grounding = grounding
    assert grounding.normalized is True  # scale recorded (validated scaled space)
    assert (action.point.x, action.point.y) == (100, 200)  # point never rewritten

    outcome = GroundingValidator().validate(
        action, observation, SessionState(session_id="s", min_confidence=0.0),
        current_observation=backend.observe(),
    )
    assert outcome.valid is True

    backend.execute(action)
    assert_single_transform(backend, screenshot_point=(100, 200), monitor_origin=(0, 0), scale=1.25)
    assert backend._cursor_physical == (125, 250)
    assert (action.point.x, action.point.y) == (100, 200)  # still the screenshot-space point
