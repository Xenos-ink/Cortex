"""Wave 2 tests: grounding strategies, validator staleness/allowlists, semantic verification.

Covers the Wave-2/P0 contract:
- every grounding + verification strategy: positive, negative, and cannot-determine paths;
- the UNCERTAIN-is-never-verified invariant (property-style sweep with insufficient data);
- staleness/observation-binding rejection (P0-H) across every identity dimension;
- coordinate grounding scale recording at 125% DPI (screenshot 1536x864 vs input
  1920x1080; the point is never rewritten — the backend applies the single
  screenshot-to-physical transform at execution);
- router selection, explicit hints, and fail-closed UnsupportedGroundingError paths;
- legacy compare() compatibility and the process-allowlist upgrade (P0-G).
"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from computer_use_mcp.grounding import (
    AccessibilityGroundingStrategy,
    CoordinateGroundingStrategy,
    GroundingRouter,
    RegionGroundingStrategy,
    TextAnchorGroundingStrategy,
    UnsupportedGroundingError,
)
from computer_use_mcp.models import (
    CoordinateSpace,
    GroundedAction,
    MonitorInfo,
    Observation,
    SessionState,
    TextRegion,
    WindowInfo,
)
from computer_use_mcp.validator import (
    GroundingValidator,
    ProcessNotAllowedError,
    StaleObservationError,
)
from computer_use_mcp.verification import (
    DEFAULT_STRATEGY_CHAIN,
    DeterministicPredicateStrategy,
    ModelVisualStrategy,
    ProcessStateStrategy,
    ScreenshotDiffStrategy,
    TextPredicateStrategy,
    VerificationEngine,
    VerificationIntent,
    VerificationKind,
    WindowStateStrategy,
)


def _png(color: str, width: int = 64, height: int = 48) -> str:
    image = Image.new("RGB", (width, height), color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def _observation(
    color: str = "white",
    width: int = 64,
    height: int = 48,
    *,
    window: WindowInfo | None = None,
    active_window: str | None = None,
    monitor: MonitorInfo | None = None,
    ocr: list[TextRegion] | None = None,
    ui_elements: list[object] | None = None,
    input_width: int | None = None,
    input_height: int | None = None,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    coordinate_space: CoordinateSpace | None = None,
) -> Observation:
    kwargs: dict[str, object] = {}
    if coordinate_space is not None:
        kwargs["coordinate_space"] = coordinate_space
    return Observation(
        image_base64=_png(color, width, height),
        width=width,
        height=height,
        active_window=active_window,
        monitor=monitor,
        active_window_info=window,
        ocr_text=ocr,
        ui_elements=ui_elements,
        input_width=input_width,
        input_height=input_height,
        coordinate_scale_x=scale_x,
        coordinate_scale_y=scale_y,
        **kwargs,  # type: ignore[arg-type]
    )


def _window(
    hwnd: int = 42,
    pid: int = 7,
    process_name: str | None = "notepad.exe",
    title: str = "Untitled - Notepad",
    bounds: tuple[int, int, int, int] | None = (0, 0, 400, 300),
) -> WindowInfo:
    return WindowInfo(hwnd=hwnd, pid=pid, process_name=process_name, title=title, bounds=bounds)


# --- grounding: coordinate strategy -------------------------------------------------------

def test_coordinate_grounding_passthrough_records_decision_confidence() -> None:
    observation = _observation(width=100, height=80)
    action = GroundedAction(action="click", point={"x": 40, "y": 30}, confidence=0.82)
    result = CoordinateGroundingStrategy().ground(action, observation)
    assert result.strategy == "coordinate"
    assert result.confidence == 0.82  # decision confidence passed through, not invented
    assert result.normalized is False
    assert "not a grounding quality score" in (result.notes or "")
    assert action.point is not None and (action.point.x, action.point.y) == (40, 30)


def test_coordinate_grounding_records_scale_without_rewriting_point_at_125_percent_dpi() -> None:
    """Single-transform invariant (F1 fix): in a verified scaled space grounding validates
    bounds and RECORDS the scale (normalized=True) but never rewrites the point; the
    backend applies the one screenshot-to-physical transform at execution."""
    # 125% DPI: the model sees the 1536x864 screenshot; input space is 1920x1080.
    observation = _observation(
        width=1536,
        height=864,
        input_width=1920,
        input_height=1080,
        scale_x=1.25,
        scale_y=1.25,
        coordinate_space=CoordinateSpace.SCALED,
    )
    action = GroundedAction(action="click", point={"x": 768, "y": 432}, confidence=0.9)
    result = CoordinateGroundingStrategy().ground(action, observation)
    assert result.normalized is True  # semantics: validated in a scaled space; scale recorded
    assert action.point is not None and (action.point.x, action.point.y) == (768, 432)  # NOT rewritten
    assert any("1.25" in item for item in result.evidence)
    assert any("recorded" in item for item in result.evidence)
    assert "stays in screenshot space" in (result.notes or "")
    assert "exactly once at execution" in (result.notes or "")


def test_coordinate_grounding_bounds_checks_screenshot_and_input_space() -> None:
    observation = _observation(width=100, height=80)
    out_of_bounds = GroundedAction(action="click", point={"x": 150, "y": 10}, confidence=0.9)
    with pytest.raises(UnsupportedGroundingError):
        CoordinateGroundingStrategy().ground(out_of_bounds, observation)
    negative = GroundedAction(action="click", point={"x": -5, "y": 10}, confidence=0.9)
    with pytest.raises(UnsupportedGroundingError):
        CoordinateGroundingStrategy().ground(negative, observation)


def test_coordinate_grounding_requires_point_and_verifiable_space() -> None:
    observation = _observation(width=100, height=80)
    with pytest.raises(UnsupportedGroundingError):
        CoordinateGroundingStrategy().ground(GroundedAction(action="click", confidence=0.9), observation)
    unverifiable = _observation(width=100, height=80, coordinate_space=CoordinateSpace.UNVERIFIABLE)
    action = GroundedAction(action="click", point={"x": 5, "y": 5}, confidence=0.9)
    with pytest.raises(UnsupportedGroundingError):
        CoordinateGroundingStrategy().ground(action, unverifiable)


# --- grounding: semantic strategies (OCR / accessibility / region) -------------------------

def test_text_anchor_resolves_region_center() -> None:
    ocr = [TextRegion(text="Cancel", x=0, y=0, width=40, height=10), TextRegion(text="Save", x=10, y=20, width=50, height=12, confidence=0.9)]
    observation = _observation(ocr=ocr)
    action = GroundedAction(action="click", confidence=0.7)
    result = TextAnchorGroundingStrategy().ground(action, observation, target="Save")
    assert action.point is not None and (action.point.x, action.point.y) == (35, 26)
    assert result.confidence == 0.9
    assert result.strategy == "text_anchor"
    assert any("exact" in item for item in result.evidence)


def test_text_anchor_raises_without_ocr_or_match() -> None:
    strategy = TextAnchorGroundingStrategy()
    with pytest.raises(UnsupportedGroundingError):
        strategy.ground(GroundedAction(action="click", confidence=0.7), _observation(), target="Save")
    ocr = [TextRegion(text="Cancel", x=0, y=0, width=40, height=10)]
    with pytest.raises(UnsupportedGroundingError):
        strategy.ground(GroundedAction(action="click", confidence=0.7), _observation(ocr=ocr), target="Save")
    with pytest.raises(UnsupportedGroundingError):
        strategy.ground(GroundedAction(action="click", confidence=0.7), _observation(ocr=ocr), target=None)


def test_accessibility_grounding_finds_element_by_name() -> None:
    elements = [
        {"role": "button", "name": "Cancel", "bounds": (0, 0, 40, 10)},
        {"role": "button", "name": "Save", "bounds": (100, 200, 80, 24)},
    ]
    observation = _observation(ui_elements=elements)
    action = GroundedAction(action="click", confidence=0.7)
    result = AccessibilityGroundingStrategy().ground(action, observation, target="Save")
    assert action.point is not None and (action.point.x, action.point.y) == (140, 212)
    assert result.strategy == "accessibility"
    assert any("role=button" in item for item in result.evidence)


def test_accessibility_grounding_raises_without_elements_or_match_or_bounds() -> None:
    strategy = AccessibilityGroundingStrategy()
    with pytest.raises(UnsupportedGroundingError):
        strategy.ground(GroundedAction(action="click", confidence=0.7), _observation(), target="Save")
    elements = [{"role": "button", "name": "Cancel", "bounds": (0, 0, 40, 10)}]
    with pytest.raises(UnsupportedGroundingError):
        strategy.ground(GroundedAction(action="click", confidence=0.7), _observation(ui_elements=elements), target="Save")
    no_bounds = [{"role": "button", "name": "Save"}]
    with pytest.raises(UnsupportedGroundingError):
        strategy.ground(GroundedAction(action="click", confidence=0.7), _observation(ui_elements=no_bounds), target="Save")


def test_region_grounding_resolves_descriptor_center() -> None:
    observation = _observation(width=640, height=480)
    action = GroundedAction(action="click", confidence=0.7)
    result = RegionGroundingStrategy().ground(action, observation, target="100,200,80,24")
    assert action.point is not None and (action.point.x, action.point.y) == (140, 212)
    assert result.strategy == "region"
    with pytest.raises(UnsupportedGroundingError):
        RegionGroundingStrategy().ground(GroundedAction(action="click", confidence=0.7), observation, target=None)


# --- grounding: router ---------------------------------------------------------------------

def test_router_default_prefers_coordinates_and_semantic_capability() -> None:
    router = GroundingRouter()
    # Point present -> coordinate strategy.
    coordinate_action = GroundedAction(action="click", point={"x": 5, "y": 5}, confidence=0.8)
    result = router.route(coordinate_action, _observation(width=100, height=80))
    assert result.strategy == "coordinate"

    # No point but OCR data and a target -> text anchor.
    ocr = [TextRegion(text="Save", x=10, y=20, width=50, height=12)]
    text_action = GroundedAction(action="click", confidence=0.8)
    result = router.route(text_action, _observation(ocr=ocr), target="Save")
    assert result.strategy == "text_anchor"

    # No point, no data, spatial action -> fail-closed with the generic router message.
    with pytest.raises(UnsupportedGroundingError) as excinfo:
        router.route(GroundedAction(action="click", confidence=0.8), _observation())
    assert excinfo.value.strategy == "router"
    assert "no point" in excinfo.value.message

    # Non-spatial action without point/target -> trivial grounding.
    wait_result = router.route(GroundedAction(action="wait", delta=1, confidence=0.8), _observation())
    assert wait_result.strategy == "none"


def test_router_accurate_refusal_for_point_bearing_action_on_unverifiable_space() -> None:
    """E6 finding D5: point-bearing actions refused for coordinate-space reasons must get
    the coordinate strategy's accurate refusal, not the generic 'no point' router message."""
    router = GroundingRouter()
    unverifiable = _observation(width=100, height=80, coordinate_space=CoordinateSpace.UNVERIFIABLE)
    action = GroundedAction(action="click", point={"x": 40, "y": 30}, confidence=0.8)
    with pytest.raises(UnsupportedGroundingError) as excinfo:
        router.route(action, unverifiable)
    assert excinfo.value.strategy == "coordinate"
    assert "unverifiable" in excinfo.value.message
    assert "coordinate spaces are unverifiable" in excinfo.value.message
    assert "no point" not in excinfo.value.message


