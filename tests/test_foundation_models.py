"""Wave 1 foundation tests: models.py backward compatibility + new contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from computer_use_mcp.models import (
    ActionType,
    AgentDecision,
    CoordinateSpace,
    ExecutionResult,
    FailureClass,
    GroundedAction,
    GroundingResult,
    GroundingValidation,
    MonitorInfo,
    Observation,
    Point,
    RiskLevel,
    SessionState,
    TerminationReason,
    TextRegion,
    VerificationResult,
    WindowInfo,
)

# --- backward compatibility: every existing model constructs exactly as before -----------

def test_action_type_members_unchanged() -> None:
    # "drag" was added additively (press-move-release); "move", "hotkey", and
    # "focus_window" were added additively the same way — the legacy members are unchanged.
    assert {member.value for member in ActionType} == {
        "click",
        "double_click",
        "drag",
        "type",
        "keypress",
        "scroll",
        "wait",
        "done",
        "move",
        "hotkey",
        "focus_window",
    }


def test_point_accepts_legacy_non_negative_coordinates() -> None:
    point = Point(x=10, y=20)
    assert (point.x, point.y) == (10, 20)


def test_point_allows_negative_multi_monitor_coordinates() -> None:
    point = Point(x=-1920, y=-100)
    assert (point.x, point.y) == (-1920, -100)


def test_point_rejects_out_of_bounds_coordinates() -> None:
    with pytest.raises(ValidationError):
        Point(x=17_000, y=0)
    with pytest.raises(ValidationError):
        Point(x=0, y=-9_000)


def test_grounded_action_legacy_construction_and_defaults() -> None:
    action = GroundedAction(action="click", point={"x": 10, "y": 20})
    assert action.action is ActionType.CLICK
    assert action.point == Point(x=10, y=20)
    assert action.text is None
    assert action.keys == []
    assert action.delta == 0
    assert action.reason == ""
    assert action.confidence == 0.0


def test_grounded_action_existing_constraints_preserved() -> None:
    with pytest.raises(ValidationError):
        GroundedAction(action="type", text="x" * 2_001)
    with pytest.raises(ValidationError):
        GroundedAction(action="keypress", keys=[str(i) for i in range(13)])
    with pytest.raises(ValidationError):
        GroundedAction(action="scroll", delta=21)
    with pytest.raises(ValidationError):
        GroundedAction(action="click", confidence=1.5)


def test_grounded_action_new_field_defaults() -> None:
    first = GroundedAction(action="wait", delta=1)
    second = GroundedAction(action="wait", delta=1)
    assert first.source_observation_id is None
    assert first.expected_effect is None
    assert first.risk is None
    assert first.grounding is None
    assert first.action_id
    assert first.action_id != second.action_id


def test_grounded_action_new_fields_settable() -> None:
    grounding = GroundingResult(strategy="coordinate", confidence=0.9, evidence=["save button"])
    action = GroundedAction(
        action="click",
        point={"x": 5, "y": 6},
        source_observation_id="obs123",
        expected_effect="Save dialog opens",
        risk=RiskLevel.MEDIUM,
        grounding=grounding,
    )
    assert action.source_observation_id == "obs123"
    assert action.expected_effect == "Save dialog opens"
    assert action.risk is RiskLevel.MEDIUM
    assert action.grounding is not None and action.grounding.strategy == "coordinate"


def test_observation_legacy_construction() -> None:
    observation = Observation(
        image_base64="abc",
        width=1920,
        height=1080,
        input_width=1536,
        input_height=864,
        coordinate_scale_x=0.8,
        coordinate_scale_y=0.8,
        coordinate_space_verified=False,
    )
    assert observation.coordinate_space_verified is False
    assert observation.redactions_applied is False
    assert observation.coordinate_scale_x == 0.8


def test_observation_existing_constraints_preserved() -> None:
    with pytest.raises(ValidationError):
        Observation(image_base64="abc", width=0, height=10)
    with pytest.raises(ValidationError):
        Observation(image_base64="abc", width=10, height=10, coordinate_scale_x=0.0)


def test_observation_cursor_allows_negative_multi_monitor_origins() -> None:
    # Commander-authorized W1-fix: cursor bounds relaxed from ge=0 to match Point.
    observation = Observation(image_base64="abc", width=100, height=100, cursor_x=-1920, cursor_y=-100)
    assert (observation.cursor_x, observation.cursor_y) == (-1920, -100)
    legacy = Observation(image_base64="abc", width=100, height=100, cursor_x=500, cursor_y=300)
    assert (legacy.cursor_x, legacy.cursor_y) == (500, 300)


def test_observation_cursor_stays_bounds_checked() -> None:
    with pytest.raises(ValidationError):
        Observation(image_base64="abc", width=100, height=100, cursor_x=-9_000)
    with pytest.raises(ValidationError):
        Observation(image_base64="abc", width=100, height=100, cursor_y=17_000)


def test_observation_new_field_defaults() -> None:
    first = Observation(image_base64="abc", width=10, height=10)
    second = Observation(image_base64="abc", width=10, height=10)
    assert first.observation_id
    assert first.observation_id != second.observation_id
    assert first.timestamp.tzinfo is not None
    assert first.timestamp.utcoffset() is not None
    assert first.monitor is None
    assert first.active_window_info is None
    assert first.ocr_text is None
    assert first.ui_elements is None


def test_observation_coordinate_space_sync_from_bool_scaled() -> None:
    observation = Observation(
        image_base64="abc",
        width=1920,
        height=1080,
        input_width=1536,
        input_height=864,
        coordinate_scale_x=0.8,
        coordinate_scale_y=0.8,
        coordinate_space_verified=False,
    )
    assert observation.coordinate_space is CoordinateSpace.SCALED
    assert observation.coordinate_space_verified is False


def test_observation_coordinate_space_sync_from_bool_unverifiable() -> None:
    observation = Observation(image_base64="abc", width=10, height=10, coordinate_space_verified=False)
    assert observation.coordinate_space is CoordinateSpace.UNVERIFIABLE
    assert observation.coordinate_space_verified is False


def test_observation_coordinate_space_enum_is_authoritative() -> None:
    unverifiable = Observation(image_base64="abc", width=10, height=10, coordinate_space="unverifiable")
    assert unverifiable.coordinate_space_verified is False
    scaled = Observation(image_base64="abc", width=10, height=10, coordinate_space="scaled")
    assert scaled.coordinate_space_verified is True


def test_observation_enum_wins_over_conflicting_bool() -> None:
    observation = Observation(
        image_base64="abc",
        width=10,
        height=10,
        coordinate_space_verified=True,
        coordinate_space="unverifiable",
    )
    assert observation.coordinate_space is CoordinateSpace.UNVERIFIABLE
    assert observation.coordinate_space_verified is False


def test_observation_default_is_verified_passthrough() -> None:
    observation = Observation(image_base64="abc", width=10, height=10)
    assert observation.coordinate_space is CoordinateSpace.VERIFIED_PASSTHROUGH
    assert observation.coordinate_space_verified is True


def test_observation_monitor_window_and_ocr() -> None:
    monitor = MonitorInfo(
        id="\\\\.\\DISPLAY1",
        index=0,
        bounds=(0, 0, 1920, 1080),
        is_primary=True,
        dpi_scale_x=1.25,
        dpi_scale_y=1.25,
    )
    window = WindowInfo(
        hwnd=1234,
        pid=5678,
        process_name="notepad.exe",
        exe_path="C:\\Windows\\notepad.exe",
        window_class="Notepad",
        title="Untitled - Notepad",
        bounds=(-1920, 0, 800, 600),
    )
    region = TextRegion(text="Save", x=10, y=20, width=50, height=12, confidence=0.9)
    observation = Observation(
        image_base64="abc",
        width=1920,
        height=1080,
        monitor=monitor,
        active_window_info=window,
        ocr_text=[region],
        ui_elements=[{"role": "button", "name": "Save"}],
    )
    assert observation.monitor is not None and observation.monitor.is_primary is True
    assert observation.active_window_info is not None
    assert observation.active_window_info.process_name == "notepad.exe"
    assert observation.ocr_text is not None and observation.ocr_text[0].text == "Save"
    assert observation.ui_elements is not None


def test_monitorinfo_rejects_non_positive_dpi_scale() -> None:
    with pytest.raises(ValidationError):
        MonitorInfo(id="m0", index=0, bounds=(0, 0, 100, 100), dpi_scale_x=0.0)


def test_textregion_rejects_bad_confidence_and_bounds() -> None:
    with pytest.raises(ValidationError):
        TextRegion(text="x", x=0, y=0, width=1, height=1, confidence=1.5)
    with pytest.raises(ValidationError):
        TextRegion(text="x", x=0, y=0, width=0, height=1)


def test_groundingresult_defaults() -> None:
    result = GroundingResult(strategy="coordinate", confidence=0.8)
    assert result.evidence == []
    assert result.normalized is False
    assert result.notes is None
    with pytest.raises(ValidationError):
        GroundingResult(strategy="coordinate", confidence=1.2)


# --- VerificationResult: dual derivation -------------------------------------------------

def test_verificationresult_legacy_verified_true_maps_to_outcome() -> None:
    result = VerificationResult(verified=True, changed=False, note="done", confidence=0.8)
    assert result.verified is True
    assert result.outcome == "verified"


def test_verificationresult_legacy_verified_false_maps_to_outcome() -> None:
    result = VerificationResult(verified=False, changed=True, note="dim changed", confidence=0.5)
    assert result.verified is False
    assert result.outcome == "failed"


def test_verificationresult_outcome_uncertain_is_never_success() -> None:
    uncertain = VerificationResult(changed=True, note="unclear")
    assert uncertain.outcome == "uncertain"
    assert uncertain.verified is False
    explicit = VerificationResult(outcome="uncertain", changed=True, note="unclear")
    assert explicit.verified is False


def test_verificationresult_outcome_is_authoritative_over_verified() -> None:
    result = VerificationResult(verified=True, outcome="failed", changed=True, note="mismatch")
    assert result.outcome == "failed"
    assert result.verified is False


def test_verificationresult_new_fields_and_dump_round_trip() -> None:
    result = VerificationResult(
        outcome="verified",
        changed=True,
        note="window title changed",
        confidence=0.95,
        evidence=["title now 'Untitled - Notepad'"],
        verification_method="window_state",
        observation_id="obs_after",
    )
    assert result.evidence == ["title now 'Untitled - Notepad'"]
    assert result.verification_method == "window_state"
    assert result.observation_id == "obs_after"
    dumped = result.model_dump()
    assert dumped["verified"] is True
    assert dumped["outcome"] == "verified"
    rebuilt = VerificationResult.model_validate(dumped)
    assert rebuilt.verified is True and rebuilt.outcome == "verified"


# --- enums pinned by the mission contract -----------------------------------------------

def test_risk_level_members() -> None:
    assert {member.value for member in RiskLevel} == {"low", "medium", "high", "critical"}


def test_failure_class_has_exactly_the_pinned_13_values() -> None:
    # 13 values since master-mission 003 (conflict C8): additive FailureClass.SUBTASK_FAILED
    # for whole-subtask failure after bounded in-subtask recovery; all 12 original members
    # and their order are untouched.
    assert {member.name for member in FailureClass} == {
        "STALE_COORDINATES",
        "MOVED_UI",
        "WRONG_WINDOW",
        "UNEXPECTED_DIALOG",
        "BLOCKED_UI",
        "NAVIGATION_DRIFT",
        "APP_CRASH",
        "AUTH_REQUIRED",
        "ALREADY_COMPLETED",
        "UNRECOVERABLE",
        "LOW_CONFIDENCE",
        "UNKNOWN",
        "SUBTASK_FAILED",
    }
    assert len(FailureClass) == 13


def test_termination_reason_members() -> None:
    assert {member.value for member in TerminationReason} == {
        "completed",
        "failed_verification",
        "blocked_safety",
        "approval_exhausted",
        "limit_exceeded",
        "stopped_by_user",
        "unrecoverable",
        "provider_error",
    }


# --- remaining pre-existing models still construct unchanged -----------------------------

def test_remaining_existing_models_backward_compatible() -> None:
    validation = GroundingValidation(valid=False, reasons=["outside"])
    assert validation.valid is False
    execution = ExecutionResult(ok=True, action=GroundedAction(action="wait"), message="ok")
    assert execution.retry_count == 0
    state = SessionState(session_id="s", max_retries_per_action=1)
    assert state.dry_run is True and state.stopped is False
    decision = AgentDecision(status="done", summary="done")
    assert decision.action is None
    with pytest.raises(ValidationError):
        AgentDecision(status="nonsense")
