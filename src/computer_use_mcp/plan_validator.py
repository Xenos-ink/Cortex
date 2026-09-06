"""Deterministic, fail-closed validation of LLM-proposed subtask plans.

Layering (master-mission 003 section 5): this module imports only ``models``. It is a PURE
validation layer: no I/O, no execution, no side effects, no clock reads — the same input
always produces the same verdict.

Doctrine (SubtasksProtocol section 2): an LLM plan is an UNTRUSTED SUGGESTION, never a
source of truth. A plan is accepted only when every deterministic rule passes; ANY
violation rejects the WHOLE plan (no partial acceptance). The planner can never use a plan
to bypass dependency validation, resource limits, or safety policy — those live in other
layers and are never influenced by plan contents.

Stable rejection codes (machine-testable, one per protocol rule):
- ``malformed_plan``         — top-level structure is not a plan (wrong type/keys/shape).
- ``malformed_subtask_entry``— an entry has wrong types, missing/unknown fields, or an
                               empty description/id.
- ``too_many_subtasks``      — more than ``max_subtasks`` (hard ceiling 50) entries.
- ``empty_plan``             — a plan proposing zero subtasks cannot be executed.
- ``duplicate_subtask_id``   — the same ``subtask_id`` appears more than once.
- ``unknown_dependency``     — a dependency references a subtask not present in the plan.
- ``self_dependency``        — a subtask depends on itself.
- ``dependency_cycle``       — the dependency graph contains a cycle.
- ``invalid_status``         — an entry carries a status value that is not a
                               :class:`~computer_use_mcp.models.SubtaskStatus` member.
- ``non_pending_status``     — an entry carries a valid status other than ``pending``
                               (a plan can never fast-track work past execution).
- ``unsafe_content``         — id/description/dependency text contains control characters
                               (data that cannot be executed/safely surfaced).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

from .models import MAX_SUBTASKS, SubtaskPlan, SubtaskPlanEntry, SubtaskStatus

__all__ = [
    "PlanRejectedError",
    "PlanValidationResult",
    "PlanValidator",
    "contains_control_characters",
    "find_cycle",
]

#: Only legitimate initial status for a planned subtask; everything else is rejected.
_PLANNABLE_STATUSES = frozenset({SubtaskStatus.PENDING})

_ENTRY_REQUIRED_KEYS = frozenset({"subtask_id", "description"})
_ENTRY_OPTIONAL_KEYS = frozenset({"depends_on", "status"})
_ENTRY_ALLOWED_KEYS = _ENTRY_REQUIRED_KEYS | _ENTRY_OPTIONAL_KEYS

_ID_MAX_LENGTH = 128
_DESCRIPTION_MAX_LENGTH = 2_000


class PlanRejectedError(RuntimeError):
    """Raised by :meth:`PlanValidator.validate_or_raise` when a plan fails any rule.

    Attributes:
        codes: Stable machine-readable rejection codes (deterministic order).
        reasons: Human-readable explanations (deterministic order; safe to surface).
    """

    def __init__(self, codes: Sequence[str], reasons: Sequence[str]) -> None:
        super().__init__("LLM plan rejected (fail-closed): " + "; ".join(reasons))
        self.codes = tuple(codes)
        self.reasons = tuple(reasons)


class PlanValidationResult(BaseModel):
    """Outcome of deterministic plan validation (mirrors ``ValidationOutcome`` idiom).

    ``entries`` is populated ONLY when ``valid`` is True — a rejected plan never yields a
    partially accepted subset (fail closed).
    """

    valid: bool
    reasons: list[str] = Field(default_factory=list)
    codes: list[str] = Field(default_factory=list)
    entries: list[SubtaskPlanEntry] = Field(default_factory=list)


def find_cycle(depends_on_by_id: Mapping[str, Iterable[str]]) -> tuple[str, ...] | None:
    """Return one dependency cycle as ``(a, ..., a)`` or ``None`` when the graph is acyclic.

    Deterministic: start nodes and neighbor sets are visited in sorted order, so the same
    graph always yields the same cycle. Dependencies referencing ids outside the node set
    are ignored here (callers enforce the unknown-dependency rule separately).
    """
    color: dict[str, int] = {node: 0 for node in depends_on_by_id}  # 0 white, 1 gray, 2 black
    for start in sorted(color):
        if color[start] != 0:
            continue
        color[start] = 1
        path = [start]
        stack = [iter(sorted(depends_on_by_id[start]))]
        while stack:
            advanced = False
            for dep in stack[-1]:
                if dep not in color:
                    continue
                if color[dep] == 1:
                    return tuple(path[path.index(dep):]) + (dep,)
                if color[dep] == 0:
                    color[dep] = 1
                    path.append(dep)
                    stack.append(iter(sorted(depends_on_by_id[dep])))
                    advanced = True
                    break
            if not advanced:
                color[path.pop()] = 2
                stack.pop()
    return None


def contains_control_characters(text: str) -> bool:
    """True when ``text`` contains C0 control characters or DEL (never safely surfaceable)."""
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)


def _valid_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= _ID_MAX_LENGTH
        and not contains_control_characters(value)
    )


class PlanValidator:
    """Validates an untrusted LLM plan against the deterministic rejection rules.

    Args:
        max_subtasks: Maximum number of entries accepted (clamped domain ceiling: 1..50).
    """

    def __init__(self, max_subtasks: int = MAX_SUBTASKS) -> None:
        if not isinstance(max_subtasks, int) or isinstance(max_subtasks, bool):
            raise TypeError("max_subtasks must be an int")
        if not 1 <= max_subtasks <= MAX_SUBTASKS:
            raise ValueError(f"max_subtasks must be within 1..{MAX_SUBTASKS}")
        self.max_subtasks = max_subtasks

    # -- public API ------------------------------------------------------------------------

    def validate(self, plan: Any) -> PlanValidationResult:
        """Deterministically validate ``plan``; never raises, never executes anything."""
        reasons: list[str] = []
        codes: set[str] = set()

        raw_entries = self._extract_raw_entries(plan, reasons, codes)
        if raw_entries is None:
            return self._rejected(reasons, codes)
        if len(raw_entries) > self.max_subtasks:
            # Fail fast on oversize plans: bounded work, no per-entry diagnostics.
            reasons.append(f"plan proposes {len(raw_entries)} subtasks; maximum is {self.max_subtasks}")
            return self._rejected(reasons, {"too_many_subtasks"})
        if not raw_entries:
            reasons.append("plan proposes zero subtasks; nothing to execute")
            return self._rejected(reasons, {"empty_plan"})

        # Phase 1: per-entry structure (input order; all entries diagnosed).
        structured: list[dict[str, Any]] = []
        for index, entry in enumerate(raw_entries):
            structured_entry = self._structure_entry(index, entry, reasons, codes)
            if structured_entry is not None:
                structured.append(structured_entry)

        # Phase 2: cross-entry semantics over structurally valid entries only.
        by_id: dict[str, dict[str, Any]] = {}
        for entry in structured:
            entry_id = entry["subtask_id"]
            if entry_id in by_id:
                codes.add("duplicate_subtask_id")
                reasons.append(f"duplicate subtask_id {entry_id!r}")
                continue
            by_id[entry_id] = entry

        for entry_id in sorted(by_id):
            entry = by_id[entry_id]
            for dep in entry["depends_on"]:
                if dep == entry_id:
                    codes.add("self_dependency")
                    reasons.append(f"subtask {entry_id!r} depends on itself")
                elif dep not in by_id:
                    codes.add("unknown_dependency")
                    reasons.append(
                        f"subtask {entry_id!r} depends on unknown subtask {dep!r}"
                    )

        graph = {entry_id: set(entry["depends_on"]) for entry_id, entry in by_id.items()}
        cycle = find_cycle(graph)
        if cycle is not None:
            codes.add("dependency_cycle")
            reasons.append("dependency cycle: " + " -> ".join(cycle))

        if codes:
            return self._rejected(reasons, codes)

        entries = [
            SubtaskPlanEntry(
                subtask_id=entry["subtask_id"],
                description=entry["description"],
                depends_on=list(entry["depends_on"]),
                status=entry["status"],
            )
            for entry_id, entry in sorted(by_id.items())
        ]
        return PlanValidationResult(valid=True, reasons=[], codes=[], entries=entries)

    def validate_or_raise(self, plan: Any) -> list[SubtaskPlanEntry]:
        """Validate and return the normalized entries; raise :class:`PlanRejectedError`."""
        result = self.validate(plan)
        if not result.valid:
            raise PlanRejectedError(result.codes, result.reasons)
        return list(result.entries)

    # -- internals -------------------------------------------------------------------------

    @staticmethod
    def _rejected(reasons: list[str], codes: set[str]) -> PlanValidationResult:
        ordered_codes = sorted(codes)
        return PlanValidationResult(valid=False, reasons=reasons, codes=ordered_codes, entries=[])

    def _extract_raw_entries(self, plan: Any, reasons: list[str], codes: set[str]) -> list[Any] | None:
        """Normalize the top level to a list of raw entries; None when irrecoverably malformed."""
        if isinstance(plan, SubtaskPlan):
            return list(plan.entries)
        if isinstance(plan, dict):
            if set(plan) != {"subtasks"}:
                codes.add("malformed_plan")
                reasons.append("plan mapping must have exactly the key 'subtasks'")
                return None
            entries = plan["subtasks"]
        elif isinstance(plan, (list, tuple)):
            entries = plan
        else:
            codes.add("malformed_plan")
            reasons.append(f"plan must be a mapping with 'subtasks' or a list; got {type(plan).__name__}")
            return None
        if not isinstance(entries, list):
            codes.add("malformed_plan")
            reasons.append("'subtasks' must be a list of subtask entries")
            return None
        return entries

    def _structure_entry(
        self, index: int, entry: Any, reasons: list[str], codes: set[str]
    ) -> dict[str, Any] | None:
        """Validate one raw entry's structure; deterministic per-entry diagnostics."""
        label = f"subtask entry #{index}"
        if isinstance(entry, SubtaskPlanEntry):
            entry = entry.model_dump()
        if not isinstance(entry, dict):
            codes.add("malformed_subtask_entry")
            reasons.append(f"{label} must be a mapping; got {type(entry).__name__}")
            return None

        keys = set(entry)
        missing = _ENTRY_REQUIRED_KEYS - keys
        unknown = keys - _ENTRY_ALLOWED_KEYS
        if missing or unknown:
            codes.add("malformed_subtask_entry")
            detail = []
            if missing:
                detail.append(f"missing required keys {sorted(missing)}")
            if unknown:
                detail.append(f"unknown keys {sorted(unknown)}")
            reasons.append(f"{label}: " + "; ".join(detail))
            return None

        subtask_id = entry["subtask_id"]
        description = entry["description"]
        depends_on = entry.get("depends_on", [])
        status = entry.get("status", SubtaskStatus.PENDING)
        valid = True

        if not _valid_id(subtask_id):
            codes.add("malformed_subtask_entry")
            reasons.append(f"{label}: 'subtask_id' must be a non-empty string of at most {_ID_MAX_LENGTH} characters without control characters")
            valid = False
        if (
            not isinstance(description, str)
            or not description.strip()
            or len(description) > _DESCRIPTION_MAX_LENGTH
            or contains_control_characters(description)
        ):
            if isinstance(description, str) and contains_control_characters(description):
                codes.add("unsafe_content")
                reasons.append(f"{label}: 'description' contains control characters")
            else:
                codes.add("malformed_subtask_entry")
                reasons.append(f"{label}: 'description' must be a non-empty string of at most {_DESCRIPTION_MAX_LENGTH} characters")
            valid = False

        if not isinstance(depends_on, (list, tuple)):
            codes.add("malformed_subtask_entry")
            reasons.append(f"{label}: 'depends_on' must be a list of subtask ids")
            valid = False
        else:
            deps: list[str] = []
            for dep in depends_on:
                if not _valid_id(dep):
                    codes.add("malformed_subtask_entry")
                    reasons.append(f"{label}: each dependency must be a non-empty string of at most {_ID_MAX_LENGTH} characters without control characters")
                    valid = False
                    break
                if dep in deps:
                    codes.add("malformed_subtask_entry")
                    reasons.append(f"{label}: duplicate dependency {dep!r}")
                    valid = False
                    break
                deps.append(dep)
            depends_on = deps

        if not isinstance(status, str):
            codes.add("malformed_subtask_entry")
            reasons.append(f"{label}: 'status' must be a string when present")
            valid = False
        else:
            try:
                status_value = SubtaskStatus(status)
            except ValueError:
                codes.add("invalid_status")
                reasons.append(f"{label}: unknown status {status!r}")
                status_value = None
            if status_value is not None and status_value not in _PLANNABLE_STATUSES:
                codes.add("non_pending_status")
                reasons.append(
                    f"{label}: status {status!r} is not plannable; planned subtasks start as 'pending'"
                )
                status_value = None
            if status_value is None:
                valid = False
            else:
                status = status_value

        if not valid:
            return None
        return {
            "subtask_id": subtask_id,
            "description": description,
            "depends_on": list(depends_on),
            "status": status,
        }