def test_router_hint_is_authoritative_and_never_silently_falls_back() -> None:
    router = GroundingRouter()
    ocr = [TextRegion(text="Save", x=10, y=20, width=50, height=12)]
    action = GroundedAction(action="click", point={"x": 5, "y": 5}, confidence=0.8)
    result = router.route(action, _observation(ocr=ocr), strategy_hint="text_anchor", target="Save")
    assert result.strategy == "text_anchor"
    assert action.point is not None and (action.point.x, action.point.y) == (35, 26)

    # An explicit hint on an observation without the required data raises — it must NOT
    # fall back to the coordinate strategy even though the action carries a valid point.
    with pytest.raises(UnsupportedGroundingError) as excinfo:
        router.route(action, _observation(width=100, height=80), strategy_hint="accessibility", target="Save")
    assert excinfo.value.strategy == "accessibility"

    with pytest.raises(UnsupportedGroundingError):
        router.route(action, _observation(), strategy_hint="does_not_exist")


# --- validator: staleness / observation binding (P0-H) --------------------------------------

def _source_and_current(**overrides: object) -> tuple[Observation, Observation]:
    defaults: dict[str, object] = {
        "width": 100,
        "height": 80,
        "window": _window(),
        "active_window": "Untitled - Notepad",
        "monitor": MonitorInfo(id="\\\\.\\DISPLAY1", index=0, bounds=(0, 0, 100, 80)),
    }
    current_kwargs = dict(defaults)
    current_kwargs.update(overrides)
    source = _observation(**defaults)  # type: ignore[arg-type]
    current = _observation(**current_kwargs)  # type: ignore[arg-type]
    return source, current


