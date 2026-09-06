"""Plan validator unit tests (SubtasksProtocol section 17, "Planner" area).

The LLM plan is an UNTRUSTED suggestion: every rejection rule must be deterministic,
typed/testable, and fail-closed (a rejected plan yields NO partially accepted entries).
Pure domain tests — no network, no provider, no execution.
"""

from __future__ import annotations

import pytest

from computer_use_mcp.models import MAX_SUBTASKS, SubtaskPlan, SubtaskPlanEntry, SubtaskStatus
from computer_use_mcp.plan_validator import (
    PlanRejectedError,
    PlanValidator,
    find_cycle,
)


def _entry(subtask_id: str, description: str = "", depends_on: list[str] | None = None, **extra: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "subtask_id": subtask_id,
        "description": description or f"do {subtask_id}",
    }
    if depends_on is not None:
        entry["depends_on"] = depends_on
    entry.update(extra)
    return entry


VALID_PLAN = [
    _entry("open", "Open the workbook"),
    _entry("filter", "Filter the data", depends_on=["open"]),
    _entry("mail", "Send the email", depends_on=["filter"]),
]

ALL_REJECTION_CODES = {
    "malformed_plan",
    "malformed_subtask_entry",
    "too_many_subtasks",
    "empty_plan",
    "duplicate_subtask_id",
    "unknown_dependency",
    "self_dependency",
    "dependency_cycle",
    "invalid_status",
    "non_pending_status",
    "unsafe_content",
}


# --- valid plans --------------------------------------------------------------------------

def test_valid_list_plan_is_accepted_and_normalized() -> None:
    result = PlanValidator().validate(VALID_PLAN)
    assert result.valid is True
    assert result.codes == [] and result.reasons == []
    assert [e.subtask_id for e in result.entries] == ["filter", "mail", "open"]  # canonical id order
    assert all(e.status is SubtaskStatus.PENDING for e in result.entries)
    assert result.entries[1].depends_on == ["filter"]  # 'mail' depends on 'filter'
    assert all(isinstance(e, SubtaskPlanEntry) for e in result.entries)


def test_valid_mapping_plan_is_accepted() -> None:
    result = PlanValidator().validate({"subtasks": VALID_PLAN})
    assert result.valid is True
    assert len(result.entries) == 3


def test_valid_pydantic_plan_is_accepted() -> None:
    plan = SubtaskPlan(entries=[SubtaskPlanEntry(subtask_id="a", description="a")])
    result = PlanValidator().validate(plan)
    assert result.valid is True
    assert [e.subtask_id for e in result.entries] == ["a"]


def test_single_entry_plan_is_accepted() -> None:
    result = PlanValidator().validate([_entry("only")])
    assert result.valid is True and len(result.entries) == 1


def test_validate_or_raise_returns_entries_for_valid_plan() -> None:
    entries = PlanValidator().validate_or_raise(VALID_PLAN)
    assert [e.subtask_id for e in entries] == ["filter", "mail", "open"]


# --- malformed structure ------------------------------------------------------------------

def test_rejects_non_plan_top_level_types() -> None:
    for bad in (None, "subtasks", 42, {"subtasks": {"subtask_id": "a", "description": "a"}}):
        result = PlanValidator().validate(bad)
        assert result.valid is False
        assert result.codes == ["malformed_plan"]
        assert result.entries == []


def test_rejects_mapping_with_unknown_or_missing_keys() -> None:
    for plan in (
        {"subtasks": VALID_PLAN, "notes": "extra"},
        {"entries": VALID_PLAN},
        {},
    ):
        result = PlanValidator().validate(plan)
        assert result.valid is False and result.codes == ["malformed_plan"]


def test_rejects_malformed_entries() -> None:
    malformed = [
        "not a mapping",
        42,
        {},  # missing required keys
        {"subtask_id": "a"},  # missing description
        {"description": "no id"},
        {"subtask_id": "a", "description": "a", "unexpected": 1},  # unknown key
        {"subtask_id": "", "description": "empty id"},
        {"subtask_id": "a", "description": ""},  # empty description
        {"subtask_id": "a", "description": "   "},  # whitespace description
        {"subtask_id": "a", "description": 42},
        {"subtask_id": 42, "description": "a"},
        {"subtask_id": "a", "description": "a", "depends_on": "open"},  # not a list
        {"subtask_id": "a", "description": "a", "depends_on": [42]},
        {"subtask_id": "a", "description": "a", "depends_on": [""]},
        {"subtask_id": "a", "description": "a", "depends_on": ["open", "open"]},  # dup deps
        {"subtask_id": "a", "description": "a", "status": 7},  # non-string status
        {"subtask_id": "x" * 129, "description": "id too long"},
    ]
    for entry in malformed:
        result = PlanValidator().validate([entry])
        assert result.valid is False, entry
        assert result.codes == ["malformed_subtask_entry"], entry
        assert result.entries == []


# --- the named rejection rules ------------------------------------------------------------

def test_rejects_duplicate_subtask_ids() -> None:
    result = PlanValidator().validate([_entry("a"), _entry("a", "second one")])
    assert result.valid is False and result.codes == ["duplicate_subtask_id"]


def test_rejects_dependency_referencing_nonexistent_subtask() -> None:
    result = PlanValidator().validate([_entry("a", depends_on=["ghost"])])
    assert result.valid is False and result.codes == ["unknown_dependency"]


def test_rejects_self_dependency() -> None:
    result = PlanValidator().validate([_entry("a", depends_on=["a"])])
    # A self-dependency is also trivially a cycle; both stable codes are reported.
    assert result.valid is False
    assert "self_dependency" in result.codes and "dependency_cycle" in result.codes