def _bound_click(observation: Observation) -> GroundedAction:
    return GroundedAction(
        action="click",
        point={"x": 10, "y": 10},
        confidence=0.9,
        source_observation_id=observation.observation_id,
    )


def test_validator_rejects_coordinate_action_missing_observation_binding() -> None:
    source, current = _source_and_current()
    action = GroundedAction(action="click", point={"x": 10, "y": 10}, confidence=0.9)
    outcome = GroundingValidator().validate(action, source, None, current_observation=current)
    assert outcome.valid is False
    assert "missing_observation_binding" in outcome.codes


def test_validator_legacy_call_shape_skips_binding_enforcement() -> None:
    source, _ = _source_and_current()
    action = GroundedAction(action="click", point={"x": 10, "y": 10}, confidence=0.9)
    outcome = GroundingValidator().validate(action, source, SessionState(session_id="s", min_confidence=0.0))
    assert outcome.valid is True  # legacy callers without staleness flow are unaffected
    assert "missing_observation_binding" not in outcome.codes


def test_validator_detects_every_staleness_dimension() -> None:
    validator = GroundingValidator()
    cases: list[tuple[dict[str, object], str]] = [
        ({"window": _window(hwnd=99)}, "active_window_hwnd"),
        ({"window": _window(pid=8)}, "active_process"),
        ({"window": _window(process_name="explorer.exe")}, "active_process"),
        ({"width": 200, "height": 100}, "screenshot_dimensions"),
        (
            {
                "monitor": MonitorInfo(id="\\\\.\\DISPLAY2", index=1, bounds=(0, 0, 100, 80)),
                "window": _window(),
                "active_window": "Untitled - Notepad",
            },
            "monitor_identity",
        ),
        (
            {
                "monitor": MonitorInfo(id="\\\\.\\DISPLAY1", index=0, bounds=(0, 0, 120, 90)),
                "window": _window(),
                "active_window": "Untitled - Notepad",
            },
            "monitor_bounds",
        ),
        ({"coordinate_space": CoordinateSpace.UNVERIFIABLE}, "coordinate_space"),
        ({"window": None, "active_window": "Other Window"}, "active_window_title"),
    ]
    for overrides, expected_detail in cases:
        source, current = _source_and_current(**overrides)
        outcome = validator.validate(_bound_click(source), source, None, current_observation=current)
        assert outcome.valid is False, f"expected staleness rejection for {overrides}"
        assert "STALE_OBSERVATION" in outcome.codes
        error = outcome.error
        assert isinstance(error, StaleObservationError)
        assert error.reason == "STALE_OBSERVATION"
        assert error.detail == expected_detail


def test_validator_detects_binding_mismatch() -> None:
    source, current = _source_and_current()
    action = GroundedAction(
        action="click", point={"x": 10, "y": 10}, confidence=0.9, source_observation_id="other-observation"
    )
    outcome = GroundingValidator().validate(action, source, None, current_observation=current)
    assert outcome.valid is False
    assert "observation_binding_mismatch" in outcome.codes


def test_validator_accepts_fresh_identical_observation() -> None:
    source, current = _source_and_current()
    outcome = GroundingValidator().validate(_bound_click(source), source, None, current_observation=current)
    assert outcome.valid is True
    assert outcome.codes == []
    assert outcome.error is None
    assert outcome.ok is True


# --- validator: allowlists (P0-G) and legacy behavior ----------------------------------------

def test_validator_process_allowlist_blocks_foreign_process() -> None:
    source, current = _source_and_current()
    outcome = GroundingValidator().validate(
        _bound_click(source),
        source,
        None,
        current_observation=current,
        allowed_processes=["explorer.exe"],
    )
    assert outcome.valid is False
    assert "process_not_allowed" in outcome.codes
    assert isinstance(outcome.error, ProcessNotAllowedError)


def test_validator_process_allowlist_accepts_matching_process_names() -> None:
    source, current = _source_and_current()
    for allowed in (["notepad.exe"], ["Notepad"], ["C:\\Windows\\notepad.exe"], ["notepad"]):
        outcome = GroundingValidator().validate(
            _bound_click(source), source, None, current_observation=current, allowed_processes=allowed
        )
        assert outcome.valid is True, f"process allowlist {allowed} should accept notepad.exe"


def test_validator_process_allowlist_fail_closed_without_identity() -> None:
    observation = _observation(width=100, height=80)  # no WindowInfo at all
    action = GroundedAction(action="click", point={"x": 10, "y": 10}, confidence=0.9)
    outcome = GroundingValidator().validate(
        action, observation, None, allowed_processes=["notepad.exe"]
    )
    assert outcome.valid is False
    assert "process_identity_unavailable" in outcome.codes


def test_validator_title_allowlist_legacy_substring_and_exact_window_title() -> None:
    validator = GroundingValidator()
    state = SessionState(session_id="s", min_confidence=0.0, allowed_windows=["Notepad"])
    # Legacy path: substring match against observation.active_window.
    legacy_observation = _observation(width=100, height=80, active_window="Untitled - Notepad")
    action = GroundedAction(action="click", point={"x": 10, "y": 10}, confidence=0.9)
    assert validator.validate(action, legacy_observation, state).valid is True
    # WindowInfo available: exact title match preferred (also accepted).
    info_observation = _observation(
        width=100, height=80, window=_window(title="Untitled - Notepad"), active_window="Untitled - Notepad"
    )
    assert validator.validate(action, info_observation, state).valid is True
    foreign = _observation(
        width=100, height=80, window=_window(title="Calculator"), active_window="Calculator"
    )
    outcome = validator.validate(action, foreign, state)
    assert outcome.valid is False and "window_not_allowed" in outcome.codes