def test_rejects_two_node_dependency_cycle() -> None:
    result = PlanValidator().validate([_entry("a", depends_on=["b"]), _entry("b", depends_on=["a"])])
    assert result.valid is False and result.codes == ["dependency_cycle"]


def test_rejects_three_node_dependency_cycle() -> None:
    plan = [
        _entry("a", depends_on=["c"]),
        _entry("b", depends_on=["a"]),
        _entry("c", depends_on=["b"]),
    ]
    result = PlanValidator().validate(plan)
    assert result.valid is False and result.codes == ["dependency_cycle"]
    assert "a -> c -> b -> a" in result.reasons[0] or "->" in result.reasons[0]


def test_rejects_more_than_fifty_subtasks() -> None:
    assert MAX_SUBTASKS == 50
    plan = [_entry(f"s{i}") for i in range(MAX_SUBTASKS + 1)]
    result = PlanValidator().validate(plan)
    assert result.valid is False and result.codes == ["too_many_subtasks"]
    assert result.entries == []


def test_rejects_invalid_and_non_pending_statuses() -> None:
    invalid = PlanValidator().validate([_entry("a", status="flying")])
    assert invalid.valid is False and invalid.codes == ["invalid_status"]
    fast_track = PlanValidator().validate([_entry("a", status="completed")])
    assert fast_track.valid is False and fast_track.codes == ["non_pending_status"]
    blocked = PlanValidator().validate([_entry("a", status="blocked")])
    assert blocked.valid is False and blocked.codes == ["non_pending_status"]


def test_rejects_unsafe_content_control_characters() -> None:
    result = PlanValidator().validate([_entry("a", "steal \x00 secrets")])
    assert result.valid is False and result.codes == ["unsafe_content"]


def test_rejects_empty_plans() -> None:
    for plan in ([], {"subtasks": []}):
        result = PlanValidator().validate(plan)
        assert result.valid is False and result.codes == ["empty_plan"]


def test_every_rejection_rule_is_fail_closed_via_validate_or_raise() -> None:
    cases = [
        None,
        "nope",
        {},
        "not a mapping",
        [],
        [_entry("a"), _entry("a")],
        [_entry("a", depends_on=["ghost"])],
        [_entry("a", depends_on=["a"])],
        [_entry("a", depends_on=["b"]), _entry("b", depends_on=["a"])],
        [_entry(f"s{i}") for i in range(51)],
        [_entry("a", status="flying")],
        [_entry("a", status="running")],
        [_entry("a", "bad \x1b escape")],
    ]
    for plan in cases:
        result = PlanValidator().validate(plan)
        assert result.valid is False
        assert result.entries == []  # no partial acceptance, ever
        assert set(result.codes) <= ALL_REJECTION_CODES
        assert len(result.codes) >= 1
        with pytest.raises(PlanRejectedError) as excinfo:
            PlanValidator().validate_or_raise(plan)
        assert excinfo.value.codes == tuple(result.codes)
        assert excinfo.value.reasons == tuple(result.reasons)


# --- determinism --------------------------------------------------------------------------

def test_same_input_yields_same_verdict() -> None:
    validator = PlanValidator()
    plan = [_entry("a", depends_on=["b"]), _entry("b", depends_on=["c"]), _entry("c")]
    first = validator.validate(plan)
    second = validator.validate(plan)
    assert first.model_dump() == second.model_dump()


def test_cycle_report_is_independent_of_input_order() -> None:
    validator = PlanValidator()
    forward = [_entry("a", depends_on=["b"]), _entry("b", depends_on=["c"]), _entry("c", depends_on=["a"])]
    reverse = list(reversed(forward))
    first = validator.validate(forward)
    second = validator.validate(reverse)
    assert first.codes == second.codes == ["dependency_cycle"]
    assert first.reasons == second.reasons


def test_validator_has_no_side_effects_and_accepts_repeated_calls() -> None:
    validator = PlanValidator()
    for _ in range(3):
        assert validator.validate(VALID_PLAN).valid is True
        assert validator.validate([_entry("dup"), _entry("dup")]).valid is False


# --- ceiling configuration ----------------------------------------------------------------

def test_validator_rejects_out_of_range_ceilings() -> None:
    for bad_range in (0, -1, 51):
        with pytest.raises(ValueError):
            PlanValidator(max_subtasks=bad_range)
    for bad_type in (True, 1.5, "50"):
        with pytest.raises(TypeError):
            PlanValidator(max_subtasks=bad_type)  # type: ignore[arg-type]


def test_custom_lower_ceiling_is_enforced() -> None:
    validator = PlanValidator(max_subtasks=3)
    assert validator.validate([_entry(f"s{i}") for i in range(3)]).valid is True
    result = validator.validate([_entry(f"s{i}") for i in range(4)])
    assert result.valid is False and result.codes == ["too_many_subtasks"]


# --- shared cycle finder ------------------------------------------------------------------

def test_find_cycle_deterministic_and_acyclic_safe() -> None:
    assert find_cycle({}) is None
    assert find_cycle({"a": [], "b": ["a"], "c": ["b"]}) is None
    cycle = find_cycle({"a": ["c"], "b": ["a"], "c": ["b"]})
    assert cycle == ("a", "c", "b", "a")  # sorted start node, sorted neighbors
    assert find_cycle({"a": ["b"], "b": ["a"]}) == ("a", "b", "a")
    # Dependencies outside the node set are ignored here (unknown-dep rule is separate).
    assert find_cycle({"a": ["ghost"]}) is None