def test_validator_preserves_legacy_reason_messages_and_codes() -> None:
    validator = GroundingValidator()
    state = SessionState(session_id="s", min_confidence=0.5)
    observation = _observation(width=100, height=80)
    action = GroundedAction(action="click", point={"x": 100, "y": 20}, confidence=0.4)
    outcome = validator.validate(action, observation, state)
    assert outcome.valid is False
    assert "Confidence 0.40 is below 0.50." in outcome.reasons[0]
    assert "confidence_below_floor" in outcome.codes
    assert "outside" in outcome.reasons[1]
    assert "point_out_of_bounds" in outcome.codes

    unverifiable = Observation(
        image_base64=observation.image_base64,
        width=1920,
        height=1080,
        input_width=1536,
        input_height=864,
        coordinate_scale_x=0.8,
        coordinate_scale_y=0.8,
        coordinate_space_verified=False,
    )
    outcome = validator.validate(
        GroundedAction(action="click", point={"x": 100, "y": 100}, confidence=1.0), unverifiable, state
    )
    assert outcome.valid is False
    assert "coordinate spaces differ" in outcome.reasons[0]
    assert "coordinate_space_unverifiable" in outcome.codes


def test_validator_accepts_grounded_screenshot_point_in_scaled_space() -> None:
    # 125% DPI with a verified scaled space: the grounded point stays in screenshot space
    # (single-transform invariant — grounding records the scale, never rewrites) and the
    # action passes validation unchanged.
    observation = Observation(
        image_base64=_png("white", 1536, 864),
        width=1536,
        height=864,
        input_width=1920,
        input_height=1080,
        coordinate_scale_x=1.25,
        coordinate_scale_y=1.25,
        coordinate_space=CoordinateSpace.SCALED,
    )
    action = GroundedAction(action="click", point={"x": 768, "y": 432}, confidence=0.9)
    action.grounding = CoordinateGroundingStrategy().ground(action, observation)
    assert action.grounding.normalized is True
    assert action.point is not None and (action.point.x, action.point.y) == (768, 432)  # NOT rewritten
    outcome = GroundingValidator().validate(action, observation, SessionState(session_id="s", min_confidence=0.0))
    assert outcome.valid is True


# --- verification: strategies ----------------------------------------------------------------

def test_screenshot_diff_expected_change_true() -> None:
    before = _observation("white")
    after = _observation("black")
    result = ScreenshotDiffStrategy().verify(
        VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=True), before, after
    )
    assert result.outcome == "verified" and result.changed is True
    failed = ScreenshotDiffStrategy().verify(
        VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=True), before, _observation("white")
    )
    assert failed.outcome == "failed" and failed.changed is False


def test_screenshot_diff_expected_change_false_requires_stability() -> None:
    before = _observation("white")
    strategy = ScreenshotDiffStrategy()
    stable = strategy.verify(
        VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=False), before, _observation("white")
    )
    assert stable.outcome == "verified"
    changed = strategy.verify(
        VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=False), before, _observation("black")
    )
    assert changed.outcome == "failed" and changed.changed is True


def test_screenshot_diff_dimensions_change_is_failed_legacy_compatible() -> None:
    before = _observation("white", 64, 48)
    after = _observation("white", 32, 24)
    result = ScreenshotDiffStrategy().verify(
        VerificationIntent(kind=VerificationKind.VISUAL_CHANGE), before, after
    )
    assert result.outcome == "failed"
    assert result.changed is True
    assert result.confidence == 0.5
    assert result.note == "Screen dimensions changed; semantic verification is required."


def test_screenshot_diff_supporting_role_for_semantic_intents() -> None:
    before = _observation("white")
    after = _observation("white")
    result = ScreenshotDiffStrategy().verify(
        VerificationIntent(kind=VerificationKind.EXPECTED_TEXT, expected_text="Save", expected_change=True),
        before,
        after,
    )
    assert result.outcome == "failed"  # pixel-identical screen falsifies any expected change
    abstain = ScreenshotDiffStrategy().verify(
        VerificationIntent(kind=VerificationKind.EXPECTED_TEXT, expected_text="Save"), before, _observation("black")
    )
    assert abstain.outcome == "uncertain"  # pixels alone cannot prove text semantics


def test_window_state_strategy_matrix() -> None:
    strategy = WindowStateStrategy()
    before = _observation(window=_window(title="Calculator"), active_window="Calculator")
    verified = strategy.verify(
        VerificationIntent(kind=VerificationKind.WINDOW_STATE, expected_window_title="Notepad"),
        before,
        _observation(window=_window(title="Untitled - Notepad"), active_window="Untitled - Notepad"),
    )
    assert verified.outcome == "verified"
    equals = strategy.verify(
        VerificationIntent(
            kind=VerificationKind.WINDOW_STATE, expected_window_title="notepad", window_title_match="equals"
        ),
        before,
        _observation(window=_window(title="Notepad"), active_window="Notepad"),
    )
    assert equals.outcome == "verified"
    failed = strategy.verify(
        VerificationIntent(kind=VerificationKind.WINDOW_STATE, expected_window_title="Notepad"), before, before
    )
    assert failed.outcome == "failed"
    uncertain = strategy.verify(
        VerificationIntent(kind=VerificationKind.WINDOW_STATE, expected_window_title="Notepad"), before, _observation()
    )
    assert uncertain.outcome == "uncertain"
    moved = strategy.verify(
        VerificationIntent(
            kind=VerificationKind.WINDOW_STATE,
            expected_window_title="Notepad",
            require_bounds_change=True,
        ),
        before,
        _observation(window=_window(title="Untitled - Notepad", bounds=(10, 10, 400, 300))),
    )
    assert moved.outcome == "verified"
    not_moved = strategy.verify(
        VerificationIntent(
            kind=VerificationKind.WINDOW_STATE,
            expected_window_title="Calculator",
            require_bounds_change=True,
        ),
        before,
        before,
    )
    assert not_moved.outcome == "failed"


def test_process_state_strategy_matrix() -> None:
    strategy = ProcessStateStrategy()
    before = _observation(window=_window(process_name="explorer.exe"), active_window="File Explorer")
    verified = strategy.verify(
        VerificationIntent(kind=VerificationKind.PROCESS_STATE, expected_process_name="notepad"),
        before,
        _observation(window=_window(process_name="notepad.exe"), active_window="Untitled - Notepad"),
    )
    assert verified.outcome == "verified"
    pid = strategy.verify(
        VerificationIntent(kind=VerificationKind.PROCESS_STATE, expected_pid=7),
        before,
        _observation(window=_window(pid=7)),
    )
    assert pid.outcome == "verified"
    failed = strategy.verify(
        VerificationIntent(kind=VerificationKind.PROCESS_STATE, expected_process_name="notepad"),
        before,
        _observation(window=_window(process_name="explorer.exe"), active_window="File Explorer"),
    )
    assert failed.outcome == "failed"
    uncertain = strategy.verify(
        VerificationIntent(kind=VerificationKind.PROCESS_STATE, expected_process_name="notepad"),
        before,
        _observation(active_window="Something"),
    )
    assert uncertain.outcome == "uncertain"


def test_deterministic_predicate_strategy_never_escalates_exceptions() -> None:
    strategy = DeterministicPredicateStrategy()

    def ok(before: Observation, after: Observation) -> bool | None:
        return before.width == after.width

    def broken(before: Observation, after: Observation) -> bool | None:
        raise RuntimeError("predicate exploded")

    def undecidable(before: Observation, after: Observation) -> bool | None:
        return None

    before, after = _observation(), _observation()
    assert strategy.verify(VerificationIntent(kind=VerificationKind.PREDICATE, predicate=ok), before, after).outcome == "verified"
    failed = strategy.verify(VerificationIntent(kind=VerificationKind.PREDICATE,
                                                predicate=lambda b, a: False,
                                                predicate_name="always_false"), before, after)
    assert failed.outcome == "failed" and failed.verification_method == "deterministic_predicate"
    assert strategy.verify(VerificationIntent(kind=VerificationKind.PREDICATE, predicate=undecidable), before, after).outcome == "uncertain"
    crashed = strategy.verify(VerificationIntent(kind=VerificationKind.PREDICATE, predicate=broken), before, after)
    assert crashed.outcome == "uncertain"
    assert any("predicate exploded" in item for item in crashed.evidence)
    assert strategy.verify(VerificationIntent(kind=VerificationKind.PREDICATE), before, after).outcome == "uncertain"


def test_text_predicate_strategy_matrix() -> None:
    strategy = TextPredicateStrategy()
    ocr = [TextRegion(text="Untitled - Notepad", x=0, y=0, width=100, height=12, confidence=0.95)]
    verified = strategy.verify(
        VerificationIntent(kind=VerificationKind.EXPECTED_TEXT, expected_text="notepad"),
        _observation(),
        _observation(ocr=ocr),
    )
    assert verified.outcome == "verified" and verified.confidence == 0.95
    absent = strategy.verify(
        VerificationIntent(kind=VerificationKind.EXPECTED_TEXT, expected_text="Calculator"),
        _observation(),
        _observation(ocr=ocr),
    )
    assert absent.outcome == "failed"
    no_ocr = strategy.verify(
        VerificationIntent(kind=VerificationKind.EXPECTED_TEXT, expected_text="Calculator"), _observation(), _observation()
    )
    assert no_ocr.outcome == "uncertain"


def test_model_visual_strategy_degrades_without_judge_and_never_trusts_garbage() -> None:
    strategy = ModelVisualStrategy()
    before, after = _observation("white"), _observation("black")
    intent = VerificationIntent(kind=VerificationKind.MODEL_JUDGE)
    assert strategy.verify(intent, before, after).outcome == "uncertain"

    class ExplodingJudge:
        def judge(self, intent: VerificationIntent, before_img: object, after_img: object) -> object:
            raise ValueError("judge down")

    assert ModelVisualStrategy(ExplodingJudge()).verify(intent, before, after).outcome == "uncertain"

    class GarbageJudge:
        def judge(self, intent: VerificationIntent, before_img: object, after_img: object) -> object:
            return "verified"

    assert ModelVisualStrategy(GarbageJudge()).verify(intent, before, after).outcome == "uncertain"

    class UncertainJudge:
        def judge(self, intent: VerificationIntent, before_img: object, after_img: object) -> object:
            from computer_use_mcp.models import VerificationResult

            return VerificationResult(outcome="uncertain", changed=True, note="judge unsure", confidence=0.4)

    result = ModelVisualStrategy(UncertainJudge()).verify(intent, before, after)
    assert result.outcome == "uncertain" and result.verified is False

    from computer_use_mcp.models import VerificationResult

    class RealJudge:
        def judge(self, intent: VerificationIntent, before_img: object, after_img: object) -> VerificationResult:
            return VerificationResult(outcome="verified", changed=True, note="dialog visible", confidence=0.8)

    enriched = ModelVisualStrategy(RealJudge()).verify(intent, before, after)
    assert enriched.outcome == "verified"
    assert enriched.verification_method == "model_visual"
    assert enriched.observation_id == after.observation_id


# --- verification: engine facade and invariants ----------------------------------------------

def test_engine_compare_is_backward_compatible_with_legacy_expectations() -> None:
    engine = VerificationEngine()
    before, after = _observation("white"), _observation("black")
    result = engine.compare(before, after, expected_change="window changes")
    assert result.changed is True
    assert result.verified is True  # mirrors tests/test_reliability.py expectations
    legacy_none_changed = engine.compare(before, after)
    assert legacy_none_changed.verified is True
    identical = engine.compare(before, _observation("white"))
    assert identical.outcome == "uncertain" and identical.verified is False
    dims = engine.compare(before, _observation("white", 32, 24))
    assert dims.verified is False and dims.changed is True and dims.confidence == 0.5


def test_engine_first_definitive_outcome_wins() -> None:
    engine = VerificationEngine()
    before = _observation(window=_window(title="Calculator"), active_window="Calculator")
    after = _observation(window=_window(title="Untitled - Notepad"), active_window="Untitled - Notepad")
    result = engine.verify(
        VerificationIntent(kind=VerificationKind.WINDOW_STATE, expected_window_title="Notepad"), before, after
    )
    assert result.outcome == "verified"
    assert result.verification_method == "window_state"
    assert result.observation_id == after.observation_id


def test_engine_combines_all_uncertain_evidence() -> None:
    engine = VerificationEngine()
    before, after = _observation("white"), _observation("white", ocr=None, active_window=None)
    result = engine.verify(
        VerificationIntent(kind=VerificationKind.EXPECTED_TEXT, expected_text="Save"), before, after
    )
    assert result.outcome == "uncertain"
    assert result.verified is False
    assert "screenshot_diff" in result.verification_method
    assert "text_predicate" in result.verification_method
    assert result.observation_id == after.observation_id
    assert result.evidence, "combined uncertain result must carry merged evidence"


def test_engine_custom_strategy_chain_override() -> None:
    engine = VerificationEngine()
    before, after = _observation("white"), _observation("black")
    only_diff = engine.verify(
        VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=True),
        before,
        after,
        strategies=[ScreenshotDiffStrategy()],
    )
    assert only_diff.outcome == "verified"
    assert only_diff.verification_method == "screenshot_diff"


def test_uncertain_is_never_verified_invariant_sweep() -> None:
    """Property-style sweep: with insufficient data every strategy must yield uncertain."""
    blank_before = _observation("white")
    blank_after = _observation("white", active_window=None, ocr=None, ui_elements=None)
    intents = [
        VerificationIntent(kind=VerificationKind.VISUAL_CHANGE),  # no expectation stated
        VerificationIntent(kind=VerificationKind.EXPECTED_TEXT, expected_text="Anything"),
        VerificationIntent(kind=VerificationKind.WINDOW_STATE, expected_window_title="Anything"),
        VerificationIntent(kind=VerificationKind.PROCESS_STATE, expected_process_name="anything.exe"),
        VerificationIntent(kind=VerificationKind.PREDICATE),  # no predicate supplied
        VerificationIntent(kind=VerificationKind.MODEL_JUDGE),  # no judge configured
    ]
    for intent in intents:
        for strategy in DEFAULT_STRATEGY_CHAIN:
            if not strategy.can_verify(intent):
                continue
            result = strategy.verify(intent, blank_before, blank_after)
            assert result.outcome in {"verified", "failed", "uncertain"}
            if result.outcome == "uncertain":
                assert result.verified is False, f"{strategy.name} mapped uncertain to success"
        engine = VerificationEngine()
        combined = engine.verify(intent, blank_before, blank_after)
        assert combined.outcome == "uncertain", f"engine collapsed uncertain for kind={intent.kind}"
        assert combined.verified is False


def test_engine_full_chain_end_to_end_semantic_verification() -> None:
    engine = VerificationEngine()
    before = _observation(window=_window(title="Blank"), active_window="Blank")
    after = _observation(
        window=_window(title="Untitled - Notepad", process_name="notepad.exe"),
        active_window="Untitled - Notepad",
        ocr=[TextRegion(text="Untitled - Notepad", x=0, y=0, width=120, height=14, confidence=0.93)],
    )
    intent = VerificationIntent(
        kind=VerificationKind.WINDOW_STATE,
        expected_window_title="Notepad",
        expected_process_name="notepad.exe",
        expected_text="Notepad",
    )
    result = engine.verify(intent, before, after)
    assert result.outcome == "verified"
    assert result.verification_method == "window_state"
    # Process and text intents over the same transition also verify independently.
    process_intent = VerificationIntent(kind=VerificationKind.PROCESS_STATE, expected_process_name="notepad")
    assert engine.verify(process_intent, before, after).outcome == "verified"
    text_intent = VerificationIntent(kind=VerificationKind.EXPECTED_TEXT, expected_text="notepad")
    assert engine.verify(text_intent, before, after).outcome == "verified"


# --- verification: strongly-changed-pixel criterion (thin compact changes) -------------------

def _observation_with_stroke(color: str, x0: int, y0: int, x1: int, y1: int, width: int = 640, height: int = 400) -> Observation:
    image = Image.new("RGB", (width, height), "white")
    for x in range(x0, x1):
        for y in range(y0, y1):
            image.putpixel((x, y), Image.new("RGB", (1, 1), color).getpixel((0, 0)))
    output = io.BytesIO()
    image.save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return Observation(image_base64=encoded, width=width, height=height)


def test_screenshot_diff_thin_stroke_counts_as_change() -> None:
    before = _observation("white", 640, 400)
    after = _observation_with_stroke("red", 100, 200, 300, 202)  # 200x2 stroke, mean diff ~0.05
    result = ScreenshotDiffStrategy().verify(
        VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=True), before, after
    )
    assert result.outcome == "verified" and result.changed is True
    assert any("Strongly-changed pixels" in e for e in result.evidence)


def test_screenshot_diff_sub_threshold_flicker_is_not_change() -> None:
    before = _observation("white", 640, 400)
    after = _observation_with_stroke("black", 300, 100, 308, 104)  # 32 strongly-changed pixels < 50
    failed = ScreenshotDiffStrategy().verify(
        VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=True), before, after
    )
    assert failed.outcome == "failed" and failed.changed is False
    uncertain = ScreenshotDiffStrategy().verify(
        VerificationIntent(kind=VerificationKind.VISUAL_CHANGE), before, after
    )
    assert uncertain.outcome == "uncertain"


def test_screenshot_diff_stability_fails_on_compact_change() -> None:
    before = _observation("white", 640, 400)
    after = _observation_with_stroke("black", 300, 100, 320, 104)  # 80 strongly-changed pixels
    result = ScreenshotDiffStrategy().verify(
        VerificationIntent(kind=VerificationKind.VISUAL_CHANGE, expected_change=False), before, after
    )
    assert result.outcome == "failed" and result.changed is True
